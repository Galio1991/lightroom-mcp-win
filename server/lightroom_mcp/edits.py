"""Changing develop settings safely (v1.1): what may be changed, backups of the old values, restore.

Rules (each one was measured on Lightroom Classic 15.2, see docs/实验记录.md):
* only whitelisted sliders, within range, and only a limited step per call;
* before anything is written the old values are saved to disk (``~/LrMcp/edits/<id>.json``);
* restoring = writing the saved values back (Lightroom snapshots and undo were found unreliable);
* a read-only switch (file ``~/LrMcp/readonly`` or ``LRMCP_READONLY=1``) refuses every write.
"""
from __future__ import annotations

import json
import math
import os
import re
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Callable

from . import config
from .bridge import BridgeTimeout

COLORS = ("Red", "Orange", "Yellow", "Green", "Aqua", "Blue", "Purple", "Magenta")
GLOBAL_KEYS = (
    "Contrast2012", "Highlights2012", "Shadows2012", "Whites2012", "Blacks2012",
    "Texture", "Clarity2012", "Dehaze", "Vibrance", "Saturation",
)
HSL_PREFIXES = ("HueAdjustment", "SaturationAdjustment", "LuminanceAdjustment")

# key -> (minimum, maximum, largest change in one call). Keep in sync with plugin/.../Limits.lua (a test compares).
LIMITS: dict[str, tuple[float, float, float]] = {
    "Exposure2012": (-5.0, 5.0, 1.0),
    "Temperature": (2000.0, 50000.0, 1000.0),  # kelvin, RAW photos only
    "Tint": (-150.0, 150.0, 30.0),
}
WB_KEYS = ("Temperature", "Tint")
WB_LABELS = ("As Shot", "Auto", "Daylight", "Cloudy", "Shade", "Tungsten", "Fluorescent", "Flash", "Custom")
for _key in GLOBAL_KEYS:
    LIMITS[_key] = (-100.0, 100.0, 30.0)
for _prefix in HSL_PREFIXES:
    for _color in COLORS:
        LIMITS[_prefix + _color] = (-100.0, 100.0, 30.0)


# batch 1 (experiment 7): colour grading, calibration, parametric tone curve. Hue values are cyclic, so no step limit.
LIMITS.update({
    "SplitToningShadowHue": (0.0, 360.0, 360.0),
    "SplitToningShadowSaturation": (0.0, 100.0, 30.0),
    "ColorGradeShadowLum": (-100.0, 100.0, 30.0),
    "SplitToningHighlightHue": (0.0, 360.0, 360.0),
    "SplitToningHighlightSaturation": (0.0, 100.0, 30.0),
    "ColorGradeHighlightLum": (-100.0, 100.0, 30.0),
    "ColorGradeMidtoneHue": (0.0, 360.0, 360.0),
    "ColorGradeMidtoneSat": (0.0, 100.0, 30.0),
    "ColorGradeMidtoneLum": (-100.0, 100.0, 30.0),
    "ColorGradeGlobalHue": (0.0, 360.0, 360.0),
    "ColorGradeGlobalSat": (0.0, 100.0, 30.0),
    "ColorGradeGlobalLum": (-100.0, 100.0, 30.0),
    "SplitToningBalance": (-100.0, 100.0, 30.0),
    "ColorGradeBlending": (0.0, 100.0, 30.0),
    "ShadowTint": (-100.0, 100.0, 30.0),
    "RedHue": (-100.0, 100.0, 30.0),
    "RedSaturation": (-100.0, 100.0, 30.0),
    "GreenHue": (-100.0, 100.0, 30.0),
    "GreenSaturation": (-100.0, 100.0, 30.0),
    "BlueHue": (-100.0, 100.0, 30.0),
    "BlueSaturation": (-100.0, 100.0, 30.0),
    "ParametricShadows": (-100.0, 100.0, 30.0),
    "ParametricDarks": (-100.0, 100.0, 30.0),
    "ParametricLights": (-100.0, 100.0, 30.0),
    "ParametricHighlights": (-100.0, 100.0, 30.0),
})

# batch 2 (experiment 8): sharpening, noise reduction, vignette, grain, defringe. Stored unchecked by Lightroom, so we check.
LIMITS.update({
    "Sharpness": (0.0, 150.0, 40.0),
    "SharpenRadius": (0.5, 3.0, 1.0),
    "SharpenDetail": (0.0, 100.0, 30.0),
    "SharpenEdgeMasking": (0.0, 100.0, 30.0),
    "LuminanceSmoothing": (0.0, 100.0, 30.0),
    "LuminanceNoiseReductionDetail": (0.0, 100.0, 30.0),
    "LuminanceNoiseReductionContrast": (0.0, 100.0, 30.0),
    "ColorNoiseReduction": (0.0, 100.0, 30.0),
    "ColorNoiseReductionDetail": (0.0, 100.0, 30.0),
    "ColorNoiseReductionSmoothness": (0.0, 100.0, 30.0),
    "PostCropVignetteAmount": (-100.0, 100.0, 30.0),
    "PostCropVignetteMidpoint": (0.0, 100.0, 30.0),
    "PostCropVignetteFeather": (0.0, 100.0, 30.0),
    "PostCropVignetteRoundness": (-100.0, 100.0, 30.0),
    "PostCropVignetteHighlightContrast": (0.0, 100.0, 30.0),
    "GrainAmount": (0.0, 100.0, 30.0),
    "GrainSize": (0.0, 100.0, 30.0),
    "GrainFrequency": (0.0, 100.0, 30.0),
    "VignetteAmount": (-100.0, 100.0, 30.0),
    "VignetteMidpoint": (0.0, 100.0, 30.0),
    "DefringePurpleAmount": (0.0, 20.0, 10.0),
    "DefringeGreenAmount": (0.0, 20.0, 10.0),
})

# batch 3 (experiment 9): crop edges as fractions of the image, straighten angle in degrees
LIMITS.update({
    "CropLeft": (0.0, 1.0, 0.3),
    "CropTop": (0.0, 1.0, 0.3),
    "CropRight": (0.0, 1.0, 0.3),
    "CropBottom": (0.0, 1.0, 0.3),
    "CropAngle": (-45.0, 45.0, 10.0),
})
CROP_KEYS = ("CropLeft", "CropTop", "CropRight", "CropBottom")
MIN_CROP_SIZE = 0.1  # the crop must keep at least 10% of the width and of the height

