"""A warm session must never answer for a library that no longer exists.

Two ways that happens, and both were seen in the field before this file was
written.  A `.vo` on disk is REBUILT under a session that loaded the old one
-- the classic cache-invalidation case, caught by fingerprinting what Rocq
says it loaded.  Or the `.vo` on disk is itself STALE: its `.v` was edited
and never recompiled, so a cold `coqc` would load the same old library and
say the same wrong thing, and the confusion is that the tool that just said
"OK" about the edited file did not make the edit visible to anything else.
The first failure mode is caught by never checking against a `.vo` that
`make` would rebuild; the second by compiling the `.vo` a green check earns.

Every test here drives the real CLI against a real daemon, because the point
of every one of them is what a SECOND process sees.
"""

import os
import re
import subprocess
import time
import unittest

from rocq_warm_helpers import TOOLS, Workspace, requires_rocq, wait_for
from rocqwarm import session as session_mod

CLI = os.path.join(TOOLS, "rocq-warm")

DEP = b"Definition dep := 1.\n"
USER = b"Require Import T.Dep.\nDefinition u := dep.\n"


def mtime(path):
    return os.stat(path).st_mtime_ns


@requires_rocq
class StalenessCase(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.cleanup)
        self.addCleanup(self.stop_daemon)

    def stop_daemon(self):
        subprocess.run([CLI, "stop", "--root", self.ws.dir],
                       capture_output=True, timeout=60)

    def cli(self, *args, **kw):
        env = kw.get("env")
        return subprocess.run([CLI] + list(args), capture_output=True,
                              cwd=self.ws.dir, timeout=kw.get("timeout", 300),
                              env=env)

    def check(self, name, *flags, **kw):
        return self.cli("check", name, *flags, **kw)

    def project(self, *names, **kw):
        lines = ["-R . T"] + list(kw.get("extra", [])) + list(names)
        with open(self.ws.path("_CoqProject"), "w") as f:
            f.write("\n".join(lines) + "\n")

    def status(self):
        return self.cli("status", "--root", self.ws.dir).stdout.decode()

    def daemon_log(self):
        try:
            with open(os.path.join(self.ws.dir, ".rocq-warm", "log"), "rb") as f:
                return f.read()
        except OSError:
            return b""

    def assertRefused(self, proc, *fragments):
        err = proc.stderr.decode()
        self.assertEqual(proc.returncode, 2,
                         "expected a refusal (exit 2), got %d:\n%s%s"
                         % (proc.returncode, proc.stdout.decode(), err))
        self.assertIn("NOT CHECKED", err)
        for frag in fragments:
            self.assertIn(frag, err)

    def assertChecked(self, proc, passed=True):
        self.assertEqual(proc.returncode, 0 if passed else 1,
                         "%s%s" % (proc.stdout.decode(), proc.stderr.decode()))
        self.assertNotIn("NOT CHECKED", proc.stderr.decode())


