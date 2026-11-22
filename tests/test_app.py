import http.client
import json
import threading
import unittest
from io import BytesIO
from urllib.error import HTTPError
from unittest.mock import AsyncMock, patch

from do_pr import github, models, providers, service as main, web


class LLMChatTests(unittest.TestCase):
    @patch(
        "do_pr.providers.urlopen",
        return_value=BytesIO(b'{"message": {"content": " resposta "}}'),
    )
    def test_sends_ollama_chat_request_with_configured_context(self, urlopen_mock):
        result = main.llm_chat(
            [{"role": "user", "content": "resuma"}],
            provider="ollama",
            model="test-model",
            base_url="http://localhost:11434/",
            timeout=30,
            num_ctx=8192,
        )

        request = urlopen_mock.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(result, "resposta")
        self.assertEqual(request.full_url, "http://localhost:11434/api/chat")
        self.assertEqual(payload["model"], "test-model")
        self.assertEqual(payload["options"]["num_ctx"], 8192)
        self.assertEqual(payload["options"]["num_predict"], 1536)
        self.assertFalse(payload["stream"])

    @patch(
        "do_pr.providers.urlopen",
        return_value=BytesIO(
            b'{"choices": [{"message": {"content": " descri\\u00e7\\u00e3o "}}]}'
        ),
    )
    def test_sends_openai_compatible_chat_request(self, urlopen_mock):
        result = main.llm_chat(
            [{"role": "user", "content": "resuma"}],
            provider="openai",
            model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
            timeout=30,
            num_ctx=8192,
            num_predict=1800,
            api_key="secret-test-key",
            json_mode=True,
        )

        request = urlopen_mock.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(result, "descrição")
        self.assertEqual(
            request.full_url,
            "https://api.openai.com/v1/chat/completions",
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-test-key")
        self.assertEqual(payload["max_completion_tokens"], 1800)
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertNotIn("options", payload)

    @patch(
        "do_pr.providers.urlopen",
        return_value=BytesIO(b'{"choices": [{"message": {"content": "ok"}}]}'),
    )
    def test_sends_request_to_generic_openai_compatible_provider(self, urlopen_mock):
        main.llm_chat(
            [{"role": "user", "content": "resuma"}],
            provider="openai-compatible",
            model="my-model",
            base_url="https://api.example/v1/",
            timeout=30,
            num_ctx=8192,
            num_predict=1024,
            api_key="provider-key",
        )

        request = urlopen_mock.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(
            request.full_url,
            "https://api.example/v1/chat/completions",
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer provider-key")
        self.assertEqual(payload["max_tokens"], 1024)

    @patch(
        "do_pr.providers.urlopen",
        return_value=BytesIO(b'{"message": {"content": "{\\"title\\":\\"ok\\"}"}}'),
    )
    def test_requests_json_format_from_ollama_when_needed(self, urlopen_mock):
        main.llm_chat(
            [{"role": "user", "content": "crie um rascunho"}],
            provider="ollama",
            model="test-model",
            base_url="http://localhost:11434",
            timeout=30,
            num_ctx=8192,
            json_mode=True,
        )

        payload = json.loads(urlopen_mock.call_args.args[0].data)
        self.assertEqual(payload["format"], "json")


class CodeChangesTests(unittest.TestCase):
    @patch(
        "do_pr.github.urlopen",
        side_effect=HTTPError(
            "https://api.github.com/repos/org/repo/compare/main...feat",
            404,
            "Not Found",
            {},
            BytesIO(
                b'{"message":"No common ancestor between main and feat."}'
            ),
        ),
    )
    def test_explains_unrelated_branch_histories(self, _urlopen_mock):
        with self.assertRaisesRegex(RuntimeError, "históricos Git independentes"):
            main.get_code_changes(
                "https://github.com/org/repo",
                target_branch="main",
                source_branch="feat",
            )

    @patch(
        "do_pr.github.urlopen",
        return_value=BytesIO(
            json.dumps(
                {
                    "files": [
                        {
                            "filename": "src/services/auth.py",
                            "status": "modified",
                            "additions": 2,
                            "deletions": 1,
                            "patch": "@@ -1 +1 @@\n-old\n+new",
                        },
                        {
                            "filename": "src/models/user.py",
                            "status": "modified",
                            "additions": 1,
                            "deletions": 0,
                            "patch": "@@ -1 +1 @@\n+field",
                        },
                        {
                            "filename": "docs/api/openapi.yaml",
                            "status": "modified",
                            "additions": 3,
                            "deletions": 0,
                            "patch": "@@ -1 +1 @@\n+endpoint",
                        },
                        {
                            "filename": "README.md",
                            "status": "modified",
                            "additions": 1,
                            "deletions": 0,
                            "patch": "@@ -1 +1 @@\n+docs",
                        },
                    ]
                }
            ).encode()
        ),
    )
    def test_fetches_complete_real_code_diff(self, urlopen_mock):
        diff = main.get_code_changes(
            "https://github.com/org/repo",
            target_branch="develop",
            source_branch="feat/login",
            github_token="github-token",
        )

        request = urlopen_mock.call_args.args[0]
        self.assertIn(
            "/compare/develop...feat%2Flogin?per_page=100",
            request.full_url,
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer github-token")
        self.assertIn("src/services/auth.py", diff)
        self.assertIn("src/models/user.py", diff)
        self.assertIn("docs/api/openapi.yaml", diff)
        self.assertIn("README.md", diff)
        self.assertIn("+new", diff)
        self.assertIn("+endpoint", diff)


class PullRequestOptionsTests(unittest.TestCase):
    @patch(
        "do_pr.github._run_github_cli",
        side_effect=[
            json.dumps([[{"login": "zara"}, {"login": "alice"}]]),
            json.dumps(
                [[
                    {"login": "reviewer", "permissions": {"push": True}},
                    {"login": "reader", "permissions": {"pull": True}},
                ]]
            ),
            json.dumps([[{"name": "bug"}, {"name": "feature"}]]),
            json.dumps(
                {
                    "projects": [
                        {"title": "Roadmap", "closed": False},
                        {"title": "Archive", "closed": True},
                    ]
                }
            ),
        ],
    )
    def test_lists_assignees_reviewers_labels_and_v2_projects(self, run_mock):
        options = github.get_pull_request_options("https://github.com/org/repo")

        self.assertEqual(options["assignees"], ["alice", "zara"])
        self.assertEqual(options["reviewers"], ["reviewer"])
        self.assertEqual(options["labels"], ["bug", "feature"])
        self.assertEqual(options["projects"], ["Roadmap"])
        self.assertEqual(options["errors"], {})
        self.assertEqual(run_mock.call_count, 4)

    @patch(
        "do_pr.github._run_github_cli",
        side_effect=[
            json.dumps([[]]),
            json.dumps([[]]),
            json.dumps([[]]),
            RuntimeError(
                "GraphQL: Resource not accessible by personal access token "
                "(user.projectsV2.nodes.0)"
            ),
        ],
    )
    def test_explains_fine_grained_token_limit_for_personal_projects(self, _run_mock):
        options = github.get_pull_request_options("https://github.com/org/repo")

        self.assertIn("fine-grained token", options["errors"]["projects"])
        self.assertIn("token classic", options["errors"]["projects"])
        self.assertIn("labels continuam disponíveis", options["errors"]["projects"])


class GeneratePullRequestDescriptionTests(unittest.TestCase):
    @patch(
        "do_pr.service.get_code_changes",
        return_value="Arquivos alterados: 1\n\n### src/services/auth.py\n+token validation",
    )
    @patch(
        "do_pr.service.llm_chat",
        return_value='{"title":"Adicionar login OAuth","body":"## Contexto\\n\\nImplementa login."}',
    )
    def test_sends_only_the_diff_and_optional_description_to_model(
        self, llm_mock, diff_mock
    ):
        result = main.generate_pull_request_description(
            "https://github.com/org/repo",
            "feat/login",
            "develop",
            model="test-model",
            change_description="Adicionar login por OAuth",
        )

        self.assertEqual(result.title, "Adicionar login OAuth")
        self.assertEqual(result.body, "## Contexto\n\nImplementa login.")
        self.assertEqual(
            result.as_markdown(),
            "# Adicionar login OAuth\n\n## Contexto\n\nImplementa login.",
        )
        self.assertEqual(llm_mock.call_count, 1)
        prompt = llm_mock.call_args.args[0][1]["content"]
        self.assertIn("Branch de origem: feat/login", prompt)
        self.assertIn("Branch de destino: develop", prompt)
        self.assertIn("Adicionar login por OAuth", prompt)
        self.assertIn("Diff de código entre destino e origem", prompt)
        self.assertIn("+token validation", prompt)
        system_prompt = llm_mock.call_args.args[0][0]["content"]
        self.assertIn("strings `title` e `body`", system_prompt)
        self.assertTrue(llm_mock.call_args.kwargs["json_mode"])
        diff_mock.assert_called_once_with(
            "https://github.com/org/repo",
            "develop",
            "feat/login",
            github_token=None,
            timeout=300,
        )

    @patch("do_pr.service.get_code_changes")
    def test_rejects_same_source_and_target_branch_before_fetching_diff(self, diff_mock):
        with self.assertRaises(ValueError):
            main.generate_pull_request_description(
                "https://github.com/org/repo", "develop", "develop"
            )

        diff_mock.assert_not_called()

    @patch("do_pr.service.get_code_changes")
    def test_requires_openai_key_before_fetching_diff(self, diff_mock):
        with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY"):
            main.generate_pull_request_description(
                "https://github.com/org/repo",
                "feat/login",
                "develop",
                provider="openai",
            )

        diff_mock.assert_not_called()

    @patch("do_pr.service.get_code_changes", return_value="início\n" + "x" * 10000 + "\nfim")
    @patch(
        "do_pr.service.llm_chat",
        return_value='{"title":"Título","body":"## Body"}',
    )
    def test_truncates_long_diff_to_fit_model_context(self, llm_mock, diff_mock):
        main.generate_pull_request_description(
            "https://github.com/org/repo",
            "feat/large",
            "develop",
            num_ctx=4096,
        )

        prompt = llm_mock.call_args.args[0][1]["content"]
        self.assertIn("diff excedente omitido para caber", prompt)
        self.assertIn("início", prompt)
        self.assertIn("fim", prompt)
        self.assertLess(len(prompt), 1500)

    @patch(
        "do_pr.service.get_code_changes",
        return_value="diff",
    )
    @patch("do_pr.service.llm_chat", return_value="Não é JSON")
    def test_rejects_model_output_without_title_and_body_json(
        self, llm_mock, diff_mock
    ):
        with self.assertRaisesRegex(RuntimeError, "identificar título e body"):
            main.generate_pull_request_description(
                "https://github.com/org/repo",
                "feat/login",
                "develop",
            )

    def test_parses_common_markdown_title_and_body_formats(self):
        responses = (
            (
                "## Título sugerido\nAdicionar suporte OAuth\n\n"
                "## Body\n### Contexto\nImplementa login."
            ),
            (
                "**Título:** Adicionar suporte OAuth\n\n"
                "**Body:**\n### Contexto\nImplementa login."
            ),
            (
                "# Adicionar suporte OAuth\n\n"
                "### Contexto\nImplementa login."
            ),
            (
                '```json\n{"title":"Adicionar suporte OAuth",'
                '"body":"### Contexto\\nImplementa login."}\n```'
            ),
        )

        for response in responses:
            with self.subTest(response=response[:30]):
                draft = providers.parse_pull_request_draft(response)
                self.assertEqual(draft.title, "Adicionar suporte OAuth")
                self.assertIn("Implementa login.", draft.body)


class CreatePullRequestTests(unittest.TestCase):
    @patch(
        "do_pr.github.subprocess.run",
        return_value=github.subprocess.CompletedProcess(
            args=["gh"], returncode=0, stdout="https://github.com/org/repo/pull/42\n", stderr=""
        ),
    )
    def test_creates_pr_with_source_head_and_target_base(self, run_mock):
        url = github.create_pull_request(
            "https://github.com/org/repo",
            source_branch="feat/login",
            target_branch="develop",
            title="Adicionar login",
            body="## Resumo\n\nImplementa OAuth.",
            assignees=["alice"],
            reviewers=["bob"],
            labels=["bug", "help wanted"],
            project="Roadmap",
        )

        self.assertEqual(url, "https://github.com/org/repo/pull/42")
        args, kwargs = run_mock.call_args
        self.assertEqual(
            args[0],
            [
                "gh",
                "pr",
                "create",
                "--repo",
                "org/repo",
                "--head",
                "feat/login",
                "--base",
                "develop",
                "--title",
                "Adicionar login",
                "--body-file",
                "-",
                "--assignee",
                "alice",
                "--reviewer",
                "bob",
                "--label",
                "bug",
                "--label",
                "help wanted",
                "--project",
                "Roadmap",
            ],
        )
        self.assertEqual(kwargs["timeout"], 60)
        self.assertFalse(kwargs["check"])
        self.assertEqual(kwargs["input"], "## Resumo\n\nImplementa OAuth.")

    @patch("do_pr.github.subprocess.run")
    def test_reports_github_cli_failure(self, run_mock):
        run_mock.return_value = github.subprocess.CompletedProcess(
            args=["gh"], returncode=1, stdout="", stderr="not logged in"
        )

        with self.assertRaisesRegex(RuntimeError, "not logged in"):
            github.create_pull_request(
                "https://github.com/org/repo",
                "feat/login",
                "develop",
                "Adicionar login",
                "Body",
            )

    @patch("do_pr.github.subprocess.run")
    def test_does_not_run_cli_for_invalid_repository_or_branches(self, run_mock):
        for repository_url, source, target in (
            ("https://example.com/org/repo", "feat/login", "develop"),
            ("https://github.com/org/repo", "develop", "develop"),
        ):
            with self.subTest(repository_url=repository_url, source=source):
                with self.assertRaises(ValueError):
                    github.create_pull_request(
                        repository_url, source, target, "Title", "Body"
                    )

        run_mock.assert_not_called()

    @patch("do_pr.github.subprocess.run")
    def test_rejects_invalid_optional_selection_before_running_cli(self, run_mock):
        with self.assertRaisesRegex(ValueError, "labels"):
                    github.create_pull_request(
                        "https://github.com/org/repo",
                        "feat/login",
                        "develop",
                        "Title",
                        "Body",
                        labels="bug",
                    )

        run_mock.assert_not_called()


class WebInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.server = web.create_server("127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(self.host, self.port)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    def test_serves_html_interface(self):
        status, body = self.request("GET", "/")

        self.assertEqual(status, 200)
        self.assertIn(b"Gerar descri", body)
        self.assertNotIn(b"Pastas para analisar", body)
        self.assertIn(b"Revise e confirme o pull request", body)
        self.assertIn(b"Confirmar e criar PR", body)
        self.assertIn(b"/api/create-pr", body)
        self.assertIn(b"/api/pr-options", body)
        self.assertIn(b'id="pr-assignees"', body)
        self.assertIn(b'id="pr-reviewers"', body)
        self.assertIn(b'id="pr-labels"', body)
        self.assertIn(b'id="pr-project"', body)
        self.assertIn(b'window.open("about:blank", "_blank")', body)
        self.assertIn(b'value="http://localhost:11434"', body)

    @patch.dict("os.environ", {"OLLAMA_URL": "http://host.docker.internal:11434"})
    def test_uses_container_ollama_url_as_interface_default(self):
        status, body = self.request("GET", "/")

        self.assertEqual(status, 200)
        self.assertIn(b'value="http://host.docker.internal:11434"', body)
        self.assertNotIn(b"{{OLLAMA_URL}}", body)

    @patch(
        "do_pr.web.generate_pull_request_description",
        return_value=models.PullRequestDraft("Adicionar login", "## Resumo"),
    )
    def test_generates_description_from_form_request(self, generate_mock):
        payload = {
            "repository_url": "https://github.com/org/repo",
            "source_branch": "feat/login",
            "target_branch": "develop",
            "provider": "ollama",
            "model": "qwen2.5-coder:7b",
            "base_url": "http://localhost:11434",
            "api_key": "",
            "timeout": 300,
            "num_ctx": 8192,
            "change_description": "Adicionar login por OAuth",
        }

        status, body = self.request(
            "POST",
            "/api/generate",
            body=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body),
            {
                "title": "Adicionar login",
                "body": "## Resumo",
                "description": "# Adicionar login\n\n## Resumo",
            },
        )
        generate_mock.assert_called_once_with(
            repository_url="https://github.com/org/repo",
            source_branch="feat/login",
            target_branch="develop",
            provider="ollama",
            model="qwen2.5-coder:7b",
            base_url="http://localhost:11434",
            api_key=None,
            timeout=300,
            num_ctx=8192,
            github_token=None,
            change_description="Adicionar login por OAuth",
        )

    @patch(
        "do_pr.web.create_pull_request",
        return_value="https://github.com/org/repo/pull/42",
    )
    def test_creates_pr_only_through_the_confirmation_endpoint(self, create_mock):
        payload = {
            "repository_url": "https://github.com/org/repo",
            "source_branch": "feat/login",
            "target_branch": "develop",
            "title": "Adicionar login",
            "body": "## Resumo",
            "assignees": ["alice"],
            "reviewers": ["bob"],
            "labels": ["bug"],
            "project": "Roadmap",
        }

        status, body = self.request(
            "POST",
            "/api/create-pr",
            body=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(status, 201)
        self.assertEqual(
            json.loads(body),
            {"url": "https://github.com/org/repo/pull/42"},
        )
        create_mock.assert_called_once_with(
            repository_url="https://github.com/org/repo",
            source_branch="feat/login",
            target_branch="develop",
            title="Adicionar login",
            body="## Resumo",
            assignees=["alice"],
            reviewers=["bob"],
            labels=["bug"],
            project="Roadmap",
        )

    @patch(
        "do_pr.web.get_pull_request_options",
        return_value={
            "assignees": ["alice"],
            "reviewers": ["bob"],
            "labels": ["bug"],
            "projects": ["Roadmap"],
            "errors": {},
        },
    )
    def test_lists_pull_request_options_for_repository(self, options_mock):
        status, body = self.request(
            "POST",
            "/api/pr-options",
            body=json.dumps({"repository_url": "https://github.com/org/repo"}),
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body),
            {
                "assignees": ["alice"],
                "reviewers": ["bob"],
                "labels": ["bug"],
                "projects": ["Roadmap"],
                "errors": {},
            },
        )
        options_mock.assert_called_once_with("https://github.com/org/repo")

    @patch("do_pr.web.create_pull_request", side_effect=RuntimeError("not logged in"))
    def test_reports_pull_request_creation_failure(self, create_mock):
        status, body = self.request(
            "POST",
            "/api/create-pr",
            body=json.dumps(
                {
                    "repository_url": "https://github.com/org/repo",
                    "source_branch": "feat/login",
                    "target_branch": "develop",
                    "title": "Adicionar login",
                    "body": "## Resumo",
                }
            ),
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body), {"error": "not logged in"})

    def test_rejects_invalid_json_request(self):
        status, body = self.request(
            "POST",
            "/api/generate",
            body="{invalid",
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(status, 400)
        self.assertIn("JSON enviado é inválido", json.loads(body)["error"])

    @patch("do_pr.github.resolve_token", return_value="test-token")
    @patch(
        "do_pr.github.fetch_remote_branches_or_tags",
        new_callable=AsyncMock,
        return_value=["develop", "feat/login", "main"],
    )
    def test_lists_repository_branches(self, fetch_branches_mock, resolve_token_mock):
        status, body = self.request(
            "POST",
            "/api/branches",
            body=json.dumps({"repository_url": "https://github.com/org/repo"}),
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body),
            {"branches": ["develop", "feat/login", "main"]},
        )
        resolve_token_mock.assert_called_once_with(None)
        fetch_branches_mock.assert_awaited_once_with(
            "https://github.com/org/repo",
            ref_type="branches",
            token="test-token",
        )

    @patch(
        "do_pr.github.fetch_remote_branches_or_tags",
        new_callable=AsyncMock,
        side_effect=RuntimeError("Repository not found"),
    )
    def test_reports_branch_lookup_errors(self, fetch_branches_mock):
        status, body = self.request(
            "POST",
            "/api/branches",
            body=json.dumps({"repository_url": "https://github.com/org/missing"}),
            headers={"Content-Type": "application/json"},
        )

        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body), {"error": "Repository not found"})


if __name__ == "__main__":
    unittest.main()
