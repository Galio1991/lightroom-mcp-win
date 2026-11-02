import asyncio
import copy
import json
import re
import urllib.parse
from pathlib import Path

import pytest

from lightroom_mcp import edits
from lightroom_mcp.bridge import BridgeError, BridgeTimeout
from lightroom_mcp.edits import EditError, EditService, EditStore, ReadOnlyError, validate_changes

LIMITS_LUA = Path(__file__).resolve().parents[2] / "plugin" / "lrmcp.lrdevplugin" / "Limits.lua"


# ---- a tiny stand-in for the plug-in: same contract and same checks as Commands.lua -----------------------------
class FakeLightroom:
    def __init__(self):
        self.photos = {
            "AAAAAAAA-1111": {"name": "one.ARW", "wb": "As Shot", "values": {**{k: 0.0 for k in edits.LIMITS}, **{c: [0.0, 0.0, 255.0, 255.0] for c in edits.CURVE_KEYS}, "Look": {"Name": "Adobe Color", "Amount": 1.0}}},
            "BBBBBBBB-2222": {"name": "two.JPG", "wb": "As Shot", "values": {**{k: 0.0 for k in edits.LIMITS}, **{c: [0.0, 0.0, 255.0, 255.0] for c in edits.CURVE_KEYS}, "Look": {"Name": "Adobe Color", "Amount": 1.0}}},
        }
        for p in self.photos.values():
            p["meta"] = {"rating": 0, "flag": "none", "color_label": "none", "title": "", "caption": "", "keywords": []}
        self.active = "AAAAAAAA-1111"
        self.selected = None   # list of ids; None = only the active photo
        self.write_calls = 0
        self.fail_next_write = False
        self.seen_edit_file_before_write = None
        self.store = None

    def _photo(self, ref):
        pid = self.active if ref == "selected" else ref
        if pid not in self.photos:
            raise BridgeError("在当前目录里找不到这张照片")
        return pid, self.photos[pid]

    def __call__(self, command, params=None, timeout=30.0):
        params = params or {}
        if command == "list_selected":
            ids = self.selected if self.selected is not None else [self.active]
            return {"active_id": self.active, "count": len(ids), "photos": [{"id": i, "name": self.photos[i]["name"]} for i in ids[: params.get("limit", 20)]]}
        if command == "get_develop":
            pid, p = self._photo(params["photo"])
            v = p["values"]
            hsl = {c: {"hue": v["HueAdjustment" + c], "saturation": v["SaturationAdjustment" + c],
                       "luminance": v["LuminanceAdjustment" + c]} for c in edits.COLORS}
            settings = {k: x for k, x in v.items() if not k.startswith(edits.HSL_PREFIXES)}
            settings["WhiteBalance"] = p["wb"]
            return {"photo": {"id": pid, "name": p["name"]}, "settings": settings, "hsl": hsl}
        if command == "set_develop":
            return self._set(params)
        if command == "get_metadata":
            pid, p = self._photo(params["photo"])
            return {"photo": {"id": pid, "name": p["name"]}, "metadata": copy.deepcopy(p["meta"])}
        if command == "set_metadata":
            return self._set_metadata(params)
        if command == "list_presets":
            return {"total": 2, "matched": 2, "shown": 2, "presets": [
                {"id": "PRESET-AAAA-0001", "name": "Warm look", "folder": "Test", "has_masks": False},
                {"id": "PRESET-BBBB-0002", "name": "Sky mask", "folder": "Test", "has_masks": True}]}
        if command == "snapshot":
            pid, p = self._photo(params["photo"])
            return {"photo": {"id": pid, "name": p["name"]}, "settings": copy.deepcopy(p["values"]), "has_local_adjustments": False}
        if command == "apply_preset":
            return self._apply_preset(params)
        if command == "restore_settings":
            return self._restore_settings(params)
        raise AssertionError(f"unexpected command {command}")

    def _set_metadata(self, params):
        self.write_calls += 1
        if self.fail_next_write:
            self.fail_next_write = False
            raise BridgeError("Lightroom 现在不能写入")
        pid, p = self._photo(params["photo"])
        m = p["meta"]
        before = copy.deepcopy(m)
        dec = urllib.parse.unquote
        if "rating" in params:
            m["rating"] = int(params["rating"])
        if "flag" in params:
            m["flag"] = params["flag"]
        if "color" in params:
            m["color_label"] = params["color"]
        for key in ("title", "caption"):
            if key in params:
                m[key] = dec(params[key])
        for name in (dec(x) for x in params.get("add_kw", "").split(",") if x):
            if name not in m["keywords"]:
                m["keywords"].append(name)
        for name in (dec(x) for x in params.get("remove_kw", "").split(",") if x):
            if name in m["keywords"]:
                m["keywords"].remove(name)
        return {"before": before, "after": copy.deepcopy(m), "verified": True}

    def _apply_preset(self, params):
        self.write_calls += 1
        if params["preset"] == "PRESET-BBBB-0002":
            raise BridgeError("这个预设带有蒙版/局部调整（自适应预设），暂不支持套用")
        pid, p = self._photo(params["photo"])
        v = p["values"]
        before = copy.deepcopy(v)
        v["Contrast2012"] = 40.0
        v["Look"] = {"Name": "Vivid", "Amount": 1.0}
        v["ToneCurvePV2012"] = [0.0, 0.0, 128.0, 140.0, 255.0, 255.0]
        v["GrainSeed"] = 123.0           # a key that did not exist before: cannot be restored, cannot be deleted
        p["wb"] = "Custom"
        keys = sorted(k for k in set(before) | set(v) if before.get(k) != v.get(k))
        scalars = {k: {"before": before.get(k), "after": v[k]} for k in keys if not isinstance(v[k], (dict, list)) and not isinstance(before.get(k), (dict, list))}
        return {"preset": "Warm look", "changed_keys": keys, "changes": scalars,
                "tables_changed": [k for k in keys if k not in scalars], "new_keys": [k for k in keys if k not in before],
                "before": {k: before[k] for k in keys if k in before}, "photo": {"id": pid}}

    def _restore_settings(self, params):
        self.write_calls += 1
        pid, p = self._photo(params["photo"])
        values = json.loads(urllib.parse.unquote(params["blob"]))
        for k, x in values.items():
            if k not in p["values"]:
                raise BridgeError(f"照片当前没有这个设置，没法写回：{k}")
            if type(p["values"][k]) is not type(x):
                raise BridgeError(f"类型对不上，已取消还原：{k}")
        before = {k: p["values"][k] for k in values if not isinstance(p["values"][k], (dict, list))}
        p["values"].update(values)
        return {"before": before, "after": {k: x for k, x in values.items() if not isinstance(x, (dict, list))},
                "verified": True, "restored": len(values)}

    def _set(self, params):
        self.write_calls += 1
        if self.store is not None:
            self.seen_edit_file_before_write = any(self.store.directory.glob("*.json"))
        if self.fail_next_write:
            self.fail_next_write = False
            raise BridgeError("Lightroom 现在不能写入")
        restoring = params.get("restore") == "1"
        pid, p = self._photo(params["photo"])
        changes = {}
        for k, text in params.items():
            if k in ("photo", "edit", "restore", "wb"):
                continue
            if k in edits.CURVE_KEYS:
                nums = [float(n) for n in text.split(",")]
                if not restoring:
                    edits.validate_curve(k, nums)
                changes[k] = nums
                continue
            if k not in edits.LIMITS:
                raise BridgeError(f"不允许修改这个参数：{k}")
            low, high, step = edits.LIMITS[k]
            x = float(text)
            if not low <= x <= high:
                raise BridgeError(f"{k} 的取值范围是 {low:g} 到 {high:g}")
            if not restoring and abs(x - p["values"][k]) > step + 1e-9:
                raise BridgeError(f"{k} 单次最多改 ±{step:g}")
            changes[k] = x
        before = {k: p["values"][k] for k in changes}
        p["values"].update(changes)
        reply = {"before": before, "after": dict(changes), "verified": True}
        if any(k in edits.WB_KEYS for k in changes):
            reply["before_wb"] = p["wb"]
            p["wb"] = params["wb"].replace("_", " ") if restoring and "wb" in params else "Custom"
        return reply


