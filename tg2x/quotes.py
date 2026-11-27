"""当日行情：财政部每日收益率曲线（收盘）+ 雅虎实时（10 年期）。

FRED 的日频序列要等美东收盘后几小时才更新，赶不上北京时间当天发稿；
这里补一层"当天"的口径。
"""

from __future__ import annotations

import csv
import io
import logging
import os
from datetime import date

import requests

log = logging.getLogger("tg2x")

TREASURY = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
            "daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
            "&field_tdr_date_value={year}&page&_format=csv")


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    proxy = (os.environ.get("TG2X_PROXY") or "").strip()
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    return s


def treasury_curve() -> dict:
    """返回最近一个交易日的官方曲线：{'date':..., '2 Yr':4.63, '10 Yr':4.91, ...}。"""
    year = date.today().year
    try:
        r = _session().get(TREASURY.format(year=year), timeout=25)
        if r.status_code != 200:
            return {}
        rows = list(csv.DictReader(io.StringIO(r.text)))
        if not rows:
            return {}
        rows.sort(key=lambda x: x.get("Date", ""))
        last = rows[-1]
        out = {"date": last.get("Date", "")}
        for k, v in last.items():
            if k == "Date" or not v:
                continue
            try:
                out[k.strip()] = float(v)
            except ValueError:
                continue
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("财政部曲线取数失败：%s", str(e)[:100])
        return {}


def yahoo_last(symbol: str) -> tuple[str, float] | None:
    """雅虎取最新价（如 ^TNX = 10 年期收益率 x10）。返回 (时间, 值)。"""
    import time as _time
    from urllib.parse import quote

    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='')}"
           f"?range=5d&interval=1d")
    try:
        r = _session().get(url, timeout=20)
        if r.status_code != 200:
            return None
        res = (r.json().get("chart") or {}).get("result") or []
        if not res:
            return None
        q = res[0]["indicators"]["quote"][0]
        ts = res[0]["timestamp"]
        for i in range(len(ts) - 1, -1, -1):
            v = q["close"][i]
            if v is None:
                continue
            stamp = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(ts[i]))
            return stamp, float(v)
        return None
    except Exception as e:  # noqa: BLE001
        log.debug("雅虎 %s 失败：%s", symbol, str(e)[:80])
        return None


def today_yields() -> str:
    """拼一句"当天"的收益率事实（官方收盘优先，盘中补一句）。"""
    parts = []
    curve = treasury_curve()
    if curve.get("2 Yr") is not None or curve.get("10 Yr") is not None:
        bits = []
        for k in ("2 Yr", "10 Yr", "3 Mo"):
            if k in curve:
                bits.append(f"{k} {curve[k]:.2f}%")
        parts.append(f"Treasury par yields on {curve.get('date')}: " + ", ".join(bits))
    live = yahoo_last("^TNX")
    if live:
        stamp, val = live
        parts.append(f"10-year yield {val:.2f}% on {stamp} (market quote, not the official close)")
    five = yahoo_last("^FVX")
    if five:
        parts.append(f"5-year {five[1]:.2f}%")
    thirty = yahoo_last("^TYX")
    if thirty:
        parts.append(f"30-year {thirty[1]:.2f}%")
    return ". ".join(parts) + ("." if parts else "")


def yahoo_close(symbol: str) -> tuple[str, float, float] | None:
    """最近一个交易日的收盘价和涨跌幅：(symbol, close, pct)。"""
    from urllib.parse import quote as _q

    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{_q(symbol, safe='')}"
           f"?range=5d&interval=1d")
    try:
        r = _session().get(url, timeout=20)
        if r.status_code != 200:
            return None
        res = (r.json().get("chart") or {}).get("result") or []
        if not res:
            return None
        q = res[0]["indicators"]["quote"][0]
        closes = [c for c in q["close"] if c is not None]
        if len(closes) < 2:
            return None
        pct = (closes[-1] / closes[-2] - 1) * 100
        return symbol, float(closes[-1]), float(pct)
    except Exception as e:  # noqa: BLE001
        log.debug("雅虎收盘 %s 失败：%s", symbol, str(e)[:80])
        return None
