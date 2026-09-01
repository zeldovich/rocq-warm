"""The property the whole tool stands or falls on.

    For every edit in a sequence, a warm session's verdict and diagnostics are
    the ones a cold `coqc` would have given for that version of the file.

Each case edits the file, asks the warm session, then runs a real `coqc` on the
same bytes and compares.  The edits are chosen to attack the reuse: they change
notations (which changes the *parser* for everything after), Ltac definitions,
`Set` options, section and module structure, and the `Require` header -- the
places where reusing a prefix could plausibly be unsound.
"""

import unittest

from rocq_warm_helpers import Workspace, render_all, requires_rocq
from rocqwarm import session as session_mod


BASE = b"""From Stdlib Require Import List.
Import ListNotations.

Notation "x <+> y" := (plus x y) (at level 50).

Ltac crush := repeat (split; try exact I).

Set Printing Depth 42.

Section Sec.
  Variable k : nat.
  Definition shifted := k <+> 1.
End Sec.

Module Mod.
  Definition inner := 7.
End Mod.

Definition early := 1 <+> 2.

Lemma first : True /\\ True.
Proof.
  crush.
Qed.

(* a gap comment, between two lemmas *)

Lemma second : forall n : nat, n = n.
Proof.
  intros n.
  reflexivity.
Qed.

Lemma third : Mod.inner = 7.
Proof.
  reflexivity.
Qed.

Definition late := shifted 3 <+> early.
"""


