import asyncio
import json
import os
import time

from PIL import Image

from lightroom_mcp import previews
from lightroom_mcp import config


def make(tmp_path, name, color):
    path = tmp_path / name
    Image.new("RGB", (64, 48), color).save(path, "JPEG", quality=95)
    return path


def test_dark_picture(tmp_path):
    stats = previews.analyze(make(tmp_path, "dark.jpg", (20, 20, 20)))
    assert stats["mean_brightness_0_255"] < 40
    assert "整体偏暗" in stats["notes_heuristic"]
    assert stats["shadow_crushed_percent"] == 0 or stats["shadow_crushed_percent"] > 0  # numeric, present


def test_bright_picture_has_clipped_highlights(tmp_path):
    stats = previews.analyze(make(tmp_path, "bright.jpg", (255, 255, 255)))
    assert stats["highlight_clipped_percent"] > 90
    assert any("过曝" in n for n in stats["notes_heuristic"])
    assert "整体偏亮" in stats["notes_heuristic"]


def test_mid_gray_is_balanced(tmp_path):
    stats = previews.analyze(make(tmp_path, "gray.jpg", (128, 128, 128)))
    assert "整体亮度适中" in stats["notes_heuristic"]
    assert abs(stats["warm_cool_index_percent"]) < 2


def test_warm_and_cool_cast(tmp_path):
    warm = previews.analyze(make(tmp_path, "warm.jpg", (200, 120, 60)))
    cool = previews.analyze(make(tmp_path, "cool.jpg", (60, 120, 200)))
    assert any("偏暖" in n for n in warm["notes_heuristic"])
    assert any("偏冷" in n for n in cool["notes_heuristic"])


def test_histogram_bins_sum_to_about_100(tmp_path):
    stats = previews.analyze(make(tmp_path, "x.jpg", (100, 150, 200)))
    assert len(stats["brightness_histogram_16_bins_percent"]) == 16
    assert 99 <= sum(stats["brightness_histogram_16_bins_percent"]) <= 101
    assert stats["width"] == 64 and stats["height"] == 48


def test_compare_stats_reports_after_minus_before(tmp_path):
    before = previews.analyze(make(tmp_path, "before.jpg", (80, 80, 80)))
    after = previews.analyze(make(tmp_path, "after.jpg", (120, 120, 120)))
    delta = previews.compare_stats(before, after)
    assert delta["mean_brightness_0_255"] == 40.0
    assert delta["highlight_clipped_percent"] == 0.0


def test_review_selected_returns_labeled_images_without_writing(tmp_path, monkeypatch):
    from lightroom_mcp.tools import build_server

    paths = {"AAAAAAAA-1111": make(tmp_path, "first.jpg", (80, 80, 80)),
             "BBBBBBBB-2222": make(tmp_path, "second.jpg", (120, 120, 120))}
    monkeypatch.setattr(config, "PREVIEW_DIR", tmp_path)
    monkeypatch.setattr(previews, "clean_old", lambda: 0)

    class Bridge:
        def call(self, command, params=None, timeout=30):
            if command == "list_selected":
                return {"count": 2, "photos": [{"id": pid, "name": path.name} for pid, path in paths.items()]}
            if command == "get_preview":
                return {"path": str(paths[params["photo"]]), "name": paths[params["photo"]].name}
            raise AssertionError(command)

    result = asyncio.run(build_server(Bridge()).call_tool("lr_review_selected", {"limit": 2}))
    info = json.loads(result.content[0].text)
    assert info["image_order"] == list(paths) and info["shown"] == 2
    assert [part.type for part in result.content] == ["text", "image", "image"]


def test_clean_old_removes_only_old_jpgs(tmp_path):
    old = make(tmp_path, "old.jpg", (1, 1, 1))
    new = make(tmp_path, "new.jpg", (2, 2, 2))
    keep = tmp_path / "notes.txt"
    keep.write_text("x")
    long_ago = time.time() - 3 * 24 * 3600
    os.utime(old, (long_ago, long_ago))
    assert previews.clean_old(tmp_path, max_age=24 * 3600) == 1
    assert not old.exists() and new.exists() and keep.exists()