@pytest.fixture
def env(tmp_path):
    lr = FakeLightroom()
    store = EditStore(tmp_path / "edits")
    lr.store = store
    service = EditService(lr, store, readonly_file=tmp_path / "readonly")
    return lr, store, service, tmp_path


# ---- validation ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [None, {}, [], "Exposure2012"])
def test_changes_must_be_a_non_empty_dict(bad):
    with pytest.raises(EditError):
        validate_changes(bad)


def test_unknown_and_forbidden_keys_are_refused_with_the_allowed_list():
    for key in ("PerspectiveVertical", "ToneCurve", "LensManualDistortionAmount", "WhiteBalance", "exposure", "../x"):
        with pytest.raises(EditError) as err:
            validate_changes({key: 1})
        assert "不允许修改" in str(err.value) and "Exposure2012" in str(err.value)


@pytest.mark.parametrize("value", [True, "1", None, float("nan"), float("inf"), [1]])
def test_values_must_be_finite_numbers(value):
    with pytest.raises(EditError):
        validate_changes({"Exposure2012": value})


def test_range_is_enforced_at_both_ends():
    assert validate_changes({"Exposure2012": 5, "Contrast2012": -100}) == {"Exposure2012": 5.0, "Contrast2012": -100.0}
    for bad in ({"Exposure2012": 5.01}, {"Exposure2012": -6}, {"Contrast2012": 100.5}, {"HueAdjustmentRed": -101}):
        with pytest.raises(EditError, match="取值范围"):
            validate_changes(bad)


def test_too_many_keys():
    many = {k: 0 for k in list(edits.LIMITS)[:31]}
    assert len(many) == 31
    with pytest.raises(EditError, match="最多改 30"):
        validate_changes(many)


