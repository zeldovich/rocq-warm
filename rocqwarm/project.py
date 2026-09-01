"""Resolving a .v file's build flags and its dependency fingerprint.

Two jobs, both about making sure a warm session is checking the same thing a
cold `coqc` would:

* turn the nearest `_CoqProject` into the flag list `rocq repl` needs, so the
  session sees exactly the load path and warning settings the build uses;
* fingerprint every `.vo` the file transitively requires, so that a rebuilt
  dependency throws the session away instead of being silently checked against
  the copy Rocq loaded into memory an hour ago.
"""

import os
import subprocess


PROJECT_NAMES = ("_CoqProject", "_RocqProject")


def find_project(start):
    """Nearest _CoqProject at or above `start`'s directory."""
    d = os.path.abspath(start if os.path.isdir(start) else os.path.dirname(start))
    while True:
        for name in PROJECT_NAMES:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def parse_project(path):
    """Flags from a _CoqProject, in the order `rocq` wants them.

    Only the load-path and `-arg` directives matter to us; the listed `.v`
    files are the build's business, not ours.
    """
    flags = []
    with open(path) as f:
        for raw in f:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            i = 0
            while i < len(parts):
                tok = parts[i]
                if tok in ("-R", "-Q") and i + 2 < len(parts):
                    flags += [tok, parts[i + 1], parts[i + 2]]
                    i += 3
                elif tok in ("-I",) and i + 1 < len(parts):
                    flags += [tok, parts[i + 1]]
                    i += 2
                elif tok == "-arg" and i + 1 < len(parts):
                    flags.append(parts[i + 1])
                    i += 2
                elif tok.startswith("-"):
                    flags.append(tok)
                    i += 1
                else:
                    i += 1              # a source file; not our business
    return flags


def flags_for(path, project=None):
    """(flags, cwd) for one .v file."""
    project = project or find_project(path)
    if project is None:
        return [], os.path.dirname(os.path.abspath(path))
    return parse_project(project), os.path.dirname(os.path.abspath(project))


def project_sources(project_path, cwd):
    """The .v files a _CoqProject lists, or every .v under its -R/-Q roots."""
    files = []
    if project_path:
        with open(project_path) as f:
            for raw in f:
                line = raw.split("#", 1)[0].strip()
                for tok in line.split():
                    if tok.endswith(".v"):
                        files.append(tok)
    if files:
        return files
    for root, _dirs, names in os.walk(cwd):
        for n in names:
            if n.endswith(".v"):
                files.append(os.path.relpath(os.path.join(root, n), cwd))
    return files


def dep_graph(flags, cwd, sources, timeout=300):
    """{target.vo: [dep.vo, ...]} for a whole project, in one `rocq dep` run.

    One call over ~1300 files costs about 1.5s, so this is affordable at cold
    start and gives us the transitive closure rather than just the direct
    `Require`s -- which matters, because Rocq loads the closure into the
    session and any of it going stale must invalidate the session.
    """
    if not sources:
        return {}
    try:
        out = subprocess.run(["rocq", "dep", "-noglob"] + flags + list(sources),
                             cwd=cwd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    graph = {}
    for line in out.stdout.decode("utf8", "replace").splitlines():
        if ":" not in line:
            continue
        lhs, rhs = line.split(":", 1)
        targets = [t for t in lhs.split() if t.endswith(".vo")]
        deps = [os.path.normpath(os.path.join(cwd, t))
                for t in rhs.split() if t.endswith(".vo")]
        for t in targets:
            graph[os.path.normpath(os.path.join(cwd, t))] = deps
    return graph


def closure(path, flags, cwd, graph):
    """Every .vo the file transitively needs, absolute and sorted."""
    rel = os.path.relpath(os.path.abspath(path), cwd)
    seed = os.path.normpath(os.path.join(cwd, rel[:-2] + ".vo"))
    direct = graph.get(seed)
    if direct is None:
        direct = _direct_deps(path, flags, cwd)
        if direct is None:
            return None
    seen, queue = set(), list(direct)
    while queue:
        d = queue.pop()
        if d in seen:
            continue
        seen.add(d)
        queue.extend(graph.get(d, ()))
    return sorted(seen)


def _direct_deps(path, flags, cwd, timeout=120):
    rel = os.path.relpath(os.path.abspath(path), cwd)
    try:
        out = subprocess.run(["rocq", "dep", "-noglob"] + flags + [rel],
                             cwd=cwd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    deps = []
    for line in out.stdout.decode("utf8", "replace").splitlines():
        if ":" in line:
            deps += [os.path.normpath(os.path.join(cwd, t))
                     for t in line.split(":", 1)[1].split() if t.endswith(".vo")]
    return sorted(set(deps))


def fingerprint(deps):
    """(path, mtime_ns, size) per dependency; missing files get None fields.

    Compared verbatim on the next check: any difference means a dependency was
    rebuilt and the warm session is holding a stale library.
    """
    out = []
    for d in deps or ():
        try:
            st = os.stat(d)
            out.append((d, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((d, None, None))
    return out
