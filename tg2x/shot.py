"""文章图表截图：临时开一个无头 Edge，截完立刻关（不常驻、用完不占内存）。

为什么要它：很多站点的图表是 JS/canvas 渲染的，页面里没有 <img>，
og:image 又只是题图。这里用 CDP 找页面上最大的 img/canvas/svg，直接截那块。

失败一律返回 b""（调用方照常发文字）。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request

log = logging.getLogger("tg2x")

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)
PORT = 9333
MIN_W, MIN_H = 480, 220          # 太小的图不当图表

_FIND_JS = """
(() => {
  const CH = /chart|graph|plot|highchart|tradingview|apexchart|echarts|canvasjs|gspread/i;
  const name = e => String((e.className && e.className.baseVal !== undefined)
                           ? e.className.baseVal : (e.className || "")) + " " + (e.id || "");
  const chartish = e => { let n = e, d = 0;
    while (n && d < 5) { if (CH.test(name(n))) return true; n = n.parentElement; d++; }
    return false; };
  const cands = [...document.querySelectorAll('canvas,svg')].map(e => {
    const r = e.getBoundingClientRect();
    return {tag: e.tagName, x: r.x + window.scrollX, y: r.y + window.scrollY,
            w: r.width, h: r.height, ok: chartish(e)};
  }).filter(o => o.w >= %d && o.h >= %d && o.y >= 0 && o.ok);
  cands.sort((a, b) => (b.w * b.h) - (a.w * a.h));
  return JSON.stringify(cands[0] || null);
})()
""" % (MIN_W, MIN_H)


def _edge() -> str:
    for p in EDGE_CANDIDATES:
        if os.path.exists(p):
            return p
    return ""


def _http(url: str, method: str = "GET", timeout: int = 3) -> str:
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return ""


def _wait_ws(timeout: float = 12.0) -> str:
    end = time.time() + timeout
    while time.time() < end:
        txt = _http(f"http://127.0.0.1:{PORT}/json/version", timeout=2)
        if txt:
            try:
                return json.loads(txt).get("webSocketDebuggerUrl", "")
            except Exception:
                pass
        time.sleep(0.3)
    return ""


def _new_page(url: str) -> str:
    from urllib.parse import quote
    txt = _http(f"http://127.0.0.1:{PORT}/json/new?{quote(url, safe=':/?&=')}",
                method="PUT", timeout=8)
    try:
        return json.loads(txt).get("webSocketDebuggerUrl", "")
    except Exception:
        return ""


class _CDP:
    def __init__(self, ws_url: str):
        import websocket            # websocket-client，本机已有
        self.ws = websocket.create_connection(ws_url, timeout=30)
        self.i = 0

    def call(self, method: str, **params):
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.i:
                return msg.get("result") or {}

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def _capture_once(url: str, wait: float = 6.0, timeout: int = 40) -> bytes:
    """截页面里最大的一块图（图表/表格）。失败返回 b""。"""
    exe = _edge()
    if not exe or not url.startswith("http"):
        return b""
    profile = tempfile.mkdtemp(prefix="bimi2x-shot-")
    args = [exe, "--headless=new", f"--remote-debugging-port={PORT}",
            f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
            "--disable-gpu", "--hide-scrollbars", "--mute-audio",
            "--remote-allow-origins=*",
            "--window-size=1400,2400", "about:blank"]
    proxy = (os.environ.get("TG2X_PROXY") or "").strip()
    if proxy:
        args.insert(1, f"--proxy-server={proxy}")
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws = _wait_ws()
        if not ws:
            log.debug("截图失败：Edge 调试端口没起来")
            return b""
        page_ws = _new_page(url)
        if not page_ws:
            log.debug("截图失败：开不了新标签")
            return b""
        cdp = _CDP(page_ws)
        try:
            cdp.call("Page.enable")
            time.sleep(wait)                    # 等首屏脚本
            res = cdp.call("Runtime.evaluate", expression=_FIND_JS, returnByValue=True)
            raw = ((res.get("result") or {}).get("value")) or ""
            box = json.loads(raw) if raw else None
            if not box:
                log.debug("截图失败：页面上没有够大的图")
                return b""
            # 滚到那块图的位置，触发懒加载/画布绘制，再等一会儿重新量
            cdp.call("Runtime.evaluate",
                     expression="window.scrollTo(0, %d);" % max(0, int(box["y"]) - 120),
                     returnByValue=True)
            time.sleep(2.5)
            res2 = cdp.call("Runtime.evaluate", expression=_FIND_JS, returnByValue=True)
            raw2 = ((res2.get("result") or {}).get("value")) or ""
            box2 = json.loads(raw2) if raw2 else None
            if box2 and box2["w"] * box2["h"] >= box["w"] * box["h"] * 0.5:
                box = box2
            clip = {"x": max(0, box["x"]), "y": max(0, box["y"]),
                    "width": min(box["w"], 1400), "height": min(box["h"], 2000),
                    "scale": 1}
            shot = cdp.call("Page.captureScreenshot", format="png", clip=clip,
                            captureBeyondViewport=True)
            data = shot.get("data") or ""
            if not data:
                return b""
            out = base64.b64decode(data)
            if len(out) < 15_000:               # 太小多半是空白
                return b""
            if _too_blank(out):                 # 纯白/纯色：图表没画出来
                log.debug("截图太素（图表没渲染）")
                return b""
            return out
        finally:
            cdp.close()
    except Exception as e:  # noqa: BLE001
        log.debug("截图异常：%s", str(e)[:120])
        return b""
    finally:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        shutil.rmtree(profile, ignore_errors=True)   # 用完就关、临时目录也删掉


def capture(url: str, wait: float = 6.0, timeout: int = 40) -> bytes:
    """截一张图；第一次空白/失败就再开一次浏览器重试（用完都关掉）。"""
    for attempt in (1, 2):
        data = _capture_once(url, wait=wait + (1.5 if attempt == 2 else 0), timeout=timeout)
        if data:
            return data
        log.debug("截图第 %d 次失败", attempt)
    return b""
