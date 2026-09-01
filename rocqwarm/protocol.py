"""Parser for the `rocq repl -emacs -time` output stream.

The stream is a strict alternation of prompts and sentence reports:

    <prompt>Rocq < 3 || 0 < </prompt>       state before sentence 3
    Chars 32 - 52 [Check~foo.] 0.1 secs     the sentence Rocq just ran
    Error: ...                              anything the sentence printed
    <prompt>Rocq < 4 || 0 < </prompt>       state after it

Three facts about this stream carry the whole design, and each is checked by
`tests/test_rocq_warm_protocol.py` against a live Rocq:

* `Chars A - B` are **byte** offsets into the stdin stream, and the counter runs
  continuously across separate writes.  That is what lets us hand Rocq's own
  parser the job of splitting sentences.
* every executed sentence gets exactly one prompt before it and one after,
  whatever the vernac (queries, `Set`, `Section`/`Module`, bullets, `Fail`).
* a sentence that FAILED does not advance the state id.  That is the verdict
  signal -- far more robust than grepping for `Error:` in output that a proof's
  own `idtac` may have written.

A *parse* error is the one sentence kind that produces no `Chars` line at all:
Rocq reports the error, skips to the next `.`, and carries on.
"""

import re

PROMPT_RE = re.compile(rb'<prompt>(.*?)</prompt>', re.S)
PROMPT_BODY_RE = re.compile(rb'^(.*) < (\d+) \|(.*)\| (\d+) < $', re.S)
# The bracketed display is the sentence text with spaces turned into `~`, and
# it is NOT escaped: Iris tactics are full of `]` (`iDestruct ... as "[H1 H2]"`),
# so this must be greedy and anchored on the trailing ` N secs (Nu,Ns)`, never
# non-greedy on the `]`.  Getting that wrong silently drops those sentences from
# the parse-progress signal and deadlocks the write-ahead window.
CHARS_RE = re.compile(
    rb'^Chars (\d+) - (\d+) \[(.*)\] ([0-9.]+) secs \(([0-9.]+)u,([0-9.]+)s\)$',
    re.M)
# Rocq writes the `Chars` line straight after `</prompt>`, on the SAME line, so
# a `^`-anchored pattern only matches once the stream has been cut into
# per-sentence segments.  Progress tracking works on the raw stream and must
# therefore not anchor.
PROGRESS_RE = re.compile(rb'Chars (\d+) - (\d+) \[')


class Sentence:
    """One vernac Rocq executed, with everything it printed."""

    __slots__ = ("stream_start", "stream_end", "display", "secs",
                 "state_before", "state_after", "messages", "start", "end",
                 "anchor")

    def __init__(self, stream_start, stream_end, display, secs,
                 state_before, state_after, messages):
        self.stream_start = stream_start
        self.stream_end = stream_end
        self.display = display
        self.secs = secs
        self.state_before = state_before
        self.state_after = state_after
        self.messages = messages
        self.start = None       # byte offset in the .v file, filled in by Session
        self.end = None
        self.anchor = None      # what Rocq's message offsets are relative to

    @property
    def failed(self):
        return self.state_after == self.state_before

    def __repr__(self):
        return "Sentence(%s-%s, %r, %s%s)" % (
            self.start, self.end, self.display[:40],
            self.state_before, " FAILED" if self.failed else "")


class ParseFailure:
    """A sentence Rocq could not even parse: no `Chars` line, no state change.

    Rocq skips to the next `.` and keeps going, so this carries no range of its
    own; `Session` reconstructs one from the surrounding sentences.
    """

    __slots__ = ("state_before", "messages", "start", "end", "anchor")

    def __init__(self, state_before, messages):
        self.state_before = state_before
        self.messages = messages
        self.start = None
        self.end = None
        self.anchor = None

    state_after = property(lambda self: self.state_before)
    failed = property(lambda self: True)
    display = property(lambda self: b"<parse error>")

    def __repr__(self):
        return "ParseFailure(%s-%s)" % (self.start, self.end)


def split_prompts(buf):
    """Cut a stream into one segment per sentence.

    Returns (segments, tail) where each segment is
    (state_before, state_after, output_bytes) -- everything Rocq printed
    between the prompt that preceded the sentence and the one that followed it
    -- and `tail` is the output after the last complete prompt, belonging to a
    sentence still running.  Anything before the first prompt (the banner) is
    dropped.

    N prompts delimit N-1 sentences, and the state on prompt i+1 is what tells
    us whether sentence i succeeded, so both ends of each pair matter.
    """
    marks = []
    for m in PROMPT_RE.finditer(buf):
        body = PROMPT_BODY_RE.match(m.group(1))
        if body is None:                    # not a prompt we understand
            continue
        marks.append((int(body.group(2)), m.start(), m.end()))
    segments = []
    for i in range(len(marks) - 1):
        state, _, end = marks[i]
        segments.append((state, marks[i + 1][0], buf[end:marks[i + 1][1]]))
    tail = buf[marks[-1][2]:] if marks else buf
    return segments, tail


def parse_segments(segments):
    """Turn segments into Sentence / ParseFailure objects."""
    out = []
    for state_before, state_after, seg in segments:
        m = CHARS_RE.search(seg)
        if m is None:
            out.append(ParseFailure(state_before, seg))
            continue
        messages = (seg[:m.start()] + seg[m.end():])
        out.append(Sentence(int(m.group(1)), int(m.group(2)), m.group(3),
                            float(m.group(4)), state_before, state_after,
                            messages))
    return out


def message_text(raw):
    """Strip the markup `-emacs` mode wraps around messages."""
    txt = raw.replace(b"<infomsg>", b"").replace(b"</infomsg>", b"")
    txt = txt.replace(b"<warning>", b"").replace(b"</warning>", b"")
    return txt.strip()


def classify(raw):
    """'error', 'warning' or 'info' for one message blob."""
    if re.search(rb'(?m)^Error:', raw):
        return "error"
    if b"<warning>" in raw or re.search(rb'(?m)^Warning:', raw):
        return "warning"
    return "info"
