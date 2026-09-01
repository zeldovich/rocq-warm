"""The stream parser, and the three facts about Rocq's output it rests on.

These are checked against a live Rocq rather than a recorded transcript: the
value of the whole tool is that its reading of the stream is Rocq's.
"""

import unittest

from rocq_warm_helpers import Workspace, requires_rocq
from rocqwarm import protocol, session as session_mod


class ChunkParsingTests(unittest.TestCase):
    """Pure parsing, on the shapes Rocq actually emits."""

    def test_chars_line_may_contain_brackets(self):
        # Iris tactics are full of `]` -- `iDestruct ... as "[H1 H2]"`.  A
        # non-greedy match on the closing bracket drops these lines entirely,
        # which silently stalls the feed instead of failing loudly.
        line = (b'Chars 10 - 40 [(iDestruct~"H"~as~"[H1~H2]")] '
                b'0.5 secs (0.4u,0.1s)')
        m = protocol.CHARS_RE.search(line)
        self.assertIsNotNone(m)
        self.assertEqual((int(m.group(1)), int(m.group(2))), (10, 40))
        self.assertEqual(m.group(3), b'(iDestruct~"H"~as~"[H1~H2]")')

    def test_progress_regex_is_not_line_anchored(self):
        # Rocq writes the Chars line immediately after `</prompt>`, on the same
        # line, so progress tracking on the raw stream must not anchor to ^.
        raw = b'<prompt>Rocq < 3 || 0 < </prompt>Chars 100 - 140 [Foo.] 0. secs (0.u,0.s)\n'
        self.assertIsNone(protocol.CHARS_RE.search(raw, 0)
                          if raw.startswith(b'Chars') else None)
        m = list(protocol.PROGRESS_RE.finditer(raw))
        self.assertEqual([(int(x.group(1)), int(x.group(2))) for x in m], [(100, 140)])

    def test_segments_pair_sentences_with_the_state_after_them(self):
        raw = (b'<prompt>Rocq < 1 || 0 < </prompt>Chars 0 - 5 [A.] 0. secs (0.u,0.s)\n'
               b'<prompt>Rocq < 2 || 0 < </prompt>Chars 6 - 9 [B.] 0. secs (0.u,0.s)\n'
               b'Error: nope\n'
               b'<prompt>Rocq < 2 || 0 < </prompt>')
        segments, _ = protocol.split_prompts(raw)
        items = protocol.parse_segments(segments)
        self.assertEqual(len(items), 2)
        self.assertFalse(items[0].failed)
        self.assertTrue(items[1].failed)          # state did not advance

    def test_a_segment_without_a_chars_line_is_a_parse_failure(self):
        raw = (b'<prompt>Rocq < 1 || 0 < </prompt>Chars 0 - 5 [A.] 0. secs (0.u,0.s)\n'
               b'<prompt>Rocq < 2 || 0 < </prompt>Toplevel input, characters 1-2:\n'
               b'Error: Syntax error\n'
               b'<prompt>Rocq < 2 || 0 < </prompt>')
        items = protocol.parse_segments(protocol.split_prompts(raw)[0])
        self.assertIsInstance(items[1], protocol.ParseFailure)
        self.assertTrue(items[1].failed)


class BlankOrCommentTests(unittest.TestCase):
    """The scanner that decides an edit cannot have changed sentence structure."""

    def check(self, text, expected):
        self.assertEqual(session_mod.Session.blank_or_comment(text), expected, text)

    def test_whitespace_and_plain_comments(self):
        self.check(b"   \n\t ", True)
        self.check(b"(* hello *)", True)
        self.check(b"  (* a *)  (* b *) ", True)

    def test_nested_comments(self):
        self.check(b"(* a (* b *) c *)", True)
        self.check(b"(* a (* b *) ", False)

    def test_a_string_inside_a_comment_hides_its_terminator(self):
        # This is a documented trap in a large development: a `\"` inside a comment makes
        # everything up to the next quote part of a string.
        self.check(b'(* he said "*)" and left *)', True)
        self.check(b'(* he said "*)" *) Definition x := 1.', False)

    def test_real_code_is_not_blank(self):
        self.check(b"Definition x := 1.", False)
        self.check(b"(* c *) Definition x := 1.", False)


@requires_rocq
class LiveStreamTests(unittest.TestCase):
    """The invariants the design depends on, re-derived from a running Rocq."""

    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.cleanup)

    def session(self, text, name="S.v"):
        path = self.ws.write(name, text)
        s = session_mod.Session(path, self.ws.flags, cwd=self.ws.dir)
        s.start()
        self.addCleanup(s.stop)
        return s, path

    def test_sentence_map_equals_coqc(self):
        """Our byte ranges must be Rocq's, on the constructs that break naive
        splitters: nested comments, `*)` inside a string, bullets, braces,
        recursive-notation `..`, and UTF-8."""
        text = (
            "(* nested (* comment *) with a \"*)\" string inside *)\n"
            "From Stdlib Require Import List.\n"
            "Import ListNotations.\n"
            "Notation \"[[ x ; .. ; y ]]\" := (cons x .. (cons y nil) ..).\n"
            "Definition l := [[ 1 ; 2 ; 3 ]].\n"
            "Notation \"x ⊎ y\" := (plus x y) (at level 50).\n"
            "Definition été := 1 ⊎ 2.\n"
            "Lemma bullets : True /\\ True /\\ True.\n"
            "Proof.\n"
            "  split. { exact I. }\n"
            "  split.\n"
            "  - exact I.\n"
            "  - exact I.\n"
            "Qed.\n"
        ).encode()
        s, path = self.session(text)
        result = s.check(text, timeout=300)
        self.assertTrue(result.ok, [d.render("S.v", text) for d in result.diags])
        self.assertEqual([(x.start, x.end) for x in s.sentences],
                         self.ws.coqc_sentences("S.v"))

    def test_every_successful_sentence_advances_the_state(self):
        """The error signal is 'the state id did not move'.  That is only sound
        if success always moves it -- for queries and structure too."""
        text = (b"Check 1.\nPrint nat.\nLocate nat.\nSet Printing All.\n"
                b"Unset Printing All.\nSection S.\nVariable n : nat.\n"
                b"Definition d := n.\nEnd S.\nModule M.\nDefinition e := 1.\n"
                b"End M.\nGoal True.\n- exact I.\nQed.\nFail Check nope.\n"
                b"Time Check 1.\n")
        s, _ = self.session(text)
        result = s.check(text, timeout=300)
        self.assertTrue(result.ok, [d.render("S.v", text) for d in result.diags])
        for sent in s.sentences:
            self.assertNotEqual(sent.state_before, sent.state_after, sent)


if __name__ == "__main__":
    unittest.main()
