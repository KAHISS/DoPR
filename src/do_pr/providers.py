import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .models import PullRequestDraft


def llm_chat(
    messages: list[dict[str, str]],
    provider: str,
    model: str,
    base_url: str,
    timeout: int,
    num_ctx: int,
    num_predict: int = 1536,
    api_key: str | None = None,
    json_mode: bool = False,
) -> str:
    if provider == "ollama":
        endpoint = f"{base_url.rstrip('/')}/api/chat"
        request_payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {
                "num_ctx": num_ctx,
                "num_predict": num_predict,
            },
        }
        if json_mode:
            request_payload["format"] = "json"
    elif provider in {"openai", "openai-compatible"}:
        if provider == "openai" and not api_key:
            raise ValueError(
                "Informe a chave da API da OpenAI ou defina OPENAI_API_KEY."
            )
        endpoint = f"{base_url.rstrip('/')}/chat/completions"
        request_payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "max_completion_tokens" if provider == "openai" else "max_tokens": num_predict,
        }
        if json_mode and provider == "openai":
            request_payload["response_format"] = {"type": "json_object"}
    else:
        raise ValueError(f"Provedor de modelo não suportado: {provider}")

    headers = {"Content-Type": "application/json"}
    if provider != "ollama" and api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    request = Request(
        endpoint,
        data=json.dumps(request_payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            response_data: Any = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        details = error.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"O provedor {provider} retornou HTTP {error.code}: "
            f"{details or error.reason}"
        ) from error
    except (URLError, TimeoutError) as error:
        raise RuntimeError(
            f"Não foi possível acessar o provedor {provider} em {base_url}: {error}"
        ) from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"O provedor {provider} retornou uma resposta JSON inválida."
        ) from error

    if not isinstance(response_data, dict):
        raise RuntimeError(f"O provedor {provider} retornou uma resposta inválida.")
    if provider == "ollama":
        message = response_data.get("message")
        content = message.get("content") if isinstance(message, dict) else None
    else:
        choices = response_data.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else None
        message = choice.get("message") if isinstance(choice, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise RuntimeError(
            f"A resposta do provedor {provider} não contém o texto esperado."
        )

    content = content.strip()
    if not content:
        raise RuntimeError(f"O provedor {provider} retornou uma resposta vazia.")
    return content


def truncate_context(text: str, max_chars: int, label: str) -> str:
    if len(text) <= max_chars:
        return text

    marker = f"\n\n[... {label} omitido para caber no contexto do modelo ...]\n\n"
    available_chars = max(0, max_chars - len(marker))
    leading_chars = available_chars * 3 // 4
    trailing_chars = available_chars - leading_chars
    end = text[-trailing_chars:] if trailing_chars else ""
    return f"{text[:leading_chars]}{marker}{end}"


def _make_pull_request_draft(title: str, body: str) -> PullRequestDraft | None:
    title = title.strip().strip("*`")
    body = body.strip()
    if (
        not title
        or len(title) > 256
        or any(ord(char) < 32 for char in title)
        or not body
        or len(body) > 50_000
    ):
        return None
    return PullRequestDraft(title=title, body=body)


def parse_pull_request_draft(response: str) -> PullRequestDraft:
    content = response.strip()
    if content.startswith("```") and content.endswith("```"):
        content = "\n".join(content.splitlines()[1:-1]).strip()

    draft_data: Any = None
    try:
        draft_data = json.loads(content)
    except json.JSONDecodeError:
        json_start = content.find("{")
        if json_start >= 0:
            try:
                draft_data, _ = json.JSONDecoder().raw_decode(content[json_start:])
            except json.JSONDecodeError:
                pass

    if isinstance(draft_data, dict):
        title = draft_data.get("title")
        body = draft_data.get("body")
        if isinstance(title, str) and isinstance(body, str):
            draft = _make_pull_request_draft(title, body)
            if draft is not None:
                return draft

    lines = content.splitlines()
    title_value: str | None = None
    title_line_index = -1
    title_label = re.compile(
        r"^(?:t[ií]tulo(?:\s+sugerido)?|title)\s*[:：]\s*(.*?)\s*$",
        re.IGNORECASE,
    )
    title_heading = re.compile(
        r"^(?:t[ií]tulo(?:\s+sugerido)?|title)\s*:?\s*$",
        re.IGNORECASE,
    )

    def normalize_heading(line: str) -> str:
        return re.sub(r"^\s*#{1,6}\s*", "", line).replace("**", "").strip()

    def is_body_heading(line: str) -> bool:
        normalized = normalize_heading(line).rstrip(":：").strip().lower()
        return normalized in {
            "body",
            "body do pull request",
            "corpo",
            "descrição",
            "descrição do pr",
            "descrição do pull request",
            "description",
        }

    for index, line in enumerate(lines):
        normalized_line = normalize_heading(line)
        match = title_label.match(normalized_line)
        if match:
            title_value = match.group(1).strip().strip("*`")
            title_line_index = index
            break
        if title_heading.match(normalized_line):
            title_line_index = index
            next_line = next(
                (
                    candidate.strip()
                    for candidate in lines[index + 1 :]
                    if candidate.strip()
                ),
                "",
            )
            title_value = next_line.strip("*`")
            if next_line:
                title_line_index = index + 1 + next(
                    offset
                    for offset, candidate in enumerate(lines[index + 1 :])
                    if candidate.strip()
                )
            break

    if title_value is None:
        first_content_index = next(
            (index for index, line in enumerate(lines) if line.strip()),
            -1,
        )
        if first_content_index >= 0:
            first_line = lines[first_content_index].strip()
            if first_line.startswith("# "):
                title_value = first_line[2:].strip()
                title_line_index = first_content_index

    if title_value:
        body_start = next(
            (
                index + 1
                for index in range(title_line_index + 1, len(lines))
                if is_body_heading(lines[index])
            ),
            title_line_index + 1,
        )
        draft = _make_pull_request_draft(
            title_value,
            "\n".join(lines[body_start:]),
        )
        if draft is not None:
            return draft

    raise RuntimeError(
        "Não foi possível identificar título e body na resposta do modelo. "
        "Tente gerar novamente ou use um modelo que siga o formato solicitado."
    )
