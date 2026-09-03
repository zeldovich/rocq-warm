"""Stopping a feed early, and getting the session back to a clean state.

When a sentence fails there is no point feeding Rocq the rest of the file: it
will happily re-prove every lemma below the error.  Cutting the feed short is
what makes the *broken* half of the edit loop cheap -- but it leaves Rocq
waiting for the rest of whatever sentence was in flight, and a bare `.` does
not terminate a sentence from inside a comment or a string.
"""

import unittest

from rocq_warm_helpers import Workspace, requires_rocq
from rocqwarm import session as session_mod

Session = session_mod.Session


class LexStateTests(unittest.TestCase):
    """What has to be closed before a `.` will terminate the sentence."""

    def test_plain_code(self):
        self.assertEqual(Session.lex_state(b"Definition x := 1"), (0, False))

    def test_open_comment(self):
        self.assertEqual(Session.lex_state(b"Definition x := (* hm"), (1, False))

    def test_nested_open_comments(self):
        self.assertEqual(Session.lex_state(b"(* a (* b (* c *)"), (2, False))

    def test_closed_comments_leave_nothing_open(self):
        self.assertEqual(Session.lex_state(b"(* a (* b *) c *) rest"), (0, False))

    def test_open_string(self):
        self.assertEqual(Session.lex_state(b'idtac "hello'), (0, True))

    def test_doubled_quote_is_an_escape(self):
        self.assertEqual(Session.lex_state(b'idtac "a""b'), (0, True))
        self.assertEqual(Session.lex_state(b'idtac "a""b"'), (0, False))

    def test_string_inside_a_comment(self):
        # The documented trap in a large development: a quote inside a comment swallows
        # everything, including a `*)`.
        self.assertEqual(Session.lex_state(b'(* he said "*)'), (1, True))
        self.assertEqual(Session.lex_state(b'(* he said "*)" '), (1, False))


