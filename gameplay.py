"""Real gameplay preview built from the selected beatmap's public ``.osu`` file.

The preview is intentionally small and honest:

* the file comes from the official ``https://osu.ppy.sh/osu/{beatmap_id}``
  endpoint and is verified before use;
* hit objects, timings, slider paths and difficulty settings are parsed from
  that file (no cover-image animation and no random fake notes);
* the browser-side canvas supports Play/Pause, seeking and playback speed.

Only the selected difficulty is requested. Files are cached next to the local
database so reopening a preview does not download the map again.
"""

from __future__ import annotations

import html
import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import httpx

OSU_FILE_URL = "https://osu.ppy.sh/osu/{beatmap_id}"
MAX_OSU_BYTES = 5_000_000
MAX_PREVIEW_OBJECTS = 6000
USER_AGENT = "Personal-osu-Map-Picker/0.4 (+local gameplay preview)"


class GameplayError(ValueError):
    """Raised when a map cannot be downloaded or safely parsed."""


@dataclass(frozen=True, slots=True)
class PreviewObject:
    start: int
    end: int
    x: float
    y: float
    kind: str  # circle | slider | spinner
    points: tuple[tuple[float, float], ...] = ()
    new_combo: bool = False
    # Uninherited beat length active at this object. Sequence analysis uses it
    # to distinguish 1/4 from 1/2 without assuming one global BPM.
    beat_length: float = 500.0


@dataclass(frozen=True, slots=True)
class BeatmapPreview:
    beatmap_id: int
    artist: str
    title: str
    creator: str
    version: str
    mode: int
    approach_rate: float
    circle_size: float
    overall_difficulty: float
    slider_multiplier: float
    audio_lead_in: int
    objects: tuple[PreviewObject, ...]
    truncated: bool = False

    @property
    def last_time(self) -> int:
        if not self.objects:
            return 0
        return max(item.end for item in self.objects)

    @property
    def display_title(self) -> str:
        if self.artist:
            return f"{self.artist} — {self.title}"
        return self.title


@dataclass(frozen=True, slots=True)
class _TimingPoint:
    time: float
    beat_length: float
    uninherited: bool


def _parse_float(value: str, default: float) -> float:
    try:
        number = float(value.strip())
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _parse_int(value: str, default: int) -> int:
    try:
        return int(float(value.strip()))
    except (TypeError, ValueError):
        return default


def _split_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(raw_line)
    return sections


def _difficulty_value(
    difficulty: dict[str, str], key: str, default: float
) -> float:
    raw = difficulty.get(key)
    return _parse_float(raw, default) if raw is not None else default


def _parse_timing_points(lines: Iterable[str]) -> list[_TimingPoint]:
    result: list[_TimingPoint] = []
    for line in lines:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        time = _parse_float(parts[0], -1)
        beat = _parse_float(parts[1], 0)
        if time < 0 or beat == 0:
            continue
        uninherited = beat > 0
        if len(parts) > 6:
            uninherited = parts[6].strip() != "0"
        result.append(_TimingPoint(time, abs(beat), uninherited))
    result.sort(key=lambda item: item.time)
    return result


def _timing_at(
    when: float, timing: list[_TimingPoint]
) -> tuple[float, float]:
    """Return (beat length, slider velocity multiplier) at ``when``."""

    beat_length = 500.0
    sv = 1.0
    for point in timing:
        if point.time > when:
            break
        if point.uninherited:
            beat_length = point.beat_length
            sv = 1.0
        else:
            # Inherited points store -100 / SV as a negative beat length.
            raw = max(0.01, point.beat_length)
            sv = max(0.1, min(10.0, 100.0 / raw))
    return beat_length, sv


def _curve_points(raw: str, start_x: float, start_y: float) -> tuple[tuple[float, float], ...]:
    points: list[tuple[float, float]] = [(start_x, start_y)]
    pieces = raw.split("|")
    for piece in pieces[1:]:
        if ":" not in piece:
            continue
        x_raw, y_raw = piece.split(":", 1)
        points.append((_parse_float(x_raw, start_x), _parse_float(y_raw, start_y)))
    if len(points) == 1:
        points.append((start_x, start_y))
    return tuple(points)


