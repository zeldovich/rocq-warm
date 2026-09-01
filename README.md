# rocq-warm

A big Rocq proof file costs minutes to `coqc`, and fixing one tactic in it
costs that again for every attempt. `rocq-warm` keeps a `rocq repl` alive with
the file already executed, so an edit re-executes only from the edit onwards.

```console
$ rocq-warm check proofs/Big.v
rocq-warm: proofs/Big.v OK [cold, 3671/3671 sentences, 102.4s, 4.3 GB]

$ # break a tactic 80% of the way in, then fix it
$ rocq-warm check proofs/Big.v
File "proofs/Big.v", line 2914, characters 4-23:
Error: The reference foo was not found in the current environment.
rocq-warm: proofs/Big.v FAILED [replay, 12/2758 sentences, 0.4s, 4.3 GB]

$ rocq-warm check proofs/Big.v
rocq-warm: proofs/Big.v OK [replay, 735/3671 sentences, 24.7s, 4.3 GB]
```

Diagnostics come out in `coqc`'s exact format and the exit code is `coqc`'s, so
whatever you already grep for keeps working.

It is an **edit-loop tool, not a build tool**: it writes no `.vo`, and your
build system stays the source of truth. `--compile` runs a real `coqc`
afterwards when you want both.

## Requirements

Rocq 9.x with `rocq` and `coqc` on `PATH`, Python 3.8+, Linux (it reads
`/proc` to tell a stuck tactic from a session waiting for input). No other
dependencies, and nothing to install into your opam switch.

## Use

```console
rocq-warm check FILE.v          # check it; exit 0 if it compiles, 1 if not
rocq-warm check FILE.v --cold   # ignore any warm session
rocq-warm check FILE.v --compile        # also run a real coqc on success
rocq-warm check FILE.v --show-output    # also print what the proof prints
rocq-warm status                # what the daemon is holding
rocq-warm stop                  # free the sessions
```

Load path and flags come from the nearest `_CoqProject`.

## How it works

`rocq repl -emacs -q -time`, driven over a pipe. Four properties of that stream
carry the whole design, and the test suite re-derives each from a live Rocq
rather than assuming it:

1. **`-time` makes Rocq split the sentences for us.** Every executed sentence
   prints `Chars A - B [display] N secs (Nu,Ns)` with **byte** offsets into the
   stdin stream, and the counter runs continuously across separate writes. That
   removes the biggest correctness risk in a tool like this: a hand-written Coq
   lexer that has to get nested comments, `*)` inside a string, `..` in
   recursive notations, bullets and brace-sentences right.
2. **`-emacs` gives one state id per sentence**, for every vernac kind —
   queries, `Set`, `Section`/`Module`, bullets, `Fail`, `Qed`.
3. **A failed sentence does not advance the state id.** That is the verdict
   signal; grepping the output for `Error:` is not, because a proof's own
   `idtac` can print anything. A *parse* error is the one case that emits no
   `Chars` line at all.
4. **`BackTo <id>` restores the whole system state** — verified for `Require`,
   `Notation` (the *parser* is restored), `Ltac`, and `Set`/`Unset`. That is
   what makes the prefix genuinely reusable.

`Set Silent.` is not cosmetic: printing a full proof goal after each of a few
thousand sentences costs more than the proof does, and without it the REPL runs
about 3x slower than `coqc`.

### Deciding what to re-execute

1. **Invalidate?** Any dependency `.vo` rebuilt, or the `_CoqProject` flags
   changed, and the session is thrown away. The dependency set is the
   transitive closure from one `rocq dep` run over the project.
2. Find the first sentence the edit could have touched, by common byte prefix.
3. A **whitespace/comment-only edit** inside one gap shifts the remaining
   offsets and executes nothing.
4. **Undoing a `Require`** forces a cold start. Adding one below the existing
   ones replays fine.
5. Otherwise `BackTo` the state after the last unaffected sentence and feed
   from there.
6. **Stop at the first failing sentence** and park the session there, so the
   next edit — the fix for it — replays from exactly that point.

## Three things that look like bugs and are not

**The look-ahead window.** Rocq is fed at most `write_ahead` bytes past what it
reports having parsed, so an error stops the feed instead of letting Rocq
re-prove the rest of the file behind it. The window must exceed the largest
single sentence — real developments have 13 KB ones — or the feed deadlocks,
because Rocq cannot report a sentence it has not finished reading. It is sized
from the largest sentence the file has actually shown us, and widens itself
when that is not enough.

**Cutting a feed short leaves Rocq mid-sentence**, and a bare `.` is not a
terminator inside a comment or a string. `Session.lex_state` scans what was
actually written and emits exactly what has to be closed first.

**Message offsets are relative to neither the sentence nor the line the error
is on.** `Toplevel input, characters A-B` is measured from wherever Rocq landed
after skipping the whitespace following the previous sentence's `.`:

