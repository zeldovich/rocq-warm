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
build system stays the source of truth. A real compile would double the cost
of every passing check, so a green check tells you instead that the `.vo` is
now behind:

```console
$ rocq-warm check proofs/FsReady.v
rocq-warm: proofs/FsReady.v OK [replay, 41/2210 sentences, 3.1s, 2.9 GB]
rocq-warm: warning: proofs/FsReady.vo was NOT regenerated (proofs/FsReady.v is newer than proofs/FsReady.vo); anything that requires it is refused until it is rebuilt -- run make, or `rocq-warm check proofs/FsReady.v --compile`
```

and a check of anything that requires a stale library is **refused** rather
than answered from the old `.vo`:

```console
$ rocq-warm check proofs/FirstTok.v
rocq-warm: proofs/FirstTok.v NOT CHECKED -- 1 dependency is stale (make would rebuild it):
  proofs/FsReady.v is newer than proofs/FsReady.vo
rocq-warm: rebuild it first, or pass --rebuild to have rocq-warm compile it, or --allow-stale to check against it anyway
```

That is exit code 2 -- not a verdict about the proof. Without it, a proof
checked against the pre-edit library fails for a reason that looks exactly
like a proof error, or passes when it should not, and nothing says so until a
real `make`. "Stale" is `make`'s own rule: a `.vo` that is missing, older than
its `.v`, or older than a `.vo` it requires, anywhere in the file's transitive
closure.

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
rocq-warm check FILE.v --compile        # on success, also write the .vo (a real rocq compile)
rocq-warm check FILE.v --rebuild        # first compile any stale dependency, in order
rocq-warm check FILE.v --allow-stale    # check against stale dependencies anyway (warns)
rocq-warm check FILE.v --show-output    # also print what the proof prints
rocq-warm status                # what the daemon is holding
rocq-warm stop                  # free the sessions
```

Exit codes: 0 the file checks, 1 it does not, 2 it could not be checked (a
stale dependency, no daemon, no `rocq`), 3 a green verdict that a real
`rocq compile` then rejected -- a bug in rocq-warm, please report it.

`--compile` and `--rebuild` run the build's own step -- `rocq compile` with
the flags from the nearest `_CoqProject`, writing the `.vo` where `make`
writes it -- and nothing else. A check in another terminal that finds a `.vo`
stale because one of these compiles is still running waits for it instead of
refusing. `--rebuild` compiles everything stale in the closure plus everything
in the closure that depends on it, in dependency order, `ROCQ_WARM_COMPILE_JOBS`
(default 2) at a time; a rebuild that fails prints that file's errors and
exits 2, leaving its old `.vo` exactly as `make` would.

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

A session is kept until: the build flags change; any `.vo` it has loaded
changes on disk (the set is what Rocq itself reports having loaded -- the
standard library and installed packages included, and a `Require` added by a
later edit); a `.vo` changes *while* a check is running, in which case the
verdict is reported and the session dropped; the
`rocq` you invoke with changes; `--cold` or `--show-output` is passed; it
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

### Several checkouts on one machine

A daemon is per-checkout, so agents working in separate trees do not share
anything: separate sockets under their own `.rocq-warm/`, separate sessions,
and `stop` in one leaves the others alone. `reap_strays` matches on cmdline as
well as pid for the same reason — a neighbouring checkout's sessions are never
in scope. Nothing here pattern-kills.

What does NOT scale by itself is memory, so the defaults assume the machine is
shared. One daemon's budget is capped rather than being a share of RAM, and
eviction also watches `MemAvailable`: when the machine as a whole is running
low, a daemon gives up its least-recently-used session — its own last one, if
it comes to that. Degrading to a cold check is a cost you pay yourself; an OOM
kill is a cost somebody else pays.

| variable | default |
|---|---|
| `ROCQ_WARM_MAX_RSS_GB` | this daemon's sessions together: `min(half of RAM, 32 GB)` |
| `ROCQ_WARM_MAX_SESSION_GB` | half the budget — enough for a big proof, not for a runaway |
| `ROCQ_WARM_MIN_FREE_GB` | `max(4 GB, 5% of RAM)` left free for everyone else |
| `ROCQ_WARM_COMPILE_JOBS` | 2 -- `--compile`/`--rebuild` compiles at once; none start below the free-memory floor |
| sessions per daemon | 4, LRU |

`rocq-warm status` shows what is resident and how close the machine is to the
floor. A session costs roughly twice what `coqc` peaks at for the same file,
because the state machine keeps a state per sentence.

## Tests

```console
$ make test
Ran 140 tests in 231s
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
- `test_rocq_warm_staleness.py` — every way a session could answer for a
  library that no longer exists: a `.vo` older than its source, transitively,
  under another `-R` root, half-rebuilt, added by a `Require` mid-session,
  rebuilt during a check; and what `--compile`, `--rebuild` and
  `--allow-stale` do about it. Each drives the real CLI, because the point
  is what a *second* process sees.
- `test_rocq_warm_compile.py` — the on-request compiler: same outputs as
  `make`, stamped so an edit during the compile shows, cancelled and cleaned
  up when the text changes, dependencies before dependents.
- `test_rocq_warm_project.py`, `test_rocq_warm_daemon.py`,
  `test_rocq_warm_robustness.py`.

The same suite runs in GitHub Actions on every push, inside the official
Rocq images, once per supported Rocq version (`.github/workflows/test.yml`).

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