def test_python_and_lua_whitelists_are_identical():
    """Limits.lua is what the plug-in enforces; edits.py is what the server enforces. They must agree exactly."""
    pattern = re.compile(r"^\s*(\w+)\s*=\s*\{\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*([\d.]+)\s*\}\s*,?\s*$")
    lua = {}
    for line in LIMITS_LUA.read_text(encoding="utf-8").splitlines():
        m = pattern.match(line)
        if m:
            lua[m.group(1)] = (float(m.group(2)), float(m.group(3)), float(m.group(4)))
    assert lua == edits.LIMITS
    assert len(lua) == 89


# ---- set_develop -----------------------------------------------------------------------------------------------
def test_set_develop_changes_the_photo_and_reports_before_and_after(env):
    lr, store, service, _ = env
    out = service.set_develop({"Exposure2012": 0.3, "Highlights2012": -20}, reason="暗部太暗")
    assert out["verified"] is True and out["photo"] == "one.ARW"
    rows = {r["parameter"]: r for r in out["changes"]}
    assert rows["Exposure2012"]["before"] == 0 and rows["Exposure2012"]["after"] == 0.3 and rows["Exposure2012"]["ok"]
    assert lr.photos["AAAAAAAA-1111"]["values"]["Highlights2012"] == -20
    saved = store.load(out["edit_id"])
    assert saved["status"] == "applied" and saved["reason"] == "暗部太暗" and saved["before"]["Highlights2012"] == 0


def test_old_values_are_on_disk_before_anything_is_written(env):
    lr, store, service, _ = env
    service.set_develop({"Exposure2012": 0.5})
    assert lr.seen_edit_file_before_write is True


def test_a_failed_write_is_recorded_and_changes_nothing(env):
    lr, store, service, _ = env
    lr.fail_next_write = True
    with pytest.raises(BridgeError):
        service.set_develop({"Exposure2012": 0.5})
    (record,) = store.all()
    assert record["status"] == "failed" and record["before"] == {"Exposure2012": 0.0}
    assert lr.photos["AAAAAAAA-1111"]["values"]["Exposure2012"] == 0


def test_step_limit_is_refused_and_the_photo_is_untouched(env):
    lr, store, service, _ = env
    with pytest.raises(BridgeError, match="单次最多改"):
        service.set_develop({"Exposure2012": 2.0})
    assert lr.photos["AAAAAAAA-1111"]["values"]["Exposure2012"] == 0
    assert store.all()[0]["status"] == "failed"


def test_selected_photo_is_resolved_once(env):
    lr, store, service, _ = env
    out = service.set_develop({"Exposure2012": 0.2}, photo="selected")
    lr.active = "BBBBBBBB-2222"  # the user clicks another photo afterwards
    service.restore(out["edit_id"])  # must still restore the first photo
    assert lr.photos["AAAAAAAA-1111"]["values"]["Exposure2012"] == 0
    assert lr.photos["BBBBBBBB-2222"]["values"]["Exposure2012"] == 0


# ---- restore ---------------------------------------------------------------------------------------------------
def test_restore_writes_the_old_values_back_exactly(env):
    lr, store, service, _ = env
    first = service.set_develop({"Exposure2012": 0.7, "Saturation": 20})
    out = service.restore(first["edit_id"])
    v = lr.photos["AAAAAAAA-1111"]["values"]
    assert v["Exposure2012"] == 0 and v["Saturation"] == 0 and out["verified"]
    assert store.load(first["edit_id"])["restored_by"] == out["edit_id"]
    assert store.load(out["edit_id"])["kind"] == "restore"


def test_restoring_twice_does_nothing_the_second_time(env):
    lr, store, service, _ = env
    first = service.set_develop({"Exposure2012": 0.7})
    service.restore(first["edit_id"])
    calls = lr.write_calls
    again = service.restore(first["edit_id"])
    assert "已经" in again["note"] and lr.write_calls == calls


def test_a_restore_record_cannot_be_restored_and_a_failed_edit_needs_none(env):
    lr, store, service, _ = env
    first = service.set_develop({"Exposure2012": 0.7})
    restore_id = service.restore(first["edit_id"])["edit_id"]
    with pytest.raises(EditError, match="本身是一次还原"):
        service.restore(restore_id)
    lr.fail_next_write = True
    with pytest.raises(BridgeError):
        service.set_develop({"Exposure2012": 0.3})
    failed = [r for r in store.all() if r["status"] == "failed"][0]
    with pytest.raises(EditError, match="不需要还原"):
        service.restore(failed["edit_id"])


