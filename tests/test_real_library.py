"""Opt-in verification against the developer's actual local catalog.

This is intentionally skipped in ordinary CI runs because the SQLite library
is user data, not a committed fixture. Run it with:

    OSU_PICKER_RUN_REAL_LIBRARY=1 pytest tests/test_real_library.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import config
from tools.diagnose_presets import _evaluate, _rerank_overlap

pytestmark = pytest.mark.skipif(
    os.getenv("OSU_PICKER_RUN_REAL_LIBRARY") != "1",
    reason="set OSU_PICKER_RUN_REAL_LIBRARY=1 to inspect the real local library",
)


def test_real_library_streams_and_stamina_overlap() -> None:
    db = config.db_path()
    if not db.is_file():
        pytest.skip(f"local library not available: {db}")
    before = db.stat().st_mtime_ns
    result = _evaluate(db.resolve(), limit=None)
    counts = result["counts"]
    assert counts["usable_sequence"] > 0
    assert counts["stream_evidence"] > 0
    assert counts["burst_evidence"] > 0
    assert counts["streams_pass"] > 0
    assert counts["stamina_pass"] > 0
    assert counts["both_pass"] > 0
    assert counts["both_pass"] <= min(
        counts["streams_pass"], counts["stamina_pass"]
    )
    assert result["both_examples"]
    # The diagnostic copies the database; the user's file is not rewritten.
    assert db.stat().st_mtime_ns == before


def test_real_library_rerank_keeps_streams_stamina_overlap() -> None:
    db = config.db_path()
    if not db.is_file():
        pytest.skip(f"local library not available: {db}")
    before = db.stat().st_mtime_ns
    overlap = _rerank_overlap(db.resolve())
    assert overlap["streams_count"] > 0
    assert overlap["stamina_count"] > 0
    assert overlap["both_count"] > 0
    assert overlap["both_count"] <= min(
        overlap["streams_count"], overlap["stamina_count"]
    )
    assert overlap["both_ids"]
    assert db.stat().st_mtime_ns == before
