#!/usr/bin/env python3
"""Run rocq-warm against real proofs from a large development and diff it against `coqc`.

The unit tests use small files so they can run in seconds.  This runs the same
property -- *the warm answer is the cold answer* -- against the files it is
actually meant to speed up: multi-thousand-sentence Iris proofs with notations,
sections, Ltac, `iDestruct ... as "[H1 H2]"` patterns and 13 KB sentences.

For each file it takes `coqc` as the oracle, once per version:

    cold        rocq-warm from scratch: same verdict, same diagnostics, and the
                same sentence map that `coqc -time` reports
    break-late  a tactic near the end replaced with an unbound name
    restore     back to the original -- this is the measurement that matters,
                because it is the shape of the edit loop
    comment     a comment inserted in a gap, which should re-execute nothing
    break-mid   the same break halfway up, where reuse buys least

Usage:
    rocq_warm_corpus.py [--jobs N] [--timeout S] proofs/Big.v ...
    rocq_warm_corpus.py --dir proofs --sample 8 --spread 24

These are minutes-long compiles; on this project run it on the GCP VM
on a big machine, not on your laptop.
"""

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rocqwarm import project, session as session_mod          # noqa: E402
from rocqwarm.diag import render                              # noqa: E402

CHARS_RE = re.compile(r'^Chars (\d+) - (\d+) \[', re.M)
# A whole `-time` line.  The bracketed display is not escaped and Iris tactics
# are full of `]`, so the match has to be greedy and anchored on the trailing
# ` N secs (Nu,Ns)`.
TIME_LINE_RE = re.compile(
    r'^Chars \d+ - \d+ \[.*\] [0-9.]+ secs \([0-9.]+u,[0-9.]+s\)$\n?', re.M)
PROBE = b"rocq_warm_probe_failure."
VERNAC_HEAD = re.compile(
    rb'^\s*(?:Lemma|Theorem|Definition|Fixpoint|Instance|Notation|Ltac|Proof'
    rb'|Qed|Defined|Admitted|Section|End|Module|Require|From|Import|Export'
    rb'|Context|Variable|Hypothesis|Local|Global|Set|Unset|Hint|Arguments)\b')


# `rocq-warm` runs under `Set Silent`, which drops everything that is not a
# diagnostic -- goals, "x is defined", and `Time`'s "Finished transaction in
# ...".  That last one is the only such line `coqc` also prints, so strip it
# before comparing: a timing is not a verdict, and a warm run has no meaningful
# one to give.
CHATTER = re.compile(r'^Finished (failing )?transaction in ')
HEADER = re.compile(r'^File "[^"]*", line \d+, characters -?\d+--?\d+:$')
# Rocq's location for this lexer warning is broken in batch mode too -- `coqc`
# itself prints a NEGATIVE column for it (`characters -203--203`), because its
# `bol` ends up ahead of the position it is reporting.  There is no right
# answer to match, so compare that the warning is there and not where it claims
# to be.  (The warning itself is the `"`-inside-a-comment trap that
# is a classic Rocq trap; several files in a large development carry it.)
UNRELIABLE_LOCATION = "comment-terminator-in-string"


def normalize(text):
    out = []
    for line in text.splitlines():
        if CHATTER.match(line):
            continue
        line = re.sub(r'File "[^"]*/([^"/]+)"', r'File "\1"', line)
        line = re.sub(r'[ \t]+', ' ', line).rstrip()
        if line:
            out.append(line)
    return "\n".join(_blank_unreliable_locations(out))


def _blank_unreliable_locations(lines):
    """Replace the header of any diagnostic Rocq cannot locate properly."""
    starts = [i for i, l in enumerate(lines) if HEADER.match(l)]
    for n, i in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        if any(UNRELIABLE_LOCATION in l for l in lines[i:end]):
            lines[i] = 'File "?", <location unreliable>:'
    return lines


