"""The background compiler: the build's own step, run on the build's behalf.

What has to hold: the .vo it writes is the one `make` would write, stamped so
that an edit during the compile is not hidden; a job for text that no longer
exists is cancelled and leaves nothing behind; jobs for a dependency and its
dependent run in that order; and a failed dependency is not built upon.
"""

import os
import subprocess
import time
import unittest

from rocq_warm_helpers import Workspace, requires_rocq, wait_for
from rocqwarm import compile as compile_mod

SLOW = b"Definition x := 1.\nLemma slow : True.\nProof. do 3000000 idtac. exact I. Qed.\n"
SLOW_BASE = b"Definition base := 1.\nLemma slow : True.\nProof. do 3000000 idtac. exact I. Qed.\n"


def mtime(path):
    return os.stat(path).st_mtime_ns


@requires_rocq
class CompilerTests(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.cleanup)
        self.comp = compile_mod.Compiler(jobs=2)
        self.addCleanup(self.comp.stop)

    def submit(self, name, **kw):
        return self.comp.submit(self.ws.path(name), self.ws.flags, self.ws.dir,
                                **kw)

    def test_a_compile_produces_what_make_would(self):
        self.ws.write("A.v", b"Definition a := 1.\n")
        job = self.submit("A.v")
        self.comp.wait(self.ws.path("A.v"), timeout=120)
        self.assertEqual(job.state, "ok", job.output)
        # .vo and .glob are what `coqc` produces on every 9.x; .vok/.vos are
        # a -vos/-vok concern and Rocq 9.2 stopped writing them on a plain
        # compile, so do not require them.
        for suffix in (".vo", ".glob"):
            self.assertTrue(os.path.exists(self.ws.path("A" + suffix)), suffix)
        # And Rocq accepts the result.
        self.ws.write("B.v", b"Require Import T.A.\nDefinition b := a.\n")
        rc, out = self.ws.coqc("B.v")
        self.assertEqual(rc, 0, out)

    def test_the_vo_is_stamped_with_the_start_of_the_compile(self):
        self.ws.write("A.v", SLOW)
        before = time.time_ns()
        job = self.submit("A.v")
        self.comp.wait(self.ws.path("A.v"), timeout=120)
        self.assertEqual(job.state, "ok", job.output)
        vo = mtime(self.ws.path("A.vo"))
        self.assertGreaterEqual(vo, before)
        self.assertEqual(vo, job.started_ns)
        self.assertLess(vo, int(job.finished * 1e9),
                        "stamped with the finish time, not the start")

    def test_a_failed_compile_leaves_nothing_behind(self):
        self.ws.write("A.v", b"Definition a := bogus.\n")
        job = self.submit("A.v")
        self.comp.wait(self.ws.path("A.v"), timeout=120)
        self.assertEqual(job.state, "failed")
        self.assertIn(b"Error:", job.output)
        self.assertFalse(os.path.exists(self.ws.path("A.vo")))

    def test_a_new_text_cancels_the_old_job_and_removes_its_output(self):
        self.ws.write("A.v", SLOW)
        first = self.submit("A.v")
        self.assertTrue(wait_for(lambda: first.state == "running", timeout=30))
        time.sleep(0.3)
        self.ws.write("A.v", b"Definition x := 2.\n")
        second = self.submit("A.v")
        self.assertIsNot(second, first)
        self.assertEqual(first.state, "cancelled")
        self.comp.wait(self.ws.path("A.v"), timeout=120)
        self.assertEqual(second.state, "ok", second.output)
        self.assertGreaterEqual(mtime(self.ws.path("A.vo")), second.started_ns)

    def test_the_same_text_reuses_the_job(self):
        self.ws.write("A.v", SLOW)
        first = self.submit("A.v")
        self.assertIs(self.submit("A.v"), first)

    def test_a_dependent_waits_for_its_dependency(self):
        self.ws.write("Base.v", SLOW_BASE)
        self.ws.write("Mid.v", b"Require Import T.Base.\nDefinition mid := base.\n")
        base = self.submit("Base.v")
        mid = self.submit("Mid.v", after=[self.ws.path("Base.vo")])
        self.comp.wait(self.ws.path("Mid.v"), timeout=120)
        self.assertEqual(base.state, "ok", base.output)
        self.assertEqual(mid.state, "ok", mid.output)
        self.assertGreaterEqual(mid.started, base.finished)

    def test_a_failed_dependency_is_not_built_upon(self):
        self.ws.write("Base.v", b"Definition base := bogus.\n")
        self.ws.write("Mid.v", b"Require Import T.Base.\nDefinition mid := base.\n")
        self.submit("Base.v")
        mid = self.submit("Mid.v", after=[self.ws.path("Base.vo")])
        self.comp.wait(self.ws.path("Mid.v"), timeout=120)
        self.assertEqual(mid.state, "skipped")
        self.assertIn("Base.v", mid.why)

    def test_the_job_limit_is_respected(self):
        comp = compile_mod.Compiler(jobs=1)
        self.addCleanup(comp.stop)
        self.ws.write("A.v", SLOW)
        self.ws.write("B.v", SLOW)
        a = comp.submit(self.ws.path("A.v"), self.ws.flags, self.ws.dir)
        b = comp.submit(self.ws.path("B.v"), self.ws.flags, self.ws.dir)
        comp.wait(self.ws.path("B.v"), timeout=240)
        self.assertEqual((a.state, b.state), ("ok", "ok"))
        self.assertGreaterEqual(b.started, a.finished)

    def test_a_queued_job_skips_when_its_source_changes_before_it_runs(self):
        """rocq compile reads the file from disk.  A job queued for text A
        that only starts running after the file has become B must NOT compile
        B under A's name -- it must step aside for the newer text.  Made
        deterministic by filling the single worker so the target job is
        provably still queued when the file changes."""
        comp = compile_mod.Compiler(jobs=1)
        self.addCleanup(comp.stop)
        self.ws.write("Blocker.v", SLOW)
        self.ws.write("A.v", b"Definition a := 1.\n")
        blocker = comp.submit(self.ws.path("Blocker.v"), self.ws.flags, self.ws.dir)
        self.assertTrue(wait_for(lambda: blocker.state == "running", timeout=30))
        target = comp.submit(self.ws.path("A.v"), self.ws.flags, self.ws.dir)
        self.assertEqual(target.state, "queued")
        # Change A.v while the target job is stuck behind the blocker.
        self.ws.write("A.v", b"Definition a := 2.\n")
        comp.wait(self.ws.path("A.v"), timeout=120)
        self.assertEqual(target.state, "skipped", target.output)
        self.assertIn("changed before", target.why)
        self.assertFalse(os.path.exists(self.ws.path("A.vo")),
                         "compiled a file it was not asked to compile")

    def test_a_source_changing_during_the_compile_discards_the_vo(self):
        """The narrower race: the file changes while rocq is reading or
        compiling it, so what landed in the .vo is neither reliably A nor B.
        It must be thrown away, not stamped fresh."""
        self.ws.write("A.v", SLOW)
        job = self.submit("A.v")
        self.assertTrue(wait_for(lambda: job.state == "running", timeout=30))
        time.sleep(0.2)
        self.ws.write("A.v", b"Definition x := 2.\n")     # mid-compile
        self.comp.wait(self.ws.path("A.v"), timeout=120)
        self.assertEqual(job.state, "skipped", job.output)
        self.assertIn("during", job.why)
        self.assertFalse(os.path.exists(self.ws.path("A.vo")),
                         "left a .vo that matches neither text")

    def test_a_stable_source_compiles_normally(self):
        """The guard must not fire when nothing changed."""
        self.ws.write("A.v", b"Definition a := 1.\n")
        job = self.submit("A.v")
        self.comp.wait(self.ws.path("A.v"), timeout=120)
        self.assertEqual(job.state, "ok", job.output)
        self.assertTrue(os.path.exists(self.ws.path("A.vo")))

    def test_stop_kills_running_jobs(self):
        self.ws.write("A.v", SLOW)
        job = self.submit("A.v")
        self.assertTrue(wait_for(lambda: job.proc is not None, timeout=30))
        pid = job.proc.pid
        self.comp.stop()
        self.assertTrue(wait_for(lambda: not _alive(pid), timeout=10),
                        "the compile outlived the compiler")
        self.assertEqual(job.state, "cancelled")


def _alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        with open("/proc/%d/stat" % pid) as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


if __name__ == "__main__":
    unittest.main()
