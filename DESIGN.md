# How `rocq-warm` works

Usage is in [README.md](README.md); this is the inside.

## The substrate

`rocq repl -emacs -q -time`, driven over a pipe. Not `coqidetop`/the XML
protocol — which on some installs is simply broken (`ltac_plugin.cmxs:
undefined symbol: camlKeys__constr_key_…`, `coqide-server` and the
`rocq-runtime` plugins out of sync) — and not an LSP server, which would be a
dependency to install into the switch.

Four properties of that stream carry the whole design. Each is re-derived from
a live Rocq by the test suite, never assumed:

1. **`-time` makes Rocq split the sentences for us.** Every executed sentence
   prints `Chars A - B [display] N secs (Nu,Ns)` with **byte** offsets into the
   stdin stream, and the counter runs continuously across separate writes.
   That removes the biggest correctness risk in a tool like this: a
   hand-written Coq lexer that has to get nested comments, `*)` inside a
   string, `..` in recursive notations, bullets and brace-sentences right.
2. **`-emacs` gives one state id per sentence** —
   `<prompt>name < 14 |stack| 0 < </prompt>` — for every vernac kind: queries,
   `Set`, `Section`/`Module`, bullets, `Fail`, `Qed`.
3. **A failed sentence does not advance the state id.** That is the verdict
   signal. Grepping the output for `Error:` is not: a proof's own `idtac` can
   print anything. A *parse* error is the one case that emits no `Chars` line
   at all — Rocq reports it, skips to the next `.`, and carries on.
4. **`BackTo <id>` restores the whole system state** — verified for `Require`
   (the names go away again), `Notation` (the *parser* is restored), `Ltac`,
   and `Set`/`Unset`. That is what makes the prefix genuinely reusable rather
   than merely "probably fine".

`Set Silent.` is not cosmetic. Printing a full proof goal after each of a few
thousand sentences costs more than the proof does; without it the REPL runs
about 3x slower than `coqc`. It leaves the `Chars` lines, the prompts, warnings
and errors alone — but it does drop everything the proof itself prints, which
is why `--show-output` exists and why it starts a fresh session.

One parsing detail that bites: the bracketed display is **not escaped**, and
proof scripts are full of `]` (`iDestruct ... as "[H1 H2]"`). The `Chars`
pattern must be greedy and anchored on the trailing ` N secs (Nu,Ns)`. Getting
that wrong silently drops those sentences from the parse-progress signal and
deadlocks the feed. Rocq also writes the `Chars` line straight after
`</prompt>`, on the same line, so progress tracking on the raw stream cannot be
`^`-anchored.

## Deciding what to re-execute

1. **Refuse?** Any `.vo` in the file's transitive closure that `make` would
   rebuild — missing, older than its `.v`, older than a `.vo` it requires —
   is not checked against at all (exit 2, naming it). See *Stale libraries*.
2. **Invalidate?** Any `.vo` the session has loaded rebuilt, the
   `_CoqProject` flags changed, or a different `rocq` — throw the session away.
   The loaded set is what Rocq reports, not what `rocq dep` predicts.
3. Find the first sentence the edit could have touched, by common byte prefix.
4. A **whitespace/comment-only edit** inside one gap shifts the stored offsets
   and executes nothing.
5. **Undoing a `Require`** (or `Declare ML Module`/`Load`) forces a cold start.
   Adding one below the existing ones replays fine.
6. Otherwise `BackTo` the state after the last unaffected sentence, and feed
   from there.
7. **Stop at the first failing sentence** and park the session there, so the
   next edit — the fix for it — replays from exactly that point.

In the shift case the stored anchor moves with the text. It is not a semantic
position there but the origin the CACHED message offsets were measured from, so
it has to travel with the text they point into; recomputing it from the new gap
leaves those offsets short by the gap's change in width.

## Stale libraries

A warm session that answers for a library that no longer exists is worse than
no session, and there are two distinct ways to get one. Both happened in the
field before this section was written, and they need different cures.

**The `.vo` on disk was rebuilt under the session.** The cache-invalidation
case. The session loaded `FsReady.vo` an hour ago; `make` replaced it; every
answer since is about the old one. The cure is a fingerprint — `(mtime_ns,
size)` per file — of everything the session loaded, compared before every
check, and the whole question is *which files*. The first version used the
`rocq dep` closure computed at cold start, which is wrong three ways, each of
which was found by looking rather than by reasoning:

- `rocq dep` is only run over the files a `_CoqProject` lists, and
  `-R ../model Riscv` makes a whole other tree loadable. Files in that tree
  appear in the graph as edges but never as nodes, so nothing beneath them is
  watched. On one real tree 7 of a file's 232 closure members had no entry.
- `rocq dep` never lists the standard library or anything installed in the
  switch. `opam upgrade` replaces those in place.
- The closure was computed once. A `Require` added by a later edit replays
  fine — and loads a library nobody had written down.

