"""File reading, searching and patching."""

import tempfile
import unittest
from pathlib import Path

from phush import editor
from phush.editor import EditError

SAMPLE = "alpha\nbeta\ngamma\nbeta\ndelta\n"


class EditorCases(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.file = self.root / "notes.md"
        self.file.write_text(SAMPLE, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    # ---- reading ----

    def test_read_lines_numbers_them(self):
        out = editor.read_lines(self.file, 10_000)
        self.assertIn("1\talpha", out)
        self.assertIn("5\tdelta", out)

    def test_read_lines_window(self):
        out = editor.read_lines(self.file, 10_000, offset=2, limit=2)
        self.assertIn("2\tbeta", out)
        self.assertIn("3\tgamma", out)
        self.assertNotIn("alpha", out)
        self.assertIn("more lines", out)

    def test_read_past_the_end_says_so(self):
        self.assertIn("past the end", editor.read_lines(self.file, 10_000, offset=99))

    def test_missing_file_is_refused(self):
        with self.assertRaises(EditError):
            editor.read_text(self.root / "nope.md", 10_000)

    def test_file_over_the_read_limit_is_refused(self):
        with self.assertRaises(EditError) as caught:
            editor.read_text(self.file, 5)
        self.assertIn("read limit", str(caught.exception))

    def test_binary_file_is_refused(self):
        binary = self.root / "image.bin"
        binary.write_bytes(b"\xff\xfe\x00\x01 not text")
        with self.assertRaises(EditError):
            editor.read_text(binary, 10_000)

    # ---- patching ----

    def test_patch_replaces_unique_text(self):
        editor.patch(self.file, "gamma", "GAMMA", 1, 10_000)
        self.assertIn("GAMMA", self.file.read_text(encoding="utf-8"))

    def test_patch_refuses_when_text_is_missing(self):
        with self.assertRaises(EditError) as caught:
            editor.patch(self.file, "epsilon", "x", 1, 10_000)
        self.assertIn("not found", str(caught.exception))
        self.assertEqual(SAMPLE, self.file.read_text(encoding="utf-8"))

    def test_patch_refuses_ambiguous_text_and_says_the_count(self):
        with self.assertRaises(EditError) as caught:
            editor.patch(self.file, "beta", "x", 1, 10_000)
        self.assertIn("appears 2 times", str(caught.exception))
        self.assertEqual(SAMPLE, self.file.read_text(encoding="utf-8"))

    def test_patch_replaces_several_when_asked(self):
        editor.patch(self.file, "beta", "BETA", 2, 10_000)
        self.assertEqual(2, self.file.read_text(encoding="utf-8").count("BETA"))

    def test_patch_refuses_empty_or_identical_text(self):
        with self.assertRaises(EditError):
            editor.patch(self.file, "", "x", 1, 10_000)
        with self.assertRaises(EditError):
            editor.patch(self.file, "beta", "beta", 1, 10_000)

    # ---- appending and writing ----

    def test_append_adds_a_newline_when_needed(self):
        no_newline = self.root / "flat.md"
        no_newline.write_text("one", encoding="utf-8")
        editor.append(no_newline, "two", 10_000)
        self.assertEqual("one\ntwo", no_newline.read_text(encoding="utf-8"))

    def test_append_creates_a_missing_file(self):
        fresh = self.root / "sub" / "new.md"
        editor.append(fresh, "hello", 10_000)
        self.assertEqual("hello", fresh.read_text(encoding="utf-8"))

    def test_write_over_the_limit_is_refused(self):
        with self.assertRaises(EditError):
            editor.write(self.file, "x" * 100, 10)
        self.assertEqual(SAMPLE, self.file.read_text(encoding="utf-8"))

    def test_write_reports_create_versus_overwrite(self):
        self.assertIn("overwrote", editor.write(self.file, "new", 10_000))
        self.assertIn("created", editor.write(self.root / "other.md", "new", 10_000))

    # ---- checksums ----

    def test_sha256_matches_hashlib(self):
        import hashlib

        expected = hashlib.sha256(SAMPLE.encode("utf-8")).hexdigest()
        self.assertTrue(editor.sha256(self.file).startswith(expected))

    def test_sha256_works_on_binary_files(self):
        binary = self.root / "image.bin"
        binary.write_bytes(b"\xff\xfe\x00\x01")
        self.assertIn("image.bin", editor.sha256(binary))

    def test_sha256_of_a_missing_file_is_refused(self):
        with self.assertRaises(EditError):
            editor.sha256(self.root / "nope.md")

    # ---- searching ----

    def test_grep_finds_matches_with_line_numbers(self):
        result = editor.grep(self.root, ["notes.md"], r"^beta$", 100, 10_000)
        self.assertEqual([2, 4], [m.line_number for m in result.matches])
        self.assertEqual([], result.skipped_too_big)
        self.assertFalse(result.hit_match_limit)

    def test_grep_stops_at_the_match_limit_and_says_so(self):
        result = editor.grep(self.root, ["notes.md"], "a", 2, 10_000)
        self.assertEqual(2, len(result.matches))
        self.assertTrue(result.hit_match_limit)

    def test_grep_skips_binary_files_quietly(self):
        (self.root / "image.bin").write_bytes(b"\xff\xfe beta")
        result = editor.grep(
            self.root, ["image.bin", "notes.md"], "beta", 100, 10_000
        )
        self.assertTrue(all(m.path == "notes.md" for m in result.matches))
        self.assertEqual([], result.skipped_too_big)

    def test_grep_does_not_name_an_oversized_binary_file(self):
        (self.root / "big.bin").write_bytes(b"\x00\xff" * 500)
        result = editor.grep(self.root, ["big.bin", "notes.md"], "beta", 100, 100)
        self.assertEqual([], result.skipped_too_big)

    def test_grep_names_the_file_it_skipped_for_size(self):
        big = self.root / "huge.md"
        big.write_text("beta\n" * 500, encoding="utf-8")
        result = editor.grep(self.root, ["huge.md", "notes.md"], "beta", 100, 100)
        self.assertEqual(["huge.md"], result.skipped_too_big)
        self.assertTrue(all(m.path == "notes.md" for m in result.matches))

    def test_grep_skips_excluded_paths_and_counts_them(self):
        archive = self.root / "dumps" / "raw"
        archive.mkdir(parents=True)
        (archive / "page1.html").write_text("beta here", encoding="utf-8")
        result = editor.grep(
            self.root,
            ["dumps/raw/page1.html", "notes.md"],
            "beta",
            100,
            10_000,
            exclude=["*/raw/*"],
        )
        self.assertTrue(all(m.path == "notes.md" for m in result.matches))
        self.assertEqual(1, result.excluded)

    def test_grep_rejects_a_bad_pattern(self):
        with self.assertRaises(EditError):
            editor.grep(self.root, ["notes.md"], "([unclosed", 100, 10_000)

    def test_patch_says_which_limit_refused_an_oversized_file(self):
        with self.assertRaises(EditError) as caught:
            editor.patch(self.file, "gamma", "GAMMA", 1, 2)
        self.assertIn("patch limit", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
