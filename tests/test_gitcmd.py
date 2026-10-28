"""The git guard: what the service is allowed to run, and what it refuses."""

import unittest
import subprocess
from unittest import mock

from phush import gitcmd
from phush.gitcmd import GitError


class CheckArgsCases(unittest.TestCase):
    def test_allowed_subcommand_passes(self):
        gitcmd.check_args(["status", "--porcelain"])
        gitcmd.check_args(["commit", "-m", "a message"])
        gitcmd.check_args(["push", "origin", "main"])

    def test_unknown_subcommand_is_refused(self):
        for sub in ["reset", "clean", "filter-branch", "update-ref", "gc", "worktree"]:
            with self.subTest(sub=sub), self.assertRaises(GitError):
                gitcmd.check_args([sub])

    def test_empty_command_is_refused(self):
        with self.assertRaises(GitError):
            gitcmd.check_args([])

    def test_history_destroying_flags_are_refused(self):
        cases = [
            ["push", "--force", "origin", "main"],
            ["push", "-f", "origin", "main"],
            ["push", "--force-with-lease", "origin", "main"],
            ["push", "--delete", "origin", "main"],
            ["commit", "--amend", "-m", "x"],
            ["fetch", "--prune", "origin"],
            ["clone", "--mirror", "url", "dir"],
        ]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(GitError):
                gitcmd.check_args(args)

    def test_force_refspec_is_refused(self):
        with self.assertRaises(GitError):
            gitcmd.check_args(["push", "origin", "+main:main"])

    def test_branch_deletion_refspec_is_refused(self):
        with self.assertRaises(GitError):
            gitcmd.check_args(["push", "origin", ":main"])

    def test_caller_cannot_inject_config(self):
        # -c would let a caller re-enable hooks or set a proxy.
        with self.assertRaises(GitError):
            gitcmd.check_args(["status", "-c", "core.hooksPath=/tmp/evil"])

    def test_rebase_abort_is_allowed(self):
        # This is how a failed rebase is rolled back, so it must stay reachable.
        gitcmd.check_args(["rebase", "--abort"])

    def test_message_that_looks_like_a_flag_is_refused_not_executed(self):
        # Refusing is fine; silently running --force as a commit message is not.
        with self.assertRaises(GitError):
            gitcmd.check_args(["commit", "-m", "--force"])

    def test_new_recovery_commands_have_no_delete_or_reset_forms(self):
        gitcmd.check_args(["branch", "phush-backup/abc123", "HEAD"])
        gitcmd.check_args(["merge", "--ff-only", "refs/remotes/origin/main"])
        gitcmd.check_args(["merge", "--no-edit", "refs/remotes/origin/main"])
        for args in (["branch", "-d", "main"], ["branch", "-f", "main", "HEAD"],
                     ["merge", "--abort"], ["merge", "-s", "ours", "main"],
                     ["merge", "--no-edit", "--exec=bad"],
                     ["branch", "phush-backup/../main", "HEAD"]):
            with self.subTest(args=args), self.assertRaises(GitError):
                gitcmd.check_args(args)

    def test_timeout_is_reported_as_a_git_error(self):
        with mock.patch.object(gitcmd.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("git", 1)):
            with self.assertRaisesRegex(GitError, "timed out"):
                gitcmd.run(None, ["status"])


if __name__ == "__main__":
    unittest.main()
