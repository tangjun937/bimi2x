"""缠论日线速读：抓一只票的日线 → 找分型/笔/中枢 → 画带数字的图 → 给出可写的结论。

口径（按 E:\\bian\\quant\\docs\\06-缠论 的简化版）：
  · 分型：三根 K 线，顶分型 high 最高、底分型 low 最低
  · 笔：顶↔底交替，端点间隔 ≥4 根；同类分型取更极端的一个
  · 中枢：最近三笔的重叠区间 [ZD, ZG]
  · 三买/三卖：价格离开中枢后回抽不回中枢
图形：K 线 + 笔（折线）+ 中枢（方块）+ 最新价/区间/信号标注。
"""

from __future__ import annotations

import io
import json
import logging
import os
import random
import time
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("tg2x")

HOT = ("NVDA", "AMD", "AVGO", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "ORCL",
       "MU", "PLTR", "CRWV", "SMCI", "MRVL", "TSM")

W, H = 1200, 675
BG, INK, MUTED = (255, 255, 255), (25, 25, 25), (115, 115, 115)
UP, DOWN = (200, 70, 70), (60, 140, 95)
CENTRE = (90, 110, 190)


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    proxy = (os.environ.get("TG2X_PROXY") or "").strip()
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    return s


def _font(size: int, bold: bool = False):
    for p in (r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
              r"C:\Windows\Fonts\segoeuib.ttf" if bold else r"C:\Windows\Fonts\segoeui.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


CACHE = Path(__file__).resolve().parent.parent / "data" / "chan_cache"


def _cache_get(symbol: str) -> list[dict] | None:
    try:
        fp = CACHE / f"{symbol.upper()}.json"
        if not fp.exists():
            return None
        d = json.loads(fp.read_text(encoding="utf-8"))
        if d.get("date") != time.strftime("%Y-%m-%d"):
            return None
        return d.get("bars") or None
    except Exception:  # noqa: BLE001
        return None


def _cache_put(symbol: str, bs: list[dict]) -> None:
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        (CACHE / f"{symbol.upper()}.json").write_text(
            json.dumps({"date": time.strftime("%Y-%m-%d"), "bars": bs}), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _yahoo(symbol: str, rng: str, interval: str) -> list[dict]:
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?range={rng}&interval={interval}")
    for attempt in range(2):
        try:
            r = _session().get(url, timeout=25)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code != 200:
                return []
            res = (r.json().get("chart") or {}).get("result") or []
            if not res:
                return []
            q = res[0]["indicators"]["quote"][0]
            out = []
            for k, t in enumerate(res[0]["timestamp"]):
                try:
                    o, h, l, c = q["open"][k], q["high"][k], q["low"][k], q["close"][k]
                except (KeyError, IndexError):
                    continue
                if None in (o, h, l, c):
                    continue
                out.append({"t": t, "o": o, "h": h, "l": l, "c": c})
            return out
        except Exception as e:  # noqa: BLE001
            log.debug("Yahoo 失败 %s: %s", symbol, str(e)[:80])
            time.sleep(1.5)
    return []


def _stooq(symbol: str) -> list[dict]:
    """Stooq 免费日线（美股用 xxx.us）。"""
    url = f"https://stooq.com/q/d/l/?s={symbol.lower()}.us&i=d"
    try:
        r = _session().get(url, timeout=25)
        if r.status_code != 200 or "Date,Open" not in r.text[:200]:
            return []
        out = []
        for line in r.text.strip().splitlines()[1:]:
            f = line.split(",")
            if len(f) < 5:
                continue
            try:
                o, h, l, c = (float(f[1]), float(f[2]), float(f[3]), float(f[4]))
            except ValueError:
                continue
            out.append({"t": f[0], "o": o, "h": h, "l": l, "c": c})
        return out[-260:]
    except Exception as e:  # noqa: BLE001
        log.debug("Stooq 失败 %s: %s", symbol, str(e)[:80])
        return []


def bars(symbol: str, rng: str = "1y", interval: str = "1d") -> list[dict]:
    """日线：先 Yahoo，拿不到就用 Stooq。"""
    cached = _cache_get(symbol)
    if cached:
        return cached
    out = _yahoo(symbol, rng, interval)
    if len(out) >= 60:
        _cache_put(symbol, out)
        return out
    alt = _stooq(symbol)
    if len(alt) > len(out):
        log.info("Yahoo 拿不到 %s 的日线，改用 Stooq（%d 根）", symbol, len(alt))
        _cache_put(symbol, alt)
        return alt
    return out


def _fractals(bs: list[dict]) -> list[dict]:
    out = []
    for i in range(1, len(bs) - 1):
        a, b, c = bs[i - 1], bs[i], bs[i + 1]
        if b["h"] > a["h"] and b["h"] > c["h"]:
            out.append({"i": i, "kind": "top", "price": b["h"]})
        if b["l"] < a["l"] and b["l"] < c["l"]:
            out.append({"i": i, "kind": "bot", "price": b["l"]})
    return out


def _strokes(fs: list[dict], min_gap: int = 4) -> list[dict]:
    """顶底交替、间隔 ≥min_gap 的笔。同类分型取更极端的。"""
    picked: list[dict] = []
    for f in fs:
        if not picked:
            picked.append(f)
            continue
        last = picked[-1]
        if f["kind"] == last["kind"]:
            better = f["price"] > last["price"] if f["kind"] == "top" else f["price"] < last["price"]
            if better:
                picked[-1] = f
            continue
        if f["i"] - last["i"] >= min_gap:
            picked.append(f)
    return [{"a": picked[i], "b": picked[i + 1],
             "up": picked[i + 1]["price"] > picked[i]["price"]}
            for i in range(len(picked) - 1)]


def _centre(strokes: list[dict]) -> tuple[float, float] | None:
    """最近三笔的重叠区间；没有就往前找，直到找到为止。"""
    for end in range(len(strokes), 2, -1):
        seg = strokes[end - 3:end]
        lows = [min(s["a"]["price"], s["b"]["price"]) for s in seg]
        highs = [max(s["a"]["price"], s["b"]["price"]) for s in seg]
        zd, zg = max(lows), min(highs)
        if zd < zg:
            return zd, zg
    return None


def analyze(symbol: str) -> dict | None:
    bs = bars(symbol)
    if len(bs) < 60:
        return None
    fs = _fractals(bs)
    st = _strokes(fs)
    if len(st) < 3:
        return None
    z = _centre(st)
    last = bs[-1]
    price = last["c"]
    lo = min(b["l"] for b in bs[-60:])
    hi = max(b["h"] for b in bs[-60:])
    up_strokes = [s for s in st if s["up"]]
    dn_strokes = [s for s in st if not s["up"]]
    last_up = up_strokes[-1] if up_strokes else None
    last_dn = dn_strokes[-1] if dn_strokes else None
    signal = "no centre"
    if z:
        zd, zg = z
        if price > zg:
            signal = "above centre (buy-3 candidate)"
        elif price < zd:
            signal = "below centre (sell-3 candidate)"
        else:
            signal = "inside centre (range)"
    return {
        "symbol": symbol, "bars": bs, "strokes": st, "centre": z,
        "price": price, "low60": lo, "high60": hi,
        "last_up": (last_up["a"]["price"], last_up["b"]["price"]) if last_up else None,
        "last_dn": (last_dn["a"]["price"], last_dn["b"]["price"]) if last_dn else None,
        "signal": signal,
        "chg_1d": (price / bs[-2]["c"] - 1) * 100 if len(bs) > 1 else 0.0,
    }


def render(a: dict, lookback: int = 90) -> bytes:
    bs = a["bars"][-lookback:]
    t0 = len(a["bars"]) - len(bs)
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    d.text((56, 34), f"{a['symbol']} daily · Chan read", font=_font(34, True), fill=INK)
    chg = a["chg_1d"]
    d.text((56, 78), f"last {a['price']:.2f}  ({chg:+.2f}% on the day)   "
                     f"60-bar range {a['low60']:.2f} – {a['high60']:.2f}",
           font=_font(20), fill=MUTED)

    left, right, top, bottom = 70, W - 170, 120, H - 70
    lo = min(b["l"] for b in bs)
    hi = max(b["h"] for b in bs)
    pad = (hi - lo) * 0.08 or 1
    lo, hi = lo - pad, hi + pad

    def y_of(p: float) -> float:
        return bottom - (p - lo) / (hi - lo) * (bottom - top)

    def x_of(i: int) -> float:
        return left + i / max(len(bs) - 1, 1) * (right - left)

    # 网格 + 右侧价格
    for k in range(5):
        p = lo + (hi - lo) * k / 4
        y = y_of(p)
        d.line([(left, y), (right, y)], fill=(238, 238, 238), width=1)
        d.text((right + 10, y - 9), f"{p:.0f}", font=_font(16), fill=MUTED)

    # K 线
    step = max((right - left) / max(len(bs), 1), 2)
    for i, b in enumerate(bs):
        x = x_of(i)
        colour = UP if b["c"] >= b["o"] else DOWN
        d.line([(x, y_of(b["h"])), (x, y_of(b["l"]))], fill=colour, width=2)
        d.rectangle([x - step * 0.3, y_of(max(b["o"], b["c"])),
                     x + step * 0.3, y_of(min(b["o"], b["c"]))], fill=colour)

    # 笔
    pts = []
    for s in a["strokes"]:
        i = s["a"]["i"] - t0
        j = s["b"]["i"] - t0
        if 0 <= i < len(bs) and 0 <= j < len(bs):
            pts.append((x_of(i), y_of(s["a"]["price"])))
            pts.append((x_of(j), y_of(s["b"]["price"])))
    if len(pts) >= 2:
        d.line(pts, fill=(40, 40, 40), width=3)

    # 中枢
    z = a["centre"]
    if z:
        zd, zg = z
        last_i = len(bs) - 1
        first = max(0, last_i - 30)
        d.rectangle([x_of(first), y_of(zg), x_of(last_i), y_of(zd)],
                    outline=CENTRE, width=2)
        d.text((x_of(first) + 6, y_of(zg) - 24),
               f"centre {zd:.2f}–{zg:.2f}", font=_font(18, True), fill=CENTRE)

    d.text((56, H - 46), f"signal: {a['signal']}   ·   source: Yahoo Finance daily bars",
           font=_font(19), fill=MUTED)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def material(a: dict) -> str:
    """给 writer 的结构化素材（都是图上的数字）。"""
    z = a["centre"]
    parts = [
        f"{a['symbol']} closed at {a['price']:.2f}, {a['chg_1d']:+.2f}% on the day.",
        f"60-session range {a['low60']:.2f} to {a['high60']:.2f}.",
    ]
    if z:
        parts.append(f"The last three strokes overlap between {z[0]:.2f} and {z[1]:.2f} "
                     f"(the centre), and price is {a['signal']}.")
    if a["last_up"]:
        parts.append(f"Last up stroke: {a['last_up'][0]:.2f} to {a['last_up'][1]:.2f}.")
    if a["last_dn"]:
        parts.append(f"Last down stroke: {a['last_dn'][0]:.2f} to {a['last_dn'][1]:.2f}.")
    parts.append(f"{len(a['strokes'])} strokes mapped on the daily chart.")
    return " ".join(parts)


def pick_symbol(avoid: tuple[str, ...] = ()) -> str:
    pool = [s for s in HOT if s not in avoid] or list(HOT)
    return random.choice(pool)