def logical_root(path, flags, cwd):
    """The `-R`/`-Q` logical prefix the file's own directory is bound to."""
    d = os.path.dirname(os.path.abspath(path))
    best = None
    for i, tok in enumerate(flags):
        if tok in ("-R", "-Q") and i + 2 < len(flags):
            phys = os.path.normpath(os.path.join(cwd, flags[i + 1]))
            if (d == phys or d.startswith(phys + os.sep)) and (
                    best is None or len(phys) > len(best[0])):
                best = (phys, flags[i + 2])
    return best[1] if best else None


def run_coqc(path, flags, cwd, out_dir, timeout, attempts=3):
    """(rc, diagnostics, sentence_ranges, seconds) from a cold compile.

    A NEGATIVE return code means the reference compile was killed, not that it
    rejected the proof -- on a shared machine a neighbour's
    `pkill -f rocqworker` takes out every tree's workers (SIGTERM), and reading
    that as "coqc says FAILED" turns somebody else's housekeeping into a
    reported disagreement.  Retry before believing it.
    """
    for attempt in range(attempts):
        rc, diags, ranges, secs = _run_coqc_once(path, flags, cwd, out_dir, timeout)
        if rc >= 0 or attempt == attempts - 1:
            return rc, diags, ranges, secs
        time.sleep(2)
    return rc, diags, ranges, secs


def _run_coqc_once(path, flags, cwd, out_dir, timeout):
    rel = os.path.relpath(path, cwd)
    vo = os.path.join(out_dir, os.path.basename(path)[:-2] + ".vo")
    t0 = time.time()
    proc = subprocess.run(["coqc", "-q", "-time", "-o", vo] + flags + [rel],
                          cwd=cwd, capture_output=True, timeout=timeout)
    secs = time.time() - t0
    out = (proc.stdout + proc.stderr).decode("utf8", "replace")
    ranges = [(int(a), int(b)) for a, b in CHARS_RE.findall(out)]
    diags = normalize(TIME_LINE_RE.sub("", out))
    return proc.returncode, diags, ranges, secs


def pick_tactic(text, ranges, fraction):
    """A sentence around `fraction` through the file that is a tactic.

    Breaking a tactic inside a proof is what an agent's failed edit looks like;
    breaking a `Definition` is not, and would also change what follows it.
    """
    if not ranges:
        return None
    target = int(len(ranges) * fraction)
    for step in range(len(ranges)):
        for i in (target - step, target + step):
            if not 0 <= i < len(ranges):
                continue
            a, b = ranges[i]
            body = text[a:b]
            if not VERNAC_HEAD.match(body) and b - a > 4 and b'"' not in body:
                return i
    return None


def mutations(text, ranges):
    """[(label, bytes, expect_ok)] -- the edit sequence to check."""
    seq = [("cold", text, True)]
    late = pick_tactic(text, ranges, 0.85)
    if late is not None:
        a, b = ranges[late]
        seq.append(("break-late", text[:a] + PROBE + text[b:], False))
        seq.append(("restore", text, True))
    # The last `Qed.` at any indentation -- most of a large development's proofs live
    # inside a `Section`, so anchoring on column zero silently skips them.
    qeds = list(re.finditer(rb'\n[ \t]*Qed\.', text))
    last_qed = qeds[-1].start() if qeds else -1
    if last_qed > 0:
        seq.append(("comment",
                    text[:last_qed] + b"\n(* rocq-warm corpus probe *)"
                    + text[last_qed:], True))
        seq.append(("restore-2", text, True))
    mid = pick_tactic(text, ranges, 0.5)
    if mid is not None:
        a, b = ranges[mid]
        seq.append(("break-mid", text[:a] + PROBE + text[b:], False))
        seq.append(("restore-3", text, True))
    return seq


def check_file(path, timeout, keep_going, rss_limit=None, attempts=2):
    """Check one file, retrying once if the session is killed from outside.

    Both build machines are usually shared, and a neighbour's
    `pkill -f rocqworker` takes out every tree's sessions (SIGTERM, which the
    death note names).  That is not a disagreement with `coqc`, so it is worth
    one retry before it is reported.
    """
    for attempt in range(attempts):
        report = _check_file_once(path, timeout, keep_going, rss_limit)
        killed = [f for f in report["failures"] if "killed by signal" in f]
        if not killed or attempt == attempts - 1:
            if killed:
                report["failures"] = [
                    f + "\n(killed from outside, on every attempt)"
                    if "killed by signal" in f else f
                    for f in report["failures"]]
            return report
    return report