So the set watched is now **what Rocq says it loaded**: after every check the
session is asked `Print Libraries.` and, for any name not yet mapped, `Locate
Library X.`, which prints the physical `.vo`. Both work under `Set Silent`,
inside a proof, and cost tens of milliseconds for a few hundred libraries; the
queries are undone with `BackTo` so the session is parked exactly where it
was. The `rocq dep` closure is still unioned in, so nothing is lost if the
answer is ever short.

One race inside that: the fingerprint of a file must be the one *as loaded*.
A `.vo` rebuilt while Rocq is busy with a long proof would, if stat'ed after
the check, be recorded with its new mtime as if that were what got loaded.
So everything known in advance is stat'ed before the session loads anything;
after the check, a file whose stat moved means the verdict may be about
either version — it is reported as such, and the session is dropped.

**The `.vo` on disk is itself stale.** The case that confused people. A file
is edited and warm-checked green; its dependents are checked next, against
the `.vo` `make` produced *before* the edit. Nothing here is a cache problem —
a cold `coqc` loads the same old `.vo` — the problem is that a tool that just
said "OK" about the edit did not make the edit visible to anything else, and
the false failures ("remaining open goals" for a conjunct that no longer
exists) and false passes that follow look exactly like proof results. The
cure is not to compile the `.vo` — that would double the cost of every green
check, which is the cost the tool exists to remove — but to make the
situation impossible to mistake:

- a green check says, on stderr, that its `.vo` was **not** regenerated,
  whenever `make` would now rebuild it;
- a check of any file is **refused** (exit 2, not 1, naming the file and the
  reason) when anything in its closure is something `make` would rebuild:
  missing, older than its `.v`, or older than a `.vo` it requires. Make's
  rule, verbatim, because it is the rule the build applies and the one users
  already reason with; equal mtimes are current.

`--compile` and `--rebuild` then run the build's own step on request —
`rocq compile` with the project's flags, writing where `make` writes — and a
check that finds a `.vo` stale because such a compile is still running waits
for it. The finished `.vo` is stamped with the time the compile *started*, so
an edit that lands mid-compile leaves the `.v` newer and the rule still
fires; a cancelled or failed compile removes whatever it wrote, because a
truncated `.vo` with a fresh mtime is indistinguishable from a good one to
every mtime rule there is.

The trap inside that, and the one place a naive version writes a wrong `.vo`:
**`rocq compile` reads the `.v` from disk, not from us.** A job queued for the
text a check just approved can start running after the file has already become
something else, and would then compile *that* under the approved job's name —
a `.vo` of text nobody checked. So a job carries the digest of the text it was
asked to compile, refuses to start if the file no longer hashes to it (a newer
job covers the newer text), and discards its output if the file changes while
it runs. Found by a flake in exactly this test: a slow proof's compile
finished in 0.1s because the file it was handed had been overwritten with a
one-line version in between.

Two `rocq dep` facts the graph code has to know: it prints **nothing at all**
when any file it is handed does not exist (and `_CoqProject` files routinely
list generated sources that are not there yet), and a full run over a large
tree costs over a second, so the graph is kept incrementally — every source is
stat'ed on each check, only the ones whose mtime moved are re-run, which is
always at least the file being edited.

## Three things that look like bugs and are not

**The look-ahead window.** Rocq is fed at most `write_ahead` bytes past what it
reports having parsed, so an error stops the feed instead of letting Rocq
re-prove the rest of the file behind it. The window must exceed the largest
single sentence — real developments have 13 KB ones — or the feed deadlocks,
because Rocq cannot report a sentence it has not finished reading. It is sized
from the largest sentence the file has actually shown us, and widens itself
when that turns out to be too small. It does not ratchet: everything in the
window still executes when a sentence fails, so the smallest window that cannot
deadlock is the one we want.

**Cutting a feed short leaves Rocq mid-sentence**, and a bare `.` is not a
terminator inside a comment or a string. `Session.lex_state` scans what was
actually written and emits exactly what has to be closed first. The same
recovery runs when the *file* ends inside a comment or string and swallows
the sentinel: without it the next thing written — a `BackTo`, a query — lands
inside the comment too, and the session is lost for a check that was never
going to pass.

**Message offsets are relative to neither the sentence nor the line the error
is on.** `Toplevel input, characters A-B` is measured from wherever Rocq landed
after skipping the whitespace following the previous sentence's `.`:

- whitespace then a newline → the anchor is just past that **first** newline, so
  further blank lines, indentation and whole comment blocks before the sentence
  all count into A;
- anything else first — **a trailing comment on the previous line**, or a
  second sentence on the same line — and the anchor stays on the *previous*
  sentence's line.

The second case is not exotic: `Require Import Foo.   (* … *)` is ordinary
style, and treating it like the first shifts every column on the following
sentence by the width of the comment. It was found by diffing against `coqc`
over real files, not by reading the code.

## A failing tactic makes the tactics behind it expensive

Rocq keeps executing whatever input it already has when a sentence fails, and
those tactics now run against a goal of the wrong shape — which is how a
`vm_compute` ends up on a free variable and eats tens of GB. `coqc` never gets
there, because it stops at the first error. On one real file (70
`vm_compute`/`native_compute` calls in 489 lines) a broken tactic mid-file took
**tens of minutes and 35 GB**; with the interrupt it takes **5.2 s and 1.0 GB**.

