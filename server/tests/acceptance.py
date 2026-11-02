"""v1.0 acceptance check against a real Lightroom (test catalog, one photo selected).

Checks: the tools return correct content; calling them changes no develop setting (before/after comparison);
a restarted Python server is found again by the plug-in without restarting Lightroom.
Stop any other server on port 58770 first. Usage: python acceptance.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lightroom_mcp import config, previews  # noqa: E402
from lightroom_mcp.bridge import Bridge, BridgeBusy  # noqa: E402

failures = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global failures
    failures += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")


def main() -> int:
    bridge = Bridge()
    try:
        bridge.start()
    except BridgeBusy as e:
        print(e)
        return 2
    try:
        check("plug-in connects", bridge.wait_connected(40), f"version {bridge.plugin_version}")

        status = bridge.call("status")
        check("status: Lightroom 15.x", status["lightroom_version"].startswith("15."), status["lightroom_version"])
        check("status: test catalog", "ceshi" in status["catalog_path"], status["catalog_path"])

        listing = bridge.call("list_selected")
        check("list_selected: one photo selected", listing["count"] == 1, f"count={listing['count']}")
        photo = listing["photos"][0]
        check("list_selected: has id/name/format/size", all(photo.get(k) for k in ("id", "name", "format", "width", "height")),
              f"{photo['name']} {photo['format']} {photo['width']}x{photo['height']}")
        check("list_selected: exposure data is optional when source EXIF is absent",
              all(photo.get(k) is None or isinstance(photo[k], (int, float, str)) and photo[k] != ""
                  for k in ("iso", "aperture", "shutter_speed")),
              f"{photo.get('iso')} {photo.get('aperture')} {photo.get('shutter_speed')}")

        before = bridge.call("get_develop", {"photo": "selected"})
        check("get_develop: core settings present", all(k in before["settings"] for k in ("Exposure2012", "Contrast2012", "Highlights2012", "Shadows2012")))
        check("get_develop: HSL has 8 colours", len(before["hsl"]) == 8)
        check("get_develop by id matches 'selected'", bridge.call("get_develop", {"photo": photo["id"]})["settings"] == before["settings"])

        t0 = time.time()
        preview = bridge.call("get_preview", {"photo": photo["id"], "long_edge": 1024}, timeout=60)
        seconds = time.time() - t0
        path = Path(preview["path"])
        check("get_preview: file inside the preview folder", config.PREVIEW_DIR.resolve() in path.resolve().parents and path.is_file(), f"{seconds:.1f}s, {preview['bytes']} bytes")
        stats = previews.analyze(path)
        check("get_preview: 1024px long edge", max(stats["width"], stats["height"]) == 1024, f"{stats['width']}x{stats['height']}")
        check("get_preview: has analysis notes", bool(stats["notes_heuristic"]), "; ".join(stats["notes_heuristic"]))

        small = bridge.call("get_preview", {"photo": "selected", "long_edge": 99999}, timeout=60)  # must be clamped to 2048
        check("long_edge is clamped to 2048", max(previews.analyze(Path(small["path"]))["width"], previews.analyze(Path(small["path"]))["height"]) <= 2048)

        after = bridge.call("get_develop", {"photo": "selected"})
        check("NO develop setting changed by any call", after["settings"] == before["settings"] and after["hsl"] == before["hsl"])

        # restart resilience: stop this server, start a new one, the plug-in must find it without restarting Lightroom
        bridge.stop()
        time.sleep(2)
        bridge = Bridge()
        bridge.start()
        t1 = time.time()
        reconnected = bridge.wait_connected(40)
        check("restarted server is found again by the plug-in", reconnected, f"{time.time() - t1:.1f}s")
        if reconnected:
            check("call works after the restart", bridge.call("status")["plugin_version"] == status["plugin_version"])
    finally:
        bridge.stop()
    print(f"\n{'ALL PASSED' if failures == 0 else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