# Point curves (flat list x1, y1, x2, y2, ...; 0..255). Not in LIMITS: they are lists, not numbers.
CURVE_KEYS = ("ToneCurvePV2012", "ToneCurvePV2012Red", "ToneCurvePV2012Green", "ToneCurvePV2012Blue")
MAX_CURVE_POINTS = 16
MAX_CURVE_DEVIATION = 90.0  # how far a point may be from the straight line (y = x) in one call

META_FLAGS = ("pick", "none", "reject")
META_LABELS = ("red", "yellow", "green", "blue", "purple", "none")
META_MAX_TITLE, META_MAX_CAPTION, META_MAX_KEYWORD, META_MAX_KEYWORDS = 200, 1000, 60, 10
MAX_BATCH_PHOTOS = 10      # photos per batch call
PLAN_LIFETIME_SECONDS = 15 * 60
# A batch changes sliders by the same RELATIVE amount on every photo. Composition (crop, straighten) is per photo and
# white balance needs a readable kelvin value, so crop/angle stay out; white balance is allowed (refused per photo if unreadable).
BATCH_FORBIDDEN = ("CropLeft", "CropTop", "CropRight", "CropBottom", "CropAngle")
PRESET_ID = re.compile(r"^[A-Za-z0-9\-]{8,64}$")
MAX_PRESET_ROWS_IN_REPLY = 60
EDIT_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")

_last_ns = 0


def _now_ns() -> int:
    """Strictly increasing timestamp. The edit id only has second resolution and a random suffix, so the order of
    edits (which decides what 'the original value' is) must not depend on it."""
    global _last_ns
    _last_ns = max(time.time_ns(), _last_ns + 1)
    return _last_ns


class ReadOnlyError(RuntimeError):
    """Writing is switched off."""


class EditError(RuntimeError):
    """A change was refused or could not be completed; the message is written for the user (Chinese)."""


def describe_limits() -> str:
    """One compact text for the tool description."""
    lines = ["Exposure2012：-5 到 +5，单次最多改 ±1.0"]
    lines.append("白平衡（只限 RAW 照片，JPEG 没有开尔文值）：Temperature 色温 2000 到 50000（单位 K，数字越大画面越偏暖），"
                 "单次最多改 ±1000；Tint 色调 -150 到 +150（负偏绿、正偏洋红），单次最多改 ±30")
    lines.append("对比度/高光/阴影/白色/黑色/纹理/清晰度/去朦胧/自然饱和度/饱和度（Contrast2012, Highlights2012, Shadows2012, "
                 "Whites2012, Blacks2012, Texture, Clarity2012, Dehaze, Vibrance, Saturation）：-100 到 +100，单次最多改 ±30")
    lines.append("颜色分级（先让对应的 Saturation > 0，色相才看得出来）：SplitToningShadowHue / SplitToningHighlightHue / ColorGradeMidtoneHue / "
                 "ColorGradeGlobalHue 色相 0 到 360（循环，不限步长）；SplitToningShadowSaturation / SplitToningHighlightSaturation / "
                 "ColorGradeMidtoneSat / ColorGradeGlobalSat 饱和度 0 到 100；ColorGradeShadowLum / ColorGradeHighlightLum / "
                 "ColorGradeMidtoneLum / ColorGradeGlobalLum 明度 -100 到 100；SplitToningBalance -100 到 100；ColorGradeBlending 0 到 100。"
                 "（除色相外）单次最多改 ±30")
    lines.append("相机校准：ShadowTint, RedHue, RedSaturation, GreenHue, GreenSaturation, BlueHue, BlueSaturation：-100 到 +100，单次最多改 ±30")
    lines.append("锐化：Sharpness 数量 0 到 150（单次最多改 ±40），SharpenRadius 半径 0.5 到 3（单次最多 ±1），SharpenDetail 细节、SharpenEdgeMasking 蒙版 0 到 100（±30）")
    lines.append("降噪：LuminanceSmoothing 明亮度、LuminanceNoiseReductionDetail 明亮度细节、LuminanceNoiseReductionContrast 明亮度对比、"
                 "ColorNoiseReduction 颜色、ColorNoiseReductionDetail 颜色细节、ColorNoiseReductionSmoothness 颜色平滑度：0 到 100，单次最多改 ±30")
    lines.append("效果：PostCropVignetteAmount 裁剪后暗角数量（负变暗、正变亮）-100 到 100、PostCropVignetteRoundness -100 到 100、"
                 "PostCropVignetteMidpoint / Feather / HighlightContrast 0 到 100；GrainAmount 颗粒数量、GrainSize 大小、GrainFrequency 粗糙度 0 到 100；"
                 "镜头暗角 VignetteAmount -100 到 100、VignetteMidpoint 0 到 100；去边 DefringePurpleAmount / DefringeGreenAmount 0 到 20（单次最多 ±10）。"
                 "以上除特别说明外单次最多改 ±30")
    lines.append("裁剪：CropLeft / CropTop / CropRight / CropBottom 是四条边的位置，用占整张图的比例表示（0 是左/上边，1 是右/下边；"
                 "不裁剪时是 0, 0, 1, 1）。每个 0 到 1，单次最多移 0.3；宽和高至少保留 10%，且左 < 右、上 < 下。"
                 "CropAngle 是拉直角度，-45 到 45，单次最多 ±10（正负两个方向，改完用 lr_get_preview 确认转的方向对不对）。"
                 "裁剪后 lr_get_preview 看到的就是裁剪后的画面；恢复原图用 lr_restore_original 或把四条边写回 0, 0, 1, 1。")
    lines.append("色调曲线·参数：ParametricShadows, ParametricDarks, ParametricLights, ParametricHighlights：-100 到 +100，单次最多改 ±30")
    lines.append("色调曲线·点：ToneCurvePV2012（亮度）、ToneCurvePV2012Red / Green / Blue（三个通道），值是点的列表，"
                 "例如 [[0,0],[64,56],[192,200],[255,255]]：x 是原亮度、y 是调整后的亮度（0 到 255），2 到 16 个点，"
                 "第一个点 x=0、最后一个点 x=255，x 递增，y 不往回走，每个点离直线 y=x 最多 90")
    lines.append("HSL（HueAdjustment / SaturationAdjustment / LuminanceAdjustment + Red, Orange, Yellow, Green, Aqua, Blue, "
                 "Purple, Magenta）：-100 到 +100，单次最多改 ±30")
    return "\n".join(lines)


