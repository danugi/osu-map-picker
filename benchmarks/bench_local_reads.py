"""Reproducible microbenchmarks for low-risk storage/worker optimizations.

Run from the repository root after installing development requirements:

    python benchmarks/bench_local_reads.py --count 800 --repeat 3

The script uses synthetic SQLite data only: no network and no user database.
Each measurement includes one warm-up followed by a median of repeated runs.
The cached-attributes test measures already stored values; it does not include
network latency or first-time API retrieval.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from conftest import make_beatmap, make_beatmapset

import library
from storage import Storage


def _median(fn, repeat: int = 3) -> float:
    """Return median elapsed time after one warm-up call."""
    if repeat < 1:
        raise ValueError("repeat must be >= 1")
    fn()
    values: list[float] = []
    for _ in range(repeat):
        started = time.perf_counter()
        fn()
        values.append(time.perf_counter() - started)
    return statistics.median(values)


def make_workload(
    root: Path, count: int
) -> tuple[Storage, list[int], list[int]]:
    store = Storage(root / "bench.db")
    sets = []
    for index in range(1, count + 1):
        beatmaps = tuple(
            make_beatmap(
                index * 10 + offset + 1,
                index,
                stars=4.0 + (index % 20) / 10,
                version=f"Diff {offset}",
            )
            for offset in range(3)
        )
        sets.append(make_beatmapset(index, beatmaps=beatmaps))
    store.save_beatmapsets(sets)
    set_ids = [item.id for item in sets]
    beatmap_ids = [beatmap.id for item in sets for beatmap in item.beatmaps]
    for beatmap_id in beatmap_ids:
        store.save_attributes(
            beatmap_id,
            {
                "star_rating": 5.1,
                "aim_difficulty": 3.0,
                "speed_difficulty": 1.2,
                "slider_factor": 1.0,
                "max_combo": 500,
                "source": "benchmark",
                "fetched_at": "2026-01-01T00:00:00+00:00",
            },
        )
    return store, set_ids, beatmap_ids


def old_get_beatmapsets(store: Storage, ids: list[int]) -> dict[int, Any]:
    result: dict[int, Any] = {}
    for set_id in dict.fromkeys(ids):
        beatmapset = store.get_beatmapset(set_id)
        if beatmapset is not None:
            result[set_id] = beatmapset
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=800)
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be >= 1")
    if args.repeat < 1:
        parser.error("--repeat must be >= 1")
    count = args.count

    with tempfile.TemporaryDirectory(prefix="osu-picker-bench-") as folder:
        store, set_ids, beatmap_ids = make_workload(Path(folder), count)
        selected_sets = set_ids[: min(500, len(set_ids))]
        selected_beatmaps = beatmap_ids[: min(300, len(beatmap_ids))]
        store.enqueue_analysis([(bid, 100, "benchmark") for bid in beatmap_ids])

        timings: dict[str, float] = {}
        timings["beatmapsets_old"] = _median(
            lambda: old_get_beatmapsets(store, selected_sets), args.repeat
        )
        timings["beatmapsets_new"] = _median(
            lambda: store.get_beatmapsets(selected_sets), args.repeat
        )
        timings["attributes_old"] = _median(
            lambda: [store.get_attributes(bid) for bid in selected_beatmaps],
            args.repeat,
        )
        timings["attributes_batch"] = _median(
            lambda: store.all_attributes(selected_beatmaps), args.repeat
        )
        filters = library.LibraryFilters(scope="all", per_page=48, page=1)
        timings["library_counts"] = _median(
            lambda: library.query_sets(store, filters, include_counts=True),
            args.repeat,
        )
        timings["library_no_counts"] = _median(
            lambda: library.query_sets(store, filters, include_counts=False),
            args.repeat,
        )

        def full_queue_rows() -> list[Any]:
            with store.connection() as conn:
                return conn.execute("SELECT * FROM analysis_queue").fetchall()

        def narrow_queue_rows() -> list[Any]:
            with store.connection() as conn:
                return conn.execute(
                    "SELECT beatmap_id, status, attempts, max_attempts "
                    "FROM analysis_queue"
                ).fetchall()

        timings["queue_old"] = _median(full_queue_rows, args.repeat)
        timings["queue_narrow"] = _median(narrow_queue_rows, args.repeat)

        print(
            f"dataset: {count} beatmapsets, {count * 3} beatmaps, "
            f"{len(selected_beatmaps)} selected beatmaps"
        )
        print(f"methodology: one warm-up + median of {args.repeat} repeats")
        for name, value in timings.items():
            print(f"{name:24s} {value * 1000:9.2f} ms")
        for old, new in (
            ("beatmapsets_old", "beatmapsets_new"),
            ("attributes_old", "attributes_batch"),
            ("library_counts", "library_no_counts"),
            ("queue_old", "queue_narrow"),
        ):
            if timings[new] > 0:
                print(
                    f"{old} -> {new}: "
                    f"{timings[old] / timings[new]:.2f}x"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
