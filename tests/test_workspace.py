"""Clone handling and the commit/push flow, against real throwaway repositories.

These tests create a bare repository on disk and point the workspace at it, so the
whole path — clone, edit, commit, rebase, push — runs for real without a network.
"""

import subprocess
import tempfile
import unittest
from unittest import mock
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
        self.assertTrue((state.path / "other.md").exists())
        self.assertEqual("", git(self.bare, "ls-tree", "--name-only", "main", "other.md").stdout)

    def test_commit_push_merges_a_remote_commit_in_a_different_file(self):
        state = self.workspace.open("notes")
        (state.path / "notes.md").write_text("server side\n", encoding="utf-8")
        self.push_from_seed("- [ ] milk\n- [ ] bread\n- [ ] jam\n", "add jam")

        report = self.workspace.commit_push("notes", "add a note")
        self.assertIn("pushed to main", report)
        history = " ".join(self.remote_log())
        self.assertIn("note", history)
        self.assertIn("jam", history)

    def test_conflicting_commit_is_not_pushed_and_can_be_inspected(self):
        state = self.workspace.open("notes")
        (state.path / "TODO.md").write_text("- [x] milk\n- [ ] bread\n", encoding="utf-8")
        self.push_from_seed("- [ ] MILK\n- [ ] bread\n", "shout about milk")

        report = self.workspace.commit_push("notes", "tick off the milk")
        self.assertIn("conflict", report.lower())
        self.assertNotIn("tick", " ".join(self.remote_log()))
        # Conflicts remain inspectable, with both histories preserved and no rebase.
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

    def test_open_reports_failed_fetch_and_preserves_files(self):
        state = self.workspace.open("notes")
        git(state.path, "remote", "set-url", "origin", str(self.remotes / "missing.git"))
        reopened = self.workspace.open("notes")
        self.assertFalse(reopened.ready)
        self.assertIn("blocked", reopened.sync_report)
        self.assertEqual("- [ ] milk\n- [ ] bread\n", (state.path / "TODO.md").read_text())

    def test_read_open_does_not_fetch_or_fast_forward(self):
        state = self.workspace.open("notes")
        before = git(state.path, "rev-parse", "HEAD", "origin/main").stdout
        self.push_from_seed("remote change\n", "remote only")
        self.workspace.open("notes", update=False)
        self.assertEqual(before, git(state.path, "rev-parse", "HEAD", "origin/main").stdout)

    def test_dirty_tree_blocks_integration_without_changing_index_or_files(self):
        state = self.workspace.open("notes")
        (state.path / "TODO.md").write_text("local work\n")
        git(state.path, "add", "TODO.md")
        (state.path / "TODO.md").write_text("more local work\n")
        (state.path / "new.md").write_text("untracked\n")
        before = git(state.path, "diff", "--cached").stdout
        head = git(state.path, "rev-parse", "HEAD").stdout
        self.push_from_seed("remote change\n", "remote only")
        report = self.workspace.repo_sync("notes", "merge")
        self.assertIn("dirty tree preserved", report)
        self.assertEqual(head, git(state.path, "rev-parse", "HEAD").stdout)
        self.assertEqual(before, git(state.path, "diff", "--cached").stdout)
        self.assertEqual("more local work\n", (state.path / "TODO.md").read_text())
        self.assertEqual("untracked\n", (state.path / "new.md").read_text())
        self.assertFalse(self.workspace.open("notes").ready)

    def test_diff_distinguishes_index_worktree_and_all_local_edits(self):
        state = self.workspace.open("notes")
        (state.path / "TODO.md").write_text("staged text\n")
        git(state.path, "add", "TODO.md")
        (state.path / "TODO.md").write_text("unstaged text\n")
        self.assertIn("+staged text", self.workspace.diff("notes", "index"))
        worktree = self.workspace.diff("notes", "worktree")
        self.assertIn("-staged text", worktree)
        self.assertIn("+unstaged text", worktree)
        self.assertIn("- [ ] milk", self.workspace.diff("notes", "head"))

    def test_sides_show_only_their_own_commits_and_changes(self):
        state = self.workspace.open("notes")
        (state.path / "local.md").write_text("local content\n")
        git(state.path, "add", "local.md")
        git(state.path, "commit", "-m", "local title")
        head = git(state.path, "rev-parse", "HEAD").stdout
        self.push_from_seed("remote content\n", "remote title")
        self.workspace.repo_sync("notes", "check")
        self.assertEqual(head, git(state.path, "rev-parse", "HEAD").stdout)
        self.assertIn("local title", self.workspace.log("notes", side="local"))
        self.assertNotIn("remote title", self.workspace.log("notes", side="local"))
        self.assertIn("remote title", self.workspace.log("notes", side="remote"))
        self.assertNotIn("local title", self.workspace.log("notes", side="remote"))
        self.assertIn("local content", self.workspace.diff("notes", "local-only"))
        self.assertNotIn("remote content", self.workspace.diff("notes", "local-only"))
        self.assertIn("remote content", self.workspace.diff("notes", "remote-only"))
        self.assertIn("local title", self.workspace.show("notes", "HEAD"))
        self.assertFalse(self.workspace.open("notes").ready)

    def test_diff_paths_are_literal_and_cannot_escape(self):
        state = self.workspace.open("notes")
        name = ":(glob)*.md"
        (state.path / name).write_text("literal content\n")
        git(state.path, "add", "--", ":(literal)" + name)
        report = self.workspace.diff("notes", "index", [name])
        self.assertIn("literal content", report)
        self.assertNotIn("milk", report)
        for path in ("../outside", ".git/config", "/etc/passwd"):
            with self.subTest(path=path), self.assertRaises(WorkspaceError):
                self.workspace.diff("notes", paths=[path])
        for ref in ("--all", "HEAD:path", "main;echo bad"):
            with self.subTest(ref=ref), self.assertRaises(WorkspaceError):
                self.workspace.show("notes", ref)

    def test_diff_has_line_pagination_and_a_character_cap(self):
        state = self.workspace.open("notes")
        (state.path / "TODO.md").write_text("long" * 10000)
        report = self.workspace.diff("notes")
        self.assertLess(len(report), 20200)
        self.assertIn("truncated", report)
        first = self.workspace.diff("notes", limit=1)
        self.assertIn("next offset 2", first)
        self.assertNotEqual(first, self.workspace.diff("notes", offset=2, limit=1))

    def test_sync_creates_backup_before_fast_forward(self):
        state = self.workspace.open("notes")
        before = git(state.path, "rev-parse", "HEAD").stdout.strip()
        self.push_from_seed("remote\n", "remote only")
        self.assertIn("synced:", self.workspace.repo_sync("notes", "ff-only"))
        backups = git(state.path, "for-each-ref", "--format=%(objectname)",
                      "refs/heads/phush-backup/").stdout.splitlines()
        self.assertIn(before, backups)

    def test_clean_tree_retries_existing_local_commit_without_rewriting_it(self):
        state = self.workspace.open("notes")
        (state.path / "local.md").write_text("local\n")
        git(state.path, "add", "local.md")
        git(state.path, "commit", "-m", "previously unpushed")
        head = git(state.path, "rev-parse", "HEAD").stdout.strip()
        self.assertIn("pushed", self.workspace.commit_push("notes", "retry", []))
        self.assertEqual(head, git(self.bare, "rev-parse", "main").stdout.strip())

    def test_merging_retains_both_commit_ids(self):
        state = self.workspace.open("notes")
        (state.path / "local.md").write_text("local\n")
        git(state.path, "add", "local.md")
        git(state.path, "commit", "-m", "local title")
        local = git(state.path, "rev-parse", "HEAD").stdout.strip()
        self.push_from_seed("remote\n", "remote title")
        git(state.path, "config", "merge.ff", "only")
        remote = git(self.seed, "rev-parse", "HEAD").stdout.strip()
        report = self.workspace.repo_sync("notes", "ff-only")
        self.assertIn("diverged", report)
        self.assertEqual(local, git(state.path, "rev-parse", "HEAD").stdout.strip())
        self.assertIn("synced:", self.workspace.repo_sync("notes", "merge"))
        git(state.path, "merge-base", "--is-ancestor", local, "HEAD")
        git(state.path, "merge-base", "--is-ancestor", remote, "HEAD")

    def test_conflict_can_be_inspected_patched_and_completed(self):
        state = self.workspace.open("notes")
        (state.path / "TODO.md").write_text("local\n")
        self.push_from_seed("remote\n", "remote title")
        report = self.workspace.commit_push("notes", "local title", ["TODO.md"])
        self.assertIn("merge conflict", report)
        self.assertIn("TODO.md", self.workspace.diff("notes"))
        self.assertIn("merge in progress", self.workspace.commit_push("notes", "finish"))
        self.assertIn("conflict markers", self.workspace.commit_push("notes", "finish", ["TODO.md"]))
        (state.path / "TODO.md").write_text("local\nremote\n")
        report = self.workspace.commit_push("notes", "resolve both", ["TODO.md"])
        self.assertIn("pushed", report)
        self.assertFalse((state.path / ".git" / "MERGE_HEAD").exists())
        self.assertEqual("local\nremote\n", git(self.bare, "show", "main:TODO.md").stdout)
        self.assertIn("local title", " ".join(self.remote_log()))
        self.assertIn("remote title", " ".join(self.remote_log()))

    def test_named_commit_does_not_include_other_previously_staged_work(self):
        state = self.workspace.open("notes")
        (state.path / "other.md").write_text("someone else's work\n")
        git(state.path, "add", "other.md")
        (state.path / "TODO.md").write_text("selected\n")
        before = git(state.path, "diff", "--cached").stdout
        report = self.workspace.commit_push("notes", "selected", ["TODO.md"])
        self.assertIn("other paths were already staged", report)
        self.assertEqual(before, git(state.path, "diff", "--cached").stdout)

    def test_foreign_operation_blocks_push_before_staging(self):
        state = self.workspace.open("notes")
        (state.path / ".git" / "rebase-merge").mkdir()
        (state.path / "TODO.md").write_text("local\n")
        report = self.workspace.commit_push("notes", "local", ["TODO.md"])
        self.assertIn("another Git operation", report)
        self.assertEqual("", git(state.path, "diff", "--cached").stdout)

    def test_fetch_failure_keeps_the_new_local_commit_for_retry(self):
        state = self.workspace.open("notes")
        (state.path / "local.md").write_text("saved locally\n")
        git(state.path, "remote", "set-url", "origin", str(self.remotes / "missing.git"))
        report = self.workspace.commit_push("notes", "keep this", ["local.md"])
        self.assertIn("local commits intact", report)
        self.assertIn("fetch failed", report)
        self.assertEqual("saved locally\n", git(state.path, "show", "HEAD:local.md").stdout)
        self.assertFalse(git(state.path, "status", "--porcelain").stdout)

    def test_push_race_is_rejected_without_losing_the_local_commit(self):
        state = self.workspace.open("notes")
        (state.path / "local.md").write_text("saved locally\n")
        original = self.workspace._git

        def race(path, args, check=False):
            if args[0] == "push":
                self.push_from_seed("concurrent remote work\n", "racing title")
            return original(path, args, check)

        with mock.patch.object(self.workspace, "_git", side_effect=race):
            report = self.workspace.commit_push("notes", "local title", ["local.md"])
        self.assertIn("push failed", report)
        self.assertEqual("saved locally\n", git(state.path, "show", "HEAD:local.md").stdout)
        self.assertNotIn("local title", " ".join(self.remote_log()))

    def test_unrelated_histories_are_not_combined(self):
        state = self.workspace.open("notes")
        before = git(state.path, "rev-parse", "HEAD").stdout
        other = self.remotes / "other.git"
        other_seed = self.remotes.parent / "other-seed"
        git(self.remotes, "init", "--bare", "--initial-branch=main", str(other))
        git(self.remotes, "init", "--initial-branch=main", str(other_seed))
        (other_seed / "different.md").write_text("independent\n")
        git(other_seed, "add", "different.md")
        git(other_seed, "commit", "-m", "independent root")
        git(other_seed, "remote", "add", "origin", str(other))
        git(other_seed, "push", "origin", "main")
        git(state.path, "remote", "set-url", "origin", str(other))
        report = self.workspace.repo_sync("notes", "merge")
        self.assertIn("unrelated histories", report)
        self.assertEqual(before, git(state.path, "rev-parse", "HEAD").stdout)
        self.assertFalse((state.path / ".git" / "MERGE_HEAD").exists())

    def test_deleted_remote_branch_is_not_treated_as_fresh_cached_data(self):
        state = self.workspace.open("notes")
        git(self.bare, "branch", "-m", "main", "alternate")
        report = self.workspace.repo_sync("notes", "ff-only")
        self.assertIn("fetch failed", report)
        self.assertFalse(self.workspace.open("notes").ready)

    def test_retry_without_staging_preserves_index_and_untracked_files(self):
        state = self.workspace.open("notes")
        (state.path / "local.md").write_text("previous commit\n")
        git(state.path, "add", "local.md")
        git(state.path, "commit", "-m", "previous title")
        before = git(state.path, "rev-parse", "HEAD").stdout
        (state.path / "TODO.md").write_text("unrelated staged work\n")
        git(state.path, "add", "TODO.md")
        (state.path / "untracked.md").write_text("unrelated new file\n")
        index = git(state.path, "diff", "--cached").stdout
        self.assertIn("pushed", self.workspace.commit_push("notes", "retry", []))
        self.assertEqual(before, git(self.bare, "rev-parse", "main").stdout)
        self.assertEqual(index, git(state.path, "diff", "--cached").stdout)
        self.assertEqual("unrelated new file\n", (state.path / "untracked.md").read_text())


if __name__ == "__main__":
    unittest.main()
