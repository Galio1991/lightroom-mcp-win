"""Preview file handling and picture statistics (Lightroom's SDK has no histogram API, so we compute it)."""
from __future__ import annotations

import time
from pathlib import Path

from PIL import Image

from . import config

# Rule-of-thumb thresholds (heuristics, not measured standards).
DARK_MEAN = 85
BRIGHT_MEAN = 170
CLIP_HIGHLIGHT_PERCENT = 2.0
CRUSHED_SHADOW_PERCENT = 5.0
CAST_PERCENT = 6.0
COMPARISON_KEYS = ("mean_brightness_0_255", "highlight_clipped_percent",
                   "shadow_crushed_percent", "warm_cool_index_percent")


def compare_stats(before: dict, after: dict) -> dict:
    """Numeric after-minus-before changes for two rendered previews."""
    return {key: round(after[key] - before[key], 2) for key in COMPARISON_KEYS}


def analyze(path: Path) -> dict:
    """Return brightness / clipping / colour-balance numbers for a JPEG, plus short Chinese notes."""
    with Image.open(path) as img:
        rgb = img.convert("RGB")
        width, height = rgb.size
        gray = rgb.convert("L")
        hist = gray.histogram()
        total = float(sum(hist)) or 1.0
        mean = sum(i * c for i, c in enumerate(hist)) / total
        highlights = sum(hist[250:]) * 100.0 / total
        shadows = sum(hist[:6]) * 100.0 / total
        bins = [round(sum(hist[i * 16 : (i + 1) * 16]) * 100.0 / total, 1) for i in range(16)]
        r_hist, g_hist, b_hist = rgb.histogram()[:256], rgb.histogram()[256:512], rgb.histogram()[512:768]

        def channel_mean(h):
            return sum(i * c for i, c in enumerate(h)) / total

        r, g, b = channel_mean(r_hist), channel_mean(g_hist), channel_mean(b_hist)

    cast = (r - b) * 100.0 / max(mean, 1.0)
    notes: list[str] = []
    if mean < DARK_MEAN:
        notes.append("整体偏暗")
    elif mean > BRIGHT_MEAN:
        notes.append("整体偏亮")
    else:
        notes.append("整体亮度适中")
    if highlights > CLIP_HIGHLIGHT_PERCENT:
        notes.append(f"高光有约 {highlights:.1f}% 的像素接近纯白，可能过曝")
    if shadows > CRUSHED_SHADOW_PERCENT:
        notes.append(f"暗部有约 {shadows:.1f}% 的像素接近纯黑，细节可能丢失")
    if cast > CAST_PERCENT:
        notes.append("色调偏暖（红多于蓝）")
    elif cast < -CAST_PERCENT:
        notes.append("色调偏冷（蓝多于红）")
    return {
        "width": width,
        "height": height,
        "mean_brightness_0_255": round(mean, 1),
        "highlight_clipped_percent": round(highlights, 2),
        "shadow_crushed_percent": round(shadows, 2),
        "brightness_histogram_16_bins_percent": bins,
        "channel_means": {"r": round(r, 1), "g": round(g, 1), "b": round(b, 1)},
        "warm_cool_index_percent": round(cast, 1),
        "notes_heuristic": notes,
    }


def clean_old(directory: Path = config.PREVIEW_DIR, max_age: float = config.PREVIEW_MAX_AGE_SECONDS) -> int:
    """Delete preview files older than ``max_age`` seconds. Returns how many were removed."""
    removed = 0
    if not directory.is_dir():
        return removed
    limit = time.time() - max_age
    for f in directory.glob("*.jpg"):
        try:
            if f.stat().st_mtime < limit:
                f.unlink()
                removed += 1
        except OSError:
            pass
    return removed
