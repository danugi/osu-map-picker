"""Persistent, cache-first analysis of one difficulty's ``.osu`` evidence.

This module is the single place that decides whether a difficulty was
analyzed, whether the result is still valid, and whether a missing file can be
downloaded as part of an explicit, bounded batch. It never runs on a Streamlit
rerun by itself.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Iterable

import evidence
import gameplay
import patterns
from models import (
    AnalysisEvidence,
    PatternChain,
    PatternSection,
    SequenceAnalysis,
)
from storage import Storage, StorageError

MAX_ATTEMPTS = 2


def sequence_to_json(sequence: SequenceAnalysis) -> str:
    return json.dumps(
        asdict(sequence), ensure_ascii=False, separators=(",", ":")
    )


def _chain_from(data: dict) -> PatternChain:
    return PatternChain(
        kind=str(data.get("kind", "stream")),
        start_index=int(data.get("start_index", 0)),
        length=int(data.get("length", 0)),
        start_ms=int(data.get("start_ms", 0)),
        end_ms=int(data.get("end_ms", 0)),
        median_gap_ms=float(data.get("median_gap_ms", 0.0)),
        rhythm_quality=float(data.get("rhythm_quality", 0.0)),
        compact_share=float(data.get("compact_share", 0.0)),
    )


def _section_from(data: dict) -> PatternSection:
    return PatternSection(
        start_ms=int(data.get("start_ms", 0)),
        end_ms=int(data.get("end_ms", 0)),
        object_count=int(data.get("object_count", 0)),
        labels=tuple(str(value) for value in data.get("labels", ())),
        jump_share=float(data.get("jump_share", 0.0)),
        wide_jump_share=float(data.get("wide_jump_share", 0.0)),
        flow_share=float(data.get("flow_share", 0.0)),
        angle_change_share=float(data.get("angle_change_share", 0.0)),
    )


def sequence_from_json(raw: str | None) -> SequenceAnalysis | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return SequenceAnalysis(
            stream_chains=tuple(
                _chain_from(item) for item in data.get("stream_chains", ())
            ),
            burst_chains=tuple(
                _chain_from(item) for item in data.get("burst_chains", ())
            ),
            analyzed_notes=int(data.get("analyzed_notes", 0)),
            total_notes=int(data.get("total_notes", 0)),
            truncated=bool(data.get("truncated", False)),
            jump_share=float(data.get("jump_share", 0.0)),
            wide_jump_share=float(data.get("wide_jump_share", 0.0)),
            flow_share=float(data.get("flow_share", 0.0)),
            angle_change_share=float(data.get("angle_change_share", 0.0)),
            slider_share=float(data.get("slider_share", 0.0)),
            notes_per_second=float(data.get("notes_per_second", 0.0)),
            rhythm_variability=float(
                data.get("rhythm_variability", 0.0)
            ),
            labels=tuple(str(value) for value in data.get("labels", ())),
            sections=tuple(
                _section_from(item) for item in data.get("sections", ())
            ),
            analyzer_version=int(data.get("analyzer_version", 0)),
        )
    except (TypeError, ValueError, KeyError):
        return None


def cache_dir_for(store: Storage) -> Path:
    return store.path.parent / "gameplay_cache"


def _file_stat(cache_dir: Path, beatmap_id: int) -> tuple[int, int] | None:
    try:
        stat = (cache_dir / f"{int(beatmap_id)}.osu").stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _row_evidence(
    row: dict, *, file_stat: tuple[int, int] | None
) -> AnalysisEvidence | None:
    if int(row.get("analyzer_version", 0)) != patterns.ANALYZER_VERSION:
        return None
    status = str(row.get("status", evidence.NOT_ANALYZED))
    sequence = sequence_from_json(row.get("sequence_json"))
    if status in {evidence.MEASURED, evidence.PARTIAL} and sequence is None:
        return None
    if file_stat is not None and (
        row.get("file_mtime_ns") is not None
        and row.get("file_size") is not None
        and (
            int(row["file_mtime_ns"]) != file_stat[0]
            or int(row["file_size"]) != file_stat[1]
        )
    ):
        # The .osu was replaced after this analysis.
        return None
    return AnalysisEvidence(
        beatmap_id=int(row["beatmap_id"]),
        status=status,
        source=str(row.get("source", "")),
        sequence=sequence,
        scope=str(row.get("scope", "")),
        analyzer_version=int(row.get("analyzer_version", 0)),
        file_mtime_ns=row.get("file_mtime_ns"),
        file_size=row.get("file_size"),
        attempts=int(row.get("attempts", 0)),
    )


def bulk_peek(
    store: Storage, beatmap_ids: Iterable[int]
) -> dict[int, AnalysisEvidence]:
    """Cheap status lookup for many difficulties in a few SQLite queries."""

    wanted = list(dict.fromkeys(int(value) for value in beatmap_ids))
    rows = store.analysis_for_beatmaps(wanted)
    cache_dir = cache_dir_for(store)
    result: dict[int, AnalysisEvidence] = {}
    for beatmap_id in wanted:
        file_stat = _file_stat(cache_dir, beatmap_id)
        row = rows.get(beatmap_id)
        if row is not None:
            cached = _row_evidence(row, file_stat=file_stat)
            if cached is not None:
                result[beatmap_id] = cached
                continue
            if (
                str(row.get("status")) == evidence.FAILED
                and int(row.get("analyzer_version", 0))
                == patterns.ANALYZER_VERSION
            ):
                result[beatmap_id] = AnalysisEvidence(
                    beatmap_id=beatmap_id,
                    status=evidence.FAILED,
                    source=str(row.get("source", "")),
                    scope=str(row.get("scope", "")),
                    analyzer_version=int(
                        row.get("analyzer_version", 0)
                    ),
                    attempts=int(row.get("attempts", 0)),
                )
                continue
        result[beatmap_id] = AnalysisEvidence(
            beatmap_id=beatmap_id,
            status=(
                evidence.SOURCE_UNAVAILABLE
                if file_stat is None
                else evidence.NOT_ANALYZED
            ),
        )
    return result


def peek_analysis(store: Storage, beatmap_id: int) -> AnalysisEvidence:
    """Cheap status lookup without parsing or network work."""

    cache_dir = cache_dir_for(store)
    file_stat = _file_stat(cache_dir, beatmap_id)
    row = store.get_analysis(beatmap_id)
    if row is not None:
        cached = _row_evidence(row, file_stat=file_stat)
        if cached is not None:
            return cached
        if (
            str(row.get("status")) == evidence.FAILED
            and int(row.get("analyzer_version", 0))
            == patterns.ANALYZER_VERSION
        ):
            return AnalysisEvidence(
                beatmap_id=int(beatmap_id),
                status=evidence.FAILED,
                source=str(row.get("source", "")),
                scope=str(row.get("scope", "")),
                analyzer_version=int(row.get("analyzer_version", 0)),
                attempts=int(row.get("attempts", 0)),
            )
    if file_stat is None:
        return AnalysisEvidence(
            beatmap_id=int(beatmap_id), status=evidence.SOURCE_UNAVAILABLE
        )
    return AnalysisEvidence(
        beatmap_id=int(beatmap_id), status=evidence.NOT_ANALYZED
    )


def _scope(sequence: SequenceAnalysis) -> str:
    if sequence.total_notes <= 0:
        return "0 objects"
    if sequence.truncated:
        return (
            f"{sequence.analyzed_notes}/{sequence.total_notes} objects "
            f"(preview limit), sections {len(sequence.sections)}"
        )
    return (
        f"{sequence.analyzed_notes}/{sequence.total_notes} objects, "
        f"sections {len(sequence.sections)}"
    )


def _save(
    store: Storage,
    beatmap_id: int,
    *,
    status: str,
    source: str,
    scope: str,
    sequence: SequenceAnalysis | None,
    file_stat: tuple[int, int] | None,
    attempts: int,
) -> None:
    try:
        store.save_analysis(
            beatmap_id,
            status=status,
            source=source,
            analyzer_version=patterns.ANALYZER_VERSION,
            file_mtime_ns=file_stat[0] if file_stat else None,
            file_size=file_stat[1] if file_stat else None,
            scope=scope,
            sequence_json=(
                sequence_to_json(sequence) if sequence is not None else "{}"
            ),
            attempts=attempts,
        )
    except StorageError:
        # A missing beatmap row must not break the analysis batch.
        pass


def analyze_beatmap(
    store: Storage,
    beatmap_id: int,
    *,
    allow_download: bool = False,
    fetcher: Callable[[str], bytes] | None = None,
    force: bool = False,
) -> AnalysisEvidence:
    """Return valid evidence, analyzing the cached file or downloading once."""

    beatmap_id = int(beatmap_id)
    cache_dir = cache_dir_for(store)
    file_stat = _file_stat(cache_dir, beatmap_id)
    row = store.get_analysis(beatmap_id)
    if row is not None and not force:
        cached = _row_evidence(row, file_stat=file_stat)
        if cached is not None:
            return cached
        if (
            str(row.get("status")) == evidence.FAILED
            and int(row.get("analyzer_version", 0))
            == patterns.ANALYZER_VERSION
            and int(row.get("attempts", 0)) >= MAX_ATTEMPTS
        ):
            return peek_analysis(store, beatmap_id)

    source = "cache"
    try:
        if file_stat is None:
            if not allow_download:
                return AnalysisEvidence(
                    beatmap_id=beatmap_id,
                    status=evidence.SOURCE_UNAVAILABLE,
                    source="",
                    analyzer_version=patterns.ANALYZER_VERSION,
                )
            source = "download"
            preview = gameplay.load_preview(
                beatmap_id, cache_dir, fetcher=fetcher
            )
        else:
            preview = gameplay.load_cached_preview(beatmap_id, cache_dir)
    except gameplay.GameplayError as exc:
        attempts = (
            int(row.get("attempts", 0)) + 1 if row is not None else 1
        )
        _save(
            store,
            beatmap_id,
            status=evidence.FAILED,
            source=source,
            scope=str(exc)[:300],
            sequence=None,
            file_stat=file_stat,
            attempts=attempts,
        )
        return AnalysisEvidence(
            beatmap_id=beatmap_id,
            status=evidence.FAILED,
            source=source,
            scope=str(exc),
            analyzer_version=patterns.ANALYZER_VERSION,
            attempts=attempts,
        )
    except Exception as exc:  # noqa: BLE001 - one file must not stop a batch
        attempts = (
            int(row.get("attempts", 0)) + 1 if row is not None else 1
        )
        _save(
            store,
            beatmap_id,
            status=evidence.FAILED,
            source=source,
            scope=type(exc).__name__,
            sequence=None,
            file_stat=file_stat,
            attempts=attempts,
        )
        return AnalysisEvidence(
            beatmap_id=beatmap_id,
            status=evidence.FAILED,
            source=source,
            scope=type(exc).__name__,
            analyzer_version=patterns.ANALYZER_VERSION,
            attempts=attempts,
        )

    sequence = patterns.analyze_sequences(preview)
    status = evidence.PARTIAL if sequence.truncated else evidence.MEASURED
    file_stat = _file_stat(cache_dir, beatmap_id)
    scope = _scope(sequence)
    _save(
        store,
        beatmap_id,
        status=status,
        source=source,
        scope=scope,
        sequence=sequence,
        file_stat=file_stat,
        attempts=0,
    )
    return AnalysisEvidence(
        beatmap_id=beatmap_id,
        status=status,
        source=source,
        sequence=sequence,
        scope=scope,
        analyzer_version=patterns.ANALYZER_VERSION,
        file_mtime_ns=file_stat[0] if file_stat else None,
        file_size=file_stat[1] if file_stat else None,
    )


def batch_analyze(
    store: Storage,
    beatmap_ids: Iterable[int],
    *,
    limit: int = 20,
    allow_download: bool = False,
    deadline_seconds: float = 60.0,
    fetcher: Callable[[str], bytes] | None = None,
) -> dict[str, int | bool]:
    """Analyze missing/stale evidence in a bounded, explicit batch.

    Already analyzed and permanently failed rows are skipped without spending
    the analysis limit. Candidates with a local file are processed before
    candidates that would need a download, so a local-only run still makes
    progress instead of stopping at missing files.
    """

    started = time.perf_counter()
    deadline = started + max(0.0, float(deadline_seconds))
    wanted = list(dict.fromkeys(int(value) for value in beatmap_ids))
    pending: list[tuple[int, str]] = []
    skipped = 0
    for beatmap_id in wanted:
        current = peek_analysis(store, beatmap_id)
        if (
            current.usable
            and current.analyzer_version == patterns.ANALYZER_VERSION
        ):
            skipped += 1
            continue
        if (
            current.status == evidence.FAILED
            and current.analyzer_version == patterns.ANALYZER_VERSION
            and current.attempts >= MAX_ATTEMPTS
        ):
            skipped += 1
            continue
        pending.append((beatmap_id, current.status))
    pending.sort(
        key=lambda item: (
            item[1] == evidence.SOURCE_UNAVAILABLE,
            item[0],
        )
    )

    analyzed = failed = 0
    processed = 0
    for beatmap_id, status in pending:
        if processed >= max(1, int(limit)):
            break
        if time.perf_counter() >= deadline:
            break
        if status == evidence.SOURCE_UNAVAILABLE and not allow_download:
            skipped += 1
            continue
        result = analyze_beatmap(
            store,
            beatmap_id,
            allow_download=allow_download,
            fetcher=fetcher,
        )
        processed += 1
        if result.usable:
            analyzed += 1
        elif result.status == evidence.FAILED:
            failed += 1
        else:
            skipped += 1
    final_states = bulk_peek(store, [beatmap_id for beatmap_id, _ in pending])
    remaining = 0
    for current in final_states.values():
        if current.usable:
            continue
        if (
            current.status == evidence.FAILED
            and current.analyzer_version == patterns.ANALYZER_VERSION
            and current.attempts >= MAX_ATTEMPTS
        ):
            continue
        remaining += 1
    return {
        "requested": len(wanted),
        "analyzed": analyzed,
        "skipped": skipped,
        "failed": failed,
        "remaining": remaining,
        "elapsed": round(time.perf_counter() - started, 3),
        "deadline_hit": time.perf_counter() >= deadline,
    }