@requires_rocq
class EarlyStopTests(unittest.TestCase):
    NAME = "R.v"

    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.cleanup)

    def session(self, text):
        path = self.ws.write(self.NAME, text)
        s = Session(path, self.ws.flags, cwd=self.ws.dir)
        s.start()
        self.addCleanup(s.stop)
        return s

    @staticmethod
    def filler(n):
        """`n` independent lemmas, so 'did it keep going?' is countable."""
        return b"".join(b"Lemma fill%d : True. Proof. exact I. Qed.\n" % i
                        for i in range(n))

    def test_an_early_error_does_not_execute_the_rest_of_the_file(self):
        # Bigger than the look-ahead window, or there is nothing to cut short.
        good = b"Definition a := 1.\n" + self.filler(4000)
        bad = b"Definition a := bogus.\n" + self.filler(4000)
        s = self.session(good)
        full = s.check(good, timeout=300)
        self.assertTrue(full.ok)
        self.ws.write(self.NAME, bad)
        broken = s.check(bad, timeout=300)
        self.assertFalse(broken.ok)
        self.assertLess(broken.replayed, full.replayed // 4,
                        "executed %d of %d sentences past an error at the top"
                        % (broken.replayed, full.replayed))

    def test_a_long_running_tactic_behind_an_error_is_interrupted(self):
        """Stopping the feed is not enough on its own: Rocq still executes the
        input already in flight, against a goal the error left in the wrong
        shape.  On a large development that is how a `vm_compute` ends up on a free
        variable and reaches tens of GB, so a command that runs long behind an
        error is interrupted rather than waited out."""
        tail = (b"Lemma slow : True.\nProof. do 400000000 idtac. exact I. Qed.\n"
                + self.filler(400))
        good = b"Definition a := 1.\n" + tail
        bad = b"Definition a := bogus.\n" + tail
        cheap = good.replace(b"do 400000000 idtac. ", b"")
        s = self.session(cheap)
        self.assertTrue(s.check(cheap, timeout=300).ok)
        self.ws.write(self.NAME, bad)
        broken = s.check(bad, timeout=300)
        self.assertFalse(broken.ok)
        self.assertLess(broken.seconds, 60,
                        "took %.1fs: the tactic behind the error ran to "
                        "completion instead of being interrupted"
                        % broken.seconds)

    def test_a_long_tactic_behind_an_error_already_in_the_buffer(self):
        """The same, but with the whole tail written before the error is
        noticed -- which is the normal case for anything smaller than the
        look-ahead window, and where an early stop cannot help."""
        tail = b"Lemma slow : True.\nProof. do 400000000 idtac. exact I. Qed.\n"
        good = b"Definition a := 1.\n" + tail.replace(b"do 400000000 idtac. ", b"")
        bad = b"Definition a := bogus.\n" + tail
        s = self.session(good)
        self.assertTrue(s.check(good, timeout=300).ok)
        self.ws.write(self.NAME, bad)
        broken = s.check(bad, timeout=300)
        self.assertFalse(broken.ok)
        self.assertLess(broken.seconds, 60,
                        "took %.1fs: the buffered tactic ran to completion"
                        % broken.seconds)

    def test_the_look_ahead_window_does_not_ratchet_up(self):
        """Everything in the window executes when a sentence fails, so the
        window must shrink back once a file's one huge sentence is behind us."""
        args = b" ".join(b"(x%d : nat)" % i for i in range(3000))
        wide = b"Definition wide " + args + b" := 0.\n"
        s = self.session(wide)
        self.assertTrue(s.check(wide, timeout=600).ok)
        grown = s.write_ahead
        self.assertGreater(grown, session_mod.DEFAULT_WRITE_AHEAD)
        small = b"Definition a := 1.\n"
        self.ws.write(self.NAME, small)
        self.assertTrue(s.check(small, timeout=300).ok)
        self.assertLessEqual(s.write_ahead, grown)

    def test_the_session_still_works_after_an_early_stop(self):
        """The recovery text must leave Rocq parsing normally again."""
        good = b"Definition a := 1.\n" + self.filler(1000)
        bad = b"Definition a := bogus.\n" + self.filler(1000)
        s = self.session(good)
        self.assertTrue(s.check(good, timeout=300).ok)
        self.assertFalse(s.check(bad, timeout=300).ok)
        self.ws.write(self.NAME, good)
        again = s.check(good, timeout=300)
        self.assertTrue(again.ok, [d.render(self.NAME, good) for d in again.diags])
        _rc, cold = self.ws.coqc(self.NAME)
        self.assertEqual(cold, "")

    def test_recovers_when_the_cut_lands_inside_a_comment(self):
        """A huge comment right after the error means the in-flight window ends
        inside it -- where `.` is just a full stop."""
        comment = b"(* " + b"padding padding padding\n" * 400 + b" *)\n"
        good = b"Definition a := 1.\n" + comment + self.filler(50)
        bad = b"Definition a := bogus.\n" + comment + self.filler(50)
        s = self.session(good)
        self.assertTrue(s.check(good, timeout=300).ok)
        self.assertFalse(s.check(bad, timeout=300).ok)
        self.ws.write(self.NAME, good)
        self.assertTrue(s.check(good, timeout=300).ok)

    def test_recovers_when_the_cut_lands_inside_a_string(self):
        big = b'Ltac noisy := idtac "' + b"x" * 60000 + b'".\n'
        good = b"Definition a := 1.\n" + big + self.filler(50)
        bad = b"Definition a := bogus.\n" + big + self.filler(50)
        s = self.session(good)
        self.assertTrue(s.check(good, timeout=300).ok)
        self.assertFalse(s.check(bad, timeout=300).ok)
        self.ws.write(self.NAME, good)
        self.assertTrue(s.check(good, timeout=300).ok)

    def test_a_file_that_ends_inside_a_comment_leaves_a_usable_session(self):
        """The sentinel goes in after the text, so a trailing open comment
        swallows it and Rocq sits waiting for more.  Reporting that is easy;
        the trap is what the NEXT write lands in.  Before the recovery here,
        the `BackTo` of the following check went into the comment too, and
        the session was lost for a file that was never going to pass."""
        good = b"Definition a := 1.\nLemma l : True.\nProof. exact I. Qed.\n"
        bad = good + b"Lemma m : True.\nProof.\n(* not finished"
        s = self.session(good)
        self.assertTrue(s.check(good, timeout=300).ok)
        broken = s.check(bad, timeout=300)
        self.assertFalse(broken.ok)
        self.assertIn(b"unterminated", broken.diags[-1].message())
        # Still answering questions from where it is parked ...
        libs = s.loaded_libraries()
        self.assertTrue(libs, "no libraries reported after an unterminated feed")
        # ... and still checking.
        fixed = bad + b" *)\nexact I. Qed.\n"
        self.ws.write(self.NAME, fixed)
        again = s.check(fixed, timeout=300)
        self.assertTrue(again.ok, [d.render(self.NAME, fixed) for d in again.diags])
        self.assertEqual(again.total, 9)

    def test_a_sentence_larger_than_the_initial_window(self):
        """The look-ahead window starts smaller than some real sentences (this
        tree has one of 13575 bytes); it has to widen itself rather than
        deadlock."""
        args = b" ".join(b"(x%d : nat)" % i for i in range(3000))
        text = b"Definition wide " + args + b" := 0.\n"
        s = self.session(text)
        result = s.check(text, timeout=600)
        self.assertTrue(result.ok, [d.render(self.NAME, text) for d in result.diags])
        self.assertGreater(s.write_ahead, session_mod.DEFAULT_WRITE_AHEAD)


if __name__ == "__main__":
    unittest.main()