def _check_file_once(path, timeout, keep_going, rss_limit=None):
    path = os.path.abspath(path)
    name = os.path.basename(path)
    flags, cwd = project.flags_for(path)
    # The mutated copies live in a scratch directory, so bind that directory to
    # the same logical prefix the real one has.  Otherwise the reference `coqc`
    # compiles them under a different module name than the session uses, and a
    # message that mentions a qualified name would differ for no real reason.
    root = logical_root(path, flags, cwd)
    text = open(path, "rb").read()
    report = {"file": name, "steps": [], "failures": []}
    out_dir = tempfile.mkdtemp(prefix="rocq-warm-corpus-")
    scratch = os.path.join(out_dir, name)
    scratch_flags = (["-R", out_dir, root] + flags) if root else flags

    rc, cold_diags, ranges, coqc_secs = run_coqc(path, flags, cwd, out_dir, timeout)
    report["coqc_seconds"] = round(coqc_secs, 1)
    report["sentences"] = len(ranges)
    if rc < 0:
        report["failures"].append(
            "the reference coqc was killed by signal %d on every attempt "
            "(a neighbour's pattern-kill, not a verdict); skipping" % -rc)
        return report
    if rc != 0:
        report["failures"].append(
            "the file does not compile cleanly to begin with; skipping:\n"
            + (cold_diags[:1500] or "(coqc said nothing)"))
        return report

    sess = session_mod.Session(path, flags, cwd=cwd, rss_limit=rss_limit)
    sess.start()
    try:
        for label, version, expect_ok in mutations(text, ranges):
            if version is text:
                want_rc, want_diags, want_secs = rc, cold_diags, coqc_secs
            else:
                with open(scratch, "wb") as f:
                    f.write(version)
                want_rc, want_diags, _r, want_secs = run_coqc(
                    scratch, scratch_flags, cwd, out_dir, timeout)
            got = sess.check(version, timeout=timeout)
            got_diags = normalize("\n".join(
                d.render(name, version) for d in got.diags))
            step = {"step": label, "mode": got.mode,
                    "replayed": got.replayed, "sentences": got.total,
                    "rocq-warm_seconds": round(got.seconds, 1),
                    "coqc_seconds": round(want_secs, 1),
                    "ok": got.ok}
            report["steps"].append(step)
            if got.ok != (want_rc == 0):
                report["failures"].append(
                    "%s: rocq-warm says %s, coqc says %s"
                    % (label, "OK" if got.ok else "FAILED",
                       "OK" if want_rc == 0 else "FAILED"))
            elif got_diags != want_diags:
                report["failures"].append(
                    "%s: diagnostics differ\n--- rocq-warm ---\n%s\n--- coqc ---\n%s"
                    % (label, got_diags[:2000], want_diags[:2000]))
            if got.ok != expect_ok and expect_ok is not None:
                step["unexpected_verdict"] = True
            if report["failures"] and not keep_going:
                break
            if label == "cold":
                got_ranges = [(s.start, s.end) for s in sess.sentences]
                if got_ranges != ranges:
                    report["failures"].append(
                        "sentence map differs from coqc -time: %d vs %d "
                        "sentences, first mismatch at %s"
                        % (len(got_ranges), len(ranges),
                           next((i for i, (x, y) in enumerate(
                               zip(got_ranges, ranges)) if x != y), "end")))
        report["rss_gb"] = round(sess.rss_bytes() / 1e9, 1)
    except Exception as e:                                  # noqa: BLE001
        report["failures"].append("%s: %s" % (type(e).__name__, e))
    finally:
        sess.stop()
    return report


