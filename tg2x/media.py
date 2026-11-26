"""配图：从文章页取 og:image → 下载 → 传到**仓库自己的 media/ 目录** → 交给 Buffer。

为什么不用第三方图床：litterbox / catbox 对云主机 IP 一律返回 412 Invalid uploader，
0x0.st 已停用上传，tmpfiles 的 /dl/ 链接是 HTML 中转页、Buffer 拿不到图。
仓库自己的 raw 链接实测提交后 4 秒可取，是唯一稳定的一条路。

为什么不能直接用文章原图链接：多数媒体有防盗链、要 referer，Buffer 抓不到。

清理：每轮顺手删掉 3 天前的图，media/ 目录保持在几十张的量级。

全部失败就返回 (None, "")，调用方照常发纯文字——配图是加分项，不是必需项。
"""

from __future__ import annotations

import base64
import hashlib
import html as html_lib
import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import requests

log = logging.getLogger("tg2x")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

MAX_BYTES = 8 * 1024 * 1024      # 8MB 以上不传（Buffer 也吃不消）
MIN_BYTES = 12 * 1024            # 太小多半是图标 / 占位图
OK_TYPES = ("image/jpeg", "image/png", "image/webp")

REPO = "tangjun937/bimi2x"
BRANCH = "main"
MEDIA_DIR = "media"
KEEP_DAYS = 3
MAX_DELETES = 15


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": UA})
    # 本机跑要过代理；云主机上没有这个环境变量，保持直连
    proxy = os.environ.get("TG2X_PROXY", "").strip()
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    return s


