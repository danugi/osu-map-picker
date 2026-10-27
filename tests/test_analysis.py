from __future__ import annotations

import os

from conftest import make_beatmap, make_beatmapset

import analysis
import evidence
import gameplay
import patterns
from models import SequenceAnalysis
from storage import Storage
from test_patterns import make_osu


def _save_set(store: Storage, set_id: int = 1) -> int:
    beatmap = make_beatmap(set_id * 10 + 1, set_id)
    store.save_beatmapset(make_beatmapset(set_id, beatmaps=(beatmap,)))
    return beatmap.id


def _write_cache(store: Storage, beatmap_id: int, text: str) -> None:
    cache = analysis.cache_dir_for(store)
    cache.mkdir(parents=True, exist_ok=True)
    (cache / f"{beatmap_id}.osu").write_text(text, encoding="utf-8")


def test_analysis_roundtrip_and_cache_reuse(storage: Storage, monkeypatch) -> None:
    beatmap_id = _save_set(storage)
    _write_cache(
        storage,
        beatmap_id,
        make_osu([(1000 + index * 125, 100, 100) for index in range(12)]),
    )
    calls = 0
    real = patterns.analyze_sequences

    def counting(preview):
        nonlocal calls
        calls += 1
        return real(preview)

    monkeypatch.setattr(patterns, "analyze_sequences", counting)
    first = analysis.analyze_beatmap(storage, beatmap_id, allow_download=False)
    assert first.usable
    assert first.status == evidence.MEASURED
    assert first.source == "cache"
    assert first.sequence is not None
    assert "streams" in first.sequence.labels
    assert calls == 1
    row = storage.get_analysis(beatmap_id)
    assert row is not None and row["status"] == evidence.MEASURED

    second = analysis.analyze_beatmap(storage, beatmap_id, allow_download=False)
    assert second.sequence == first.sequence
    assert calls == 1

    _write_cache(
        storage,
        beatmap_id,
        make_osu([(2000 + index * 600, 100, 100) for index in range(12)]),
    )
    path = analysis.cache_dir_for(storage) / f"{beatmap_id}.osu"
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    third = analysis.analyze_beatmap(storage, beatmap_id, allow_download=False)
    assert calls == 2
    assert third.sequence is not None
    assert not third.sequence.stream_chains


def test_analysis_json_roundtrip() -> None:
    original = SequenceAnalysis(
        analyzed_notes=10,
        total_notes=12,
        truncated=True,
        jump_share=0.4,
        wide_jump_share=0.2,
        labels=("wide_aim",),
        analyzer_version=2,
    )
    restored = analysis.sequence_from_json(analysis.sequence_to_json(original))
    assert restored == original


def test_missing_file_is_source_unavailable_not_negative(storage: Storage) -> None:
    beatmap_id = _save_set(storage)
    peeked = analysis.peek_analysis(storage, beatmap_id)
    assert peeked.status == evidence.SOURCE_UNAVAILABLE
    result = analysis.analyze_beatmap(storage, beatmap_id, allow_download=False)
    assert result.status == evidence.SOURCE_UNAVAILABLE
    assert result.sequence is None


def test_download_and_cache_in_one_batch(storage: Storage) -> None:
    beatmap_id = _save_set(storage)
    payload = make_osu(
        [(1000 + index * 125, 100, 100) for index in range(12)]
    ).encode("utf-8")
    result = analysis.analyze_beatmap(
        storage,
        beatmap_id,
        allow_download=True,
        fetcher=lambda url: payload,
    )
    assert result.usable
    assert result.source == "download"
    path = analysis.cache_dir_for(storage) / f"{beatmap_id}.osu"
    assert path.exists()
    again = analysis.analyze_beatmap(
        storage,
        beatmap_id,
        allow_download=True,
        fetcher=lambda url: (_ for _ in ()).throw(AssertionError("download again")),
    )
    assert again.usable
    assert again.source in {"cache", "download"}


