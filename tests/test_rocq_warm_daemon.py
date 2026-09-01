"""The daemon, end to end: `rocq-warm check` from a cold shell twice over.

The point of the daemon is that the second `rocq-warm` process, with no memory
of the first, still lands in the session the first one left parked.  These
tests therefore drive the real CLI in a subprocess rather than the Session API.
"""

import os
import re
import shutil
import signal
import subprocess
import time
import unittest

from rocq_warm_helpers import TOOLS, Workspace, requires_rocq
from rocqwarm import server

CLI = os.path.join(TOOLS, "rocq-warm")

GOOD = b"""Definition a := 1.

Lemma one : True.
Proof. exact I. Qed.

Lemma two : 1 = 1.
Proof. reflexivity. Qed.
"""
BAD = GOOD.replace(b"Proof. reflexivity. Qed.", b"Proof. exact I. Qed.")


@requires_rocq
class DaemonTests(unittest.TestCase):
    NAME = "Dm.v"

    def setUp(self):
        self.ws = Workspace()
        # addCleanup is LIFO: the daemon must be stopped BEFORE the workspace
        # (and its socket) is deleted, or the session is orphaned.
        self.addCleanup(self.ws.cleanup)
        self.addCleanup(self.stop_daemon)
        self.path = self.ws.write(self.NAME, GOOD)

    def stop_daemon(self):
        subprocess.run([CLI, "stop", "--root", self.ws.dir],
                       capture_output=True, timeout=60)

    def run_cli(self, *args, **kw):
        return subprocess.run([CLI] + list(args), capture_output=True,
                              cwd=self.ws.dir, timeout=kw.get("timeout", 300))

    def check(self, **kw):
        return self.run_cli("check", self.NAME, **kw)

    def test_verdicts_and_exit_codes(self):
        ok = self.check()
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn(b"OK", ok.stderr)

        self.ws.write(self.NAME, BAD)
        bad = self.check()
        self.assertEqual(bad.returncode, 1)
        self.assertIn(b"Error:", bad.stdout)
        self.assertIn(b'File "%s"' % self.NAME.encode(), bad.stdout)

    def test_a_second_process_reuses_the_warm_session(self):
        self.assertEqual(self.check().returncode, 0)
        self.ws.write(self.NAME, BAD)
        self.assertEqual(self.check().returncode, 1)
        self.ws.write(self.NAME, GOOD)
        again = self.check()
        self.assertEqual(again.returncode, 0)
        self.assertIn(b"replay", again.stderr,
                      "a separate process should have found the parked session")

    def test_the_proof_s_own_output_is_suppressed_unless_asked_for(self):
        """Printing an Iris goal after every sentence costs more than the proof
        does, so `Set Silent` is on by default -- which also drops `Time`'s
        "Finished transaction".  Diagnostics are never suppressed."""
        text = GOOD + b"\nTime Check 1.\n"
        self.ws.write(self.NAME, text)
        quiet = self.check()
        self.assertEqual(quiet.returncode, 0, quiet.stderr)
        self.assertNotIn(b"Finished transaction", quiet.stdout)
        loud = self.run_cli("check", self.NAME, "--show-output")
        self.assertEqual(loud.returncode, 0, loud.stderr)
        self.assertIn(b"Finished transaction", loud.stdout)

    def test_status_lists_the_session_and_stop_clears_it(self):
        self.check()
        st = self.run_cli("status", "--root", self.ws.dir)
        self.assertIn(self.NAME.encode(), st.stdout)
        self.stop_daemon()
        st2 = self.run_cli("status", "--root", self.ws.dir)
        self.assertIn(b"no daemon running", st2.stdout)

    def test_cold_flag_discards_the_session(self):
        self.check()
        forced = self.run_cli("check", self.NAME, "--cold")
        self.assertIn(b"cold", forced.stderr)

    def test_a_rebuilt_dependency_invalidates_the_session(self):
        """The failure this prevents is the worst one available: reporting a
        pass against a library that was replaced on disk."""
        self.ws.write("Dep.v", b"Definition dep := 1.\n")
        user = b"Require Import T.Dep.\nDefinition u := dep.\n"
        self.ws.write("User.v", user)
        with open(os.path.join(self.ws.dir, "_CoqProject"), "w") as f:
            f.write("-R . T\nDep.v\nUser.v\n")
        subprocess.run(["coqc", "-q"] + self.ws.flags + ["Dep.v"],
                       cwd=self.ws.dir, check=True, capture_output=True)

        first = self.run_cli("check", "User.v")
        self.assertEqual(first.returncode, 0, first.stderr)

        # Rebuild the dependency with `dep` gone.
        self.ws.write("Dep.v", b"Definition other := 1.\n")
        subprocess.run(["coqc", "-q"] + self.ws.flags + ["Dep.v"],
                       cwd=self.ws.dir, check=True, capture_output=True)

        second = self.run_cli("check", "User.v")
        self.assertEqual(second.returncode, 1,
                         "stale .vo accepted: %s / %s"
                         % (second.stdout, second.stderr))
        self.assertIn(b"cold", second.stderr)

    def test_a_different_rocq_invalidates_the_session(self):
        """A daemon outlives the shell that started it.  On a machine with
        several opam switches the next caller may be in a different one, and
        silently answering from the old switch's Rocq is the worst kind of
        wrong: a confident OK about a toolchain you are not using."""
        first = self.check()
        self.assertEqual(first.returncode, 0, first.stderr)
        real = shutil.which("rocq")
        self.assertIsNotNone(real)
        # A different absolute path to the same binary is enough: what matters
        # is that the daemon notices it is not the one it warmed up with.
        alias_dir = os.path.join(self.ws.dir, "bin")
        os.makedirs(alias_dir, exist_ok=True)
        for tool in ("rocq", "coqc"):
            link = os.path.join(alias_dir, tool)
            if not os.path.exists(link):
                os.symlink(shutil.which(tool), link)
        env = dict(os.environ, PATH=alias_dir + os.pathsep + os.environ["PATH"])
        again = subprocess.run([CLI, "check", self.NAME], cwd=self.ws.dir,
                               env=env, capture_output=True, timeout=300)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn(b"cold", again.stderr,
                      "reused a session warmed up with a different rocq")

    def test_the_session_runs_the_client_s_rocq(self):
        self.assertEqual(self.check().returncode, 0)
        pids = self.session_pids()
        self.assertTrue(pids)
        got = os.path.realpath("/proc/%d/exe" % pids[0])
        want_prefix = os.path.dirname(os.path.dirname(
            os.path.realpath(shutil.which("rocq"))))
        self.assertTrue(got.startswith(want_prefix),
                        "session runs %s, not the rocq the client resolved "
                        "(%s)" % (got, want_prefix))

    def test_changing_the_project_flags_invalidates_the_session(self):
        self.assertEqual(self.check().returncode, 0)
        with open(os.path.join(self.ws.dir, "_CoqProject"), "a") as f:
            f.write("-arg -w\n-arg -all\n")
        again = self.check()
        self.assertIn(b"cold", again.stderr)

    def test_concurrent_checks_of_one_file_do_not_interleave(self):
        """Two clients, one session: the second must wait, not corrupt the
        first's stream."""
        self.assertEqual(self.check().returncode, 0)
        procs = [subprocess.Popen([CLI, "check", self.NAME],
                                  cwd=self.ws.dir, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
                 for _ in range(4)]
        for p in procs:
            out, err = p.communicate(timeout=300)
            self.assertEqual(p.returncode, 0, err)

    def test_killing_the_daemon_takes_its_idle_sessions_with_it(self):
        """Not because anything reaps them: the daemon holds the only writer on
        each child's stdin, so its death is an EOF and Rocq exits on its own.
        A child that is BUSY will not notice until it finishes, which is what
        `reap_strays` is for."""
        self.assertEqual(self.check().returncode, 0)
        pids = self.session_pids()
        self.assertTrue(pids, "no session pid reported")
        os.kill(self.daemon_pid(), signal.SIGKILL)
        deadline = time.time() + 20
        while time.time() < deadline and any(_alive(p) for p in pids):
            time.sleep(0.2)
        self.assertFalse([p for p in pids if _alive(p)],
                         "sessions outlived the daemon that owned their stdin")

    def test_reap_strays_kills_a_session_a_dead_daemon_left_behind(self):
        stray = self.fake_session(self.path)
        self.write_pidfile("%d\t%s\n" % (stray.pid, self.path))
        self.assertEqual(self.check().returncode, 0)
        deadline = time.time() + 10
        while time.time() < deadline and stray.poll() is None:
            time.sleep(0.2)
        self.assertIsNotNone(stray.poll(), "the stray was left running")

    def test_reap_strays_ignores_a_pid_that_was_recycled(self):
        """Pids get reused, and on a shared machine the wrong kill takes out
        somebody else's work.  Match the cmdline, not just the number."""
        bystander = subprocess.Popen(["sleep", "600"])
        self.addCleanup(_reap, bystander)
        self.write_pidfile("%d\t%s\n" % (bystander.pid, self.path))
        self.assertEqual(self.check().returncode, 0)
        time.sleep(1.0)
        self.assertIsNone(bystander.poll(), "killed an unrelated process")

    def fake_session(self, path):
        """A process that looks like one of ours: `repl` and the .v in argv."""
        argv = "rocqworker --kind=repl -emacs -topfile %s" % path
        p = subprocess.Popen(["bash", "-c", "exec -a %r sleep 600" % argv])
        self.addCleanup(_reap, p)
        time.sleep(0.3)
        return p

    def write_pidfile(self, text):
        d = os.path.join(self.ws.dir, ".rocq-warm")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "sessions"), "w") as f:
            f.write(text)

    def daemon_pid(self):
        out = self.run_cli("status", "--root", self.ws.dir).stdout.decode()
        return int(re.search(r'daemon pid (\d+)', out).group(1))

    def test_a_killed_session_is_restarted_rather_than_reported_as_broken(self):
        self.assertEqual(self.check().returncode, 0)
        killed = 0
        for pid in self.session_pids():
            try:
                os.kill(pid, 9)
                killed += 1
            except OSError:
                pass
        if not killed:
            self.skipTest("could not find the session's rocq process")
        time.sleep(0.5)
        again = self.check()
        self.assertEqual(again.returncode, 0, again.stderr)

    def session_pids(self):
        """The pids of THIS daemon's sessions, from `status`.

        Never `pgrep -f "repl -emacs"`: build machines are usually shared, and
        a pattern kill takes out every other checkout's sessions too -- which
        surfaces in the victim as an unexplained `SessionDead` with no cause
        anywhere near it.  (This test learned that by killing a concurrent
        corpus run.)
        """
        out = self.run_cli("status", "--root", self.ws.dir).stdout.decode()
        return [int(m.group(1)) for m in re.finditer(r'pid (\d+)$', out, re.M)]


