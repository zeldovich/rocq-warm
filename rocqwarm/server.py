"""The rocq-warm supervisor: warm sessions, kept alive between `rocq-warm` runs.

One process per workspace, listening on a Unix socket under
`<workspace>/.rocq-warm/`.  It owns the `rocq repl` children, so a check from a
fresh shell reuses the session the previous check left parked at the edit.

Every guard here exists because a warm session that lies is worse than no
session at all:

* a rebuilt dependency `.vo` throws the session away rather than being checked
  against the copy Rocq loaded into memory an hour ago -- and the set of
  `.vo` files watched is the one Rocq itself reports having loaded, not a
  guess from `rocq dep`;
* a dependency whose `.vo` is older than its `.v` (or than a `.vo` it
  requires) is refused, not checked against: the verdict would be about a
  library that no longer exists.  Make's own rule decides what "older" means;
* a green check does NOT write a `.vo` -- that would double the cost of
  every passing check -- so it says so, and a dependent checked next is
  refused until the `.vo` is rebuilt (by `make`, by `--compile`, or by
  `--rebuild`).  A compile the daemon is running itself is waited for;
* a session that exceeds its RSS ceiling or a check that exceeds its wall
  timeout is killed and reported, not left resident -- a large development has a history
  of a single `vm_compute` reaching 31 GB;
* sessions are evicted LRU under a global memory budget, and idle ones time out.
"""

import fcntl
import json
import os
import signal
import socket
import struct
import sys
import threading
import time

from . import compile as compile_mod, project, session as session_mod

DEFAULT_IDLE_TIMEOUT = 1800.0
DEFAULT_MAX_SESSIONS = 4
DEFAULT_CHECK_TIMEOUT = 1800.0


# A daemon is per-checkout, and build machines run many checkouts at once.
# Every one of these defaults is therefore about NOT assuming the machine is
# ours: a daemon that helps itself to half of RAM is fine alone and ruinous
# ten-up, and the process it gets killed to make room for is somebody else's.
DEFAULT_BUDGET_CAP = 32e9       # this daemon's sessions, all together
DEFAULT_MIN_FREE = 4e9          # ... and leave at least this much for others


def _total_bytes():
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError):
        return 0


def _available_bytes():
    """What the kernel thinks can still be allocated without swapping.

    The number that matters on a shared machine: it moves when OTHER people's
    work grows, which per-daemon bookkeeping cannot see.
    """
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _budget_bytes():
    env = os.environ.get("ROCQ_WARM_MAX_RSS_GB")
    if env:
        return float(env) * 1e9
    total = _total_bytes()
    if not total:
        return DEFAULT_BUDGET_CAP
    return min(total * 0.5, DEFAULT_BUDGET_CAP)


def _min_free_bytes():
    env = os.environ.get("ROCQ_WARM_MIN_FREE_GB")
    if env:
        return float(env) * 1e9
    return max(DEFAULT_MIN_FREE, _total_bytes() * 0.05)


def _session_ceiling(budget, max_sessions):
    env = os.environ.get("ROCQ_WARM_MAX_SESSION_GB")
    if env:
        return float(env) * 1e9
    # Half the budget, not budget/max_sessions: one big proof legitimately
    # costs several GB, and a ceiling tight enough to kill it is worse than no
    # ceiling at all.  This still catches a runaway an order of magnitude out.
    return budget / 2.0


class Entry:
    def __init__(self, sess, flags, cwd, toolchain=None):
        self.sess = sess
        self.flags = flags
        self.cwd = cwd
        self.toolchain = toolchain
        # .vo path -> (mtime_ns, size) as each was when the session loaded it.
        # Filled in after every check from what Rocq says it has loaded.
        self.loaded = {}
        self.libraries = {}         # logical name -> .vo path
        self.last_used = time.time()
        self.lock = threading.Lock()

    def loaded_changed(self):
        """Has any .vo the session holds been rebuilt, removed or replaced?"""
        now = project.fingerprint(sorted(self.loaded))
        return any(self.loaded[p] != (m, sz) for p, m, sz in now)


