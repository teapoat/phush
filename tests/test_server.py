"""Exercise registered MCP tools with the actual SDK and local Git fixtures."""
import unittest

from tests import test_workspace as fixtures

try:
    from phush.server import build_server
except ModuleNotFoundError as exc:
    if exc.name not in ("mcp", "uvicorn", "dotenv"):
        raise
    build_server = None


@unittest.skipIf(build_server is None, "MCP server dependencies not installed")
class MCPTools(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.GitFlowCases("runTest")
        self.fixture.setUp()
        self.server = build_server(self.fixture.workspace.config)

    def tearDown(self):
        self.fixture.tearDown()

    async def call(self, tool, **arguments):
        result = await self.server.call_tool(tool, {"repo": "notes", **arguments})
        return "\n".join(item.text for item in result.content if hasattr(item, "text"))

    async def test_new_tools_and_parameters_are_registered(self):
        tools = {tool.name: tool for tool in await self.server.list_tools()}
        self.assertEqual(19, len(tools))
        for name in ("repo_diff", "repo_show", "repo_sync"):
            self.assertIn(name, tools)
        self.assertIn("side", tools["repo_log"].input_schema["properties"])
        self.assertIn("paths", tools["repo_diff"].input_schema["properties"])

    async def test_open_reports_unavailable_remote_without_claiming_readiness(self):
        await self.call("repo_open")
        path = self.fixture.workspace.repo_path("notes")
        fixtures.git(path, "remote", "set-url", "origin", str(self.fixture.remotes / "missing.git"))
        report = await self.call("repo_open")
        self.assertIn("not synchronized", report)
        self.assertIn("fetch failed", report)
        self.assertNotIn("notes ready on branch", report)

    async def test_read_is_stable_and_explicit_check_exposes_remote_changes(self):
        await self.call("repo_open")
        path = self.fixture.workspace.repo_path("notes")
        before = fixtures.git(path, "rev-parse", "HEAD", "origin/main").stdout
        self.fixture.push_from_seed("new remote text\n", "new remote title")
        report = await self.call("read_file", path="TODO.md")
        self.assertIn("milk", report)
        self.assertEqual(before, fixtures.git(path, "rev-parse", "HEAD", "origin/main").stdout)
        self.assertIn("fetched", await self.call("repo_sync"))
        self.assertIn("new remote title", await self.call("repo_log", side="remote"))
        self.assertIn("new remote text", await self.call("repo_diff", scope="remote-only"))
        oid = fixtures.git(self.fixture.seed, "rev-parse", "HEAD").stdout.strip()
        self.assertIn("new remote title", await self.call("repo_show", commit=oid))

    async def test_phone_can_resolve_and_publish_a_conflict(self):
        await self.call("repo_open")
        await self.call("patch_file", path="TODO.md", old_text="milk", new_text="MILK")
        self.fixture.push_from_seed("- [ ] milk and tea\n- [ ] bread\n", "remote title")
        report = await self.call("commit_push", message="local title", paths=["TODO.md"])
        self.assertIn("merge conflict", report)
        path = self.fixture.workspace.repo_path("notes") / "TODO.md"
        conflict = path.read_text()
        self.assertIn("<<<<<<<", await self.call("read_file", path="TODO.md"))
        await self.call("patch_file", path="TODO.md", old_text=conflict,
                        new_text="- [ ] MILK and tea\n- [ ] bread\n")
        report = await self.call("commit_push", message="keep both", paths=["TODO.md"])
        self.assertIn("pushed", report)
        remote = fixtures.git(self.fixture.bare, "show", "main:TODO.md").stdout
        self.assertEqual("- [ ] MILK and tea\n- [ ] bread\n", remote)
