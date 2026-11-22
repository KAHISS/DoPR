import asyncio
import json
import os
import subprocess
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen

from gitingest.utils.auth import resolve_token
from gitingest.utils.git_utils import fetch_remote_branches_or_tags


_MAX_COMPARE_RESPONSE_BYTES = 20 * 1024 * 1024


def _github_repository_coordinates(repository_url: str) -> tuple[str, str]:
    parsed_url = urlparse(repository_url)
    if (
        parsed_url.scheme not in {"http", "https"}
        or parsed_url.hostname not in {"github.com", "www.github.com"}
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise ValueError("Informe uma URL válida de repositório do GitHub.")
    repository_parts = [
        unquote(part)
        for part in parsed_url.path.strip("/").split("/")
        if part
    ]
    if len(repository_parts) != 2 or any(
        not part or any(ord(char) < 32 for char in part)
        for part in repository_parts
    ):
        raise ValueError("Informe a URL raiz do repositório, sem caminho de branch.")

    owner, repository = repository_parts
    if repository.endswith(".git"):
        repository = repository[:-4]
    if not owner or not repository:
        raise ValueError("A URL não contém um proprietário e repositório válidos.")
    return owner, repository


def get_repository_branches(repository_url: str) -> list[str]:
    token = resolve_token(os.getenv("GITHUB_TOKEN"))
    return asyncio.run(
        fetch_remote_branches_or_tags(
            repository_url,
            ref_type="branches",
            token=token,
        )
    )


def get_code_changes(
    repository_url: str,
    target_branch: str,
    source_branch: str,
    github_token: str | None = None,
    timeout: int = 300,
) -> str:
    owner, repository = _github_repository_coordinates(repository_url)
    comparison = f"{quote(target_branch, safe='')}...{quote(source_branch, safe='')}"
    endpoint = (
        f"https://api.github.com/repos/{quote(owner, safe='')}/"
        f"{quote(repository, safe='')}/compare/{comparison}?per_page=100"
    )
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "do-pr-description-generator",
    }
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    request = Request(endpoint, headers=headers)

    try:
        with urlopen(request, timeout=timeout) as response:
            response_body = response.read(_MAX_COMPARE_RESPONSE_BYTES + 1)
        if len(response_body) > _MAX_COMPARE_RESPONSE_BYTES:
            raise RuntimeError(
                "A resposta completa do diff do GitHub excede 20 MB. "
                "Tente comparar branches com menos alterações."
            )
        comparison_data: Any = json.loads(response_body.decode("utf-8"))
    except HTTPError as error:
        details = error.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"GitHub retornou HTTP {error.code} ao buscar o diff: "
            f"{details or error.reason}"
        ) from error
    except (URLError, TimeoutError) as error:
        raise RuntimeError(f"Não foi possível buscar o diff no GitHub: {error}") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("O GitHub retornou uma resposta de diff inválida.") from error

    if not isinstance(comparison_data, dict):
        raise RuntimeError("O GitHub retornou dados de comparação inválidos.")
    files = comparison_data.get("files")
    if not isinstance(files, list):
        raise RuntimeError("A resposta do GitHub não contém a lista de arquivos alterados.")

    changed_files = [
        file_data
        for file_data in files
        if isinstance(file_data, dict)
        and isinstance(file_data.get("filename"), str)
    ]
    if not changed_files:
        return "Nenhum arquivo alterado foi encontrado entre as branches."

    diff_sections = [
        (
            f"### {file_data['filename']} "
            f"({file_data.get('status', 'alterado')}, "
            f"+{file_data.get('additions', 0)}/"
            f"-{file_data.get('deletions', 0)})\n"
            f"{file_data.get('patch') or '[Patch textual indisponível.]'}"
        )
        for file_data in changed_files
    ]
    file_limit_note = (
        "Aviso: o GitHub retornou o limite de 300 arquivos na comparação; "
        "a lista pode estar incompleta.\n\n"
        if len(files) >= 300
        else ""
    )
    return (
        f"{file_limit_note}Arquivos alterados: {len(changed_files)}\n\n"
        + "\n\n".join(diff_sections)
    )


def create_pull_request(
    repository_url: str,
    source_branch: str,
    target_branch: str,
    title: str,
    body: str,
) -> str:
    owner, repository = _github_repository_coordinates(repository_url)
    source_branch = source_branch.strip()
    target_branch = target_branch.strip()
    if not source_branch or not target_branch:
        raise ValueError("Informe as branches de origem e destino.")
    if source_branch == target_branch:
        raise ValueError("As branches de origem e destino devem ser diferentes.")
    if any(ord(char) < 32 for char in source_branch + target_branch):
        raise ValueError("Os nomes das branches contêm caracteres inválidos.")
    if (
        not title.strip()
        or len(title) > 256
        or any(ord(char) < 32 for char in title)
    ):
        raise ValueError("O título do PR deve ter entre 1 e 256 caracteres.")
    if not body.strip() or len(body) > 50_000:
        raise ValueError("O body do PR deve ter entre 1 e 50000 caracteres.")

    endpoint = (
        f"repos/{quote(owner, safe='')}/{quote(repository, safe='')}/pulls"
    )
    payload = json.dumps(
        {
            "title": title.strip(),
            "head": source_branch,
            "base": target_branch,
            "body": body.strip(),
        }
    )
    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                "--method",
                "POST",
                endpoint,
                "--input",
                "-",
                "--jq",
                ".html_url",
            ],
            input=payload,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except FileNotFoundError as error:
        raise RuntimeError(
            "GitHub CLI (gh) não encontrado. Instale o GitHub CLI e autentique "
            "com `gh auth login` antes de criar o PR."
        ) from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("O GitHub CLI excedeu o tempo limite ao criar o PR.") from error

    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            f"Não foi possível criar o PR pelo GitHub CLI: "
            f"{details or 'erro desconhecido'}"
        )
    pull_request_url = result.stdout.strip()
    if not pull_request_url.startswith("https://github.com/"):
        raise RuntimeError(
            "O GitHub CLI não retornou a URL do pull request criado."
        )
    return pull_request_url