class RefusalTests(StalenessCase):
    """A dependency `make` would rebuild is not one to check against."""

    def setUp(self):
        super().setUp()
        self.ws.write("Dep.v", DEP)
        self.ws.write("User.v", USER)
        self.project("Dep.v", "User.v")

    def test_an_unbuilt_dependency_is_refused_not_failed(self):
        """Without the .vo, Rocq's own error ("Cannot find a physical path")
        is exit 1 -- the same code as a broken proof, which is how a missing
        build step gets read as a proof error."""
        proc = self.check("User.v")
        self.assertRefused(proc, "Dep.vo has not been built")
        self.assertNotIn("User.v", self.status(),
                         "a refused check must not leave a session behind")

    def test_a_source_newer_than_its_vo_is_refused(self):
        self.ws.build("Dep.v")
        self.assertChecked(self.check("User.v"))
        self.ws.touch("Dep.v", b"Definition dep := 2.\n")
        self.assertRefused(self.check("User.v"), "Dep.v is newer than Dep.vo")

    def test_allow_stale_checks_anyway_and_says_so(self):
        self.ws.build("Dep.v")
        self.ws.touch("Dep.v", b"Definition other := 2.\n")
        proc = self.check("User.v", "--allow-stale")
        # The OLD library still defines `dep`, so against it the file passes:
        # that is exactly the false pass the default refuses to produce.
        self.assertChecked(proc, passed=True)
        self.assertIn("warning: checking against a stale dependency",
                      proc.stderr.decode())

    def test_equal_mtimes_are_up_to_date(self):
        """`make` treats an output as current when it is not OLDER than its
        input; a build that stamps the .vo with the source's mtime is fine."""
        self.ws.build("Dep.v")
        t = mtime(self.ws.path("Dep.v"))
        os.utime(self.ws.path("Dep.vo"), ns=(t, t))
        self.assertChecked(self.check("User.v"))

    def test_a_stale_transitive_dependency_is_refused(self):
        """User requires Mid, Mid requires Base: an edit to Base is just as
        fatal, and the message names Base, not the file that was asked for."""
        self.ws.write("Base.v", b"Definition base := 1.\n")
        self.ws.write("Mid.v", b"Require Import T.Base.\nDefinition mid := base.\n")
        self.ws.write("User.v", b"Require Import T.Mid.\nDefinition u := mid.\n")
        self.project("Base.v", "Mid.v", "User.v")
        self.ws.build("Base.v", "Mid.v")
        self.assertChecked(self.check("User.v"))
        self.ws.touch("Base.v", b"Definition base := 2.\n")
        self.assertRefused(self.check("User.v"), "Base.v is newer than Base.vo")

    def test_a_vo_older_than_a_vo_it_requires_is_refused(self):
        """Half a rebuild: Base.vo is fresh, Mid.vo was built against the old
        Base.  Rocq would say "inconsistent assumptions" and exit 1; make
        would rebuild Mid; so do we -- by refusing until somebody does."""
        self.ws.write("Base.v", b"Definition base := 1.\n")
        self.ws.write("Mid.v", b"Require Import T.Base.\nDefinition mid := base.\n")
        self.ws.write("User.v", b"Require Import T.Mid.\nDefinition u := mid.\n")
        self.project("Base.v", "Mid.v", "User.v")
        self.ws.build("Base.v", "Mid.v")
        self.assertChecked(self.check("User.v"))
        self.ws.touch("Base.v", b"Definition base := 2.\n")
        self.ws.build("Base.v")
        self.assertRefused(self.check("User.v"),
                           "Mid.vo is older than Base.vo, which it requires")

    def test_a_dependency_under_another_root_is_watched(self):
        """`-R ../lib L` makes a whole other tree loadable.  Its files are not
        in this _CoqProject, so `rocq dep` over the listed sources never sees
        THEIR dependencies -- and an edit two levels down in that tree was
        invisible.  (Found on a real tree: 7 of 232 closure members had no
        graph entry.)"""
        lib = os.path.join(self.ws.dir, "lib")
        os.makedirs(lib)
        self.ws.write("lib/Base.v", b"Definition base := 1.\n")
        self.ws.write("lib/Mid.v", b"Require Import L.Base.\nDefinition mid := base.\n")
        self.ws.write("User.v", b"Require Import L.Mid.\nDefinition u := mid.\n")
        self.project("User.v", extra=["-R lib L"])
        for name in ("Base.v", "Mid.v"):
            subprocess.run(["coqc", "-q", "-R", ".", "L", name], cwd=lib,
                           check=True, capture_output=True)
        self.assertChecked(self.check("User.v"))
        self.ws.touch("lib/Base.v", b"Definition base := 2.\n")
        self.assertRefused(self.check("User.v"), "Base.v is newer than")

    def test_a_listed_source_that_does_not_exist_does_not_blind_the_graph(self):
        """`rocq dep` prints NOTHING when any file it is given is missing, and
        _CoqProject files routinely list generated sources that are not there
        yet.  An empty graph meant an empty closure meant nothing watched."""
        self.ws.write("Base.v", b"Definition base := 1.\n")
        self.ws.write("Mid.v", b"Require Import T.Base.\nDefinition mid := base.\n")
        self.ws.write("User.v", b"Require Import T.Mid.\nDefinition u := mid.\n")
        self.project("Generated.v", "Base.v", "Mid.v", "User.v")
        self.ws.build("Base.v", "Mid.v")
        self.assertChecked(self.check("User.v"))
        self.ws.touch("Base.v", b"Definition base := 2.\n")
        self.assertRefused(self.check("User.v"), "Base.v is newer than Base.vo")

    def test_a_comment_only_edit_after_a_rebuild_goes_cold(self):
        """The cheapest replay -- nothing to execute -- must still not be
        allowed to answer from a session holding a replaced library."""
        self.ws.build("Dep.v")
        self.assertChecked(self.check("User.v"))
        self.ws.touch("Dep.v", b"Definition other := 1.\n")
        self.ws.build("Dep.v")
        self.ws.write("User.v", USER + b"(* a comment *)\n")
        proc = self.check("User.v")
        self.assertChecked(proc, passed=False)
        self.assertIn(b"cold", proc.stderr)


