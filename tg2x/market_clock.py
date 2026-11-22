"""市场时钟：把「现在几点」翻译成「该发什么、能不能发」。

两条规则：

1) **发布窗口**固定在**北京时间 18:00 → 次日 08:20**（= UTC 10:00 → 00:20）。
   换算成美东是 05:00 → 19:20（EST），整段盖住盘前、开盘、盘中、收盘、盘后。
   因为窗口按北京时间写死，cron 直接用 UTC 表达即可，**夏令时不用改任何东西**：
   美东那段会自动前后挪 1 小时，仍然全程覆盖交易时段。

2) **节点匹配**：每小时跑一次，抓到候选后按「当前时段该关心的东西」加权——
      盘前：期货、隔夜、评级/目标价、指引预告
      开盘：涨跌异动、熔断、跳空
      盘中：订单、capex、产能、监管、并购
      收盘：全天结算、盘后财报预告
      盘后：财报、guidance、电话会
   这就是「选时间节点最搭的那条」：同样两条新闻，谁更贴当前时段谁先发。
"""

from __future__ import annotations

import os
import re
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

BJ = ZoneInfo("Asia/Shanghai")
ET = ZoneInfo("America/New_York")

DEFAULT_START = dtime(18, 0)   # 北京时间
DEFAULT_END = dtime(8, 20)     # 次日北京时间


# ---------------------------------------------------------------- 发布窗口
def _hhmm(value, fallback: dtime) -> dtime:
    m = re.match(r"^\s*(\d{1,2})[:：](\d{2})\s*$", str(value or ""))
    if not m:
        return fallback
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return fallback
    return dtime(h, mi)


def _cfg(cfg, key: str, default=None):
    try:
        return cfg.get(key, default) if cfg is not None else default
    except Exception:  # noqa: BLE001
        return default


def window(cfg=None) -> tuple[dtime, dtime]:
    """（开始, 结束）两个北京时间点。环境变量 > config > 默认 18:00 / 08:20。"""
    start = _hhmm(
        os.environ.get("PUBLISH_WINDOW_START") or _cfg(cfg, "publish.window.start", ""),
        DEFAULT_START,
    )
    end = _hhmm(
        os.environ.get("PUBLISH_WINDOW_END") or _cfg(cfg, "publish.window.end", ""),
        DEFAULT_END,
    )
    return start, end


def in_window(now: datetime | None = None, cfg=None) -> bool:
    start, end = window(cfg)
    t = (now or datetime.now(BJ)).astimezone(BJ).time()
    if start <= end:
        return start <= t <= end
    return t >= start or t <= end     # 跨零点：18:00-23:59 或 00:00-08:20


# ---------------------------------------------------------------- 市场节点
_PHASES = (
    # key,       中文,   起(美东分钟),   止
    ("premarket", "盘前", 4 * 60, 9 * 60 + 30),
    ("open", "开盘", 9 * 60 + 30, 10 * 60 + 30),
    ("midday", "盘中", 10 * 60 + 30, 15 * 60),
    ("close", "收盘", 15 * 60, 16 * 60),
    ("afterhours", "盘后", 16 * 60, 20 * 60),
)


def phase(now: datetime | None = None) -> tuple[str, str]:
    """返回（key, 中文名）。按美东本地时间判断，夏令时自动跟随。"""
    et = (now or datetime.now(ET)).astimezone(ET)
    mins = et.hour * 60 + et.minute
    for key, label, lo, hi in _PHASES:
        if lo <= mins < hi:
            return key, label
    return "closed", "休市"


def _re(s: str):
    return re.compile(s, re.I)


