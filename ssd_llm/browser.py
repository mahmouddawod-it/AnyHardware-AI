"""Zero-dependency browser automation via CDP (Chrome DevTools Protocol).

Drives an installed Edge/Chrome (headless or visible) using only the standard
library: a minimal RFC 6455 WebSocket client plus JSON commands over the
remote-debugging endpoint. No Playwright / no browser download.
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path
from urllib.request import urlopen

HOST = "127.0.0.1"

VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 800
SCREENSHOT_QUALITY = 70

# Interactive elements we expose to the model as a numbered snapshot.
_SELECTOR = (
    'a[href], button, input:not([type="hidden"]), textarea, select, '
    '[role="button"], [role="link"], [role="textbox"], [role="searchbox"], '
    '[contenteditable="true"]'
)

_EDGE_CANDIDATES = [
    Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
    / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    Path(os.environ.get("ProgramFiles", "C:/Program Files"))
    / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
]

_SNAPSHOT_JS = r"""
(() => {
  const els = [...document.querySelectorAll(SELECTOR_PLACEHOLDER)]
    .filter(n => { const r = n.getBoundingClientRect(); return r.width >= 4 && r.height >= 4; })
    .slice(0, 45);
  const items = [];
  els.forEach((n, i) => {
    let name = n.getAttribute('aria-label') || '';
    if (!name && n.tagName === 'INPUT') {
      name = n.placeholder || n.value || n.type;
    }
    if (!name) name = (n.textContent || '').trim().slice(0, 60);
    const r = n.getBoundingClientRect();
    items.push({
      i,
      tag: n.tagName.toLowerCase(),
      role: n.getAttribute('role') || '',
      name,
      x: Math.round(r.x + r.width / 2),
      y: Math.round(r.y + r.height / 2),
    });
  });
  return { url: location.href, title: document.title, scrollY: Math.round(scrollY),
           items };
})()
"""

_POINT_JS = r"""
(() => {
  const els = [...document.querySelectorAll(SELECTOR_PLACEHOLDER)]
    .filter(n => { const r = n.getBoundingClientRect(); return r.width >= 4 && r.height >= 4; });
  const n = els[INDEX_PLACEHOLDER];
  if (!n) return { error: 'no element at that index' };
  if (n.tagName === 'A' && (n.target === '_blank' || n.target === '_new')) n.removeAttribute('target');
  n.scrollIntoView({ block: 'center', inline: 'center' });
  const r = n.getBoundingClientRect();
  return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2),
           tag: n.tagName.toLowerCase() };
})()
"""

_FOCUS_JS = r"""
(() => {
  const els = [...document.querySelectorAll(SELECTOR_PLACEHOLDER)]
    .filter(n => { const r = n.getBoundingClientRect(); return r.width >= 4 && r.height >= 4; });
  const n = els[INDEX_PLACEHOLDER];
  if (!n) return { error: 'no element at that index' };
  n.scrollIntoView({ block: 'center', inline: 'center' });
  n.focus();
  if (typeof n.select === 'function') { try { n.select(); } catch (e) {} }
  return { tag: n.tagName.toLowerCase() };
})()
"""

_FIRST_TEXT_INPUT_JS = r"""
(() => {
  const els = [...document.querySelectorAll(SELECTOR_PLACEHOLDER)]
    .filter(n => { const r = n.getBoundingClientRect(); return r.width >= 4 && r.height >= 4; });
  for (let i = 0; i < els.length; i++) {
    const t = els[i].tagName;
    if (t === 'INPUT' || t === 'TEXTAREA' || els[i].getAttribute('contenteditable') === 'true') return i;
  }
  return -1;
})()
"""

_BING_EXTRACT_JS = r"""
(() => {
  const out = [];
  document.querySelectorAll('li.b_algo').forEach(n => {
    const a = n.querySelector('h2 a') || n.querySelector('a');
    const p = n.querySelector('p');
    if (a) out.push(
      (a.textContent || '').trim().slice(0, 120) + ' | ' + a.href +
      (p ? ' | ' + (p.textContent || '').trim().slice(0, 200) : ''));
  });
  return out.slice(0, 8);
})()
"""


class WebSocketError(Exception):
    pass


class CDPError(Exception):
    pass


class BrowserError(Exception):
    pass


class WebSocket:
    """Minimal RFC 6455 client (no extensions, no fragmentation)."""

    def __init__(self, host: str, port: int, path: str, timeout: float = 30.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        headers = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(headers.encode("ascii"))
        response = self._read_until_headers()
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise WebSocketError(f"handshake failed: {response[:200]!r}")

    def _read_until_headers(self) -> bytes:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            data += chunk
        return data

    def _read_exact(self, n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise WebSocketError("connection closed")
            data += chunk
        return data

    def _send_frame(self, payload: bytes, opcode: int = 0x1) -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        mask = secrets.token_bytes(4)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(text.encode("utf-8"))

    def recv_frame(self, timeout: float | None = None) -> bytes | None:
        if timeout is not None:
            self.sock.settimeout(timeout)
        b1, b2 = self._read_exact(2)
        opcode = b1 & 0x0F
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exact(8))[0]
        masked = bool(b2 & 0x80)
        mask = self._read_exact(4) if masked else b""
        data = self._read_exact(length)
        if masked:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if opcode == 0x8:  # close
            return None
        if opcode == 0x9:  # ping -> pong
            self._send_frame(data, opcode=0xA)
            return self.recv_frame(timeout)
        return data

    def close(self) -> None:
        try:
            self._send_frame(b"", opcode=0x8)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class CDP:
    """Synchronous CDP session over a single WebSocket."""

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self._id = 0
        self._lock = threading.Lock()

    def call(self, method: str, params: dict | None = None,
             timeout: float | None = None) -> dict:
        with self._lock:
            self._id += 1
            mid = self._id
            self.ws.send_text(json.dumps(
                {"id": mid, "method": method, "params": params or {}}))
            while True:
                raw = self.ws.recv_frame(timeout)
                if raw is None:
                    raise CDPError("websocket closed")
                try:
                    msg = json.loads(raw.decode("utf-8"))
                except ValueError:
                    continue
                if msg.get("id") != mid:
                    continue
                if "error" in msg:
                    err = msg["error"]
                    raise CDPError(f"{err.get('message', err)}")
                return msg.get("result", {})

    def close(self) -> None:
        self.ws.close()


class BrowserSession:
    """One Edge/Chrome process driven through CDP."""

    def __init__(self, executable: Path, visible: bool = False,
                 profile_dir: Path | None = None):
        self.executable = executable
        self.visible = visible
        self.proc: subprocess.Popen | None = None
        self.cdp: CDP | None = None
        self.port: int | None = None
        self.profile_dir = profile_dir
        self._owns_profile = profile_dir is None
        self._items: list[dict] = []

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((HOST, 0))
            return s.getsockname()[1]

    def start(self) -> None:
        if self.cdp is not None:
            return
        if not self.executable.is_file():
            raise BrowserError(f"browser not found: {self.executable}")
        port = self._free_port()
        profile = self.profile_dir or Path(os.environ.get("TEMP", "/tmp")) / \
            f"ssd-llm-edge-{secrets.token_hex(4)}"
        if not self.visible:
            profile = self.profile_dir or profile
        args = [
            str(self.executable),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--headless=new" if not self.visible else "--window-size=1280,800",
            "--no-first-run", "--disable-default-apps", "--disable-extensions",
            "--mute-audio", "--disable-features=Translate",
            "about:blank",
        ]
        self.port = port
        self.profile_dir = profile
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, creationflags=creationflags)

        target = None
        for _ in range(50):
            if self.proc.poll() is not None:
                raise BrowserError("browser exited during startup")
            try:
                with urlopen(f"http://{HOST}:{port}/json/list", timeout=1.5) as r:
                    targets = json.loads(r.read().decode("utf-8"))
                for t in targets:
                    if t.get("type") == "page":
                        target = t
                        break
            except OSError:
                time.sleep(0.2)
                continue
            if target:
                break
        if not target:
            raise BrowserError("browser debug endpoint did not come up")
        path = target["webSocketDebuggerUrl"].split("://", 1)[-1]
        path = path.split("/", 1)[1] if "/" in path else "/"
        self.cdp = CDP(WebSocket(HOST, port, "/" + path))
        self.cdp.call("Page.enable")
        self.cdp.call("Runtime.enable")
        self.cdp.call("Emulation.setDeviceMetricsOverride", {
            "width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT,
            "deviceScaleFactor": 1, "mobile": False})

    def stop(self) -> None:
        if self.cdp is not None:
            try:
                self.cdp.close()
            except OSError:
                pass
            self.cdp = None
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if self._owns_profile and self.profile_dir is not None:
            try:
                shutil_rmtree(self.profile_dir)
            except OSError:
                pass
        self.port = None

    @property
    def running(self) -> bool:
        return self.cdp is not None and self.proc is not None and \
            self.proc.poll() is None

    def evaluate(self, script: str) -> tuple[object, str | None]:
        result = self.cdp.call("Runtime.evaluate", {
            "expression": script, "returnByValue": True}, timeout=30)
        if "exceptionDetails" in result:
            det = result["exceptionDetails"]
            desc = ((det.get("exception") or {}).get("description")
                    or det.get("text", "") or "")
            return None, f"JS error: {desc}"
        value = result.get("result", {}).get("value")
        return value, None

    def navigate(self, url: str) -> None:
        if "://" not in url:
            url = "https://" + url
        self.cdp.call("Page.navigate", {"url": url})
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                state, err = self.evaluate("document.readyState")
                if err is None and state in ("complete", "interactive"):
                    time.sleep(0.4)
                    state, err = self.evaluate("document.readyState")
                    if err is None and state == "complete":
                        loc, _ = self.evaluate("location.href")
                        if loc and loc != "about:blank":
                            return
            except OSError:
                pass
            time.sleep(0.3)

    def snapshot(self) -> str:
        value, err = self.evaluate(_SNAPSHOT_JS.replace("SELECTOR_PLACEHOLDER", json.dumps(_SELECTOR)))
        if err:
            return f"ERROR: {err}"
        data = value or {}
        lines = [f"URL: {data.get('url', '')}", f"Title: {data.get('title', '')}",
                 f"ScrollY: {data.get('scrollY', 0)}"]
        items = data.get("items") or []
        self._items = items
        for it in items:
            label = f"[{it['i']}] <{it['tag']}"
            if it.get("role"):
                label += f" role={it['role']}"
            label += f"> \"{it['name']}\" @({it['x']},{it['y']})"
            lines.append(label)
        if not items:
            lines.append("(no interactive elements found)")
        return "\n".join(lines)

    def screenshot(self) -> str:
        result = self.cdp.call("Page.captureScreenshot", {
            "format": "jpeg", "quality": SCREENSHOT_QUALITY,
            "captureBeyondViewport": False}, timeout=30)
        data = result.get("data")
        if not data:
            raise BrowserError("screenshot returned no data")
        return "data:image/jpeg;base64," + data

    def _resolve_index(self, query: str) -> int | None:
        self.snapshot()
        q = str(query).lower().strip()
        best: int | None = None
        best_score = 0
        for it in self._items:
            name = str(it.get("name", "")).lower()
            tag = it.get("tag", "")
            role = str(it.get("role", "")).lower()
            score = 0
            if name == q:
                score = 100
            elif name.startswith(q):
                score = 80
            elif q in name:
                score = 60
            elif name in q:
                score = 50
            if score == 0:
                continue
            if any(w in q for w in ("button", "btn", "submit")):
                if tag == "button":
                    score += 20
                elif "button" in role or (tag == "input" and role == "button"):
                    score += 15
            if "search" in q:
                if "search" in name or "بحث" in name:
                    score += 10
                elif tag in ("input", "textarea"):
                    score += 5
            if any(w in q for w in ("field", "input", "box")):
                if tag in ("input", "textarea"):
                    score += 10
            if score > best_score:
                best = it["i"]
                best_score = score
        if best is not None:
            return best
        if any(w in q for w in ("search", "submit", "button", "btn", "go")):
            for it in self._items:
                tag = it.get("tag", "")
                role = str(it.get("role", "")).lower()
                if (tag == "button" or (tag == "input" and role == "button")) \
                        and it.get("name"):
                    return it["i"]
        for it in self._items:
            if it.get("tag") in ("input", "textarea"):
                return it["i"]
        return None

    def _first_text_input_index(self) -> int | None:
        script = _FIRST_TEXT_INPUT_JS.replace(
            "SELECTOR_PLACEHOLDER", json.dumps(_SELECTOR))
        value, err = self.evaluate(script)
        if err:
            return None
        idx = int(value) if value is not None and int(value) >= 0 else None
        return idx

    def click_selector(self, selector: str) -> str:
        script = (
            "(() => { const n = document.querySelector(" +
            json.dumps(selector) + "); if (!n) return { error: 'no element for selector' };"
            " if (n.tagName === 'A' && (n.target === '_blank' || n.target === '_new'))"
            " n.removeAttribute('target');"
            " n.scrollIntoView({ block: 'center', inline: 'center' });"
            " const r = n.getBoundingClientRect();"
            " return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2),"
            "          tag: n.tagName.toLowerCase(), text: (n.textContent || '').trim().slice(0, 40) }; })()"
        )
        value, err = self.evaluate(script)
        if err:
            return f"ERROR: {err}"
        if not value or "error" in value:
            return f"ERROR: {value}"
        self._mouse_click(value["x"], value["y"])
        return f"clicked {value.get('text', '')} ({value.get('tag', 'element')})"

    def click(self, index_or_name) -> str:
        if isinstance(index_or_name, str):
            s = index_or_name.strip()
            if s.isdigit():
                return self.click(int(s))
            index = self._resolve_index(s)
            if index is None:
                return f"ERROR: could not find element '{index_or_name}'"
            return self.click(index)
        index = int(index_or_name)
        if index < 0:
            return "ERROR: index must be a non-negative number"
        script = _POINT_JS.replace("SELECTOR_PLACEHOLDER", json.dumps(_SELECTOR))
        script = script.replace("INDEX_PLACEHOLDER", str(index))
        value, err = self.evaluate(script)
        if err:
            return f"ERROR: {err}"
        if not value or "error" in value:
            return f"ERROR: {value}"
        self._mouse_click(value["x"], value["y"])
        return f"clicked [{index}] {value.get('tag', 'element')}"

    def type_text(self, index, text: str) -> str:
        if index is None or (isinstance(index, str)
                             and not index.strip().isdigit()):
            if isinstance(index, str):
                idx = self._resolve_index(index)
            else:
                idx = self._first_text_input_index()
            if idx is None:
                return "ERROR: no input element to type into"
            index = idx
        index = int(index)
        if index < 0:
            return "ERROR: index must be a non-negative number"
        script = _FOCUS_JS.replace("SELECTOR_PLACEHOLDER", json.dumps(_SELECTOR))
        script = script.replace("INDEX_PLACEHOLDER", str(index))
        value, err = self.evaluate(script)
        if err:
            return f"ERROR: {err}"
        if not value or "error" in value:
            return f"ERROR: {value}"
        self.cdp.call("Input.insertText", {"text": text})
        return f"typed {len(text)} chars into [{index}] {value.get('tag', 'element')}"

    def type_selector(self, selector: str, text: str) -> str:
        script = (
            "(() => { const n = document.querySelector(" +
            json.dumps(selector) + "); if (!n) return { error: 'no element for selector' };"
            " n.scrollIntoView({ block: 'center', inline: 'center' }); n.focus();"
            " if (typeof n.select === 'function') { try { n.select(); } catch (e) {} }"
            " return { tag: n.tagName.toLowerCase() }; })()"
        )
        value, err = self.evaluate(script)
        if err:
            return f"ERROR: {err}"
        if not value or "error" in value:
            return f"ERROR: {value}"
        self.cdp.call("Input.insertText", {"text": text})
        return f"typed {len(text)} chars into {value.get('tag', 'element')} ({selector})"

    def press_enter(self) -> str:
        for kind in ("keyDown", "keyUp"):
            self.cdp.call("Input.dispatchKeyEvent", {
                "type": kind, "key": "Enter", "code": "Enter",
                "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13})
        return "pressed Enter"

    def scroll(self, direction: str) -> str:
        top = "600" if direction == "down" else "-600"
        value, err = self.evaluate(f"window.scrollBy({{top: {top}, behavior: 'instant'}}); 'ok'")
        return f"scrolled {direction}"

    def run_js(self, script: str) -> str:
        value, err = self.evaluate(script)
        if err and "SyntaxError" in err:
            value, err = self.evaluate(f"(() => {{\n{script}\n}})()")
        if err:
            return f"ERROR: {err}"
        if value is None:
            return "undefined"
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)[:4000]
        return str(value)[:4000]

    def page_info(self) -> str:
        value, err = self.evaluate("JSON.stringify({url: location.href, title: document.title})")
        if err:
            return f"ERROR: {err}"
        text = str(value or "")
        if "chrome-error://" in text:
            return "ERROR: could not navigate (invalid or unreachable URL)"
        return text

    def search_bing(self, query: str) -> str:
        import urllib.parse
        if not query.strip():
            return "ERROR: empty query"
        # Arabic queries need mkt/setlang forced to ar-EG, otherwise Bing
        # interprets them as English and returns irrelevant results.
        if re.search(r"[\u0600-\u06FF]", query):
            url = ("https://www.bing.com/search?q=" + urllib.parse.quote(query)
                   + "&mkt=ar-EG&setlang=ar&cc=EG")
        else:
            url = ("https://www.bing.com/search?q=" + urllib.parse.quote(query)
                   + "&mkt=en-US&cc=EG")
        best = ""
        for attempt in range(2):
            self.navigate(url)
            time.sleep(1.5)
            current, _ = self.evaluate("location.href")
            if not current or current == "about:blank":
                time.sleep(1.5)
                continue
            value, err = self.evaluate(_BING_EXTRACT_JS)
            if err:
                return f"ERROR: {err}"
            items = value if isinstance(value, list) else []
            if len(items) > len(best.splitlines()):
                lines = []
                for i, item in enumerate(items, 1):
                    parts = item.split(" | ")
                    title = parts[0]
                    href = parts[1] if len(parts) > 1 else ""
                    snippet = parts[2] if len(parts) > 2 else ""
                    if not href.startswith("http") and snippet.startswith("http"):
                        href, snippet = snippet, href
                    if href.startswith("http"):
                        entry = f"{i}. {title}\nURL: {self._unredirect(href)}"
                    else:
                        entry = f"{i}. {title}"
                    if snippet and not snippet.startswith("http"):
                        entry += f"\nDescription: {snippet}"
                    lines.append(entry)
                best = "\n\n".join(lines)
            if len(items) >= 3:
                break
            time.sleep(1.0)
        return best or "no results found"

    @staticmethod
    def _unredirect(href: str) -> str:
        if "bing.com/ck/a" in href and "u=a1" in href:
            try:
                m = re.search(r"[?&]u=a1([^&]+)", href)
                if m:
                    import base64
                    b64 = m.group(1)
                    b64 += "=" * ((4 - len(b64) % 4) % 4)
                    decoded = base64.b64decode(b64).decode("utf-8", "replace")
                    if decoded.startswith("http"):
                        return decoded
            except Exception:
                pass
        return href

    def _mouse_click(self, x: int, y: int) -> None:
        base = {"x": x, "y": y, "button": "left", "clickCount": 1,
                "pointerType": "mouse"}
        self.cdp.call("Input.dispatchMouseEvent",
                      {**base, "type": "mousePressed"})
        self.cdp.call("Input.dispatchMouseEvent",
                      {**base, "type": "mouseReleased"})


def shutil_rmtree(path: Path) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)


_lock = threading.Lock()
_session: BrowserSession | None = None


def detect_browser() -> Path | None:
    for p in _EDGE_CANDIDATES:
        if p.is_file():
            return p
    return None


def browser_running() -> bool:
    return _session is not None and _session.running


def browser_info() -> dict:
    global _session
    with _lock:
        if _session is None or not _session.running or _session.proc is None:
            return {"running": False}
        return {"running": True, "pid": _session.proc.pid,
                "visible": _session.visible}


def get_browser(visible: bool = False) -> BrowserSession:
    global _session
    with _lock:
        if _session is not None and _session.running:
            if _session.visible != visible:
                _session.stop()
            else:
                return _session
        exe = detect_browser()
        if exe is None:
            raise BrowserError("no browser found (looked for Edge and Chrome)")
        _session = BrowserSession(exe, visible=visible)
        _session.start()
        return _session


def stop_browser() -> None:
    global _session
    with _lock:
        session, _session = _session, None
    if session is not None:
        session.stop()
