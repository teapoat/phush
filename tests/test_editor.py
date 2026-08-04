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
        matches = editor.grep(self.root, ["notes.md"], r"^beta$", 100, 10_000)
        self.assertEqual([2, 4], [m.line_number for m in matches])

    def test_grep_stops_at_the_match_limit(self):
        matches = editor.grep(self.root, ["notes.md"], "a", 2, 10_000)
        self.assertEqual(2, len(matches))

    def test_grep_skips_binary_and_oversized_files(self):
        (self.root / "image.bin").write_bytes(b"\xff\xfe beta")
        matches = editor.grep(self.root, ["image.bin", "notes.md"], "beta", 100, 10_000)
        self.assertTrue(all(m.path == "notes.md" for m in matches))

    def test_grep_rejects_a_bad_pattern(self):
        with self.assertRaises(EditError):
            editor.grep(self.root, ["notes.md"], "([unclosed", 100, 10_000)


if __name__ == "__main__":
    unittest.main()