def test_restore_original_goes_back_to_the_very_first_values(env):
    lr, store, service, _ = env
    service.set_develop({"Exposure2012": 0.8})
    service.set_develop({"Exposure2012": 1.5, "Contrast2012": 25})  # step from 0.8 is 0.7: allowed
    service.set_develop({"Exposure2012": 1.0})
    assert lr.photos["AAAAAAAA-1111"]["values"]["Exposure2012"] == 1.0
    dry = service.restore_original(dry_run=True)
    assert dry["dry_run"] is True and dry["will_write"] == {"Exposure2012": 0.0, "Contrast2012": 0.0}
    assert lr.photos["AAAAAAAA-1111"]["values"]["Exposure2012"] == 1.0  # the dry run wrote nothing
    out = service.restore_original()
    v = lr.photos["AAAAAAAA-1111"]["values"]
    assert v["Exposure2012"] == 0 and v["Contrast2012"] == 0 and out["verified"]
    assert len(out["edits_undone"]) == 3
    assert "无需还原" in service.restore_original()["note"]


def test_restore_original_uses_first_value_even_after_earlier_edit_was_restored(env):
    lr, _, service, _ = env
    first = service.set_develop({"Exposure2012": 0.8})
    service.set_develop({"Exposure2012": 1.5})
    service.restore(first["edit_id"])
    assert service.restore_original(dry_run=True)["will_write"]["Exposure2012"] == 0.0
    service.restore_original()
    assert lr.photos[A]["values"]["Exposure2012"] == 0.0


def test_timed_out_write_keeps_a_restorable_backup(env):
    lr, store, service, _ = env
    original = lr._set

    def applied_then_timed_out(params):
        original(params)
        raise BridgeTimeout("reply timed out")

    lr._set = applied_then_timed_out
    with pytest.raises(BridgeTimeout):
        service.set_develop({"Exposure2012": 0.5})
    record = store.all()[0]
    assert record["status"] == "uncertain" and record["before"]["Exposure2012"] == 0.0
    lr._set = original
    service.restore(record["edit_id"])
    assert lr.photos[A]["values"]["Exposure2012"] == 0.0


def test_restore_original_only_touches_the_chosen_photo(env):
    lr, store, service, _ = env
    service.set_develop({"Exposure2012": 0.5}, photo="AAAAAAAA-1111")
    service.set_develop({"Exposure2012": 0.4}, photo="BBBBBBBB-2222")
    service.restore_original(photo="AAAAAAAA-1111")
    assert lr.photos["AAAAAAAA-1111"]["values"]["Exposure2012"] == 0
    assert lr.photos["BBBBBBBB-2222"]["values"]["Exposure2012"] == 0.4


# ---- read-only switch ------------------------------------------------------------------------------------------
def test_read_only_file_refuses_every_write_without_calling_the_plugin(env):
    lr, store, service, tmp = env
    first = service.set_develop({"Exposure2012": 0.5})
    (tmp / "readonly").write_text("1")
    calls = lr.write_calls
    with pytest.raises(ReadOnlyError, match="只读模式"):
        service.set_develop({"Exposure2012": 0.6})
    with pytest.raises(ReadOnlyError):
        service.restore(first["edit_id"])
    with pytest.raises(ReadOnlyError):
        service.restore_original()
    assert lr.write_calls == calls
    assert service.restore_original(dry_run=True)["dry_run"]  # looking is still allowed
    assert service.list_edits()["count"] == 1


def test_read_only_environment_variable(env, monkeypatch):
    lr, store, service, _ = env
    monkeypatch.setenv("LRMCP_READONLY", "1")
    with pytest.raises(ReadOnlyError):
        service.set_develop({"Exposure2012": 0.1})
    assert lr.write_calls == 0


# ---- records ---------------------------------------------------------------------------------------------------
def test_list_edits_newest_first_and_filtered(env):
    lr, store, service, _ = env
    a = service.set_develop({"Exposure2012": 0.1}, photo="AAAAAAAA-1111")
    b = service.set_develop({"Exposure2012": 0.2}, photo="BBBBBBBB-2222")
    everything = service.list_edits()
    assert [e["edit_id"] for e in everything["edits"]] == [b["edit_id"], a["edit_id"]]
    only_a = service.list_edits(photo="AAAAAAAA-1111")
    assert [e["edit_id"] for e in only_a["edits"]] == [a["edit_id"]]
    assert service.list_edits(photo="selected")["count"] == 1


def test_edit_ids_cannot_escape_the_edit_folder(env):
    _, store, service, _ = env
    for bad in ("../../secret", "..\\x", "20261102-120000-zzzz", "", "a/b"):
        with pytest.raises(EditError):
            store.load(bad)


# ---- the MCP tools ---------------------------------------------------------------------------------------------
def test_tools_are_registered_with_the_right_hints():
    from lightroom_mcp.tools import build_server

    tools = asyncio.run(build_server(None, "x").list_tools())
    hints = {t.name: t.annotations.read_only_hint for t in tools}
    assert hints == {
        "lr_status": True, "lr_list_selected": True, "lr_get_develop": True, "lr_get_preview": True, "lr_doctor": True,
        "lr_set_develop": False, "lr_restore": False, "lr_restore_original": False, "lr_list_edits": True,
        "lr_list_presets": True, "lr_apply_preset": False, "lr_batch_set_develop": False, "lr_get_metadata": True, "lr_set_metadata": False,
    }
    for tool in tools:   # every description is English first and also has the Chinese text
        text = tool.description or ""
        assert text[:1].isascii() and text[:1].isalpha(), tool.name
        assert any("一" <= c <= "鿿" for c in text), tool.name
    set_tool = next(t for t in tools if t.name == "lr_set_develop")
    assert "Exposure2012" in set_tool.description and "单次最多改" in set_tool.description
    assert json.dumps(set_tool.input_schema)  # the schema can be serialised


