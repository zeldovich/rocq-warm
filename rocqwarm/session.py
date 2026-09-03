"""A warm `rocq repl` session parked inside one .v file.

The session keeps a Rocq REPL alive with the file already executed, remembers
where every sentence started and which STM state it ran from, and on the next
check replays only from the first sentence the edit could have touched.

`BackTo` restores the *whole* system state -- verified experimentally against
Rocq 9.0.1 for `Require`, `Notation` (the parser itself), `Ltac` and
`Set`/`Unset` -- which is what makes the prefix genuinely reusable rather than
merely "probably fine".
"""

import errno
import os
import re
import signal
import subprocess
import threading
import time

from . import diag as diagmod
from . import protocol

# `Set Silent.` is not cosmetic.  Printing a full Iris goal after each of a few
# thousand sentences costs more than the proof does; without it the REPL runs
# ~3x slower than coqc and the whole idea collapses.  It leaves `-time`'s Chars
# lines, the prompts, warnings and errors alone.
PROLOGUE = b"Set Silent.\n"

# A sentinel must PARSE (a parse error emits no Chars line at all), execute,
# succeed, and be unmistakable in the truncated `[...]` display.  `Locate` on an
# unknown name does all four: it prints "No object of basename ..." and moves
# the state on.  `Print`/`Check` on an unknown name FAIL, and `Print Rocq
# Version.` does not parse.
SENTINEL_FMT = "Locate rocq_warm_snt_%d."

# How far ahead of Rocq's reported parse position we are willing to write.  It
# must exceed the largest single sentence -- ProofIput.v has one of 13575 bytes
# -- plus whatever Rocq's own input channel buffers, or the feed deadlocks:
# Rocq cannot report a sentence it has not finished reading.  The window grows
# on its own when it turns out to be too small (see `_write_all`), so this is
# only a starting guess; it is small on purpose, because on an error everything
# already in flight still gets executed before we can stop.
DEFAULT_WRITE_AHEAD = 16384
MAX_WRITE_AHEAD = 1 << 21
STALL_GRACE = 1.0
DEFAULT_IDLE_KILL = 20.0        # seconds of zero CPU while input is owed

# How long one command must run, behind an error, before we interrupt it.  It
# has to be long enough that formatting a large Iris goal never looks like a
# stuck tactic: printing burns CPU without advancing the parse, and a SIGINT
# that lands during printing is fatal rather than catchable.  A pathological
# `vm_compute` runs for minutes, so there is plenty of room.
INTERRUPT_STALL = 2.0


class SessionDead(Exception):
    pass


class FeedTimeout(Exception):
    pass


class Unterminated(Exception):
    """The text fed ended in the middle of a sentence."""


class MemoryLimit(Exception):
    """The session outgrew its RSS ceiling and was killed."""


