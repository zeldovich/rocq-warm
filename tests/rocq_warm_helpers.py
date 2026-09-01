"""Shared scaffolding for the rocq-warm tests.

Every test here runs a real Rocq.  There is no mock: the whole point of the
tool is that its answers match `coqc`'s, and a mock would only prove that the
code agrees with our beliefs about Rocq rather than with Rocq.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

HAVE_ROCQ = shutil.which("rocq") is not None and shutil.which("coqc") is not None
requires_rocq = unittest.skipUnless(
    HAVE_ROCQ, "needs rocq and coqc on PATH (eval $(opam env))")


class Workspace:
    """A throwaway one-file Rocq project."""

    def __init__(self, logical="T"):
        self.dir = tempfile.mkdtemp(prefix="rocq-warm-test-")
        self.logical = logical
        with open(os.path.join(self.dir, "_CoqProject"), "w") as f:
            f.write("-R . %s\n" % logical)

    def write(self, name, text):
        if isinstance(text, str):
            text = text.encode()
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(text)
        return path

    @property
    def flags(self):
        return ["-R", ".", self.logical]

    def coqc(self, name, timeout=300):
        """(returncode, normalized diagnostics) from a real cold compile."""
        proc = subprocess.run(["coqc", "-q"] + self.flags + [name],
                              cwd=self.dir, capture_output=True, timeout=timeout)
        out = (proc.stdout + proc.stderr).decode("utf8", "replace")
        return proc.returncode, normalize(out, self.dir)

    def coqc_sentences(self, name, timeout=300):
        """[(start, end)] for every sentence, straight from Rocq's own parser.

        `-time` reports the byte range of each sentence it executes, which makes
        `coqc` the oracle for the sentence map rather than anything we wrote.
        """
        proc = subprocess.run(["coqc", "-q", "-time"] + self.flags + [name],
                              cwd=self.dir, capture_output=True, timeout=timeout)
        out = (proc.stdout + proc.stderr).decode("utf8", "replace")
        return [(int(a), int(b))
                for a, b in re.findall(r'^Chars (\d+) - (\d+) \[', out, re.M)]

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


_WS = re.compile(r'[ \t]+')


def normalize(text, root=None):
    """Compare diagnostics on their content, not their incidental layout.

    Absolute paths become basenames and runs of spaces collapse, so a test
    failure means Rocq and rocq-warm actually disagree.
    """
    out = []
    for line in text.splitlines():
        line = re.sub(r'File "[^"]*/([^"/]+)"', r'File "\1"', line)
        line = re.sub(r'File "([^"/]+)"', r'File "\1"', line)
        line = _WS.sub(" ", line).rstrip()
        if line:
            out.append(line)
    return "\n".join(out)


def render_all(result, display, text):
    """rocq-warm's diagnostics, normalized the same way `coqc`'s are."""
    return normalize("\n".join(d.render(display, text) for d in result.diags))