class Server:
    def __init__(self, root, idle_timeout=DEFAULT_IDLE_TIMEOUT,
                 max_sessions=DEFAULT_MAX_SESSIONS):
        self.root = os.path.abspath(root)
        self.dir = os.path.join(self.root, ".rocq-warm")
        self.sock_path = os.path.join(self.dir, "sock")
        self.pids_path = os.path.join(self.dir, "sessions")
        self.sessions = {}
        self.lock = threading.Lock()
        self.idle_timeout = idle_timeout
        self.max_sessions = max_sessions
        self._lock_fd = None
        self.budget = _budget_bytes()
        self.min_free = _min_free_bytes()
        self.started = time.time()
        self.graphs = {}
        # Only ever compiles on request: `--compile` and `--rebuild`.
        self.compiler = compile_mod.Compiler(
            available=_available_bytes, min_free=self.min_free)

    # -------------------------------------------------------------- sessions

    @staticmethod
    def _toolchain_env(toolchain):
        rocq, env_items = (toolchain or (None, ()))
        # MERGE, never replace: subprocess `env=` is the child's whole
        # environment, and a child without HOME or TMPDIR misbehaves in
        # ways that have nothing to do with Rocq.
        env = dict(os.environ, **dict(env_items)) if env_items else None
        return rocq or "rocq", env

    def _graph(self, flags, cwd, toolchain):
        """The dependency graph for one project, shared by its sessions."""
        key = (cwd, tuple(flags), toolchain)
        with self.lock:
            g = self.graphs.get(key)
        if g is None:
            rocq, env = self._toolchain_env(toolchain)
            g = project.DepGraph(flags, cwd, project.find_project(cwd),
                                 rocq=rocq, env=env)
            with self.lock:
                self.graphs[key] = g
        return g

    def _entry(self, path, force_cold=False, silent=True, toolchain=None):
        """The warm session for `path`, cold-started if anything moved."""
        key = os.path.abspath(path)
        with self.lock:
            entry = self.sessions.get(key)
        flags, cwd = project.flags_for(key)
        if entry is not None:
            if entry.flags != flags:
                self._drop(key)                 # the build flags changed
                entry = None
            elif entry.toolchain != toolchain:
                self._drop(key)                 # a different Rocq / opam switch
                entry = None
            elif entry.loaded_changed():
                self._drop(key)                 # a dependency was rebuilt
                entry = None
            elif (force_cold or not entry.sess.alive
                  or entry.sess.silent != silent):
                self._drop(key)
                entry = None
        if entry is None:
            rocq, env = self._toolchain_env(toolchain)
            sess = session_mod.Session(
                key, flags, cwd=cwd, silent=silent, rocq=rocq, env=env,
                rss_limit=_session_ceiling(self.budget, self.max_sessions))
            entry = Entry(sess, flags, cwd, toolchain)
            with self.lock:
                self.sessions[key] = entry
            self._record_sessions()
            self._evict()
        entry.last_used = time.time()
        return entry

    def _drop(self, key):
        with self.lock:
            entry = self.sessions.pop(key, None)
        if entry is not None:
            entry.sess.stop()
        self._record_sessions()

    # ------------------------------------------------------- stray children

    def _record_sessions(self):
        """Write down which `rocq` processes we own.

        `Session.stop` only runs if the daemon is alive to run it.  Kill the
        daemon itself -- `kill -9`, a lost terminal, an OOM -- and its children
        are left blocked on a closed stdin, holding several GB each, with
        nothing that will ever reap them.  A pid file lets the NEXT daemon
        clean up after the last one.
        """
        try:
            with self.lock:
                rows = ["%d\t%s" % (e.sess.proc.pid, k)
                        for k, e in self.sessions.items() if e.sess.alive]
            tmp = self.pids_path + ".tmp"
            with open(tmp, "w") as f:
                f.write("\n".join(rows) + ("\n" if rows else ""))
            os.replace(tmp, self.pids_path)
        except OSError:
            pass

    def reap_strays(self):
        """Kill sessions a previous daemon left behind.

        By pid AND cmdline: a bare pid may have been recycled by an unrelated
        process, and on a shared machine a pattern kill would take out other
        checkouts' sessions as well as ours.
        """
        killed = 0
        try:
            rows = open(self.pids_path).read().splitlines()
        except OSError:
            return 0
        for row in rows:
            pid_s, _, path = row.partition("\t")
            if not pid_s.isdigit() or not path:
                continue
            try:
                with open("/proc/%s/cmdline" % pid_s, "rb") as f:
                    args = f.read().replace(b"\0", b" ").decode("utf8", "replace")
            except OSError:
                continue
            if "repl" in args and path in args:
                try:
                    os.kill(int(pid_s), signal.SIGKILL)
                    killed += 1
                except OSError:
                    pass
        self._record_sessions()
        return killed

    def _evict(self):
        """Keep the session set inside our own budget AND the machine's.

        Our own budget bounds one daemon.  It cannot bound ten, and it cannot
        see the compile somebody else just started, so eviction also yields
        when the machine as a whole is running out -- which is the only signal
        that works when the pressure is not ours.  Under real pressure we will
        give up our last session too: degrading to a cold check is a cost we
        pay ourselves, where an OOM kill is a cost somebody else pays.
        """
        while True:
            with self.lock:
                items = sorted(self.sessions.items(), key=lambda kv: kv[1].last_used)
            if not items:
                return
            used = sum(e.sess.rss_bytes() for _k, e in items)
            avail = _available_bytes()
            pressure = avail is not None and avail < self.min_free
            if not (pressure or len(items) > self.max_sessions
                    or used > self.budget):
                return
            if not pressure and len(items) == 1:
                return                  # our own budget never costs the last one
            victim = self._idle_victim(items)
            if victim is None:
                return                  # everything we hold is mid-check
            self._drop(victim)

    @staticmethod
    def _idle_victim(items):
        """The least-recently-used session that is not mid-check.

        Evicting a session out from under a running check would kill the child
        it is talking to; that recovers, but it wastes exactly the work we are
        trying to save.
        """
        for key, entry in items:
            if entry.lock.acquire(blocking=False):
                entry.lock.release()
                return key
        return None

    def reap_idle(self):
        now = time.time()
        for key, entry in list(self.sessions.items()):
            if now - entry.last_used > self.idle_timeout:
                self._drop(key)

    # -------------------------------------------------------------- requests

    def handle(self, req):
        cmd = req.get("cmd")
        if cmd == "check":
            return self.do_check(req)
        if cmd == "status":
            return self.do_status()
        if cmd == "stop":
            self.compiler.stop()
            for key in list(self.sessions):
                self._drop(key)
            return {"ok": True, "stopped": True}     # _serve_one then exits
        if cmd == "ping":
            return {"ok": True, "pid": os.getpid()}
        return {"ok": False, "error": "unknown command %r" % cmd}

    def do_check(self, req):
        path = os.path.abspath(req["path"])
        try:
            text = open(path, "rb").read()
        except OSError as e:
            return {"ok": False, "error": str(e)}
        t0 = time.time()
        timeout = float(req.get("timeout") or DEFAULT_CHECK_TIMEOUT)
        # `Set Silent` is decided when the session starts, so asking for the
        # proof's own output means starting over.
        toolchain = (req.get("rocq"),
                     tuple(sorted((req.get("env") or {}).items())))
        rocq, env = self._toolchain_env(toolchain)
        flags, cwd = project.flags_for(path)

        # What this text loads, from its Require lines as they are NOW.
        graph = self._graph(flags, cwd, toolchain)
        graph.refresh(extra=[path])
        closure = graph.closure(path)

        # A compile of this file from an older text is describing a file that
        # no longer exists; a compile of the same text is left to finish.
        digest = compile_mod.digest_of(text=text)
        compile_mod.log("check: %s (digest %s%s%s)", path, digest[:8],
                        ", --compile" if req.get("wait_vo") else "",
                        ", --rebuild" if req.get("rebuild") else "")
        self._cancel_other_text(path, digest)

        # Refuse to check against a library that no longer matches its
        # source.  A compile we started ourselves is waited for instead.
        stale = self._stale(closure, graph, deadline=t0 + timeout)
        if stale and req.get("rebuild"):
            failure = self._rebuild(closure, stale, graph, rocq, env,
                                    deadline=t0 + timeout)
            if failure is not None:
                return failure
            stale = self._stale(closure, graph, deadline=t0 + timeout)
        stale_rows = [self._stale_row(vo, why) for vo, why in stale]
        if stale and not req.get("allow_stale"):
            return {"ok": False, "stale": stale_rows,
                    "error": "%d stale dependenc%s" % (
                        len(stale), "y" if len(stale) == 1 else "ies")}

        entry = self._entry(path, force_cold=bool(req.get("cold")),
                            silent=not bool(req.get("verbose")),
                            toolchain=toolchain)
        # Everything we know the session holds or is about to load, stat'ed
        # BEFORE it loads anything.  A .vo rebuilt while the check runs must
        # not be recorded with its new mtime as if that were what was loaded.
        watched = sorted(set(closure) | set(entry.loaded))
        pre = {p: (m, sz) for p, m, sz in project.fingerprint(watched)}
        with entry.lock:
            if not entry.sess.alive:
                entry.sess.start()
            try:
                result = entry.sess.check(text, timeout=timeout)
            except session_mod.FeedTimeout as e:
                entry.sess.stop()
                self._drop(path)
                return {"ok": False, "error": "timed out after %.0fs (%s); "
                                              "session discarded" % (timeout, e)}
            except session_mod.MemoryLimit as e:
                entry.sess.stop()
                self._drop(path)
                return {"ok": False,
                        "error": "%s; session discarded (raise the ceiling "
                                 "with ROCQ_WARM_MAX_SESSION_GB)" % e}
            except session_mod.SessionDead as e:
                self._drop(path)
                return {"ok": False, "error": "rocq died: %s" % e}
            unreliable = None
            try:
                libraries = entry.sess.loaded_libraries()
            except Exception as e:                      # noqa: BLE001
                libraries, unreliable = {}, "%s: %s" % (type(e).__name__, e)
            rss = entry.sess.rss_bytes()
        post = {p: (m, sz) for p, m, sz in project.fingerprint(
            sorted(set(watched) | set(libraries.values())))}
        entry.loaded = {p: pre.get(p, post[p]) for p in post}
        entry.libraries = libraries
        moved = [p for p in watched if pre[p] != post[p]]
        note = None
        if moved:
            note = ("%s changed during the check; the verdict may be about "
                    "either version, and the session was discarded"
                    % ", ".join(os.path.relpath(p, self.root) for p in moved))
            self._drop(path)
        elif unreliable:
            note = ("could not ask rocq what it loaded (%s); the session was "
                    "discarded" % unreliable)
            self._drop(path)
        compile_mod.log("check: %s %s [%s, %d sentences, %.1fs]%s", path,
                        "OK" if result.ok else "FAILED", result.mode,
                        result.replayed, result.seconds,
                        "; " + note if note else "")
        job = None
        if result.ok and not moved and req.get("wait_vo"):
            job = self.compiler.submit(path, flags, cwd, rocq=rocq, env=env,
                                       digest=digest)
            self.compiler.wait(path, timeout=max(0.0, t0 + timeout - time.time()))
            compile_mod.log("check: %s waited for job %d: %s", path, job.seq,
                            job.state)
        self._evict()
        return {
            "ok": True,
            "passed": result.ok,
            "mode": result.mode,
            "replayed": result.replayed,
            "sentences": result.total,
            "seconds": result.seconds,
            "rss": rss,
            "libraries": len(libraries),
            "stale": stale_rows,            # only when allow_stale let it through
            "note": note,
            "vo": _describe_job(job) if job is not None else None,
            # Why make would rebuild THIS file's .vo now -- after a green
            # check that is "its source is newer", which the user is told.
            "vo_stale": project.staleness(project.vo_of(path), graph.graph),
            "diags": [{"kind": d.kind,
                       "span": d.span(text),
                       "message": d.message().decode("utf8", "replace")}
                      for d in result.diags],
        }

    # ------------------------------------------------------------ staleness

    def _stale(self, closure, graph, deadline):
        """Stale members of `closure`, after waiting for our own compiles.

        A `.vo` that is stale because its compile has not finished yet is
        not a reason to refuse; it is a reason to wait.  Only the daemon's
        own jobs are waited for -- somebody's `make` in another terminal is
        invisible, and guessing at it would be guessing.
        """
        while True:
            stale = project.stale_deps(closure, graph.graph)
            pending = [j for j in (self.compiler.pending(vo) for vo, _w in stale)
                       if j is not None]
            if not pending or time.time() >= deadline:
                return stale
            for j in pending:
                self.compiler.wait(j.v, timeout=max(0.0, deadline - time.time()))

    def _stale_row(self, vo, why):
        row = {"vo": vo, "why": why}
        job = self.compiler.jobs.get(project.v_of(vo))
        if job is not None and job.state == "failed":
            row["compile_output"] = job.output.decode("utf8", "replace")
            row["why"] += " (rocq-warm's own compile of it failed)"
        return row

    def _rebuild(self, closure, stale, graph, rocq, env, deadline):
        """Compile what is stale, and what that makes stale, in order.

        Returns a response describing the failure, or None on success.
        """
        plan = project.rebuild_plan(closure, stale, graph.graph)
        jobs = []
        for vo, after in plan:
            v = project.v_of(vo)
            if not os.path.isfile(v):
                return {"ok": False, "error": "cannot rebuild %s: %s does not "
                                              "exist" % (vo, v)}
            flags, cwd = project.flags_for(v)
            jobs.append(self.compiler.submit(v, flags, cwd, rocq=rocq, env=env,
                                             after=after))
        self.compiler.wait_all(jobs, timeout=max(0.0, deadline - time.time()))
        failed = [j for j in jobs if j.done and not j.succeeded]
        if failed:
            return {"ok": False,
                    "error": "rebuilding %s failed" % ", ".join(
                        os.path.relpath(j.v, self.root) for j in failed),
                    "compile_failed": [_describe_job(j) for j in failed]}
        if not all(j.done for j in jobs):
            return {"ok": False, "error": "timed out rebuilding %d stale "
                                          "dependencies" % len(jobs)}
        return None

    def _cancel_other_text(self, path, digest):
        job = self.compiler.jobs.get(path)
        if job is not None and not job.done and job.digest != digest:
            self.compiler.cancel(path)

    def do_status(self):
        out = []
        for key, entry in sorted(self.sessions.items()):
            out.append({
                "path": key,
                "pid": entry.sess.proc.pid if entry.sess.alive else None,
                "alive": entry.sess.alive,
                "sentences": len(entry.sess.sentences),
                "complete": entry.sess.complete,
                "rss": entry.sess.rss_bytes(),
                "idle": time.time() - entry.last_used,
                "libraries": len(entry.libraries),
                "watched": len(entry.loaded),
            })
        return {"ok": True, "pid": os.getpid(), "uptime": time.time() - self.started,
                "budget": self.budget, "min_free": self.min_free,
                "available": _available_bytes(), "sessions": out,
                "compiles": self.compiler.status()}

    # ----------------------------------------------------------------- serve

    def serve(self):
        """Serve this workspace, if nobody else already is.

        Two clients can race to spawn a daemon.  Without the lock the loser
        unlinks the winner's socket and binds its own, which orphans a daemon
        that keeps its sessions resident and unreachable until it times out --
        exactly the memory nobody can account for.  The lock fd is held for the
        daemon's life and released when it exits.
        """
        os.makedirs(self.dir, exist_ok=True)
        self._lock_fd = os.open(os.path.join(self.dir, "lock"),
                                os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return                      # somebody else is serving this tree
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.sock_path)
        srv.listen(16)
        strays = self.reap_strays()
        if strays:
            sys.stderr.write("reaped %d session(s) left by a previous daemon\n"
                             % strays)
        threading.Thread(target=self._reaper, daemon=True).start()
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=self._serve_one, args=(conn,),
                             daemon=True).start()

    def _reaper(self):
        """Drop idle sessions, and eventually the daemon itself.

        A daemon whose workspace has gone away -- a temp tree, a deleted
        worktree -- would otherwise sit on several GB of Rocq for ever.
        """
        empty_since = None
        while True:
            time.sleep(30)
            try:
                if not os.path.isdir(self.root):
                    self.shutdown()             # the workspace itself is gone
                self.reap_idle()
                self._evict()
                if self.sessions:
                    empty_since = None
                    continue
                empty_since = empty_since or time.time()
                if time.time() - empty_since > self.idle_timeout:
                    self.shutdown()
            except Exception:
                pass

    def shutdown(self):
        """Stop every session, then the daemon.  Never leave a child behind."""
        self.compiler.stop()
        for key in list(self.sessions):
            self._drop(key)
        os._exit(0)

    def _serve_one(self, conn):
        try:
            req = recv_msg(conn)
            if req is None:
                return
            try:
                resp = self.handle(req)
            except Exception as e:                      # never take the daemon
                resp = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
            send_msg(conn, resp)
            if req.get("cmd") == "stop":
                os._exit(0)
        finally:
            try:
                conn.close()
            except Exception:
                pass


def _describe_job(job):
    if job is None:
        return None
    d = job.describe()
    d["output"] = job.output.decode("utf8", "replace")
    return d


def send_msg(sock, obj):
    payload = json.dumps(obj).encode()
    sock.sendall(struct.pack("!I", len(payload)) + payload)


def recv_msg(sock):
    head = _recv_exactly(sock, 4)
    if head is None:
        return None
    body = _recv_exactly(sock, struct.unpack("!I", head)[0])
    return None if body is None else json.loads(body)


def _recv_exactly(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def main(argv):
    root = argv[1] if len(argv) > 1 else os.getcwd()
    Server(root).serve()


if __name__ == "__main__":
    main(sys.argv)