def test_failed_analysis_is_bounded_and_not_retried_forever(
    storage: Storage,
) -> None:
    beatmap_id = _save_set(storage)
    _write_cache(storage, beatmap_id, "not an osu file")
    first = analysis.analyze_beatmap(storage, beatmap_id, allow_download=False)
    assert first.status == evidence.FAILED
    second = analysis.analyze_beatmap(storage, beatmap_id, allow_download=False)
    assert second.status == evidence.FAILED
    # A failed row is returned as failed; it must not be parsed repeatedly.
    assert second.attempts == first.attempts
    third = analysis.analyze_beatmap(storage, beatmap_id, allow_download=False)
    assert third.status == evidence.FAILED
    assert third.attempts == second.attempts


def test_batch_analyze_reports_analyzed_skipped_and_remaining(
    storage: Storage,
) -> None:
    ids = []
    for set_id in (1, 2, 3):
        beatmap_id = _save_set(storage, set_id)
        ids.append(beatmap_id)
    _write_cache(
        storage,
        ids[0],
        make_osu([(1000 + index * 300, 100, 100) for index in range(8)]),
    )
    _write_cache(
        storage,
        ids[1],
        make_osu([(1200 + index * 300, 120, 120) for index in range(8)]),
    )
    result = analysis.batch_analyze(
        storage, ids, limit=1, allow_download=False, deadline_seconds=20
    )
    assert result["requested"] == 3
    assert result["analyzed"] == 1
    assert result["remaining"] == 2
    second = analysis.batch_analyze(
        storage, ids, limit=5, allow_download=False, deadline_seconds=20
    )
    assert second["analyzed"] == 1
    assert second["remaining"] == 1  # the file-less difficulty
    third = analysis.batch_analyze(
        storage, ids, limit=5, allow_download=False, deadline_seconds=20
    )
    assert third["analyzed"] == 0
    assert third["remaining"] == 1


def test_truncated_analysis_reports_scope_and_partial_status(
    storage: Storage,
) -> None:
    beatmap_id = _save_set(storage)
    notes = [
        (1000 + index * 125, 100 + (index % 2) * 60, 100)
        for index in range(gameplay.MAX_PREVIEW_OBJECTS + 5)
    ]
    _write_cache(storage, beatmap_id, make_osu(notes))
    result = analysis.analyze_beatmap(storage, beatmap_id, allow_download=False)
    assert result.status == evidence.PARTIAL
    assert result.sequence is not None and result.sequence.truncated
    assert "preview limit" in result.scope
    assert result.sequence.analyzed_notes == gameplay.MAX_PREVIEW_OBJECTS


def test_stale_analyzer_version_is_not_trusted(storage: Storage) -> None:
    beatmap_id = _save_set(storage)
    _write_cache(storage, beatmap_id, make_osu([(1000, 100, 100)] * 10))
    storage.save_analysis(
        beatmap_id,
        status=evidence.MEASURED,
        source="cache",
        analyzer_version=patterns.ANALYZER_VERSION - 1,
        file_mtime_ns=None,
        file_size=None,
        scope="old",
        sequence_json=analysis.sequence_to_json(SequenceAnalysis()),
    )
    assert not analysis.peek_analysis(storage, beatmap_id).usable
    fresh = analysis.analyze_beatmap(storage, beatmap_id, allow_download=False)
    assert fresh.analyzer_version == patterns.ANALYZER_VERSION


def test_failed_row_from_old_analyzer_is_retried(storage: Storage) -> None:
    beatmap_id = _save_set(storage)
    _write_cache(
        storage,
        beatmap_id,
        make_osu([(1000 + index * 200, 100, 100) for index in range(10)]),
    )
    storage.save_analysis(
        beatmap_id,
        status=evidence.FAILED,
        source="cache",
        analyzer_version=patterns.ANALYZER_VERSION - 1,
        file_mtime_ns=None,
        file_size=None,
        scope="old failure",
        sequence_json="{}",
        attempts=analysis.MAX_ATTEMPTS,
    )
    assert analysis.peek_analysis(storage, beatmap_id).status != evidence.FAILED
    fresh = analysis.analyze_beatmap(storage, beatmap_id, allow_download=False)
    assert fresh.usable
