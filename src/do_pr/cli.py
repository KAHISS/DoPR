import argparse
import os
import sys
from pathlib import Path

from .service import generate_pull_request_description
from .web import create_server


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Gera uma descrição Markdown de pull request comparando duas "
            "branches pelo diff do GitHub e um provedor de IA."
        )
    )
    parser.add_argument("--repo", help="URL do repositório GitHub.")
    parser.add_argument(
        "--source",
        help="Branch de origem que será mergeada, por exemplo feat/login.",
    )
    parser.add_argument(
        "--target",
        default="develop",
        help="Branch de destino (padrão: develop).",
    )
    parser.add_argument(
        "--provider",
        choices=("ollama", "openai", "openai-compatible"),
        default="ollama",
        help="Provedor do modelo (padrão: ollama).",
    )
    parser.add_argument(
        "--model",
        help="Identificador do modelo no provedor selecionado.",
    )
    parser.add_argument(
        "--base-url",
        "--ollama-url",
        dest="base_url",
        help="URL base da API do modelo (ou configure a variável adequada).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Timeout em segundos para cada chamada ao provedor (padrão: 300).",
    )
    parser.add_argument(
        "--num-ctx",
        type=int,
        default=8192,
        help="Orçamento de contexto da entrada (padrão: 8192; mínimo: 4096).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("pr_description.md"),
        help="Arquivo Markdown de saída (padrão: pr_description.md).",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Inicia a interface web local em vez de gerar pela linha de comando.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Endereço para o servidor web (padrão: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Porta do servidor web (padrão: 8000).",
    )
    return parser


def run_web_server(host: str, port: int) -> None:
    server = create_server(host, port)
    print(f"Interface disponível em http://{host}:{port}")
    print("Pressione Ctrl+C para encerrar.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor encerrado.")
    finally:
        server.server_close()


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()
    if args.web:
        try:
            run_web_server(args.host, args.port)
        except OSError as error:
            print(f"Erro ao iniciar o servidor: {error}", file=sys.stderr)
            return 1
        return 0

    if not args.repo or not args.source:
        parser.error("--repo e --source são obrigatórios fora do modo web.")
    if args.timeout <= 0 or args.num_ctx < 4096:
        parser.error(
            "--timeout deve ser maior que zero e --num-ctx deve ser pelo menos 4096."
        )

    model = args.model or {
        "ollama": "qwen2.5-coder:7b",
        "openai": "gpt-4o-mini",
    }.get(args.provider)
    if not model:
        parser.error("--model é obrigatório para o provedor OpenAI-compatible.")
    base_url = args.base_url or {
        "ollama": os.getenv("OLLAMA_URL", "http://localhost:11434"),
        "openai": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "openai-compatible": os.getenv("LLM_BASE_URL", "http://localhost:1234/v1"),
    }[args.provider]
    api_key = None
    if args.provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
    elif args.provider == "openai-compatible":
        api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")

    try:
        draft = generate_pull_request_description(
            repository_url=args.repo,
            source_branch=args.source,
            target_branch=args.target,
            provider=args.provider,
            model=model,
            base_url=base_url,
            api_key=api_key,
            timeout=args.timeout,
            num_ctx=args.num_ctx,
            github_token=os.getenv("GITHUB_TOKEN"),
        )
        args.output.write_text(draft.as_markdown().rstrip() + "\n", encoding="utf-8")
    except (RuntimeError, OSError, ValueError) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1

    print(f"Descrição do PR salva em: {args.output}")
    return 0
