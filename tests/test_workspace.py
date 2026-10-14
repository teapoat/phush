"""Clone handling and the commit/push flow, against real throwaway repositories.

These tests create a bare repository on disk and point the workspace at it, so the
whole path — clone, edit, commit, rebase, push — runs for real without a network.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from phush.config import Config
from phush.workspace import Workspace, WorkspaceError, safe_join


def git(cwd, *args):
    """Plain git for building fixtures — not the guarded wrapper under test."""
    return subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def make_config(root: Path, remotes: Path, allow=()) -> Config:
    return Config(
        host="127.0.0.1",
        port=8766,
        owner="tester",
        remote_template=str(remotes) + "/{repo}.git",
        root=root,
        default_branch="main",
        allow_repos=frozenset(allow),
        author_name="phush-test",
        author_email="phush@example.com",
        max_read_bytes=5_000_000,
        search_exclude=(),
        max_patch_bytes=5_000_000,
        max_write_bytes=1_000_000,
        max_grep_matches=100,
        git_timeout_seconds=60,
        rate_max_requests=1000,
        rate_window_seconds=60,
        token="unused",  # these tests never build the HTTP layer
    )


class SafeJoinCases(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_plain_path_resolves_inside_the_repo(self):
        self.assertEqual(self.root.resolve() / "docs" / "a.md",
                         safe_join(self.root, "docs/a.md"))

    def test_absolute_path_is_refused(self):
        with self.assertRaises(WorkspaceError):
            safe_join(self.root, "/etc/passwd")

    def test_parent_traversal_is_refused(self):
        for bad in ["../secrets", "docs/../../secrets", ".."]:
            with self.subTest(path=bad), self.assertRaises(WorkspaceError):
                safe_join(self.root, bad)

    def test_git_directory_is_refused(self):
        for bad in [".git/config", "docs/.git/hooks/pre-commit"]:
            with self.subTest(path=bad), self.assertRaises(WorkspaceError):
                safe_join(self.root, bad)

    def test_empty_path_is_refused(self):
        with self.assertRaises(WorkspaceError):
            safe_join(self.root, "   ")

    def test_symlink_out_of_the_repo_is_refused(self):
        outside = Path(tempfile.mkdtemp())
        (self.root / "escape").symlink_to(outside)
        with self.assertRaises(WorkspaceError):
            safe_join(self.root, "escape/loot.txt")


class RepoNameCases(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.workspace = Workspace(make_config(base / "clones", base / "remotes"))

    def tearDown(self):
        self._tmp.cleanup()

    def test_bad_names_are_refused(self):
        for bad in ["../evil", "-flag", "a b", "", "sub/dir", ".hidden"]:
            with self.subTest(name=bad), self.assertRaises(WorkspaceError):
                self.workspace.check_name(bad)

    def test_ordinary_names_pass(self):
        for good in ["notes", "arm_table", "my.repo-2"]:
            self.assertEqual(good, self.workspace.check_name(good))

    def test_allow_list_restricts_when_it_is_not_empty(self):
        base = Path(self._tmp.name)
        limited = Workspace(
            make_config(base / "c2", base / "r2", allow=("notes",))
        )
        self.assertEqual("notes", limited.check_name("notes"))
        with self.assertRaises(WorkspaceError):
            limited.check_name("other")


class GitFlowCases(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.remotes = base / "remotes"
        self.remotes.mkdir(parents=True)
        self.clones = base / "clones"

        # A bare "GitHub" repository with one commit on main.
        self.bare = self.remotes / "notes.git"
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(self.bare)],
            check=True,
            capture_output=True,
        )
        self.seed = base / "seed"
        subprocess.run(
            ["git", "clone", str(self.bare), str(self.seed)],
            check=True,
            capture_output=True,
        )
        (self.seed / "TODO.md").write_text("- [ ] milk\n- [ ] bread\n", encoding="utf-8")
        git(self.seed, "add", "-A")
        git(self.seed, "commit", "-m", "first")
        git(self.seed, "push", "origin", "main")

        self.workspace = Workspace(make_config(self.clones, self.remotes))

    def tearDown(self):
        self._tmp.cleanup()

    def remote_log(self):
        out = subprocess.run(
            ["git", "log", "--format=%s", "main"],
            cwd=str(self.bare),
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.split()

    def push_from_seed(self, text, message):
        (self.seed / "TODO.md").write_text(text, encoding="utf-8")
        git(self.seed, "add", "-A")
        git(self.seed, "commit", "-m", message)
        git(self.seed, "push", "origin", "main")

    def test_open_clones_then_reuses(self):
        first = self.workspace.open("notes")
        self.assertTrue(first.cloned_now)
        self.assertEqual("main", first.branch)
        self.assertTrue((first.path / "TODO.md").is_file())

        second = self.workspace.open("notes")
        self.assertFalse(second.cloned_now)

    def test_open_pulls_new_commits_from_the_remote(self):
        state = self.workspace.open("notes")
        self.push_from_seed("- [ ] milk\n- [ ] bread\n- [ ] jam\n", "add jam")
        self.workspace.open("notes")
        self.assertIn("jam", (state.path / "TODO.md").read_text(encoding="utf-8"))

    def test_missing_repository_is_reported_not_crashed(self):
        with self.assertRaises(Exception):
            self.workspace.open("does-not-exist")

    def test_patch_then_commit_push_reaches_the_remote(self):
        state = self.workspace.open("notes")
        target = state.path / "TODO.md"
        target.write_text("- [x] milk\n- [ ] bread\n", encoding="utf-8")

        report = self.workspace.commit_push("notes", "tick off the milk")
        self.assertIn("pushed to main", report)
        self.assertIn("TODO.md", report)
        self.assertIn("tick", " ".join(self.remote_log()))

    def test_commit_push_with_no_changes_says_so(self):
        self.workspace.open("notes")
        self.assertIn("nothing to commit",
                      self.workspace.commit_push("notes", "empty"))

    def test_commit_push_needs_a_message(self):
        self.workspace.open("notes")
        with self.assertRaises(WorkspaceError):
            self.workspace.commit_push("notes", "   ")

    def test_commit_push_only_the_named_paths(self):
        state = self.workspace.open("notes")
        (state.path / "TODO.md").write_text("- [x] milk\n", encoding="utf-8")
        (state.path / "other.md").write_text("not part of this commit\n", encoding="utf-8")

        report = self.workspace.commit_push("notes", "just the todo", ["TODO.md"])
        self.assertIn("TODO.md", report)
        self.assertNotIn("other.md", report)

    def test_commit_push_rebases_over_an_unrelated_remote_commit(self):
        state = self.workspace.open("notes")
        (state.path / "notes.md").write_text("server side\n", encoding="utf-8")
        self.push_from_seed("- [ ] milk\n- [ ] bread\n- [ ] jam\n", "add jam")

        report = self.workspace.commit_push("notes", "add a note")
        self.assertIn("pushed to main", report)
        history = " ".join(self.remote_log())
        self.assertIn("note", history)
        self.assertIn("jam", history)

    def test_conflicting_commit_is_not_pushed_and_leaves_a_clean_tree(self):
        state = self.workspace.open("notes")
        (state.path / "TODO.md").write_text("- [x] milk\n- [ ] bread\n", encoding="utf-8")
        self.push_from_seed("- [ ] MILK\n- [ ] bread\n", "shout about milk")

        report = self.workspace.commit_push("notes", "tick off the milk")
        self.assertIn("conflict", report.lower())
        self.assertNotIn("tick", " ".join(self.remote_log()))
        # The abort must leave a usable repository, not a half-finished rebase.
        self.assertFalse((state.path / ".git" / "rebase-merge").exists())

    def test_status_and_log_report_the_repository(self):
        self.workspace.open("notes")
        self.assertIn("main", self.workspace.status("notes"))
        self.assertIn("first", self.workspace.log("notes"))

    def test_status_of_an_unopened_repo_is_a_message_not_a_crash(self):
        self.assertIn("not open", self.workspace.status("notes"))

    def test_tracked_files_lists_committed_files(self):
        self.workspace.open("notes")
        self.assertEqual(["TODO.md"], self.workspace.tracked_files("notes"))

    def test_list_clones_sees_the_clone(self):
        self.assertEqual([], self.workspace.list_clones())
        self.workspace.open("notes")
        self.assertEqual(["notes"], self.workspace.list_clones())


if __name__ == "__main__":
    unittest.main()