class Session:
    def __init__(self, path, flags, cwd=None, write_ahead=DEFAULT_WRITE_AHEAD,
                 silent=True, env=None, rss_limit=None, rocq="rocq"):
        self.path = os.path.abspath(path)
        self.flags = list(flags)
        self.cwd = cwd or os.path.dirname(self.path)
        self.write_ahead = write_ahead
        self.silent = silent
        self.env = env
        # The absolute `rocq` the CLIENT resolved, not whatever is on the
        # daemon's PATH.  A daemon outlives the shell that started it, and on a
        # machine with several opam switches the next caller may well be in a
        # different one.
        self.rocq = rocq
        # A ceiling, checked while a check is running.  This tree has had a
        # `vm_compute` on a goal with a free variable reach 31 GB in six
        # minutes; a daemon that keeps such a session resident is worse than no
        # daemon.  None disables it.
        self.rss_limit = rss_limit
        self.proc = None
        self.buf = b""              # output from the last complete prompt on
        self.stream_written = 0     # bytes ever written to Rocq's stdin
        self.parsed_end = 0         # highest stream offset Rocq reports parsing
        self.sentences = []         # the file's sentences, in order
        self.text = b""             # the bytes of the file we have executed
        self._sentinel = 0
        self._stop_writing = threading.Event()
        self._write_done = threading.Event()
        self._reader = None
        self._cv = threading.Condition()
        self.complete = False
        self.text_being_fed = b""
        self._scan_pos = 0
        self._libmap = {}           # logical name -> .vo path, as Rocq reports it

    # ---------------------------------------------------------------- process

    def start(self):
        # A previous child may be dead but still holding its pipes; `stop` is a
        # no-op when there is none.
        self.stop()
        argv = [self.rocq, "repl", "-emacs", "-q", "-time",
                "-topfile", self.path] + self.flags
        self.proc = subprocess.Popen(
            argv, cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0, env=self.env,
            start_new_session=True)
        self.buf = b""
        self._scan_pos = 0
        self.stream_written = 0
        self.parsed_end = 0
        self.sentences = []
        self.text = b""
        self.complete = False
        self._libmap = {}
        self._stop_writing.clear()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._await(lambda: protocol.PROMPT_RE.search(self.buf) is not None,
                    timeout=120, what="banner")
        self._trim_to_last_prompt()
        if self.silent:
            self._feed_raw(PROLOGUE, timeout=120)

    def stop(self):
        if self.proc is None:
            return
        self._stop_writing.set()
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:
            pass
        for pipe in (self.proc.stdin, self.proc.stdout):
            try:
                pipe.close()
            except Exception:
                pass
        self.proc = None

    @property
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def rss_bytes(self):
        try:
            with open("/proc/%d/statm" % self.proc.pid) as f:
                return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        except Exception:
            return 0

    def _cpu_ticks(self):
        """utime+stime.  A Rocq blocked on stdin does not burn CPU; a runaway
        tactic does.  That is how we tell 'waiting for more input' apart from
        'this tactic is going to take ten minutes'."""
        try:
            with open("/proc/%d/stat" % self.proc.pid) as f:
                fields = f.read().rsplit(")", 1)[1].split()
            return int(fields[11]) + int(fields[12])
        except Exception:
            return None

    # ------------------------------------------------------------------- I/O

    def _read_loop(self):
        while True:
            try:
                chunk = self.proc.stdout.read(1 << 16)
            except Exception:
                chunk = b""
            with self._cv:
                if not chunk:
                    self._cv.notify_all()
                    return
                self.buf += chunk
                m = None
                for m in protocol.PROGRESS_RE.finditer(self.buf, self._scan_pos):
                    self._scan_pos = m.end()
                if m is not None:
                    self.parsed_end = max(self.parsed_end, int(m.group(2)))
                self._cv.notify_all()

    def _await(self, pred, timeout, what):
        deadline = time.time() + timeout
        with self._cv:
            while not pred():
                if not self.alive:
                    raise SessionDead(self._death_note("waiting for " + what))
                left = deadline - time.time()
                if left <= 0:
                    raise FeedTimeout(what)
                self._cv.wait(min(left, 0.5))

    def _death_note(self, when):
        """Why the child is gone, in the words that tell the two cases apart.

        A negative return code is a signal, and which one matters: -9 is the
        OOM killer or somebody's `pkill`, -15 is a deliberate terminate.  Both
        look identical in the transcript, and neither is a bug in the proof.
        """
        rc = self.proc.poll() if self.proc is not None else None
        if rc is not None and rc < 0:
            why = "killed by signal %d%s" % (
                -rc, " (out of memory, or somebody pattern-killed it)"
                if rc == -9 else "")
        else:
            why = "exited with status %s" % rc
        return "rocq %s %s:\n%s" % (why, when,
                                     self.buf[-4000:].decode("utf8", "replace"))

    def _trim_to_last_prompt(self):
        with self._cv:
            last = None
            for last in protocol.PROMPT_RE.finditer(self.buf):
                pass
            if last is not None:
                self.buf = self.buf[last.start():]
                self._scan_pos = 0

    def _raw_write(self, data):
        """Write bypassing the look-ahead window (recovery text only)."""
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            return
        with self._cv:
            self.stream_written += len(data)
            self._cv.notify_all()

    def _write_all(self, data):
        """Write with a bounded look-ahead so an error stops the feed promptly.

        Rocq only tells us how far it has parsed, so we keep at most
        `write_ahead` bytes of unparsed input in flight.  Without this, a
        failure at the top of a file would still let Rocq re-prove everything
        below it out of the pipe buffer.
        """
        self._write_done.clear()
        try:
            self._write_loop(data)
        finally:
            self._write_done.set()

    def _write_loop(self, data):
        pos = 0
        while pos < len(data):
            if self._stop_writing.is_set():
                return
            with self._cv:
                budget = self.parsed_end + self.write_ahead - self.stream_written
                blocked_since = time.time()
                while budget <= 0:
                    if not self.alive or self._stop_writing.is_set():
                        return
                    self._cv.wait(0.2)
                    if time.time() - blocked_since > STALL_GRACE:
                        # Rocq is not reporting progress and is not asking for
                        # anything: the window is smaller than the sentence it
                        # is trying to read.  Widen it -- permanently, so the
                        # session learns this file's shape once.
                        if self.write_ahead < MAX_WRITE_AHEAD:
                            self.write_ahead *= 2
                            blocked_since = time.time()
                    budget = self.parsed_end + self.write_ahead - self.stream_written
            n = min(len(data) - pos, max(budget, 512))
            try:
                self.proc.stdin.write(data[pos:pos + n])
                self.proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError) as e:
                if isinstance(e, OSError) and e.errno not in (errno.EPIPE,):
                    raise
                return
            pos += n
            with self._cv:
                self.stream_written += n
                self._cv.notify_all()

    # ------------------------------------------------------------------ feed

    def _feed_raw(self, data, timeout, stop_on_error=False):
        """Feed `data`, wait for it all to be executed, return the sentences.

        A trailing sentinel makes 'done' an exact signal rather than a
        quiescence guess: a slow tactic and a finished feed look identical from
        the outside otherwise.
        """
        self._sentinel += 1
        sentinel = (SENTINEL_FMT % self._sentinel)
        sentinel_pat = re.compile(
            rb'Chars \d+ - \d+ \[' + re.escape(sentinel.replace(" ", "~").encode()) + rb'\]')
        payload = data + b"\n" + sentinel.encode() + b"\n"
        # Size the window from the largest sentence this file has actually
        # shown us, rather than letting it ratchet upwards for ever: everything
        # in the window still executes when a sentence fails, so the smallest
        # window that cannot deadlock is the one we want.
        biggest = max((x.end - x.start for x in self.sentences), default=0)
        self.write_ahead = max(DEFAULT_WRITE_AHEAD, 2 * biggest + 8192)
        base = self.stream_written
        self._stop_writing.clear()
        # Clear before the thread starts, so the waiter cannot observe the
        # previous feed's flag and skip error detection.
        self._write_done.clear()
        writer = threading.Thread(target=self._write_all, args=(payload,), daemon=True)
        writer.start()
        try:
            try:
                stopped_early = self._await_sentinel(
                    sentinel_pat, timeout, stop_on_error)
            except Unterminated:
                # Everything was written and Rocq is still waiting: the text
                # ended inside a sentence, a comment or a string, and our
                # sentinel went in after it.  Close it off before reporting,
                # or the NEXT feed -- a `BackTo`, a query -- lands inside it too
                # and the session is lost for a check that was never going to
                # pass anyway.
                self._stop_writing.set()
                writer.join(timeout=10)
                self._recover(data, base, timeout)
                self._trim_to_last_prompt()
                raise
            if stopped_early:
                # We stopped mid-chunk, so Rocq is waiting for the rest of a
                # sentence and will never reach the sentinel we queued.  Close
                # whatever is lexically open, terminate, and re-send it.
                self._stop_writing.set()
                writer.join(timeout=10)
                self._interrupt_stalled_work()
                sentinel = self._recover(data, base, timeout)
        finally:
            self._stop_writing.set()
            writer.join(timeout=10)
        segments, _ = protocol.split_prompts(self.buf)
        items = protocol.parse_segments(segments)
        # Anything Rocq reported from before this chunk began is left over from
        # the previous feed and its offsets belong to a different chunk.
        items = [it for it in items
                 if not isinstance(it, protocol.Sentence)
                 or it.stream_start >= base]
        # drop the sentinel and anything after it
        end = len(items)
        for i, it in enumerate(items):
            if (isinstance(it, protocol.Sentence)
                    and it.display == sentinel.replace(" ", "~").encode()):
                end = i
                break
        items = items[:end]
        self._trim_to_last_prompt()
        return items, base

    def _recover(self, data, base, timeout):
        """Close whatever of `data` is lexically open, terminate the
        sentence Rocq is waiting on, and bring it back to a prompt.  Returns
        the sentinel that marks the recovery."""
        consumed = self.stream_written - base
        depth, in_string = self.lex_state(data[:consumed])
        recovery = (b'"' if in_string else b"") + b" *)" * depth + b" .\n"
        self._sentinel += 1
        sentinel = (SENTINEL_FMT % self._sentinel)
        sentinel_pat = re.compile(
            rb'Chars \d+ - \d+ \['
            + re.escape(sentinel.replace(" ", "~").encode()) + rb'\]')
        self._raw_write(recovery + sentinel.encode() + b"\n")
        self._await_sentinel(sentinel_pat, timeout, stop_on_error=False)
        return sentinel

    def _sigint(self):
        """Interrupt the command Rocq is running.

        Rocq only protects itself from `Sys.Break` while it is *executing*; a
        signal that lands while it is reading input, printing a prompt, or
        FORMATTING A LARGE GOAL kills it outright (`Fatal error: exception
        Stdlib.Sys.Break`).  That last one is not hypothetical -- an Iris error
        context takes a noticeable time to print, during which Rocq burns CPU
        and reports no new sentence, which looks exactly like a stuck tactic.
        So callers must have established all three: burning CPU, no new
        sentence, and **no new output**, for `INTERRUPT_STALL` seconds.  Never
        send this on spec.
        """
        try:
            os.kill(self.proc.pid, signal.SIGINT)
        except (OSError, AttributeError):
            pass

    def _interrupt_stalled_work(self, limit=60.0):
        """SIGINT a single command that is running long behind an error.

        Once a sentence has failed, the input already in flight still executes
        -- against a goal of the wrong shape, which is how a `vm_compute` ends
        up on a free variable and reaches tens of GB (`ProofIsmapped.v`
        has 70 of them in 489 lines).  Rocq turns SIGINT into
        `Error: User interrupt.` and carries on with the next sentence.

        The predicate matters.  Rocq only protects itself from `Sys.Break`
        while it is *executing* a command; a signal that lands while it is
        reading input or printing a prompt kills the process outright
        (`Fatal error: exception Stdlib.Sys.Break`).  So signal only when it is
        burning CPU AND has not reported a new sentence for a while -- which is
        exactly "one command has been running a long time", and never the gap
        between two fast ones.  Short sentences are left to finish; they are
        cheap, and they are not the problem.
        """
        deadline = time.time() + limit
        idle_since = None
        last_ticks = self._cpu_ticks()
        last_parsed, last_out = self.parsed_end, len(self.buf)
        stuck_since = time.time()
        while time.time() < deadline and self.alive:
            time.sleep(0.05)
            now = time.time()
            ticks = self._cpu_ticks()
            if ticks is None:
                return
            if self.parsed_end != last_parsed or len(self.buf) != last_out:
                last_parsed, last_out, stuck_since = (
                    self.parsed_end, len(self.buf), now)
            if ticks == last_ticks:
                idle_since = idle_since or now
                if now - idle_since > 0.4:
                    return          # drained: it is waiting for input again
                continue
            last_ticks, idle_since = ticks, None
            if now - stuck_since > INTERRUPT_STALL:
                self._sigint()
                stuck_since = now

    def _await_sentinel(self, pat, timeout, stop_on_error=False):
        """Wait for the sentinel AND the prompt that follows it.

        Waiting only for the sentinel's own `Chars` line is a race: the prompt
        after it may not have arrived, `_trim_to_last_prompt` then keeps the
        sentinel in the buffer, and the NEXT feed parses it as one of its own
        sentences -- with stream offsets from the previous chunk.  That
        corrupts the sentence map and makes the following replay resume in the
        middle of a sentence.  It is timing-dependent, so it shows up as a
        flake rather than a failure.
        """
        def done():
            m = pat.search(self.buf)
            return m is not None and protocol.PROMPT_RE.search(self.buf, m.end())

        def hit_error():
            """A sentence that failed leaves the state id where it was.

            Spotting that DURING the feed is what makes a broken proof cheap:
            otherwise Rocq happily re-proves the whole rest of the file behind
            an error we already know about.
            """
            states = [int(m.group(2))
                      for m in (protocol.PROMPT_BODY_RE.match(p.group(1))
                                for p in protocol.PROMPT_RE.finditer(self.buf))
                      if m is not None]
            return any(a == b for a, b in zip(states, states[1:]))

        deadline = time.time() + timeout
        last_ticks = self._cpu_ticks()
        last_move = time.time()
        last_parsed, last_out = self.parsed_end, len(self.buf)
        stuck_since = time.time()
        error_seen = False
        while True:
            with self._cv:
                if done():
                    return False
                if not self.alive:
                    raise SessionDead(self._death_note("mid-feed"))
                if stop_on_error and not error_seen and hit_error():
                    error_seen = True
                    self._stop_writing.set()
                    if not self._write_done.is_set():
                        return True     # caller closes the sentence and retries
                self._cv.wait(0.25)
                if done():
                    return False
            now = time.time()
            if now > deadline:
                raise FeedTimeout("feed exceeded %.0fs" % timeout)
            if self.rss_limit and self.rss_bytes() > self.rss_limit:
                raise MemoryLimit(
                    "rocq reached %.1f GB, over the %.1f GB ceiling"
                    % (self.rss_bytes() / 1e9, self.rss_limit / 1e9))
            if self.parsed_end != last_parsed or len(self.buf) != last_out:
                last_parsed, last_out, stuck_since = (
                    self.parsed_end, len(self.buf), now)
            ticks = self._cpu_ticks()
            if ticks is None or ticks != last_ticks:
                if error_seen and now - stuck_since > INTERRUPT_STALL:
                    # One command has been running a long time behind an error
                    # we already know about, on a goal of the wrong shape, and
                    # is producing no output while it does.  See `_sigint` for
                    # why the predicate has to be that narrow.
                    self._sigint()
                    stuck_since = now
                last_ticks, last_move = ticks, now
            elif (self._write_done.is_set()
                  and now - last_move > DEFAULT_IDLE_KILL):
                # Rocq is burning no CPU and still owes us a sentinel: it is
                # blocked reading stdin, which means the text we fed ended in
                # the middle of a sentence and swallowed the sentinel too.
                raise Unterminated(
                    "end of file inside an unterminated sentence")

    # --------------------------------------------------------------- mapping

    def _absorb(self, items, base, file_start, file_end):
        """Attach .v byte offsets, and the anchor each message is relative to.

        Rocq's Chars counter is a running offset into everything ever written to
        its stdin, so the stream offset at which this chunk began is what turns
        a Chars range into a file range.

        The anchor is subtler and is the thing that makes locations agree with
        `coqc`.  `Toplevel input, characters A-B` is measured from a line start
        that is neither the sentence's nor the error's: Rocq consumes the
        whitespace after a sentence's terminating `.` and takes the line it
        lands on.  So

          * whitespace-then-newline after the previous sentence -> the anchor is
            just past that FIRST newline, and further blank lines, indentation
            and comment blocks before the sentence all count into A;
          * anything else first -- a trailing comment on the previous line, or a
            second sentence on the same line -- and the anchor stays on the
            PREVIOUS sentence's line.

        The second case is not exotic: `Require Import WpInstr.   (* ... *)` is
        ordinary style here, and getting it wrong shifts every column on the
        following sentence by the width of the comment.  For the first sentence
        of a chunk the previous "sentence" is our own sentinel, which we always
        follow with a newline, so the anchor is exactly where the chunk begins.
        Derived by solving for it over the shapes in
        `tests/test_rocq_warm_diag.py`, each re-checked against a live `coqc`.
        """
        text = self.text_being_fed
        for it in items:
            if isinstance(it, protocol.Sentence):
                it.start = it.stream_start - base + file_start
                it.end = it.stream_end - base + file_start
        prev_end = None
        for i, it in enumerate(items):
            if isinstance(it, protocol.ParseFailure):
                # A parse error has no Chars line and so no range of its own.
                it.start = diagmod.skip_blanks(
                    text, file_start if prev_end is None else prev_end)
                it.end = file_end
                for later in items[i + 1:]:
                    if isinstance(later, protocol.Sentence):
                        it.end = later.start
                        break
            it.anchor = (file_start if prev_end is None
                         else diagmod.message_anchor(text, prev_end))
            prev_end = it.end
        return items

    # --------------------------------------------------------- edit analysis

    @staticmethod
    def common_prefix_len(a, b):
        n = min(len(a), len(b))
        if a[:n] == b[:n]:
            return n
        lo, hi = 0, n                       # binary search: these are big
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if a[:mid] == b[:mid]:
                lo = mid
            else:
                hi = mid - 1
        return lo

    @staticmethod
    def blank_or_comment(chunk):
        """True if `chunk` is nothing but whitespace and balanced comments.

        Rocq comments nest, and a string inside a comment hides a `*)`, so this
        follows the lexer's rules.  Used only to prove an edit cannot have
        changed the sentence structure -- a false negative just costs a replay.
        """
        i, n, depth = 0, len(chunk), 0
        while i < n:
            if chunk[i:i + 2] == b"(*":
                depth, i = depth + 1, i + 2
                continue
            if depth == 0:
                if chunk[i:i + 1] not in b" \t\r\n":
                    return False
                i += 1
                continue
            if chunk[i:i + 2] == b"*)":
                depth, i = depth - 1, i + 2
            elif chunk[i:i + 1] == b'"':
                i += 1
                while i < n and chunk[i:i + 1] != b'"':
                    i += 1
                i += 1
            else:
                i += 1
        return depth == 0

    @staticmethod
    def lex_state(chunk):
        """(comment_depth, in_string) at the end of `chunk`.

        Needed to cut a feed short safely.  Stopping mid-sentence leaves Rocq
        waiting for the rest of it, so we have to hand it a terminator -- and a
        bare `.` is not a terminator inside a comment or a string.  Knowing the
        exact lexical state lets us close whatever is open and then terminate,
        instead of guessing with a ladder of escapes.
        """
        i, n, depth, in_string = 0, len(chunk), 0, False
        while i < n:
            if in_string:
                if chunk[i:i + 2] == b'""':
                    i += 2
                elif chunk[i:i + 1] == b'"':
                    in_string, i = False, i + 1
                else:
                    i += 1
            elif chunk[i:i + 2] == b"(*":
                depth, i = depth + 1, i + 2
            elif depth and chunk[i:i + 2] == b"*)":
                depth, i = depth - 1, i + 2
            elif chunk[i:i + 1] == b'"':
                in_string, i = True, i + 1
            else:
                i += 1
        return depth, in_string

    UNSAFE_HEAD = re.compile(
        rb'^\s*(?:From\s+\S+\s+)?(?:Require|Declare\s+ML\s+Module|Load)\b')

    def _unsafe_to_undo(self, index):
        """Is any sentence at or after `index` one we would rather not undo?

        Experimentally `BackTo` does undo a `Require` correctly (the names go
        away again), but unloading a library is the one place a warm session
        could plausibly diverge from a cold `coqc`, and the only edits that
        reach it are edits to the header -- where a cold start is the honest
        answer anyway.  Cheap insurance, no real cost.
        """
        for s in self.sentences[index:]:
            if self.UNSAFE_HEAD.match(self.text[s.start:s.end]):
                return True
        return False

    # ----------------------------------------------------------------- check

    def plan(self, text):
        """How to check `text`, without doing it.

        ('cold',)                    -- throw the session away and start over
        ('shift', delta, from_index) -- the edit was whitespace/comments only
        ('replay', offset, state)    -- BackTo `state`, feed from `offset`
        """
        if not self.alive or not self.sentences:
            return ("cold",)
        d = self.common_prefix_len(self.text, text)
        k = 0
        while k < len(self.sentences) and self.sentences[k].end <= d:
            k += 1
        if k == 0 or self._unsafe_to_undo(k):
            return ("cold",)
        prev = self.sentences[k - 1]
        if self.complete and k < len(self.sentences):
            gap_start = prev.end
            gap_end_old = self.sentences[k].start
            delta = len(text) - len(self.text)
            gap_end_new = gap_end_old + delta
            if (d >= gap_start and gap_end_new >= gap_start
                    and self.text[gap_end_old:] == text[gap_end_new:]
                    and self.blank_or_comment(self.text[gap_start:gap_end_old])
                    and self.blank_or_comment(text[gap_start:gap_end_new])):
                return ("shift", delta, k)
        return ("replay", prev.end, prev.state_after)

    def check(self, text, timeout=1800, _retry=True):
        """Execute `text`, reusing as much of the warm prefix as is sound.

        If the child dies mid-check -- a neighbour's `pkill` on this shared
        box, or our own interrupt landing in the wrong place -- that costs a
        cold run, not an error.
        """
        try:
            return self._check(text, timeout)
        except SessionDead:
            if not _retry:
                raise
            self.stop()
            self.start()
            return self.check(text, timeout=timeout, _retry=False)

    def _check(self, text, timeout):
        t0 = time.time()
        plan = self.plan(text)
        mode = plan[0]

        if mode == "shift":
            # Every stored anchor from k on moves with the text -- including
            # sentence k's, whose anchor sits in the gap that just changed.
            # The anchor is not a semantic position here but the origin the
            # CACHED message offsets were measured from, so it has to travel
            # with the text they point into; recomputing it from the new gap
            # leaves those offsets short by the gap's change in width.
            _, delta, k = plan
            for s in self.sentences[k:]:
                s.start += delta
                s.end += delta
                s.anchor += delta
            self.text = text
            return CheckResult(True, self._prefix_diags(), mode="shift",
                               replayed=0, seconds=time.time() - t0,
                               total=len(self.sentences))

        if mode == "cold":
            self.start()
            resume = 0
        else:
            _, resume, state = plan
            self._backtrack(state)
            keep = 0
            while keep < len(self.sentences) and self.sentences[keep].end <= resume:
                keep += 1
            del self.sentences[keep:]

        try:
            items, base = self._feed_raw(text[resume:], timeout=timeout,
                                         stop_on_error=True)
        except Unterminated:
            self.text = text[:resume]
            self.complete = False
            if self.sentences:
                self._backtrack(self.sentences[-1].state_after)
            return CheckResult(
                False,
                self._prefix_diags() + [Diag(
                    "error", resume,
                    b"Error: Syntax error: end of file inside an "
                    b"unterminated sentence")],
                mode=mode, replayed=0, seconds=time.time() - t0,
                total=len(self.sentences))

        self.text_being_fed = text
        items = self._absorb(items, base, resume, len(text))
        first_bad = None
        for i, it in enumerate(items):
            if it.failed:
                first_bad = i
                break
        good = items[:first_bad] if first_bad is not None else items
        self.sentences.extend(s for s in good if isinstance(s, protocol.Sentence))
        # Warnings from the REUSED prefix have to be reported too.  A warm run
        # never re-executes those sentences, so without this a replay silently
        # drops every warning above the edit and stops matching `coqc`.
        diags = self._prefix_diags()
        if first_bad is not None:
            diags += _diags_of(items[first_bad], include_info=not self.silent)
        if first_bad is None:
            self.text = text
            self.complete = True
        else:
            # Park the session exactly at the broken sentence, so that the next
            # edit -- the fix for it -- replays from here and nothing before.
            self.text = text[:items[first_bad].start]
            self.complete = False
            if self.sentences:
                self._backtrack(self.sentences[-1].state_after)
        return CheckResult(first_bad is None, diags, mode=mode,
                           replayed=len(items), seconds=time.time() - t0,
                           total=len(self.sentences))

    def _prefix_diags(self):
        """Every message the sentences we are keeping produced.

        Errors and warnings always; the proof's own output only when the
        session was started without `Set Silent`, since otherwise Rocq never
        printed it in the first place.
        """
        out = []
        for s in self.sentences:
            out += _diags_of(s, include_info=not self.silent)
        return out

    def _backtrack(self, state):
        self._feed_raw(("BackTo %d." % state).encode(), timeout=300)

    def _state_now(self):
        """The state id Rocq is parked at, from its most recent prompt."""
        with self._cv:
            last = None
            for last in protocol.PROMPT_RE.finditer(self.buf):
                pass
            if last is None:
                return None
            body = protocol.PROMPT_BODY_RE.match(last.group(1))
            return int(body.group(2)) if body else None

    LIB_NAME_RE = re.compile(rb'^[^\s"<>]+$')
    LOCATED_RE = re.compile(
        rb'(\S+) has been loaded from file\s+(.+?)\s*$', re.S)

    def loaded_libraries(self, timeout=300):
        """{logical name: .vo path} for every library Rocq has loaded.

        Asked of Rocq itself -- `Print Libraries.` for the names, `Locate
        Library` for the files -- because that is the only source that knows.
        `rocq dep` cannot see an installed library, nor a `Require` that was
        added after the session started; the process that did the loading
        can.  The queries are undone with `BackTo`, so the session is parked
        exactly where it was.  A few hundred `Locate Library` queries cost
        tens of milliseconds, and each name is asked about once per session.
        """
        if not self.alive:
            return {}
        parked = self._state_now()
        try:
            items, _ = self._feed_raw(b"Print Libraries.", timeout=timeout)
            names = []
            for it in items:
                if not isinstance(it, protocol.Sentence):
                    continue
                for line in protocol.message_text(it.messages).splitlines():
                    line = line.strip()
                    if line and self.LIB_NAME_RE.match(line):
                        names.append(line.decode("utf8", "replace"))
            unknown = [n for n in names if n not in self._libmap]
            if unknown:
                query = b"".join(b"Locate Library %s.\n" % n.encode()
                                 for n in unknown)
                items, _ = self._feed_raw(query, timeout=timeout)
                for it in items:
                    if not isinstance(it, protocol.Sentence):
                        continue
                    m = self.LOCATED_RE.search(protocol.message_text(it.messages))
                    if m is None:
                        continue
                    name = m.group(1).decode("utf8", "replace")
                    path = re.sub(rb'\s+', b'', m.group(2)).decode("utf8", "replace")
                    self._libmap[name] = os.path.normpath(
                        os.path.join(self.cwd, path))
        finally:
            if parked is not None and self.alive:
                self._backtrack(parked)
        return {n: self._libmap[n] for n in names if n in self._libmap}


