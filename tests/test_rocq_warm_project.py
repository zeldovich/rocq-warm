"""Build flags and dependency fingerprints.

A warm session holds the `.vo` files Rocq loaded into memory when it started.
If one of them is rebuilt underneath it, every later answer is about a library
that no longer exists on disk -- so the fingerprint is the difference between a
cache and a liar.
"""

import os
import subprocess
import time
import unittest

from rocq_warm_helpers import Workspace, requires_rocq
from rocqwarm import project


class ProjectFileTests(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.cleanup)

    def write_project(self, body):
        path = os.path.join(self.ws.dir, "_CoqProject")
        with open(path, "w") as f:
            f.write(body)
        return path

    def test_load_path_and_args(self):
        path = self.write_project(
            "-R . MyProj\n"
            "-Q ../model Riscv\n"
            "# a comment line\n"
            "-arg -w\n"
            "-arg -notation-overridden\n"
            "Foo.v\n"
            "Bar.v\n")
        self.assertEqual(project.parse_project(path),
                         ["-R", ".", "MyProj", "-Q", "../model", "Riscv",
                          "-w", "-notation-overridden"])

    def test_source_files_are_not_flags(self):
        path = self.write_project("-R . T\nA.v\nB.v\n")
        self.assertEqual(project.project_sources(path, self.ws.dir), ["A.v", "B.v"])

    def test_trailing_comment_on_a_directive(self):
        path = self.write_project("-R . T   # the root\n")
        self.assertEqual(project.parse_project(path), ["-R", ".", "T"])

    def test_project_is_found_from_a_subdirectory(self):
        sub = os.path.join(self.ws.dir, "a", "b")
        os.makedirs(sub)
        found = project.find_project(os.path.join(sub, "X.v"))
        self.assertEqual(found, os.path.join(self.ws.dir, "_CoqProject"))

    def test_fingerprint_notices_a_rewrite(self):
        f = os.path.join(self.ws.dir, "x.vo")
        with open(f, "wb") as fh:
            fh.write(b"one")
        first = project.fingerprint([f])
        time.sleep(0.01)
        with open(f, "wb") as fh:
            fh.write(b"two")
        self.assertNotEqual(first, project.fingerprint([f]))

    def test_fingerprint_tolerates_a_missing_file(self):
        f = os.path.join(self.ws.dir, "gone.vo")
        self.assertEqual(project.fingerprint([f]), [(f, None, None)])


@requires_rocq
class DependencyClosureTests(unittest.TestCase):
    """`rocq dep` gives direct requires; the session loads the closure."""

    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.cleanup)
        self.ws.write("Base.v", b"Definition base := 1.\n")
        self.ws.write("Mid.v", b"Require Import T.Base.\nDefinition mid := base.\n")
        self.ws.write("Top.v", b"Require Import T.Mid.\nDefinition top := mid.\n")
        with open(os.path.join(self.ws.dir, "_CoqProject"), "w") as f:
            f.write("-R . T\nBase.v\nMid.v\nTop.v\n")
        for name in ("Base.v", "Mid.v", "Top.v"):
            subprocess.run(["coqc", "-q"] + self.ws.flags + [name],
                           cwd=self.ws.dir, check=True, capture_output=True)

    def test_closure_reaches_indirect_dependencies(self):
        flags, cwd = project.flags_for(os.path.join(self.ws.dir, "Top.v"))
        graph = project.dep_graph(
            flags, cwd, project.project_sources(
                os.path.join(cwd, "_CoqProject"), cwd))
        deps = project.closure(os.path.join(cwd, "Top.v"), flags, cwd, graph)
        names = {os.path.basename(d) for d in deps}
        self.assertIn("Mid.vo", names)
        self.assertIn("Base.vo", names,
                      "the closure must reach Base.vo through Mid.vo, or a "
                      "rebuild of Base would not invalidate a Top session")


if __name__ == "__main__":
    unittest.main()