@requires_rocq
class IncrementalEquivalenceTests(unittest.TestCase):
    """One session, a sequence of edits, `coqc` consulted after every one."""

    NAME = "Eq.v"

    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.cleanup)
        self.path = self.ws.write(self.NAME, BASE)
        self.sess = session_mod.Session(self.path, self.ws.flags, cwd=self.ws.dir)
        self.sess.start()
        self.addCleanup(self.sess.stop)

    def assertMatchesCoqc(self, text, label, expect_mode=None):
        self.ws.write(self.NAME, text)
        result = self.sess.check(text, timeout=300)
        rc, cold = self.ws.coqc(self.NAME)
        warm = render_all(result, self.NAME, text)
        self.assertEqual(
            result.ok, rc == 0,
            "%s: rocq-warm says %s, coqc says %s\nrocq-warm:\n%s\ncoqc:\n%s"
            % (label, "OK" if result.ok else "FAILED",
               "OK" if rc == 0 else "FAILED", warm, cold))
        self.assertEqual(warm, cold, "%s: diagnostics differ" % label)
        if expect_mode is not None:
            self.assertEqual(result.mode, expect_mode, label)
        return result

    def test_edit_sequence_matches_cold_compilation_at_every_step(self):
        steps = [
            ("original", BASE, "cold"),

            # A broken tactic late in the file, then its fix: the loop the tool
            # exists for.  The fix must be a replay, not a cold start.
            ("break a late tactic",
             BASE.replace(b"  reflexivity.\nQed.\n\nLemma third",
                          b"  exact I.\nQed.\n\nLemma third"), "replay"),
            ("fix it again", BASE, "replay"),

            # Comments and whitespace in a gap change no sentence at all.
            ("comment inserted in a gap",
             BASE.replace(b"(* a gap comment, between two lemmas *)",
                          b"(* a gap comment, rewritten entirely *)"), "shift"),
            ("blank line added in a gap",
             BASE.replace(b"(* a gap comment, between two lemmas *)\n",
                          b"(* a gap comment, between two lemmas *)\n\n"), "shift"),

            # Renaming a bound name inside one proof.
            ("rename a hypothesis",
             BASE.replace(b"intros n.\n  reflexivity.",
                          b"intros m.\n  reflexivity."), "replay"),

            # Structure edits.
            ("delete a whole lemma",
             BASE.replace(b"Lemma third : Mod.inner = 7.\nProof.\n"
                          b"  reflexivity.\nQed.\n\n", b""), "replay"),
            ("insert a new lemma mid-file",
             BASE.replace(b"Lemma third :",
                          b"Lemma inserted : 1 = 1.\nProof. reflexivity. Qed.\n\n"
                          b"Lemma third :"), "replay"),

            # A notation change alters the PARSER for every later sentence; if
            # BackTo did not roll the parser back this would pass when it must
            # fail, or vice versa.
            # Replaying from a notation is sound only because `BackTo` rolls
            # the parser back too; if it did not, the later uses of `<+>` would
            # still parse and this step would wrongly pass.
            ("change the notation's spelling",
             BASE.replace(b'"x <+> y"', b'"x <-> y"'), "replay"),
            ("break a use of the notation",
             BASE.replace(b"Definition late := shifted 3 <+> early.",
                          b"Definition late := shifted 3 <?> early."), "replay"),

            # Ltac and options are session state too.
            ("redefine the Ltac to something wrong",
             BASE.replace(b"Ltac crush := repeat (split; try exact I).",
                          b"Ltac crush := exact 0."), "replay"),
            ("change a Set option",
             BASE.replace(b"Set Printing Depth 42.",
                          b"Set Printing Depth 17."), "replay"),

            # ADDING a Require below the existing ones only ever loads more, so
            # it can be replayed.  Removing one cannot -- see the dedicated
            # test below.
            ("add a Require",
             BASE.replace(b"Import ListNotations.",
                          b"From Stdlib Require Import Arith.\nImport ListNotations."),
             "replay"),

            # Errors of each kind.
            ("a syntax error",
             BASE.replace(b"Definition early := 1 <+> 2.",
                          b"Definition early := (1 <+>."), None),
            ("an unbound name",
             BASE.replace(b"Definition late := shifted 3 <+> early.",
                          b"Definition late := shifted 3 <+> nonexistent."), None),
            ("an incomplete proof",
             BASE.replace(b"  intros n.\n  reflexivity.\n", b"  intros n.\n"), None),

            ("back to the original", BASE, None),
        ]
        for label, text, mode in steps:
            with self.subTest(step=label):
                self.assertMatchesCoqc(text, label, expect_mode=mode)

    def test_undoing_a_require_forces_a_cold_start(self):
        """We never unload a library warm.  Any edit that would have to take a
        `Require` back out of the session throws the session away instead."""
        self.assertMatchesCoqc(BASE, "warm up")
        with_extra = BASE.replace(
            b"Import ListNotations.",
            b"From Stdlib Require Import Arith.\nImport ListNotations.")
        self.assertMatchesCoqc(with_extra, "add a Require", expect_mode="replay")
        # Now take it away again: the session is holding that library.
        self.assertMatchesCoqc(BASE, "remove the Require again", expect_mode="cold")

    def test_editing_the_first_require_is_a_cold_start(self):
        self.assertMatchesCoqc(BASE, "warm up")
        moved = BASE.replace(b"From Stdlib Require Import List.",
                             b"From Stdlib Require Import List Arith.")
        self.assertMatchesCoqc(moved, "edit the first Require", expect_mode="cold")

    def test_file_ending_inside_a_sentence(self):
        """A truncated file must fail, not hang: Rocq simply blocks waiting for
        the rest of the sentence, which looks exactly like a slow tactic until
        you notice it is burning no CPU."""
        text = BASE + b"\nDefinition dangling :=\n"
        self.ws.write(self.NAME, text)
        result = self.sess.check(text, timeout=300)
        rc, _cold = self.ws.coqc(self.NAME)
        self.assertNotEqual(rc, 0)
        self.assertFalse(result.ok)

    def test_a_warm_replay_touches_only_the_tail(self):
        """The point of the exercise: fixing a late proof must not re-execute
        the early ones."""
        self.assertMatchesCoqc(BASE, "warm up")
        total = len(self.sess.sentences)
        broken = BASE.replace(b"Lemma third : Mod.inner = 7.\nProof.\n  reflexivity.",
                              b"Lemma third : Mod.inner = 7.\nProof.\n  exact I.")
        self.assertMatchesCoqc(broken, "break the last lemma")
        result = self.assertMatchesCoqc(BASE, "fix it", expect_mode="replay")
        self.assertLess(result.replayed, total // 2,
                        "replayed %d of %d sentences" % (result.replayed, total))


if __name__ == "__main__":
    unittest.main()
