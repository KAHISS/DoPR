# Gerador de descrição de Pull Request

Este script busca o diff entre duas branches do GitHub e o envia a um provedor
de IA para gerar uma descrição de PR em Markdown. A branch de origem é a que
será mergeada na branch de destino. A interface também carrega a lista de
branches automaticamente.

## Requisitos

- Python 3.14 ou superior
- Um provedor de modelos: [Ollama](https://ollama.com/), OpenAI ou uma API
  compatível com o formato OpenAI Chat Completions
- Acesso ao repositório GitHub (repositórios privados exigem um token)

Instale as dependências do projeto:

```bash
uv sync
```

Para usar o Ollama, baixe um modelo e inicie o serviço:

```bash
ollama pull qwen2.5-coder:7b
ollama serve
```

## Uso

### Interface web

Inicie o servidor local:

```bash
uv run do-pr --web
```

Abra [http://127.0.0.1:8000](http://127.0.0.1:8000), preencha o repositório e
as branches serão carregadas automaticamente nos seletores. Escolha a branch
de origem e confirme a branch de destino (que começa em `develop`, quando
disponível), depois clique em **Gerar descrição do PR**. A interface permite
copiar o resultado ou baixá-lo como `pr_description.md`. O servidor usa
`127.0.0.1` por padrão; só altere `--host` se quiser permitir conexões de outros
dispositivos na rede.

O campo **O que mudou?** é opcional e permite informar o objetivo da feature
para direcionar a descrição. O modelo usa o diff como evidência das mudanças;
o texto serve apenas como contexto e não deve ser apresentado como prova de
uma mudança implementada.

Depois de gerar o rascunho, a interface abre uma confirmação com o título e o
body em Markdown, que podem ser revisados e editados. O PR só é criado depois
de clicar em **Confirmar e criar PR**; **Cancelar** mantém o rascunho sem
publicá-lo. Na confirmação, também é possível selecionar responsáveis,
revisores, labels e um GitHub Project V2. Depois que o PR for criado, ele será
aberto em uma nova guia. A branch de origem é enviada como `head` e a branch de
destino como `base`.

Para criar PRs, instale o [GitHub CLI](https://cli.github.com/) e autentique a
conta com acesso ao repositório:

```bash
gh auth login
```

As branches selecionadas precisam existir no GitHub. Se o CLI não estiver
instalado ou autenticado, a interface mostrará o erro retornado e não indicará
que o PR foi criado.

### Docker

O container inclui o GitHub CLI. A autenticação é passada por `GH_TOKEN`, sem
montar nem expor a configuração pessoal do `gh` do host:

#### Criar um token de acesso

1. Acesse [Fine-grained personal access tokens](https://github.com/settings/personal-access-tokens/new) e entre na sua conta do GitHub.
2. Dê um nome ao token e escolha uma validade.
3. Em **Resource owner**, selecione a conta ou organização proprietária do repositório.
4. Em **Repository access**, selecione **Only select repositories** e escolha o repositório usado no app ou todos.
5. Em **Repository permissions**, configure:
   - **Contents: Read-only**, para listar branches e ler a comparação.
   - **Issues: Read and write**, para listar e associar responsáveis e labels.
   - **Pull requests: Read and write**, para criar o PR pelo GitHub CLI.
   - **Projects: Read and write**, para listar e associar GitHub Projects V2.
6. Clique em **Generate token** e copie o valor. O GitHub só o exibe na criação.

**Limitação importante:** fine-grained tokens não conseguem acessar Projects V2
pertencentes a uma conta pessoal. Se o erro mencionar `user.projectsV2`, use um
token classic para essa funcionalidade. Em **Settings > Developer settings >
Personal access tokens > Tokens (classic)**, gere um token com o escopo
`project` e também `repo` para repositórios privados (`public_repo` para
públicos). Tokens classic têm acesso mais amplo; use-os somente se precisar
associar o PR a um projeto pessoal. Consulte as
[limitações dos fine-grained tokens](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#fine-grained-personal-access-tokens-limitations).

Se a organização exigir aprovação para tokens ou usar SSO, solicite a aprovação
ou autorize o token conforme as políticas da organização.

Não publique nem envie o token a outras pessoas. Configure-o no arquivo `.env`
local (não no `.env.example`). Se ainda não houver um `.env`, crie-o a partir do
exemplo; se já existir, edite-o sem sobrescrever:

```bash
cp .env.example .env
```

Edite `.env` e substitua o valor de exemplo:

```dotenv
GH_TOKEN=github_pat_seu_token
```

O Compose também fornece esse valor à aplicação como `GITHUB_TOKEN`. Depois,
inicie ou recrie o container:

```bash
docker compose up --build
```

#### Usar a imagem publicada no Docker Hub

Para executar a imagem pronta, sem construir o projeto localmente, use o arquivo
[compose.dockerhub.yaml](./compose.dockerhub.yaml). Ele sempre busca
`kahiss/do_pr:latest` no Docker Hub e usa as mesmas variáveis do `.env`:

```bash
docker compose -f compose.dockerhub.yaml up -d
```

Para atualizar a imagem e recriar o container:

```bash
docker compose -f compose.dockerhub.yaml pull
docker compose -f compose.dockerhub.yaml up -d --force-recreate
```

Se receber **Resource not accessible by personal access token (HTTP 403)** ao
listar projetos pessoais, fine-grained tokens não são compatíveis com esse
recurso; use o token classic com escopo `project` descrito acima. Para projetos
de uma organização, confirme **Projects: Read and write**, o resource owner e a
aprovação/SSO. Se usar uma sessão local autenticada pelo `gh` em vez de `GH_TOKEN`,
autorize o escopo com `gh auth refresh -s project`.

Com qualquer um dos dois Compose, abra
[http://127.0.0.1:8000](http://127.0.0.1:8000). O container usa
`host.docker.internal:11434` como URL inicial do Ollama. No Linux, o Compose
configura esse nome para o gateway do host. O Ollama precisa aceitar conexões
vindas do Docker; por padrão, ele pode escutar apenas em `localhost`. Se
necessário, configure `OLLAMA_HOST=0.0.0.0:11434` no serviço do Ollama e
restrinja o acesso com o firewall para não expor a API à rede. Altere
`OLLAMA_URL` no `.env` se o serviço estiver em outro endereço.

OpenAI e outros provedores compatíveis continuam disponíveis nas configurações
da interface; as chaves podem ser definidas em `.env` ou digitadas na própria
interface. O arquivo `.env` é ignorado pelo Git.

Para encerrar:

```bash
# Imagem construída localmente:
docker compose down

# Imagem do Docker Hub:
docker compose -f compose.dockerhub.yaml down
```

Em **Configurações do modelo**, selecione Ollama, OpenAI ou **Outro compatível
com OpenAI**. Informe o identificador do modelo e a URL base da API. Para OpenAI,
use `https://api.openai.com/v1` e informe a chave no campo ou configure
`OPENAI_API_KEY`. Para outros provedores compatíveis, use a URL e o modelo
indicados pelo provedor (por exemplo, OpenRouter, Groq, Together ou um servidor
local como LM Studio); a chave pode ser informada no campo ou por
`LLM_API_KEY`. A interface também aceita endpoints compatíveis que não exigem
chave.

O campo da chave não é persistido pelo aplicativo. O diff é enviado ao endpoint
configurado para a inferência; para provedores hospedados, considere as
políticas de privacidade do provedor.

Para mudar a porta ou o endereço:

```bash
uv run do-pr --web --host 127.0.0.1 --port 8080
```

### Linha de comando

```bash
uv run do-pr \
  --repo https://github.com/organizacao/repositorio \
  --source feat/minha-feature \
  --target develop \
  --model qwen2.5-coder:7b \
  --output pr_description.md
```

Para usar OpenAI pela linha de comando:

```bash
uv run do-pr \
  --provider openai \
  --repo https://github.com/organizacao/repositorio \
  --source feat/minha-feature \
  --target develop \
  --model gpt-4o-mini
```

Defina `OPENAI_API_KEY` no ambiente antes de executar o comando.

`--target` é opcional e usa `develop` por padrão. O script salva o Markdown em
`pr_description.md` por padrão e também aceita os parâmetros opcionais:

- `--provider`: `ollama` (padrão), `openai` ou `openai-compatible`.
- `--model`: identificador do modelo no provedor selecionado.
- `--base-url` (alias legado `--ollama-url`): URL base da API. Use
  `OPENAI_BASE_URL` para OpenAI ou `LLM_BASE_URL` para um endpoint compatível.
- Credenciais: `OPENAI_API_KEY` para OpenAI; `LLM_API_KEY` para endpoint
  compatível (também aceita `OPENAI_API_KEY` como fallback).
- `--timeout`: timeout de cada solicitação ao provedor em segundos (padrão: 300).
- `--num-ctx`: orçamento de contexto de entrada (padrão: 8192; mínimo: 4096).
  Para Ollama também configura a janela do modelo. O diff é limitado para
  caber no contexto configurado.
- `--output`: caminho do arquivo Markdown gerado.

Para repositórios privados, defina `GITHUB_TOKEN` no ambiente antes de executar.
O token é usado pela API de comparação do GitHub e pela listagem de branches
para acessar o repositório.

A interface e a linha de comando usam o provedor escolhido. O diff obtido pelo
GitHub, os nomes das branches e a descrição adicional opcional são enviados ao
modelo; o conteúdo completo do repositório não é coletado.

## Observação sobre a comparação

O diff é obtido pela API de comparação do GitHub entre destino e origem. O
GitHub pode não fornecer patch textual para arquivos binários ou alterações
muito grandes; nesses casos, o modelo recebe o nome do arquivo e as estatísticas
disponíveis, mas pode não conseguir descrever o conteúdo. Revise a descrição e
valide os testes antes de abrir o PR.

Se o GitHub informar que não há ancestral comum entre as branches, elas têm
históricos independentes. Nesse caso, o GitHub não consegue comparar nem criar
o PR, e um token diferente não resolve. Crie uma branch a partir da branch de
destino e reaplique nela as alterações da feature; depois envie essa nova branch
ao GitHub e gere o PR novamente.