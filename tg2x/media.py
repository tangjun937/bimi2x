"""配图：从文章页取 og:image → 下载 → 传到公网图床 → 交给 Buffer。

为什么不能直接用文章原图的链接：多数媒体有防盗链、要 referer、或者干脆是
CDN 私有路径，Buffer 抓不到（历史坑：Buffer 只接受公网可直接访问的 URL）。
所以这里先下载到内存，再传到 litterbox（catbox 的临时图床，72 小时过期），
拿一个干净的 https 直链。

全部失败就返回 (None, "")，调用方照常发纯文字——配图是加分项，不是必需项。
"""

from __future__ import annotations

import logging
import re
import time
from urllib.parse import urljoin, urlparse

import requests

log = logging.getLogger("tg2x")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

MAX_BYTES = 8 * 1024 * 1024      # 8MB 以上不传（Buffer 也吃不消）
MIN_BYTES = 12 * 1024            # 太小多半是图标 / 占位图
OK_TYPES = ("image/jpeg", "image/png", "image/webp")

HOST = "https://litterbox.catbox.moe/resources/internals/api.php"


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": UA})
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


def resolve(url: str, timeout: int = 20) -> tuple[str, str]:
    """跟随跳转拿到最终页面。返回（最终 URL, HTML）。失败返回 ("", "")。"""
    s = _session()
    try:
        r = s.get(url, timeout=timeout, allow_redirects=True)
        if r.status_code != 200:
            return "", ""
        final = r.url
        # Google 的同意页 / 反爬页：不是文章正文
        if "consent.google.com" in final or "sorry/index" in final:
            return "", ""
        return final, r.text
    except Exception as e:  # noqa: BLE001
        log.debug("解析文章页失败 %s: %s", url[:80], str(e)[:100])
        return "", ""


def find_image(html: str, base_url: str) -> tuple[str, str]:
    """返回（图片绝对地址, 署名）。署名优先 og:site_name，否则用域名。"""
    img = _first(_META_PATTERNS, html)
    if not img:
        return "", ""
    img = urljoin(base_url, img)
    credit = _first(_SITE_PATTERN, html)
    if not credit:
        credit = urlparse(base_url).netloc.replace("www.", "")
    return img, credit


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


def upload(data: bytes, ext: str = ".jpg", timeout: int = 40) -> str:
    """传到 litterbox（72 小时有效），返回公网直链。失败返回空串。"""
    s = _session()
    name = f"bimi2x{ext}"
    try:
        r = s.post(
            HOST,
            data={"reqtype": "fileupload", "time": "72h"},
            files={"fileToUpload": (name, data)},
            timeout=timeout,
        )
        txt = (r.text or "").strip()
        if txt.startswith("http"):
            return txt
        log.warning("图床上传失败：%s", txt[:120])
    except Exception as e:  # noqa: BLE001
        log.warning("图床上传异常：%s", str(e)[:120])
    return ""


def _upgrade_google(img: str) -> str:
    """Google 的图片代理默认给 w300 缩略图，换成 w1200（实测 12KB -> 177KB）。"""
    if "googleusercontent.com" not in img:
        return img
    return re.sub(r"=s0-w\d+-rw", "=s0-w1200-rw", img)


def attach(url: str, do_upload: bool = True, credit_hint: str = "") -> tuple[str, str]:
    """完整链路：解析 → 找图 → 下载 → 上传。返回（公网 URL, 署名）。

    do_upload=False 时只验证链路，不上传（dry-run 用，免得白占图床）。
    credit_hint：RSS 里带出来的媒体名（Google 的同意页/代理图只能给 "Google News"
    这种错的署名，用它顶掉）。
    """
    if not url or not url.startswith("http"):
        return "", ""
    final, page = resolve(url)
    if not page:
        return "", ""
    img, credit = find_image(page, final)
    if not img:
        return "", ""
    if "googleusercontent.com" in img:
        img = _upgrade_google(img)
        if credit.lower() in ("google news", "news.google.com") or not credit:
            credit = credit_hint
    if not credit:
        credit = credit_hint or urlparse(final).netloc.replace("www.", "")
    data, ext = download(img)
    if not data:
        return "", ""
    if not do_upload:
        return img, credit          # dry-run：只回报将要用的图
    pub = upload(data, ext)
    if not pub:
        return "", ""
    return pub, credit
