"""图表池：把对标账号（@RyanDetrick / @charliebilello / @LizAnnSonders …）带图的帖子
收进本地队列，发帖时优先挂一张跟话题对得上的图表，拿不到再退回文章自己的 og:image。

为什么要有"队列"：X 的 timeline 接口在这台机器上一直 429，扫不了账号主页；
所以发现靠 agent 的 `x_search`（拿到帖子 id），取图靠 `cdn.syndication.twimg.com`。

队列文件：data/chart_queue.json
一条记录：{id, author, text, created, image, fetched_at}
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import requests

log = logging.getLogger("tg2x")

QUEUE = Path(__file__).resolve().parent.parent / "data" / "chart_queue.json"
MAX_ITEMS = 80
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 话题词：两边都出现才算"对得上"
_TOPICS = (
    "ai", "artificial intelligence", "capex", "capital spending", "chips", "chip",
    "semiconductor", "semis", "gpu", "hbm", "data center", "datacenter", "hyperscaler",
    "earnings", "revenue", "guidance", "valuation", "multiple", "bubble", "selloff",
    "rally", "correction", "rates", "cpi", "inflation", "nvidia", "amd", "broadcom",
    "tsmc", "micron", "microsoft", "google", "alphabet", "amazon", "meta", "openai",
    "anthropic", "tesla", "oracle", "s&p", "nasdaq",
)
_TICKER = re.compile(r"\$([A-Z]{1,5})\b")


def _session() -> requests.Session:
    import os
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": UA})
    proxy = (os.environ.get("TG2X_PROXY") or "").strip()
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    return s


def load() -> list[dict]:
    try:
        data = json.loads(QUEUE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def save(items: list[dict]) -> None:
    QUEUE.parent.mkdir(parents=True, exist_ok=True)
    QUEUE.write_text(json.dumps(items[:MAX_ITEMS], ensure_ascii=False, indent=1),
                     encoding="utf-8")


def fetch_tweet(tweet_id: str, timeout: int = 25) -> dict | None:
    """用公开的 syndication 接口取一条推文（含图片地址），不需要 token。"""
    try:
        r = _session().get(
            "https://cdn.syndication.twimg.com/tweet-result",
            params={"id": tweet_id, "lang": "en", "token": "x"}, timeout=timeout)
        if r.status_code != 200:
            log.warning("取推文 %s 失败：HTTP %s", tweet_id, r.status_code)
            return None
        j = r.json() or {}
        media = j.get("mediaDetails") or (j.get("entities") or {}).get("media") or []
        photo = thumb = video = ""
        for m in media:
            mt = (m.get("type") or "").lower()
            if mt == "photo" and not photo:
                photo = m.get("media_url_https") or ""
            elif mt in ("video", "animated_gif") and not video:
                thumb = m.get("media_url_https") or ""
                info = m.get("video_info") or {}
                variants = [v for v in (info.get("variants") or [])
                            if (v.get("content_type") or "") == "video/mp4" and v.get("url")]
                variants.sort(key=lambda v: v.get("bitrate") or 0)
                if variants:
                    video = variants[-1]["url"]
        if not (photo or video):
            return None
        user = (j.get("user") or {}).get("screen_name") or ""
        return {"id": str(tweet_id), "author": user, "text": (j.get("text") or "").strip(),
                "created": j.get("created_at") or "",
                "image": photo or thumb, "video": video,
                "kind": "video" if video else "photo",
                "fetched_at": int(time.time())}
    except Exception as e:  # noqa: BLE001
        log.warning("取推文 %s 异常：%s", tweet_id, str(e)[:120])
        return None


def harvest(tweet_ids: list[str]) -> dict:
    """把若干推文 id 收进队列（去重）。返回统计。"""
    items = load()
    have = {it.get("id") for it in items}
    added = 0
    for tid in tweet_ids:
        tid = str(tid).strip()
        if not tid or tid in have:
            continue
        rec = fetch_tweet(tid)
        if not rec:
            continue
        items.insert(0, rec)
        have.add(tid)
        added += 1
    items.sort(key=lambda it: it.get("fetched_at") or 0, reverse=True)
    save(items)
    return {"added": added, "total": len(items)}


def _keywords(text: str) -> set[str]:
    low = (text or "").lower()
    return {w for w in _TOPICS if w in low}


def _tickers(text: str) -> set[str]:
    return {m.group(1) for m in _TICKER.finditer(text or "")}


def score(post_text: str, item: dict) -> int:
    """话题重合度：同代码 +3，同主题词 +1（至少一个才算匹配）。"""
    pt, it = post_text or "", item.get("text") or ""
    s = 3 * len(_tickers(pt) & _tickers(it))
    s += len(_keywords(pt) & _keywords(it))
    return s


def used_recently(item: dict, days: int = 7) -> bool:
    ts = item.get("used_at") or 0
    return bool(ts) and (time.time() - ts) < days * 86400


def mark_used(tweet_id: str) -> None:
    """挂出去之后记一笔，避免下一轮又挑同一张。"""
    items = load()
    for it in items:
        if it.get("id") == str(tweet_id):
            it["used_at"] = int(time.time())
    save(items)


def pick(post_text: str, min_score: int = 2) -> dict | None:
    """挑一张跟这条稿子最对得上的图表；没有够格的返回 None。"""
    items = [it for it in load() if not used_recently(it)]
    if not items:
        return None
    ranked = sorted(items, key=lambda it: (score(post_text, it),
                                           1 if it.get("video") else 0,
                                           it.get("fetched_at") or 0),
                    reverse=True)
    best = ranked[0]
    if score(post_text, best) < min_score:
        return None
    return best