Once a sentence has failed, a command that keeps burning CPU without finishing
**and without printing anything** gets a SIGINT, which Rocq turns into
`Error: User interrupt.` and carries on from.

**That predicate is not optional, and each clause was paid for.** Rocq only
protects itself from `Sys.Break` while it is *executing*; a signal that lands
while it is reading input, printing a prompt, or **formatting a large goal**
kills the process outright with `Fatal error: exception Stdlib.Sys.Break`. The
formatting case is not hypothetical — a large proof context takes a noticeable
time to print, during which Rocq burns CPU and reports no new sentence, which
looks exactly like a stuck tactic. Hence: burning CPU, no new sentence, no new
output, for two seconds. Short sentences are left to finish; they are cheap,
and they are not the problem.

## Sessions

One daemon per workspace, one `rocq repl` per `.v` file, each child in its own
process group. Sessions are keyed on the file, the build flags, and the
**toolchain**: the absolute `rocq` the client resolved plus the environment
that resolved it (`PATH`, `OCAMLPATH`, `COQPATH`, `COQLIB`, …). A daemon
outlives the shell that started it, and on a machine with several opam switches
the next caller may be in a different one; answering from the switch the daemon
happened to warm up with is the worst failure available — a confident OK about
a toolchain you are not using. The client's environment is *merged over* the
daemon's, never substituted for it: `env=` is the child's whole environment,
and a child with no `HOME` or `TMPDIR` misbehaves for reasons that have nothing
to do with Rocq.

**Killing the daemon does not strand its children, and not because anything
reaps them.** The daemon holds the only writer on each child's stdin, so its
death is an EOF and Rocq exits on its own. The exception is a child that is
*busy*: it will not read stdin, so a long `vm_compute` can outlive the daemon
by minutes. For that the daemon records its sessions in `.rocq-warm/sessions`
and the next daemon kills what the last one left — matching on pid **and**
cmdline, because pids get recycled and because on a shared machine a pattern
kill takes out other checkouts' sessions too.

### Many daemons, one machine

Isolation is structural: everything a daemon owns lives under its own
`<workspace>/.rocq-warm/`, and the stray reaper matches cmdline as well as pid,
so a neighbouring checkout is never in scope. Nothing pattern-kills.

Memory is the part that does not isolate itself, and it is where a per-daemon
design goes wrong: a budget expressed as "half of RAM" is correct for one
daemon and catastrophic for ten, and the process the kernel kills to make room
belongs to somebody else. So the budget is capped in absolute terms rather than
being a share, and `_evict` additionally yields whenever `MemAvailable` is
below a floor -- the only signal that moves when the pressure is not ours.
Under that pressure a daemon will drop its last session, which costs it a cold
check and costs its neighbours nothing.

Eviction skips a session that is mid-check, since killing the child out from
under a running check throws away exactly the work being saved.

Startup is serialised by an `flock` on `.rocq-warm/lock`. Two clients can begin
a check at the same instant and both try to spawn a daemon; without the lock
the loser unlinks the winner's socket and binds its own, orphaning a daemon
that keeps its sessions resident and unreachable -- memory nobody can account
for, and the failure looks like the tool being merely forgetful.

Guards, because a session that lies is worse than no session: a per-check wall
timeout; a per-session RSS ceiling that kills the child mid-check; LRU eviction
under a global budget; an idle timeout after which the daemon exits. And the
one that is not obvious — **a session blocked reading stdin burns no CPU**,
which is how a file that ends inside an unterminated sentence is told apart
from a tactic that is going to take ten minutes.

## Checking it against `coqc`

`tests/rocq_warm_corpus.py` replays a scripted edit sequence over real proofs
with `coqc` as the oracle, once per version: break a tactic late, restore,
insert a comment, restore, break one halfway, restore. It compares the verdict,
the diagnostics, and — for the cold pass — the sentence map against
`coqc -time`'s.

Two things it has to be careful about, both learned the hard way:

- **A negative return code from the reference `coqc` is not a verdict.** On a
  shared machine a neighbour's `pkill -f rocqworker` takes out every
  checkout's workers, and reading that as "coqc says FAILED" turns somebody
  else's housekeeping into a reported disagreement. Retry before believing it.
  The same applies to a session dying: the death note names the signal (9 is
  the OOM killer, 6 is Rocq aborting on its own, 15 is somebody else).
- **`Set Silent` drops `Time`'s "Finished transaction in …"**, the one piece of
  non-diagnostic chatter `coqc` also prints. Strip it before comparing; a
  timing is not a verdict, and a warm run has no meaningful one to give.

### The one place it cannot match `coqc`

The `comment-terminator-in-string` warning — a `"` inside a comment — is
reported at a location Rocq computes wrongly in batch mode too: `coqc` itself
prints a **negative** column for it (`characters -203--203`), its `bol` having
ended up ahead of the position it names. There is no right answer to reproduce,
so `rocq-warm` reports it at its own reading and the corpus runner compares
that warning's presence but not its location. Everything else — every error,
every other warning — matches `coqc` byte for byte.
