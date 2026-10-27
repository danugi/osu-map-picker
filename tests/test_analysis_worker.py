from __future__ import annotations

from pathlib import Path

from conftest import make_beatmap, make_beatmapset

import analysis
import analysis_worker
import evidence
from storage import Storage


def _seed(store: Storage, count: int = 3) -> list[int]:
    ids = []
    for set_id in range(1, count + 1):
        beatmap = make_beatmap(set_id * 10 + 1, set_id)
        store.save_beatmapset(
            make_beatmapset(set_id, beatmaps=(beatmap,))
        )
        ids.append(beatmap.id)
    return ids


def _write_osu(store: Storage, beatmap_id: int, body: str) -> None:
    cache = analysis.cache_dir_for(store)
    cache.mkdir(parents=True, exist_ok=True)
    (cache / f"{beatmap_id}.osu").write_text(body, encoding="utf-8")


def _valid_osu() -> str:
    from test_patterns import make_osu

    return make_osu(
        [(1000 + index * 125, 100, 100) for index in range(12)]
    )


def test_queue_persists_across_storage_instances(storage: Storage) -> None:
    ids = _seed(storage)
    added = analysis_worker.enqueue_catalog(storage, allow_download=False)
    assert added == 0  # no .osu and no API: nothing useful to enqueue
    _write_osu(storage, ids[0], _valid_osu())
    added = analysis_worker.enqueue_catalog(storage, allow_download=False)
    assert added == 1
    reopened = Storage(storage.path)
    stats = reopened.queue_stats()
    assert stats["pending"] == 1
    assert stats["total"] == 1


def test_claim_is_atomic_between_workers(storage: Storage) -> None:
    ids = _seed(storage)
    _write_osu(storage, ids[0], _valid_osu())
    analysis_worker.enqueue_catalog(storage, allow_download=False)
    first = storage.claim_analysis("owner-a", 1)
    second = storage.claim_analysis("owner-b", 1)
    assert [int(row["beatmap_id"]) for row in first] == [ids[0]]
    assert second == []
    storage.finish_analysis(
        ids[0], status="done", attempts=0, next_attempt_at=None
    )
    assert storage.queue_stats()["done"] == 1


def test_interrupted_processing_jobs_are_recovered(storage: Storage) -> None:
    ids = _seed(storage)
    _write_osu(storage, ids[0], _valid_osu())
    analysis_worker.enqueue_catalog(storage, allow_download=False)
    storage.claim_analysis("dead-worker", 1)
    assert storage.queue_stats()["processing"] == 1
    assert storage.recover_analysis_jobs() == 1
    assert storage.queue_stats()["pending"] == 1


def test_lease_prevents_duplicate_workers(storage: Storage) -> None:
    assert storage.acquire_worker_lease("owner-a", 45) is True
    assert storage.acquire_worker_lease("owner-b", 45) is False
    assert storage.acquire_worker_lease("owner-a", 45) is True
    storage.release_worker_lease("owner-a")
    assert storage.acquire_worker_lease("owner-b", 45) is True


def test_pause_resume_and_status_are_persistent(storage: Storage) -> None:
    analysis_worker.set_paused(storage, True)
    assert storage.worker_state()["paused"] == 1
    status = analysis_worker.worker_status(storage)
    assert status["paused"] is True
    analysis_worker.set_paused(storage, False)
    assert storage.worker_state()["paused"] == 0


def test_process_once_analyzes_cached_osu_and_completes_job(
    storage: Storage,
) -> None:
    ids = _seed(storage)
    _write_osu(storage, ids[0], _valid_osu())
    analysis_worker.enqueue_catalog(storage, allow_download=False)
    assert analysis_worker.process_once(
        storage, limit=1, allow_download=False
    ) == 1
    assert storage.queue_stats()["done"] == 1
    assert storage.analysis_stats().get("measured") == 1


def test_failed_analysis_retries_are_bounded(
    storage: Storage, monkeypatch
) -> None:
    ids = _seed(storage)
    _write_osu(storage, ids[0], "not an osu file")
    analysis_worker.enqueue_catalog(storage, allow_download=False)
    monkeypatch.setattr(
        analysis_worker,
        "_backoff",
        lambda index: analysis_worker._now()
        - analysis_worker.timedelta(seconds=1),
    )
    for _ in range(5):
        analysis_worker.process_once(
            storage, limit=1, allow_download=False
        )
        if storage.queue_stats()["failed"]:
            break
    stats = storage.queue_stats()
    assert stats["failed"] == 1
    row = storage.queue_failures(limit=1)[0]
    assert row["status"] == "failed"
    assert int(row["attempts"]) >= 1
    # Failed rows are not claimed again.
    assert analysis_worker.process_once(
        storage, limit=1, allow_download=False
    ) == 0


def test_unavailable_without_api_is_terminal_until_requeued(
    storage: Storage,
) -> None:
    ids = _seed(storage)
    # allow_download=True enqueues a map without a local file.
    analysis_worker.enqueue_catalog(storage, allow_download=True)
    for _ in range(3):
        analysis_worker.process_once(
            storage, limit=1, allow_download=False
        )
    assert storage.queue_stats()["unavailable"] == 3
    assert analysis_worker.process_once(
        storage, limit=1, allow_download=False
    ) == 0
    # When API becomes available the catalog scan can requeue the files.
    assert analysis_worker.enqueue_catalog(
        storage, allow_download=True
    ) == 3
    assert storage.queue_stats()["pending"] == 3