def parse_osu(
    content: str | bytes,
    *,
    beatmap_id: int = 0,
) -> BeatmapPreview:
    """Parse a public ``.osu`` document into the small preview model."""

    if isinstance(content, bytes):
        if not content:
            raise GameplayError("Пустой .osu-файл.")
        if len(content) > MAX_OSU_BYTES:
            raise GameplayError("Файл карты слишком большой для превью.")
        text = content.decode("utf-8-sig", errors="replace")
    else:
        text = content.lstrip("\ufeff")
    if "[HitObjects]" not in text or "[Metadata]" not in text:
        raise GameplayError("Ответ не похож на .osu: нет Metadata или HitObjects.")

    sections = _split_sections(text)
    metadata: dict[str, str] = {}
    for line in sections.get("Metadata", []):
        if ":" in line:
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip()
    general: dict[str, str] = {}
    for line in sections.get("General", []):
        if ":" in line:
            key, value = line.split(":", 1)
            general[key.strip()] = value.strip()
    difficulty: dict[str, str] = {}
    for line in sections.get("Difficulty", []):
        if ":" in line:
            key, value = line.split(":", 1)
            difficulty[key.strip()] = value.strip()

    mode = _parse_int(general.get("Mode", "0"), 0)
    if mode != 0:
        raise GameplayError("Превью геймплея поддерживает только osu!standard.")
    od = _difficulty_value(difficulty, "OverallDifficulty", 5.0)
    ar = _difficulty_value(difficulty, "ApproachRate", od)
    cs = _difficulty_value(difficulty, "CircleSize", 4.0)
    slider_multiplier = _difficulty_value(difficulty, "SliderMultiplier", 1.4)
    audio_lead_in = _parse_int(general.get("AudioLeadIn", "0"), 0)
    timing = _parse_timing_points(sections.get("TimingPoints", []))

    objects: list[PreviewObject] = []
    truncated = False
    for raw_line in sections.get("HitObjects", []):
        if len(objects) >= MAX_PREVIEW_OBJECTS:
            truncated = True
            break
        parts = raw_line.split(",")
        if len(parts) < 5:
            continue
        x = _parse_float(parts[0], 0.0)
        y = _parse_float(parts[1], 0.0)
        start = _parse_int(parts[2], 0)
        type_bits = _parse_int(parts[3], 0)
        new_combo = bool(type_bits & 4)
        beat_length, sv = _timing_at(start, timing)
        if type_bits & 2 and len(parts) >= 8:
            curve = _curve_points(parts[5], x, y)
            length = max(0.0, _parse_float(parts[7], 0.0))
            velocity = max(0.1, slider_multiplier * 100.0 * sv)
            duration = int(round(length / velocity * beat_length))
            duration = max(50, min(30_000, duration))
            objects.append(
                PreviewObject(
                    start=start,
                    end=start + duration,
                    x=x,
                    y=y,
                    kind="slider",
                    points=curve,
                    new_combo=new_combo,
                    beat_length=beat_length,
                )
            )
        elif type_bits & 8 and len(parts) >= 6:
            end = max(start, _parse_int(parts[5], start))
            objects.append(
                PreviewObject(
                    start=start,
                    end=end,
                    x=x,
                    y=y,
                    kind="spinner",
                    new_combo=new_combo,
                    beat_length=beat_length,
                )
            )
        elif type_bits & 1:
            objects.append(
                PreviewObject(
                    start=start,
                    end=start,
                    x=x,
                    y=y,
                    kind="circle",
                    new_combo=new_combo,
                    beat_length=beat_length,
                )
            )
    if not objects:
        raise GameplayError("В .osu нет поддерживаемых hit objects.")
    objects.sort(key=lambda item: (item.start, item.end, item.x, item.y))

    return BeatmapPreview(
        beatmap_id=int(beatmap_id),
        artist=metadata.get("Artist", ""),
        title=metadata.get("Title", ""),
        creator=metadata.get("Creator", ""),
        version=metadata.get("Version", ""),
        mode=mode,
        approach_rate=max(0.0, min(11.0, ar)),
        circle_size=max(0.0, min(11.0, cs)),
        overall_difficulty=max(0.0, min(11.0, od)),
        slider_multiplier=max(0.1, slider_multiplier),
        audio_lead_in=max(0, audio_lead_in),
        objects=tuple(objects),
        truncated=truncated,
    )


def _validate_download(data: bytes) -> None:
    if not data:
        raise GameplayError("osu! вернул пустой .osu-файл.")
    if len(data) > MAX_OSU_BYTES:
        raise GameplayError("Скачанный .osu больше допустимого размера.")
    head = data[:4096].decode("utf-8", errors="replace")
    if "osu file format" not in head or "[HitObjects]" not in data.decode(
        "utf-8", errors="replace"
    ):
        raise GameplayError("Ответ osu! не похож на .osu-файл.")


