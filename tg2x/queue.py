"""到点发稿的队列：文本稿件按时间排好，由计划任务每 10 分钟检查一次。

队列文件：data/queue.json
每条：{due, kind, text, chart, done}
  kind = "text"         直接发 text（可选 chart 指定本地 PNG）
  kind = "market_close" 到点用当天收盘价现写一条，并画个股涨跌图
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

log = logging.getLogger("tg2x")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUEUE = os.path.join(ROOT, "data", "queue.json")
TZ = timezone(timedelta(hours=8))


def load() -> list[dict]:
    try:
        return json.load(open(QUEUE, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []


def save(items: list[dict]) -> None:
    os.makedirs(os.path.dirname(QUEUE), exist_ok=True)
    json.dump(items, open(QUEUE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def pending() -> list[dict]:
    return [x for x in load() if not x.get("done")]


def has_pending_within(hours: int = 24) -> bool:
    """未来 N 小时内有待发稿（用于让常规流程让路）。"""
    now = datetime.now(TZ)
    for x in pending():
        try:
            due = datetime.fromisoformat(x["due"])
        except Exception:  # noqa: BLE001
            continue
        if due.tzinfo is None:
            due = due.replace(tzinfo=TZ)
        if now - timedelta(minutes=30) <= due <= now + timedelta(hours=hours):
            return True
    return False


def _publish(text: str, chart: str = "") -> tuple[bool, str]:
    from . import config as cfgmod, media
    from .models import TgPost, TransPost
    from .publisher import Publisher

    cfg = cfgmod.load(os.path.join(ROOT, "config.yaml"))
    publisher = Publisher(cfg)
    post = TgPost(channel="hot", message_id=int(datetime.now(TZ).timestamp()), text="", url="")
    if chart and os.path.exists(chart):
        try:
            pub = media.upload_bytes(open(chart, "rb").read(), os.path.splitext(chart)[1] or ".png",
                                     dedupe=False)
            if pub:
                post.media_urls = [pub]
        except Exception as e:  # noqa: BLE001
            log.warning("队列配图上传失败：%s", str(e)[:100])
    item = TransPost(post=post, text_en=text)
    item.final_text = publisher.build_text(item)
    res = publisher.publish(item)
    return res.ok, (res.tweet_id or res.detail or "")


def _market_close() -> tuple[str, str]:
    """用当天收盘价写一条，并画个股涨跌柱状图。"""
    from . import chartgen
    from .config import load
    from .writer import NewsWriter

    from . import chan

    rows = []
    for sym in ("NVDA", "MU", "ORCL", "AVGO", "CRWV"):
        try:
            b = chan.bars(sym, rng="6mo")
        except Exception:  # noqa: BLE001
            b = []
        if len(b) >= 2:
            close, prev = b[-1]["c"], b[-2]["c"]
            rows.append((sym, close, (close / prev - 1) * 100))
    if len(rows) < 3:
        return "", ""
    lines = "; ".join(f"{s} closed at {p:.2f} ({c:+.2f}%)" for s, p, c in rows)
    mat = ("US semiconductor and AI-infrastructure closes for the session: " + lines +
           ". Use these exact numbers; do not add any other figure.")
    writer = NewsWriter(load(os.path.join(ROOT, "config.yaml")))
    text = writer.write(mat, has_image=True)
    if not text:
        return "", ""
    spec = {"title": "AI hardware at the close",
            "subtitle": "One-day move, percent",
            "unit": "%",
            "items": [[s, round(c, 2)] for s, _p, c in rows],
            "source": "Yahoo Finance close"}
    chart = os.path.join(ROOT, "data", f"close_{datetime.now(TZ):%Y%m%d}.png")
    open(chart, "wb").write(chartgen.render(spec))
    return text, chart


def post_due() -> int:
    """发掉到点的稿子，返回到点数量。"""
    now = datetime.now(TZ)
    items = load()
    sent = 0
    for x in items:
        if x.get("done"):
            continue
        try:
            due = datetime.fromisoformat(x["due"])
        except Exception:  # noqa: BLE001
            continue
        if due.tzinfo is None:
            due = due.replace(tzinfo=TZ)
        if due > now:
            continue
        text = x.get("text", "")
        chart = x.get("chart", "")
        if x.get("kind") == "market_close":
            text, chart = _market_close()
            if not text:
                log.warning("收盘稿生成失败，留到下轮")
                continue
        ok, info = _publish(text, chart)
        log.info("队列发布 %s -> %s %s", x.get("due"), ok, info)
        if ok:
            x["done"] = True
            x["tweet"] = info
            sent += 1
    if sent:
        save(items)
    return sent


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("tg2x").setLevel(logging.INFO)
    print("posted:", post_due())
