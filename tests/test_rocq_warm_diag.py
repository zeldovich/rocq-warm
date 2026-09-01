"""Diagnostic locations, checked against `coqc` on the shapes that move them.

`rocq repl -emacs` measures `Toplevel input, characters A-B` from the start of
the line on which Rocq RESUMED READING -- so blank lines, indentation and
comment blocks between two sentences all inflate A, and when two sentences
share a line the anchor is that line instead.  Each case here is a shape that
moves the anchor; the assertion is always the same: the same string `coqc`
prints.
"""

import unittest

from rocq_warm_helpers import Workspace, render_all, requires_rocq
from rocqwarm import session as session_mod


PREAMBLE = b"Definition a := 1.\n"


@requires_rocq
class LocationTests(unittest.TestCase):
    NAME = "D.v"

    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.cleanup)

    def assertSameAsCoqc(self, text, label):
        path = self.ws.write(self.NAME, text)
        sess = session_mod.Session(path, self.ws.flags, cwd=self.ws.dir)
        sess.start()
        self.addCleanup(sess.stop)
        result = sess.check(text, timeout=300)
        _rc, cold = self.ws.coqc(self.NAME)
        self.assertEqual(render_all(result, self.NAME, text), cold, label)

    def test_error_on_the_line_after_the_previous_sentence(self):
        self.assertSameAsCoqc(
            PREAMBLE + b"Definition l := plus a bogus.\n", "adjacent lines")

    def test_blank_lines_before_the_failing_sentence(self):
        for n in (1, 2, 5):
            with self.subTest(blank_lines=n):
                self.assertSameAsCoqc(
                    PREAMBLE + b"\n" * n + b"Definition l := plus a bogus.\n",
                    "%d blank lines" % n)

    def test_comment_block_before_the_failing_sentence(self):
        self.assertSameAsCoqc(
            PREAMBLE + b"(* one\n   two\n   three *)\n\n"
            b"Definition l := plus a bogus.\n", "comment block in the gap")

    def test_indented_sentence(self):
        self.assertSameAsCoqc(
            PREAMBLE + b"\n      Definition l := plus a bogus.\n", "indented")

    def test_trailing_comment_on_the_previous_line(self):
        """The case that shifts the anchor backwards, and ordinary style here:
        `Require Import WpInstr.   (* ... *)`.  Rocq meets the comment before
        any newline, so the anchor stays on the PREVIOUS sentence's line and
        every column on this one is offset by the comment's width."""
        self.assertSameAsCoqc(
            b"Definition a := 1. (* why a is one *)\n"
            b"Definition l := plus a bogus.\n", "trailing comment")

    def test_trailing_comment_then_a_blank_line(self):
        self.assertSameAsCoqc(
            b"Definition a := 1. (* why *)\n\n"
            b"Definition l := plus a bogus.\n", "trailing comment, blank line")

    def test_trailing_comment_then_another_comment(self):
        self.assertSameAsCoqc(
            b"Definition a := 1. (* why *)\n(* and also *)\n"
            b"Definition l := plus a bogus.\n", "two comments")

    def test_trailing_spaces_before_the_newline(self):
        self.assertSameAsCoqc(
            b"Definition a := 1.   \n\n"
            b"Definition l := plus a bogus.\n", "trailing spaces")

    def test_comment_between_two_sentences_on_one_line(self):
        self.assertSameAsCoqc(
            b"Lemma z : False.\nProof. (* here *) exact bogus. Qed.\n",
            "comment mid-line")

    def test_two_sentences_on_one_line(self):
        self.assertSameAsCoqc(
            b"Lemma z : False.\nProof. exact bogus. Qed.\n", "shared line")

    def test_error_inside_a_multi_line_sentence(self):
        self.assertSameAsCoqc(
            PREAMBLE + b"\nDefinition l :=\n   plus\n     a\n     bogus.\n",
            "error on the last line of a multi-line sentence")

    def test_span_crossing_a_line_break(self):
        self.assertSameAsCoqc(
            PREAMBLE + b"Definition l :=\n   a\n   a.\n", "span across lines")

    def test_parse_error_has_no_sentence_range(self):
        self.assertSameAsCoqc(
            PREAMBLE + b"\n\nDefinition l := (1 +).\n", "syntax error")

    def test_error_after_a_multibyte_character(self):
        """Rocq counts BYTES, and so must we -- a UTF-8 identifier before the
        error is the case where a character count would drift."""
        self.assertSameAsCoqc(
            "Definition é := 1.\n\nDefinition l := plus é bogus.\n".encode(),
            "utf-8 before the error")

    def test_warning_location(self):
        self.assertSameAsCoqc(
            b"Require Import List.\nDefinition l := 1.\n", "deprecation warning")

    def test_error_in_the_very_first_sentence(self):
        self.assertSameAsCoqc(b"Definition l := plus 1 bogus.\n", "first sentence")

    def test_a_replay_still_reports_the_warnings_above_the_edit(self):
        """A warm run never re-executes the prefix, so its warnings have to be
        remembered and re-emitted -- otherwise a replay silently drops every
        warning above the edit and stops matching `coqc`."""
        good = b"Require Import List.\n\nDefinition l := plus 1 2.\n"
        bad = b"Require Import List.\n\nDefinition l := plus 1 bogus.\n"
        path = self.ws.write(self.NAME, good)
        sess = session_mod.Session(path, self.ws.flags, cwd=self.ws.dir)
        sess.start()
        self.addCleanup(sess.stop)
        first = sess.check(good, timeout=300)
        self.assertTrue(any(d.kind == "warning" for d in first.diags),
                        "expected the Stdlib deprecation warning")
        self.ws.write(self.NAME, bad)
        result = sess.check(bad, timeout=300)
        self.assertEqual(result.mode, "replay")
        _rc, cold = self.ws.coqc(self.NAME)
        self.assertEqual(render_all(result, self.NAME, bad), cold)

    def test_repl_only_wording_is_rewritten_to_coqc_s(self):
        """`rocq repl` parses at a different grammar entry than `coqc`, so its
        syntax errors name `toplevel:vernac_toplevel` where `coqc` says
        `vernac`."""
        self.assertSameAsCoqc(
            PREAMBLE + b"\nrocq_warm_probe_failure.\n", "illegal begin of vernac")

    def test_locations_survive_a_whitespace_only_edit(self):
        """A comment-only edit re-executes nothing, so the cached locations are
        reused as they stand -- but the sentence right after the edited gap is
        anchored ON that gap, and does not simply move with the text."""
        head = (b'#[deprecated(since="1.0", note="use new")]\n'
                b"Definition old := 1.\n")
        tail = b"Definition use := old.\nDefinition b := 2.\n"
        before = head + b"(* a gap comment *)\n" + tail
        after = head + b"(* a much longer gap comment here *)\n" + tail
        path = self.ws.write(self.NAME, before)
        sess = session_mod.Session(path, self.ws.flags, cwd=self.ws.dir)
        sess.start()
        self.addCleanup(sess.stop)
        self.assertTrue(sess.check(before, timeout=300).ok)
        result = sess.check(after, timeout=300)
        self.assertEqual(result.mode, "shift")
        self.ws.write(self.NAME, after)
        _rc, cold = self.ws.coqc(self.NAME)
        self.assertEqual(render_all(result, self.NAME, after), cold)

    def test_locations_survive_a_warm_replay(self):
        """The anchor for the first sentence of a replayed chunk is where the
        chunk begins, not where its line begins; getting that wrong shifts every
        location in the replayed tail."""
        good = PREAMBLE + b"\n\nDefinition l := plus a 2.\n"
        bad = PREAMBLE + b"\n\nDefinition l := plus a bogus.\n"
        path = self.ws.write(self.NAME, good)
        sess = session_mod.Session(path, self.ws.flags, cwd=self.ws.dir)
        sess.start()
        self.addCleanup(sess.stop)
        self.assertTrue(sess.check(good, timeout=300).ok)
        result = sess.check(bad, timeout=300)
        self.assertEqual(result.mode, "replay")
        self.ws.write(self.NAME, bad)
        _rc, cold = self.ws.coqc(self.NAME)
        self.assertEqual(render_all(result, self.NAME, bad), cold)


if __name__ == "__main__":
    unittest.main()