# ---- white balance (Temperature / Tint) -----------------------------------------------------------------------
def test_white_balance_change_sets_custom_and_restore_brings_the_label_back(env):
    lr, store, service, _ = env
    lr.photos["AAAAAAAA-1111"]["values"].update(Temperature=5600.0, Tint=13.0)
    result = service.set_develop({"Temperature": 6400, "Tint": 20}, reason="warmer")
    assert result["verified"] and lr.photos["AAAAAAAA-1111"]["wb"] == "Custom"
    assert store.load(result["edit_id"])["before_wb"] == "As Shot"
    service.restore(result["edit_id"])
    photo = lr.photos["AAAAAAAA-1111"]
    assert photo["wb"] == "As Shot" and photo["values"]["Temperature"] == 5600.0 and photo["values"]["Tint"] == 13.0


def test_restore_original_after_two_white_balance_edits_uses_the_first_label(env):
    lr, store, service, _ = env
    lr.photos["AAAAAAAA-1111"]["values"].update(Temperature=5600.0, Tint=13.0)
    service.set_develop({"Temperature": 6400})
    service.set_develop({"Temperature": 7000})
    service.restore_original()
    photo = lr.photos["AAAAAAAA-1111"]
    assert photo["wb"] == "As Shot" and photo["values"]["Temperature"] == 5600.0


def test_white_balance_range_is_checked():
    with pytest.raises(EditError):
        validate_changes({"Temperature": 1500})
    with pytest.raises(EditError):
        validate_changes({"Tint": 151})
    assert validate_changes({"Temperature": 6500, "Tint": -20}) == {"Temperature": 6500.0, "Tint": -20.0}


# ---- batch 1: colour grading, calibration, parametric curve, point curves --------------------------------------
def test_colour_grading_ranges():
    assert validate_changes({"SplitToningShadowHue": 220, "SplitToningShadowSaturation": 25, "ColorGradeGlobalLum": -10})
    for bad in ({"SplitToningShadowHue": 361}, {"ColorGradeMidtoneSat": -1}, {"ColorGradeBlending": 101}, {"RedHue": 150}):
        with pytest.raises(EditError):
            validate_changes(bad)


def test_point_curve_validation():
    good = [[0, 0], [64, 56], [192, 200], [255, 255]]
    assert validate_changes({"ToneCurvePV2012": good})["ToneCurvePV2012"] == [0.0, 0.0, 64.0, 56.0, 192.0, 200.0, 255.0, 255.0]
    bad_curves = [
        [], [[0, 0]], [[0, 0], [255, 300]], [[5, 5], [255, 255]], [[0, 0], [100, 100], [100, 120], [255, 255]],
        [[0, 0], [100, 150], [150, 100], [255, 255]],           # goes back down
        [[0, 0], [100, 230], [255, 255]],                        # too far from the straight line
        [[0, 0]] + [[i * 10, i * 10] for i in range(1, 20)] + [[255, 255]],  # too many points
        [[0, 0], [True, 5], [255, 255]], "abc", [[0, 0], [float("nan"), 5], [255, 255]],
    ]
    for curve in bad_curves:
        with pytest.raises(EditError):
            validate_changes({"ToneCurvePV2012": curve})


def test_curve_edit_and_restore_roundtrip(env):
    lr, store, service, _ = env
    result = service.set_develop({"ToneCurvePV2012": [[0, 0], [64, 50], [192, 205], [255, 255]], "ParametricHighlights": -20}, reason="s-curve")
    assert result["verified"] and all(row["ok"] for row in result["changes"])
    assert lr.photos["AAAAAAAA-1111"]["values"]["ToneCurvePV2012"] == [0.0, 0.0, 64.0, 50.0, 192.0, 205.0, 255.0, 255.0]
    service.restore(result["edit_id"])
    values = lr.photos["AAAAAAAA-1111"]["values"]
    assert values["ToneCurvePV2012"] == [0.0, 0.0, 255.0, 255.0] and values["ParametricHighlights"] == 0.0


def test_restore_original_after_two_curve_edits(env):
    lr, store, service, _ = env
    service.set_develop({"ToneCurvePV2012": [[0, 0], [100, 90], [255, 255]]})
    service.set_develop({"ToneCurvePV2012": [[0, 0], [100, 120], [255, 255]]})
    service.restore_original()
    assert lr.photos["AAAAAAAA-1111"]["values"]["ToneCurvePV2012"] == [0.0, 0.0, 255.0, 255.0]