class WatchedSetTests(StalenessCase):
    """What is watched is what Rocq loaded, not what `rocq dep` guessed."""

    def setUp(self):
        super().setUp()
        self.ws.write("Dep.v", DEP)
        self.project("Dep.v", "User.v")
        self.ws.build("Dep.v")

    def test_a_require_added_mid_session_is_watched(self):
        """The closure was computed when the session started.  A `Require`
        added by a later edit replays fine -- and the library it loads was
        then a dependency nobody had written down."""
        first = b"Definition z := 0.\nDefinition u := 1.\n"
        second = b"Definition z := 0.\nRequire Import T.Dep.\nDefinition u := dep.\n"
        self.ws.write("User.v", first)
        self.assertChecked(self.check("User.v"))
        self.ws.write("User.v", second)
        proc = self.check("User.v")
        self.assertChecked(proc)
        self.assertIn(b"replay", proc.stderr, "the edit is below sentence 1")
        # Now Dep loses `dep`.  A session still holding the old Dep.vo would
        # say OK from its parked state without executing anything.
        self.ws.touch("Dep.v", b"Definition other := 1.\n")
        self.ws.build("Dep.v")
        proc = self.check("User.v")
        self.assertChecked(proc, passed=False)
        self.assertIn(b"cold", proc.stderr)
        self.assertIn(b"dep was not found", proc.stdout)

    def test_the_prelude_is_watched_too(self):
        """`rocq dep` never lists the standard library or anything installed
        in the switch, and an `opam upgrade` replaces those in place.  The
        session watches every file Rocq reports loading, which is dozens for
        the prelude alone."""
        self.ws.write("User.v", USER)
        self.assertChecked(self.check("User.v"))
        m = re.search(r'(\d+) \.vo watched', self.status())
        self.assertIsNotNone(m, self.status())
        self.assertGreater(int(m.group(1)), 10,
                           "only the rocq-dep closure is being watched")

    def test_a_dependency_rebuilt_during_a_check_discards_the_session(self):
        """Stat'ed after the check, a .vo that was rebuilt while Rocq was
        busy would be recorded with its NEW mtime -- as if that were what got
        loaded -- and the session kept."""
        slow = (b"Require Import T.Dep.\nLemma slow : True.\n"
                b"Proof. do 3000000 idtac. exact I. Qed.\nDefinition u := dep.\n")
        self.ws.write("User.v", slow)
        proc = subprocess.Popen([CLI, "check", "User.v"], cwd=self.ws.dir,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.6)             # the session is inside the slow proof
        self.ws.touch("Dep.v", b"Definition dep := 1. (* rebuilt *)\n")
        self.ws.build("Dep.v")
        out, err = proc.communicate(timeout=300)
        self.assertEqual(proc.returncode, 0, out + err)
        self.assertIn(b"changed during the check", err)
        self.assertNotIn("User.v", self.status(),
                         "the session must not survive a rebuild it may or "
                         "may not have loaded")

    def test_loaded_libraries_are_reported_with_their_files(self):
        self.ws.write("User.v", USER)
        s = session_mod.Session(self.ws.path("User.v"), self.ws.flags,
                                cwd=self.ws.dir)
        s.start()
        self.addCleanup(s.stop)
        self.assertTrue(s.check(USER, timeout=300).ok)
        libs = s.loaded_libraries()
        self.assertEqual(libs.get("T.Dep"), self.ws.path("Dep.vo"))
        prelude = [p for n, p in libs.items() if n.endswith(".Init.Prelude")]
        self.assertTrue(prelude, sorted(libs))
        self.assertTrue(os.path.isfile(prelude[0]), prelude[0])
        # The queries were undone: the session still replays from the edit.
        again = s.check(USER + b"Definition v := u.\n", timeout=300)
        self.assertTrue(again.ok)
        self.assertEqual(again.mode, "replay")
        self.assertEqual(again.replayed, 1)


