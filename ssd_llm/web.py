from __future__ import annotations

import http.client
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from .browser import browser_info, browser_running, get_browser, stop_browser
from .planner import discover_machine, plan_run, RunPlan

HOST = "127.0.0.1"
DEFAULT_PORT = 8300
CONFIG_DIR = Path.home() / ".ssd-llm"
CONFIG_FILE = CONFIG_DIR / "web.json"
CHATS_DIR = CONFIG_DIR / "chats"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = PROJECT_ROOT / "models"
STATIC_DIR = Path(__file__).resolve().parent / "static"
AGENT_MAX_ITERATIONS = 18
AGENT_TIMEOUT_SECONDS = 1500

_BROWSER_INTENT_RE = re.compile(
    r"افتح|المتصفح|تصفح|موقع|جوجل|جوجول|غوغل|متصفح|اذهب|روح|شوف"
    r"|\b(open|browser|google|visit|go to|navigate to)\b", re.IGNORECASE)
TOOL_TIMEOUT_SECONDS = 30

DEFAULT_SETTINGS = {
    "engine": "",
    "model": "",
    "threads": "auto",
    "context": "auto",
    "batch": "auto",
    "temperature": 0.7,
    "max_tokens": 512,
    "system_prompt": "",
    "workspace": "",
    "browser_visible": False,
}


def _to_int_or_none(value: object) -> int | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("", "auto"):
        return None
    return int(text)


def resolve_plan(settings: dict, machine=None) -> RunPlan:
    if machine is None:
        machine = discover_machine()
    return plan_run(
        machine,
        threads=_to_int_or_none(settings.get("threads")),
        context=_to_int_or_none(settings.get("context")),
        batch=_to_int_or_none(settings.get("batch")),
    )


def detect_engine() -> str:
    exe = shutil.which("llama-server")
    if exe:
        return str(Path(exe))
    pkg_dir = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    if pkg_dir.is_dir():
        for p in sorted(pkg_dir.glob("ggml.llamacpp*/**/llama-server.exe")):
            return str(p)
    return ""


def _is_mmproj(path: Path) -> bool:
    return path.name.lower().startswith("mmproj-")


def _common_prefix_len(a: str, b: str) -> int:
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    return i


def _mmproj_compatible(other: str, key: str) -> bool:
    a = other.split("-")
    b = key.split("-")
    if not a or not b or a[0] != b[0]:
        return False
    return len(a) >= 2 and len(b) >= 2 and a[1] == b[1]


def _find_mmproj(model: Path) -> Path | None:
    if not model.parent.is_dir():
        return None
    candidates = [p for p in model.parent.glob("mmproj-*.gguf")]
    if not candidates:
        return None
    key = model.stem
    best = None
    best_len = -1
    for p in candidates:
        other = p.stem[len("mmproj-"):]
        if not _mmproj_compatible(other, key):
            continue
        n = _common_prefix_len(other, key)
        if n > best_len:
            best, best_len = p, n
    return best


def build_server_command(engine: Path, model: Path, plan: RunPlan, *,
                         port: int, api_key: str, mmproj: Path | None = None) -> list[str]:
    cmd = [str(engine), "-m", str(model), "--host", HOST, "--port", str(port),
           "-c", str(plan.context), "-b", str(plan.batch), "-t", str(plan.threads),
           "-ngl", str(plan.gpu_layers), "--no-ui", "--api-key", api_key]
    if mmproj is not None:
        # Qwen-VL models need >=1024 image tokens for reliable grounding.
        cmd += ["--mmproj", str(mmproj), "--image-min-tokens", "1024"]
    return cmd


def build_chat_body(messages: list, settings: dict) -> dict:
    return {
        "model": str(settings.get("model")),
        "messages": messages,
        "stream": True,
        "temperature": float(settings.get("temperature", 0.7)),
        "max_tokens": int(settings.get("max_tokens", 512)),
        "cache_prompt": True,
    }


AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "Execute Python code in an isolated subprocess (stdlib only, 30s timeout). "
                           "Use for computation, testing code, and data processing.",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string", "description": "Python source code"}},
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file from the workspace directory.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "relative path inside workspace"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write text content to a file inside the workspace directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "relative path inside workspace"},
                    "content": {"type": "string", "description": "file content"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List the entries of a directory inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "relative path (default '.')"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for current information and return the top results.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "search query"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "navigate",
            "description": "Open a URL in the browser (adds https:// if missing). Returns the page URL and title.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "URL to open"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_snapshot",
            "description": "Get a numbered list of the visible interactive elements on the current page. "
                           "Call this to observe the page before using click_element or type_text.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click_element",
            "description": "Click the page element. Provide 'index' (from browser_snapshot), an element 'name' from the snapshot, or a CSS 'selector'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "number", "description": "element index from browser_snapshot"},
                    "name": {"type": "string", "description": "element text/name from browser_snapshot"},
                    "selector": {"type": "string", "description": "optional CSS selector (e.g. '[role=button]')"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Type text into an input element. Provide either 'index' (from browser_snapshot) or a CSS 'selector'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "number", "description": "element index from browser_snapshot"},
                    "selector": {"type": "string", "description": "optional CSS selector of the input"},
                    "text": {"type": "string", "description": "text to type"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scroll",
            "description": "Scroll the page. Use direction 'down' or 'up'.",
            "parameters": {
                "type": "object",
                "properties": {"direction": {"type": "string", "enum": ["down", "up"]}},
                "required": ["direction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_js",
            "description": "Execute a JavaScript expression in the page and return its value.",
            "parameters": {
                "type": "object",
                "properties": {"script": {"type": "string", "description": "JavaScript expression"}},
                "required": ["script"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_screenshot",
            "description": "Capture a screenshot of the current page as an image you can see.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_enter",
            "description": "Press the Enter key (submit the focused search box / form).",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]


def _workspace(settings: dict) -> Path:
    ws = (settings.get("workspace") or "").strip()
    return Path(ws).resolve() if ws else PROJECT_ROOT


def _safe_path(workspace: Path, rel: str) -> Path:
    base = workspace.resolve()
    p = (base / rel).resolve()
    if p != base and base not in p.parents:
        raise ValueError(f"path escapes workspace: {rel}")
    return p


def _run_python(code: str, workspace: Path) -> str:
    if not code.strip():
        return "ERROR: empty code"
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-u", "-c", code],
            capture_output=True, text=True, timeout=TOOL_TIMEOUT_SECONDS, cwd=workspace,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: execution timed out after {TOOL_TIMEOUT_SECONDS}s"
    except OSError as exc:
        return f"ERROR: could not start python: {exc}"
    out = (proc.stdout or "") + (proc.stderr or "")
    return (out or "(no output)")[-4000:]


def _web_search(query: str) -> str:
    if not query.strip():
        return "ERROR: empty query"
    url = "https://duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        html = urlopen(req, timeout=TOOL_TIMEOUT_SECONDS).read().decode("utf-8", "replace")
    except Exception as exc:
        html = ""
    links = re.findall(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html)
    snips = re.findall(r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', html)
    results = []
    for i, (href, title) in enumerate(links[:5]):
        title = re.sub(r"<[^>]+>", "", title)
        snippet = re.sub(r"<[^>]+>", "", snips[i]) if i < len(snips) else ""
        results.append(f"{i + 1}. {title}\nURL: {href}\nDescription: {snippet}")
    if results:
        return "\n\n".join(results)
    # DuckDuckGo is bot-blocked here; fall back to a real browser search (Bing).
    try:
        from .browser import get_browser
        return get_browser(visible=False).search_bing(query)
    except Exception as exc:
        return f"ERROR: web search unavailable: {exc}"


def _to_index(value) -> int | None:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    try:
        idx = int(value)
    except (TypeError, ValueError):
        return None
    if idx < 0:
        return None
    return idx


def _browser_snapshot(visible: bool) -> tuple[str, str | None]:
    browser = get_browser(visible=visible)
    try:
        text = browser.snapshot()
        image = browser.screenshot()
    except Exception as exc:
        return f"ERROR: {exc}", None
    return text, image


def _browser_screenshot(visible: bool) -> tuple[str, str | None]:
    try:
        image = get_browser(visible=visible).screenshot()
    except Exception as exc:
        return f"ERROR: {exc}", None
    return "screenshot captured", image


_ARG_ALIASES = {
    "path": ["path", "f", "file", "filepath", "filename", "name", "d", "dir"],
    "content": ["content", "c", "text", "code", "data"],
    "query": ["query", "q", "search", "terms", "search_terms", "search_term", "text"],
    "url": ["url", "u", "link", "href", "site", "target"],
    "index": ["index", "el", "element", "id", "i", "idx"],
    "text": ["text", "t", "txt", "input", "content", "value"],
    "code": ["code", "c", "script", "expr", "expression", "python"],
    "script": ["script", "code", "expression", "expr", "js"],
    "direction": ["direction", "dir"],
}


def _norm_args(args: dict) -> dict:
    out = dict(args or {})
    keys = {k: v for k, v in out.items()}
    for canonical, aliases in _ARG_ALIASES.items():
        if canonical in keys:
            continue
        for a in aliases[1:]:
            if a in keys:
                out[canonical] = keys[a]
                break
    return out


def _clean_index(value) -> str | int | None:
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    s = value.strip()
    if s.lower().startswith("i") and s[1:].isdigit():
        return int(s[1:])
    if s.startswith("#") and s[1:].isdigit():
        return int(s[1:])
    if s.isdigit():
        return int(s)
    return s or None


_URL_RE = re.compile(r"https?://[^\s\u0600-\u06FF'\"]+")


def _clean_url(value) -> str:
    if not isinstance(value, str):
        return ""
    s = value.strip().strip('"').strip("'")
    m = _URL_RE.match(s)
    if m:
        return m.group(0)
    m = re.match(r"https?://\S+", s)
    if m:
        return m.group(0)
    return s


def _execute_tool(name: str, args: dict, workspace: Path,
                  browser_visible: bool = False) -> tuple[str, str | None]:
    try:
        args = _norm_args(args)
        set_activity("tool", tool=name, args=args, detail="")
        if name == "run_python":
            return _run_python(args.get("code") or "", workspace), None
        if name == "read_file":
            path = _safe_path(workspace, args.get("path") or "")
            if not path.is_file():
                return f"ERROR: file not found: {path}", None
            return path.read_text("utf-8", errors="replace")[-4000:], None
        if name == "write_file":
            path = _safe_path(workspace, args.get("path") or "")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args.get("content") or "", "utf-8")
            return f"wrote {len(args.get('content') or '')} bytes to {path}", None
        if name == "list_files":
            path = _safe_path(workspace, args.get("path") or ".")
            if not path.is_dir():
                return f"ERROR: not a directory: {path}", None
            entries = []
            for x in sorted(path.iterdir()):
                entries.append(f"{x.name}/" if x.is_dir() else x.name)
            return ("\n".join(entries) if entries else "(empty directory)"), None
        if name == "web_search":
            return _web_search(args.get("query") or ""), None
        if name == "navigate":
            try:
                browser = get_browser(visible=browser_visible)
                browser.navigate(_clean_url(args.get("url") or ""))
                return browser.page_info(), None
            except Exception as exc:
                return f"ERROR: {exc}", None
        if name == "browser_snapshot":
            return _browser_snapshot(browser_visible)
        if name == "browser_screenshot":
            return _browser_screenshot(browser_visible)
        if name == "click_element":
            selector = str(args.get("selector") or args.get("css") or "").strip()
            target = _clean_index(args.get("index"))
            if target is None and str(args.get("name") or "").strip():
                target = str(args.get("name")).strip()
            if selector:
                if re.fullmatch(r"#\d+", selector):
                    target = int(selector[1:])
                else:
                    try:
                        return get_browser(visible=browser_visible).click_selector(selector), None
                    except Exception as exc:
                        return f"ERROR: {exc}", None
            if target is None:
                return "ERROR: click_element needs an index or element name", None
            try:
                return get_browser(visible=browser_visible).click(target), None
            except Exception as exc:
                return f"ERROR: {exc}", None
        if name == "type_text":
            text = str(args.get("text") or "")
            if not text.strip():
                return "ERROR: type_text needs text", None
            try:
                selector = str(args.get("selector") or args.get("css") or "").strip()
                if selector and not re.fullmatch(r"#\d+", selector):
                    return get_browser(visible=browser_visible).type_selector(selector, text), None
                index = _clean_index(args.get("index"))
                if selector and re.fullmatch(r"#\d+", selector):
                    index = int(selector[1:])
                return get_browser(visible=browser_visible).type_text(index, text), None
            except Exception as exc:
                return f"ERROR: {exc}", None
        if name == "scroll":
            direction = str(args.get("direction") or "down")
            if direction not in ("down", "up"):
                return "ERROR: direction must be 'down' or 'up'", None
            try:
                return get_browser(visible=browser_visible).scroll(direction), None
            except Exception as exc:
                return f"ERROR: {exc}", None
        if name == "run_js":
            try:
                script = (args.get("script") or args.get("expression")
                          or args.get("code") or "").strip()
                if not script:
                    return "ERROR: run_js needs a 'script' argument", None
                return get_browser(visible=browser_visible).run_js(script), None
            except Exception as exc:
                return f"ERROR: {exc}", None
        if name == "press_enter":
            try:
                return get_browser(visible=browser_visible).press_enter(), None
            except Exception as exc:
                return f"ERROR: {exc}", None
        return f"ERROR: unknown tool: {name}", None
    except Exception as exc:
        return f"ERROR: {exc}", None


def _call_completion(port: int, api_key: str, messages: list, settings: dict,
                     tools: list | None = None, timeout: int = 600) -> dict:
    body = {
        "model": str(settings.get("model")),
        "messages": messages,
        "stream": False,
        "temperature": float(settings.get("temperature", 0.7)),
        "max_tokens": int(settings.get("max_tokens", 512)),
        "cache_prompt": True,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    conn = http.client.HTTPConnection(HOST, port, timeout=timeout)
    try:
        conn.request("POST", "/v1/chat/completions", json.dumps(body), headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        })
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", "replace")
    finally:
        conn.close()
    if resp.status != 200:
        raise RuntimeError(f"llama-server: {resp.status} {raw[:500]}")
    return json.loads(raw)


def _stream_completion(port: int, api_key: str, messages: list, settings: dict,
                       timeout: int = 600):
    """Stream a chat completion as a generator of content deltas.

    Uses ``cache_prompt`` so llama.cpp reuses its KV cache for the message
    prefix, making multi-turn / multi-iteration prompts nearly free after the
    first pass. Closing the generator early aborts server-side generation.
    """
    body = {
        "model": str(settings.get("model")),
        "messages": messages,
        "stream": True,
        "temperature": float(settings.get("temperature", 0.7)),
        "max_tokens": int(settings.get("max_tokens", 512)),
        "cache_prompt": True,
    }
    conn = http.client.HTTPConnection(HOST, port, timeout=timeout)
    try:
        conn.request("POST", "/v1/chat/completions", json.dumps(body), headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        })
        resp = conn.getresponse()
    except OSError as exc:
        conn.close()
        raise RuntimeError(f"llama-server unreachable: {exc}") from exc
    if resp.status != 200:
        raw = resp.read().decode("utf-8", "replace")
        conn.close()
        raise RuntimeError(f"llama-server: {resp.status} {raw[:500]}")
    try:
        for raw in resp:
            if AGENT_STOP_EVENT.is_set():
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except ValueError:
                continue
            delta = ((obj.get("choices") or [{}])[0].get("delta") or {})
            content = delta.get("content")
            if content:
                yield content
    finally:
        conn.close()


def _scan_first_tool_call(text: str) -> tuple[str, dict] | None:
    """Return the first complete ``TOOL: {...}`` JSON in a partially streamed
    buffer (without needing the whole response). ``None`` if none is complete
    yet, which lets the caller keep streaming or stop early."""
    idx = text.find("TOOL:")
    while idx != -1:
        start = text.find("{", idx + 5)
        if start == -1:
            return None
        depth = 0
        in_str = False
        esc = False
        end = -1
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end == -1:
            return None
        try:
            obj = json.loads(text[start:end + 1])
        except ValueError:
            idx = text.find("TOOL:", end)
            continue
        name = obj.get("name")
        if name:
            return (name, obj.get("arguments") or {})
        return None
    return None


def _agent_prompt(settings: dict, browser_mode: bool = False) -> str:
    sys = (settings.get("system_prompt") or "").strip()
    tools = AGENT_TOOLS
    if browser_mode:
        tools = [t for t in tools
                 if t["function"]["name"] not in
                 ("read_file", "write_file", "list_files", "run_python")]
    docs = "\n".join(
        f"- {t['function']['name']}: {t['function']['description']}"
        for t in tools
    )
    base = (
        "You are an AI agent running locally with access to tools.\n"
        "Your final answer MUST be written in the exact same language as the user's "
        "latest question (Arabic if the user wrote Arabic).\n"
        f"Available tools:\n{docs}\n\n"
        "When you need to use a tool, output ONLY this single line and nothing else:\n"
        'TOOL: {"name": "<tool_name>", "arguments": {...}}\n'
        "After the tool executes you will receive a TOOL_RESULT message with the real output. "
        "Then either call another tool or give the final answer in plain text.\n"
        "To control the browser: call navigate to open a page, then browser_snapshot to "
        "get numbered elements, then click_element/type_text using those numbers or names. "
        "After typing in a search box, call press_enter. Use browser_screenshot to see the "
        "page. run_js takes its code in the 'script' argument. Keep tool arguments valid JSON.\n"
        "IMPORTANT rules: use only exact URLs returned by web_search; never invent, guess or "
        "edit URLs. After web_search, to extract a list (like company names), navigate to one "
        "of the result URLs and call browser_snapshot. If a tool returns ERROR, do NOT retry "
        "the same call - adapt or give the final answer.\n"
        "Google often blocks automated searches with a CAPTCHA ('sorry' page). If you hit a "
        "captcha or see 'sorry/index', switch to web_search or navigate to bing.com or "
        "duckduckgo.com instead. You may also call tools one at a time. "
        "Always reply in the same language the user used (Arabic if the user wrote Arabic).\n"
        "IMPORTANT: do ONLY what the user asked for. Complete the requested action(s), give "
        "a concise answer in the user's language, then STOP - do not keep browsing, clicking, "
        "searching or running extra steps on your own, and do not invent extra tasks. Wait "
        "for the user's next message."
    )
    return (sys + "\n\n" + base) if sys else base


def _parse_tool_lines(content: str) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []
    for raw in content.splitlines():
        line = raw.strip()
        if not line.startswith("TOOL:"):
            calls.extend(_parse_positional_call(line))
            continue
        payload = line[len("TOOL:"):].strip()
        if payload.startswith("```"):
            payload = payload.lstrip("`").lstrip("json").strip()
            payload = payload.rstrip("`").strip()
        try:
            obj = json.loads(payload)
        except ValueError:
            continue
        name = obj.get("name")
        args = obj.get("arguments") or {}
        if name:
            calls.append((name, args))
    return calls


_TOOL_NAMES = {t["function"]["name"] for t in AGENT_TOOLS}
_POSITIONAL_RE = re.compile(r"^\s*(\w+)\s*\((.*)\)\s*$", re.S)


def _extract_positional_args(inner: str) -> list:
    args: list = []
    i, n = 0, len(inner)
    while i < n:
        c = inner[i]
        if c in "\"'":
            quote = c
            i += 1
            start = i
            while i < n and inner[i] != quote:
                i += 1
            args.append(inner[start:i])
            i += 1
        elif c == "-" or c.isdigit():
            start = i
            while i < n and (inner[i].isdigit() or inner[i] in ".-"):
                i += 1
            tok = inner[start:i]
            try:
                args.append(int(tok))
            except ValueError:
                args.append(float(tok))
        elif inner[i:i + 4].lower() == "true":
            args.append(True)
            i += 4
        elif inner[i:i + 5].lower() == "false":
            args.append(False)
            i += 5
        elif inner[i:i + 4].lower() == "null":
            args.append(None)
            i += 4
        else:
            i += 1
    return args


def _args_from_positional(name: str, positionals: list) -> dict:
    if name == "type_text":
        if len(positionals) == 1:
            return {"text": positionals[0]}
        if len(positionals) >= 2:
            return {"index": positionals[0], "text": positionals[1]}
    if name == "write_file" and len(positionals) >= 2:
        return {"path": positionals[0], "content": positionals[1]}
    keys = {
        "navigate": ["url"], "click_element": ["index"],
        "scroll": ["direction"], "run_js": ["script"],
        "web_search": ["query"], "read_file": ["path"],
        "write_file": ["path"], "list_files": ["path"],
        "run_python": ["code"],
    }
    keys_for = keys.get(name, [])
    return {keys_for[i]: positionals[i]
            for i in range(min(len(positionals), len(keys_for)))}


def _parse_positional_call(line: str) -> list[tuple[str, dict]]:
    m = _POSITIONAL_RE.match(line)
    if not m:
        return []
    name = m.group(1)
    if name not in _TOOL_NAMES:
        return []
    args = _extract_positional_args(m.group(2))
    return [(name, _args_from_positional(name, args))]


_TOOL_INTENT_RE = re.compile(
    r"\b(?:navigate|browser_snapshot|browser_screenshot|click_element|"
    r"type_text|scroll|run_js|web_search|press_enter|read_file|write_file|"
    r"list_files|run_python)\s*\("
    r"|\b(?:use|using|call|called|will|to)\s+"
    r"(?:navigate|click_element|type_text|run_js|web_search|browser_snapshot)\b")


def _looks_like_tool_intent(content: str) -> bool:
    return bool(_TOOL_INTENT_RE.search(content))


CORRECTION = (
    "Your previous reply did not actually call a tool. Call tools using exactly "
    "this format, one tool per line, nothing else:\n"
    'TOOL: {"name": "navigate", "arguments": {"url": "https://..."}}\n'
    'TOOL: {"name": "type_text", "arguments": {"index": 0, "text": "..."}}\n'
    "Use indices or element names from browser_snapshot. Wait for the "
    "TOOL_RESULT before calling the next tool."
)


def _trim_old_images(history: list) -> None:
    for m in history:
        c = m.get("content")
        if isinstance(c, list) and any(
                p.get("type") == "image_url" for p in c):
            text = "".join(p.get("text", "") for p in c
                           if p.get("type") == "text")
            m["content"] = text or "[previous screenshot]"


def _trim_agent_history(history: list, keep_last: int = 12) -> None:
    if len(history) <= keep_last + 3:
        return
    total = sum(len(str(m.get("content") or "")) for m in history)
    if total < 45000:
        return
    system_msgs = [m for m in history if m.get("role") == "system"]
    rest = [m for m in history if m.get("role") != "system"]
    first_user = next((m for m in rest if m.get("role") == "user"), None)
    keep = [first_user] if first_user else []
    keep += rest[-keep_last:]
    history[:] = system_msgs + keep


def _run_agent_loop(messages: list, settings: dict, port: int, api_key: str,
                    workspace: Path, sse) -> None:
    history = [dict(m) for m in messages]
    browser_visible = bool(settings.get("browser_visible"))
    vision = False
    model_path = settings.get("model") or ""
    if model_path and Path(model_path).is_file():
        vision = _find_mmproj(Path(model_path)) is not None
    browser_mode = False
    if not (history and history[0].get("role") == "system"):
        history.insert(0, {"role": "system", "content": _agent_prompt(settings)})
    first_user = next((m.get("content", "") for m in history
                       if m.get("role") == "user"), "")
    if isinstance(first_user, str) and _BROWSER_INTENT_RE.search(first_user):
        browser_mode = True
        history[0]["content"] = _agent_prompt(settings, browser_mode=True)
        history.insert(1, {"role": "system", "content": (
            "The user's request asks to open a browser or visit a website. Do ONLY "
            "what the user literally asked for. If they asked to open a site, call "
            "navigate to it (google.com if none specified), then browser_snapshot, "
            "then reply briefly with what is on the page.\n"
            "Do NOT continue browsing, clicking, scrolling or searching on your own. "
            "Do NOT invent extra tasks (like making ranking lists, extra searches, or "
            "more results) unless the user explicitly asked for them. Once the requested "
            "action is done, give a short confirmation in the same language the user "
            "wrote in and STOP - wait for the user's next message.\n"
            "Use ONLY browser tools and web_search - the file tools are NOT available.")})
    recent: list[tuple] = []
    deadline = time.time() + AGENT_TIMEOUT_SECONDS
    for i in range(AGENT_MAX_ITERATIONS):
        if AGENT_STOP_EVENT.is_set():
            break
        if time.time() > deadline:
            sse({"choices": [{"delta": {"content":
                 "\n\n[Agent stopped: timed out]."}}]})
            return
        if i == AGENT_MAX_ITERATIONS // 2:
            history.append({"role": "user", "content": (
                "You have used enough tool steps. Give the FINAL answer to the "
                "user's actual question now, in their language, using only what "
                "you already found. Do not call more tools.")})
        _trim_agent_history(history)
        remaining = int(deadline - time.time())
        set_activity("thinking", detail="reasoning")
        content = ""
        pending = ""
        gen = _stream_completion(port, api_key, history, settings,
                                 timeout=max(60, remaining))
        try:
            for delta in gen:
                content += delta
                # Execute the tool as soon as its JSON is complete instead of
                # waiting for the model to finish generating (kills the lag on
                # rambling responses and streams the answer live at the end).
                if _scan_first_tool_call(content) is not None:
                    gen.close()
                    break
                pending += delta
                if "\n" in pending:
                    lines, pending = pending.rsplit("\n", 1)
                    for ln in lines.split("\n"):
                        if ln.strip() and not ln.strip().startswith("TOOL:"):
                            sse({"choices": [{"delta": {"content": ln + "\n"}}]})
        finally:
            gen.close()
        if AGENT_STOP_EVENT.is_set():
            break
        if pending.strip() and not pending.strip().startswith("TOOL:"):
            sse({"choices": [{"delta": {"content": pending}}]})
        calls = _parse_tool_lines(content)
        if not calls:
            if _looks_like_tool_intent(content):
                history.append({"role": "assistant", "content": content})
                history.append({"role": "user", "content": CORRECTION})
                continue
            return
        history.append({"role": "assistant", "content": content})
        observations = []
        nudge = False
        for name, args in calls:
            if AGENT_STOP_EVENT.is_set():
                break
            sse({"type": "tool_call", "name": name, "args": args})
            if browser_mode and name in ("read_file", "write_file",
                                         "list_files", "run_python"):
                result = (f"ERROR: tool '{name}' is not available in browser mode. "
                          "Use only navigate, browser_snapshot, click_element, "
                          "type_text, scroll, run_js, press_enter or web_search.")
                image = None
            else:
                result, image = _execute_tool(name, args, workspace, browser_visible)
            preview = result[:800]
            if image:
                preview += "\n[screenshot attached]"
            sse({"type": "tool_result", "name": name, "preview": preview,
                 "has_image": bool(image), "image": image})
            observations.append((f"[{name}] {result}", image))
            recent.append((name, result))
            recent = recent[-6:]
            if any(recent.count(pair) >= 3 for pair in set(recent)):
                nudge = True
                break
        if AGENT_STOP_EVENT.is_set():
            break
        if nudge:
            history.append({"role": "user", "content": (
                "You keep repeating the same tool call without getting new "
                "information. Stop browsing in circles. Either run a web_search "
                "with a new query, or give the FINAL answer now using only the "
                "real names you already saw in the tool results.")})
            continue
        for text, image in observations:
            if image and vision:
                _trim_old_images(history)
                history.append({"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": image}},
                    {"type": "text", "text": "TOOL_RESULT:\n" + text},
                ]})
            elif image:
                history.append({"role": "user", "content": (
                    "TOOL_RESULT:\n" + text +
                    "\n[screenshot taken, but this model is text-only; "
                    "use the text snapshot instead]")})
            else:
                history.append({"role": "user",
                                "content": "TOOL_RESULT:\n" + text})
    if AGENT_STOP_EVENT.is_set():
        sse({"choices": [{"delta": {"content": "\n\n[Stopped by user]."}}]})
        return
    sse({"choices": [{"delta": {"content": "\n\n[Agent stopped: too many tool steps]."}}]})


def load_settings() -> dict:
    if CONFIG_FILE.is_file():
        try:
            data = json.loads(CONFIG_FILE.read_text("utf-8"))
            return {**DEFAULT_SETTINGS, **data}
        except (OSError, ValueError):
            pass
    return dict(DEFAULT_SETTINGS)


def save_settings(settings: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(settings, indent=2), "utf-8")


class LlamaServer:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.port: int | None = None
        self.api_key: str | None = None
        self.settings_key: str | None = None
        self.lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        with self.lock:
            proc, self.proc = self.proc, None
            self.port = None
            self.api_key = None
            self.settings_key = None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((HOST, 0))
            return s.getsockname()[1]


SERVER = LlamaServer()


# --- agent run control (Stop button / abort) ---
# The stop event lets the UI halt a running agent or chat turn without killing
# the llama-server engine or the browser, so the conversation session stays alive.
_AGENT_RUN_LOCK = threading.Lock()
AGENT_STOP_EVENT = threading.Event()
AGENT_RUNNING = False


def _agent_running() -> bool:
    with _AGENT_RUN_LOCK:
        return AGENT_RUNNING


def _set_agent_running(running: bool) -> None:
    global AGENT_RUNNING
    with _AGENT_RUN_LOCK:
        AGENT_RUNNING = running


def _request_agent_stop() -> bool:
    """Request a soft stop. Returns True if an agent/chat turn was active."""
    AGENT_STOP_EVENT.set()
    return _agent_running()


# --- live background-activity tracking (shown by the UI indicator) ---
_ACTIVITY_LOCK = threading.Lock()
ACTIVITY: dict = {"state": "idle", "tool": None, "args": None,
                  "detail": "", "since": 0.0}


def set_activity(state: str, **kw) -> None:
    with _ACTIVITY_LOCK:
        ACTIVITY["state"] = state
        ACTIVITY["tool"] = kw.get("tool")
        ACTIVITY["args"] = kw.get("args")
        ACTIVITY["detail"] = kw.get("detail", "")
        ACTIVITY["since"] = time.time()


def activity_payload() -> dict:
    with _ACTIVITY_LOCK:
        activity = dict(ACTIVITY)
    procs = [{"name": "web", "pid": os.getpid(), "kind": "web"}]
    if SERVER.running and SERVER.proc is not None and SERVER.proc.pid:
        procs.append({"name": "llama-server", "pid": SERVER.proc.pid,
                      "kind": "llm", "port": SERVER.port})
    binfo = browser_info()
    if binfo.get("running") and binfo.get("pid"):
        procs.append({"name": "browser", "pid": binfo["pid"], "kind": "browser",
                      "visible": bool(binfo.get("visible"))})
    return {
        "state": activity.get("state", "idle"),
        "tool": activity.get("tool"),
        "args": activity.get("args"),
        "detail": activity.get("detail", ""),
        "since": activity.get("since", 0.0),
        "server_running": SERVER.running,
        "server_port": SERVER.port,
        "browser_running": browser_running(),
        "processes": procs,
    }


# --- persistent conversation storage ---
_CHAT_ID_RE = re.compile(r"[A-Za-z0-9_-]+")


def _chat_file(chat_id: str) -> Path:
    return CHATS_DIR / f"{chat_id}.json"


def _chat_id_from_path(path: str) -> str | None:
    prefix = "/api/chats/"
    if not path.startswith(prefix):
        return None
    chat_id = path[len(prefix):].split("/", 1)[0]
    if not _CHAT_ID_RE.fullmatch(chat_id):
        return None
    return chat_id


def _default_title(messages: list) -> str:
    for m in messages:
        if m.get("role") != "user":
            continue
        content = m.get("content")
        text = content if isinstance(content, str) else ""
        if isinstance(content, list):
            for p in content:
                if isinstance(p, dict) and p.get("type") == "text":
                    text += p.get("text", "")
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            return text[:60]
    return "Untitled"


def _chat_meta(data: dict) -> dict:
    msgs = data.get("messages") or []
    return {
        "id": data.get("id"),
        "title": data.get("title") or "Untitled",
        "created": data.get("created", 0.0),
        "updated": data.get("updated", 0.0),
        "msg_count": len(msgs),
        "mode": data.get("mode", "chat"),
    }


def list_chats() -> list[dict]:
    if not CHATS_DIR.is_dir():
        return []
    out = []
    for f in CHATS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("id"):
            out.append(_chat_meta(data))
    out.sort(key=lambda m: m.get("updated", 0.0), reverse=True)
    return out


def load_chat(chat_id: str) -> dict | None:
    f = _chat_file(chat_id)
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return data


def save_chat(chat_id: str, title: str | None, messages: list,
              mode: str = "chat") -> dict:
    CHATS_DIR.mkdir(parents=True, exist_ok=True)
    existing = load_chat(chat_id) or {}
    data = {
        "id": chat_id,
        "title": title or existing.get("title") or _default_title(messages),
        "created": existing.get("created", time.time()),
        "updated": time.time(),
        "messages": messages,
        "mode": mode or existing.get("mode") or "chat",
    }
    _chat_file(chat_id).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    return _chat_meta(data)


def delete_chat(chat_id: str) -> bool:
    f = _chat_file(chat_id)
    if not f.is_file():
        return False
    f.unlink()
    return True


def _log_tail(n: int = 30) -> str:
    log = CONFIG_DIR / "llama-server.log"
    if not log.is_file():
        return ""
    lines = log.read_text("utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:])


def _debug_log(message: str) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_DIR / "web-debug.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def _wait_ready(port: int, timeout: float = 180.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if SERVER.proc is not None and SERVER.proc.poll() is not None:
            raise RuntimeError(f"llama-server exited early.\n{_log_tail()}")
        try:
            with urlopen(f"http://{HOST}:{port}/health", timeout=1.5) as r:
                if r.status == 200:
                    return
        except OSError:
            time.sleep(0.3)
    raise TimeoutError(f"llama-server did not become ready in time.\n{_log_tail()}")


def start_server(settings: dict) -> int:
    engine = settings.get("engine") or detect_engine()
    if not engine:
        raise ValueError("engine (llama-server) not found; set its path in Settings")
    engine_path = Path(engine)
    if not engine_path.is_file():
        raise ValueError(f"engine not found: {engine}")
    model = settings.get("model")
    if not model:
        raise ValueError("no model selected")
    model_path = Path(model)
    if not model_path.is_file():
        raise ValueError(f"model not found: {model}")
    if _is_mmproj(model_path):
        raise ValueError(
            f"{model_path.name} is a vision projector (mmproj), not a runnable model. "
            "Pick the main GGUF instead."
        )

    key = json.dumps([str(engine_path), str(model_path),
                      settings.get("threads"), settings.get("context"), settings.get("batch")])
    with SERVER.lock:
        if SERVER.running and SERVER.settings_key == key:
            return SERVER.port
    if SERVER.running:
        SERVER.stop()
    with SERVER.lock:
        machine = discover_machine()
        plan = resolve_plan(settings, machine)
        port = SERVER._free_port()
        api_key = secrets.token_hex(16)
        cmd = build_server_command(engine_path, model_path, plan, port=port,
                                   api_key=api_key, mmproj=_find_mmproj(model_path))
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = open(CONFIG_DIR / "llama-server.log", "a", encoding="utf-8")
        proc = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)
        SERVER.proc = proc
        SERVER.port = port
        SERVER.api_key = api_key
        SERVER.settings_key = key
    _wait_ready(port)
    return port


def status_payload() -> dict:
    machine = discover_machine()
    settings = load_settings()
    plan = resolve_plan(settings, machine)
    models = []
    if MODELS_DIR.is_dir():
        for p in sorted(MODELS_DIR.glob("*.gguf")):
            if _is_mmproj(p):
                continue
            models.append({"name": p.name, "path": str(p), "size": p.stat().st_size})
    return {
        "machine": {
            "cpus": machine.logical_cpus,
            "ram_gib": round(machine.available_ram_bytes / (1024 ** 3), 1),
        },
        "plan": asdict(plan),
        "models": models,
        "engine_detected": detect_engine(),
        "settings": settings,
        "server_running": SERVER.running,
        "server_port": SERVER.port,
        "browser_running": browser_running(),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "SSD-LLM/0.1"

    def log_message(self, fmt, *args) -> None:
        pass

    def _send_json(self, data, status: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _serve_static(self, name: str) -> None:
        path = STATIC_DIR / name
        if not path.is_file():
            self._send_json({"error": "not found"}, 404)
            return
        data = path.read_bytes()
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/api/status":
            self._send_json(status_payload())
        elif self.path == "/api/activity":
            self._send_json(activity_payload())
        elif self.path == "/api/chats":
            self._send_json({"chats": list_chats()})
        elif self.path.startswith("/api/chats/"):
            chat_id = _chat_id_from_path(self.path)
            data = load_chat(chat_id) if chat_id else None
            if data is None:
                self._send_json({"error": "not found"}, 404)
            else:
                data["msg_count"] = len(data.get("messages") or [])
                self._send_json(data)
        elif self.path in ("/", "/index.html"):
            self._serve_static("index.html")
        elif self.path == "/api/stop":
            self._handle_stop()
        else:
            self._send_json({"error": "not found"}, 404)

    def _handle_stop(self) -> None:
        if _request_agent_stop():
            self._send_json({"ok": True, "stopped": "agent"})
        else:
            SERVER.stop()
            self._send_json({"ok": True, "stopped": "engine"})

    def do_POST(self) -> None:
        if self.path == "/api/stop":
            self._handle_stop()
        elif self.path == "/api/settings":
            settings = {**DEFAULT_SETTINGS, **self._read_json()}
            save_settings(settings)
            self._send_json({"ok": True, "settings": settings})
        elif self.path == "/api/start":
            settings = {**DEFAULT_SETTINGS, **self._read_json()}
            try:
                set_activity("loading", detail="starting engine")
                port = start_server(settings)
            except (ValueError, RuntimeError, TimeoutError) as exc:
                set_activity("idle")
                self._send_json({"error": str(exc)}, 500)
                return
            set_activity("idle")
            self._send_json({"ok": True, "port": port})
        elif self.path == "/api/chats":
            body = self._read_json()
            chat_id = secrets.token_hex(8)
            meta = save_chat(chat_id, body.get("title"),
                             body.get("messages") or [],
                             mode=body.get("mode") or "chat")
            self._send_json(meta, 201)
        elif self.path == "/api/chat":
            self._handle_chat()
        elif self.path == "/api/agent":
            self._handle_agent()
        else:
            self._send_json({"error": "not found"}, 404)

    def do_PUT(self) -> None:
        chat_id = _chat_id_from_path(self.path)
        if chat_id is None:
            self._send_json({"error": "not found"}, 404)
            return
        existing = load_chat(chat_id)
        if existing is None:
            self._send_json({"error": "not found"}, 404)
            return
        body = self._read_json()
        messages = body["messages"] if "messages" in body else existing.get("messages") or []
        mode = body.get("mode") or existing.get("mode") or "chat"
        meta = save_chat(chat_id, body.get("title"), messages, mode=mode)
        self._send_json(meta)

    def do_DELETE(self) -> None:
        chat_id = _chat_id_from_path(self.path)
        if chat_id is None or not delete_chat(chat_id):
            self._send_json({"error": "not found"}, 404)
        else:
            self._send_json({"ok": True})

    def _handle_agent(self) -> None:
        data = self._read_json()
        settings = {**DEFAULT_SETTINGS, **(data.get("settings") or {})}
        messages = data.get("messages") or []
        try:
            set_activity("loading", detail="starting engine")
            port = start_server(settings)
        except (ValueError, RuntimeError, TimeoutError) as exc:
            self._send_json({"error": str(exc)}, 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.connection.settimeout(30)

        def sse(payload) -> None:
            self.wfile.write(b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n")
            self.wfile.flush()

        AGENT_STOP_EVENT.clear()
        _set_agent_running(True)
        try:
            _run_agent_loop(messages, settings, port, SERVER.api_key,
                            _workspace(settings), sse)
        except BrokenPipeError:
            pass
        except Exception as exc:
            _debug_log(f"agent error: {exc!r}")
            try:
                sse({"choices": [{"delta": {"content": f"\n\n[Agent error] {exc}"}}]})
            except OSError:
                pass
        finally:
            _set_agent_running(False)
            set_activity("idle")
            try:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except OSError:
                pass

    def _handle_chat(self) -> None:
        data = self._read_json()
        settings = {**DEFAULT_SETTINGS, **(data.get("settings") or {})}
        messages = data.get("messages") or []
        try:
            set_activity("loading", detail="starting engine")
            port = start_server(settings)
        except (ValueError, RuntimeError, TimeoutError) as exc:
            _debug_log(f"chat start error: {exc!r}\n{traceback.format_exc()}")
            set_activity("idle")
            self._send_json({"error": str(exc)}, 500)
            return
        except Exception as exc:
            _debug_log(f"chat start error: {exc!r}\n{traceback.format_exc()}")
            set_activity("idle")
            self._send_json({"error": str(exc)}, 500)
            return
        body = build_chat_body(messages, settings)
        conn = http.client.HTTPConnection(HOST, port, timeout=600)
        try:
            conn.request("POST", "/v1/chat/completions", json.dumps(body), headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {SERVER.api_key}",
            })
            resp = conn.getresponse()
        except OSError as exc:
            self._send_json({"error": f"llama-server unreachable: {exc}"}, 502)
            return
        if resp.status != 200:
            err = resp.read().decode("utf-8", "replace")
            self._send_json({"error": f"llama-server: {resp.status} {err[:500]}"}, 502)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.connection.settimeout(30)
        AGENT_STOP_EVENT.clear()
        _set_agent_running(True)
        try:
            set_activity("generating", detail="streaming answer")
            for raw in resp:
                if AGENT_STOP_EVENT.is_set():
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("data:"):
                    self.wfile.write((line + "\n\n").encode("utf-8"))
                    self.wfile.flush()
        finally:
            _set_agent_running(False)
            set_activity("idle")
            conn.close()


class WebServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True


def serve(host: str = HOST, port: int = DEFAULT_PORT, open_browser: bool = True) -> int:
    try:
        httpd = WebServer((host, port), Handler)
    except OSError:
        print(f"[ERROR] Port {port} is already in use on {host}. "
              f"Stop the other SSD-LLM instance (or change --port) and try again.")
        return 1
    url = f"http://{host}:{port}/"
    print(f"SSD-LLM web UI: {url}")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        SERVER.stop()
        stop_browser()
        httpd.server_close()
    return 0