def test_ensure_worker_starts_one_daemon_thread(
    storage: Storage, monkeypatch
) -> None:
    monkeypatch.setenv("OSU_PICKER_WORKER", "1")
    analysis_worker.stop_worker()
    try:
        analysis_worker.ensure_worker(storage)
        analysis_worker.ensure_worker(storage)
        assert analysis_worker.worker_alive()
        assert analysis_worker._THREAD is not None
        assert analysis_worker._THREAD.daemon
        # Lease acquisition alone must not look like a completed discovery.
        assert storage.worker_state()["last_discovery_at"] is None
    finally:
        analysis_worker.stop_worker()


def test_discovery_request_flag_is_consumed_once(storage: Storage) -> None:
    storage.request_worker_discovery()
    assert storage.consume_worker_discovery_request() is True
    assert storage.consume_worker_discovery_request() is False


def test_auto_discovery_skips_without_api(storage: Storage) -> None:
    message = analysis_worker.run_auto_discovery(storage)
    assert "API не настроен" in message
    assert storage.search_run_count() == 0


def test_bounded_concurrency_processes_jobs_in_parallel(
    storage: Storage, monkeypatch
) -> None:
    ids = _seed(storage, 8)
    for beatmap_id in ids:
        _write_osu(storage, beatmap_id, _valid_osu())
    analysis_worker.enqueue_catalog(storage, allow_download=False)
    monkeypatch.setenv("OSU_PICKER_ANALYSIS_CONCURRENCY", "4")
    import threading as _threading
    import time as _time

    original = analysis.analyze_beatmap
    lock = _threading.Lock()
    active = 0
    maximum = 1

    def tracked(*args, **kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            _time.sleep(0.05)
            return original(*args, **kwargs)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(analysis, "analyze_beatmap", tracked)
    assert analysis_worker.process_once(
        storage, limit=8, allow_download=False
    ) == 8
    assert maximum >= 2
    assert storage.queue_stats()["done"] == 8


def test_active_result_priority_is_preserved_by_rescan(
    storage: Storage,
) -> None:
    ids = _seed(storage, 3)
    for beatmap_id in ids:
        _write_osu(storage, beatmap_id, _valid_osu())
    analysis_worker.prioritize_beatmaps(
        storage, [ids[2]], priority=-5, reason="active_result"
    )
    analysis_worker.enqueue_catalog(storage, allow_download=False)
    claimed = storage.claim_analysis("owner", 1)
    assert int(claimed[0]["beatmap_id"]) == ids[2]
    assert int(claimed[0]["priority"]) == -5
    assert claimed[0]["reason"] == "active_result"


def test_rate_limit_retry_after_schedules_next_attempt(
    storage: Storage, monkeypatch
) -> None:
    ids = _seed(storage)
    _write_osu(storage, ids[0], _valid_osu())
    analysis_worker.enqueue_catalog(storage, allow_download=False)
    from models import AnalysisEvidence, now_iso

    monkeypatch.setattr(
        analysis,
        "analyze_beatmap",
        lambda *a, **k: AnalysisEvidence(
            beatmap_id=ids[0],
            status=evidence.FAILED,
            scope="HTTP 429",
            retry_after=12.0,
        ),
    )
    try:
        analysis_worker.process_once(storage, limit=1, allow_download=True)
    finally:
        analysis_worker._RATE_LIMIT_UNTIL = 0.0
    with storage.connection() as conn:
        row = conn.execute(
            "SELECT status, next_attempt_at, attempts FROM analysis_queue "
            "WHERE beatmap_id=?",
            (ids[0],),
        ).fetchone()
    assert row["status"] == "pending"
    assert row["next_attempt_at"] is not None
    assert row["next_attempt_at"] > now_iso()
    assert int(row["attempts"]) == 1


def test_discovery_runs_despite_queue_and_records_attempt_time(
    storage: Storage, monkeypatch
) -> None:
    ids = _seed(storage, 12)
    for beatmap_id in ids:
        _write_osu(storage, beatmap_id, _valid_osu())
    analysis_worker.enqueue_catalog(storage, allow_download=False)
    calls: list[str] = []
    monkeypatch.setattr(analysis_worker, "_configured", lambda: True)
    monkeypatch.setattr(
        analysis_worker,
        "run_auto_discovery",
        lambda store: calls.append("run") or "Автопоиск: ok",
    )
    worker = analysis_worker.AnalysisWorker(storage.path)
    worker._maybe_discovery(storage)
    assert calls == ["run"]
    assert storage.worker_state()["last_discovery_at"] is not None
    # A second call inside the interval is skipped even with the queue full.
    worker._maybe_discovery(storage)
    assert calls == ["run"]
    # After the interval it is allowed again.
    storage.set_worker_discovery_time("2000-01-01T00:00:00+00:00")
    worker._maybe_discovery(storage)
    assert calls == ["run", "run"]


def test_favorite_and_recent_maps_get_priority(storage: Storage) -> None:
    ids = _seed(storage, 3)
    for beatmap_id in ids:
        _write_osu(storage, beatmap_id, _valid_osu())
    storage.set_favorite(3, True)
    analysis_worker.enqueue_catalog(storage, allow_download=False)
    claimed = storage.claim_analysis("owner", 1)
    assert int(claimed[0]["beatmap_id"]) == ids[2]
    assert int(claimed[0]["priority"]) == 0
