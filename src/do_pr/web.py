import html
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .github import (
    create_pull_request,
    get_pull_request_options,
    get_repository_branches,
)
from .service import generate_pull_request_description


_PAGE_PATH = Path(__file__).parent / "web" / "index.html"
_MAX_REQUEST_SIZE = 256 * 1024


class PullRequestHandler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if urlparse(self.path).path != "/":
            self.send_error(404, "Página não encontrada")
            return

        try:
            page = _PAGE_PATH.read_text(encoding="utf-8")
        except OSError as error:
            self.send_error(500, f"Não foi possível carregar a interface: {error}")
            return

        ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
        body = page.replace(
            "{{OLLAMA_URL}}",
            html.escape(ollama_url, quote=True),
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        endpoint = urlparse(self.path).path
        if endpoint not in {
            "/api/generate",
            "/api/branches",
            "/api/pr-options",
            "/api/create-pr",
        }:
            self._send_json(404, {"error": "Endpoint não encontrado."})
            return

        content_length = self.headers.get("Content-Length")
        try:
            request_size = int(content_length or "")
        except ValueError:
            self._send_json(400, {"error": "Cabeçalho Content-Length inválido."})
            return
        if request_size < 0 or request_size > _MAX_REQUEST_SIZE:
            self._send_json(413, {"error": "A solicitação excede o limite permitido."})
            return
        if self.headers.get_content_type() != "application/json":
            self._send_json(415, {"error": "Envie os dados no formato JSON."})
            return

        try:
            payload: Any = json.loads(self.rfile.read(request_size))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "O JSON enviado é inválido."})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "O corpo deve ser um objeto JSON."})
            return

        repository_url = payload.get("repository_url")
        if endpoint == "/api/branches":
            if not isinstance(repository_url, str) or not repository_url.strip():
                self._send_json(400, {"error": "Informe a URL do repositório."})
                return
            try:
                branches = get_repository_branches(repository_url.strip())
            except (OSError, RuntimeError, ValueError, TypeError) as error:
                self._send_json(400, {"error": str(error)})
                return
            self._send_json(200, {"branches": branches})
            return

        if endpoint == "/api/pr-options":
            if not isinstance(repository_url, str) or not repository_url.strip():
                self._send_json(400, {"error": "Informe a URL do repositório."})
                return
            try:
                options = get_pull_request_options(repository_url.strip())
            except (OSError, RuntimeError, ValueError, TypeError) as error:
                self._send_json(400, {"error": str(error)})
                return
            self._send_json(200, options)
            return

        if endpoint == "/api/create-pr":
            source_branch = payload.get("source_branch")
            target_branch = payload.get("target_branch")
            title = payload.get("title")
            body = payload.get("body")
            if not all(
                isinstance(value, str) and value.strip()
                for value in (
                    repository_url,
                    source_branch,
                    target_branch,
                    title,
                    body,
                )
            ):
                self._send_json(
                    400,
                    {"error": "Informe repositório, branches, título e body do PR."},
                )
                return
            try:
                pull_request_url = create_pull_request(
                    repository_url=repository_url.strip(),
                    source_branch=source_branch.strip(),
                    target_branch=target_branch.strip(),
                    title=title.strip(),
                    body=body.strip(),
                    assignees=payload.get("assignees", []),
                    reviewers=payload.get("reviewers", []),
                    labels=payload.get("labels", []),
                    project=payload.get("project"),
                )
            except (OSError, RuntimeError, ValueError, TypeError) as error:
                self._send_json(400, {"error": str(error)})
                return
            self._send_json(201, {"url": pull_request_url})
            return

        source_branch = payload.get("source_branch")
        target_branch = payload.get("target_branch")
        model = payload.get("model")
        provider = payload.get("provider", "ollama")
        base_url = payload.get("base_url")
        api_key = payload.get("api_key", "")
        change_description = payload.get("change_description", "")
        if not isinstance(provider, str) or provider not in {
            "ollama",
            "openai",
            "openai-compatible",
        }:
            self._send_json(400, {"error": "Provedor de modelo não suportado."})
            return
        if not isinstance(api_key, str):
            self._send_json(400, {"error": "A chave da API deve ser texto."})
            return
        api_key = api_key.strip() or (
            os.getenv("OPENAI_API_KEY")
            if provider == "openai"
            else os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
            if provider == "openai-compatible"
            else None
        )
        if not isinstance(change_description, str) or len(change_description) > 2000:
            self._send_json(
                400,
                {"error": "A descrição adicional deve ter no máximo 2000 caracteres."},
            )
            return
        if not all(
            isinstance(value, str) and value.strip()
            for value in (
                repository_url,
                source_branch,
                target_branch,
                model,
                base_url,
            )
        ):
            self._send_json(400, {"error": "Preencha todos os campos obrigatórios."})
            return

        try:
            timeout = int(payload.get("timeout", 300))
            num_ctx = int(payload.get("num_ctx", 8192))
            if timeout <= 0 or num_ctx < 4096:
                raise ValueError(
                    "Timeout deve ser maior que zero e o contexto deve ser pelo menos 4096."
                )

            draft = generate_pull_request_description(
                repository_url=repository_url.strip(),
                source_branch=source_branch.strip(),
                target_branch=target_branch.strip(),
                provider=provider,
                model=model.strip(),
                base_url=base_url.strip(),
                api_key=api_key,
                timeout=timeout,
                num_ctx=num_ctx,
                github_token=os.getenv("GITHUB_TOKEN"),
                change_description=change_description.strip(),
            )
        except (OSError, RuntimeError, ValueError, TypeError) as error:
            self._send_json(400, {"error": str(error)})
            return

        self._send_json(
            200,
            {
                "title": draft.title,
                "body": draft.body,
                "description": draft.as_markdown(),
            },
        )

    def log_message(self, format: str, *args: Any) -> None:
        super().log_message(format, *args)


def create_server(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), PullRequestHandler)