# ---- batch 2: sharpening, noise reduction, effects ------------------------------------------------------------
def test_detail_and_effect_ranges():
    assert validate_changes({"Sharpness": 120, "SharpenRadius": 1.5, "GrainAmount": 30, "PostCropVignetteAmount": -25})
    for bad in ({"Sharpness": 151}, {"SharpenRadius": 0.4}, {"ColorNoiseReduction": -1}, {"DefringeGreenAmount": 21}, {"VignetteAmount": 101}):
        with pytest.raises(EditError):
            validate_changes(bad)


# ---- batch 3: crop and straighten ------------------------------------------------------------------------------
def test_crop_ranges_and_rectangle_rule(env):
    lr, store, service, _ = env
    for k, v in (("CropLeft", 0.0), ("CropTop", 0.0), ("CropRight", 1.0), ("CropBottom", 1.0)):
        lr.photos["AAAAAAAA-1111"]["values"][k] = v
    result = service.set_develop({"CropLeft": 0.1, "CropRight": 0.9}, reason="crop in")
    assert result["verified"]
    with pytest.raises(EditError):
        validate_changes({"CropLeft": -0.1})
    with pytest.raises(EditError):
        validate_changes({"CropAngle": 46})
    with pytest.raises(EditError) as err:   # 0.95 leaves less than 10% of the width next to CropRight=0.9
        service.set_develop({"CropLeft": 0.95})
    assert "至少保留" in str(err.value)
    service.restore(result["edit_id"])
    values = lr.photos["AAAAAAAA-1111"]["values"]
    assert values["CropLeft"] == 0.0 and values["CropRight"] == 1.0


# ---- batch 4: presets --------------------------------------------------------------------------------------------
def test_preset_apply_saves_the_whole_state_first_and_restore_brings_everything_back(env):
    lr, store, service, _ = env
    values = lr.photos["AAAAAAAA-1111"]["values"]
    start = copy.deepcopy(values)
    result = service.apply_preset("PRESET-AAAA-0001", reason="warm it up")
    assert result["preset"] == "Warm look" and result["settings_changed"] >= 4
    assert values["Contrast2012"] == 40.0 and values["Look"]["Name"] == "Vivid"
    record = store.load(result["edit_id"])
    assert record["status"] == "applied" and record["generic"] and "snapshot" not in record
    assert record["before"]["Look"] == {"Name": "Adobe Color", "Amount": 1.0}
    assert "GrainSeed" in record["new_keys"] and "GrainSeed" not in record["before"]
    restored = service.restore(result["edit_id"])
    assert restored["verified"]
    now = {k: v for k, v in values.items() if k != "GrainSeed"}
    assert now == start


def test_preset_pending_record_holds_a_snapshot_before_anything_is_applied(env):
    lr, store, service, _ = env
    seen = {}
    original = lr._apply_preset

    def spy(params):
        recs = store.all()
        seen["snapshot_on_disk"] = bool(recs) and "snapshot" in recs[-1] and recs[-1]["status"] == "pending"
        return original(params)

    lr._apply_preset = spy
    service.apply_preset("PRESET-AAAA-0001")
    assert seen["snapshot_on_disk"] is True


def test_timed_out_preset_keeps_snapshot_and_can_be_restored(env):
    lr, store, service, _ = env
    original = lr._apply_preset

    def applied_then_timed_out(params):
        original(params)
        raise BridgeTimeout("reply timed out")

    lr._apply_preset = applied_then_timed_out
    with pytest.raises(BridgeTimeout):
        service.apply_preset("PRESET-AAAA-0001")
    record = store.all()[0]
    assert record["status"] == "uncertain" and "snapshot" in record
    service.restore(record["edit_id"])
    assert lr.photos[A]["values"]["Contrast2012"] == 0.0
    assert lr.photos[A]["values"]["Look"]["Name"] == "Adobe Color"


def test_restore_original_recovers_a_timed_out_preset(env):
    lr, _, service, _ = env
    original = lr._apply_preset

    def applied_then_timed_out(params):
        original(params)
        raise BridgeTimeout("reply timed out")

    lr._apply_preset = applied_then_timed_out
    with pytest.raises(BridgeTimeout):
        service.apply_preset("PRESET-AAAA-0001")
    service.restore_original()
    assert lr.photos[A]["values"]["Contrast2012"] == 0.0


def test_masked_preset_is_refused_and_recorded_as_failed(env):
    lr, store, service, _ = env
    with pytest.raises(BridgeError):
        service.apply_preset("PRESET-BBBB-0002")
    records = store.all()
    assert records[-1]["status"] == "failed" and "snapshot" not in records[-1]
    with pytest.raises(EditError):
        service.restore(records[-1]["edit_id"])


def test_restore_original_mixes_slider_edits_and_a_preset(env):
    lr, store, service, _ = env
    values = lr.photos["AAAAAAAA-1111"]["values"]
    start = copy.deepcopy(values)
    service.set_develop({"Exposure2012": 0.5})
    service.apply_preset("PRESET-AAAA-0001")
    service.set_develop({"Contrast2012": 60.0})
    service.restore_original()
    assert {k: v for k, v in values.items() if k != "GrainSeed"} == start


