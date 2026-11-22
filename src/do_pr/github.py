import asyncio
import json
import os
import subprocess
from typing import Any, Callable
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
        try:
            error_data = json.loads(details)
        except json.JSONDecodeError:
            error_data = None
        if (
            error.code == 404
            and isinstance(error_data, dict)
            and isinstance(error_data.get("message"), str)
            and error_data["message"].startswith("No common ancestor between ")
        ):
            raise RuntimeError(
                "O GitHub não permite comparar essas branches nem abrir um PR "
                "porque elas têm históricos Git independentes e não compartilham "
                "um commit ancestral. Crie uma nova branch a partir da branch de "
                "destino e reaplique nela as alterações da feature; depois envie "
                "essa branch e tente novamente."
            ) from error
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


def _run_github_cli(
    arguments: list[str],
    input_text: str | None = None,
    timeout: int = 60,
) -> str:
    try:
        result = subprocess.run(
            ["gh", *arguments],
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as error:
        raise RuntimeError(
            "GitHub CLI (gh) não encontrado. Instale o GitHub CLI e autentique "
            "com `gh auth login` antes de continuar."
        ) from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("O GitHub CLI excedeu o tempo limite.") from error

    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            f"GitHub CLI retornou erro: {details or 'erro desconhecido'}"
        )
    return result.stdout.strip()


def _github_api_list(endpoint: str) -> list[dict[str, Any]]:
    response = _run_github_cli(
        ["api", "--paginate", "--slurp", endpoint],
    )
    try:
        pages: Any = json.loads(response)
    except json.JSONDecodeError as error:
        raise RuntimeError("O GitHub CLI retornou uma lista inválida.") from error
    if not isinstance(pages, list) or any(
        not isinstance(page, list) for page in pages
    ) or any(
        not isinstance(item, dict)
        for page in pages
        for item in page
    ):
        raise RuntimeError("O GitHub CLI retornou uma lista inválida.")
    return [item for page in pages for item in page]


def get_pull_request_options(repository_url: str) -> dict[str, Any]:
    owner, repository = _github_repository_coordinates(repository_url)
    repository_path = (
        f"repos/{quote(owner, safe='')}/{quote(repository, safe='')}"
    )
    options: dict[str, Any] = {
        "assignees": [],
        "reviewers": [],
        "labels": [],
        "projects": [],
        "errors": {},
    }

    def load_option(name: str, load: Callable[[], list[str]]) -> None:
        try:
            options[name] = load()
        except (RuntimeError, ValueError, TypeError) as error:
            options["errors"][name] = str(error)

    def get_assignees() -> list[str]:
        users = _github_api_list(f"{repository_path}/assignees?per_page=100")
        return sorted(
            {
                user["login"]
                for user in users
                if isinstance(user.get("login"), str)
            },
            key=str.casefold,
        )

    def get_reviewers() -> list[str]:
        users = _github_api_list(f"{repository_path}/collaborators?per_page=100")
        return sorted(
            {
                user["login"]
                for user in users
                if isinstance(user.get("login"), str)
                and (
                    not isinstance(user.get("permissions"), dict)
                    or any(
                        user["permissions"].get(permission) is True
                        for permission in ("push", "maintain", "admin")
                    )
                )
            },
            key=str.casefold,
        )

    def get_labels() -> list[str]:
        labels = _github_api_list(f"{repository_path}/labels?per_page=100")
        return sorted(
            {
                label["name"]
                for label in labels
                if isinstance(label.get("name"), str)
            },
            key=str.casefold,
        )

    def get_projects() -> list[str]:
        response = _run_github_cli(
            [
                "project",
                "list",
                "--owner",
                owner,
                "--limit",
                "100",
                "--format",
                "json",
            ]
        )
        try:
            projects: Any = json.loads(response)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                "O GitHub CLI retornou uma lista de projetos inválida."
            ) from error
        if isinstance(projects, dict):
            projects = projects.get("projects")
        if not isinstance(projects, list):
            raise RuntimeError("O GitHub CLI retornou uma lista de projetos inválida.")
        return sorted(
            {
                project["title"]
                for project in projects
                if isinstance(project, dict)
                and isinstance(project.get("title"), str)
                and project.get("closed") is not True
            },
            key=str.casefold,
        )

    load_option("assignees", get_assignees)
    load_option("reviewers", get_reviewers)
    load_option("labels", get_labels)
    load_option("projects", get_projects)
    return options


def _validate_names(name: str, values: list[str] | None) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > 100:
        raise ValueError(f"A seleção de {name} deve conter até 100 itens.")
    result: list[str] = []
    for value in values:
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 256
            or any(ord(char) < 32 for char in value)
        ):
            raise ValueError(f"A seleção de {name} contém um valor inválido.")
        cleaned_value = value.strip()
        if cleaned_value not in result:
            result.append(cleaned_value)
    return result


def create_pull_request(
    repository_url: str,
    source_branch: str,
    target_branch: str,
    title: str,
    body: str,
    assignees: list[str] | None = None,
    reviewers: list[str] | None = None,
    labels: list[str] | None = None,
    project: str | None = None,
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
    assignees = _validate_names("responsáveis", assignees)
    reviewers = _validate_names("revisores", reviewers)
    labels = _validate_names("labels", labels)
    if project is not None:
        if (
            not isinstance(project, str)
            or len(project) > 256
            or any(ord(char) < 32 for char in project)
        ):
            raise ValueError("O projeto selecionado é inválido.")
        project = project.strip() or None

    arguments = [
        "pr",
        "create",
        "--repo",
        f"{owner}/{repository}",
        "--head",
        source_branch,
        "--base",
        target_branch,
        "--title",
        title.strip(),
        "--body-file",
        "-",
    ]
    for assignee in assignees:
        arguments.extend(["--assignee", assignee])
    for reviewer in reviewers:
        arguments.extend(["--reviewer", reviewer])
    for label in labels:
        arguments.extend(["--label", label])
    if project:
        arguments.extend(["--project", project])

    result = _run_github_cli(arguments, input_text=body.strip())
    pull_request_url = result.splitlines()[-1] if result else ""
    if not pull_request_url.startswith("https://github.com/"):
        raise RuntimeError(
            "O GitHub CLI não retornou a URL do pull request criado."
        )
    return pull_request_url
