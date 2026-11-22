"""Diagnose Streams/Stamina eligibility using the real local catalog.

The original database is never opened read-write. The script copies it to a
temporary directory, reads persisted sequence evidence, and evaluates the
current preset policies against exactly the same difficulty pool. It is safe
to run repeatedly against a user's library.

Usage:
    python tools/diagnose_presets.py
    python tools/diagnose_presets.py --db path/to/osu_picker.db --json

Environment variables respected by the application are used by default:
``OSU_PICKER_DB`` and ``OSU_PICKER_DATA_DIR``.
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import analysis as analysis_store  # noqa: E402
import config  # noqa: E402
import evidence  # noqa: E402
import presets  # noqa: E402
from discovery import DiscoveryEngine  # noqa: E402
from features import derive_features  # noqa: E402
from models import Candidate, DiscoverySettings  # noqa: E402
from relevance import evaluate_relevance  # noqa: E402
from skillsets import evaluate_skills  # noqa: E402
from storage import Storage  # noqa: E402

_FEATURE_KEYS = (
    "streams",
    "bursts",
    "stream_longest",
    "stream_chain_count",
    "stream_rhythm_quality",
    "burst_count",
    "burst_tempo",
)


def _value(scores: dict[str, float], key: str) -> float:
    return round(float(scores.get(key, 0.0)), 4)


def _row(
    beatmapset: Any,
    beatmap: Any,
    sequence: Any,
    traits: Any,
    skillsets: dict[str, Any],
    streams_check: Any,
    stamina_check: Any,
) -> dict[str, Any]:
    stream_lengths = [chain.length for chain in sequence.stream_chains]
    burst_lengths = [chain.length for chain in sequence.burst_chains]
    return {
        "beatmapset_id": beatmapset.id,
        "difficulty_id": beatmap.id,
        "version": beatmap.version,
        "stars": beatmap.difficulty_rating,
        "total_length": beatmap.total_length,
        "analyzed_notes": sequence.analyzed_notes,
        "total_notes": sequence.total_notes,
        "stream_chain_count": len(stream_lengths),
        "burst_chain_count": len(burst_lengths),
        "max_stream_chain": max(stream_lengths, default=0),
        "max_burst_chain": max(burst_lengths, default=0),
        "stream_note_share": _value(traits.scores, "streams"),
        "burst_note_share": _value(traits.scores, "bursts"),
        "features": {
            key: _value(traits.scores, key) for key in _FEATURE_KEYS
        },
        "skills": {
            key: (
                None
                if skillsets.get(key) is None
                else skillsets[key].score
            )
            for key in (
                "stream_consistency",
                "stream_long",
                "burst_control",
                "stream_endurance",
            )
        },
        "streams_passed": streams_check.passed,
        "streams_reason": streams_check.reason,
        "stamina_passed": stamina_check.passed,
        "stamina_reason": stamina_check.reason,
    }


def _evaluate(
    db_path: Path,
    *,
    limit: int | None,
    legacy_streams_policy: bool = False,
) -> dict[str, Any]:
    streams = presets.builtin_by_name("Streams")
    stamina = presets.builtin_by_name("Stamina")
    if streams is None or stamina is None:
        raise RuntimeError("current Streams/Stamina built-ins are missing")
    if legacy_streams_policy:
        streams = copy.deepcopy(streams)
        streams.ranking.relevance_policy = copy.deepcopy(
            presets._PRE_V18_STREAMS_POLICY
        )

    with tempfile.TemporaryDirectory(prefix="map-picker-diag-") as raw_tmp:
        tmp = Path(raw_tmp)
        copied = tmp / "osu_picker.db"
        shutil.copy2(db_path, copied)
        store = Storage(copied)
        sets = store.list_beatmapsets(5000)
        pairs = [
            (beatmapset, beatmap)
            for beatmapset in sets
            for beatmap in beatmapset.beatmaps
            if beatmap.mode == "osu"
        ]
        if limit is not None:
            pairs = pairs[: max(0, limit)]
        ids = [beatmap.id for _, beatmap in pairs]
        evidence_by_id = analysis_store.bulk_peek(store, ids)

        counts: Counter[str] = Counter()
        counts["catalogued_sets"] = len(sets)
        counts["catalogued_difficulties"] = len(pairs)
        counts["usable_sequence"] = sum(
            1 for item in evidence_by_id.values() if item.usable
        )
        counts["status_measured"] = sum(
            1
            for item in evidence_by_id.values()
            if item.status == evidence.MEASURED
        )
        counts["status_partial"] = sum(
            1
            for item in evidence_by_id.values()
            if item.status == evidence.PARTIAL
        )
        counts["status_source_unavailable"] = sum(
            1
            for item in evidence_by_id.values()
            if item.status == evidence.SOURCE_UNAVAILABLE
        )
        counts["status_not_analyzed"] = sum(
            1
            for item in evidence_by_id.values()
            if item.status == evidence.NOT_ANALYZED
        )
        counts["status_failed"] = sum(
            1
            for item in evidence_by_id.values()
            if item.status == evidence.FAILED
        )

        rows: list[dict[str, Any]] = []
        for beatmapset, beatmap in pairs:
            current = evidence_by_id.get(beatmap.id)
            if current is None or not current.usable or current.sequence is None:
                continue
            sequence = current.sequence
            traits = derive_features(
                beatmap,
                beatmapset,
                None,
                sequence=sequence,
                analysis_status=current.status,
                analysis_scope=current.scope,
                analyzer_version=current.analyzer_version,
            )
            candidate = Candidate(
                beatmapset=beatmapset,
                beatmap=beatmap,
                traits=traits,
                skillsets=evaluate_skills(traits),
                sequence=sequence,
                analysis=current,
            )
            streams_check = evaluate_relevance(candidate, streams.ranking)
            stamina_check = evaluate_relevance(candidate, stamina.ranking)
            rows.append(
                _row(
                    beatmapset,
                    beatmap,
                    sequence,
                    traits,
                    candidate.skillsets,
                    streams_check,
                    stamina_check,
                )
            )

        counts["stream_evidence"] = sum(
            1 for row in rows if row["stream_note_share"] > 0
        )
        counts["burst_evidence"] = sum(
            1 for row in rows if row["burst_note_share"] > 0
        )
        counts["streams_pass"] = sum(
            1 for row in rows if row["streams_passed"]
        )
        counts["stamina_pass"] = sum(
            1 for row in rows if row["stamina_passed"]
        )
        counts["both_pass"] = sum(
            1
            for row in rows
            if row["streams_passed"] and row["stamina_passed"]
        )
        counts["stamina_only"] = sum(
            1
            for row in rows
            if row["stamina_passed"] and not row["streams_passed"]
        )
        counts["streams_only"] = sum(
            1
            for row in rows
            if row["streams_passed"] and not row["stamina_passed"]
        )

        reasons: Counter[str] = Counter(
            row["streams_reason"]
            for row in rows
            if row["stamina_passed"] and not row["streams_passed"]
        )
        examples: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            if row["stamina_passed"] and not row["streams_passed"]:
                bucket = examples[row["streams_reason"]]
                if len(bucket) < 3:
                    bucket.append(row)
        return {
            "database": str(db_path),
            "counts": dict(counts),
            "streams_examples": [
                row
                for row in rows
                if row["streams_passed"]
            ][:10],
            "both_examples": [
                row
                for row in rows
                if row["streams_passed"] and row["stamina_passed"]
            ][:10],
            "streams_only_examples": [
                row
                for row in rows
                if row["streams_passed"] and not row["stamina_passed"]
            ][:10],
            "stamina_only_reasons": [
                {
                    "count": count,
                    "reason": reason,
                    "examples": examples.get(reason, []),
                }
                for reason, count in reasons.most_common()
            ],
        }



def _rerank_overlap(db_path: Path) -> dict[str, Any]:
    """Run the real local search pipeline for Streams and Stamina."""
    with tempfile.TemporaryDirectory(prefix="map-picker-overlap-") as raw_tmp:
        tmp = Path(raw_tmp)
        copied = tmp / "osu_picker.db"
        shutil.copy2(db_path, copied)
        store = Storage(copied)
        presets.ensure_builtin_presets(store)
        engine = DiscoveryEngine(
            None,
            store,
            DiscoverySettings(
                max_candidates=1000,
                fetch_attributes_limit=0,
                fetch_sequences_limit=0,
                use_oldest=False,
                use_rating=False,
                use_popular=False,
                expand_seeds=False,
            ),
        )
        lists: dict[str, dict[int, tuple[int, int]]] = {}
        for name in ("Streams", "Stamina"):
            preset = presets.builtin_by_name(name)
            if preset is None:
                raise RuntimeError(f"missing preset: {name}")
            result = engine.rerank_stored(
                preset.filters,
                preset.ranking,
                all_candidates=True,
            )
            lists[name] = {
                item.candidate.beatmap.id: (
                    item.candidate.beatmapset.id,
                    item.candidate.beatmap.id,
                )
                for item in result.scored
            }
        streams = set(lists["Streams"])
        stamina = set(lists["Stamina"])
        return {
            "streams_count": len(streams),
            "stamina_count": len(stamina),
            "both_count": len(streams & stamina),
            "streams_only_count": len(streams - stamina),
            "stamina_only_count": len(stamina - streams),
            "both_ids": [
                lists["Streams"][beatmap_id]
                for beatmap_id in sorted(streams & stamina)
            ],
        }

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=config.db_path(),
        help="SQLite database path (default: application data directory)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="optional difficulty limit for quick checks",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--compare-before",
        action="store_true",
        help="also evaluate the pre-v18 raw-share Streams policy",
    )
    parser.add_argument(
        "--rerank-overlap",
        action="store_true",
        help="run the real local Streams/Stamina result pipeline",
    )
    args = parser.parse_args(argv)

    if not args.db.is_file():
        print(f"database not found: {args.db}", file=sys.stderr)
        return 1
    try:
        result = _evaluate(args.db.resolve(), limit=args.limit)
        before = (
            _evaluate(
                args.db.resolve(),
                limit=args.limit,
                legacy_streams_policy=True,
            )
            if args.compare_before
            else None
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic must explain failure
        print(f"diagnostic failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    overlap = (
        _rerank_overlap(args.db.resolve()) if args.rerank_overlap else None
    )
    if args.json:
        payload = {"current": result}
        if before is not None:
            payload["before_fix"] = before
        if overlap is not None:
            payload["rerank_overlap"] = overlap
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    counts = result["counts"]
    print(f"database: {result['database']}")
    for key in (
        "catalogued_sets",
        "catalogued_difficulties",
        "usable_sequence",
        "stream_evidence",
        "burst_evidence",
        "streams_pass",
        "stamina_pass",
        "both_pass",
        "stamina_only",
        "streams_only",
    ):
        print(f"{key}: {counts.get(key, 0)}")
    print("stamina-only failure reasons:")
    for bucket in result["stamina_only_reasons"][:10]:
        print(f"  {bucket['count']} x {bucket['reason']}")
        for example in bucket["examples"][:2]:
            print(
                "    set={beatmapset_id} difficulty={difficulty_id} "
                "streams={stream_note_share} bursts={burst_note_share} "
                "chains={stream_chain_count} max={max_stream_chain}".format(
                    **example
                )
            )
    if before is not None:
        old = before["counts"]
        print("before-fix counts (pre-v18 raw-share policy):")
        for key in (
            "streams_pass",
            "stamina_pass",
            "both_pass",
            "stamina_only",
            "streams_only",
        ):
            print(f"  {key}: {old.get(key, 0)}")
    print("streams-only examples:")
    for row in result["streams_only_examples"][:5]:
        print(
            "  set={beatmapset_id} difficulty={difficulty_id} "
            "streams={stream_note_share} bursts={burst_note_share} "
            "max_stream={max_stream_chain}".format(**row)
        )
    if overlap is not None:
        print("real local rerank overlap:")
        for key in (
            "streams_count",
            "stamina_count",
            "both_count",
            "streams_only_count",
            "stamina_only_count",
        ):
            print(f"  {key}: {overlap[key]}")
        for set_id, beatmap_id in overlap["both_ids"][:5]:
            print(f"  both: set={set_id} difficulty={beatmap_id}")
    print("both-pass examples:")
    for row in result["both_examples"][:5]:
        print(
            "  set={beatmapset_id} difficulty={difficulty_id} "
            "streams={stream_note_share} bursts={burst_note_share} "
            "endurance={endurance}".format(
                endurance=row["skills"]["stream_endurance"], **row
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
