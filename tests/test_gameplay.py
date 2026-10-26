from __future__ import annotations

import re
from pathlib import Path

import pytest

import gameplay


SAMPLE = """osu file format v14

[General]
AudioLeadIn: 500
Mode: 0

[Metadata]
Title:Test Song
Artist:Test Artist
Creator:Test Mapper
Version:Hard

[Difficulty]
ApproachRate:9.6
CircleSize:4.2
OverallDifficulty:8.0
SliderMultiplier:1.4

[TimingPoints]
0,500,4,2,1,60,1,0
0,-50,4,2,1,60,0,0

[HitObjects]
100,100,1000,1,0,0:0:0:0:
200,200,2000,2,0,L|300:200,1,140
256,192,3000,8,0,4000
"""


def test_parse_osu_reads_metadata_timings_slider_and_spinner() -> None:
    preview = gameplay.parse_osu(SAMPLE, beatmap_id=75)
    assert preview.beatmap_id == 75
    assert preview.display_title == "Test Artist — Test Song"
    assert preview.creator == "Test Mapper"
    assert preview.version == "Hard"
    assert preview.approach_rate == pytest.approx(9.6)
    assert preview.circle_size == pytest.approx(4.2)
    assert preview.overall_difficulty == pytest.approx(8.0)
    assert len(preview.objects) == 3

    circle, slider, spinner = preview.objects
    assert circle.kind == "circle"
    assert (circle.start, circle.end) == (1000, 1000)
    assert (circle.x, circle.y) == (100, 100)
    assert slider.kind == "slider"
    assert slider.points == ((200.0, 200.0), (300.0, 200.0))
    # 140 px at 1.4 * 100 * 2 SV with a 500 ms beat = 250 ms.
    assert slider.start == 2000
    assert slider.end == 2250
    assert spinner.kind == "spinner"
    assert (spinner.start, spinner.end) == (3000, 4000)
    assert circle.beat_length == pytest.approx(500.0)
    assert slider.beat_length == pytest.approx(500.0)
    assert preview.truncated is False
    assert preview.last_time == 4000


def test_parse_osu_rejects_non_standard_and_empty_documents() -> None:
    with pytest.raises(gameplay.GameplayError):
        gameplay.parse_osu(SAMPLE.replace("Mode: 0", "Mode: 3"))
    with pytest.raises(gameplay.GameplayError):
        gameplay.parse_osu("not a beatmap")
    with pytest.raises(gameplay.GameplayError):
        gameplay.parse_osu(
            "[Metadata]\nTitle:x\n[HitObjects]\n"
        )


def test_download_verifies_and_reuses_the_exact_difficulty(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def fetcher(url: str) -> bytes:
        calls.append(url)
        return SAMPLE.encode("utf-8")

    cache = tmp_path / "gameplay"
    first = gameplay.download_osu(75, cache, fetcher=fetcher)
    second = gameplay.download_osu(75, cache, fetcher=fetcher)
    assert first == second == SAMPLE.encode("utf-8")
    assert calls == ["https://osu.ppy.sh/osu/75"]
    assert (cache / "75.osu").read_bytes() == first

    loaded = gameplay.load_preview(76, cache, fetcher=fetcher)
    assert loaded.beatmap_id == 76
    assert calls[-1] == "https://osu.ppy.sh/osu/76"


def test_invalid_cache_file_is_replaced(tmp_path: Path) -> None:
    cache = tmp_path / "gameplay"
    cache.mkdir()
    (cache / "75.osu").write_text("broken", encoding="utf-8")
    calls: list[str] = []

    def fetcher(url: str) -> bytes:
        calls.append(url)
        return SAMPLE.encode("utf-8")

    gameplay.download_osu(75, cache, fetcher=fetcher)
    assert calls == ["https://osu.ppy.sh/osu/75"]
    assert gameplay.parse_osu((cache / "75.osu").read_bytes()).version == "Hard"


def test_download_rejects_html_and_invalid_ids(tmp_path: Path) -> None:
    with pytest.raises(gameplay.GameplayError):
        gameplay.download_osu(0, tmp_path)
    with pytest.raises(gameplay.GameplayError):
        gameplay.download_osu(
            75,
            tmp_path,
            fetcher=lambda url: b"<html>not a beatmap</html>",
        )


def test_preview_html_contains_real_objects_and_controls() -> None:
    preview = gameplay.parse_osu(SAMPLE, beatmap_id=75)
    markup = gameplay.preview_html(preview)
    assert "Test Artist — Test Song" in markup
    assert "[Hard]" in markup
    assert "3 objects" in markup
    assert "REAL .OSU PREVIEW" in markup
    assert "gp-play" in markup
    assert "gp-seek" in markup
    assert "gp-speed" in markup
    assert '"last":4000' in markup
    assert '"k":"s"' in markup
    assert '"k":"p"' in markup
    assert '"p":[[200.0,200.0],[300.0,200.0]]' in markup
    # The preview starts playing automatically; Play/Pause and seek remain
    # wired to the same state. This avoids a blank 0:00 field looking broken.
    assert "playing = true" in markup
    assert "⏸ Pause" in markup
    assert "current >= lastTime" in markup
    assert "current = 0" in markup
    assert "seek.addEventListener" in markup
    assert "speedSelect.addEventListener" in markup


def test_streamlit_requirement_supports_unsafe_html_javascript() -> None:
    requirements = (
        Path(__file__).resolve().parents[1] / "requirements.txt"
    ).read_text(encoding="utf-8")
    match = re.search(r"streamlit>=(\d+)\.(\d+)", requirements)
    assert match is not None
    # unsafe_allow_javascript was added in Streamlit 1.52. Older versions make
    # st.html reject the argument, so the preview cannot execute in them.
    assert (int(match.group(1)), int(match.group(2))) >= (1, 52)