def test_preset_ids_are_checked_and_readonly_blocks(env):
    lr, store, service, tmp = env
    for bad in ("", "../x", "a b", "short", None, 5):
        with pytest.raises(EditError):
            service.apply_preset(bad)
    (tmp / "readonly").write_text("1")
    with pytest.raises(ReadOnlyError):
        service.apply_preset("PRESET-AAAA-0001")


def test_list_presets_uses_one_word(env):
    lr, store, service, _ = env
    data = service.list_presets("warm look")
    assert data["total"] == 2 and "warm" in data["note"]


# ---- batch --------------------------------------------------------------------------------------------------------
A, B = "AAAAAAAA-1111", "BBBBBBBB-2222"


def test_batch_dry_run_changes_nothing_and_apply_needs_the_plan(env):
    lr, store, service, _ = env
    lr.selected = [A, B]
    plan = service.batch_plan({"Exposure2012": 0.3, "Contrast2012": 10}, "selected", reason="brighter")
    assert plan["nothing_was_changed"] and plan["will_change"] == 2 and plan["plan_id"]
    assert lr.write_calls == 0 and not store.all()
    first = plan["photos"][0]["changes"][0]
    assert first["parameter"] == "Exposure2012" and abs(first["after"] - first["before"] - 0.3) < 1e-9
    done = service.batch_apply(plan["plan_id"])
    assert done["changed"] == 2 and all(r["verified"] for r in done["results"])
    for pid in (A, B):
        assert lr.photos[pid]["values"]["Exposure2012"] == 0.3 and lr.photos[pid]["values"]["Contrast2012"] == 10.0
    with pytest.raises(EditError):    # the plan is single use
        service.batch_apply(plan["plan_id"])


def test_batch_stops_if_a_photo_changed_after_dry_run(env):
    lr, _, service, _ = env
    lr.selected = [A, B]
    plan = service.batch_plan({"Exposure2012": 0.3}, "selected")
    lr.photos[A]["values"]["Exposure2012"] = 0.2
    result = service.batch_apply(plan["plan_id"])
    assert result["changed"] == 0 and "重新 dry_run" in result["results"][0]["error"]
    assert lr.write_calls == 0
    assert lr.photos[B]["values"]["Exposure2012"] == 0.0


def test_batch_restore_undoes_every_photo_and_is_idempotent(env):
    lr, store, service, _ = env
    lr.selected = [A, B]
    done = service.batch_apply(service.batch_plan({"Exposure2012": 0.5}, "selected")["plan_id"])
    result = service.restore(done["batch_id"])
    assert not result["problems"] and len(result["restored"]) == 2
    assert lr.photos[A]["values"]["Exposure2012"] == 0.0 and lr.photos[B]["values"]["Exposure2012"] == 0.0
    assert "已经" in service.restore(done["batch_id"])["note"]
    assert service.list_edits("all")["count"] >= 5   # 2 sets + 2 restores + the batch record


def test_batch_refuses_what_cannot_be_done_per_photo_and_what_is_too_big(env):
    lr, store, service, _ = env
    lr.photos[A]["values"].update(Temperature=5600.0, Tint=13.0)
    lr.photos[B]["values"].pop("Temperature")      # like a JPEG: no kelvin value
    plan = service.batch_plan({"Temperature": 300}, [A, B])
    by_id = {r["photo_id"]: r for r in plan["photos"]}
    assert by_id[A]["will_change"] and not by_id[B]["will_change"] and "JPEG" in by_id[B]["reason_skipped"]
    assert plan["will_change"] == 1 and plan["skipped"] == 1
    for bad in ({"CropLeft": 0.1}, {"ToneCurvePV2012": 1}, {"Exposure2012": 2.0}, {"Exposure2012": True}, {"Nope": 1}, {}, "x"):
        with pytest.raises(EditError):
            service.batch_plan(bad, [A])
    lr.photos[A]["values"]["Exposure2012"] = 4.9
    plan = service.batch_plan({"Exposure2012": 0.5}, [A])    # 5.4 is out of range for that photo
    assert plan["will_change"] == 0 and plan["plan_id"] is None
    with pytest.raises(EditError):
        service.batch_apply("nope")


def test_batch_limits_on_photo_count_and_ids(env):
    lr, store, service, _ = env
    for bad in ([], ["../x"], ["selected"], [5], "abc", ["a" * 8] * 0):
        with pytest.raises(EditError):
            service.batch_plan({"Exposure2012": 0.1}, bad)
    many = [f"PHOTO{i:04d}-0000" for i in range(11)]
    with pytest.raises(EditError):
        service.batch_plan({"Exposure2012": 0.1}, many)


