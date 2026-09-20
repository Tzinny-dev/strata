"""Writer concurrency: a per-module lock (`strata.exec._module_lock`)
serializing everything that reads or writes a module's history/manifest.

Real bug this closes: `record_run`/`save_manifest` (strata/exec.py) do a
plain read-modify-write over plain files (read the whole history, append,
overwrite). Two processes racing lose one writer's entry outright — DuckDB
hides this by accident (opening the same .duckdb file twice normally just
fails on its own), Postgres does not.

Uses `threading`, not `multiprocessing`/real subprocesses: each thread's
call to `_module_lock` does its own `os.open()` of the same lock file, and
POSIX `flock` enforces real mutual exclusion across distinct file
descriptors even within one process — no need to orchestrate real OS
processes to exercise this honestly.
"""
import tempfile
import threading
import time
import unittest
from pathlib import Path

from strata.exec import _module_lock

try:
    import duckdb
    HAVE_DUCKDB = True
except ImportError:
    HAVE_DUCKDB = False


class TestModuleLockMechanism(unittest.TestCase):
    """The primitive itself, independent of exec.py's higher-level use."""

    def test_locked_increments_are_never_lost(self):
        d = tempfile.mkdtemp()
        module = str(Path(d) / "m.strata")
        counter_file = Path(d) / "counter.txt"
        counter_file.write_text("0")

        def bump():
            with _module_lock(module):
                n = int(counter_file.read_text())
                time.sleep(0.01)  # widen the read-modify-write window
                counter_file.write_text(str(n + 1))

        threads = [threading.Thread(target=bump) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(int(counter_file.read_text()), 20)

    def test_negative_control_the_same_pattern_loses_updates_unlocked(self):
        """Proves the sleep-widened window above is a real race, not a
        false sense of safety from thread scheduling or the GIL: the exact
        same pattern, minus the lock, reliably loses updates."""
        d = tempfile.mkdtemp()
        counter_file = Path(d) / "counter.txt"
        counter_file.write_text("0")

        def bump_unlocked():
            n = int(counter_file.read_text())
            time.sleep(0.01)
            counter_file.write_text(str(n + 1))

        threads = [threading.Thread(target=bump_unlocked) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertLess(int(counter_file.read_text()), 20)


@unittest.skipUnless(HAVE_DUCKDB, "duckdb not available (use the venv interpreter)")
class TestRunSerializedAcrossThreads(unittest.TestCase):
    """exec.run() end to end: N concurrent publications, none lost from
    history. Each thread uses a distinct `reason` so its entry is
    content-addressed to a distinct run_id regardless of timing — the test
    doesn't depend on controlling how the threads actually interleave."""

    def test_no_lost_history_entries_under_concurrent_publications(self):
        from strata.analysis import Checker, Project
        from strata.parser import parse_strata
        from strata import exec as ex

        d = tempfile.mkdtemp()
        module = str(Path(d) / "m.strata")
        text = 'source s(ns: "n", dataset: "s") { columns: { id: int64 } }\nmodel m { from s }\n'
        Path(module).write_text(text)
        con = duckdb.connect(str(Path(d) / "w.duckdb"))
        con.execute("CREATE TABLE s (id BIGINT)")
        con.execute("INSERT INTO s VALUES (0)")

        proj = Project(parse_strata(text, module))
        tms = Checker(proj).check_all()

        errors = []
        n = 10

        def worker(i):
            try:
                ex.run(con, proj, tms, module, only_stale=False, stage_only=True,
                      reason=f"concurrent-{i}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        history = ex.load_history(module)
        self.assertEqual(len(history), n)
        self.assertEqual({e["reason"] for e in history},
                         {f"concurrent-{i}" for i in range(n)})


@unittest.skipUnless(HAVE_DUCKDB, "duckdb not available (use the venv interpreter)")
class TestGcSerializedWithLock(unittest.TestCase):
    def test_gc_snapshots_blocks_while_another_holder_has_the_lock(self):
        from strata import exec as ex

        d = tempfile.mkdtemp()
        module = str(Path(d) / "m.strata")
        Path(module).write_text(
            'source s(ns: "n", dataset: "s") { columns: { id: int64 } }\nmodel m { from s }\n')

        def hold_lock():
            with _module_lock(module):
                time.sleep(0.3)

        holder = threading.Thread(target=hold_lock)
        holder.start()
        time.sleep(0.05)  # let the holder acquire first

        con = duckdb.connect()
        start = time.monotonic()
        ex.gc_snapshots(con, module, keep=2)  # empty history: a no-op plan
        duration = time.monotonic() - start
        holder.join()

        self.assertGreaterEqual(
            duration, 0.2,
            "gc_snapshots must block on the module lock instead of racing "
            "the concurrent holder")


if __name__ == "__main__":
    unittest.main()
