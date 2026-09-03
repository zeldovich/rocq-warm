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


class StalenessRuleTests(unittest.TestCase):
    """Make's rule, and only make's rule."""

    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.cleanup)

    def stamp(self, name, t):
        os.utime(self.ws.path(name), ns=(t, t))

    def test_missing_vo(self):
        self.ws.write("A.v", b"")
        self.assertEqual(project.staleness(self.ws.path("A.vo"), {}),
                         "%s has not been built" % self.ws.path("A.vo"))

    def test_missing_everything(self):
        self.assertIn("does not exist",
                      project.staleness(self.ws.path("A.vo"), {}))

    def test_source_newer(self):
        self.ws.write("A.v", b""); self.ws.write("A.vo", b"")
        self.stamp("A.vo", 1_000); self.stamp("A.v", 2_000)
        self.assertIn("A.v is newer than", project.staleness(self.ws.path("A.vo"), {}))

    def test_equal_is_current(self):
        self.ws.write("A.v", b""); self.ws.write("A.vo", b"")
        self.stamp("A.vo", 1_000); self.stamp("A.v", 1_000)
        self.assertIsNone(project.staleness(self.ws.path("A.vo"), {}))

    def test_older_than_a_requirement(self):
        for n in ("A.v", "A.vo", "B.vo"):
            self.ws.write(n, b"")
        self.stamp("A.v", 1_000); self.stamp("A.vo", 2_000); self.stamp("B.vo", 3_000)
        graph = {self.ws.path("A.vo"): [self.ws.path("B.vo")]}
        why = project.staleness(self.ws.path("A.vo"), graph)
        self.assertIn("A.vo is older than", why)
        self.assertIn("B.vo", why)

    def test_a_missing_requirement_is_its_own_problem(self):
        """B.vo missing is reported for B.vo (it is in the closure too), not
        as A.vo being out of date."""
        self.ws.write("A.v", b""); self.ws.write("A.vo", b"")
        self.stamp("A.v", 1_000); self.stamp("A.vo", 2_000)
        graph = {self.ws.path("A.vo"): [self.ws.path("B.vo")]}
        self.assertIsNone(project.staleness(self.ws.path("A.vo"), graph))

    def test_rebuild_plan_cascades_and_orders(self):
        a, b, c, d = (self.ws.path(n) for n in ("A.vo", "B.vo", "C.vo", "D.vo"))
        graph = {c: [b], b: [a], d: [a]}          # C -> B -> A, D -> A
        plan = project.rebuild_plan([a, b, c, d], [(a, "old")], graph)
        order = [vo for vo, _after in plan]
        self.assertEqual(set(order), {a, b, c, d}, "everything above A is stale too")
        self.assertLess(order.index(a), order.index(b))
        self.assertLess(order.index(b), order.index(c))
        self.assertLess(order.index(a), order.index(d))
        after = dict(plan)
        self.assertEqual(after[c], [b])
        self.assertEqual(after[a], [])

    def test_rebuild_plan_leaves_unrelated_members_alone(self):
        a, b, z = (self.ws.path(n) for n in ("A.vo", "B.vo", "Z.vo"))
        graph = {b: [a]}
        plan = project.rebuild_plan([a, b, z], [(a, "old")], graph)
        self.assertEqual({vo for vo, _ in plan}, {a, b})


class SourceDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.cleanup)

    def test_files_under_a_root_are_found_even_when_not_listed(self):
        os.makedirs(self.ws.path("lib/deep"))
        os.makedirs(self.ws.path(".hidden"))
        self.ws.write("Listed.v", b"")
        self.ws.write("lib/deep/Unlisted.v", b"")
        self.ws.write(".hidden/Skipped.v", b"")
        proj = self.ws.path("_CoqProject")
        with open(proj, "w") as f:
            f.write("-R . T\n-R lib L\nListed.v\n")
        got = project.project_sources(proj, self.ws.dir, ["-R", ".", "T", "-R", "lib", "L"])
        self.assertEqual(got[0], "Listed.v")
        self.assertIn("lib/deep/Unlisted.v", got)
        self.assertEqual(got.count("lib/deep/Unlisted.v"), 1, "roots overlap")
        self.assertNotIn(".hidden/Skipped.v", got)

    def test_load_roots(self):
        self.assertEqual(
            project.load_roots(["-R", ".", "T", "-Q", "../m", "M", "-w", "-all"],
                               "/x/y"),
            ["/x/y", "/x/m"])


@requires_rocq
class DepGraphTests(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.cleanup)
        self.ws.write("Base.v", b"Definition base := 1.\n")
        self.ws.write("Mid.v", b"Require Import T.Base.\nDefinition mid := base.\n")
        self.ws.write("Top.v", b"Definition top := 1.\n")
        self.proj = self.ws.path("_CoqProject")
        with open(self.proj, "w") as f:
            f.write("-R . T\nBase.v\nMid.v\nTop.v\nNotYet.v\n")
        self.g = project.DepGraph(self.ws.flags, self.ws.dir, self.proj)

    def test_first_refresh_scans_everything_that_exists(self):
        self.assertEqual(self.g.refresh(), 3)
        self.assertEqual(self.g.refreshes, 1)
        self.assertEqual(self.g.closure(self.ws.path("Mid.v")),
                         [self.ws.path("Base.vo")])
        self.assertEqual(self.g.closure(self.ws.path("Top.v")), [])

    def test_a_second_refresh_runs_nothing(self):
        self.g.refresh()
        self.assertEqual(self.g.refresh(), 0)
        self.assertEqual(self.g.refreshes, 1)

    def test_an_edit_rescans_only_that_file(self):
        self.g.refresh()
        self.ws.touch("Top.v", b"Require Import T.Mid.\nDefinition top := mid.\n")
        self.assertEqual(self.g.refresh(), 1)
        self.assertEqual(self.g.refreshes, 2)
        self.assertEqual(self.g.closure(self.ws.path("Top.v")),
                         sorted([self.ws.path("Base.vo"), self.ws.path("Mid.vo")]))

    def test_a_new_file_is_picked_up(self):
        self.g.refresh()
        self.ws.write("NotYet.v", b"Require Import T.Base.\n")
        self.assertEqual(self.g.refresh(), 1)
        self.assertEqual(self.g.closure(self.ws.path("NotYet.v")),
                         [self.ws.path("Base.vo")])

    def test_a_deleted_file_is_dropped(self):
        self.g.refresh()
        os.unlink(self.ws.path("Mid.v"))
        self.g.refresh()
        self.assertNotIn(self.ws.path("Mid.vo"), self.g.graph)

    def test_a_file_outside_the_project_is_scanned_on_request(self):
        outside = Workspace()
        self.addCleanup(outside.cleanup)
        path = outside.write("Elsewhere.v", b"Require Import T.Mid.\n")
        self.g.refresh(extra=[path])
        self.assertEqual(self.g.closure(path),
                         sorted([self.ws.path("Base.vo"), self.ws.path("Mid.vo")]))

    def test_dep_graph_skips_listed_files_that_do_not_exist(self):
        graph = project.dep_graph(self.ws.flags, self.ws.dir,
                                  ["Base.v", "NotYet.v", "Mid.v"])
        self.assertIn(self.ws.path("Mid.vo"), graph,
                      "one missing file must not blank the whole graph")


if __name__ == "__main__":
    unittest.main()
