"""Persistent background analysis worker.

The worker owns a single daemon thread inside the Streamlit process and
coordinates through SQLite:

* ``analysis_queue`` is the durable job queue;
* ``analysis_worker_state`` is the lease/pause/heartbeat row.

A lease prevents two processes from working at once, while an atomic claim
prevents two workers from processing the same difficulty.  If the process
dies, the next startup waits for the stale lease and recovers ``processing``
jobs back to ``pending``.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import analysis
import config
import evidence
import patterns
from discovery import DiscoveryEngine
from models import DiscoverySettings, parse_datetime
from osu_api import OsuApiClient
from storage import Storage

logger = logging.getLogger("osu_picker.worker")

_WORKER_ENV = "OSU_PICKER_WORKER"
_START_LOCK = threading.Lock()
_THREAD: threading.Thread | None = None
_WORKER: "AnalysisWorker | None" = None


def _worker_enabled() -> bool:
    return os.getenv(_WORKER_ENV, "1").strip().casefold() not in {
        "",
        "0",
        "false",
        "off",
        "no",
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat()


def _backoff(index: int) -> datetime:
    values = config.DEFAULT_ANALYSIS_RETRY_MINUTES
    minutes = values[min(max(0, index), len(values) - 1)]
    return _now() + timedelta(minutes=minutes)


def _cache_file(store: Storage, beatmap_id: int) -> Path:
    return analysis.cache_dir_for(store) / f"{int(beatmap_id)}.osu"


_DOWNLOAD_LOCK = threading.Lock()
_LAST_DOWNLOAD_START = 0.0
_RATE_LIMIT_UNTIL = 0.0


def _analysis_concurrency() -> int:
    raw = os.getenv(
        "OSU_PICKER_ANALYSIS_CONCURRENCY",
        str(config.DEFAULT_ANALYSIS_CONCURRENCY),
    )
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = config.DEFAULT_ANALYSIS_CONCURRENCY
    return max(1, min(8, value))


def _throttle_download(interval: float) -> None:
    """Serialize download starts while allowing requests to overlap."""

    global _LAST_DOWNLOAD_START
    with _DOWNLOAD_LOCK:
        now = time.monotonic()
        delay = max(
            interval - (now - _LAST_DOWNLOAD_START),
            _RATE_LIMIT_UNTIL - now,
        )
        if delay > 0:
            time.sleep(delay)
        _LAST_DOWNLOAD_START = time.monotonic()


def _set_rate_limit(retry_after: float | None) -> None:
    global _RATE_LIMIT_UNTIL
    seconds = max(1.0, min(300.0, float(retry_after or 5.0)))
    with _DOWNLOAD_LOCK:
        _RATE_LIMIT_UNTIL = max(
            _RATE_LIMIT_UNTIL, time.monotonic() + seconds
        )


def enqueue_catalog(
    store: Storage, *, allow_download: bool
) -> int:
    """Enqueue missing/stale analysis jobs, prioritizing user-relevant maps.

    Without API access, files that are not already cached cannot be analyzed,
    so they are left out of the queue instead of becoming permanent noise.
    """

    with store.connection() as conn:
        beatmaps = conn.execute(
            "SELECT id, beatmapset_id FROM beatmaps ORDER BY id"
        ).fetchall()
        analyses = {
            int(row["beatmap_id"]): dict(row)
            for row in conn.execute("SELECT * FROM beatmap_analysis")
        }
        queue = {
            int(row["beatmap_id"]): dict(row)
            for row in conn.execute("SELECT * FROM analysis_queue")
        }
        favorite_ids = {
            int(row["beatmapset_id"])
            for row in conn.execute("SELECT beatmapset_id FROM favorites")
        }
        feedback_ids = {
            int(row["beatmapset_id"])
            for row in conn.execute("SELECT beatmapset_id FROM feedback")
        }
        recent_rows = conn.execute(
            "SELECT DISTINCT beatmapset_id FROM history "
            "ORDER BY shown_at DESC LIMIT 300"
        ).fetchall()
        recent_ids = {int(row["beatmapset_id"]) for row in recent_rows}

    rows: list[tuple[int, int, str]] = []
    for row in beatmaps:
        beatmap_id = int(row["id"])
        rows_existing = analyses.get(beatmap_id)
        queued = queue.get(beatmap_id)
        if queued is not None and queued.get("status") in {
            "processing",
            "pending",
        }:
            continue
        file_path = _cache_file(store, beatmap_id)
        file_exists = file_path.exists()
        if not file_exists and not allow_download:
            continue
        reason = ""
        if rows_existing is None:
            reason = "no_analysis"
        elif int(rows_existing.get("analyzer_version", 0)) != (
            patterns.ANALYZER_VERSION
        ):
            reason = "analyzer_changed"
        elif str(rows_existing.get("status")) == evidence.FAILED:
            if int(rows_existing.get("attempts", 0)) >= analysis.MAX_ATTEMPTS:
                continue
            reason = "retry_failed"
        elif str(rows_existing.get("status")) in {
            evidence.MEASURED,
            evidence.PARTIAL,
        } and file_exists:
            try:
                stat = file_path.stat()
            except OSError:
                continue
            if (
                rows_existing.get("file_mtime_ns") is not None
                and (
                    int(rows_existing["file_mtime_ns"]) != stat.st_mtime_ns
                    or int(rows_existing.get("file_size") or -1)
                    != stat.st_size
                )
            ):
                reason = "file_changed"
        if not reason:
            continue
        if (
            reason == "retry_failed"
            and queued is not None
            and int(queued.get("attempts", 0))
            >= int(queued.get("max_attempts", 3))
        ):
            continue
        set_id = int(row["beatmapset_id"])
        priority = 100
        if set_id in favorite_ids:
            priority = 0
        elif set_id in feedback_ids:
            priority = 10
        elif set_id in recent_ids:
            priority = 20
        rows.append((beatmap_id, priority, reason))

    if not rows:
        return 0
    added = store.enqueue_analysis(
        rows, max_attempts=config.DEFAULT_ANALYSIS_MAX_ATTEMPTS
    )
    # A new/changed source is not a failed attempt: give it a fresh retry
    # budget instead of inheriting a previous exhausted counter.
    reset_ids = [
        beatmap_id
        for beatmap_id, _, reason in rows
        if reason in {"no_analysis", "analyzer_changed", "file_changed"}
    ]
    if reset_ids:
        with store.connection() as conn:
            placeholders = ",".join("?" for _ in reset_ids)
            conn.execute(
                f"UPDATE analysis_queue SET attempts=0, last_error='' "
                f"WHERE beatmap_id IN ({placeholders})",
                reset_ids,
            )
    return added


def prioritize_beatmaps(
    store: Storage,
    beatmap_ids: list[int] | tuple[int, ...],
    *,
    priority: int = -5,
    reason: str = "active_result",
) -> int:
    """Nudge the current search/random result to the front of the queue.

    Existing queue rows are reprioritized without creating duplicates. Only
    missing/stale analysis is inserted as a new pending job.
    """

    wanted = list(dict.fromkeys(int(value) for value in beatmap_ids))
    if not wanted:
        return 0
    with store.connection() as conn:
        analyses = {
            int(row["beatmap_id"]): dict(row)
            for row in conn.execute(
                "SELECT * FROM beatmap_analysis"
            )
        }
        queued = {
            int(row["beatmap_id"]): dict(row)
            for row in conn.execute("SELECT * FROM analysis_queue")
        }
    new_rows: list[tuple[int, int, str]] = []
    for beatmap_id in wanted:
        existing_queue = queued.get(beatmap_id)
        if existing_queue is not None:
            continue
        existing = analyses.get(beatmap_id)
        if existing is not None:
            if int(existing.get("analyzer_version", 0)) == (
                patterns.ANALYZER_VERSION
            ) and str(existing.get("status")) in {
                evidence.MEASURED,
                evidence.PARTIAL,
            }:
                continue
            if str(existing.get("status")) == evidence.FAILED and (
                int(existing.get("attempts", 0)) >= analysis.MAX_ATTEMPTS
            ):
                continue
        new_rows.append((beatmap_id, int(priority), str(reason)))
    if new_rows:
        store.enqueue_analysis(
            new_rows, max_attempts=config.DEFAULT_ANALYSIS_MAX_ATTEMPTS
        )
    store.prioritize_analysis(
        wanted, priority=priority, reason=reason
    )
    return len(new_rows)


def _configured() -> bool:
    try:
        return bool(config.load_api_config().configured)
    except ValueError:
        return False


def _preset_for_worker(store: Storage):
    raw = store.get_preference("selected_preset")
    try:
        preset = store.get_preset(int(raw)) if raw else None
    except (TypeError, ValueError):
        preset = None
    if preset is None:
        preset = store.get_preset_by_name("Mixed Aim")
    return preset


def run_auto_discovery(
    store: Storage, *, deadline_seconds: float | None = None
) -> str:
    """Bounded catalog refresh. Returns a short status string."""

    if not _configured():
        return "API не настроен; discovery пропущен."
    preset = _preset_for_worker(store)
    if preset is None:
        return "Нет активного пресета; discovery пропущен."
    settings = DiscoverySettings.from_dict(
        preset.config.discovery.to_dict()
    )
    settings.max_requests = min(
        int(settings.max_requests),
        config.DEFAULT_AUTO_DISCOVERY_MAX_REQUESTS,
    )
    settings.max_pages_per_strategy = 1
    settings.fetch_attributes_limit = 0
    settings.fetch_sequences_limit = 0
    client = None
    try:
        api_config = config.load_api_config()
        client = OsuApiClient(api_config)
        engine = DiscoveryEngine(
            client,
            store,
            settings,
            deadline_seconds=(
                deadline_seconds
                or config.DEFAULT_AUTO_DISCOVERY_DEADLINE_SECONDS
            ),
        )
        result = engine.run(
            preset.config.filters,
            preset.config.ranking,
            record=True,
        )
        return (
            f"Автопоиск: {result.count} карт, запросов "
            f"{result.api_requests}, полнота "
            f"{'да' if result.complete else 'нет'}."
        )
    except Exception as exc:  # noqa: BLE001 - worker must survive
        logger.warning("auto discovery failed: %s", type(exc).__name__)
        return f"Автопоиск не удался: {type(exc).__name__}."
    finally:
        if client is not None:
            client.close()


class AnalysisWorker:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.owner = (
            f"{socket.gethostname()}:{os.getpid()}:"
            f"{uuid.uuid4().hex[:8]}"
        )
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    def _heartbeat(self, store: Storage) -> bool:
        return store.heartbeat_worker(
            self.owner, config.DEFAULT_ANALYSIS_LEASE_SECONDS
        )

    def _wait_for_lease(self, store: Storage) -> bool:
        while not self.stop_event.is_set():
            try:
                if store.acquire_worker_lease(
                    self.owner,
                    config.DEFAULT_ANALYSIS_LEASE_SECONDS,
                ):
                    return True
            except Exception as exc:  # noqa: BLE001
                logger.warning("worker lease failed: %s", type(exc).__name__)
            self.stop_event.wait(2.0)
        return False

    def _process_job(
        self, store: Storage, row: dict[str, Any], allow_download: bool
    ) -> None:
        beatmap_id = int(row["beatmap_id"])
        attempts = int(row.get("attempts", 0))
        max_attempts = int(row.get("max_attempts", 3))
        if allow_download and not _cache_file(store, beatmap_id).exists():
            if not analysis.peek_analysis(store, beatmap_id).usable:
                _throttle_download(
                    config.DEFAULT_WORKER_DOWNLOAD_INTERVAL_SECONDS
                )
        try:
            result = analysis.analyze_beatmap(
                store,
                beatmap_id,
                allow_download=allow_download,
            )
        except Exception as exc:  # noqa: BLE001
            result = None
            error = type(exc).__name__
        else:
            error = ""
        if result is not None and result.usable:
            store.finish_analysis(
                beatmap_id,
                status="done",
                attempts=attempts,
                next_attempt_at=None,
            )
            return
        if result is not None and result.retry_after:
            _set_rate_limit(result.retry_after)
        if (
            result is not None
            and result.status == evidence.SOURCE_UNAVAILABLE
            and not allow_download
        ):
            store.finish_analysis(
                beatmap_id,
                status="unavailable",
                attempts=attempts,
                next_attempt_at=None,
                error="нет .osu; API не настроен",
            )
            return
        attempts += 1
        if attempts >= max_attempts:
            store.finish_analysis(
                beatmap_id,
                status="failed",
                attempts=attempts,
                next_attempt_at=None,
                error=error or (
                    result.scope if result is not None else "ошибка анализа"
                ),
            )
        else:
            if result is not None and result.retry_after:
                next_at = _iso(
                    _now()
                    + timedelta(
                        seconds=max(
                            1.0,
                            min(300.0, float(result.retry_after)),
                        )
                    )
                )
            else:
                next_at = _iso(_backoff(attempts - 1))
            store.finish_analysis(
                beatmap_id,
                status="pending",
                attempts=attempts,
                next_attempt_at=next_at,
                error=error or (
                    result.scope if result is not None else "ошибка анализа"
                ),
            )

    def process_batch(
        self,
        store: Storage,
        rows: list[dict[str, Any]],
        allow_download: bool,
    ) -> None:
        """Process one claimed batch with bounded, database-safe concurrency."""

        if not rows:
            return
        workers = min(_analysis_concurrency(), len(rows))
        if workers <= 1:
            for row in rows:
                self._process_job(store, row, allow_download)
            return
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="osu-analysis-job",
        ) as pool:
            futures = {
                pool.submit(self._process_job, store, row, allow_download): row
                for row in rows
            }
            pending = set(futures)
            while pending and not self.stop_event.is_set():
                _, pending = wait(pending, timeout=5.0)
                self._heartbeat(store)
            for future in futures:
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001 - one job must not stop
                    row = futures[future]
                    logger.warning(
                        "analysis job failed: %s", type(exc).__name__
                    )
                    attempts = int(row.get("attempts", 0)) + 1
                    maximum = int(row.get("max_attempts", 3))
                    try:
                        store.finish_analysis(
                            int(row["beatmap_id"]),
                            status=(
                                "failed"
                                if attempts >= maximum
                                else "pending"
                            ),
                            attempts=attempts,
                            next_attempt_at=(
                                None
                                if attempts >= maximum
                                else _iso(_backoff(attempts - 1))
                            ),
                            error=type(exc).__name__,
                        )
                    except Exception:  # noqa: BLE001
                        pass

    def _maybe_discovery(self, store: Storage) -> None:
        requested = store.consume_worker_discovery_request()
        # The portable launcher opens the browser without making an osu! API
        # request. Scheduled discovery is deferred until the user explicitly
        # requests it from the Analysis page; queued analysis work is separate.
        if (
            not requested
            and os.getenv("OSU_PICKER_DEFER_AUTO_DISCOVERY", "").strip()
            in {"1", "true", "yes"}
        ):
            return
        if not _configured():
            if requested:
                store.set_worker_error("Discovery запрошен, но API не настроен.")
            return
        if not requested:
            state = store.worker_state()
            last = parse_datetime(state.get("last_discovery_at"))
            interval = timedelta(
                minutes=config.DEFAULT_AUTO_DISCOVERY_INTERVAL_MINUTES
            )
            if last is not None and _now() - last < interval:
                return
        # Discovery is allowed even while the analysis queue is large. It is
        # bounded independently and must not be starved by analysis backlog.
        message = run_auto_discovery(store)
        # Record the attempt time, not lease-acquisition time. A failed
        # attempt also has a real timestamp and is retried only on schedule.
        store.set_worker_discovery_time()
        if "не удался" in message:
            store.set_worker_error(message)
        logger.info("%s", message)

    def run(self) -> None:
        store = Storage(self.path)
        if not self._wait_for_lease(store):
            return
        try:
            store.recover_analysis_jobs()
            last_enqueue = 0.0
            while not self.stop_event.is_set():
                try:
                    state = store.worker_state()
                    if bool(state.get("paused")):
                        self._heartbeat(store)
                        self.stop_event.wait(config.DEFAULT_WORKER_POLL_SECONDS)
                        continue
                    configured = _configured()
                    now = time.monotonic()
                    if now - last_enqueue >= 60.0:
                        added = enqueue_catalog(
                            store, allow_download=configured
                        )
                        if added:
                            logger.info("enqueued %s analysis jobs", added)
                        last_enqueue = now
                    self._heartbeat(store)
                    self._maybe_discovery(store)
                    batch = store.claim_analysis(
                        self.owner, config.DEFAULT_ANALYSIS_BATCH_SIZE
                    )
                    if not batch:
                        self.stop_event.wait(
                            config.DEFAULT_WORKER_POLL_SECONDS
                        )
                        continue
                    if bool(store.worker_state().get("paused")):
                        for row in batch:
                            store.finish_analysis(
                                int(row["beatmap_id"]),
                                status="pending",
                                attempts=int(row.get("attempts", 0)),
                                next_attempt_at=None,
                            )
                        continue
                    self.process_batch(store, batch, configured)
                    self._heartbeat(store)
                    # Newly analyzed rows may make more catalog work eligible.
                    last_enqueue = 0.0
                except Exception as exc:  # noqa: BLE001 - keep worker alive
                    logger.warning(
                        "analysis worker loop error: %s",
                        type(exc).__name__,
                    )
                    store.set_worker_error(type(exc).__name__)
                    self.stop_event.wait(config.DEFAULT_WORKER_POLL_SECONDS)
        finally:
            store.release_worker_lease(self.owner)


def ensure_worker(store: Storage) -> None:
    """Start the single in-process worker if background work is enabled."""

    global _THREAD, _WORKER
    if not _worker_enabled():
        return
    with _START_LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return
        worker = AnalysisWorker(store.path)
        thread = threading.Thread(
            target=worker.run,
            name="osu-analysis-worker",
            daemon=True,
        )
        _WORKER = worker
        _THREAD = thread
        thread.start()


def stop_worker(wait_seconds: float = 2.0) -> None:
    global _WORKER, _THREAD
    with _START_LOCK:
        if _WORKER is not None:
            _WORKER.stop()
        if _THREAD is not None:
            _THREAD.join(timeout=max(0.0, wait_seconds))
        _WORKER = None
        _THREAD = None


def worker_alive() -> bool:
    return _THREAD is not None and _THREAD.is_alive()


def set_paused(store: Storage, paused: bool) -> None:
    store.set_worker_paused(paused)


def worker_status(store: Storage) -> dict[str, Any]:
    state = store.worker_state()
    queue = store.queue_stats()
    analysis_stats = store.analysis_stats(patterns.ANALYZER_VERSION)
    heartbeat = parse_datetime(state.get("heartbeat_at"))
    age = (
        max(0.0, (_now() - heartbeat).total_seconds())
        if heartbeat is not None
        else None
    )
    measured = int(analysis_stats.get(evidence.MEASURED, 0))
    partial = int(analysis_stats.get(evidence.PARTIAL, 0))
    return {
        "enabled": _worker_enabled(),
        "thread_alive": worker_alive(),
        "owner": str(state.get("owner", "")),
        "paused": bool(state.get("paused")),
        "heartbeat_age": age,
        "last_error": str(state.get("last_error", "")),
        "last_discovery_at": state.get("last_discovery_at"),
        "queue": queue,
        "analysis": analysis_stats,
        "analyzed": measured + partial,
        "measured": measured,
        "partial": partial,
        "catalog": store.beatmapset_count(),
        "beatmaps": store.beatmap_count(),
        "api_configured": _configured(),
    }


def process_once(
    store: Storage,
    *,
    limit: int = 1,
    allow_download: bool = False,
) -> int:
    """Synchronous helper for tests and diagnostics; no worker thread."""

    owner = f"test:{uuid.uuid4().hex[:8]}"
    if not store.acquire_worker_lease(
        owner, config.DEFAULT_ANALYSIS_LEASE_SECONDS
    ):
        return 0
    try:
        store.recover_analysis_jobs()
        batch = store.claim_analysis(owner, limit)
        worker = AnalysisWorker(store.path)
        worker.process_batch(store, batch, allow_download)
        return len(batch)
    finally:
        store.release_worker_lease(owner)
