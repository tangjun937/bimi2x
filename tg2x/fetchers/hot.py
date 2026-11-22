"""热点抓取：不走 Telegram，直接从公开源抓「金融 / AI」热点。

来源（全部免费、无需凭证）：
  - Hacker News 首页（Algolia API，带 points）
  - Google News RSS：7 组查询，覆盖芯片算力 / 大厂 capex / 财报指引 /
    AI 融资估值 / 宏观→AI / 板块异动 / 人物表态，不再只盯 Nvidia
  - TechCrunch AI RSS

`_arxiv()` 的代码先留着，但 fetch() 已不调用：纯论文不符合「AI × 钱」口径。

输出统一成 TgPost（channel="hot"），message_id 由 URL/标题哈希稳定生成，
所以同一条热点第二次出现会被 store 的去重表挡住；超过保留天数的记录由
store.purge_older_than() 清掉（默认 5 天）。
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import requests

from ..models import TgPost
from ..market_clock import node_bonus

log = logging.getLogger("tg2x")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 选题口径：**AI × 钱**。必须同时满足「AI 相关」和「金融/商业相关」，
# 或者是马斯克/特朗普这类重要人物对 AI 的表态。纯论文、纯产品发布不要。
_AI_RE = re.compile(
    r"\b("
    r"ai|a\.i\.|artificial intelligence|machine learning|llm|gpt|chatgpt|claude|gemini|"
    r"openai|anthropic|deepmind|deepseek|mistral|llama|copilot|agentic|agent|agents|"
    r"model|models|inference|training|gpu|nvidia|amd|tsmc|chip|chips|semiconductors?|"
    r"data cent(?:er|re)|datacenter|cloud|compute|robotics?|autonomous|self-driving|"
    r"copilot|chatbot|palantir|coreweave|broadcom|micron|marvell|qualcomm|intel|"
    r"supermicro|smci|oracle|salesforce|servicenow|snowflake|databricks|perplexity|"
    r"mistral|neocloud|hbm|foundry|wafer"
    r")\b",
    re.I,
)

_MONEY_RE = re.compile(
    r"\b("
    r"stock|stocks|shares?|share price|market cap|valuation|valued|ipo|listing|"
    r"funding|raise[sd]?|investment|investors?|revenue|earnings|profit|margin|"
    r"billion|million|trillion|price|pricing|cost|costs|spend|spending|capex|"
    r"deal|contract|acquisition|acquire|merger|buyout|layoffs?|hiring|jobs|"
    r"tariff|tariffs|subsid(?:y|ies)|antitrust|regulat(?:ion|e|or)|lawsuit|fine|"
    r"bond|yields?|fed|inflation|rate cut|nasdaq|s&p|sell-?off|rally|bubble|"
    r"budget|debt|fund|hedge|bank|banks|broker|analyst|forecast|guidance"
    r")\b",
    re.I,
)

# 重要人物表态也收：他们的观点能带出钱和政策
_PERSON_RE = re.compile(
    r"\b(musk|trump|altman|jensen huang|huang|zuckerberg|nadella|pichai|thiel|"
    r"bessent|lutnick|vance|sacks|cook|ellison|oracle|softbank|masayoshi son)\b",
    re.I,
)


def _relevant(text: str) -> bool:
    if not text:
        return False
    if not _AI_RE.search(text):
        return False
    return bool(_MONEY_RE.search(text) or _PERSON_RE.search(text))


# 人物权重：总统 > 财团主理人 > 公司本身 > 其他政客
# 「财团主理人」按影响力算，不按头衔：创始人即使离职（Sutskever / Murati / Karpathy）
# 仍算该公司最重要的人；AI 团队创始人没有个人财富也算主理人。
_PERSON_T1 = re.compile(
    r"\b(president\s+(?:trump|donald|biden|vance)|the president|"
    r"trump|white house|oval office)\b",
    re.I,
)
_PERSON_T2 = re.compile(
    r"\b(musk|altman|jensen huang|lisa su|zuckerberg|nadella|pichai|bezos|thiel|"
    r"dimon|fink|masayoshi son|ellison|cook|amodei|hassabis|sutskever|murati|karpathy|"
    r"brockman|lecun|fei-fei li|andrew ng|mensch|srinivasan|alexandr wang|suleyman|"
    r"delangue|mostaque|lutnick|bessent)\b",
    re.I,
)
# 公司本身也算「财团」：新闻里没有点名主理人，但事是这家公司的，同样重要
_COMPANY_T2 = re.compile(
    r"\b(openai|anthropic|nvidia|amd|xai|deepmind|google|microsoft|meta|amazon|apple|"
    r"tesla|tsmc|broadcom|micron|palantir|mistral|perplexity|databricks|oracle|"
    r"softbank|coreweave)\b",
    re.I,
)
_PERSON_T3 = re.compile(
    r"\b(senator|congressman|congresswoman|representative|governor|lawmakers?|"
    r"parliament|minister|secretary of|commissioner|regulator|meps?|mp)\b",
    re.I,
)


def _person_bonus(text: str) -> float:
    if _PERSON_T1.search(text):
        return 18.0
    if _PERSON_T2.search(text):
        return 12.0
    if _COMPANY_T2.search(text):
        return 8.0
    if _PERSON_T3.search(text):
        return 6.0
    return 0.0


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
        # Google News 对同一 IP 的连续请求会 429，7 组查询必须错开
        self.gnews_delay = 1.8
        self._gnews_calls = 0

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
            story = _clean(hit.get("story_text") or "")
            if not title or not _relevant(title + " " + story):
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
            out.append((40.0 + min(points, 500) / 10.0 + _person_bonus(title + " " + story)
                        + node_bonus(title + " " + story), TgPost(
                channel="hot",
                message_id=_hid(link, title),
                text=text,
                posted_at=_iso(dt),
                url=link,
                source="hackernews",
            )))
        return out

    def _google_news(self, query: str, weight: float = 30.0) -> list[tuple[float, TgPost]]:
        # 同一 IP 连着打 Google News 会被 429：除第一条外先错开 1.8 秒
        if self._gnews_calls:
            time.sleep(self.gnews_delay)
        self._gnews_calls += 1
        url = (
            "https://news.google.com/rss/search?q="
            + urllib.parse.quote(query)
            + "&hl=en-US&gl=US&ceid=US:en"
        )
        return self._rss(url, weight, "googlenews")

    def _rss(self, url: str, weight: float, source: str) -> list[tuple[float, TgPost]]:
        out: list[tuple[float, TgPost]] = []
        root = None
        for attempt in range(1, 4):
            try:
                r = self.session.get(url, timeout=self.timeout)
                if r.status_code == 429:
                    wait = 4 * attempt
                    log.warning("%s 被限流(429)，%d 秒后重试（第 %d/3 次）",
                                source, wait, attempt)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                root = ET.fromstring(r.content)
                break
            except Exception as e:  # noqa: BLE001
                log.warning("%s 抓取失败（第 %d/3 次）：%s", source, attempt, str(e)[:120])
                time.sleep(2 * attempt)
        if root is None:
            log.error("%s 三次都没抓到，本轮少一个源：%s", source, url[:90])
            return out
        for item in root.iter("item"):
            title = _clean(item.findtext("title") or "")
            link = (item.findtext("link") or "").strip()
            if not title or not link:
                continue
            # Google News 标题尾巴带 " - 来源"，去掉
            title = re.sub(r"\s+-\s+[^-]{2,40}$", "", title).strip()
            desc = _clean(item.findtext("description") or "")
            desc = re.sub(r"^\s*" + re.escape(title) + r"\s*", "", desc).strip()
            if not _relevant(title + " " + desc):
                continue
            try:
                dt = parsedate_to_datetime(item.findtext("pubDate") or "")
            except Exception:
                dt = datetime.now(timezone.utc)
            if _age_hours(dt) > self.max_age_hours:
                continue
            text = f"{title}\n\n{desc}\n\nSource: {link}"
            out.append((weight + max(0.0, 10.0 - _age_hours(dt)) * 0.8
                        + _person_bonus(title + " " + desc)
                        + node_bonus(title + " " + desc), TgPost(
                channel="hot",
                message_id=_hid(link, title),
                text=text,
                posted_at=_iso(dt),
                url=link,
                source=source,
            )))
        if not out:
            log.info("%s 没解析出符合口径的条目：%s", source, url[:90])
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
            # 1) 芯片 / 算力：整条 AI 硬件链，不只 Nvidia
            self._google_news(
                '(Nvidia OR AMD OR Broadcom OR TSMC OR Micron OR Marvell OR Qualcomm) '
                '("AI chip" OR "AI chips" OR HBM OR GPU OR "data center" OR capex) when:2d',
                32.0)
            # 2) 大厂 AI 资本开支：谁在花钱
            + self._google_news(
                "(Microsoft OR Alphabet OR Amazon OR Meta OR Oracle OR Apple OR Tesla) "
                "AI (capex OR spending OR investment OR cloud OR chips) when:2d",
                30.0)
            # 3) 财报与指引：盘前 / 盘后的主力
            + self._google_news(
                "(Nvidia OR Microsoft OR Alphabet OR Amazon OR Meta OR AMD OR Broadcom "
                "OR TSMC OR Palantir OR CoreWeave) "
                "(earnings OR guidance OR outlook OR forecast) when:2d",
                30.0)
            # 4) AI 融资 / 估值 / 并购：谁在投钱
            + self._google_news(
                "(OpenAI OR Anthropic OR xAI OR Mistral OR Perplexity OR Databricks OR CoreWeave) "
                "(funding OR valuation OR IPO OR acquisition OR stake) when:2d",
                27.0)
            # 5) 宏观数据 → AI 板块
            + self._google_news(
                '(Fed OR CPI OR inflation OR "jobs report" OR Powell OR yields) '
                '(AI OR "tech stocks" OR Nasdaq OR semiconductor) when:2d',
                26.0)
            # 6) 个股 / 板块异动：开盘和尾盘最能用的就是这条
            + self._google_news(
                '("AI stocks" OR "chip stocks" OR "tech stocks" OR Nasdaq) '
                "(selloff OR sell-off OR rally OR surge OR plunge OR slump) when:2d",
                26.0)
            # 7) 人物表态（权重最高的一层，总统 > 主理人）
            + self._google_news("(Musk OR Trump OR Altman OR Huang) AI when:2d", 28.0)
        )
        techcrunch = self._rss(
            "https://techcrunch.com/category/artificial-intelligence/feed/", 24.0, "techcrunch"
        )

        pool: list[tuple[float, TgPost]] = hn + google + techcrunch

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
