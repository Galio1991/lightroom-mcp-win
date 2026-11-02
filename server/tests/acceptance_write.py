"""v1.1 acceptance check for the WRITE tools against a real Lightroom.

SAFETY: runs only when the open catalog's path contains "ceshi" (the test catalog) and exactly one photo is selected.
Every change is undone at the end (restore_original) and the script then compares ALL values that get_develop reports
with the state it started from, and the preview file size (pixel identity) with the baseline.
It uses its own temporary edit folder, so your real ~/LrMcp/edits records are not touched.

Stop any other lrmcp server on port 58770 first. Usage: python acceptance_write.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lightroom_mcp import config, edits  # noqa: E402
from lightroom_mcp.bridge import Bridge, BridgeBusy, BridgeError  # noqa: E402
from lightroom_mcp.edits import EditError, EditService, EditStore, ReadOnlyError  # noqa: E402

failures = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global failures
    failures += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")


def preview_size(bridge: Bridge, photo_id: str) -> int:
    data = bridge.call("get_preview", {"photo": photo_id, "long_edge": 1024}, timeout=60)
    return Path(data["path"]).stat().st_size


def everything(develop: dict) -> dict:
    """All values get_develop reports, flattened, to compare before/after."""
    flat = dict(develop["settings"])
    for color, row in develop["hsl"].items():
        for field, value in row.items():
            flat[f"hsl.{color}.{field}"] = value
    return flat


def main() -> int:
    bridge = Bridge()
    try:
        bridge.start()
    except BridgeBusy as e:
        print(e)
        return 2
    store_dir = Path(tempfile.mkdtemp(prefix="lrmcp-acceptance-"))
    service = EditService(bridge.call, EditStore(store_dir), readonly_file=store_dir / "readonly")
    readonly_flag = config.DATA_DIR / "readonly"  # the file the PLUG-IN checks
    readonly_flag_created = False
    photo_id = None
    try:
        if not bridge.wait_connected(40):
            print("plug-in did not connect")
            return 2
        status = bridge.call("status")
        if "ceshi" not in status["catalog_path"]:
            print(f"REFUSED: the open catalog is not the test catalog: {status['catalog_path']}")
            return 2
        listing = bridge.call("list_selected")
        if listing["count"] != 1:
            print(f"REFUSED: select exactly one photo (count={listing['count']})")
            return 2
        if readonly_flag.exists():
            print(f"REFUSED: {readonly_flag} exists; remove it first")
            return 2
        photo = listing["photos"][0]
        photo_id = photo["id"]
        print(f"photo: {photo['name']} ({photo['format']})\n")

        baseline_develop = bridge.call("get_develop", {"photo": photo_id})
        baseline = everything(baseline_develop)
        baseline_size = preview_size(bridge, photo_id)
        plain_keys = [k for k in edits.LIMITS if k not in edits.WB_KEYS]
        base = edits.current_values(baseline_develop, plain_keys)
        has_kelvin = isinstance(baseline_develop["settings"].get("Temperature"), (int, float))
        check(f"baseline read: all {len(plain_keys)} non-white-balance sliders found", len(base) == len(plain_keys))

        # 1. a normal change
        e1 = service.set_develop({"Exposure2012": base["Exposure2012"] + 0.3, "Vibrance": base["Vibrance"] + 10}, reason="acceptance")
        check("set_develop: verified by read-back", e1["verified"] and all(r["ok"] for r in e1["changes"]))
        after1 = edits.current_values(bridge.call("get_develop", {"photo": photo_id}), ["Exposure2012", "Vibrance"])
        check("set_develop: Lightroom really holds the new values",
              abs(after1["Exposure2012"] - (base["Exposure2012"] + 0.3)) < 1e-3 and abs(after1["Vibrance"] - (base["Vibrance"] + 10)) < 1e-3)
        check("preview changed after the edit", preview_size(bridge, photo_id) != baseline_size)

        # 2. protections (the plug-in must refuse by itself, so call it directly too)
        try:
            service.set_develop({"Exposure2012": base["Exposure2012"] + 0.3 + 1.5})
            check("step limit refuses a +1.5 EV jump", False, "no error")
        except BridgeError as e:
            check("step limit refuses a +1.5 EV jump", "单次最多改" in str(e), str(e)[:60])
        try:
            service.set_develop({"Exposure2012": 9})
            check("range refused by the server", False)
        except EditError as e:
            check("range refused by the server", "取值范围" in str(e))
        for label, params, needle in (
            ("range refused by the plug-in itself", {"Exposure2012": "9"}, "取值范围"),
            ("white balance beyond its range refused by the plug-in itself", {"Temperature": "99999"}, "取值范围"),
            ("unknown key refused by the plug-in itself", {"LensManualDistortionAmount": "5"}, "不允许修改"),
        ):
            try:
                bridge.call("set_develop", {"photo": photo_id, "edit": "acceptance", **params})
                check(label, False, "no error")
            except BridgeError as e:
                check(label, needle in str(e), str(e)[:60])
        still = edits.current_values(bridge.call("get_develop", {"photo": photo_id}), ["Exposure2012"])
        check("refused changes changed nothing", abs(still["Exposure2012"] - (base["Exposure2012"] + 0.3)) < 1e-3)

        # 3. HSL
        e2 = service.set_develop({"HueAdjustmentRed": base["HueAdjustmentRed"] + 10, "SaturationAdjustmentBlue": base["SaturationAdjustmentBlue"] - 15,
                                  "LuminanceAdjustmentGreen": base["LuminanceAdjustmentGreen"] + 5}, reason="acceptance hsl")
        check("HSL change verified", e2["verified"])

        # 3b. white balance: RAW photos can be changed (label becomes Custom); JPEG must be refused with a clear message
        if has_kelvin:
            t0, tint0, label0 = (baseline_develop["settings"][k] for k in ("Temperature", "Tint", "WhiteBalance"))
            size_before = preview_size(bridge, photo_id)
            ew = service.set_develop({"Temperature": t0 + 500, "Tint": tint0 + 5}, reason="acceptance white balance")
            now_wb = bridge.call("get_develop", {"photo": photo_id})["settings"]
            check("white balance change verified, label is Custom", ew["verified"] and now_wb["WhiteBalance"] == "Custom"
                  and abs(now_wb["Temperature"] - (t0 + 500)) < 1e-3, f"label before: {label0}")
            check("preview changed after the white balance edit", preview_size(bridge, photo_id) != size_before)
            try:
                service.set_develop({"Temperature": t0 + 500 + 2500})
                check("white balance step limit (+2500 K) refused", False, "no error")
            except BridgeError as e:
                check("white balance step limit (+2500 K) refused", "单次最多改" in str(e))
            rw = service.restore(ew["edit_id"])
            back = bridge.call("get_develop", {"photo": photo_id})["settings"]
            check("restore brings back the white balance LABEL and the numbers",
                  rw["verified"] and back["WhiteBalance"] == label0 and abs(back["Temperature"] - t0) < 1e-3 and abs(back["Tint"] - tint0) < 1e-3)
        else:
            try:
                service.set_develop({"Temperature": 6500})
                check("white balance on a JPEG is refused with a clear message", False, "no error")
            except EditError as e:
                check("white balance on a JPEG is refused with a clear message", "JPEG" in str(e))

        # 3c. batch 1: colour grading + calibration + parametric tone curve + a point curve, in one change
        curve0 = baseline_develop["settings"].get("ToneCurvePV2012")
        size_before = preview_size(bridge, photo_id)
        eb = service.set_develop({
            "SplitToningShadowHue": 220, "SplitToningShadowSaturation": base["SplitToningShadowSaturation"] + 25,
            "SplitToningHighlightHue": 40, "SplitToningHighlightSaturation": base["SplitToningHighlightSaturation"] + 20,
            "ColorGradeBlending": base["ColorGradeBlending"] + 10, "RedHue": base["RedHue"] + 10, "BlueSaturation": base["BlueSaturation"] - 15,
            "ParametricHighlights": base["ParametricHighlights"] - 20,
            "ToneCurvePV2012": [[0, 0], [64, 50], [192, 205], [255, 255]],
        }, reason="acceptance batch 1")
        check("batch 1 change verified (colour grading, calibration, parametric, point curve)", eb["verified"] and all(r["ok"] for r in eb["changes"]),
              str([r["parameter"] for r in eb["changes"] if not r["ok"]]))
        check("preview changed after the batch 1 edit", preview_size(bridge, photo_id) != size_before)
        try:
            service.set_develop({"ToneCurvePV2012": [[0, 0], [100, 230], [255, 255]]})
            check("an extreme point curve is refused", False, "no error")
        except EditError as e:
            check("an extreme point curve is refused", "最多" in str(e))
        try:
            bridge.call("set_develop", {"photo": photo_id, "edit": "acceptance", "ToneCurvePV2012": "0,0,100,230,255,255"})
            check("an extreme point curve is refused by the plug-in itself", False, "no error")
        except BridgeError as e:
            check("an extreme point curve is refused by the plug-in itself", "最多" in str(e), str(e)[:60])
        rb = service.restore(eb["edit_id"])
        backed = bridge.call("get_develop", {"photo": photo_id})["settings"]
        check("restore brings the point curve and sliders back", rb["verified"] and backed.get("ToneCurvePV2012") == curve0
              and abs(backed["ColorGradeBlending"] - base["ColorGradeBlending"]) < 1e-3)

        # 3d. batch 2: sharpening, noise reduction, grain, vignettes, defringe
        size_before = preview_size(bridge, photo_id)
        wanted2 = {"Sharpness": base["Sharpness"] + 30, "SharpenRadius": base["SharpenRadius"] + 0.5,
                   "LuminanceSmoothing": base["LuminanceSmoothing"] + 20, "ColorNoiseReduction": base["ColorNoiseReduction"] + 15,
                   "GrainAmount": base["GrainAmount"] + 25, "GrainSize": base["GrainSize"] + 10,
                   "PostCropVignetteAmount": base["PostCropVignetteAmount"] - 25, "VignetteAmount": base["VignetteAmount"] + 20,
                   "DefringePurpleAmount": base["DefringePurpleAmount"] + 5}
        ec = service.set_develop(wanted2, reason="acceptance batch 2")
        check("batch 2 change verified (sharpening, noise, grain, vignettes, defringe)", ec["verified"] and all(r["ok"] for r in ec["changes"]),
              str([r["parameter"] for r in ec["changes"] if not r["ok"]]))
        check("preview changed after the batch 2 edit", preview_size(bridge, photo_id) != size_before)
        try:
            service.set_develop({"Sharpness": base["Sharpness"] + 30 + 100})
            check("a big sharpening jump is refused", False, "no error")
        except (EditError, BridgeError) as e:
            check("a big sharpening jump is refused", "最多" in str(e) or "取值范围" in str(e))
        rc = service.restore(ec["edit_id"])
        now2 = edits.current_values(bridge.call("get_develop", {"photo": photo_id}), list(wanted2))
        check("restore brings batch 2 back", rc["verified"] and all(abs(now2[k] - base[k]) < 1e-3 for k in wanted2))

        # 3e. batch 3: crop and straighten
        size_before = preview_size(bridge, photo_id)
        ek = service.set_develop({"CropLeft": base["CropLeft"] + 0.1, "CropRight": base["CropRight"] - 0.1, "CropTop": base["CropTop"] + 0.1,
                                  "CropBottom": base["CropBottom"] - 0.1, "CropAngle": base["CropAngle"] + 3}, reason="acceptance batch 3")
        check("batch 3 change verified (crop + straighten)", ek["verified"] and all(r["ok"] for r in ek["changes"]),
              str([r["parameter"] for r in ek["changes"] if not r["ok"]]))
        listed = bridge.call("list_selected")["photos"][0]
        check("Lightroom reports the photo as cropped", listed["is_cropped"] is True)
        check("preview changed after the crop", preview_size(bridge, photo_id) != size_before)
        for label, change, needle in (("a crop edge outside the image is refused", {"CropLeft": -0.1}, "取值范围"),
                                      ("a straighten jump of 30 degrees is refused", {"CropAngle": base["CropAngle"] + 33}, "最多")):
            try:
                service.set_develop(change)
                check(label, False, "no error")
            except (EditError, BridgeError) as e:
                check(label, needle in str(e), str(e)[:60])
        try:
            bridge.call("set_develop", {"photo": photo_id, "edit": "acceptance", "CropLeft": "0.85", "CropRight": "0.9"})
            check("a crop that keeps under 10% of the width is refused by the plug-in itself", False, "no error")
        except BridgeError as e:
            check("a crop that keeps under 10% of the width is refused by the plug-in itself", "至少保留" in str(e) or "单次最多" in str(e), str(e)[:60])
        rk = service.restore(ek["edit_id"])
        listed = bridge.call("list_selected")["photos"][0]
        check("restore brings the crop back (photo is not cropped any more)", rk["verified"] and listed["is_cropped"] is False)

        # 4. restore one edit: only that edit's sliders go back
        r1 = service.restore(e1["edit_id"])
        now = edits.current_values(bridge.call("get_develop", {"photo": photo_id}), plain_keys)
        check("restore(edit 1) puts Exposure and Vibrance back", r1["verified"] and abs(now["Exposure2012"] - base["Exposure2012"]) < 1e-3
              and abs(now["Vibrance"] - base["Vibrance"]) < 1e-3)
        check("restore(edit 1) leaves the HSL edit alone", abs(now["HueAdjustmentRed"] - (base["HueAdjustmentRed"] + 10)) < 1e-3)
        check("restoring the same edit again does nothing", "已经" in service.restore(e1["edit_id"])["note"])

        # 5. read-only switch: the plug-in checks its own flag file even if the server is bypassed
        readonly_flag.write_text("1")
        readonly_flag_created = True
        try:
            try:
                service2 = EditService(bridge.call, EditStore(store_dir), readonly_file=readonly_flag)
                service2.set_develop({"Exposure2012": base["Exposure2012"] + 0.1})
                check("read-only: server refuses", False)
            except ReadOnlyError:
                check("read-only: server refuses", True)
            try:
                bridge.call("set_develop", {"photo": photo_id, "edit": "acceptance", "Exposure2012": "0.1"})
                check("read-only: plug-in refuses by itself", False, "no error")
            except BridgeError as e:
                check("read-only: plug-in refuses by itself", "只读模式" in str(e))
        finally:
            readonly_flag.unlink(missing_ok=True)
            readonly_flag_created = False

        # 6. history of edits
        listed = service.list_edits(photo=photo_id)
        check("list_edits shows 3 records (2 edits + 1 restore)", listed["count"] >= 3, f"count={listed['count']}")

        # 7. back to the very start
        original = service.restore_original(photo=photo_id)
        check("restore_original verified", original["verified"], f"undid {len(original['edits_undone'])} edit(s)")
    finally:
        if readonly_flag_created:
            readonly_flag.unlink(missing_ok=True)
        if photo_id:
            try:  # whatever happened above, put the test photo back
                service.restore_original(photo=photo_id)
            except Exception as e:  # noqa: BLE001
                print("cleanup:", e)

    final_develop = bridge.call("get_develop", {"photo": photo_id})
    final = everything(final_develop)
    diffs = {k: (baseline.get(k), final.get(k)) for k in set(baseline) | set(final)
             if not (isinstance(baseline.get(k), (int, float)) and isinstance(final.get(k), (int, float)) and abs(baseline[k] - final[k]) < 1e-6)
             and baseline.get(k) != final.get(k)}
    check(f"EVERY value get_develop reports is back to the start ({len(baseline)} values)", not diffs, str(diffs)[:200])
    final_size = preview_size(bridge, photo_id)
    check("preview file size equals the baseline (pixel-identical)", final_size == baseline_size, f"{final_size} vs {baseline_size}")
    bridge.stop()
    print(f"\n{'ALL PASSED' if failures == 0 else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