def project_files(directory):
    """The proofs a directory's build actually compiles, biggest first.

    Straight from its `_CoqProject`: a source directory usually also holds work
    in progress that is deliberately not in the build and does not compile
    standalone, and sampling those only produces "does not compile to begin
    with".
    """
    root = os.path.abspath(directory)
    listed = project.project_sources(os.path.join(root, "_CoqProject"), root)
    files = [os.path.join(root, f) for f in listed
             if os.path.isfile(os.path.join(root, f))]
    files.sort(key=os.path.getsize, reverse=True)
    return files


def biggest(directory, n):
    """The N biggest proofs, which are the ones this is meant to help."""
    return project_files(directory)[:n]


def spread(directory, n):
    """N files spread across the size distribution.

    The biggest proofs show the speedup; the odd corners -- notation modules,
    section-heavy files, generated catalogues -- are spread through the middle
    of the list, and those are where a sentence map or a backtrack goes wrong.
    """
    files = project_files(directory)
    if n >= len(files):
        return files
    step = len(files) / float(n)
    return [files[int(i * step)] for i in range(n)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--timeout", type=float, default=3600)
    ap.add_argument("--rss-limit-gb", type=float, default=16.0,
                    help="kill a session that grows past this (0 disables). "
                         "An injected failure leaves the tactics still in "
                         "flight running against a broken goal, and one of "
                         "those can be a vm_compute that reaches tens of GB.")
    ap.add_argument("--dir", help="a directory of proofs, for --sample/--spread")
    ap.add_argument("--sample", type=int, default=0,
                    help="check the N largest proofs in --dir")
    ap.add_argument("--spread", type=int, default=0,
                    help="check N proofs from --dir, across the size range")
    ap.add_argument("--keep-going", action="store_true",
                    help="report every failing step, not just the first")
    ap.add_argument("--json")
    args = ap.parse_args()

    files = list(args.files)
    if (args.sample or args.spread) and not args.dir:
        ap.error("--sample/--spread need --dir")
    if args.sample:
        files += biggest(args.dir, args.sample)
    if args.spread:
        files += spread(args.dir, args.spread)
    if not files:
        ap.error("give some .v files, or --sample N")

    reports = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        limit = args.rss_limit_gb * 1e9 if args.rss_limit_gb else None
        futures = {pool.submit(check_file, f, args.timeout, args.keep_going,
                               limit): f for f in files}
        for fut in concurrent.futures.as_completed(futures):
            rep = fut.result()
            reports.append(rep)
            print(format_report(rep), flush=True)

    bad = [r for r in reports if r["failures"]]
    print("\n%d/%d files agreed with coqc at every step"
          % (len(reports) - len(bad), len(reports)))
    # The number that matters is the "restore" step: fixing a broken proof is
    # what the edit loop actually does over and over.
    gains = sorted(s["coqc_seconds"] / max(s["rocq-warm_seconds"], 0.05)
                   for r in reports for s in r["steps"]
                   if s["step"].startswith("restore"))
    if gains:
        print("fixing a proof and re-checking: median %.0fx faster than coqc "
              "(range %.0fx-%.0fx over %d edits)"
              % (gains[len(gains) // 2], gains[0], gains[-1], len(gains)))
    if args.json:
        with open(args.json, "w") as f:
            json.dump(reports, f, indent=2)
    return 1 if bad else 0


def format_report(rep):
    lines = ["=== %s (%d sentences, coqc %.0fs, peak %s GB)"
             % (rep["file"], rep.get("sentences", 0), rep.get("coqc_seconds", 0),
                rep.get("rss_gb", "?"))]
    for s in rep["steps"]:
        speedup = (s["coqc_seconds"] / s["rocq-warm_seconds"]
                   if s["rocq-warm_seconds"] > 0.05 else float("inf"))
        lines.append(
            "    %-12s %-7s rocq-warm %6.1fs  coqc %6.1fs  %5.0fx  "
            "executed %5d of %-5d  proof %s"
            % (s["step"], s["mode"], s["rocq-warm_seconds"], s["coqc_seconds"],
               speedup, s["replayed"], s["sentences"],
               "compiles" if s["ok"] else "rejected"))
    for f in rep["failures"]:
        lines.append("    !! " + f.replace("\n", "\n       "))
    if not rep["failures"]:
        lines.append("    agrees with coqc at every step")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
