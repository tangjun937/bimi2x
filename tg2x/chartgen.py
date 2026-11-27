"""自己画配图：把稿子里的数字变成一张横向柱状图（PIL 画，不依赖 matplotlib）。

为什么自己画：文章题图常常是楼/人像，图表又常被 iframe 包住截不到；
而稿子里的关键数字本来就有——直接画成一张干净的数据图，既"有数字"又不涉及版权。
"""

from __future__ import annotations

import io
import json
import logging
import re

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("tg2x")

W, H = 1200, 675
BG = (255, 255, 255)
INK = (20, 20, 20)
MUTED = (110, 110, 110)
BAR = (196, 62, 62)          # 主色：砖红，和 ZeroHedge / 财经图一个调子
GRID = (232, 232, 232)

_SPEC_SYSTEM = (
    "You turn a news story into ONE chart spec. Output JSON only, no prose, no code fence.\n"
    "Schema: {\"title\": str, \"subtitle\": str, \"unit\": str, "
    "\"items\": [[label, number], ...], \"source\": str}\n"
    "Rules: 3 to 8 items; all numbers share one unit (bp, %, $B, x, days); "
    "labels are short (<= 24 chars); numbers must appear in the source, never invented; "
    "title is a claim with a number in it; subtitle says what the number measures.\n"
    "If the source has fewer than three comparable numbers, output {\"skip\": true}."
)


def _font(size: int, bold: bool = False):
    names = (
        r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\segoeuib.ttf" if bold else r"C:\Windows\Fonts\segoeui.ttf",
    )
    for p in names:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def spec_from_text(chat, material: str, post: str = "") -> dict | None:
    """让模型把素材里可比的数字整理成图表规格。失败返回 None。"""
    user = f"STORY MATERIAL:\n{material[:4000]}\n\nDRAFT POST:\n{post[:1500]}"
    try:
        raw = chat(_SPEC_SYSTEM, user) or ""
    except Exception as e:  # noqa: BLE001
        log.debug("图表规格生成失败：%s", str(e)[:100])
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        spec = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(spec, dict) or spec.get("skip"):
        return None
    items = spec.get("items") or []
    clean = []
    for it in items:
        try:
            label, val = str(it[0])[:26], float(it[1])
        except Exception:
            continue
        clean.append((label, val))
    if len(clean) < 3:
        return None
    spec["items"] = clean[:8]
    return spec


def render(spec: dict) -> bytes:
    """把规格画成 PNG（1200x675）。"""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    items = sorted(spec["items"], key=lambda x: x[1], reverse=True)
    unit = str(spec.get("unit") or "")

    title = str(spec.get("title") or "")[:70]
    subtitle = str(spec.get("subtitle") or "")[:110]
    source = str(spec.get("source") or "")[:110]

    d.text((56, 40), title, font=_font(38, True), fill=INK)
    d.text((56, 92), subtitle, font=_font(21), fill=MUTED)
    d.line([(56, 136), (W - 56, 136)], fill=(220, 220, 220), width=2)

    top, bottom = 170, H - 90
    rows = len(items)
    row_h = (bottom - top) / max(rows, 1)
    label_w = 300
    plot_l, plot_r = 56 + label_w, W - 130
    vmax = max(v for _, v in items) or 1.0
    vmin = min(0.0, min(v for _, v in items))
    span = (vmax - vmin) or 1.0

    def x_of(v: float) -> float:
        return plot_l + (v - vmin) / span * (plot_r - plot_l)

    for i, (label, val) in enumerate(items):
        y0 = top + i * row_h + row_h * 0.18
        y1 = y0 + row_h * 0.64
        # 行背景条
        d.rectangle([plot_l, y0, plot_r, y1], fill=(248, 248, 248))
        x0, x1 = sorted((x_of(0), x_of(val)))
        colour = BAR if val >= 0 else (60, 130, 90)
        d.rectangle([x0, y0, x1, y1], fill=colour)
        d.text((56, y0 + row_h * 0.10), label, font=_font(22), fill=INK)
        txt = f"{val:+.0f} {unit}".strip() if val < 0 else f"{val:.0f} {unit}".strip()
        tw = d.textlength(txt, font=_font(22, True))
        tx = x1 + 12
        if tx + tw > W - 40:
            tx = max(plot_l + 8, x1 - tw - 12)
        d.text((tx, y0 + row_h * 0.10), txt, font=_font(22, True), fill=INK)

    zero_x = x_of(0)
    d.line([(zero_x, top - 8), (zero_x, bottom + 4)], fill=(180, 180, 180), width=2)
    d.text((56, H - 62), f"Source: {source}" if source else "", font=_font(18), fill=MUTED)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
