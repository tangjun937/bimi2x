"""FRED 宏观数据：拉序列 → 画折线图（带最新值/变化/日期）→ 供给写作。

FRED 的官方图（fredgraph.png）在这台机器上超时，所以只取数据、自己画。
密钥从 .env 的 FRED_API_KEY 读。
"""

from __future__ import annotations

import io
import logging
import os
from datetime import datetime

import requests
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("tg2x")

API = "https://api.stlouisfed.org/fred/series/observations"
W, H = 1200, 675

# 关键词 → (series_id, 展示名, 单位)
SERIES = {
    "hy oas": ("BAMLH0A0HYM2", "US high-yield credit spread", "%"),
    "credit spread": ("BAMLH0A0HYM2", "US high-yield credit spread", "%"),
    "spread": ("BAMLH0A0HYM2", "US high-yield credit spread", "%"),
    "investment grade": ("BAMLC0A0CM", "US investment-grade spread", "%"),
    "m2": ("M2SL", "US M2 money stock", "$B"),
    "money supply": ("M2SL", "US M2 money stock", "$B"),
    "10-year": ("DGS10", "10-year Treasury yield", "%"),
    "10 year": ("DGS10", "10-year Treasury yield", "%"),
    "yield": ("DGS10", "10-year Treasury yield", "%"),
    "curve": ("T10Y2Y", "10y-2y Treasury spread", "%"),
    "inflation": ("CPIAUCSL", "US CPI", "index"),
    "cpi": ("CPIAUCSL", "US CPI", "index"),
    "unemployment": ("UNRATE", "US unemployment rate", "%"),
    "fed balance sheet": ("WALCL", "Fed balance sheet", "$M"),
    "balance sheet": ("WALCL", "Fed balance sheet", "$M"),
    "fed funds": ("FEDFUNDS", "Effective fed funds rate", "%"),
    "rate hike": ("FEDFUNDS", "Effective fed funds rate", "%"),
    "capex": ("PNFIC1", "Private nonresidential investment", "$B"),
}


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    proxy = (os.environ.get("TG2X_PROXY") or "").strip()
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    return s


def _key() -> str:
    return (os.environ.get("FRED_API_KEY") or "").strip()


def series(sid: str, start: str = "2015-01-01", limit: int = 100000) -> list[tuple[str, float]]:
    """取一组观测，返回 [(date, value)]。失败返回空。"""
    if not _key():
        log.info("没有 FRED_API_KEY，跳过 FRED 数据")
        return []
    params = {"series_id": sid, "api_key": _key(), "file_type": "json",
              "observation_start": start, "sort_order": "asc", "limit": limit}
    try:
        r = _session().get(API, params=params, timeout=25)
        if r.status_code != 200:
            log.warning("FRED %s HTTP %s", sid, r.status_code)
            return []
        out = []
        for o in r.json().get("observations") or []:
            v = o.get("value")
            if v in (".", "", None):
                continue
            try:
                out.append((o["date"], float(v)))
            except (KeyError, ValueError):
                continue
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("FRED %s 失败：%s", sid, str(e)[:100])
        return []


def pick(text: str) -> tuple[str, str, str] | None:
    """按关键词挑一个序列。返回 (sid, 展示名, 单位)。"""
    low = (text or "").lower()
    for kw, meta in SERIES.items():
        if kw in low:
            return meta
    return None


def fact(sid: str, name: str, unit: str) -> str:
    """给 writer 的一句话事实（最新值、变化、日期）。"""
    obs = series(sid)
    if len(obs) < 2:
        return ""
    (d0, v0), (d1, v1) = obs[-2], obs[-1]
    chg = v1 - v0
    year_ago = obs[max(0, len(obs) - 13)]
    yoy = v1 - year_ago[1]
    return (f"{name} was {v1:.2f} {unit} on {d1} (prior reading {d0}: {v0:.2f}; "
            f"change {chg:+.2f}; 12 months earlier {year_ago[1]:.2f}, delta {yoy:+.2f}).")


def _font(size: int, bold: bool = False):
    for p in (r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
              r"C:\Windows\Fonts\segoeuib.ttf" if bold else r"C:\Windows\Fonts\segoeui.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def render(sid: str, name: str, unit: str, years: int = 5) -> bytes:
    """画一张带数值标注的折线图。"""
    obs = series(sid, start=f"{datetime.now().year - years}-01-01")
    if len(obs) < 10:
        return b""
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    last_d, last_v = obs[-1]
    first_v = obs[0][1]
    d.text((56, 34), f"{name} · {unit}", font=_font(34, True), fill=(25, 25, 25))
    chg = last_v - obs[-2][1]
    d.text((56, 78), f"latest {last_v:.2f} {unit} on {last_d}   "
                     f"({chg:+.2f} vs prior)   {years}y range "
                     f"{min(v for _, v in obs):.2f}–{max(v for _, v in obs):.2f}",
           font=_font(20), fill=(110, 110, 110))

    left, right, top, bottom = 80, W - 160, 130, H - 80
    lo = min(v for _, v in obs)
    hi = max(v for _, v in obs)
    pad = (hi - lo) * 0.1 or 1
    lo, hi = lo - pad, hi + pad

    def y_of(v: float) -> float:
        return bottom - (v - lo) / (hi - lo) * (bottom - top)

    def x_of(i: int) -> float:
        return left + i / max(len(obs) - 1, 1) * (right - left)

    for k in range(5):
        v = lo + (hi - lo) * k / 4
        y = y_of(v)
        d.line([(left, y), (right, y)], fill=(236, 236, 236), width=1)
        d.text((right + 10, y - 9), f"{v:.2f}", font=_font(16), fill=(120, 120, 120))

    d.line([(x_of(i), y_of(v)) for i, (_, v) in enumerate(obs)], fill=(190, 60, 60), width=3)
    d.ellipse([x_of(len(obs) - 1) - 6, y_of(last_v) - 6,
               x_of(len(obs) - 1) + 6, y_of(last_v) + 6], fill=(190, 60, 60))
    d.text((56, H - 46), f"Source: FRED, series {sid} ({obs[0][0]} to {obs[-1][0]})",
           font=_font(18), fill=(120, 120, 120))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