def describe_limits_en() -> str:
    """The same table as describe_limits(), in English (for the tool description)."""
    lines = ["Exposure2012: -5 to +5, at most +-1.0 per call"]
    lines.append("White balance (RAW photos only, JPEGs have no kelvin value): Temperature 2000 to 50000 K (higher = warmer picture), "
                 "at most +-1000 per call; Tint -150 to +150 (negative = greener, positive = more magenta), at most +-30 per call")
    lines.append("Contrast2012, Highlights2012, Shadows2012, Whites2012, Blacks2012, Texture, Clarity2012, Dehaze, Vibrance, Saturation: "
                 "-100 to +100, at most +-30 per call")
    lines.append("Colour grading (raise the matching saturation above 0 or the hue is not visible): hue SplitToningShadowHue / "
                 "SplitToningHighlightHue / ColorGradeMidtoneHue / ColorGradeGlobalHue 0 to 360 (cyclic, no step limit); saturation "
                 "SplitToningShadowSaturation / SplitToningHighlightSaturation / ColorGradeMidtoneSat / ColorGradeGlobalSat 0 to 100; "
                 "luminance ColorGradeShadowLum / ColorGradeHighlightLum / ColorGradeMidtoneLum / ColorGradeGlobalLum -100 to 100; "
                 "SplitToningBalance -100 to 100; ColorGradeBlending 0 to 100. At most +-30 per call (except hue)")
    lines.append("Camera calibration: ShadowTint, RedHue, RedSaturation, GreenHue, GreenSaturation, BlueHue, BlueSaturation: "
                 "-100 to +100, at most +-30 per call")
    lines.append("Sharpening: Sharpness 0 to 150 (at most +-40), SharpenRadius 0.5 to 3 (at most +-1), SharpenDetail and "
                 "SharpenEdgeMasking 0 to 100 (+-30)")
    lines.append("Noise reduction: LuminanceSmoothing, LuminanceNoiseReductionDetail, LuminanceNoiseReductionContrast, ColorNoiseReduction, "
                 "ColorNoiseReductionDetail, ColorNoiseReductionSmoothness: 0 to 100, at most +-30 per call")
    lines.append("Effects: PostCropVignetteAmount (negative = darker, positive = lighter) and PostCropVignetteRoundness -100 to 100; "
                 "PostCropVignetteMidpoint / Feather / HighlightContrast 0 to 100; GrainAmount / GrainSize / GrainFrequency 0 to 100; "
                 "lens vignette VignetteAmount -100 to 100, VignetteMidpoint 0 to 100; defringe DefringePurpleAmount / DefringeGreenAmount "
                 "0 to 20 (at most +-10). +-30 per call unless stated")
    lines.append("Crop: CropLeft / CropTop / CropRight / CropBottom are the edge positions as fractions of the whole image (0 = left/top edge, "
                 "1 = right/bottom edge; uncropped is 0, 0, 1, 1). Each 0 to 1, an edge moves at most 0.3 per call; at least 10% of the width "
                 "and of the height must stay, with left < right and top < bottom. CropAngle is the straighten angle, -45 to 45, at most +-10 "
                 "per call (check with lr_get_preview which way it turns). lr_get_preview shows the cropped picture; to undo, use "
                 "lr_restore_original or write the edges back as 0, 0, 1, 1")
    lines.append("Parametric tone curve: ParametricShadows, ParametricDarks, ParametricLights, ParametricHighlights: -100 to +100, at most +-30 per call")
    lines.append("Point tone curve: ToneCurvePV2012 (luminance) and ToneCurvePV2012Red / Green / Blue; the value is a list of points, e.g. "
                 "[[0,0],[64,56],[192,200],[255,255]]: x = input level, y = output level (0 to 255), 2 to 16 points, the first point has x=0 and "
                 "the last x=255, x increasing, y never going back down, every point at most 90 away from the straight line y=x")
    lines.append("HSL (HueAdjustment / SaturationAdjustment / LuminanceAdjustment + Red, Orange, Yellow, Green, Aqua, Blue, Purple, Magenta): "
                 "-100 to +100, at most +-30 per call")
    return "\n".join(lines)


def validate_curve(key: str, value: object) -> list[float]:
    """A point curve as [[x, y], ...] (or a flat list). Returns the flat list [x1, y1, x2, y2, ...]."""
    flat: list[float] = []
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, (list, tuple)):
                flat.extend(item)
            else:
                flat.append(item)
    if not flat or len(flat) % 2:
        raise EditError(f"{key} 必须是点的列表，例如 [[0,0],[64,56],[192,200],[255,255]]（x 是原亮度，y 是调整后的亮度，都是 0 到 255）。")
    for n in flat:
        if isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) or not 0 <= n <= 255:
            raise EditError(f"{key} 里的坐标必须是 0 到 255 的数字，你给的是 {n!r}。")
    xs, ys = flat[0::2], flat[1::2]
    if not 2 <= len(xs) <= MAX_CURVE_POINTS:
        raise EditError(f"{key} 需要 2 到 {MAX_CURVE_POINTS} 个点，你给了 {len(xs)} 个。")
    if xs[0] != 0 or xs[-1] != 255:
        raise EditError(f"{key} 的第一个点的 x 必须是 0，最后一个点的 x 必须是 255。")
    if any(b <= a for a, b in zip(xs, xs[1:])):
        raise EditError(f"{key} 的点必须按 x 从小到大排列，且 x 不能重复。")
    if any(b < a for a, b in zip(ys, ys[1:])):
        raise EditError(f"{key} 的 y 不能往回走（曲线必须一直向上或持平；不支持反相曲线）。")
    if any(abs(y - x) > MAX_CURVE_DEVIATION for x, y in zip(xs, ys)):
        raise EditError(f"{key} 的每个点离直线（y = x）最多 {MAX_CURVE_DEVIATION:g}，一次改太大了。请先改一部分，用 lr_get_preview 看效果再继续。")
    return [float(n) for n in flat]


def validate_changes(changes: object) -> dict[str, object]:
    """Check keys, types and ranges. The per-call step limit is enforced by the plug-in (it knows the current value)."""
    if not isinstance(changes, dict) or not changes:
        raise EditError("changes 必须是非空的 {参数名: 数值} 字典，例如 {\"Exposure2012\": 0.3}。")
    if len(changes) > config.MAX_KEYS_PER_EDIT:
        raise EditError(f"一次最多改 {config.MAX_KEYS_PER_EDIT} 个参数，你给了 {len(changes)} 个。请分几次改。")
    clean: dict[str, object] = {}
    for key, value in changes.items():
        if key in CURVE_KEYS:
            clean[key] = validate_curve(key, value)
            continue
        if key not in LIMITS:
            raise EditError(
                f"不允许修改“{key}”。当前版本只允许这些：\n{describe_limits()}\n（色调曲线、蒙版、裁剪、预设暂不支持。）"
            )
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise EditError(f"{key} 的值必须是有限的数字，你给的是 {value!r}。")
        low, high, _ = LIMITS[key]
        if value < low or value > high:
            raise EditError(f"{key} 的取值范围是 {low:g} 到 {high:g}，你给的是 {value:g}。")
        clean[key] = float(value)
    return clean


