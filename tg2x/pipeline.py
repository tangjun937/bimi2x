"""主流程：抓取 -> 去重 -> 归档 -> 翻译 -> 发布。"""
from __future__ import annotations

import inspect
import json
import logging
import mimetypes
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from . import charts as charts_mod
from . import market_clock
from . import media as media_mod
from .adfilter import ad_reason
from .config import Config
from .dedup import is_duplicate
from .fetchers import build_fetcher
from .models import TgPost, TransPost, now_iso
from .notify import TgNotifier
from .publisher import Publisher
from .store import Store
from .translator import Translator
from .writer import NewsWriter, content_filter_reason

log = logging.getLogger("tg2x")


def _paragraph_spacing(text: str) -> str:
    """把单个换行提升为段落间空行（\n\n），保留已有空行。

    翻译直发（不经过 DeepSeek 写作）时，译文段落间只留一个空行，避免太紧凑。
    """
    text = (text or "").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"(?<!\n)\n(?!\n)", "\n\n", text)
    return text


_NUM_RE = re.compile(r"\d")
# 从原文的 "Source: Livemint | https://…" 里取媒体名，做配图署名
_SOURCE_HINT = re.compile(r"Source:\s*([^|\n]{2,60})\s*\|")


def _has_numbers(text: str) -> bool:
    """正文里有没有可核对的事实（金额 / 百分比 / 日期 / 数量）。

    先去掉来源链接和 Hacker News 的 "N points"，避免 URL、分数里的数字冒充事实。
    """
    body = (text or "").split("Source:")[0]
    body = re.sub(r"https?://\S+", " ", body)
    body = re.sub(r"\b\d+\s+points on Hacker News\b", " ", body, flags=re.I)
    return bool(_NUM_RE.search(body))


# 运营文档要求：每篇必须点到具体公司 / 股票代码（cashtag）
_TICKER_RE = re.compile(r"\$[A-Z]{1,5}\b")
_COMPANIES = (
    "nvidia", "amd", "broadcom", "tsmc", "micron", "intel", "qualcomm", "arm",
    "microsoft", "alphabet", "google", "amazon", "meta", "apple", "oracle", "tesla",
    "palantir", "supermicro", "sk hynix", "samsung", "coreweave", "nebius",
    "openai", "anthropic", "xai", "cohere", "mistral",
)


def _word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9$'’]+", text or ""))


def _has_stock_ref(text: str) -> bool:
    """有 $代码 或点到具体公司名。"""
    body = text or ""
    if _TICKER_RE.search(body):
        return True
    low = body.lower()
    return any(c in low for c in _COMPANIES)


def _has_hook(text: str) -> bool:
    """最后一行是问句（互动钩子）。"""
    lines = [ln.strip() for ln in (text or "").strip().splitlines() if ln.strip()]
    return bool(lines) and lines[-1].endswith("?")


