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


def _session_ceiling(budget, max_sessions):
    env = os.environ.get("ROCQ_WARM_MAX_SESSION_GB")
    if env:
        return float(env) * 1e9
    return budget / max(max_sessions, 1)


def _budget_bytes():
    env = os.environ.get("ROCQ_WARM_MAX_RSS_GB")
    if env:
        return float(env) * 1e9
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError):
        return 32e9
    return total * 0.5


class Entry:
    def __init__(self, sess, flags, cwd, deps, graph):
        self.sess = sess
        self.flags = flags
        self.cwd = cwd
        self.deps = deps
        self.fingerprint = project.fingerprint(deps)
        self.graph = graph
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
        self.budget = _budget_bytes()
        self.started = time.time()

    # -------------------------------------------------------------- sessions

    def _entry(self, path, force_cold=False, silent=True):
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
            elif (force_cold or not entry.sess.alive
                  or entry.sess.silent != silent):
                self._drop(key)
                entry = None
        if entry is None:
            proj = project.find_project(key)
            graph = project.dep_graph(
                flags, cwd, project.project_sources(proj, cwd))
            deps = project.closure(key, flags, cwd, graph) or []
            sess = session_mod.Session(
                key, flags, cwd=cwd, silent=silent,
                rss_limit=_session_ceiling(self.budget, self.max_sessions))
            entry = Entry(sess, flags, cwd, deps, graph)
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
        """Keep the session set inside the count and memory budgets."""
        while True:
            with self.lock:
                items = sorted(self.sessions.items(), key=lambda kv: kv[1].last_used)
                if not items:
                    return
                used = sum(e.sess.rss_bytes() for _k, e in items)
                over = len(items) > self.max_sessions or used > self.budget
                if not over or len(items) == 1:
                    return
                victim = items[0][0]
            self._drop(victim)

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
        entry = self._entry(path, force_cold=bool(req.get("cold")),
                            silent=not bool(req.get("verbose")))
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
                "budget": self.budget, "sessions": out}

    # ----------------------------------------------------------------- serve

    def serve(self):
        os.makedirs(self.dir, exist_ok=True)
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