def download_osu(
    beatmap_id: int,
    cache_dir: Path,
    *,
    fetcher: Callable[[str], bytes] | None = None,
    timeout: float = 20.0,
) -> bytes:
    """Download and cache one public ``.osu`` file by difficulty id."""

    beatmap_id = int(beatmap_id)
    if beatmap_id <= 0:
        raise GameplayError("Некорректный beatmap id.")
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{beatmap_id}.osu"
    if path.exists():
        try:
            cached = path.read_bytes()
        except OSError as exc:
            raise GameplayError(f"Не удалось прочитать кеш .osu: {exc}") from exc
        try:
            _validate_download(cached)
        except GameplayError:
            try:
                path.unlink()
            except OSError:
                pass
        else:
            return cached

    url = OSU_FILE_URL.format(beatmap_id=beatmap_id)
    try:
        if fetcher is not None:
            data = fetcher(url)
        else:
            response = httpx.get(
                url,
                timeout=timeout,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            )
            if response.status_code == 404:
                raise GameplayError(
                    "osu! не нашёл .osu-файл для этой difficulty."
                )
            if response.status_code >= 400:
                raise GameplayError(
                    f"osu! вернул HTTP {response.status_code} для .osu."
                )
            data = response.content
    except GameplayError:
        raise
    except (httpx.HTTPError, OSError) as exc:
        raise GameplayError(f"Не удалось скачать .osu: {exc}") from exc
    _validate_download(data)

    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{beatmap_id}-", suffix=".osu.tmp", dir=cache_dir
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temp_name, path)
    except OSError as exc:
        raise GameplayError(f"Не удалось сохранить .osu в кеш: {exc}") from exc
    return data


def load_preview(
    beatmap_id: int,
    cache_dir: Path,
    *,
    fetcher: Callable[[str], bytes] | None = None,
) -> BeatmapPreview:
    data = download_osu(beatmap_id, cache_dir, fetcher=fetcher)
    return parse_osu(data, beatmap_id=beatmap_id)


def load_cached_preview(beatmap_id: int, cache_dir: Path) -> BeatmapPreview:
    """Read one already cached ``.osu`` file without making a network request."""

    beatmap_id = int(beatmap_id)
    if beatmap_id <= 0:
        raise GameplayError("Некорректный beatmap id.")
    path = Path(cache_dir) / f"{beatmap_id}.osu"
    if not path.exists():
        raise GameplayError("Нет .osu-файла в локальном кеше.")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise GameplayError(f"Не удалось прочитать кеш .osu: {exc}") from exc
    _validate_download(data)
    return parse_osu(data, beatmap_id=beatmap_id)


def _payload(preview: BeatmapPreview) -> dict[str, object]:
    objects = []
    for item in preview.objects:
        row: dict[str, object] = {
            "s": item.start,
            "e": item.end,
            "x": round(item.x, 2),
            "y": round(item.y, 2),
            "k": {"circle": "c", "slider": "s", "spinner": "p"}[item.kind],
        }
        if item.new_combo:
            row["c"] = 1
        if item.points:
            row["p"] = [[round(x, 2), round(y, 2)] for x, y in item.points]
        objects.append(row)
    return {
        "id": preview.beatmap_id,
        "ar": preview.approach_rate,
        "cs": preview.circle_size,
        "od": preview.overall_difficulty,
        "lead": preview.audio_lead_in,
        "last": preview.last_time,
        "objects": objects,
    }