class VoTests(StalenessCase):
    """A green check writes no .vo, and says so; `--compile` writes one."""

    # Slow enough that a second process can act while the compile runs even
    # on a loaded machine; the check and the compile each take a few seconds.
    SLOW_DEP = (b"Definition dep := 1.\nLemma slow : True.\n"
                b"Proof. do 8000000 idtac. exact I. Qed.\n")

    def setUp(self):
        super().setUp()
        self.ws.write("Dep.v", DEP)
        self.ws.write("User.v", USER)
        self.project("Dep.v", "User.v")

    def vo_fresh(self, name="Dep"):
        vo, v = self.ws.path(name + ".vo"), self.ws.path(name + ".v")
        return os.path.exists(vo) and mtime(vo) >= mtime(v)

    def test_a_green_check_warns_that_the_vo_was_not_regenerated(self):
        """A real compile would double the cost of every passing check.  What
        the check must do instead is make sure nobody mistakes it for one."""
        proc = self.check("Dep.v")
        self.assertChecked(proc)
        self.assertIn(b"Dep.vo was NOT regenerated", proc.stderr)
        self.assertIn(b"has not been built", proc.stderr)
        time.sleep(1.0)
        self.assertFalse(os.path.exists(self.ws.path("Dep.vo")))
        self.assertRefused(self.check("User.v"), "Dep.vo has not been built")

    def test_the_warning_names_the_edit_when_there_is_an_old_vo(self):
        self.ws.build("Dep.v")
        self.ws.touch("Dep.v", b"Definition dep := 2.\n")
        proc = self.check("Dep.v")
        self.assertChecked(proc)
        self.assertIn(b"Dep.vo was NOT regenerated (Dep.v is newer than Dep.vo)",
                      proc.stderr)

    def test_no_warning_when_the_vo_is_current(self):
        self.ws.build("Dep.v")
        proc = self.check("Dep.v")
        self.assertChecked(proc)
        self.assertNotIn(b"NOT regenerated", proc.stderr)

    def test_a_red_check_does_not_warn_about_the_vo(self):
        self.ws.write("Dep.v", b"Definition dep := bogus.\n")
        proc = self.check("Dep.v")
        self.assertChecked(proc, passed=False)
        self.assertNotIn(b"NOT regenerated", proc.stderr)

    def test_compile_writes_the_vo(self):
        proc = self.check("Dep.v", "--compile")
        self.assertChecked(proc)
        self.assertIn(b"wrote Dep.vo", proc.stderr)
        self.assertNotIn(b"NOT regenerated", proc.stderr)
        self.assertTrue(self.vo_fresh())
        for extra in (".glob", ".vok", ".vos"):
            self.assertTrue(os.path.exists(self.ws.path("Dep" + extra)),
                            "make would have produced Dep%s too" % extra)
        self.assertChecked(self.check("User.v"))

    def test_compile_on_a_red_check_writes_nothing(self):
        self.ws.write("Dep.v", b"Definition dep := bogus.\n")
        self.assertChecked(self.check("Dep.v", "--compile"), passed=False)
        self.assertFalse(os.path.exists(self.ws.path("Dep.vo")))

    def test_a_dependent_waits_for_a_compile_in_flight(self):
        """`--compile` on Dep in one terminal, a check of User in another:
        Dep.vo is not there yet, but it is on its way, and waiting beats
        refusing."""
        self.ws.write("Dep.v", self.SLOW_DEP)
        compiling = subprocess.Popen([CLI, "check", "Dep.v", "--compile"],
                                     cwd=self.ws.dir, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
        self.assertTrue(wait_for(lambda: "compile" in self.status(), timeout=60),
                        "the compile never showed up in status")
        self.assertFalse(self.vo_fresh(), "too slow a machine to test the race")
        proc = self.check("User.v")
        out, err = compiling.communicate(timeout=300)
        self.assertEqual(compiling.returncode, 0, out + err)
        self.assertChecked(proc)
        self.assertTrue(self.vo_fresh())

    def test_an_edit_while_a_compile_is_queued_never_writes_the_wrong_vo(self):
        """The one that flushed out a real bug.  `rocq compile` reads the .v
        from disk, so a compile queued for text A that starts after the file
        has become B would write a .vo of B -- unchecked -- under A's name, or
        of A that the edit meant to supersede.  Whoever loses the race, the
        durable outcome must be exactly one thing: a .vo of the text that was
        checked last, and never a .vo of the old text passing as current.

        Asserted on the outcome, not on who wins, because which of the two
        checks lands first is inherently timing-dependent -- but the outcome
        must not be.
        """
        self.ws.write("Dep.v", self.SLOW_DEP)       # A: slow, defines dep := 1
        first = subprocess.Popen([CLI, "check", "Dep.v", "--compile"],
                                 cwd=self.ws.dir, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
        self.assertTrue(wait_for(lambda: "compile" in self.status(), timeout=60),
                        self.daemon_log())
        self.ws.write("Dep.v", b"Definition dep := 2.\n")   # B, while A compiles
        proc = self.check("Dep.v", "--compile")
        out, err = first.communicate(timeout=300)
        self.assertIn(first.returncode, (0, 2),
                      b"first: %s%s\nlog: %s" % (out, err, self.daemon_log()))
        if first.returncode == 0:
            # first's compile of A won the race and finished before the edit.
            # Then B is newer than the .vo, and staleness must catch it, so
            # first must have warned rather than claimed a current .vo.
            self.assertIn(b"wrote Dep.vo", err)
        else:
            self.assertIn(b"not written", err)      # skipped or cancelled
        self.assertChecked(proc)                     # the check of B is green
        # The durable invariant: what is on disk as Dep.vo is B, or is stale
        # (older than Dep.v) so nothing trusts it -- never a current .vo of A.
        self.assertTrue(wait_for(
            lambda: "compile" not in self.status(), timeout=120), "compile stuck")
        self.ws.write("User.v", b"Require Import T.Dep.\n"
                                b"Lemma l : dep = 2. Proof. reflexivity. Qed.\n")
        # User wants dep = 2 (the B text).  It passes iff Dep.vo is B; if Dep.vo
        # is stale it is refused; it must never pass as dep = 1.
        u = self.check("User.v")
        self.assertNotEqual(u.returncode, 1,
                            "User checked green/refused, never against old dep=1"
                            "\n%s%s" % (u.stdout.decode(), u.stderr.decode()))

    def test_compile_disagreeing_with_the_verdict_is_exit_3(self):
        """Not reachable through Rocq -- that would be the bug the code
        exists to report -- so provoke it by making the compile step fail
        for a reason the session cannot see: the .v directory is read-only,
        so the .vo cannot be written."""
        if os.geteuid() == 0:
            self.skipTest("root can write anywhere")
        os.chmod(self.ws.dir, 0o555)
        self.addCleanup(os.chmod, self.ws.dir, 0o755)
        proc = self.check("Dep.v", "--compile")
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn(b"DISAGREED", proc.stderr)


class RebuildTests(StalenessCase):
    """`--rebuild` runs the build's own step for what is stale, in order."""

    def setUp(self):
        super().setUp()
        self.ws.write("Base.v", b"Definition base := 1.\n")
        self.ws.write("Mid.v", b"Require Import T.Base.\nDefinition mid := base.\n")
        self.ws.write("Top.v", b"Require Import T.Mid.\nDefinition top := mid.\n")
        self.project("Base.v", "Mid.v", "Top.v")

    def test_rebuild_compiles_what_is_stale_and_what_that_makes_stale(self):
        self.ws.build("Base.v", "Mid.v")
        self.assertChecked(self.check("Top.v"))
        old_mid = mtime(self.ws.path("Mid.vo"))
        # Base gains `base2`; Top wants it through Mid.
        self.ws.touch("Base.v", b"Definition base := 1.\nDefinition base2 := 2.\n")
        self.ws.write("Mid.v", b"Require Import T.Base.\nDefinition mid := base2.\n")
        self.ws.touch("Mid.v")
        self.ws.write("Top.v", b"Require Import T.Mid.\n"
                               b"Lemma l : mid = 2. Proof. reflexivity. Qed.\n")
        self.assertRefused(self.check("Top.v"), "Base.v is newer")
        proc = self.check("Top.v", "--rebuild")
        self.assertChecked(proc)
        self.assertIn(b"cold", proc.stderr)
        self.assertGreater(mtime(self.ws.path("Mid.vo")), old_mid)
        self.assertGreaterEqual(mtime(self.ws.path("Mid.vo")),
                                mtime(self.ws.path("Base.vo")))

    def test_rebuild_cascades_to_dependents_that_were_not_touched(self):
        """Only Base changed; Mid.vo is now older than Base.vo, so make would
        rebuild it too, and so must we -- or Top loads an inconsistent pair."""
        self.ws.build("Base.v", "Mid.v")
        old_mid = mtime(self.ws.path("Mid.vo"))
        self.ws.touch("Base.v", b"Definition base := 3.\n")
        proc = self.check("Top.v", "--rebuild")
        self.assertChecked(proc)
        self.assertGreater(mtime(self.ws.path("Mid.vo")), old_mid)

    def test_rebuild_reports_a_dependency_that_does_not_compile(self):
        self.ws.build("Base.v", "Mid.v")
        old_vo = mtime(self.ws.path("Base.vo"))
        self.ws.touch("Base.v", b"Definition base := bogus.\n")
        proc = self.check("Top.v", "--rebuild")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn(b"rebuilding Base.v FAILED", proc.stderr)
        self.assertIn(b"Error:", proc.stdout)
        self.assertIn(b"bogus", proc.stdout)
        # The old .vo is left exactly as make would leave it: still there,
        # still older than its source, still refused.
        self.assertEqual(mtime(self.ws.path("Base.vo")), old_vo)
        self.assertRefused(self.check("Top.v"), "Base.v is newer than Base.vo")

    def test_rebuild_builds_an_unbuilt_tree_from_scratch(self):
        proc = self.check("Top.v", "--rebuild")
        self.assertChecked(proc)
        for name in ("Base.vo", "Mid.vo"):
            self.assertTrue(os.path.exists(self.ws.path(name)), name)


if __name__ == "__main__":
    unittest.main()