# 每个时段关心的词：命中加分，同类不重复叠加（见 node_bonus 的封顶）
_RULES: dict[str, list[tuple[re.Pattern, float]]] = {
    "premarket": [
        (_re(r"\b(pre-?market|before the bell|ahead of the open|futures|overnight)\b"), 5.0),
        (_re(r"\b(upgrade[sd]?|downgrade[sd]?|price target|initiated coverage|"
             r"overweight|underweight|buy rating|sell rating)\b"), 4.0),
        (_re(r"\b(guidance|outlook|forecast|preview|estimates?|expects?)\b"), 3.0),
        (_re(r"\b(asian|european)\s+(stocks|markets)\b"), 2.0),
    ],
    "open": [
        (_re(r"\b(surges?|surged|jumps?|jumped|soars?|soared|rallies|rallied|"
             r"plunges?|plunged|slumps?|slumped|tumbles?|tumbled|sell-?off)\b"), 5.0),
        (_re(r"\b(opens?|opened|at the open|halted?|circuit breaker|gaps?|gapped)\b"), 4.0),
        (_re(r"\b(rises?|rose|falls?|fell|gains?|gained|slides?|slid|climbs?|climbed|drops?|dropped)\b"), 2.0),
    ],
    "midday": [
        (_re(r"\b(capex|capital expenditure|data cent(?:er|re)|capacity|shipments?|"
             r"supply|shortage|orders?|contract|partnership|joint venture|expansion)\b"), 4.0),
        (_re(r"\b(regulat(?:e|es|ed|ion|ors?)|antitrust|tariffs?|export controls?|"
             r"probe|lawsuit|fine|sanctions?)\b"), 3.0),
        (_re(r"\b(acquires?|acquisition|merger|buyout|stake|deal)\b"), 3.0),
    ],
    "close": [
        (_re(r"\b(clos(?:e|es|ed|ing)|at the close|session|all-time high|record high|"
             r"record close|weekly|monthly)\b"), 4.0),
        (_re(r"\b(after the bell|post-earnings|ahead of earnings|earnings today)\b"), 4.0),
        (_re(r"\b(winners?|losers?|laggards?|rotation|breadth)\b"), 2.0),
    ],
    "afterhours": [
        (_re(r"\b(after hours|after-hours|after the bell|extended trading)\b"), 6.0),
        (_re(r"\b(earnings|results|quarter(?:ly)?|fiscal|q[1-4]|revenue|profit|margin)\b"), 4.0),
        (_re(r"\b(beats?|beat|misses?|missed|guidance|outlook|forecast|raises?|raised|"
             r"lowers?|lowered|cuts?|cut)\b"), 3.0),
        (_re(r"\b(conference call|earnings call|analyst day|investor day)\b"), 2.0),
    ],
}


def node_bonus(text: str, now: datetime | None = None) -> float:
    """当前时段与内容的匹配度，0-10 分。越低代表这条更适合别的时段。"""
    if not text:
        return 0.0
    key, _label = phase(now)
    total = 0.0
    for pattern, weight in _RULES.get(key, ()):
        if pattern.search(text):
            total += weight
    return min(total, 10.0)


def describe(now: datetime | None = None) -> str:
    bj = (now or datetime.now(BJ)).astimezone(BJ)
    et = bj.astimezone(ET)
    _key, label = phase(bj)
    return f"{label}（美东 {et:%H:%M} / 北京 {bj:%H:%M}）"


def publish_allowed(cfg=None) -> tuple[bool, str]:
    """现在能不能真发。返回（是否允许, 说明）。

    窗口外只扫不发：cron 的补跑、手动触发、时区漂移都不会把帖子放到白天。
    临时要在窗口外发，设 FORCE_PUBLISH=1。
    """
    if str(os.environ.get("FORCE_PUBLISH", "")).strip() == "1":
        return True, "FORCE_PUBLISH=1（跳过窗口检查）"
    if _cfg(cfg, "publish.window.enabled", True) is False:
        return True, "窗口检查已关闭（publish.window.enabled=false）"
    start, end = window(cfg)
    now = datetime.now(BJ)
    if in_window(now, cfg):
        return True, describe(now)
    return False, f"北京 {now:%H:%M} 不在窗口 {start:%H:%M}-{end:%H:%M}"