def preview_html(preview: BeatmapPreview) -> str:
    """Return a self-contained canvas preview with real hit objects."""

    element_id = f"osu-gp-{preview.beatmap_id}"
    data = _payload(preview)
    data_json = json.dumps(
        data, ensure_ascii=False, separators=(",", ":")
    ).replace("</", "<\\/")
    title = html.escape(preview.display_title or "osu! map", quote=True)
    version = html.escape(preview.version or "difficulty", quote=True)
    creator = html.escape(preview.creator or "unknown", quote=True)
    meta = html.escape(
        f"{preview.approach_rate:g} AR · {preview.circle_size:g} CS · "
        f"{len(preview.objects)} objects",
        quote=True,
    )
    return f"""
<div class="osu-gameplay" id="{element_id}">
  <div class="gp-head">
    <div>
      <div class="gp-title">{title}</div>
      <div class="gp-sub">[{version}] · mapped by {creator} · {meta}</div>
    </div>
    <div class="gp-live">REAL .OSU PREVIEW</div>
  </div>
  <canvas class="gp-canvas" width="760" height="570" aria-label="osu! gameplay preview"></canvas>
  <div class="gp-controls">
    <button type="button" class="gp-play">▶ Play</button>
    <input class="gp-seek" type="range" min="0" max="{max(1, preview.last_time + 500)}" value="0" step="1" aria-label="Seek">
    <span class="gp-time">0:00.000</span>
    <label class="gp-speed-label">Speed
      <select class="gp-speed">
        <option value="0.5">0.5×</option>
        <option value="0.75">0.75×</option>
        <option value="1" selected>1×</option>
        <option value="1.25">1.25×</option>
        <option value="1.5">1.5×</option>
        <option value="2">2×</option>
      </select>
    </label>
  </div>
  <div class="gp-note">Hit objects, positions, timings and slider paths are parsed from the public .osu of this exact difficulty. Audio is not included.</div>
  <script type="application/json" class="gp-data">{data_json}</script>
</div>
<style>
.osu-gameplay {{
  background: linear-gradient(160deg, #14101c, #0a0910 70%);
  border: 1px solid rgba(255,255,255,.13);
  border-radius: 14px;
  padding: .8rem;
  margin: .5rem 0 1rem;
  color: #f7f1f7;
  font-family: "Segoe UI", system-ui, sans-serif;
  box-shadow: 0 18px 45px rgba(0,0,0,.28);
}}
.osu-gameplay .gp-head {{ display:flex; justify-content:space-between; gap:1rem; align-items:flex-start; margin-bottom:.55rem; }}
.osu-gameplay .gp-title {{ font-weight:700; font-size:1.02rem; }}
.osu-gameplay .gp-sub {{ color:#b9acb9; font-size:.77rem; margin-top:.12rem; }}
.osu-gameplay .gp-live {{ color:#ff66aa; border:1px solid rgba(255,102,170,.45); border-radius:99px; padding:.12rem .5rem; font-size:.63rem; letter-spacing:.08em; white-space:nowrap; }}
.osu-gameplay .gp-canvas {{ display:block; width:100%; aspect-ratio:4/3; border-radius:10px; background:#090910; }}
.osu-gameplay .gp-controls {{ display:flex; align-items:center; gap:.65rem; flex-wrap:wrap; margin-top:.55rem; }}
.osu-gameplay .gp-play {{ background:#ff66aa; color:#1b0e18; border:0; border-radius:8px; padding:.34rem .8rem; font-weight:700; cursor:pointer; }}
.osu-gameplay .gp-play:hover {{ filter:brightness(1.08); }}
.osu-gameplay .gp-seek {{ flex:1 1 230px; accent-color:#ff66aa; }}
.osu-gameplay .gp-time {{ font-variant-numeric:tabular-nums; color:#e9dfe9; min-width:5.4rem; font-size:.8rem; }}
.osu-gameplay .gp-speed-label {{ color:#b9acb9; font-size:.76rem; }}
.osu-gameplay .gp-speed {{ background:#221a2a; color:#f7f1f7; border:1px solid rgba(255,255,255,.16); border-radius:6px; padding:.18rem; }}
.osu-gameplay .gp-note {{ color:#9f93a4; font-size:.68rem; line-height:1.35; margin-top:.45rem; }}
</style>
<script>
(function () {{
  const root = document.getElementById({json.dumps(element_id)});
  if (!root) return;
  const canvas = root.querySelector('.gp-canvas');
  const ctx = canvas.getContext('2d');
  const dataNode = root.querySelector('.gp-data');
  const data = JSON.parse(dataNode.textContent || '{{}}');
  const objects = Array.isArray(data.objects) ? data.objects : [];
  const button = root.querySelector('.gp-play');
  const seek = root.querySelector('.gp-seek');
  const timeLabel = root.querySelector('.gp-time');
  const speedSelect = root.querySelector('.gp-speed');
  const lastTime = Math.max(0, Number(data.last) || 0);
  const ar = Number(data.ar) || 5;
  const cs = Number(data.cs) || 4;
  const preempt = ar < 5 ? 1200 + 120 * (5 - ar) : 1200 - 150 * (ar - 5);
  let current = 0;
  let playing = false;
  let speed = 1;
  let lastFrame = 0;
  let width = 760;
  let height = 570;

  function fit() {{
    const rect = canvas.getBoundingClientRect();
    const dpr = Math.max(1, window.devicePixelRatio || 1);
    width = Math.max(320, rect.width || 760);
    height = width * 0.75;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    canvas.style.height = height + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }}

  function pad() {{ return Math.max(10, width * 0.022); }}
  function scale() {{ return (width - pad() * 2) / 512; }}
  function sx(x) {{ return pad() + Number(x) * scale(); }}
  function sy(y) {{ return pad() + Number(y) * scale(); }}
  function radius() {{ return Math.max(6, scale() * (54.4 - 4.48 * cs) / 2); }}
  function fmt(ms) {{
    const total = Math.max(0, Math.floor(ms));
    const m = Math.floor(total / 60000);
    const s = Math.floor((total % 60000) / 1000);
    const millis = total % 1000;
    return m + ':' + String(s).padStart(2, '0') + '.' + String(millis).padStart(3, '0');
  }}

  function pathLength(points) {{
    let total = 0;
    for (let i = 1; i < points.length; i++) {{
      total += Math.hypot(points[i][0] - points[i-1][0], points[i][1] - points[i-1][1]);
    }}
    return total;
  }}

  function pointAt(points, fraction) {{
    if (!points || points.length === 0) return null;
    if (points.length === 1) return points[0];
    const total = pathLength(points);
    if (total <= 0) return points[0];
    let target = Math.max(0, Math.min(1, fraction)) * total;
    for (let i = 1; i < points.length; i++) {{
      const a = points[i-1], b = points[i];
      const segment = Math.hypot(b[0]-a[0], b[1]-a[1]);
      if (target <= segment) {{
        const r = segment <= 0 ? 0 : target / segment;
        return [a[0] + (b[0]-a[0]) * r, a[1] + (b[1]-a[1]) * r];
      }}
      target -= segment;
    }}
    return points[points.length - 1];
  }}

  function drawBackground() {{
    const p = pad();
    ctx.clearRect(0, 0, width, height);
    const grad = ctx.createRadialGradient(width*.5, height*.45, 20, width*.5, height*.45, width*.7);
    grad.addColorStop(0, '#1a1422');
    grad.addColorStop(1, '#08070c');
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, width, height);
    ctx.strokeStyle = 'rgba(255,255,255,.06)';
    ctx.lineWidth = 1;
    ctx.strokeRect(p, p, width - p*2, height - p*2);
    ctx.globalAlpha = .18;
    for (let i = 1; i < 4; i++) {{
      const y = p + (height - p*2) * i / 4;
      ctx.beginPath(); ctx.moveTo(p, y); ctx.lineTo(width-p, y); ctx.stroke();
      const x = p + (width - p*2) * i / 4;
      ctx.beginPath(); ctx.moveTo(x, p); ctx.lineTo(x, height-p); ctx.stroke();
    }}
    ctx.globalAlpha = 1;
  }}

  function drawObject(obj) {{
    const start = Number(obj.s) || 0;
    const end = Math.max(start, Number(obj.e) || start);
    const visibleFrom = start - preempt;
    if (current < visibleFrom || current > end + 420) return;
    const x = sx(obj.x), y = sy(obj.y), r = radius();
    const kind = obj.k || 'c';
    if (kind === 's' && Array.isArray(obj.p) && obj.p.length > 1) {{
      const points = obj.p;
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';
      ctx.strokeStyle = 'rgba(103,200,255,.20)';
      ctx.lineWidth = r * .9;
      ctx.beginPath();
      points.forEach((pt, i) => {{ i ? ctx.lineTo(sx(pt[0]), sy(pt[1])) : ctx.moveTo(sx(pt[0]), sy(pt[1])); }});
      ctx.stroke();
      const fraction = current <= start ? 0 : Math.min(1, (current-start) / Math.max(1, end-start));
      const traveled = pointAt(points, fraction);
      if (traveled) {{
        ctx.strokeStyle = 'rgba(103,200,255,.9)';
        ctx.lineWidth = Math.max(2, r * .38);
        ctx.beginPath();
        let consumed = 0;
        const total = pathLength(points);
        ctx.moveTo(sx(points[0][0]), sy(points[0][1]));
        if (total > 0) {{
          for (let i = 1; i < points.length; i++) {{
            const a = points[i-1], b = points[i];
            const seg = Math.hypot(b[0]-a[0], b[1]-a[1]);
            if (consumed + seg <= total * fraction) {{
              ctx.lineTo(sx(b[0]), sy(b[1]));
              consumed += seg;
            }} else {{
              const need = total * fraction - consumed;
              const rr = seg <= 0 ? 0 : need / seg;
              ctx.lineTo(sx(a[0] + (b[0]-a[0])*rr), sy(a[1] + (b[1]-a[1])*rr));
              break;
            }}
          }}
        }}
        ctx.stroke();
        ctx.fillStyle = '#d9f4ff';
        ctx.beginPath(); ctx.arc(sx(traveled[0]), sy(traveled[1]), Math.max(2.5, r*.28), 0, Math.PI*2); ctx.fill();
      }}
    }}
    if (kind === 'p') {{
      const active = current >= start && current <= end;
      ctx.strokeStyle = active ? 'rgba(255,102,170,.9)' : 'rgba(255,102,170,.28)';
      ctx.lineWidth = Math.max(2, r*.25);
      ctx.beginPath();
      ctx.arc(width/2, height/2, Math.max(20, width*.12), 0, Math.PI*2);
      ctx.stroke();
      if (active) {{
        ctx.fillStyle = '#ff66aa';
        ctx.font = '600 ' + Math.max(12, width*.025) + 'px Segoe UI, sans-serif';
        ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.fillText('SPIN', width/2, height/2);
      }}
      return;
    }}

    const alpha = current < start ? Math.max(0, Math.min(1, (current - visibleFrom) / Math.max(1, preempt * .45))) : 1;
    ctx.globalAlpha = alpha;
    ctx.fillStyle = '#ff66aa';
    ctx.strokeStyle = '#fff2f8';
    ctx.lineWidth = Math.max(1.5, r*.13);
    ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI*2); ctx.fill(); ctx.stroke();
    ctx.globalAlpha = 1;
    if (current <= start) {{
      const progress = Math.max(0, Math.min(1, (current - visibleFrom) / Math.max(1, preempt)));
      const approach = r * (2.6 - 1.6 * progress);
      ctx.strokeStyle = 'rgba(255,255,255,.78)';
      ctx.lineWidth = Math.max(1, r*.1);
      ctx.beginPath(); ctx.arc(x, y, approach, 0, Math.PI*2); ctx.stroke();
      if (obj.c) {{
        ctx.fillStyle = '#ffe8f2';
        ctx.font = '600 ' + Math.max(9, r*.65) + 'px Segoe UI, sans-serif';
        ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.fillText('•', x, y - r - r*.55);
      }}
    }} else if (current <= start + 190) {{
      const k = (current - start) / 190;
      ctx.strokeStyle = 'rgba(125,235,255,' + (1-k) + ')';
      ctx.lineWidth = Math.max(1, r*.12);
      ctx.beginPath(); ctx.arc(x, y, r * (1 + k * .8), 0, Math.PI*2); ctx.stroke();
    }}
  }}

  function draw() {{
    drawBackground();
    const visible = objects.filter(obj => {{
      const s = Number(obj.s) || 0, e = Math.max(s, Number(obj.e) || s);
      return current >= s - preempt - 100 && current <= e + 600;
    }});
    visible.forEach(drawObject);
  }}

  function updateLabel() {{ timeLabel.textContent = fmt(current); seek.value = String(Math.round(current)); }}
  function frame(now) {{
    if (!lastFrame) lastFrame = now;
    const delta = Math.min(100, now - lastFrame);
    lastFrame = now;
    if (playing) {{
      current += delta * speed;
      if (current > lastTime + 350) {{ current = lastTime + 350; playing = false; button.textContent = '▶ Play'; }}
    }}
    updateLabel();
    draw();
    requestAnimationFrame(frame);
  }}

  button.addEventListener('click', () => {{
    // Play after the end means restart from the beginning, not a no-op.
    if (!playing && current >= lastTime) {{
      current = 0;
      updateLabel();
      draw();
    }}
    playing = !playing;
    button.textContent = playing ? '⏸ Pause' : '▶ Play';
    lastFrame = 0;
  }});
  seek.addEventListener('input', () => {{ current = Number(seek.value) || 0; updateLabel(); draw(); }});
  speedSelect.addEventListener('change', () => {{ speed = Number(speedSelect.value) || 1; }});
  window.addEventListener('resize', () => {{ fit(); draw(); }});
  fit();
  draw();
  // Start immediately so opening the preview visibly plays the chart instead
  // of showing an empty field at 0:00 until the user finds Play. Audio is not
  // included, so autoplay is safe and needs no browser permission.
  playing = true;
  button.textContent = '⏸ Pause';
  lastFrame = 0;
  requestAnimationFrame(frame);
}})();
</script>
"""