class Pipeline:
    def __init__(self, cfg: Config, dry_run: bool | None = None):
        self.cfg = cfg
        # TG 用户名大小写不敏感，统一小写，避免和解析结果对不上
        self.channel = (cfg.get("channel") or "").lstrip("@").lower()
        self.channels = self._parse_channels(cfg)
        self.store = Store(cfg.path_of("storage.data_dir", "data"))
        self.translator = Translator(cfg)
        self.writer = NewsWriter(cfg)
        # writer 支持 avoid 参数时，把最近 5 条已发的首尾句喂进去，防同一句式反复用
        try:
            self._writer_takes_avoid = "avoid" in inspect.signature(self.writer.write).parameters
        except (TypeError, ValueError):
            self._writer_takes_avoid = False
        self.publisher = Publisher(cfg)
        if dry_run is not None:
            self.publisher.dry_run = dry_run
        self._fetchers: dict[str, object] = {}
        self.notifier = TgNotifier(cfg)
        self._items: list[dict] = []
        self.max_per_cycle = int(cfg.get("fetch.max_per_cycle", 5) or 5)
        # 一次运行最多尝试多少条候选：碰到重复就换下一条，直到发够 max_per_cycle 篇
        self.max_tries = int(cfg.get("fetch.max_tries", self.max_per_cycle) or self.max_per_cycle)
        # 是否用 DeepSeek 再做一次影响排序。热点模式关掉：抓取器已经按
        # 「总统 > 财团主理人 > 其他政客 + 热度」排好，不想被模型改掉。
        self.rank_enabled = bool(cfg.get("rank.enabled", True))
        self.archive_media = bool(cfg.get("storage.archive_media", True))
        self.dedup_enabled = bool(cfg.get("cleanup.dedup_enabled", True))
        self.dedup_window = int(cfg.get("cleanup.dedup_window", 10) or 10)
        self.dedup_threshold = float(cfg.get("cleanup.dedup_threshold", 0.6) or 0.6)
        self.prune_keep = int(cfg.get("storage.prune_keep", 10) or 10)
        self.topic_first = bool(cfg.get("rank.topic_first", False))
        # 内容合规过滤（中国政治敏感 + 关于中国只发 AI 类）。默认关闭，仅 bimi2x 开启
        self.content_filter = bool(cfg.get("cleanup.content_filter", False))
        # 只搬带图/视频的内容，纯文字长文跳过。默认关闭，宠物视频类账号开启
        self.media_only = bool(cfg.get("cleanup.media_only", False))
        # 素材门槛：有数字的排前面（不强制跳过）。薄素材也能发，只是优先级低。
        self.prefer_numbers = bool(cfg.get("cleanup.prefer_numbers", True))
        # 配图：从文章页取 og:image，下载后传公网图床再交给 Buffer。
        # 真正要挂上去还得 publish.buffer.media = "telegram_url"，否则只取不发。
        self.attach_image = bool(cfg.get("publish.attach_image", True))
        # 配图优先：按影响力顺序往下探前 N 条候选，谁有 og:image 就把谁提到第一位。
        # 探不到就维持原顺序——配图是加分项，不为了配图牺牲选题质量。
        self.prefer_image = bool(cfg.get("publish.prefer_image", True))
        self.image_probe = int(cfg.get("publish.image_probe", 4) or 4)
        self.image_budget = float(cfg.get("publish.image_probe_seconds", 20) or 20)
        # 风格硬要求（运营文档 + 用户 2026-11-22 指令）：
        #   每篇必须点到具体的公司/股票代码、最后一行必须是互动问句、没配图就不许写长。
        self.require_stock = bool(cfg.get("writer.require_stock_ref", True))
        self.require_hook = bool(cfg.get("writer.require_hook", False))
        self.max_words_no_image = int(cfg.get("writer.max_words_no_image", 110) or 0)
        # 图表池：优先挂对标账号（@RyanDetrick / @charliebilello …）的图表，
        # 拿不到对得上的再退回文章自己的 og:image。
        self.chart_first = bool(cfg.get("publish.chart_first", True))
        self.chart_credit = bool(cfg.get("publish.chart_credit", True))
        self.chart_min_score = int(cfg.get("publish.chart_min_score", 4) or 4)
        self.chart_max_age = float(cfg.get("publish.chart_max_age_days", 10) or 10)
        # 新鲜度过滤：只发最近 N 小时内的帖子，超过的直接跳过（防止频道更新慢时补发旧闻）
        self.max_age_hours = int(cfg.get("cleanup.max_age_hours", 0) or 0)
        # 溢出丢弃：超过 max_per_cycle 的部分直接标记跳过（24h扫一次的视频号用）。
        # 默认 false = 保留给后续轮次继续发；dogs_vd/catx1t 设 true。
        self.drop_overflow = bool(cfg.get("cleanup.drop_overflow", False))
        # 记录保留天数：热点模式写过的内容只保留 N 天，过期删除。0 = 不清理
        self.retention_days = int(cfg.get("cleanup.retention_days", 0) or 0)
        # 每日配额：一个发布日（18:00→次日 08:20）最多发几条，其中一篇写长文
        self.daily_max = int(cfg.get("fetch.daily_max", 0) or 0)
        self.long_per_day = bool(cfg.get("publish.long_per_day", True))
        self.long_hour = int(cfg.get("publish.long_at_hour", 21) or 21)

    # ---------- 每日配额 ----------
    def _bj_now(self):
        return datetime.now(market_clock.BJ)

    def _bj_hour(self) -> int:
        return self._bj_now().hour

    def _day_key(self) -> str:
        """发布日：18:00 之后算今天；00:00–12:00 算前一天（同一夜）。"""
        now = self._bj_now()
        day = now.date() if now.hour >= 12 else (now - timedelta(days=1)).date()
        return day.isoformat()

    def _daily_state(self) -> tuple[str, int, bool]:
        day = self._day_key()
        used = int(self.store.get_meta(f"pub_{day}_n", "0") or 0)
        long_used = (self.store.get_meta(f"pub_{day}_long", "0") or "0") == "1"
        return day, used, long_used

    def _bump_daily(self, long_mode: bool) -> None:
        day, used, _ = self._daily_state()
        self.store.set_meta(f"pub_{day}_n", str(used + 1))
        if long_mode:
            self.store.set_meta(f"pub_{day}_long", "1")

    @staticmethod
    def _parse_channels(cfg: Config) -> list[dict]:
        chans = cfg.get("channels", None)
        out: list[dict] = []
        if chans and isinstance(chans, list):
            for c in chans:
                name = str(c.get("name", "")).lstrip("@").lower()
                if not name:
                    continue
                out.append({
                    "name": name,
                    "lang": (c.get("lang") or "zh").lower(),
                    "rewrite": bool(c.get("rewrite", False)),
                })
        if not out:
            out.append({"name": (cfg.get("channel") or "").lstrip("@").lower(),
                        "lang": "zh", "rewrite": False})
        return out

    # ---------- fetcher ----------
    def _fetcher(self, channel: str | None = None):
        key = (channel or self.channel).lower()
        if key not in self._fetchers:
            offset = int(self.store.get_meta("bot_offset", "0") or 0)
            self._fetchers[key] = build_fetcher(self.cfg, offset=offset, channel=key)
        return self._fetchers[key]

    # ---------- 一轮 ----------
    def cycle(self, publish: bool = True) -> dict:
        result = {"fetched": 0, "new": 0, "published": 0, "failed": 0, "skipped": 0}
        self._items = []

        # 发布窗口：北京时间 18:00 -> 次日 08:20（cron 用 UTC 写死，夏令时不影响）。
        # 窗口外只扫不发——cron 补跑、手动触发、时区漂移都不会把帖子放到白天。
        if publish and not getattr(self.publisher, "dry_run", False):
            allowed, why = market_clock.publish_allowed(self.cfg)
            if not allowed:
                log.info("不在发布窗口（%s），本轮不发布", why)
                return result
        log.info("市场时钟：%s", market_clock.describe())

        # 每日配额：一个"发布日" = 北京 18:00 → 次日 08:20（跨午夜算同一天）。
        # 配额里留 1 篇长文（默认 21:00 这一档写），其余按短文写。
        day_key, used, long_used = self._daily_state()
        if self.daily_max and used >= self.daily_max:
            log.info("今日配额已满（%d/%d），本轮不发", used, self.daily_max)
            return result
        long_mode = bool(self.long_per_day and not long_used and
                         self._bj_hour() >= self.long_hour)
        if long_mode:
            log.info("今日长文档：这一篇按长文写（配额 %d/%d）", used, self.daily_max)

        # 1. 收集所有频道的新帖（开启合规过滤时，先过滤避免占用发文名额）
        candidates: list[tuple[dict, TgPost]] = []
        for ch in self.channels:
            for p in self._gather_channel(ch, result):
                if self.content_filter:
                    cr = content_filter_reason(p.text)
                    if cr:
                        log.info("跳过 [%s]：合规过滤:%s", p.key, cr)
                        self.store.record(p, "skipped")
                        self.store.archive(p, {"reason": f"合规过滤:{cr}"})
                        result["skipped"] += 1
                        continue
                # 只搬带图/视频的短视频，纯文字长文跳过
                if self.media_only and not (p.has_photo or p.has_video):
                    log.info("跳过 [%s]：无媒体（纯文字/长文）", p.key)
                    self.store.record(p, "skipped")
                    self.store.archive(p, {"reason": "no-media"})
                    result["skipped"] += 1
                    continue
                # 新鲜度过滤：只发最近 N 小时内的帖子，防止频道更新慢时补发旧闻
                if self.max_age_hours > 0 and self._is_stale(p):
                    log.info("跳过 [%s]：超过 %d 小时的旧帖", p.key, self.max_age_hours)
                    self.store.record(p, "skipped")
                    self.store.archive(p, {"reason": "stale"})
                    result["skipped"] += 1
                    continue
                # 数字优先（不强制）：有金额/日期/涨跌动作的排前面，薄素材留作兜底
                candidates.append((ch, p))

        # 1.5 数字优先：有可核数字或涨跌动作的排前面（稳定排序，不打乱抓取器原有分数序）
        if self.prefer_numbers and len(candidates) > 1:
            candidates.sort(key=lambda cp: 0 if _has_numbers(cp[1].text) else 1)

        # 2. 影响排序（DeepSeek），只发影响大的前 N 篇
        ranked = candidates
        if len(candidates) > 1 and self.rank_enabled:
            ch_map = {id(p): ch for ch, p in candidates}
            posts = [p for _, p in candidates]
            ordered = self.writer.rank(posts, topic_first=self.topic_first)
            if len(ordered) == len(posts):
                ranked = [(ch_map[id(p)], p) for p in ordered]

        # 2.5 媒体优先：视频 > 图片 > 纯文字（只探前 N 条，层内保持影响力顺序）
        ranked = self._order_by_media(ranked)

        # 候选池：一次只发 max_per_cycle 篇，但最多尝试 max_tries 条——
        # 遇到重复（关键词去重命中）就跳过，换下一条，保证每条都是当场抓到的。
        try_limit = min(len(ranked), max(self.max_tries, self.max_per_cycle))
        planned = ranked[:try_limit]
        if len(ranked) > try_limit:
            overflow = ranked[try_limit:]
            if self.drop_overflow:
                log.info("本轮 %d 篇候选，只尝试前 %d 篇，其余 %d 篇去除",
                         len(ranked), try_limit, len(overflow))
                for _ch, p in overflow:
                    self.store.record(p, "skipped")
                    self.store.archive(p, {"reason": "overflow"})
                    result["skipped"] += 1
            else:
                log.info("本轮 %d 篇候选，只尝试前 %d 篇，其余 %d 篇留待后续轮次",
                         len(ranked), try_limit, len(overflow))

        # 3. 逐条处理：发够 max_per_cycle 篇就停；重复的跳过换下一条
        for ch, post in planned:
            if result["published"] >= self.max_per_cycle:
                break
            result["new"] += 1
            status = "failed"
            try:
                status = self._handle(post, ch, publish=publish, position=0, long_mode=long_mode)
            except Exception as e:  # noqa: BLE001
                log.exception("处理失败 %s: %s", post.key, e)
                self.store.mark(post.channel, post.message_id, "failed", error=str(e)[:500])
                self._items.append({"key": post.key, "text": "", "error": str(e)[:300]})
            result[status] += 1

        # 每轮结束清理：每频道只保留最近 N 条，防止库无限增长
        if self.prune_keep > 0:
            removed = self.store.prune(self.prune_keep)
            if removed:
                log.info("清理去重库：删除 %d 条旧记录（每频道保留最近 %d 条）",
                         removed, self.prune_keep)
        if self.retention_days > 0:
            purged = self.store.purge_older_than(self.retention_days)
            if purged:
                log.info("过期清理：删除 %d 条超过 %d 天的记录", purged, self.retention_days)
        self.notifier.report(result, self._items)
        return result

    def _gather_channel(self, ch: dict, result: dict) -> list[TgPost]:
        """抓取某频道并返回本轮新帖（首次运行只建基线，返回空）。"""
        fetcher = self._fetcher(ch["name"])
        posts = fetcher.fetch(limit=30)
        result["fetched"] += len(posts)

        # 中文原文只导出主频道，供本机国内分发器拉取（英文频道不用导）
        if ch["name"] == self.channel:
            self._export_cn_feed(posts)

        if getattr(fetcher, "offset", None):
            self.store.set_meta("bot_offset", str(fetcher.offset))

        boot_key = f"boot_done:{ch['name']}"
        first_run = self.store.get_meta(boot_key) != "1"
        if (first_run and not self.cfg.get("publish.existing_on_boot", False)
                and not getattr(fetcher, "skip_baseline", False)):
            for p in posts:
                self.store.record(p, status="skipped")
                self.store.archive(p, {"reason": "boot-baseline"})
            self.store.set_meta(boot_key, "1")
            result["skipped"] += len(posts)
            log.info("频道 @%s 首次启动：已把 %d 条历史帖标记为基线，不发布",
                     ch["name"], len(posts))
            return []

        fresh = [p for p in posts if not self.store.seen(p.channel, p.message_id)]
        if not getattr(fetcher, "preserve_order", False):
            fresh.sort(key=lambda p: p.message_id, reverse=True)
        return fresh

    def _is_stale(self, post: TgPost) -> bool:
        """判断帖子是否超过 max_age_hours（旧帖）。解析失败视为不旧（放行）。"""
        if not post.posted_at:
            return False
        try:
            dt = datetime.fromisoformat(post.posted_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc).astimezone() - dt
            return age.total_seconds() > self.max_age_hours * 3600
        except Exception:  # noqa: BLE001
            return False

    def _handle(self, post: TgPost, ch: dict, publish: bool = True, position: int = 0,
                long_mode: bool = False) -> str:
        """返回 published / skipped / failed 三种状态之一。"""
        if not post.text.strip() and not post.media_urls:
            self.store.record(post, "skipped")
            return "skipped"

        reason = self._skip_reason(post.text)
        if reason:
            log.info("跳过 [%s]：%s", post.key, reason)
            self.store.record(post, "skipped")
            self.store.archive(post, {"reason": reason})
            return "skipped"

        self.store.record(post, "new")
        self._fetch_media(post)

        text_en = ""
        has_image = self._has_image(post)
        if post.text.strip():
            # 先清理原文（去标题、去尾巴、去信源），再交给 DeepSeek 写作
            clean = (self.translator._clean_en(post.text) if ch["lang"] == "en"
                     else self.translator._clean(post.text))
            # 反重复：把最近 5 条已发正文交给 writer，要求别复用它们的开头/结尾句式
            kwargs = {}
            if self._writer_takes_avoid:
                kwargs["avoid"] = self.store.recent_published(5)
            if long_mode:
                # 走 writer 的 fix 通道下"长文"指令，不动 writer.py 的签名
                kwargs["fix"] = ("MODE: LONG COLUMN. Write the full piece for this story: every number "
                                 "in the source that matters, the mechanism in plain words, and who ends "
                                 "up holding the cost. Up to 25,000 characters. Stop when the chain is "
                                 "complete, not at a word count.")
            text_en = self._write(clean, has_image=has_image, **kwargs)
            if not text_en:
                # 翻译直发：段落间空一行
                text_en = (self.translator._clean_en(post.text) if ch["lang"] == "en"
                           else self.translator.translate(post.text))
                text_en = _paragraph_spacing(text_en)
            # 风格硬要求：股票代码 / cashtag / 没图别写长（不合规就定向重写）
            text_en = self._enforce_style(clean, text_en, has_image,
                                          mode_fix=kwargs.get("fix", ""))
        # 统一品牌前缀（如 "FinBrief Alert: "）
        prefix = self.cfg.get("translate.prefix", "") or ""
        if prefix and text_en and not text_en.startswith(prefix.rstrip()):
            text_en = f"{prefix}{text_en}"

        # 没有公司/股票代码 = 没有具体抓手，宁可不发这条，换下一条
        if self.require_stock and text_en and not _has_stock_ref(text_en):
            log.warning("跳过 [%s]：稿子里没有股票代码/公司名", post.key)
            self.store.record(post, "skipped")
            self.store.archive(post, {"reason": "no-stock-ref", "text_en": text_en})
            return "skipped"

        # 关键词去重：与最近已发内容高度重复则跳过（纯规则，不用 AI）
        if self.dedup_enabled and text_en and is_duplicate(
                text_en, self.store.recent_published(self.dedup_window),
                self.dedup_threshold):
            log.info("跳过 [%s]：与最近已发内容重复", post.key)
            self.store.record(post, "skipped")
            self.store.archive(post, {"reason": "duplicate-keyword"})
            return "skipped"

        item = TransPost(post=post, text_en=text_en)
        # 媒体优先级：图表池 → 文章视频 → 文章图 → 纯文字
        chart = None
        if self.chart_first and not self.publisher.dry_run and post.text.strip():
            chart = charts_mod.pick(post.text, self.chart_min_score, self.chart_max_age)
            if not chart:
                log.info("图表池没有和这条同源的图（同代码/同公司 + %.0f 天内），用文章自己的图",
                         self.chart_max_age)
        used_chart = bool(chart) and self._attach_chart(post, chart)
        if not used_chart:
            kind, img, vid = self._media_of(post)
            if kind == "video" and vid:
                post.video_urls = [vid]
                if img:
                    post.media_urls = [img]        # 缩略图，失败时还能退回图片帖
                log.info("配视频：%s", vid[:100])
            else:
                self._attach_image(post)
        if used_chart and self.chart_credit:
            label = "Video" if chart.get("video") else "Chart"
            text_en = text_en.rstrip() + f"\n\n{label}: @{chart.get('author') or 'Twitter'}"
            item.text_en = text_en
        item.final_text = self.publisher.build_text(item)
        self.store.mark(post.channel, post.message_id, "translated", text_en=text_en)
        self.store.archive(post, {"text_en": text_en, "final_text": item.final_text,
                                  "archived_at": now_iso()})
        log.info("译文 [%s] %s", post.key, text_en.replace("\n", " ")[:120])

        if not publish:
            self.store.mark(post.channel, post.message_id, "translated")
            return "skipped"

        res = self.publisher.publish(item, position=position)
        if res.ok:
            if self.publisher.dry_run:
                # dry run 不写去重库，否则这条会被当成已发，真跑时反而漏掉
                self.store.mark(post.channel, post.message_id, "translated")
                log.info("DRY-RUN 全文 [%s]：\n%s", post.key, item.final_text)
                log.info("DRY-RUN 生成完毕 [%s]（未入库，下一轮仍会真发）", post.key)
                self._items.append({"key": post.key, "text": item.final_text, "error": None})
                return "published"
            self.store.mark(post.channel, post.message_id, "published",
                            tweet_id=res.tweet_id, error=None)
            self._bump_daily(long_mode)
            log.info("已发 X [%s] tweet=%s", post.key, res.tweet_id or "?")
            self._items.append({"key": post.key, "text": item.final_text, "error": None})
            return "published"

        self.store.mark(post.channel, post.message_id, "failed", error=res.detail)
        log.error("发 X 失败 [%s] %s", post.key, res.detail)
        self._items.append({"key": post.key, "text": item.final_text, "error": res.detail})
        return "failed"

    # ---------- 中文原文导出 ----------
    def _export_cn_feed(self, posts: list[TgPost]) -> None:
        """把抓到的中文原文导出到 data/cn_feed.json，供本机国内分发器拉取。"""
        try:
            feed = {
                "channel": self.channel,
                "updated_at": now_iso(),
                "posts": [
                    {"message_id": p.message_id, "text": p.text,
                     "posted_at": p.posted_at, "url": p.url}
                    for p in posts if p.text.strip()
                ],
            }
            fp = self.store.data_dir / "cn_feed.json"
            fp.write_text(json.dumps(feed, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            log.warning("导出中文原文失败：%s", e)

    # ---------- 跳过判定 ----------
    def _skip_reason(self, text: str) -> str:
        """合规过滤（开启时）+ 黑名单 + 广告特征。"""
        if self.content_filter:
            cr = content_filter_reason(text)
            if cr:
                return f"合规过滤:{cr}"
        hit = self._blocked_by(text)
        if hit:
            return f"屏蔽词:{hit}"
        return ad_reason(
            text,
            strong=self.cfg.get("cleanup.ad_keywords", None),
            weak=self.cfg.get("cleanup.ad_weak_keywords", None),
            min_weak=int(self.cfg.get("cleanup.ad_weak_threshold", 2) or 2),
        )

    # ---------- 关键词屏蔽 ----------
    def _blocked_by(self, text: str) -> str:
        """命中 cleanup.block_keywords 里的任一词就返回该词，否则返回空串。"""
        words = self.cfg.get("cleanup.block_keywords", None) or []
        if not words or not text:
            return ""
        low = text.lower()
        for w in words:
            if w and str(w).lower() in low:
                return str(w)
        return ""

    # ---------- 写作 & 风格 ----------
    @staticmethod
    def _supports(fn, name: str) -> bool:
        try:
            return name in inspect.signature(fn).parameters
        except (TypeError, ValueError):
            return False

    def _write(self, clean: str, has_image: bool, avoid=None, fix: str = "") -> str:
        """调 writer；按它实际支持的参数传（老版 writer 没有 has_image/fix 也能跑）。"""
        kwargs = {}
        if avoid is not None and self._supports(self.writer.write, "avoid"):
            kwargs["avoid"] = avoid
        if self._supports(self.writer.write, "has_image"):
            kwargs["has_image"] = has_image
        if fix and self._supports(self.writer.write, "fix"):
            kwargs["fix"] = fix
        try:
            return self.writer.write(clean, **kwargs) or ""
        except TypeError:            # 签名不兼容的兜底
            return self.writer.write(clean) or ""

    def _media_of(self, post: TgPost) -> tuple[str, str, str]:
        """这条候选的媒体类型（探过的直接用，没探过现探）：(kind, 图, 视频)。"""
        kind = getattr(post, "_media_kind", None)
        if kind:
            return kind, getattr(post, "_img_url", "") or "", getattr(post, "_video_url", "") or ""
        if not (self.attach_image and (post.url or "").startswith("http")):
            return "text", "", ""
        try:
            kind, img, vid = media_mod.probe_media(post.url, allow_api=True)
        except Exception:  # noqa: BLE001
            kind, img, vid = "text", "", ""
        post._media_kind, post._img_url, post._video_url = kind, img, vid
        return kind, img, vid

    def _has_image(self, post: TgPost) -> bool:
        """这条稿子会不会带图/视频（决定写多长）。"""
        kind, _img, _vid = self._media_of(post)
        return kind in ("video", "image")

    def _style_problems(self, text: str, has_image: bool) -> list[str]:
        """运营文档口径的三条硬要求，返回不合规的说明（空 = 合规）。"""
        out = []
        if self.require_stock and not _has_stock_ref(text):
            out.append("the draft never names a company or a cashtag")
        elif self.require_stock and not _TICKER_RE.search(text or "") and \
                any(c in (text or "").lower() for c in _COMPANIES):
            out.append("the draft names a listed company but has no cashtag: add it, e.g. ($ANT)")
        if self.require_hook and not _has_hook(text):
            out.append("the last line must be one engagement question")
        if (not has_image) and self.max_words_no_image and \
                _word_count(text) > self.max_words_no_image:
            out.append(f"no image is attached and this draft is {_word_count(text)} words: "
                       f"cut it to {self.max_words_no_image} or fewer by dropping a fact")
        return out

    def _enforce_style(self, clean: str, text: str, has_image: bool,
                       mode_fix: str = "") -> str:
        """不合规就给 writer 一条定向修改意见，重写一次；仍不合规由调用方决定发不发。"""
        if not text:
            return text
        problems = self._style_problems(text, has_image)
        if not problems:
            return text
        if not self._supports(self.writer.write, "fix"):
            log.info("风格不合规（%s），但 writer 不支持定向重写，按现稿处理",
                     "; ".join(problems))
            return text
        cur = text
        for attempt in range(2):          # 最多两次定向重写：缺代码/缺钩子/太长
            log.info("风格不合规（%s），定向重写第 %d 次", "; ".join(problems), attempt + 1)
            fix = ("The previous draft failed these checks: " + "; ".join(problems)
                   + ". Keep the same facts, fix only that, and return the whole post again.")
            if attempt:
                fix += (" Hard cut now: delete one whole paragraph or the weakest sentences, "
                        "do not reword them into longer clauses.")
            if mode_fix:
                fix = mode_fix + " " + fix
            again = self._write(clean, has_image=has_image, fix=fix)
            if not again:
                return cur
            cur = again
            problems = self._style_problems(cur, has_image)
            if not problems:
                return cur
        log.warning("重写两次后仍不合规（%s），按现稿处理", "; ".join(problems))
        return cur

    # ---------- 配图 ----------
    _TIER = {"video": 0, "image": 1, "text": 2}

    def _order_by_media(self, ranked: list[tuple[dict, TgPost]]) -> list[tuple[dict, TgPost]]:
        """媒体优先：视频 > 图片 > 纯文字（只探前 N 条，层内保持原影响力顺序）。

        探不到 / 超时 / 关掉开关时原样返回。
        """
        if not (self.prefer_image and self.attach_image) or len(ranked) < 2:
            return ranked
        if getattr(self.publisher, "dry_run", False):
            return ranked                      # dry-run 只验文风，不多花时间探媒体
        t0 = time.monotonic()
        tiers: dict[int, str] = {}
        for i, (_ch, p) in enumerate(ranked[:self.image_probe]):
            if not (p.url or "").startswith("http"):
                continue
            try:
                # 只有第一条允许走代抓兜底（microlink 免费额度有限）
                kind, img, vid = media_mod.probe_media(p.url, allow_api=(i == 0))
            except Exception as e:  # noqa: BLE001
                log.debug("探媒体失败 %s: %s", p.key, str(e)[:100])
                kind, img, vid = "text", "", ""
            p._media_kind, p._img_url, p._video_url = kind, img, vid
            tiers[id(p)] = kind
            if time.monotonic() - t0 > self.image_budget:
                log.info("探媒体超时（%.0fs），剩下的按原顺序", self.image_budget)
                break
        if not tiers:
            return ranked
        head = ranked[:self.image_probe]
        head.sort(key=lambda cp: self._TIER.get(tiers.get(id(cp[1]), "text"), 3))
        return head + ranked[len(head):]

    def _attach_chart(self, post: TgPost, chart: dict) -> bool:
        """挂一张图表池里的图/视频。成功返回 True。"""
        try:
            if chart.get("video"):
                post.video_urls = [chart["video"]]
                if chart.get("image"):
                    post.media_urls = [chart["image"]]
                charts_mod.mark_used(str(chart.get("id") or ""))
                log.info("配视频：图表池 @%s（tweet %s）", chart.get("author") or "?", chart.get("id"))
                return True
            pub = media_mod.attach_image_url(chart.get("image") or "")
        except Exception as e:  # noqa: BLE001
            log.warning("挂图表失败（改用文章图）：%s", str(e)[:120])
            return False
        if not pub:
            return False
        post.media_urls = [pub]
        charts_mod.mark_used(str(chart.get("id") or ""))
        log.info("配图：图表池 @%s（tweet %s）", chart.get("author") or "?", chart.get("id"))
        return True

    def _attach_image(self, post: TgPost) -> str:
        """给这条配一张图：文章 og:image → 下载 → 传图床。返回署名，失败返回空串。

        配图是加分项：任何一步失败都照常发纯文字，不影响发布。
        """
        if post.media_urls:          # 图表池已经挂上了
            return ""
        if not self.attach_image or not (post.url or "").startswith("http"):
            return ""
        dry = bool(self.publisher.dry_run)
        hint = ""
        m = _SOURCE_HINT.search(post.text or "")
        if m:
            hint = m.group(1).strip()
        try:
            img_url, credit = media_mod.attach(post.url, do_upload=not dry, credit_hint=hint)
        except Exception as e:  # noqa: BLE001
            log.warning("配图失败（照常发文字）：%s", str(e)[:150])
            return ""
        if not img_url:
            log.info("这条取不到配图（源站没给 og:image 或被拦），照常发文字")
            return ""
        if dry:
            log.info("DRY-RUN 配图候选：%s  署名=%s", img_url, credit or "?")
            return ""
        post.media_urls = [img_url]
        log.info("配图：%s  署名=%s", img_url, credit or "?")
        try:
            media_mod.cleanup()          # 顺手清掉 3 天前的图，仓库不长胖
        except Exception as e:  # noqa: BLE001
            log.debug("清理旧图失败：%s", str(e)[:100])
        return credit

    # ---------- 图片落地 ----------
    def _fetch_media(self, post: TgPost) -> None:
        if not self.archive_media or not post.media_urls:
            return
        files: list[str] = []
        for i, url in enumerate(post.media_urls[:4], 1):
            if not url.startswith("http"):
                continue  # tg-media:// 等需 telethon 下载，暂跳过
            try:
                r = requests.get(url, timeout=30,
                                 headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code != 200:
                    continue
                ext = mimetypes.guess_extension(
                    (r.headers.get("Content-Type") or "").split(";")[0]
                ) or ".jpg"
                fp = self.store.media_dir / f"{post.channel}_{post.message_id}_{i}{ext}"
                fp.write_bytes(r.content)
                files.append(str(fp))
            except Exception as e:  # noqa: BLE001
                log.warning("图片下载失败 %s: %s", url[:80], e)
        post.media_files = files

    # ---------- 只写不发（调文风用）----------
    def demo(self, limit: int = 3, prefer_numbers: bool = True) -> list[dict]:
        """给排序最前的前 N 条候选各生成一版正文，用来判断文风是否稳定。

        **不发布、不写去重库、不消耗候选**——所以可以反复跑，不会把号发秃。
        """
        collected: list[tuple[dict, TgPost]] = []
        for ch in self.channels:
            for p in self._gather_channel(ch, {"fetched": 0, "new": 0}):
                if self.content_filter and content_filter_reason(p.text):
                    continue
                collected.append((ch, p))
        if prefer_numbers:
            collected.sort(key=lambda cp: 0 if _has_numbers(cp[1].text) else 1)
        collected = collected[:limit]

        out: list[dict] = []
        for ch, p in collected[:limit]:
            clean = (self.translator._clean_en(p.text) if ch["lang"] == "en"
                     else self.translator._clean(p.text))
            try:
                text = self.writer.write(clean)
            except Exception as e:  # noqa: BLE001
                text = f"(writer error: {e})"
            out.append({
                "key": p.key,
                "url": p.url,
                "headline": (p.text or "").splitlines()[0][:140],
                "text": text or "(empty)",
            })
        return out

    # ---------- 回填 ----------
    def backfill(self, limit: int = 20, publish: bool = False) -> dict:
        ch = self.channels[0]
        posts = self._fetcher(ch["name"]).fetch(limit=limit)
        out = {"fetched": len(posts), "new": 0, "published": 0, "failed": 0}
        for idx, p in enumerate(posts):
            if self.store.seen(p.channel, p.message_id):
                continue
            out["new"] += 1
            try:
                status = self._handle(p, ch, publish=publish, position=idx)
                out["published" if status == "published" else "failed"] += 1
            except Exception as e:  # noqa: BLE001
                out["failed"] += 1
                log.error("回填失败 %s: %s", p.key, e)
        return out

    def loop(self, interval: int | None = None) -> None:
        interval = interval or int(self.cfg.get("fetch.interval_seconds", 30) or 30)
        names = ", ".join(c["name"] for c in self.channels)
        log.info("开始轮询 @%s（后端=%s，间隔=%ss）", names,
                 self.cfg.get("fetch.backend"), interval)
        while True:
            try:
                r = self.cycle()
                log.info("本轮：新帖 %d，发布 %d，失败 %d",
                         r["new"], r["published"], r["failed"])
            except KeyboardInterrupt:
                raise
            except Exception as e:  # noqa: BLE001
                log.error("抓取异常：%s", e)
            time.sleep(interval)

    def close(self) -> None:
        for f in self._fetchers.values():
            try:
                f.close()
            except Exception:  # noqa: BLE001
                pass
