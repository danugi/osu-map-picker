"""Reproducible local throughput benchmark for the analysis worker.

Run from the repository root:

    .\\.venv\\Scripts\\python.exe benchmarks\\bench_analysis_worker.py

It creates a temporary SQLite catalog with 32 cached ``.osu`` files. The
second measurement simulates network latency (120 ms per analysis call) so
sequential and bounded-concurrent processing can be compared. No network is
used.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from conftest import make_beatmap, make_beatmapset
from test_patterns import make_osu

import analysis
import analysis_worker
from storage import Storage

COUNT = 32
LATENCY = 0.12


def make_workload(root: Path, count: int) -> tuple[Path, list[int]]:
    db = root / "bench.db"
    cache = root / "gameplay_cache"
    cache.mkdir(parents=True, exist_ok=True)
    store = Storage(db)
    ids: list[int] = []
    for index in range(1, count + 1):
        beatmap = make_beatmap(index * 10 + 1, index)
        store.save_beatmapset(
            make_beatmapset(index, beatmaps=(beatmap,))
        )
        (cache / f"{beatmap.id}.osu").write_text(
            make_osu(
                [
                    (1000 + note * 125, 100 + (note % 2) * 60, 100)
                    for note in range(12)
                ]
            ),
            encoding="utf-8",
        )
        ids.append(beatmap.id)
    return db, ids


def reset_queue(store: Storage, ids: list[int]) -> None:
    with store.connection() as conn:
        conn.execute("DELETE FROM analysis_queue")
        conn.execute("DELETE FROM beatmap_analysis")
    analysis_worker.enqueue_catalog(store, allow_download=False)


def timed_analyze(original, latency: float):
    def wrapped(*args, **kwargs):
        time.sleep(latency)
        return original(*args, **kwargs)

    return wrapped


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=COUNT)
    parser.add_argument("--latency", type=float, default=LATENCY)
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    os.environ["OSU_PICKER_ANALYSIS_CONCURRENCY"] = str(
        max(1, min(8, args.concurrency))
    )
    root = ROOT / ".worker_bench"
    if root.exists():
        import shutil

        shutil.rmtree(root)
    root.mkdir()
    db, ids = make_workload(root, args.count)
    store = Storage(db)

    print(
        "workload",
        args.count,
        "cached maps, simulated latency",
        args.latency,
        "concurrency",
        args.concurrency,
        flush=True,
    )

    reset_queue(store, ids)
    started = time.perf_counter()
    processed = analysis_worker.process_once(
        store, limit=args.count, allow_download=False
    )
    local = time.perf_counter() - started
    print("local_cached_seconds", round(local, 3), "processed", processed, flush=True)

    original = analysis.analyze_beatmap
    analysis.analyze_beatmap = timed_analyze(original, args.latency)
    try:
        reset_queue(store, ids)
        started = time.perf_counter()
        processed = analysis_worker.process_once(
            store, limit=args.count, allow_download=False
        )
        simulated = time.perf_counter() - started
    finally:
        analysis.analyze_beatmap = original
    print(
        "simulated_latency_seconds",
        round(simulated, 3),
        "processed",
        processed,
        "maps_per_second",
        round(processed / simulated, 2) if simulated else 0,
        flush=True,
    )
    import shutil

    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
