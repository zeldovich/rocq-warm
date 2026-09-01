"""Failure modes that must never turn into a wrong "OK".

Large Rocq developments have proofs that do not merely fail but
consume the machine -- a `vm_compute` on a goal with a free variable took a
worker to 31 GB in six minutes, and a unary `nat` fuel constant makes `Qed`
diverge.  A daemon that keeps such a session resident is worse than no daemon,
so every one of these must end in a killed child and an honest error.
"""

import os
import signal
import time
import unittest

from rocq_warm_helpers import Workspace, requires_rocq
from rocqwarm import session as session_mod


@requires_rocq
class RobustnessTests(unittest.TestCase):
    NAME = "Rb.v"

    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.cleanup)

    def session(self, text, **kw):
        path = self.ws.write(self.NAME, text)
        s = session_mod.Session(path, self.ws.flags, cwd=self.ws.dir, **kw)
        s.start()
        self.addCleanup(s.stop)
        return s

    # A tactic that runs essentially forever without recursing in Ltac (a
    # self-recursive `Ltac` just stack-overflows, which is not a hang).
    SPIN = b"Lemma stuck : True.\nProof. do 100000000 idtac. exact I. Qed.\n"

    def test_a_nonterminating_tactic_hits_the_timeout(self):
        text = self.SPIN
        s = self.session(text)
        t0 = time.time()
        with self.assertRaises(session_mod.FeedTimeout):
            s.check(text, timeout=5)
        self.assertLess(time.time() - t0, 60)

    def test_the_child_is_killed_not_orphaned(self):
        text = self.SPIN
        s = self.session(text)
        pid = s.proc.pid
        try:
            s.check(text, timeout=3)
        except session_mod.FeedTimeout:
            pass
        s.stop()
        time.sleep(0.5)
        with self.assertRaises(OSError):
            os.kill(pid, 0)

    def test_a_killed_child_is_reported_not_hung(self):
        text = b"Definition a := 1.\n"
        s = self.session(text)
        s.check(text, timeout=60)
        os.kill(s.proc.pid, signal.SIGKILL)
        time.sleep(0.3)
        self.assertFalse(s.alive)
        # The next check must cold-start rather than wait on a dead process.
        result = s.check(text, timeout=60)
        self.assertTrue(result.ok)
        self.assertEqual(result.mode, "cold")

    def test_a_session_over_its_rss_ceiling_is_killed(self):
        """The ceiling exists because a large Iris development has had a single tactic reach
        31 GB.  Testing it with a real 31 GB proof is not an option, so the
        ceiling is set below what any session costs."""
        text = b"From Stdlib Require Import List ZArith.\nDefinition a := 1.\n"
        s = self.session(text)
        s.rss_limit = s.rss_bytes() // 2      # already over it
        with self.assertRaises(session_mod.MemoryLimit):
            s.check(text, timeout=120)

    def test_no_ceiling_means_no_ceiling(self):
        text = b"Definition a := 1.\n"
        s = self.session(text)
        self.assertIsNone(s.rss_limit)
        self.assertTrue(s.check(text, timeout=60).ok)

    def test_an_empty_file(self):
        s = self.session(b"")
        self.assertTrue(s.check(b"", timeout=60).ok)

    def test_a_file_that_is_only_a_comment(self):
        text = b"(* nothing to see *)\n"
        s = self.session(text)
        self.assertTrue(s.check(text, timeout=60).ok)

    def test_a_file_with_no_trailing_newline(self):
        text = b"Definition a := 1."
        s = self.session(text)
        result = s.check(text, timeout=60)
        self.assertTrue(result.ok, [d.render(self.NAME, text) for d in result.diags])
        rc, _ = self.ws.coqc(self.NAME)
        self.assertEqual(rc, 0)

    def test_an_unterminated_sentence_at_end_of_file(self):
        """Rocq simply blocks waiting for the rest of the sentence.  That looks
        exactly like a slow tactic from the outside -- the difference is that it
        burns no CPU, which is how the session tells them apart."""
        text = b"Definition a := 1.\nDefinition dangling :=\n"
        s = self.session(text)
        t0 = time.time()
        result = s.check(text, timeout=600)
        self.assertFalse(result.ok)
        self.assertLess(time.time() - t0, 120, "should not have waited out the "
                                               "whole 600s timeout")
        rc, _ = self.ws.coqc(self.NAME)
        self.assertNotEqual(rc, 0)

    def test_an_unterminated_comment_at_end_of_file(self):
        text = b"Definition a := 1.\n(* never closed\n"
        s = self.session(text)
        result = s.check(text, timeout=600)
        self.assertFalse(result.ok)

    def test_the_session_recovers_after_a_timeout(self):
        s = self.session(self.SPIN)
        try:
            s.check(self.SPIN, timeout=3)
        except session_mod.FeedTimeout:
            pass
        s.stop()
        s.start()
        good = b"Lemma fine : True.\nProof. exact I. Qed.\n"
        self.ws.write(self.NAME, good)
        self.assertTrue(s.check(good, timeout=60).ok)


if __name__ == "__main__":
    unittest.main()
