"""`rocq-warm` -- the CLI in front of the warm-session daemon.

Prints diagnostics in `coqc`'s exact format, so `grep Error` and every other
habit built around a build log keeps working, and exits 0/1 the way `coqc`
does.  It does NOT write a `.vo`: this is the edit loop, `make` is still the
source of truth.  `--compile` runs a real `coqc` afterwards when you want both.
"""

import argparse
import json
import shutil
import os
import socket
import subprocess
import sys
import time

from . import diag, project, server


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
    os.makedirs(os.path.join(root, ".rocq-warm"), exist_ok=True)
    with open(os.path.join(root, ".rocq-warm", "log"), "ab") as log:
        subprocess.Popen(
            [sys.executable, "-m", "rocqwarm.server", root],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
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
    resp = request(root, {"cmd": "check", "path": path, "cold": args.cold,
                          "timeout": args.timeout, "verbose": args.show_output})
    if resp is None or not resp.get("ok"):
        sys.stderr.write("rocq-warm: %s\n" % (resp or {}).get("error", "no response"))
        return 2
    text = open(path, "rb").read()
    display = os.path.relpath(path, root)
    if args.json:
        print(json.dumps(resp, indent=2))
    else:
        for d in resp["diags"]:
            span = tuple(d["span"]) if d["span"] else None
            print(diag.render(display, text, span, d["message"].encode()))
    verdict = "OK" if resp["passed"] else "FAILED"
    sys.stderr.write(
        "rocq-warm: %s %s [%s, %d/%d sentences, %.1fs, %.1f GB]\n"
        % (display, verdict, resp["mode"], resp["replayed"], resp["sentences"],
           resp["seconds"], resp["rss"] / 1e9))
    if resp["passed"] and args.compile:
        rc = compile_for_real(path)
        if rc != 0:
            sys.stderr.write("rocq-warm: coqc DISAGREED (exit %d) -- this is a "
                             "bug in rocq-warm, please report it\n" % rc)
            return 3
    return 0 if resp["passed"] else 1


def compile_for_real(path):
    """A real `coqc`, for when you want the .vo as well as the verdict."""
    flags, cwd = project.flags_for(path)
    rel = os.path.relpath(path, cwd)
    return subprocess.call(["coqc", "-q"] + flags + [rel], cwd=cwd)


def cmd_status(args):
    root = os.path.abspath(args.root or workspace_for(os.getcwd()))
    resp = request(root, {"cmd": "status"}, spawn=False)
    if resp is None:
        print("rocq-warm: no daemon running for %s" % root)
        return 0
    print("daemon pid %d, up %.0fs, budget %.1f GB"
          % (resp["pid"], resp["uptime"], resp["budget"] / 1e9))
    for s in resp["sessions"]:
        print("  %-60s %s %4d sentences  %5.1f GB  idle %4.0fs  pid %s"
              % (s["path"], "complete" if s["complete"] else "  parked",
                 s["sentences"], s["rss"] / 1e9, s["idle"], s["pid"]))
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
                   help="on success, also run a real coqc (writes the .vo)")
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
