"""The rocq-warm supervisor: warm sessions, kept alive between `rocq-warm` runs.

One process per workspace, listening on a Unix socket under
`<workspace>/.rocq-warm/`.  It owns the `rocq repl` children, so a check from a
fresh shell reuses the session the previous check left parked at the edit.

Every guard here exists because a warm session that lies is worse than no
session at all:

* a rebuilt dependency `.vo` throws the session away rather than being checked
  against the copy Rocq loaded into memory an hour ago;
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

from . import project, session as session_mod

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
    def __init__(self, sess, flags, cwd, deps, graph, toolchain=None):
        self.sess = sess
        self.flags = flags
        self.cwd = cwd
        self.deps = deps
        self.fingerprint = project.fingerprint(deps)
        self.graph = graph
        self.toolchain = toolchain
        self.last_used = time.time()
        self.lock = threading.Lock()


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

    # -------------------------------------------------------------- sessions

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
            elif project.fingerprint(entry.deps) != entry.fingerprint:
                self._drop(key)                 # a dependency was rebuilt
                entry = None
            elif entry.toolchain != toolchain:
                self._drop(key)                 # a different Rocq / opam switch
                entry = None
            elif (force_cold or not entry.sess.alive
                  or entry.sess.silent != silent):
                self._drop(key)
                entry = None
        if entry is None:
            rocq, env_items = (toolchain or (None, ()))
            rocq = rocq or "rocq"
            # MERGE, never replace: subprocess `env=` is the child's whole
            # environment, and a child without HOME or TMPDIR misbehaves in
            # ways that have nothing to do with Rocq.
            env = dict(os.environ, **dict(env_items)) if env_items else None
            proj = project.find_project(key)
            graph = project.dep_graph(
                flags, cwd, project.project_sources(proj, cwd),
                rocq=rocq, env=env)
            deps = project.closure(key, flags, cwd, graph,
                                   rocq=rocq, env=env) or []
            sess = session_mod.Session(
                key, flags, cwd=cwd, silent=silent, rocq=rocq, env=env,
                rss_limit=_session_ceiling(self.budget, self.max_sessions))
            entry = Entry(sess, flags, cwd, deps, graph, toolchain)
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
            for key in list(self.sessions):
                self._drop(key)
            return {"ok": True, "stopped": True}     # _serve_one then exits
        if cmd == "ping":
            return {"ok": True, "pid": os.getpid()}
        return {"ok": False, "error": "unknown command %r" % cmd}

    def do_check(self, req):
        path = req["path"]
        try:
            text = open(path, "rb").read()
        except OSError as e:
            return {"ok": False, "error": str(e)}
        timeout = float(req.get("timeout") or DEFAULT_CHECK_TIMEOUT)
        # `Set Silent` is decided when the session starts, so asking for the
        # proof's own output means starting over.
        toolchain = (req.get("rocq"),
                     tuple(sorted((req.get("env") or {}).items())))
        entry = self._entry(path, force_cold=bool(req.get("cold")),
                            silent=not bool(req.get("verbose")),
                            toolchain=toolchain)
        with entry.lock:
            if not entry.sess.alive:
                entry.sess.start()
            try:
                result = entry.sess.check(text, timeout=timeout)
            except session_mod.FeedTimeout as e:
                entry.sess.stop()
                self._drop(os.path.abspath(path))
                return {"ok": False, "error": "timed out after %.0fs (%s); "
                                              "session discarded" % (timeout, e)}
            except session_mod.MemoryLimit as e:
                entry.sess.stop()
                self._drop(os.path.abspath(path))
                return {"ok": False,
                        "error": "%s; session discarded (raise the ceiling "
                                 "with ROCQ_WARM_MAX_SESSION_GB)" % e}
            except session_mod.SessionDead as e:
                self._drop(os.path.abspath(path))
                return {"ok": False, "error": "rocq died: %s" % e}
            rss = entry.sess.rss_bytes()
        self._evict()
        return {
            "ok": True,
            "passed": result.ok,
            "mode": result.mode,
            "replayed": result.replayed,
            "sentences": result.total,
            "seconds": result.seconds,
            "rss": rss,
            "diags": [{"kind": d.kind,
                       "span": d.span(text),
                       "message": d.message().decode("utf8", "replace")}
                      for d in result.diags],
        }

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
            })
        return {"ok": True, "pid": os.getpid(), "uptime": time.time() - self.started,
                "budget": self.budget, "min_free": self.min_free,
                "available": _available_bytes(), "sessions": out}

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
