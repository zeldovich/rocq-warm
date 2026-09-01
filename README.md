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

## Install

There is nothing to build. Put the entry point on your `PATH`; symlinking is
supported, and it finds its package through the link:

```sh
git clone https://github.com/zeldovich/rocq-warm ~/src/rocq-warm
ln -s ~/src/rocq-warm/rocq-warm ~/bin/rocq-warm
```

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

**It uses the Rocq your shell resolves**, not a configured one, so activate
your switch the way you would for a raw `coqc`:

```sh
eval $(opam env --switch=/path/to/switch)
rocq-warm check proofs/Big.v
```

A call from a different switch cold-starts rather than answering from the one
the daemon happened to warm up with.

## How it works, briefly

`rocq repl -emacs -q -time`, driven over a pipe. `-time` makes Rocq report each
sentence's byte range from its own parser, so there is no hand-written Coq
lexer to get wrong; `-emacs` gives a state id per sentence, and a failed
sentence does not advance it; `BackTo` restores the whole system state,
including the parser after a `Notation`. An edit is located by common byte
prefix, the session backtracks to the last unaffected sentence, and the rest is
replayed.

**[DESIGN.md](DESIGN.md) has the rest** — what the message offsets are actually
anchored on, why the look-ahead window is sized the way it is, what happens to
the tactics behind a failure, and how sessions are keyed and collected.

## What stays running

One daemon per workspace (the git checkout, else the `_CoqProject` directory),
on `<root>/.rocq-warm/sock`. Under it, one `rocq repl` session per `.v` file.

A session is kept until: the build flags change; a `.vo` it depends on changes;
the `rocq` you invoke with changes; `--cold` or `--show-output` is passed; it
falls out of the LRU under the session-count or memory budget; it goes
untouched for the idle timeout; it exceeds its own RSS ceiling mid-check; or
the check exceeds its wall timeout.
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
Ran 82 tests in 119s
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
