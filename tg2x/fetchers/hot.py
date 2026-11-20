"""热点抓取：不走 Telegram，直接从公开源抓「金融 / AI」热点。

来源（全部免费、无需凭证）：
  - Hacker News 首页（Algolia API，带 points）
  - Google News RSS（AI 查询 / 金融市场查询）
  - arXiv cs.AI / cs.LG 最新论文

输出统一成 TgPost（channel="hot"），message_id 由 URL/标题哈希稳定生成，
所以同一条热点第二次出现会被 store 的去重表挡住；超过保留天数的记录由
store.purge_older_than() 清掉（默认 5 天）。
"""
from __future__ import annotations

import hashlib
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import requests

from ..models import TgPost

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 只保留金融 / AI 相关（HN 首页噪音最大，必须过滤；RSS 查询本身已限定）
_RELEVANT = re.compile(
    r"\b("
    r"ai|a\.i\.|artificial intelligence|machine learning|llm|gpt|chatgpt|claude|gemini|"
    r"openai|anthropic|deepmind|deepseek|mistral|llama|copilot|agent|agents|"
    r"model|models|inference|training|gpu|nvidia|amd|tsmc|chip|chips|semiconductor|"
    r"data center|datacenter|cloud|compute|robot|robotics|autonomous|self-driving|"
    r"startup|funding|valuation|ipo|revenue|earnings|profit|margin|stocks?|shares?|"
    r"market|markets|investors?|bond|yield|fed|inflation|rate cut|hiring|layoffs?|"
    r"regulation|antitrust|acquisition|acquire|merger"
    r")\b",
    re.I,
)

_HTML_TAG = re.compile(r"<[^>]+>")


def _hid(*parts: str) -> int:
    """把 URL/标题变成稳定的整数 id（去重用，跨运行一致）。"""
    h = hashlib.sha1("|".join(p.strip().lower() for p in parts).encode("utf-8")).hexdigest()
    return int(h[:12], 16)


def _clean(text: str) -> str:
    return _HTML_TAG.sub(" ", text or "").strip()


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().isoformat(timespec="seconds")


def _age_hours(dt: datetime) -> float:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 3600