@requires_rocq
class ConcurrentAgentTests(unittest.TestCase):
    """Several checkouts on one machine, which is the normal case on a build
    box: one agent's use of the tool must not disturb another's."""

    def setUp(self):
        self.a = Workspace("A")
        self.b = Workspace("B")
        for ws in (self.a, self.b):
            self.addCleanup(ws.cleanup)
            self.addCleanup(self.stop, ws)
            ws.write("P.v", b"Definition a := 1.\n"
                            b"Lemma l : True. Proof. exact I. Qed.\n")

    def stop(self, ws):
        subprocess.run([CLI, "stop", "--root", ws.dir],
                       capture_output=True, timeout=60)

    def cli(self, ws, *args):
        return subprocess.run([CLI] + list(args), cwd=ws.dir,
                              capture_output=True, timeout=300)

    def daemons(self):
        out = {}
        for name, ws in (("a", self.a), ("b", self.b)):
            st = self.cli(ws, "status", "--root", ws.dir).stdout.decode()
            m = re.search(r'daemon pid (\d+)', st)
            out[name] = int(m.group(1)) if m else None
        return out

    def test_each_checkout_gets_its_own_daemon(self):
        self.assertEqual(self.cli(self.a, "check", "P.v").returncode, 0)
        self.assertEqual(self.cli(self.b, "check", "P.v").returncode, 0)
        pids = self.daemons()
        self.assertIsNotNone(pids["a"])
        self.assertIsNotNone(pids["b"])
        self.assertNotEqual(pids["a"], pids["b"])

    def test_stopping_one_leaves_the_other_running(self):
        self.assertEqual(self.cli(self.a, "check", "P.v").returncode, 0)
        self.assertEqual(self.cli(self.b, "check", "P.v").returncode, 0)
        before = self.daemons()
        self.stop(self.a)
        after = self.daemons()
        self.assertIsNone(after["a"], "A's daemon should be gone")
        self.assertEqual(after["b"], before["b"], "B's daemon was disturbed")
        warm = self.cli(self.b, "check", "P.v")
        self.assertEqual(warm.returncode, 0, warm.stderr)
        self.assertIn(b"replay", warm.stderr, "B lost its warm session")

    def test_racing_clients_produce_exactly_one_daemon(self):
        """Two `rocq-warm check` calls can start at the same instant.  Without
        a lock the loser unlinks the winner's socket and binds its own, and the
        winner is orphaned -- still holding sessions, unreachable, and invisible
        to `status` and `stop`."""
        procs = [subprocess.Popen([CLI, "check", "P.v"], cwd=self.a.dir,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for _ in range(8)]
        for p in procs:
            _out, err = p.communicate(timeout=300)
            self.assertEqual(p.returncode, 0, err)
        time.sleep(1.0)
        self.assertEqual(_daemons_for(self.a.dir), 1,
                         "expected exactly one daemon serving the tree")

    def test_one_checkout_does_not_reap_another_s_sessions(self):
        """`reap_strays` matches on cmdline as well as pid precisely so that a
        neighbouring checkout's sessions are never in scope."""
        self.assertEqual(self.cli(self.b, "check", "P.v").returncode, 0)
        b_sessions = self.session_pids(self.b)
        self.assertTrue(b_sessions)
        # Point A's daemon at B's live sessions, then start it.
        d = os.path.join(self.a.dir, ".rocq-warm")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "sessions"), "w") as f:
            for pid in b_sessions:
                f.write("%d\t%s\n" % (pid, os.path.join(self.a.dir, "P.v")))
        self.assertEqual(self.cli(self.a, "check", "P.v").returncode, 0)
        time.sleep(1.0)
        self.assertEqual(self.session_pids(self.b), b_sessions,
                         "A's daemon killed B's sessions")

    def session_pids(self, ws):
        out = self.cli(ws, "status", "--root", ws.dir).stdout.decode()
        return [int(m.group(1)) for m in re.finditer(r'pid (\d+)$', out, re.M)]