def check_crop(wanted: dict, settings: dict) -> None:
    """The four crop edges together must make a sensible rectangle. ``settings`` are the current values."""
    if not any(k in wanted for k in CROP_KEYS):
        return
    now = {k: wanted.get(k, settings.get(k)) for k in CROP_KEYS}
    if not all(isinstance(v, (int, float)) for v in now.values()):
        raise EditError("读不到这张照片当前的裁剪值，已取消修改。")
    if now["CropRight"] - now["CropLeft"] < MIN_CROP_SIZE or now["CropBottom"] - now["CropTop"] < MIN_CROP_SIZE:
        raise EditError(
            f"裁剪后的宽和高都必须至少保留原图的 {MIN_CROP_SIZE:.0%}，并且 CropLeft < CropRight、CropTop < CropBottom。"
            f"改完会是：左 {now['CropLeft']:g} 右 {now['CropRight']:g} 上 {now['CropTop']:g} 下 {now['CropBottom']:g}。"
        )


def fmt(value) -> str:
    """Number (or point list) as text for the plug-in command line (no spaces, no locale)."""
    if isinstance(value, (list, tuple)):
        return ",".join(format(float(v), ".10g") for v in value)
    return format(float(value), ".10g")


def as_number_or_list(value):
    """Numbers and number lists become floats; anything else (strings, flags, tables of a preset's old values) is kept."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (list, tuple)) and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
        return [float(v) for v in value]
    return value


def current_values(develop: dict, keys) -> dict:
    """Pick the current value of each wanted key out of a ``get_develop`` reply."""
    out: dict[str, float] = {}
    for key in keys:
        value = None
        if key in CURVE_KEYS:
            curve = develop.get("settings", {}).get(key)
            if not isinstance(curve, list) or len(curve) < 4:
                raise EditError(f"读不到 {key} 的当前曲线，已取消修改。")
            out[key] = [float(n) for n in curve]
            continue
        for prefix, field in (("HueAdjustment", "hue"), ("SaturationAdjustment", "saturation"), ("LuminanceAdjustment", "luminance")):
            if key.startswith(prefix):
                value = develop.get("hsl", {}).get(key[len(prefix):], {}).get(field)
                break
        else:
            value = develop.get("settings", {}).get(key)
        if not isinstance(value, (int, float)):
            if key in WB_KEYS:
                raise EditError("这张照片读不到色温/色调的当前值（JPEG 没有开尔文值；没编辑过的 RAW 也可能读不到，"
                                "先在 Lightroom 里对它做任意一次编辑再试），已取消修改。")
            raise EditError(f"读不到 {key} 的当前值，已取消修改。")
        out[key] = float(value)
    return out


class EditStore:
    """One JSON file per change in ``directory``."""

    def __init__(self, directory: Path = config.EDIT_DIR):
        self.directory = directory

    def new_id(self) -> str:
        return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)

    def _path(self, edit_id: str) -> Path:
        if not EDIT_ID.match(edit_id):
            raise EditError(f"修改编号“{edit_id}”不合法（应形如 20261102-120000-ab12）。")
        return self.directory / f"{edit_id}.json"

    def save(self, record: dict) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(record["edit_id"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def load(self, edit_id: str) -> dict:
        path = self._path(edit_id)
        if not path.is_file():
            raise EditError(f"没有找到修改记录 {edit_id}。用 lr_list_edits 查看已有的记录。")
        return json.loads(path.read_text(encoding="utf-8"))

    def all(self) -> list[dict]:
        records = []
        if self.directory.is_dir():
            for f in self.directory.glob("*.json"):
                try:
                    records.append(json.loads(f.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    pass
        return sorted(records, key=lambda r: (r.get("created_ns", 0), r.get("edit_id", "")))


Call = Callable[..., dict]


class EditService:
    """The logic behind lr_set_develop / lr_restore / lr_restore_original / lr_list_edits.

    ``call(command, params, timeout)`` talks to the plug-in (``Bridge.call``).
    """

    def __init__(self, call: Call, store: EditStore | None = None, readonly_file: Path = config.READONLY_FILE):
        self.call = call
        self.store = store or EditStore()
        self.readonly_file = readonly_file
        self._plans: dict[str, dict] = {}   # batch plans from a dry run; in memory only, single use

    # ---- guards ----------------------------------------------------
    def check_writable(self) -> None:
        if self.readonly_file.exists() or os.environ.get("LRMCP_READONLY") == "1":
            raise ReadOnlyError(
                "只读模式已开启，不允许修改照片。要关闭它：删除文件 "
                f"{self.readonly_file}（并确认环境变量 LRMCP_READONLY 不是 1）。"
            )

    # ---- helpers ---------------------------------------------------
    def _resolve_photo(self, photo: str) -> tuple[str, dict]:
        """Return (uuid, develop reply) for 'selected' or a uuid, so later steps use one fixed photo."""
        develop = self.call("get_develop", {"photo": photo}, timeout=30)
        return develop["photo"]["id"], develop

    def _write(self, photo_id: str, edit_id: str, values: dict[str, float], restoring: bool, wb: str | None = None) -> dict:
        params = {"photo": photo_id, "edit": edit_id}
        if restoring:
            params["restore"] = "1"
            if wb in WB_LABELS and any(k in WB_KEYS for k in values):
                params["wb"] = wb.replace(" ", "_")  # the command line is split at spaces
        params.update({k: fmt(v) for k, v in values.items()})
        return self.call("set_develop", params, timeout=60)

    def _write_generic(self, photo_id: str, edit_id: str, values: dict) -> dict:
        """Write back old values of any setting (used to undo presets). The plug-in checks key, type and range itself."""
        blob = urllib.parse.quote(json.dumps(values, ensure_ascii=False, separators=(",", ":")), safe="")
        return self.call("restore_settings", {"photo": photo_id, "edit": edit_id, "blob": blob}, timeout=60)

    @staticmethod
    def _summary(before: dict, after: dict, requested: dict) -> list[dict]:
        rows = []
        for key, want in requested.items():
            got = after.get(key)
            if not isinstance(want, (int, float, list)) or isinstance(want, bool):
                ok = got == want
            elif isinstance(want, list):
                ok = isinstance(got, list) and len(got) == len(want) and all(abs(a - b) < 1e-3 for a, b in zip(got, want))
            else:
                ok = isinstance(got, (int, float)) and abs(got - want) < 1e-3
            rows.append({"parameter": key, "before": before.get(key), "requested": want, "after": got, "ok": ok})
        return rows

    # ---- the four operations ---------------------------------------
    def set_develop(self, changes: dict, reason: str = "", photo: str = "selected") -> dict:
        self.check_writable()
        wanted = validate_changes(changes)
        photo_id, develop = self._resolve_photo(photo)
        before_pre = current_values(develop, wanted)
        check_crop(wanted, develop.get("settings", {}))

        record = {
            "edit_id": self.store.new_id(), "kind": "set", "status": "pending",
            "created": time.strftime("%Y-%m-%d %H:%M:%S"), "created_ns": _now_ns(),
            "photo_id": photo_id, "photo_name": develop["photo"].get("name"),
            "reason": str(reason)[:500], "requested": wanted, "before": before_pre,
        }
        if any(k in WB_KEYS for k in wanted):
            record["before_wb"] = develop.get("settings", {}).get("WhiteBalance")
        self.store.save(record)  # the old values are on disk BEFORE anything is written
        try:
            data = self._write(photo_id, record["edit_id"], wanted, restoring=False)
        except BridgeTimeout as e:
            record.update(status="uncertain", error=str(e))
            self.store.save(record)
            raise
        except Exception as e:
            record.update(status="failed", error=str(e))
            self.store.save(record)
            raise
        record.update(status="applied", before=data.get("before", before_pre), after=data.get("after", {}),
                      verified=bool(data.get("verified")))
        self.store.save(record)
        return {
            "edit_id": record["edit_id"],
            "photo": record["photo_name"],
            "photo_id": photo_id,
            "verified": record["verified"],
            "changes": self._summary(record["before"], record["after"], wanted),
            "undo": f"lr_restore(edit_id=\"{record['edit_id']}\") 写回这次修改前的值；"
                    "lr_restore_original 写回 LrMCP 第一次改这张照片之前的值。",
        }

    def restore(self, edit_id: str) -> dict:
        self.check_writable()
        record = self.store.load(edit_id)
        if record.get("kind") == "meta":
            return self._restore_meta(record)
        if record.get("kind") == "batch":
            return self._restore_batch(record)
        if record.get("kind") != "set":
            raise EditError("这条记录本身是一次还原，不能再还原。")
        if record.get("restored_by"):
            return {"edit_id": edit_id, "note": f"这次修改已经在 {record['restored_by']} 里还原过了，没有再做任何事。"}
        if record.get("status") == "failed":
            raise EditError("这次修改当时失败了，没有改动照片，不需要还原。")
        values = {k: as_number_or_list(v) for k, v in record["before"].items()}
        new_id = self.store.new_id()
        if record.get("generic"):
            if record.get("status") == "uncertain":
                snapshot = record["snapshot"]
                current = self.call("snapshot", {"photo": record["photo_id"]}, timeout=60)["settings"]
                values = {k: v for k, v in snapshot.items() if k in current and current[k] != v}
                if not values:
                    record["restored_by"] = "no change"
                    self.store.save(record)
                    return {"edit_id": edit_id, "note": "预设没有改变已有设置，无需还原。"}
            data = self._write_generic(record["photo_id"], new_id, values)
            values = {k: v for k, v in values.items() if not isinstance(v, (dict, list))}  # what the summary can compare
        else:
            data = self._write(record["photo_id"], new_id, values, restoring=True, wb=record.get("before_wb"))
        self.store.save({
            "edit_id": new_id, "kind": "restore", "status": "applied", "of": [edit_id],
            "created": time.strftime("%Y-%m-%d %H:%M:%S"), "created_ns": _now_ns(), "photo_id": record["photo_id"],
            "photo_name": record.get("photo_name"), "requested": values,
            "before": data.get("before", {}), "after": data.get("after", {}), "verified": bool(data.get("verified")),
        })
        record["restored_by"] = new_id
        self.store.save(record)
        return {
            "edit_id": new_id, "restored": edit_id, "photo": record.get("photo_name"),
            "verified": bool(data.get("verified")),
            "changes": self._summary(data.get("before", {}), data.get("after", {}), values),
        }

    def restore_original(self, photo: str = "selected", dry_run: bool = False) -> dict:
        if not dry_run:
            self.check_writable()
        if photo == "selected":
            listing = self.call("list_selected", {"limit": 1}, timeout=30)
            if not listing.get("active_id"):
                raise EditError("Lightroom 里没有选中的照片。")
            photo_id = listing["active_id"]
        else:
            photo_id = photo
        history = [r for r in self.store.all()
                   if r.get("photo_id") == photo_id and r.get("kind") == "set"
                   and r.get("status") in ("applied", "pending", "uncertain")]
        records = [r for r in history if not r.get("restored_by")]
        if not records:
            return {"photo_id": photo_id, "note": "没有 LrMCP 改过这张照片（或都已还原），无需还原。"}
        for r in records:
            if r.get("generic") and r.get("status") == "uncertain" and r.get("snapshot"):
                current = self.call("snapshot", {"photo": photo_id}, timeout=60)["settings"]
                r["before"] = {k: v for k, v in r["snapshot"].items() if k in current and current[k] != v}
        earliest: dict = {}
        earliest_wb: str | None = None
        active_keys = {k for r in records for k in r.get("before", {})}
        for r in history:  # restored earlier edits still establish the original value
            for key, value in r.get("before", {}).items():
                if key in active_keys:
                    earliest.setdefault(key, as_number_or_list(value))
            if earliest_wb is None and r.get("before_wb"):
                earliest_wb = r["before_wb"]
        if not earliest:
            return {"photo_id": photo_id, "note": "没有需要写回的设置。"}
        plan = {"photo": records[0].get("photo_name"), "edits_undone": [r["edit_id"] for r in records], "will_write": earliest}
        if dry_run:
            return {**plan, "dry_run": True,
                    "warning": "会把这些滑块写回 LrMCP 第一次改动之前的值；如果你期间手动改过同一个滑块，也会被覆盖。"}
        new_id = self.store.new_id()
        if any(r.get("generic") for r in records):
            payload = dict(earliest)
            if earliest_wb and any(k in WB_KEYS for k in payload) and "WhiteBalance" not in payload:
                payload["WhiteBalance"] = earliest_wb
            data = self._write_generic(photo_id, new_id, payload)
            earliest = {k: v for k, v in earliest.items() if not isinstance(v, (dict, list))}
        else:
            data = self._write(photo_id, new_id, earliest, restoring=True, wb=earliest_wb)
        self.store.save({
            "edit_id": new_id, "kind": "restore", "status": "applied", "of": plan["edits_undone"],
            "created": time.strftime("%Y-%m-%d %H:%M:%S"), "created_ns": _now_ns(), "photo_id": photo_id, "photo_name": plan["photo"],
            "requested": earliest, "before": data.get("before", {}), "after": data.get("after", {}),
            "verified": bool(data.get("verified")),
        })
        for r in records:
            r["restored_by"] = new_id
            self.store.save(r)
        return {
            "edit_id": new_id, "photo": plan["photo"], "edits_undone": plan["edits_undone"],
            "verified": bool(data.get("verified")),
            "changes": self._summary(data.get("before", {}), data.get("after", {}), earliest),
        }

    # ---- catalog information: rating, flag, colour label, title, caption, keywords ----------------
    @staticmethod
    def _quote(text: str) -> str:
        return urllib.parse.quote(text, safe="")

    @staticmethod
    def _keyword_list(names, what: str) -> list[str]:
        if names is None:
            return []
        if not isinstance(names, (list, tuple)) or not all(isinstance(n, str) for n in names):
            raise EditError(f"{what} 必须是关键词的列表，例如 [\"sunset\", \"Torino\"]。")
        clean = []
        for n in names:
            n = n.strip()
            if not n or len(n) > META_MAX_KEYWORD or re.search(r"[\x00-\x1f\x7f,]", n):
                raise EditError(f"{what} 里的关键词不合法：不能为空、不能超过 {META_MAX_KEYWORD} 个字符、不能含逗号或控制字符（{n!r}）。")
            if n not in clean:
                clean.append(n)
        if len(clean) > META_MAX_KEYWORDS:
            raise EditError(f"一次最多处理 {META_MAX_KEYWORDS} 个关键词，{what} 给了 {len(clean)} 个。")
        return clean

    def get_metadata(self, photo: str = "selected") -> dict:
        return self.call("get_metadata", {"photo": photo}, timeout=30)

    def _meta_params(self, rating, flag, color_label, title, caption, add_keywords, remove_keywords) -> tuple[dict, dict]:
        """Validate and return (plain values for the record, command parameters for the plug-in)."""
        plain: dict = {}
        wire: dict = {}
        if rating is not None:
            if isinstance(rating, bool) or not isinstance(rating, int) or not 0 <= rating <= 5:
                raise EditError("rating（星级）必须是 0 到 5 的整数，0 表示没有星。")
            plain["rating"], wire["rating"] = rating, str(rating)
        if flag is not None:
            if flag not in META_FLAGS:
                raise EditError("flag（旗标）必须是 pick（留用）、none（无）或 reject（排除）。")
            plain["flag"], wire["flag"] = flag, flag
        if color_label is not None:
            if str(color_label).lower() not in META_LABELS:
                raise EditError("color_label（颜色标签）必须是 red、yellow、green、blue、purple 或 none。")
            plain["color_label"], wire["color"] = str(color_label).lower(), str(color_label).lower()
        for name, value, limit, key in (("title（标题）", title, META_MAX_TITLE, "title"), ("caption（说明文字）", caption, META_MAX_CAPTION, "caption")):
            if value is not None:
                if not isinstance(value, str) or len(value) > limit or re.search(r"[\x00-\x1f\x7f]", value):
                    raise EditError(f"{name} 必须是不超过 {limit} 个字符、不含控制字符的文字（空字符串表示清空）。")
                plain[key], wire[key] = value, self._quote(value)
        adds = self._keyword_list(add_keywords, "add_keywords")
        removes = self._keyword_list(remove_keywords, "remove_keywords")
        if set(adds) & set(removes):
            raise EditError("同一个关键词不能同时出现在 add_keywords 和 remove_keywords 里。")
        if adds:
            plain["add_keywords"], wire["add_kw"] = adds, ",".join(self._quote(n) for n in adds)
        if removes:
            plain["remove_keywords"], wire["remove_kw"] = removes, ",".join(self._quote(n) for n in removes)
        if not plain:
            raise EditError("没有要修改的内容：至少给 rating、flag、color_label、title、caption、add_keywords、remove_keywords 之一。")
        return plain, wire

    def set_metadata(self, rating=None, flag=None, color_label=None, title=None, caption=None,
                     add_keywords=None, remove_keywords=None, reason: str = "", photo: str = "selected") -> dict:
        self.check_writable()
        plain, wire = self._meta_params(rating, flag, color_label, title, caption, add_keywords, remove_keywords)
        current = self.call("get_metadata", {"photo": photo}, timeout=30)
        photo_id, before = current["photo"]["id"], current["metadata"]
        record = {
            "edit_id": self.store.new_id(), "kind": "meta", "status": "pending",
            "created": time.strftime("%Y-%m-%d %H:%M:%S"), "created_ns": _now_ns(),
            "photo_id": photo_id, "photo_name": current["photo"].get("name"),
            "reason": str(reason)[:500], "requested": plain, "before": before,
        }
        self.store.save(record)   # the old values are on disk BEFORE anything is written
        try:
            data = self.call("set_metadata", {"photo": photo_id, "edit": record["edit_id"], **wire}, timeout=60)
        except BridgeTimeout as e:
            had = set(before.get("keywords", []))
            record.update(status="uncertain", error=str(e),
                          added_keywords=[k for k in plain.get("add_keywords", []) if k not in had],
                          removed_keywords=[k for k in plain.get("remove_keywords", []) if k in had])
            self.store.save(record)
            raise
        except Exception as e:
            record.update(status="failed", error=str(e))
            self.store.save(record)
            raise
        after = data.get("after", {})
        had = set(before.get("keywords", []))
        record.update(status="applied", after=after, verified=bool(data.get("verified")),
                      added_keywords=[k for k in plain.get("add_keywords", []) if k not in had],
                      removed_keywords=[k for k in plain.get("remove_keywords", []) if k in had])
        self.store.save(record)
        return {
            "edit_id": record["edit_id"], "photo": record["photo_name"], "photo_id": photo_id,
            "verified": record["verified"], "before": before, "after": after,
            "undo": f"lr_restore(edit_id=\"{record['edit_id']}\") 把这些信息写回修改前的样子（新建过的关键词会留在 Lightroom 的关键词列表里，只是从这张照片上去掉）。",
        }

    def _restore_meta(self, record: dict) -> dict:
        if record.get("restored_by"):
            return {"edit_id": record["edit_id"], "note": f"这次修改已经在 {record['restored_by']} 里还原过了，没有再做任何事。"}
        if record.get("status") == "failed":
            raise EditError("这次修改当时失败了，没有改动照片，不需要还原。")
        before, asked = record["before"], record["requested"]
        wire: dict = {}
        if "rating" in asked:
            wire["rating"] = str(int(before.get("rating", 0)))
        if "flag" in asked:
            wire["flag"] = before.get("flag", "none")
        if "color_label" in asked:
            label = str(before.get("color_label", "none")).lower()
            wire["color"] = label if label in META_LABELS else "none"
        for key in ("title", "caption"):
            if key in asked:
                wire[key] = self._quote(before.get(key, ""))
        if record.get("removed_keywords"):
            wire["add_kw"] = ",".join(self._quote(n) for n in record["removed_keywords"])
        if record.get("added_keywords"):
            wire["remove_kw"] = ",".join(self._quote(n) for n in record["added_keywords"])
        if not wire:
            raise EditError("这次修改没有改变任何东西，不需要还原。")
        new_id = self.store.new_id()
        data = self.call("set_metadata", {"photo": record["photo_id"], "edit": new_id, **wire}, timeout=60)
        self.store.save({
            "edit_id": new_id, "kind": "restore", "status": "applied", "of": [record["edit_id"]],
            "created": time.strftime("%Y-%m-%d %H:%M:%S"), "created_ns": _now_ns(), "photo_id": record["photo_id"],
            "photo_name": record.get("photo_name"), "requested": {}, "before": data.get("before", {}), "after": data.get("after", {}),
            "verified": bool(data.get("verified")),
        })
        record["restored_by"] = new_id
        self.store.save(record)
        return {"edit_id": new_id, "restored": record["edit_id"], "photo": record.get("photo_name"),
                "verified": bool(data.get("verified")), "now": data.get("after", {})}

    # ---- batch -----------------------------------------------------
    def _batch_photo_ids(self, photos) -> list[str]:
        if photos in ("selected", None, ""):
            listing = self.call("list_selected", {"limit": MAX_BATCH_PHOTOS + 1}, timeout=30)
            ids = [p["id"] for p in listing.get("photos", [])]
            if listing.get("count", len(ids)) > MAX_BATCH_PHOTOS:
                raise EditError(f"选中了 {listing.get('count')} 张照片，一次批量最多 {MAX_BATCH_PHOTOS} 张。请少选几张，或分几批做。")
        elif isinstance(photos, (list, tuple)) and all(isinstance(p, str) for p in photos):
            ids = list(dict.fromkeys(photos))   # no duplicates, order kept
        else:
            raise EditError('photos 必须是 "selected"，或 lr_list_selected 返回的照片 id 列表。')
        if not ids:
            raise EditError("没有要处理的照片：Lightroom 里没有选中的照片。")
        if len(ids) > MAX_BATCH_PHOTOS:
            raise EditError(f"一次批量最多 {MAX_BATCH_PHOTOS} 张照片，你给了 {len(ids)} 张。")
        for p in ids:
            if p == "selected" or not re.match(r"^[A-Za-z0-9\-]{8,64}$", p):
                raise EditError("照片 id 不合法：请用 lr_list_selected 返回的 id。")
        return ids

    def batch_plan(self, deltas: dict, photos="selected", reason: str = "") -> dict:
        """Dry run: nothing is written. Returns what every photo would become and a single-use plan id."""
        self.check_writable()
        if not isinstance(deltas, dict) or not deltas:
            raise EditError("changes 必须是非空的 {参数名: 相对改动量} 字典，例如 {\"Exposure2012\": 0.3}（每张照片在自己当前值上加这个数）。")
        clean: dict[str, float] = {}
        for key, value in deltas.items():
            if key in BATCH_FORBIDDEN or key in CURVE_KEYS:
                raise EditError(f"批量不支持“{key}”（裁剪、拉直、曲线都要一张一张看着改）。")
            if key not in LIMITS:
                raise EditError(f"不允许修改“{key}”。可用的参数见 lr_set_develop 的说明。")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise EditError(f"{key} 的改动量必须是有限的数字，你给的是 {value!r}。")
            step = LIMITS[key][2]
            if abs(value) > step + 1e-9:
                raise EditError(f"{key} 单次最多改 ±{step:g}，你给的是 {value:g}。请分几次改。")
            clean[key] = float(value)
        ids = self._batch_photo_ids(photos)
        rows, items = [], []
        for pid in ids:
            develop = self.call("get_develop", {"photo": pid}, timeout=30)
            name = develop["photo"].get("name")
            try:
                before = current_values(develop, clean)
                targets = {k: before[k] + clean[k] for k in clean}
                validate_changes(targets)
            except EditError as e:
                rows.append({"photo": name, "photo_id": pid, "will_change": False, "reason_skipped": str(e)})
                continue
            rows.append({"photo": name, "photo_id": pid, "will_change": True,
                         "changes": [{"parameter": k, "before": before[k], "after": targets[k]} for k in clean]})
            items.append((pid, name, before, targets))
        plan_id = secrets.token_hex(6)
        now = time.time()
        for old in [k for k, v in self._plans.items() if v["expires"] < now]:
            del self._plans[old]
        if items:
            self._plans[plan_id] = {"items": items, "deltas": clean, "reason": str(reason)[:500], "expires": now + PLAN_LIFETIME_SECONDS}
        return {
            "dry_run": True, "nothing_was_changed": True,
            "plan_id": plan_id if items else None,
            "photos": rows, "will_change": len(items), "skipped": len(rows) - len(items),
            "next": ("把上面每张照片的结果用大白话讲给用户，得到同意后再调用 lr_batch_set_develop(dry_run=false, plan_id=\"" + plan_id + "\")。"
                     f"这个计划 {PLAN_LIFETIME_SECONDS // 60} 分钟内有效，只能用一次。") if items else "没有任何照片可以改，不需要执行。",
        }

    def batch_apply(self, plan_id: str, reason: str = "") -> dict:
        self.check_writable()
        plan = self._plans.pop(str(plan_id), None)
        if plan is None or plan["expires"] < time.time():
            raise EditError("找不到这个计划，或者它已经用过 / 过期了。请先用 dry_run=true 重新预演一次。")
        results, members = [], []
        stopped = None
        for pid, name, before, targets in plan["items"]:
            try:
                current = current_values(self.call("get_develop", {"photo": pid}, timeout=30), before)
                if any(abs(current[k] - value) > 1e-3 for k, value in before.items()):
                    raise EditError("照片参数在预演后已改变；请重新 dry_run。")
                done = self.set_develop(targets, reason=reason or plan["reason"], photo=pid)
                members.append(done["edit_id"])
                results.append({"photo": name, "edit_id": done["edit_id"], "verified": done["verified"]})
            except Exception as e:  # noqa: BLE001 - stop at the first problem, what is done can be restored
                results.append({"photo": name, "error": str(e)})
                stopped = name
                break
        batch_id = self.store.new_id()
        self.store.save({
            "edit_id": batch_id, "kind": "batch", "status": "applied" if stopped is None else "partial",
            "created": time.strftime("%Y-%m-%d %H:%M:%S"), "created_ns": _now_ns(),
            "photo_id": "", "photo_name": f"批量 {len(members)} 张", "reason": plan["reason"],
            "requested": plan["deltas"], "members": members,
        })
        not_done = len(plan["items"]) - len(results)   # photos after the one that failed
        return {
            "batch_id": batch_id, "changed": len(members), "results": results,
            "stopped_early_at": stopped, "not_done": not_done if stopped else 0,
            "undo": f"lr_restore(edit_id=\"{batch_id}\") 一次还原这批里全部照片；也可以用其中某张的 edit_id 单独还原。",
        }

    def _restore_batch(self, record: dict) -> dict:
        if record.get("restored_by"):
            return {"edit_id": record["edit_id"], "note": "这批修改已经还原过了，没有再做任何事。"}
        restored, problems = [], []
        for member in record.get("members", []):
            try:
                result = self.restore(member)
                restored.append({"edit_id": member, "photo": result.get("photo"), "note": result.get("note"), "verified": result.get("verified")})
            except Exception as e:  # noqa: BLE001
                problems.append({"edit_id": member, "error": str(e)})
        if not problems:
            record["restored_by"] = "restored"
            self.store.save(record)
        return {"batch": record["edit_id"], "restored": restored, "problems": problems,
                "note": "有些照片没能还原，请看 problems；成功的已经还原，可以再次调用补做剩下的。" if problems else "整批已还原。"}

    # ---- presets ---------------------------------------------------
    def list_presets(self, query: str = "", limit: int = 30) -> dict:
        words = str(query).split()
        data = self.call("list_presets", {"query": words[0] if words else "", "limit": max(1, min(int(limit), 50))}, timeout=60)
        if len(words) > 1:
            data["note"] = f"只用了第一个词“{words[0]}”来搜索（一次只能搜一个词）。"
        return data

    def apply_preset(self, preset_id: str, reason: str = "", photo: str = "selected") -> dict:
        self.check_writable()
        if not isinstance(preset_id, str) or not PRESET_ID.match(preset_id):
            raise EditError("preset_id 不合法：请用 lr_list_presets 返回的 id。")
        photo_id, develop = self._resolve_photo(photo)
        snap = self.call("snapshot", {"photo": photo_id}, timeout=60)
        if snap.get("has_local_adjustments"):
            raise EditError("这张照片已经有蒙版/局部调整，套预设可能影响它们且无法安全还原，已取消。")
        record = {
            "edit_id": self.store.new_id(), "kind": "set", "status": "pending", "generic": True,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"), "created_ns": _now_ns(),
            "photo_id": photo_id, "photo_name": develop["photo"].get("name"),
            "reason": str(reason)[:500], "requested": {"preset": preset_id}, "before": {},
            "snapshot": snap.get("settings", {}),  # the whole state is on disk BEFORE the preset is applied
        }
        self.store.save(record)
        try:
            data = self.call("apply_preset", {"photo": photo_id, "edit": record["edit_id"], "preset": preset_id}, timeout=60)
        except BridgeTimeout as e:
            record.update(status="uncertain", error=str(e))
            self.store.save(record)  # keep the snapshot so a timed-out write remains restorable
            raise
        except Exception as e:
            record.update(status="failed", error=str(e))
            record.pop("snapshot", None)
            self.store.save(record)
            raise
        changed = list(data.get("changed_keys", []))
        before = dict(data.get("before") or {})
        for key in changed:  # normally identical; the saved snapshot fills any gap
            if key not in before and record["snapshot"].get(key) is not None:
                before[key] = record["snapshot"][key]
        name = data.get("preset") or preset_id
        record.update(status="applied", before=before, requested={"preset": name}, preset_id=preset_id,
                      changed_keys=changed, new_keys=list(data.get("new_keys", [])), verified=bool(changed))
        record.pop("snapshot", None)
        self.store.save(record)
        rows = [{"parameter": k, "before": v.get("before"), "after": v.get("after")} for k, v in sorted((data.get("changes") or {}).items())]
        return {
            "edit_id": record["edit_id"], "photo": record["photo_name"], "photo_id": photo_id, "preset": name,
            "settings_changed": len(changed),
            "changes": rows[:MAX_PRESET_ROWS_IN_REPLY],
            "more_changes_not_shown": max(0, len(rows) - MAX_PRESET_ROWS_IN_REPLY),
            "tables_changed": list(data.get("tables_changed", [])),
            "note": ("这个预设没有改变任何设置（照片本来就是这个状态）。" if not changed else
                     "预设可能一次改动很多地方，请用 lr_get_preview 看效果。"),
            "undo": f"lr_restore(edit_id=\"{record['edit_id']}\") 写回套预设之前的设置（预设改过的全部设置，不只是它列出的）；"
                    "lr_restore_original 写回 LrMCP 第一次改这张照片之前的状态。"
                    + ("（预设新增的 " + ", ".join(record["new_keys"]) + " 是 Lightroom 自己生成的内部值，没法删除，不影响画面。）" if record["new_keys"] else ""),
        }

    def list_edits(self, photo: str = "all", limit: int = 20) -> dict:
        records = self.store.all()
        if photo != "all":
            if photo == "selected":
                photo = self.call("list_selected", {"limit": 1}, timeout=30).get("active_id") or ""
            records = [r for r in records if r.get("photo_id") == photo]
        rows = [{
            "edit_id": r["edit_id"], "kind": r.get("kind"), "status": r.get("status"), "time": r.get("created"),
            "photo": r.get("photo_name"), "reason": r.get("reason", ""),
            "parameters": r.get("requested", {}), "restored_by": r.get("restored_by"),
        } for r in reversed(records)][: max(1, min(int(limit), 100))]
        return {"count": len(rows), "edits": rows}