class HotFetcher:
    """把几个免费热点源合并成一个「频道」。"""

    name = "hot"
    preserve_order = True   # 已经按分数排好，不要按 message_id 重排
    skip_baseline = True    # 首次运行也要发，不建基线

    def __init__(self, timeout: int = 20, user_agent: str = "",
                 max_age_hours: int = 36, max_items: int = 40):
        self.timeout = timeout
        self.max_age_hours = max_age_hours
        self.max_items = max_items
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"User-Agent": user_agent or DEFAULT_UA})

    # ---------- 各数据源 ----------
    def _hacker_news(self) -> list[tuple[float, TgPost]]:
        out: list[tuple[float, TgPost]] = []
        try:
            r = self.session.get(
                "https://hn.algolia.com/api/v1/search",
                params={"tags": "front_page", "hitsPerPage": 60},
                timeout=self.timeout,
            )
            r.raise_for_status()
            hits = r.json().get("hits") or []
        except Exception:
            return out
        for hit in hits:
            title = _clean(hit.get("title") or "")
            if not title or not _RELEVANT.search(title):
                continue
            link = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
            points = int(hit.get("points") or 0)
            try:
                dt = datetime.fromisoformat((hit.get("created_at") or "").replace("Z", "+00:00"))
            except Exception:
                dt = datetime.now(timezone.utc)
            if _age_hours(dt) > self.max_age_hours:
                continue
            text = f"{title}\n\n{points} points on Hacker News. Source: {link}"
            out.append((40.0 + min(points, 500) / 10.0, TgPost(
                channel="hot",
                message_id=_hid(link, title),
                text=text,
                posted_at=_iso(dt),
                url=link,
                source="hackernews",
            )))
        return out

    def _google_news(self, query: str, weight: float = 30.0) -> list[tuple[float, TgPost]]:
        url = (
            "https://news.google.com/rss/search?q="
            + urllib.parse.quote(query)
            + "&hl=en-US&gl=US&ceid=US:en"
        )
        return self._rss(url, weight, "googlenews")

    def _rss(self, url: str, weight: float, source: str) -> list[tuple[float, TgPost]]:
        out: list[tuple[float, TgPost]] = []
        try:
            r = self.session.get(url, timeout=self.timeout)
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception:
            return out
        for item in root.iter("item"):
            title = _clean(item.findtext("title") or "")
            link = (item.findtext("link") or "").strip()
            if not title or not link:
                continue
            # Google News 标题尾巴带 " - 来源"，去掉
            title = re.sub(r"\s+-\s+[^-]{2,40}$", "", title).strip()
            if not _RELEVANT.search(title):
                continue
            try:
                dt = parsedate_to_datetime(item.findtext("pubDate") or "")
            except Exception:
                dt = datetime.now(timezone.utc)
            if _age_hours(dt) > self.max_age_hours:
                continue
            desc = _clean(item.findtext("description") or "")
            desc = re.sub(r"^\s*" + re.escape(title) + r"\s*", "", desc).strip()
            text = f"{title}\n\n{desc}\n\nSource: {link}"
            out.append((weight + max(0.0, 12.0 - _age_hours(dt)) / 2.0, TgPost(
                channel="hot",
                message_id=_hid(link, title),
                text=text,
                posted_at=_iso(dt),
                url=link,
                source=source,
            )))
        return out

    def _arxiv(self) -> list[tuple[float, TgPost]]:
        out: list[tuple[float, TgPost]] = []
        ns = {"a": "http://www.w3.org/2005/Atom"}
        try:
            r = self.session.get(
                "http://export.arxiv.org/api/query"
                "?search_query=cat:cs.AI+OR+cat:cs.LG+OR+cat:cs.CL"
                "&sortBy=submittedDate&sortOrder=descending&max_results=15",
                timeout=self.timeout,
            )
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception:
            return out
        for entry in root.findall("a:entry", ns):
            title = _clean((entry.findtext("a:title", "", ns) or "").replace("\n", " "))
            summary = _clean((entry.findtext("a:summary", "", ns) or "").replace("\n", " "))
            link = (entry.findtext("a:id", "", ns) or "").strip()
            if not title or not link:
                continue
            try:
                dt = datetime.fromisoformat((entry.findtext("a:published", "", ns) or "").replace("Z", "+00:00"))
            except Exception:
                dt = datetime.now(timezone.utc)
            if _age_hours(dt) > self.max_age_hours:
                continue
            text = f"{title}\n\n{summary[:400]}\n\nSource: {link}"
            out.append((20.0 + max(0.0, 24.0 - _age_hours(dt)) / 4.0, TgPost(
                channel="hot",
                message_id=_hid(link, title),
                text=text,
                posted_at=_iso(dt),
                url=link,
                source="arxiv",
            )))
        return out

    # ---------- 统一入口 ----------
    def fetch(self, limit: int = 30) -> list[TgPost]:
        hn = self._hacker_news()
        google = (
            self._google_news("artificial intelligence OR AI OR OpenAI OR Nvidia when:1d", 30.0)
            + self._google_news("stock market OR earnings OR Federal Reserve when:1d", 26.0)
        )
        techcrunch = self._rss(
            "https://techcrunch.com/category/artificial-intelligence/feed/", 24.0, "techcrunch"
        )
        arxiv = sorted(self._arxiv(), key=lambda x: x[0], reverse=True)[:8]

        pool: list[tuple[float, TgPost]] = hn + google + techcrunch + arxiv

        seen: set[int] = set()
        picked: list[TgPost] = []
        for _score, post in sorted(pool, key=lambda x: x[0], reverse=True):
            if post.message_id in seen:
                continue
            seen.add(post.message_id)
            picked.append(post)
            if len(picked) >= max(limit, self.max_items):
                break
        return picked
