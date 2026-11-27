"""半导体供给侧与公司口径的两个来源：

- TrendForce 新闻中心：HBM/DRAM 合约价、交期、产能、封测——"瓶颈"那一层
- Motley Fool 财报电话会纪要：capex、guidance、毛利、订单——"公司数字"那一层

只做「列表 → 正文」，数字交给写作层去用。
"""

from __future__ import annotations

import logging
import os
import re

import requests

log = logging.getLogger("tg2x")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    proxy = (os.environ.get("TG2X_PROXY") or "").strip()
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    return s


def _clean(html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    paras = re.findall(r"<p[^>]*>(.*?)</p>", text, flags=re.S | re.I)
    out = []
    for p in paras:
        t = re.sub(r"<[^>]+>", " ", p)
        t = re.sub(r"\s+", " ", t).strip()
        if len(t) < 70:
            continue
        out.append(t)
    return "\n\n".join(out)


def _numbers(text: str) -> list[str]:
    return re.findall(r"\b\d+(?:\.\d+)?\s?(?:%|percent|GB|TB|PB|nm|weeks?|months?|billion|million)\b",
                      text or "", re.I)


def trendforce(limit: int = 5) -> list[dict]:
    """TrendForce 新闻中心最新几条（带 URL 和正文）。"""
    s = _session()
    try:
        r = s.get("https://www.trendforce.com/news/", timeout=25)
        if r.status_code != 200:
            return []
        links = re.findall(r'href="(https://www\.trendforce\.com/news/20\d\d/\d\d/\d\d/[^"]+)"', r.text)
        links = list(dict.fromkeys(links))[:limit]
        out = []
        for u in links:
            a = s.get(u, timeout=25)
            if a.status_code != 200:
                continue
            body = _clean(a.text)
            title = re.search(r"<title>(.*?)</title>", a.text, re.S)
            out.append({"source": "trendforce", "url": u,
                        "title": (title.group(1).strip() if title else u.rsplit("/", 1)[-1]),
                        "text": body, "numbers": _numbers(body)})
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("TrendForce 抓取失败：%s", str(e)[:100])
        return []


def transcripts(limit: int = 5) -> list[dict]:
    """Motley Fool 最新财报电话会纪要。"""
    s = _session()
    try:
        r = s.get("https://www.fool.com/earnings-call-transcripts/", timeout=25)
        if r.status_code != 200:
            return []
        links = re.findall(r'href="(/earnings/call-transcripts/20\d\d/\d\d/\d\d/[^"]+)"', r.text)
        links = list(dict.fromkeys(links))[:limit]
        out = []
        for u in links:
            a = s.get("https://www.fool.com" + u, timeout=25)
            if a.status_code != 200:
                continue
            body = _clean(a.text)
            title = re.search(r"<title>(.*?)</title>", a.text, re.S)
            out.append({"source": "transcript", "url": "https://www.fool.com" + u,
                        "title": (title.group(1).strip() if title else u.rsplit("/", 1)[-1]),
                        "text": body, "numbers": _numbers(body)})
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("纪要抓取失败：%s", str(e)[:100])
        return []