class BudgetTests(unittest.TestCase):
    """A daemon is per-checkout; the machine is not."""

    def test_the_budget_does_not_assume_the_machine_is_ours(self):
        total = server._total_bytes()
        self.assertGreater(total, 0)
        self.assertLessEqual(server._budget_bytes(), server.DEFAULT_BUDGET_CAP,
                             "one daemon must not claim an unbounded share")
        self.assertLessEqual(server._budget_bytes(), total)

    def test_a_session_ceiling_leaves_room_for_a_real_proof(self):
        budget = server._budget_bytes()
        self.assertGreater(server._session_ceiling(budget, 4), 4e9,
                           "a ceiling this tight would kill legitimate proofs")

    def test_pressure_is_read_from_the_machine_not_from_us(self):
        avail = server._available_bytes()
        self.assertIsNotNone(avail, "MemAvailable should be readable on Linux")
        self.assertGreater(avail, 0)
        self.assertGreater(server._min_free_bytes(), 0)


def _daemons_for(root):
    """How many `rocqwarm.server <root>` processes are alive."""
    n = 0
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                args = f.read().split(b"\0")
        except OSError:
            continue
        if b"rocqwarm.server" in args and root.encode() in args:
            n += 1
    return n


def _reap(proc):
    """Kill and WAIT: an unwaited child is a zombie and a ResourceWarning."""
    if proc.poll() is None:
        proc.kill()
    proc.wait()


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


if __name__ == "__main__":
    unittest.main()
