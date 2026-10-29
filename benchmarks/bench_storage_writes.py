"""Write-path benchmark: legacy per-row UPSERT vs bounded executemany batches.

Run from the repository root:

    .\\.venv\\Scripts\\python.exe benchmarks\\bench_storage_writes.py --count 300

Temporary SQLite databases only. Both implementations use the same SQL,
values and transaction boundary; the legacy path executes one row at a time.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from conftest import make_beatmap, make_beatmapset

import storage as storage_module
from storage import Storage


def _set_values(beatmapset):
    return (
        beatmapset.id,
        beatmapset.artist,
        beatmapset.artist_unicode,
        beatmapset.title,
        beatmapset.title_unicode,
        beatmapset.creator,
        beatmapset.status,
        beatmapset.bpm,
        beatmapset.favourite_count,
        beatmapset.play_count,
        beatmapset.rating,
        beatmapset.genre_id,
        beatmapset.language_id,
        json.dumps(list(beatmapset.tags), ensure_ascii=False),
        int(beatmapset.nsfw),
        int(beatmapset.video),
        int(beatmapset.storyboard),
        beatmapset.preview_url,
        beatmapset.cover_url,
        beatmapset.ranked_date,
        beatmapset.submitted_date,
        beatmapset.last_updated,
        beatmapset.source,
        beatmapset.fetched_at,
    )


def _map_values(beatmap, now: str):
    return (
        beatmap.id,
        beatmap.beatmapset_id,
        beatmap.mode,
        beatmap.version,
        beatmap.difficulty_rating,
        beatmap.total_length,
        beatmap.hit_length,
        beatmap.bpm,
        beatmap.ar,
        beatmap.od,
        beatmap.cs,
        beatmap.hp,
        beatmap.max_combo,
        beatmap.count_circles,
        beatmap.count_sliders,
        beatmap.count_spinners,
        beatmap.status,
        beatmap.url,
        json.dumps(list(beatmap.owners), ensure_ascii=False),
        now,
    )


def legacy_save(store: Storage, beatmapsets) -> int:
    """Faithful reproduction of the previous per-row implementation."""

    saved = 0
    with store.connection() as conn:
        for beatmapset in beatmapsets:
            conn.execute(storage_module._BEATMAPSET_UPSERT_SQL, _set_values(beatmapset))
            for beatmap in beatmapset.beatmaps:
                conn.execute(
                    storage_module._BEATMAP_UPSERT_SQL,
                    _map_values(beatmap, storage_module.now_iso()),
                )
            saved += 1
    return saved


def workload(count: int, *, updated: bool = False):
    fetched = "2026-08-01T00:00:00+00:00"
    if updated:
        fetched = "2026-09-01T00:00:00+00:00"
    sets = []
    for index in range(1, count + 1):
        beatmaps = tuple(
            make_beatmap(
                index * 10 + offset + 1,
                index,
                stars=4.0 + (index % 20) / 10 + (0.5 if updated else 0.0),
                version=f"Diff {offset}{' updated' if updated else ''}",
            )
            for offset in range(3)
        )
        sets.append(
            make_beatmapset(
                index,
                title=f"{'Updated' if updated else 'Bench'} Set {index}",
                fetched_at=fetched,
                beatmaps=beatmaps,
            )
        )
    return sets


def dump(store: Storage):
    with store.connection() as conn:
        sets = [
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM beatmapsets ORDER BY id"
            ).fetchall()
        ]
        maps = [
            tuple(row)
            for row in conn.execute(
                "SELECT id, beatmapset_id, mode, version, difficulty_rating, "
                "total_length, hit_length, bpm, ar, od, cs, hp, max_combo, "
                "count_circles, count_sliders, count_spinners, status, url, "
                "owners FROM beatmaps ORDER BY id"
            ).fetchall()
        ]
    return sets, maps


def peak_allocation(impl, data) -> int:
    with tempfile.TemporaryDirectory(prefix="osu-picker-write-peak-") as folder:
        store = Storage(Path(folder) / "bench.db")
        tracemalloc.start()
        impl(store, data)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return int(peak)


def timed(impl, data, *, seed=None, repeat=3):
    values = []
    dumps = []
    for _ in range(repeat + 1):
        with tempfile.TemporaryDirectory(prefix="osu-picker-write-bench-") as folder:
            store = Storage(Path(folder) / "bench.db")
            if seed is not None:
                seed(store)
            started = time.perf_counter()
            impl(store, data)
            values.append(time.perf_counter() - started)
            dumps.append(dump(store))
    # warm-up is discarded
    return statistics.median(values[1:]), dumps[-1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=300)
    parser.add_argument("--larger", type=int, default=1200)
    args = parser.parse_args()
    count = max(1, args.count)
    larger = max(count, args.larger)
    insert_data = workload(count)
    update_data = workload(count, updated=True)

    old_insert, old_insert_dump = timed(legacy_save, insert_data)
    new_insert, new_insert_dump = timed(Storage.save_beatmapsets, insert_data)
    if old_insert_dump != new_insert_dump:
        raise SystemExit("insert contents differ between implementations")

    def seed(store):
        legacy_save(store, insert_data)

    old_update, old_update_dump = timed(
        legacy_save, update_data, seed=seed
    )
    new_update, new_update_dump = timed(
        Storage.save_beatmapsets, update_data, seed=seed
    )
    if old_update_dump != new_update_dump:
        raise SystemExit("update contents differ between implementations")

    larger_data = workload(larger)
    old_large, _ = timed(legacy_save, larger_data, repeat=1)
    new_large, _ = timed(Storage.save_beatmapsets, larger_data, repeat=1)
    old_peak = peak_allocation(legacy_save, larger_data)
    new_peak = peak_allocation(Storage.save_beatmapsets, larger_data)

    print(f"insert dataset: {count} sets, {count * 3} difficulties")
    print(f"update dataset: {count} sets, {count * 3} difficulties")
    print(f"larger dataset: {larger} sets, {larger * 3} difficulties")
    print("methodology: temp DB, one warm-up discarded, median of 3 (large: 1)")
    for label, old, new in (
        ("insert", old_insert, new_insert),
        ("upsert", old_update, new_update),
        ("larger insert", old_large, new_large),
    ):
        print(
            f"{label:14s} old={old * 1000:8.2f} ms new={new * 1000:8.2f} ms "
            f"speedup={old / new:.2f}x"
        )
    print(
        f"larger Python peak allocation old={old_peak / 1024 / 1024:.2f} MiB "
        f"new={new_peak / 1024 / 1024:.2f} MiB"
    )
    print("content check: equivalent beatmapsets/beatmaps rows (updated_at excluded)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
