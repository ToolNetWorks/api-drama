"""Cross-process (multi-worker) coordination via the Turso lease.

Each worker is a separate OS process with its own service, connection pool and event loop,
sharing only the database file (the Turso test double). This demonstrates the lease logic
under the tested conditions; it does NOT prove exactly-once in general (see
docs/EPISODE_CACHE.md for the documented limits).
"""
from __future__ import annotations

import multiprocessing as mp
import time

import pytest

from tests.ec_helpers import sql
from tests.ec_worker import run_worker


def _run(db, counter, keys, workers=4, per_worker=5, delay=0.5):
    ctx = mp.get_context("spawn")
    start_at = time.time() + 2.0  # all processes released together
    jobs = [(db, "series", k, counter, start_at, per_worker, delay)
            for k in keys for _ in range(workers)]
    with ctx.Pool(processes=min(len(jobs), 8)) as pool:
        return pool.map(run_worker, jobs)


def test_multi_process_workers_share_one_upstream_call_per_key(tmp_path):
    db = str(tmp_path / "shared.db")
    counter = str(tmp_path / "upstream_calls.log")
    open(counter, "w").close()
    keys = ["ep_a", "ep_b", "ep_c"]
    results = _run(db, counter, keys)
    lines = [ln for ln in open(counter).read().splitlines() if ln]
    per_key = {k: sum(1 for ln in lines if ln.split(":")[2] == k) for k in keys}
    # 4 processes x 5 concurrent requests per key = 20 customers per key
    assert per_key == {"ep_a": 1, "ep_b": 1, "ep_c": 1}, per_key
    flat = [r for res in results for r in res]
    assert len(flat) == 4 * 5 * len(keys)
    assert sum(1 for hit, _ in flat if not hit) == len(keys)  # exactly one MISS per key
    assert all(src == "cache:turso" for hit, src in flat if hit)
    assert sql(db, "SELECT COUNT(*) FROM episode_cache")[0][0] == len(keys)
    assert sql(db, "SELECT COUNT(*) FROM episode_cache_locks")[0][0] == 0


def test_second_wave_after_all_workers_exit_is_all_hits(tmp_path):
    """Simulates container restart: fresh processes, same Turso data, zero upstream calls."""
    db = str(tmp_path / "shared2.db")
    counter = str(tmp_path / "calls2.log")
    open(counter, "w").close()
    _run(db, counter, ["ep_x"], workers=2, per_worker=2, delay=0.2)
    first = len([ln for ln in open(counter).read().splitlines() if ln])
    results = _run(db, counter, ["ep_x"], workers=3, per_worker=3, delay=0.2)
    after = len([ln for ln in open(counter).read().splitlines() if ln])
    assert first == 1 and after == 1
    assert all(hit for res in results for hit, _ in res)