- whitespace then a newline: the anchor is just past that **first** newline, so
  further blank lines, indentation and whole comment blocks before the sentence
  all count into A;
- anything else first — **a trailing comment on the previous line**, or a
  second sentence on the same line — and the anchor stays on the *previous*
  sentence's line.

The second case is not exotic. `Require Import Foo.   (* ... *)` is ordinary
style, and treating it like the first shifts every column on the following
sentence by the width of the comment.

## A failing tactic makes the tactics behind it expensive

Rocq keeps executing whatever input it already has when a sentence fails, and
those tactics now run against a goal of the wrong shape — which is how a
`vm_compute` ends up on a free variable and eats tens of GB. `coqc` never gets
there, because it stops at the first error. On one real file (70
`vm_compute`/`native_compute` calls in 489 lines) a broken tactic mid-file took
**tens of minutes and 35 GB**; with the interrupt below it takes **5.2 s and
1.0 GB**.

Once a sentence has failed, any command that keeps burning CPU without
finishing and without printing anything gets a SIGINT, which Rocq turns into
`Error: User interrupt.` and carries on from. **That predicate is not
optional**: Rocq only protects itself from `Sys.Break` while it is *executing*,
and a signal that lands while it is reading input, printing a prompt, or
formatting a large goal kills the process outright.

## What stays running

One daemon per workspace (the git checkout, else the `_CoqProject` directory),
on `<root>/.rocq-warm/sock`. Under it, one `rocq repl` session per `.v` file.

A session is kept until: the build flags change; a `.vo` it depends on changes;
`--cold` or `--show-output` is passed; it falls out of the LRU under the
session-count or memory budget; it goes untouched for the idle timeout; it
exceeds its own RSS ceiling mid-check; or the check exceeds its wall timeout.
The daemon exits once it has held nothing for the idle timeout, or as soon as
its workspace directory disappears.

Killing the daemon does not strand its children, and not because anything reaps
them: the daemon holds the only writer on each child's stdin, so its death is
an EOF and Rocq exits on its own. The exception is a child that is *busy* — it
will not read stdin, so a long `vm_compute` can outlive the daemon. For that
case the daemon records its sessions and the next daemon kills what the last
one left, matching on pid **and** cmdline (pids get recycled, and on a shared
machine a pattern kill takes out other checkouts' sessions too).

| variable | default |
|---|---|
| `ROCQ_WARM_MAX_RSS_GB` | half of RAM, across all sessions |
| `ROCQ_WARM_MAX_SESSION_GB` | the global budget divided by the session limit |

A session costs roughly twice what `coqc` peaks at for the same file, because
the state machine keeps a state per sentence.

## Tests

```console
$ make test
Ran 80 tests in 117s
OK
```

Every test drives a real `rocq` and diffs the answer against a real `coqc`. A
mock would only prove the code agrees with our beliefs about Rocq rather than
with Rocq.

- `test_rocq_warm_protocol.py` — the stream parser, and the sentence map
  compared against `coqc -time` on the constructs that break naive splitters.
- `test_rocq_warm_diag.py` — every diagnostic-location shape, against `coqc`.
- `test_rocq_warm_equivalence.py` — the property that matters: a sequence of
  edits, with `coqc` consulted after every one.
- `test_rocq_warm_recovery.py` — stopping a feed early, and the lexical
  recovery that lets it resume.
- `test_rocq_warm_project.py`, `test_rocq_warm_daemon.py`,
  `test_rocq_warm_robustness.py`.

`tests/rocq_warm_corpus.py` runs the same equivalence property against real
proofs — `coqc` as the oracle, once per edited version:

```console
$ tests/rocq_warm_corpus.py --dir proofs --jobs 6 --spread 24
=== Big.v (3671 sentences, coqc 102s, peak 4.3 GB)
    cold        cold    rocq-warm 101.9s  coqc 102.4s     1x  executed 3671 of 3671  proof compiles
    break-late  replay  rocq-warm   1.4s  coqc  84.1s    60x  executed  199 of 3120  proof rejected
    restore     replay  rocq-warm  24.7s  coqc 102.4s     4x  executed  735 of 3671  proof compiles
    comment     shift   rocq-warm   0.0s  coqc 101.7s   infx  executed    0 of 3671  proof compiles
    agrees with coqc at every step
```

Those are minutes-long compiles: run it on a big machine, and give it
`--rss-limit-gb` room.

### The one place it cannot match `coqc`

The `comment-terminator-in-string` warning (a `"` inside a comment) is reported
at a location Rocq computes wrongly in batch mode too — `coqc` itself prints a
**negative** column for it. There is no right answer to reproduce, so
`rocq-warm` reports it at its own reading and the corpus runner compares that
warning's presence but not its location. Everything else matches `coqc` byte
for byte.

## Status

Developed against Rocq 9.0.1 on a large Iris development (~1500 files, proofs
up to 7242 sentences). Checked against `coqc` on 18 medium proofs and a spread
of 32 larger ones; the numbers above are measured, not estimated.
