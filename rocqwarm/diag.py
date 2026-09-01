"""Turning REPL messages into `coqc`-format diagnostics.

`rocq repl -emacs` reports a location as `Toplevel input, characters A-B:`,
where A and B are byte offsets relative to an anchor that `Session._absorb`
computes -- the start of the line on which Rocq resumed reading, which is
neither the sentence nor the line the error is on.  Adding the anchor recovers
the absolute position, and from there the rendering is `coqc`'s:

    File "./Foo.v", line 9, characters 7-17:
    Error: The variable bogus_name was not found in the current environment.

Verified against `coqc` for an execution error inside a multi-line sentence, an
error whose span crosses a line break, and a parse error (which reports no
sentence range at all).  `tests/test_rocq_warm_diag.py` re-checks all three
against a live `coqc` rather than against a recorded string.
"""

import re

LOC_RE = re.compile(rb'^Toplevel input, characters (\d+)-(\d+):$', re.M)


def line_bol(text, off):
    """Byte offset of the start of the line containing `off`."""
    return text.rfind(b"\n", 0, off) + 1


def line_number(text, off):
    return text.count(b"\n", 0, off) + 1


def skip_blanks(text, i):
    """First byte at or after `i` that is neither whitespace nor a comment.

    Rocq's comments nest and can contain strings that hide a `*)`; a parse
    error carries no range of its own, so this is how we find where the
    sentence it choked on actually began.
    """
    n = len(text)
    while i < n:
        c = text[i:i + 1]
        if c in b" \t\r\n":
            i += 1
        elif text[i:i + 2] == b"(*":
            depth, i = 1, i + 2
            while i < n and depth:
                if text[i:i + 2] == b"(*":
                    depth, i = depth + 1, i + 2
                elif text[i:i + 2] == b"*)":
                    depth, i = depth - 1, i + 2
                elif text[i:i + 1] == b'"':
                    i += 1
                    while i < n and text[i:i + 1] != b'"':
                        i += 1
                    i += 1
                else:
                    i += 1
        else:
            return i
    return n


def message_anchor(text, prev_end):
    """Where Rocq measures the next sentence's message offsets from.

    See `Session._absorb` for what this is and why it matters.  Rocq skips the
    whitespace after a sentence's `.`; if it crosses a newline the anchor is the
    line it lands on, and if it meets anything else first -- a trailing comment,
    or another sentence on the same line -- the anchor stays on the line the
    previous sentence ended on.
    """
    i, n = prev_end, len(text)
    while i < n and text[i:i + 1] in b" \t\r":
        i += 1
    if i < n and text[i:i + 1] == b"\n":
        return i + 1
    return line_bol(text, prev_end)


def locate(raw, anchor):
    """(abs_start, abs_end) for one message blob, or None if it has no location.

    `anchor` is computed in `Session._absorb`; see the note there for what
    Rocq's offsets are actually relative to.
    """
    m = LOC_RE.search(raw)
    if m is None or anchor is None:
        return None
    return anchor + int(m.group(1)), anchor + int(m.group(2))


# `rocq repl` parses at the `vernac_toplevel` grammar entry (the one that also
# accepts `BackTo`), so its syntax errors name that entry where `coqc` names
# plain `vernac`.  Same error, same location, different word -- rewritten so a
# warm diagnostic is byte-identical to the batch one.
REPL_WORDING = ((b"illegal begin of toplevel:vernac_toplevel",
                 b"illegal begin of vernac"),)


def strip_location(raw):
    """The message itself: no location header, no echoed source, no markup."""
    out = []
    for line in raw.split(b"\n"):
        if LOC_RE.match(line) or line.startswith(b"> "):
            continue
        out.append(line)
    txt = b"\n".join(out)
    for tag in (b"<infomsg>", b"</infomsg>", b"<warning>", b"</warning>"):
        txt = txt.replace(tag, b"")
    for repl_form, batch_form in REPL_WORDING:
        txt = txt.replace(repl_form, batch_form)
    return txt.strip()


def render(display_path, text, span, message):
    """One diagnostic, byte-for-byte in `coqc`'s shape."""
    body = message.decode("utf8", "replace")
    if span is None:
        # Rocq's message already carries its own `Error:`/`Warning:` prefix;
        # all that is missing is a location, and there is none to give.
        return body
    start, end = span
    bol = line_bol(text, start)
    return 'File "%s", line %d, characters %d-%d:\n%s' % (
        display_path, line_number(text, start), start - bol, end - bol, body)