def _diags_of(item, include_info=False):
    out = []
    for blob in _split_messages(item.messages):
        kind = protocol.classify(blob)
        if kind != "info" or include_info:
            out.append(Diag(kind, item.anchor, blob))
    return out


def _split_messages(raw):
    """Split one sentence's output into individual message blobs."""
    if not raw or not raw.strip():
        return []
    parts = re.split(rb'(?m)(?=^Toplevel input, characters )', raw)
    return [p for p in parts if p.strip()]


class Diag:
    """One error or warning, still carrying Rocq's raw blob.

    The absolute span is not baked in here: it depends on the file text, which
    the caller has and which `diag.locate` needs anyway.
    """

    __slots__ = ("kind", "anchor", "raw")

    def __init__(self, kind, anchor, raw):
        self.kind, self.anchor, self.raw = kind, anchor, raw

    def span(self, text):
        return diagmod.locate(self.raw, self.anchor)

    def message(self):
        return diagmod.strip_location(self.raw)

    def render(self, display_path, text):
        return diagmod.render(display_path, text, self.span(text),
                              self.message())

    def __repr__(self):
        return "Diag(%s, @%s, %r)" % (self.kind, self.anchor,
                                      self.message()[:60])


class CheckResult:
    def __init__(self, ok, diags, mode, replayed, seconds, total):
        self.ok, self.diags, self.mode = ok, diags, mode
        self.replayed, self.seconds, self.total = replayed, seconds, total

    def __repr__(self):
        return "CheckResult(ok=%s, mode=%s, replayed=%d/%d, %.1fs, %d diags)" % (
            self.ok, self.mode, self.replayed, self.total, self.seconds,
            len(self.diags))