_META_PATTERNS = (
    r'<meta[^>]+property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*property=["\']og:image["\']',
    r'<meta[^>]+name=["\']twitter:image["\'][^>]*content=["\']([^"\']+)["\']',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*name=["\']twitter:image["\']',
)
_SITE_PATTERN = (
    r'<meta[^>]+property=["\']og:site_name["\'][^>]*content=["\']([^"\']+)["\']',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*property=["\']og:site_name["\']',
)


def _first(pattern_list, text: str) -> str:
    for p in pattern_list:
        m = re.search(p, text, re.I)
        if m:
            return m.group(1).strip()
    return ""


_PAGES: dict[str, tuple[str, str]] = {}
_PAGES_MAX = 16


def _page(url: str) -> tuple[str, str]:
    """带缓存的 resolve：同一轮里先 probe 再 attach，只抓一次文章页。"""
    hit = _PAGES.get(url)
    if hit is not None:
        return hit
    final, html = resolve(url)
    if len(_PAGES) >= _PAGES_MAX:
        _PAGES.clear()
    _PAGES[url] = (final, html)
    return final, html


_VIDEO_PATTERNS = (
    r'<meta[^>]+property=["\']og:video(?::url|:secure_url)?["\'][^>]*content=["\']([^"\']+)["\']',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*property=["\']og:video',
    r'<meta[^>]+name=["\']twitter:player:stream["\'][^>]*content=["\']([^"\']+)["\']',
)


def extract_text(html: str, max_chars: int = 6000) -> str:
    """从文章页抠正文（给写作当素材：RSS 摘要太薄，数字都在正文里）。

    只取 <p> 段落，去掉脚本/样式/导航；太短的段落（版权、按钮）丢掉。
    """
    if not html:
        return ""
    body = re.sub(r"<(script|style|noscript|nav|footer|header)[^>]*>.*?</\1>", " ",
                  html, flags=re.S | re.I)
    paras = re.findall(r"<p[^>]*>(.*?)</p>", body, flags=re.S | re.I)
    out: list[str] = []
    for p in paras:
        t = re.sub(r"<[^>]+>", " ", p)
        t = html_lib.unescape(t)
        t = re.sub(r"\s+", " ", t).strip()
        if len(t) < 60:
            continue
        if t in out:
            continue
        out.append(t)
        if sum(len(x) for x in out) > max_chars:
            break
    return "\n\n".join(out)[:max_chars]


_CHART_HINT = re.compile(r"(chart|graph|figure|data|table|plot|cdn|upload|media|images)", re.I)


def find_chart(html: str, base_url: str) -> str:
    """优先找正文里的大图（图表/表格），找不到返回空串。

    只认带明确宽高、宽度 ≥600 的 <img>，避免把图标、头像、logo 当成配图。
    """
    best, best_w = "", 0
    for m in re.finditer(r"<img\b[^>]*>", html or "", flags=re.I):
        tag = m.group(0)
        src = re.search(r' src=["\']([^"\']+)["\']', tag, re.I)
        if not src:
            continue
        url = urljoin(base_url, html_lib.unescape(src.group(1)))
        if not url.startswith("http"):
            continue
        w = re.search(r'width=["\']?(\d+)', tag, re.I)
        h = re.search(r'height=["\']?(\d+)', tag, re.I)
        width = int(w.group(1)) if w else 0
        height = int(h.group(1)) if h else 0
        if width < 600 or height and height < 200:
            continue
        weight = width + (200 if _CHART_HINT.search(url) else 0)
        if weight > best_w:
            best, best_w = url, weight
    return best


def probe_media(url: str, allow_api: bool = False) -> tuple[str, str, str]:
    """看这篇文章带什么媒体：返回 (kind, 图片地址, 视频地址)。

    kind = "video" | "image" | "text"。视频只认能直接喂给 Buffer 的 mp4 直链；
    其它（YouTube 嵌入页、播放器页）不算——挂了也放不出来。
    """
    if not url or not url.startswith("http"):
        return "text", "", ""
    final, page = _page(url)
    img, credit = find_image(page, final) if page else ("", "")
    vid = ""
    if page:
        cand = _first(_VIDEO_PATTERNS, page)
        if cand:
            cand = urljoin(final, cand)
            if ".mp4" in cand.lower():
                vid = cand
    if not img and allow_api:
        api_img, api_credit = _api_image(_target_url(url) or url)
        if api_img:
            img, credit = api_img, (api_credit or credit)
    if vid:
        return "video", _upgrade_google(img), vid
    if img:
        return "image", _upgrade_google(img), ""
    return "text", "", ""


def probe(url: str, allow_api: bool = False) -> tuple[str, str]:
    """只看这篇文章有没有可用配图（不下载、不上传）。返回（图地址, 署名）。

    给"有配图的优先选"用：命中就返回非空，没命中返回 ("", "")。
    allow_api=True 时，直连被反爬挡住的才走 microlink 兜底（省额度，只给第一条用）。
    """
    if not url or not url.startswith("http"):
        return "", ""
    final, page = _page(url)
    img, credit = find_image(page, final) if page else ("", "")
    if not img and allow_api:
        img, credit = _api_image(_target_url(url) or url)
    if not img:
        return "", ""
    return _upgrade_google(img), credit


_GN_ARTICLE = re.compile(r"news\.google\.com/rss/articles/([A-Za-z0-9_\-]+)")
# Google News 给所有文章配的同一张社交卡（用户反馈"图片都是同一张"就是它）
_GN_CARD = "J6_coFbogxhRI9iM864NL_liGXvsQp2AupsKei7z0cNNfDvGUmWUy20nuUhkREQyrpY4bEeIBuc"


def _decode_gnews(art_id: str, s: requests.Session) -> str:
    """把 news.google.com 的文章 id 换成真正的发布方 URL（batchexecute）。

    换不到就返回空串——宁可这条不带图，也不要挂一张 Google 的通用卡。
    """
    try:
        page = s.get(f"https://news.google.com/rss/articles/{art_id}", timeout=30)
        if page.status_code != 200:
            return ""
        sg = re.search(r'data-n-a-sg="([^"]+)"', page.text)
        ts = re.search(r'data-n-a-ts="([^"]+)"', page.text)
        if not (sg and ts):
            return ""
        payload = [
            "Fbv4je",
            '["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,'
            'null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{}",{},"{}"]'.format(
                art_id, ts.group(1), sg.group(1)),
        ]
        body = "f.req=" + quote(json.dumps([[payload]]))
        r = s.post("https://news.google.com/_/DotsSplashUi/data/batchexecute",
                   data=body, timeout=30,
                   headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                            "Origin": "https://news.google.com",
                            "Referer": f"https://news.google.com/rss/articles/{art_id}"})
        if r.status_code != 200:
            return ""
        parts = r.text.split("\n\n")
        if len(parts) < 2:
            return ""
        inner = json.loads(json.loads(parts[1])[0][2])
        url = inner[1] if len(inner) > 1 else ""
        return url if url.startswith("http") else ""
    except Exception as e:  # noqa: BLE001
        log.debug("Google News 解码失败 %s: %s", art_id[:24], str(e)[:100])
        return ""


_DECODED: dict[str, str] = {}


def _target_url(url: str) -> str:
    """Google News 的 RSS 链接换成发布方真地址（带缓存，probe/attach 只解一次）。"""
    m = _GN_ARTICLE.search(url or "")
    if not m:
        return url
    art = m.group(1)
    if art not in _DECODED:
        _DECODED[art] = _decode_gnews(art, _session())
    return _DECODED[art]


def _api_image(url: str, timeout: int = 45) -> tuple[str, str]:
    """直连被反爬（403 / 无 og:image）时，用 microlink 代抓一张图。

    匿名免费额度有限（每天几十次），所以只在真的选中的那条上兜底用。
    """
    if not url or not url.startswith("http"):
        return "", ""
    try:
        r = _session().get("https://api.microlink.io/", params={"url": url}, timeout=timeout)
        if r.status_code != 200:
            return "", ""
        d = (r.json() or {}).get("data") or {}
        img = d.get("image") or {}
        u = str(img.get("url") or "")
        w = int(img.get("width") or 0)
        h = int(img.get("height") or 0)
        if not u.startswith("http") or w < 300 or h < 200:
            return "", ""
        if _GN_CARD in u or "googleusercontent.com" in u or "gstatic.com/gnews/" in u:
            return "", ""          # 又是 Google 那张通用卡，宁可不配图
        return u, str(d.get("publisher") or "")
    except Exception as e:  # noqa: BLE001
        log.debug("microlink 取图失败 %s: %s", url[:70], str(e)[:100])
        return "", ""


def resolve(url: str, timeout: int = 20) -> tuple[str, str]:
    """跟随跳转拿到最终页面。返回（最终 URL, HTML）。失败返回 ("", "")。

    Google News 的 RSS 链接在服务端只会看到 Google 自己的聚合页（og:image 是一张
    通用卡，所有文章都一样），所以先解出发布方 URL 再去抓真页面。
    """
    s = _session()
    if _GN_ARTICLE.search(url or ""):
        url = _target_url(url)
        if not url:
            return "", ""          # 解不出来就不配图，绝不挂 Google 的通用卡
    for attempt in range(3):
        try:
            r = s.get(url, timeout=timeout, allow_redirects=True)
            if r.status_code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code != 200:
                return "", ""
            final = r.url
            # Google 的同意页 / 反爬页：不是文章正文
            if "consent.google.com" in final or "sorry/index" in final:
                return "", ""
            if "news.google.com" in final:      # 没跳出去，还是 Google 的壳
                return "", ""
            return final, r.text
        except Exception as e:  # noqa: BLE001
            log.debug("解析文章页失败(%d/3) %s: %s", attempt + 1, url[:80], str(e)[:100])
            time.sleep(2 * (attempt + 1))
    return "", ""


def find_image(html: str, base_url: str) -> tuple[str, str]:
    """返回（图片绝对地址, 署名）。署名优先 og:site_name，否则用域名。"""
    img = _first(_META_PATTERNS, html)
    if not img:
        return "", ""
    img = urljoin(base_url, img)
    # Google News / gstatic 的通用社交卡：所有文章一张图，直接当没有
    if _GN_CARD in img or "gstatic.com/gnews/" in img:
        return "", ""
    credit = _first(_SITE_PATTERN, html)
    if not credit:
        credit = urlparse(base_url).netloc.replace("www.", "")
    return img, credit


# ---------- 图片去重：同一张图不要在最近几天重复用 ----------
_USED = Path(__file__).resolve().parent.parent / "data" / "used_images.json"
_USED_DAYS = 7


def _load_used() -> dict:
    try:
        return json.loads(_USED.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_used(data: dict) -> None:
    try:
        _USED.parent.mkdir(parents=True, exist_ok=True)
        _USED.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.debug("记录图片去重失败：%s", str(e)[:80])


def _seen_before(digest: str) -> bool:
    data = _load_used()
    cutoff = time.time() - _USED_DAYS * 86400
    fresh = {k: v for k, v in data.items() if isinstance(v, (int, float)) and v >= cutoff}
    if len(fresh) != len(data):
        _save_used(fresh)
    return digest in fresh


def _remember(digest: str) -> None:
    data = _load_used()
    data[digest] = int(time.time())
    _save_used(data)


def download(url: str, timeout: int = 25) -> tuple[bytes, str]:
    """下载图片，返回（字节, 扩展名）。不合法返回 (b"", "")。"""
    s = _session()
    try:
        r = s.get(url, timeout=timeout, stream=True)
        if r.status_code != 200:
            return b"", ""
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype not in OK_TYPES:
            return b"", ""
        buf = b""
        for chunk in r.iter_content(65536):
            buf += chunk
            if len(buf) > MAX_BYTES:
                return b"", ""
        if len(buf) < MIN_BYTES:
            return b"", ""
        ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[ctype]
        return buf, ext
    except Exception as e:  # noqa: BLE001
        log.debug("下载图片失败 %s: %s", url[:80], str(e)[:100])
        return b"", ""


def _gh_token() -> str:
    return (os.environ.get("MEDIA_UPLOAD_TOKEN") or os.environ.get("GITHUB_TOKEN")
            or os.environ.get("GITHUB_PAT") or "").strip()


def _gh_headers() -> dict:
    token = _gh_token()
    h = {"Accept": "application/vnd.github+json", "User-Agent": "bimi2x"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _api(path: str) -> str:
    return f"https://api.github.com/repos/{REPO}/contents/{path}"


def upload(data: bytes, ext: str = ".jpg", timeout: int = 60) -> str:
    """把图提交到仓库 media/ 目录，返回 raw 公网直链。失败返回空串。"""
    if not _gh_token():
        log.warning("没有 GITHUB_TOKEN，跳过配图上传")
        return ""
    name = time.strftime("%Y%m%d-%H%M%S") + "-" + hashlib.sha1(data).hexdigest()[:6] + ext
    path = f"{MEDIA_DIR}/{name}"
    body = {"message": f"media: {name}",
            "content": base64.b64encode(data).decode(),
            "branch": BRANCH}
    s = _session()
    try:
        r = s.put(_api(path), headers={**_gh_headers(), "Content-Type": "application/json"},
                  data=json.dumps(body).encode(), timeout=timeout)
        if r.status_code not in (200, 201):
            log.warning("图上传失败：HTTP %s %s", r.status_code, (r.text or "")[:120])
            return ""
    except Exception as e:  # noqa: BLE001
        log.warning("图上传异常：%s", str(e)[:120])
        return ""

    url = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{path}"
    for _ in range(8):                      # 确认 Buffer 马上能取到
        try:
            g = s.get(url, timeout=20)
            if g.status_code == 200 and len(g.content) > 1000:
                return url
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)
    log.warning("图已提交但 raw 取不到：%s", url)
    return ""


def cleanup(days: int = KEEP_DAYS) -> int:
    """删掉 media/ 下超过 N 天的图，避免仓库无限长胖。返回删除数量。"""
    if not _gh_token():
        return 0
    s = _session()
    try:
        r = s.get(_api(MEDIA_DIR), headers=_gh_headers(), timeout=30)
        if r.status_code != 200:
            return 0
        items = r.json()
    except Exception:  # noqa: BLE001
        return 0
    if not isinstance(items, list):
        return 0
    cutoff = time.strftime("%Y%m%d", time.gmtime(time.time() - days * 86400))
    removed = 0
    for it in sorted(items, key=lambda x: x.get("name", "")):
        if removed >= MAX_DELETES:
            break
        name = it.get("name", "")
        m = re.match(r"^(\d{8})-", name)
        if not m or m.group(1) >= cutoff:
            continue
        try:
            d = s.delete(_api(f"{MEDIA_DIR}/{name}"),
                         headers={**_gh_headers(), "Content-Type": "application/json"},
                         data=json.dumps({"message": f"media: prune {name}",
                                          "sha": it["sha"], "branch": BRANCH}).encode(),
                         timeout=30)
            if d.status_code in (200, 201):
                removed += 1
        except Exception:  # noqa: BLE001
            continue
    if removed:
        log.info("清理旧配图：删除 %d 张（保留 %d 天）", removed, days)
    return removed


def _upgrade_google(img: str) -> str:
    """Google 的图片代理默认给 w300 缩略图，换成 w1200（实测 12KB -> 177KB）。"""
    if "googleusercontent.com" not in img:
        return img
    return re.sub(r"=s0-w\d+-rw", "=s0-w1200-rw", img)


def attach_image_url(url: str) -> str:
    """把一个已经确定好的图片 URL 挂上去：下载 → 去重 → 提交仓库。返回公网直链。

    图表池（对标账号的图）走这条路；去重口径和文章配图共用一份 ledger。
    """
    data, ext = download(url)
    if not data:
        return ""
    digest = hashlib.sha1(data).hexdigest()[:12]
    if _seen_before(digest):
        log.info("这张图和最近 %d 天用过的重复（%s），这次不配图", _USED_DAYS, digest[:6])
        return ""
    pub = upload(data, ext)
    if not pub:
        return ""
    _remember(digest)
    return pub


def attach(url: str, do_upload: bool = True, credit_hint: str = "") -> tuple[str, str]:
    """完整链路：解析 → 找图 → 下载 → 上传。返回（公网 URL, 署名）。

    do_upload=False 时只验证链路，不上传（dry-run 用，免得白占图床）。
    credit_hint：RSS 里带出来的媒体名（Google 的同意页/代理图只能给 "Google News"
    这种错的署名，用它顶掉）。
    """
    if not url or not url.startswith("http"):
        return "", ""
    final, page = _page(url)      # 上一轮 probe 抓过的页面直接复用
    img, credit = "", ""
    if page:
        img = find_chart(page, final)          # 正文里的大图/图表优先
        if not img:
            img, credit = find_image(page, final)
    if not img and _screenshot_enabled():      # 页面里没有现成的图：截一张图表
        from . import shot as shot_mod
        png = shot_mod.capture(_target_url(url) or url)
        if png:
            pub = upload(png, ".png")
            if pub:
                _remember(hashlib.sha1(png).hexdigest()[:12])
                log.info("配图：文章页截图（%d KB）", len(png) // 1024)
                return pub, credit or "screenshot"
    if not img:                   # 发布方反爬 / 页面没给 og:image：代抓一张
        api_img, api_credit = _api_image(_target_url(url) or url)
        if api_img:
            img, credit = api_img, (api_credit or credit)
    if not img:
        return "", ""
    if "googleusercontent.com" in img:
        img = _upgrade_google(img)
        if credit.lower() in ("google news", "news.google.com") or not credit:
            credit = credit_hint
    if not credit:
        credit = credit_hint or urlparse(final).netloc.replace("www.", "")
    art_host = urlparse(final).netloc.replace("www.", "")
    img_host = urlparse(img).netloc.replace("www.", "")
    if art_host and img_host:
        # 审计用：图是哪来的、文章是哪来的，一眼能对上（同源就该同域或同 CDN）
        log.info("配图来源：%s ← %s", img_host, art_host)
    data, ext = download(img)
    if not data:
        return "", ""
    digest = hashlib.sha1(data).hexdigest()[:12]
    if _seen_before(digest):
        log.info("这张图和最近 %d 天用过的重复（%s），这次不配图", _USED_DAYS, digest[:6])
        return "", ""
    if not do_upload:
        return img, credit          # dry-run：只回报将要用的图
    pub = upload(data, ext)
    if not pub:
        return "", ""
    _remember(digest)
    return pub, credit
