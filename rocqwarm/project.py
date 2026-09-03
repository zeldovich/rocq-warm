"""Resolving a .v file's build flags, its dependency graph, and what is stale.

Three jobs, all about making sure a warm session is checking the same thing a
cold `coqc` would:

* turn the nearest `_CoqProject` into the flag list `rocq repl` needs, so the
  session sees exactly the load path and warning settings the build uses;
* keep the project's `.vo` dependency graph current, so that the transitive
  closure a file loads is known before the session starts and re-known after
  every edit -- adding a `Require` mid-session must not create a dependency
  nobody is watching;
* decide which of those `.vo` files `make` would rebuild.  A `.vo` that is
  older than its `.v`, or older than a `.vo` it depends on, is a library that
  no longer matches its source, and checking a proof against it produces
  verdicts about a program that does not exist.  Make's rule is the one the
  build system already applies, so it is the one used here, verbatim.
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


def load_roots(flags, cwd):
    """The physical directories the -R/-Q flags bind, absolute."""
    roots = []
    i = 0
    while i < len(flags):
        if flags[i] in ("-R", "-Q") and i + 2 < len(flags):
            roots.append(os.path.normpath(os.path.join(cwd, flags[i + 1])))
            i += 3
        else:
            i += 1
    return roots


def project_sources(project_path, cwd, flags=None):
    """The .v files a _CoqProject lists, plus every .v under its -R/-Q roots.

    Both, not either: a `_CoqProject` lists the files ITS build compiles, but
    `-R ../model Riscv` makes a whole other tree loadable, and a `.vo` in
    there has dependencies of its own that a session loads just the same.
    Without walking the roots those files appear in the graph only as edges,
    never as nodes, so nothing beneath them is ever watched.  Relative to
    `cwd`, listed files first, then the rest sorted.
    """
    files = []
    seen = set()
    if project_path:
        with open(project_path) as f:
            for raw in f:
                line = raw.split("#", 1)[0].strip()
                for tok in line.split():
                    if tok.endswith(".v"):
                        key = os.path.normpath(os.path.join(cwd, tok))
                        if key not in seen:
                            seen.add(key)
                            files.append(tok)
    roots = load_roots(flags, cwd) if flags is not None else []
    if not files and not roots:
        roots = [cwd]
    walked = []
    for root in roots:
        for d, dirs, names in os.walk(root):
            dirs[:] = sorted(x for x in dirs if not x.startswith("."))
            for n in names:
                if n.endswith(".v"):
                    key = os.path.normpath(os.path.join(d, n))
                    if key not in seen:
                        seen.add(key)
                        walked.append(os.path.relpath(key, cwd))
    return files + sorted(walked)


def _run_dep(flags, cwd, sources, timeout, rocq, env):
    """{target.vo: [dep.vo, ...]} from one `rocq dep` run, or None if it
    could not be run at all.

    `rocq dep` exits without printing ANYTHING when one of the files it is
    handed does not exist, so callers must only hand it files that do -- a
    `_CoqProject` routinely lists generated files that are not there yet.
    """
    if not sources:
        return {}
    try:
        out = subprocess.run([rocq, "dep", "-noglob"] + flags + list(sources),
                             cwd=cwd, capture_output=True, timeout=timeout,
                             env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
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
    if not graph and out.returncode != 0:
        return None
    return graph


def dep_graph(flags, cwd, sources, timeout=300, rocq="rocq", env=None):
    """{target.vo: [dep.vo, ...]} for a whole project, in one `rocq dep` run.

    One call over ~1300 files costs about 1.5s.  Files that do not exist are
    left out rather than handed to `rocq dep`, which would otherwise refuse
    the whole batch.
    """
    present = [s for s in sources if os.path.isfile(os.path.join(cwd, s))]
    return _run_dep(flags, cwd, present, timeout, rocq, env) or {}


def vo_of(v_path):
    return v_path[:-2] + ".vo"


def v_of(vo_path):
    return vo_path[:-3] + ".v"


class DepGraph:
    """A project's `.vo` dependency graph, kept current between checks.

    A full `rocq dep` over a large tree costs a second or two, which is fine
    once and not fine on every edit.  So the graph is refreshed incrementally:
    every source is stat'ed on each refresh (a few milliseconds for a thousand
    files) and only the ones whose mtime moved are re-run through `rocq dep`.
    The file being edited is always among them, which is the point: the
    `Require` lines at the top of a file are edited like anything else, and the
    closure has to follow.
    """

    def __init__(self, flags, cwd, project_path=None, rocq="rocq", env=None,
                 timeout=300):
        self.flags = list(flags)
        self.cwd = cwd
        self.project_path = project_path
        self.rocq, self.env, self.timeout = rocq, env, timeout
        self.graph = {}         # vo -> [vo]
        self.stamps = {}        # abs .v -> mtime_ns
        self.refreshes = 0      # how many `rocq dep` runs, for the tests

    def refresh(self, extra=()):
        """Bring the graph up to date; returns the number of files re-scanned.

        `extra` names .v files to include even if no root or listing covers
        them -- the file being checked is always one, so that a file outside
        the project still gets its own dependencies looked up.
        """
        rel = {}
        for s in project_sources(self.project_path, self.cwd, self.flags):
            rel[os.path.normpath(os.path.join(self.cwd, s))] = s
        for e in extra:
            e = os.path.abspath(e)
            if e not in rel:
                rel[e] = os.path.relpath(e, self.cwd)
        changed = []
        for v_abs, r in rel.items():
            try:
                st = os.stat(v_abs).st_mtime_ns
            except OSError:
                st = None
            if v_abs not in self.stamps or self.stamps[v_abs] != st:
                changed.append((v_abs, r, st))
        for v_abs in [v for v in self.stamps if v not in rel]:
            del self.stamps[v_abs]
            self.graph.pop(vo_of(v_abs), None)
        present = [(v, r, st) for v, r, st in changed if st is not None]
        for v_abs, _r, _st in changed:
            if _st is None:
                self.stamps[v_abs] = None
                self.graph.pop(vo_of(v_abs), None)
        if present:
            patch = _run_dep(self.flags, self.cwd, [r for _v, r, _s in present],
                             self.timeout, self.rocq, self.env)
            self.refreshes += 1
            if patch is None:
                return 0                # could not run it; try again next time
            for v_abs, _r, st in present:
                self.stamps[v_abs] = st
                vo = vo_of(v_abs)
                if vo in patch:
                    self.graph[vo] = patch[vo]
                else:
                    self.graph.pop(vo, None)
        return len(present)

    def direct(self, path):
        """The .vo files `path` requires directly, or None if unknown."""
        return self.graph.get(vo_of(os.path.abspath(path)))

    def closure(self, path):
        """Every .vo `path` transitively loads, absolute and sorted."""
        direct = self.direct(path)
        if direct is None:
            direct = _direct_deps(path, self.flags, self.cwd,
                                  rocq=self.rocq, env=self.env) or []
        seen, queue = set(), list(direct)
        while queue:
            d = queue.pop()
            if d in seen:
                continue
            seen.add(d)
            queue.extend(self.graph.get(d, ()))
        return sorted(seen)


def closure(path, flags, cwd, graph, rocq="rocq", env=None):
    """Every .vo the file transitively needs, absolute and sorted."""
    rel = os.path.relpath(os.path.abspath(path), cwd)
    seed = os.path.normpath(os.path.join(cwd, rel[:-2] + ".vo"))
    direct = graph.get(seed)
    if direct is None:
        direct = _direct_deps(path, flags, cwd, rocq=rocq, env=env)
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


def _direct_deps(path, flags, cwd, timeout=120, rocq="rocq", env=None):
    rel = os.path.relpath(os.path.abspath(path), cwd)
    try:
        out = subprocess.run([rocq, "dep", "-noglob"] + flags + [rel],
                             cwd=cwd, capture_output=True, timeout=timeout,
                             env=env)
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


def _mtime(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def staleness(vo, graph):
    """Why `make` would rebuild `vo`, or None if it would not.

    Make's rule, verbatim: a target is rebuilt when it is missing or older
    than any prerequisite.  The prerequisites of a `.vo` are its `.v` and the
    `.vo` files it requires.  Equal mtimes are up to date, as they are for
    make -- a build that sets the output's mtime to the source's is fine.
    """
    vo_m = _mtime(vo)
    v = v_of(vo)
    v_m = _mtime(v)
    if vo_m is None:
        if v_m is None:
            return "%s does not exist (nor does %s)" % (vo, v)
        return "%s has not been built" % vo
    if v_m is not None and v_m > vo_m:
        return "%s is newer than %s" % (v, vo)
    for d in graph.get(vo, ()):
        d_m = _mtime(d)
        if d_m is not None and d_m > vo_m:
            return "%s is older than %s, which it requires" % (vo, d)
    return None


def stale_deps(closure, graph):
    """[(vo, why)] for every member of `closure` that make would rebuild."""
    out = []
    for vo in closure:
        why = staleness(vo, graph)
        if why:
            out.append((vo, why))
    return out


def rebuild_plan(closure, stale, graph):
    """The .vo files to rebuild, in an order that respects their dependencies.

    Everything stale, plus everything in the closure that depends on
    something stale: once `Base.vo` is rebuilt, `Mid.vo` is older than it and
    make would rebuild that too.  Each entry is (vo, [vo it must wait for]).
    """
    members = set(closure)
    dependents = {}
    for vo in members:
        for d in graph.get(vo, ()):
            if d in members:
                dependents.setdefault(d, []).append(vo)
    todo = set(vo for vo, _why in stale)
    queue = list(todo)
    while queue:
        vo = queue.pop()
        for up in dependents.get(vo, ()):
            if up not in todo:
                todo.add(up)
                queue.append(up)
    order, done = [], set()

    def visit(vo, trail):
        if vo in done:
            return
        if vo in trail:
            return                              # a cycle; rocq will complain
        for d in graph.get(vo, ()):
            if d in todo:
                visit(d, trail | {vo})
        done.add(vo)
        order.append((vo, [d for d in graph.get(vo, ()) if d in todo]))

    for vo in sorted(todo):
        visit(vo, frozenset())
    return order
