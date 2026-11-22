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
import json
import logging
import os
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

REPO = "tangjun937/bimi2x"
BRANCH = "main"
MEDIA_DIR = "media"
KEEP_DAYS = 3
MAX_DELETES = 15


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