def test_batch_stops_at_the_first_failure_and_what_was_done_can_be_restored(env):
    lr, store, service, _ = env
    lr.selected = [A, B]
    plan = service.batch_plan({"Exposure2012": 0.4}, "selected")
    original = lr._set
    calls = {"n": 0}

    def flaky(params):
        calls["n"] += 1
        if calls["n"] == 2:
            raise BridgeError("Lightroom 现在不能写入")
        return original(params)

    lr._set = flaky
    done = service.batch_apply(plan["plan_id"])
    assert done["changed"] == 1 and done["stopped_early_at"] and "error" in done["results"][1]
    assert lr.photos[A]["values"]["Exposure2012"] == 0.4 and lr.photos[B]["values"]["Exposure2012"] == 0.0
    service.restore(done["batch_id"])
    assert lr.photos[A]["values"]["Exposure2012"] == 0.0


def test_batch_respects_read_only(env):
    lr, store, service, tmp = env
    (tmp / "readonly").write_text("1")
    with pytest.raises(ReadOnlyError):
        service.batch_plan({"Exposure2012": 0.1}, [A])
    with pytest.raises(ReadOnlyError):
        service.batch_apply("whatever")


# ---- catalog information: rating, flag, colour label, title, caption, keywords --------------------------------------
def test_metadata_change_is_saved_first_and_restore_brings_everything_back(env):
    lr, store, service, _ = env
    lr.photos[A]["meta"].update(rating=2, title="Old title", keywords=["Torino", "sera"])
    start = copy.deepcopy(lr.photos[A]["meta"])
    result = service.set_metadata(rating=5, flag="pick", color_label="red", title="Tramonto", caption="Superga, 18:40",
                                  add_keywords=["tramonto", "Torino"], remove_keywords=["sera"], reason="culling")
    assert result["verified"] and lr.photos[A]["meta"]["rating"] == 5 and lr.photos[A]["meta"]["keywords"] == ["Torino", "tramonto"]
    record = store.load(result["edit_id"])
    assert record["kind"] == "meta" and record["before"]["rating"] == 2
    assert record["added_keywords"] == ["tramonto"] and record["removed_keywords"] == ["sera"]   # "Torino" was already there
    restored = service.restore(result["edit_id"])
    assert restored["verified"]
    now = lr.photos[A]["meta"]
    assert now["rating"] == 2 and now["title"] == "Old title" and now["flag"] == "none" and now["caption"] == ""
    assert sorted(now["keywords"]) == sorted(start["keywords"])
    assert "已经" in service.restore(result["edit_id"])["note"]


def test_metadata_restore_only_touches_what_was_changed(env):
    lr, store, service, _ = env
    result = service.set_metadata(rating=4)
    lr.photos[A]["meta"]["title"] = "typed by hand later"
    service.restore(result["edit_id"])
    assert lr.photos[A]["meta"]["rating"] == 0 and lr.photos[A]["meta"]["title"] == "typed by hand later"


def test_timed_out_metadata_write_keeps_keyword_undo_data(env):
    lr, store, service, _ = env
    original = lr._set_metadata

    def applied_then_timed_out(params):
        original(params)
        raise BridgeTimeout("reply timed out")

    lr._set_metadata = applied_then_timed_out
    with pytest.raises(BridgeTimeout):
        service.set_metadata(rating=4, add_keywords=["test"])
    record = store.all()[0]
    assert record["status"] == "uncertain" and record["added_keywords"] == ["test"]
    lr._set_metadata = original
    service.restore(record["edit_id"])
    assert lr.photos[A]["meta"]["rating"] == 0
    assert lr.photos[A]["meta"]["keywords"] == []


def test_metadata_validation(env):
    lr, store, service, _ = env
    bad = [dict(), dict(rating=6), dict(rating=-1), dict(rating=True), dict(rating="3"), dict(flag="maybe"), dict(color_label="pink"),
           dict(title="x" * 201), dict(title="a\nb"), dict(caption="x" * 1001), dict(add_keywords="sunset"), dict(add_keywords=[""]),
           dict(add_keywords=["a,b"]), dict(add_keywords=["x" * 61]), dict(add_keywords=[f"k{i}" for i in range(11)]),
           dict(add_keywords=["a"], remove_keywords=["a"])]
    for kwargs in bad:
        with pytest.raises(EditError):
            service.set_metadata(**kwargs)
    assert lr.write_calls == 0 and not store.all()
    service.set_metadata(title="")        # clearing is allowed
    service.set_metadata(rating=0)


def test_metadata_failure_is_recorded_and_readonly_blocks(env):
    lr, store, service, tmp = env
    lr.fail_next_write = True
    with pytest.raises(BridgeError):
        service.set_metadata(rating=3)
    assert store.all()[-1]["status"] == "failed"
    with pytest.raises(EditError):
        service.restore(store.all()[-1]["edit_id"])
    (tmp / "readonly").write_text("1")
    with pytest.raises(ReadOnlyError):
        service.set_metadata(rating=3)


def test_metadata_edits_do_not_disturb_restore_original_of_develop_changes(env):
    lr, store, service, _ = env
    service.set_develop({"Exposure2012": 0.5})
    service.set_metadata(rating=5)
    service.restore_original()
    assert lr.photos[A]["values"]["Exposure2012"] == 0.0 and lr.photos[A]["meta"]["rating"] == 5
