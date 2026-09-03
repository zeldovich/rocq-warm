"""`rocq-warm` -- the CLI in front of the warm-session daemon.

Prints diagnostics in `coqc`'s exact format, so `grep Error` and every other
habit built around a build log keeps working, and exits 0/1 the way `coqc`
does.  It does NOT write a `.vo`: this is the edit loop, `make` is still the
source of truth, and a real compile would double the cost of every passing
check.  So a green check says that its `.vo` is now behind, and a check of
anything that requires it is refused until the `.vo` is rebuilt -- by `make`,
by `--compile` on the file, or by `--rebuild` on the dependent.

Exit codes: 0 the file checks, 1 it does not, 2 it could not be checked at
all -- a dependency whose `.vo` is older than its source, no daemon, no rocq
-- and 3 for the one thing that must never happen, a green verdict that a
real `rocq compile` then rejects.
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time

from . import diag, project, server


# The environment variables that decide WHICH Rocq runs and where it looks for
# libraries.  The daemon spawns its sessions with exactly these, taken from the
# client, so a warm session behaves like the shell you invoked from -- not like
# the shell that happened to start the daemon an hour ago.
ROCQ_ENV = ("PATH", "OCAMLPATH", "CAML_LD_LIBRARY_PATH", "OCAMLLIB",
            "COQPATH", "ROCQPATH", "COQLIB", "ROCQLIB", "COQCORELIB")


def rocq_environment():
    """(absolute rocq, the env that resolved it) for this invocation."""
    return (shutil.which("rocq"),
            {k: os.environ[k] for k in ROCQ_ENV if k in os.environ})


def workspace_for(path):
    """Where the daemon for `path` lives.

    The git checkout, when there is one, so that `status` and `stop` find the
    same daemon from anywhere in the tree -- a project can have several
    `_CoqProject` files and one daemon serves them all.
    """
    start = path if os.path.isdir(path) else os.path.dirname(os.path.abspath(path))
    try:
        top = subprocess.run(["git", "-C", start, "rev-parse", "--show-toplevel"],
                             capture_output=True, timeout=30)
        if top.returncode == 0:
            return top.stdout.decode().strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    proj = project.find_project(path)
    if proj:
        return os.path.dirname(proj)
    return start


def connect(root, spawn=True, timeout=30.0):
    sock_path = os.path.join(root, ".rocq-warm", "sock")
    deadline = time.time() + timeout
    spawned = False
    while True:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(sock_path)
            return s
        except (FileNotFoundError, ConnectionRefusedError):
            s.close()
            if not spawn:
                return None
            if not spawned:
                spawn_daemon(root)
                spawned = True
            if time.time() > deadline:
                raise SystemExit("rocq-warm: daemon did not come up at %s" % sock_path)
            time.sleep(0.1)


def spawn_daemon(root):
    """Start the daemon, detached, with an explicit path to our own package.

    Not by cwd: `python -m` finding the package because of where it happens to
    be run from is exactly the kind of thing that breaks when someone moves the
    checkout or symlinks the entry point.
    """
    os.makedirs(os.path.join(root, ".rocq-warm"), exist_ok=True)
    package_parent = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    env = dict(os.environ)
    env["PYTHONPATH"] = (package_parent + os.pathsep + env["PYTHONPATH"]
                         if env.get("PYTHONPATH") else package_parent)
    with open(os.path.join(root, ".rocq-warm", "log"), "ab") as log:
        subprocess.Popen(
            [sys.executable, "-m", "rocqwarm.server", root],
            cwd=package_parent, env=env,
            stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            start_new_session=True)


def request(root, msg, spawn=True):
    sock = connect(root, spawn=spawn)
    if sock is None:
        return None
    try:
        server.send_msg(sock, msg)
        return server.recv_msg(sock)
    finally:
        sock.close()


def cmd_check(args):
    path = os.path.abspath(args.file)
    if not os.path.isfile(path):
        raise SystemExit("rocq-warm: no such file: %s" % path)
    root = workspace_for(path)
    rocq, env = rocq_environment()
    display = os.path.relpath(path, root)
    resp = request(root, {"cmd": "check", "path": path, "cold": args.cold,
                          "timeout": args.timeout, "verbose": args.show_output,
                          "rocq": rocq, "env": env,
                          "allow_stale": args.allow_stale,
                          "rebuild": args.rebuild,
                          "wait_vo": args.compile})
    if resp is None:
        sys.stderr.write("rocq-warm: no response\n")
        return 2
    if not resp.get("ok"):
        return report_refusal(resp, display, root)
    text = open(path, "rb").read()
    if args.json:
        print(json.dumps(resp, indent=2))
    else:
        for d in resp["diags"]:
            span = tuple(d["span"]) if d["span"] else None
            print(diag.render(display, text, span, d["message"].encode()))
    for row in resp.get("stale") or ():
        sys.stderr.write("rocq-warm: warning: checking against a stale "
                         "dependency: %s\n" % relativize(row["why"], root))
    if resp.get("note"):
        sys.stderr.write("rocq-warm: warning: %s\n" % resp["note"])
    verdict = "OK" if resp["passed"] else "FAILED"
    vo = resp.get("vo")
    tail = "; wrote %s.vo" % display[:-2] if vo and vo["state"] == "ok" else ""
    sys.stderr.write(
        "rocq-warm: %s %s [%s, %d/%d sentences, %.1fs, %.1f GB]%s\n"
        % (display, verdict, resp["mode"], resp["replayed"], resp["sentences"],
           resp["seconds"], resp["rss"] / 1e9, tail))
    if resp["passed"] and not args.compile and resp.get("vo_stale"):
        # The thing that bites: the file is green, and everything that
        # requires it is still reading the .vo from before the edit.
        sys.stderr.write(
            "rocq-warm: warning: %s.vo was NOT regenerated (%s); anything "
            "that requires it is refused until it is rebuilt -- run make, or "
            "`rocq-warm check %s --compile`\n"
            % (display[:-2], relativize(resp["vo_stale"], root), display))
    if resp["passed"] and args.compile:
        if vo is None or vo["state"] != "ok":
            sys.stdout.write(vo["output"] if vo else "")
            if vo is not None and vo["state"] == "failed":
                sys.stderr.write("rocq-warm: rocq compile DISAGREED (exit %s) "
                                 "-- this is a bug in rocq-warm, please "
                                 "report it\n" % vo["rc"])
                return 3
            sys.stderr.write("rocq-warm: the .vo was not written (%s)\n"
                             % (vo["state"] if vo else "no compile was run"))
            return 2
    return 0 if resp["passed"] else 1


def relativize(text, root):
    return text.replace(root + os.sep, "")


def report_refusal(resp, display, root):
    """A check that did not happen, and exactly why.  Exit 2, never 1: this
    is not a verdict about the proof."""
    stale = resp.get("stale")
    if stale:
        sys.stderr.write(
            "rocq-warm: %s NOT CHECKED -- %d dependenc%s stale (make would "
            "rebuild %s):\n" % (display, len(stale),
                                 "y is" if len(stale) == 1 else "ies are",
                                 "it" if len(stale) == 1 else "them"))
        for row in stale:
            sys.stderr.write("  %s\n" % relativize(row["why"], root))
            if row.get("compile_output"):
                sys.stdout.write(row["compile_output"])
        sys.stderr.write(
            "rocq-warm: rebuild %s first, or pass --rebuild to have rocq-warm "
            "compile %s, or --allow-stale to check against %s anyway\n"
            % (("it", "it", "it") if len(stale) == 1 else
               ("them", "them", "them")))
        return 2
    for job in resp.get("compile_failed") or ():
        sys.stdout.write(job["output"])
        sys.stderr.write("rocq-warm: rebuilding %s FAILED (%s)\n"
                         % (relativize(job["path"], root), job["why"]))
    sys.stderr.write("rocq-warm: %s\n" % resp.get("error", "no response"))
    return 2


def cmd_status(args):
    root = os.path.abspath(args.root or workspace_for(os.getcwd()))
    resp = request(root, {"cmd": "status"}, spawn=False)
    if resp is None:
        print("rocq-warm: no daemon running for %s" % root)
        return 0
    avail = resp.get("available")
    print("daemon pid %d, up %.0fs, budget %.1f GB%s"
          % (resp["pid"], resp["uptime"], resp["budget"] / 1e9,
             "" if avail is None else
             "; machine has %.1f GB free, yields below %.1f"
             % (avail / 1e9, resp.get("min_free", 0) / 1e9)))
    for s in resp["sessions"]:
        print("  %-60s %s %4d sentences  %5.1f GB  idle %4.0fs  %4d .vo watched  pid %s"
              % (s["path"], "complete" if s["complete"] else "  parked",
                 s["sentences"], s["rss"] / 1e9, s["idle"],
                 s.get("watched", 0), s["pid"]))
    for j in resp.get("compiles") or ():
        print("  compile %-52s %-9s %5.0fs%s"
              % (j["path"], j["state"], j["seconds"],
                 "  " + j["why"] if j.get("why") else ""))
    return 0


def cmd_stop(args):
    root = os.path.abspath(args.root or workspace_for(os.getcwd()))
    resp = request(root, {"cmd": "stop"}, spawn=False)
    print("rocq-warm: %s" % ("stopped" if resp else "no daemon running"))
    return 0


def main(argv=None):
    if shutil.which("rocq") is None:
        raise SystemExit("rocq-warm: no `rocq` on PATH -- "
                         "eval $(opam env)")
    ap = argparse.ArgumentParser(prog="rocq-warm")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="check a .v file, reusing a warm session")
    c.add_argument("file")
    c.add_argument("--cold", action="store_true",
                   help="discard any warm session first")
    c.add_argument("--timeout", type=float, default=1800.0)
    c.add_argument("--json", action="store_true")
    c.add_argument("--compile", action="store_true",
                   help="on success, also run a real rocq compile (writes the "
                        ".vo; exit 3 if it disagrees)")
    c.add_argument("--rebuild", action="store_true",
                   help="compile stale dependencies (and what depends on "
                        "them) before checking, instead of refusing")
    c.add_argument("--allow-stale", action="store_true",
                   help="check even if a dependency's .vo is older than its "
                        "source (the verdict is then about the OLD library)")
    c.add_argument("--show-output", action="store_true",
                   help="also print what the proof itself prints (goals, Time, "
                        "Print Assumptions); much slower on Iris proofs")
    c.set_defaults(func=cmd_check)

    s = sub.add_parser("status", help="what the daemon is holding")
    s.add_argument("--root")
    s.set_defaults(func=cmd_status)

    k = sub.add_parser("stop", help="stop the daemon and free its sessions")
    k.add_argument("--root")
    k.set_defaults(func=cmd_stop)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
