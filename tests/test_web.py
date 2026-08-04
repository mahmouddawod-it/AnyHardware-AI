from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from ssd_llm.planner import Machine, plan_run
from ssd_llm.web import (
    AGENT_TOOLS,
    _execute_tool,
    _find_mmproj,
    _is_mmproj,
    _safe_path,
    _web_search,
    build_chat_body,
    build_server_command,
    detect_engine,
    load_settings,
    resolve_plan,
    save_settings,
    start_server,
)


def test_build_server_command_forces_cpu_only(tmp_path: Path):
    engine = tmp_path / "llama-server.exe"
    model = tmp_path / "model.gguf"
    plan = plan_run(Machine(4, 8 * 1024**3))
    cmd = build_server_command(engine, model, plan, port=12345, api_key="key")
    assert cmd[cmd.index("-ngl") + 1] == "0"
    assert "--no-ui" in cmd
    assert cmd[cmd.index("--host") + 1] == "127.0.0.1"
    assert cmd[cmd.index("--port") + 1] == "12345"
    assert cmd[cmd.index("-c") + 1] == str(plan.context)
    assert cmd[cmd.index("-t") + 1] == str(plan.threads)


def test_build_server_command_includes_mmproj_when_present(tmp_path: Path):
    model = tmp_path / "Qwen.gguf"
    mmproj = tmp_path / "mmproj-Qwen-f16.gguf"
    model.touch(); mmproj.touch()
    plan = plan_run(Machine(4, 8 * 1024**3))
    cmd = build_server_command(tmp_path / "llama-server.exe", model, plan,
                               port=1, api_key="k", mmproj=mmproj)
    assert cmd[cmd.index("--mmproj") + 1] == str(mmproj)
    assert cmd[cmd.index("--image-min-tokens") + 1] == "1024"


def test_build_server_command_no_image_flags_without_mmproj(tmp_path: Path):
    model = tmp_path / "Qwen.gguf"
    model.touch()
    plan = plan_run(Machine(4, 8 * 1024**3))
    cmd = build_server_command(tmp_path / "llama-server.exe", model, plan,
                               port=1, api_key="k")
    assert "--image-min-tokens" not in cmd


def test_resolve_plan_auto_and_manual():
    machine = Machine(8, 16 * 1024**3)
    auto = resolve_plan({"threads": "auto", "context": "auto", "batch": "auto"}, machine)
    assert auto.threads == 7
    assert auto.context == 4096
    assert auto.batch == 2048
    manual = resolve_plan({"threads": 2, "context": 1024, "batch": 32}, machine)
    assert (manual.threads, manual.context, manual.batch) == (2, 1024, 32)


def test_build_chat_body_streams():
    body = build_chat_body(
        [{"role": "user", "content": "hi"}],
        {"model": "m.gguf", "temperature": 0.3, "max_tokens": 64},
    )
    assert body["stream"] is True
    assert body["temperature"] == 0.3
    assert body["max_tokens"] == 64
    assert body["messages"][0]["content"] == "hi"


def test_settings_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("ssd_llm.web.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("ssd_llm.web.CONFIG_FILE", tmp_path / "web.json")
    save_settings({"model": "x.gguf", "temperature": 0.9})
    loaded = load_settings()
    assert loaded["model"] == "x.gguf"
    assert loaded["temperature"] == 0.9
    assert loaded["threads"] == "auto"


def test_detect_engine_returns_string():
    result = detect_engine()
    assert isinstance(result, str)


def test_is_mmproj_detects_projector_files():
    assert _is_mmproj(Path("mmproj-Qwen-f16.gguf")) is True
    assert _is_mmproj(Path("Qwen-Q4_K_M.gguf")) is False


def test_find_mmproj_pairs_with_model_stem(tmp_path: Path):
    model = tmp_path / "Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf"
    model.touch()
    wrong = tmp_path / "mmproj-Other-Model-f16.gguf"
    right = tmp_path / "mmproj-Qwen2.5-VL-3B-Instruct-f16.gguf"
    wrong.touch(); right.touch()
    assert _find_mmproj(model) == right


def test_start_server_rejects_mmproj_as_model(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("ssd_llm.web.CONFIG_DIR", tmp_path)
    engine = tmp_path / "llama-server.exe"
    model = tmp_path / "mmproj-Qwen-f16.gguf"
    engine.touch(); model.touch()
    with pytest.raises(ValueError, match="mmproj"):
        start_server({"engine": str(engine), "model": str(model),
                      "threads": "auto", "context": "auto", "batch": "auto"})


def test_start_server_does_not_deadlock_on_restart(tmp_path: Path, monkeypatch):
    import ssd_llm.web as web
    engine = tmp_path / "llama-server.exe"
    model = tmp_path / "model.gguf"
    engine.touch(); model.touch()
    fake_proc = SimpleNamespace(poll=lambda: None, terminate=lambda *a: None,
                                wait=lambda *a, **k: None, kill=lambda *a: None)
    web.SERVER.proc = fake_proc
    web.SERVER.port = 1
    web.SERVER.api_key = "k"
    web.SERVER.settings_key = "DIFFERENT_KEY"
    monkeypatch.setattr(web, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(web, "discover_machine", lambda: SimpleNamespace(cpus=8, ram_gib=8))
    monkeypatch.setattr(web, "resolve_plan",
                        lambda s, m: SimpleNamespace(threads=4, context=4096, batch=256, gpu_layers=0))
    monkeypatch.setattr(web, "build_server_command", lambda *a, **k: ["llama-server"])
    monkeypatch.setattr(web, "subprocess", SimpleNamespace(
        Popen=lambda *a, **k: SimpleNamespace(poll=lambda: None),
        STDOUT=-1, DEVNULL=-2,
        TimeoutExpired=Exception))
    monkeypatch.setattr(web, "_wait_ready", lambda port: None)
    result: dict = {}
    def run():
        try:
            result["port"] = start_server(
                {"engine": str(engine), "model": str(model),
                 "threads": "auto", "context": "auto", "batch": "auto"})
        except Exception as exc:
            result["err"] = exc
    t = threading.Thread(target=run)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive(), "start_server deadlocked while holding SERVER.lock"
    assert "err" not in result
    web.SERVER.proc = None
    web.SERVER.port = None
    web.SERVER.api_key = None
    web.SERVER.settings_key = None


def test_execute_run_python(tmp_path: Path):
    result, image = _execute_tool("run_python", {"code": "print(2 + 2)"}, tmp_path)
    assert "4" in result
    assert image is None


def test_execute_write_and_read_file(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    result, _ = _execute_tool("write_file", {"path": "a/b.txt", "content": "hello"}, ws)
    assert "hello" in _execute_tool("read_file", {"path": "a/b.txt"}, ws)[0]


def test_execute_list_files(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "x.py").write_text("", "utf-8")
    listing, _ = _execute_tool("list_files", {"path": "."}, ws)
    assert "x.py" in listing


def test_execute_unknown_tool(tmp_path: Path):
    result, image = _execute_tool("nope", {}, tmp_path)
    assert "ERROR" in result
    assert image is None


def test_browser_tool_argument_validation(tmp_path: Path):
    result, _ = _execute_tool("click_element", {}, tmp_path)
    assert "ERROR" in result
    result, _ = _execute_tool("type_text", {"index": 0, "text": "   "}, tmp_path)
    assert "ERROR" in result
    result, _ = _execute_tool("scroll", {"direction": "sideways"}, tmp_path)
    assert "ERROR" in result
    result, _ = _execute_tool("run_js", {"script": ""}, tmp_path)
    assert "ERROR" in result


def test_parse_positional_tool_calls():
    from ssd_llm.web import _parse_tool_lines
    content = (
        'navigate("https://www.google.com")\n'
        'type_text("شركات التكنولوجيا في مصر")\n'
        'click_element("search button")\n'
        "run_js('document.title')\n"
        'scroll("down")\n'
    )
    calls = _parse_tool_lines(content)
    assert calls[0] == ("navigate", {"url": "https://www.google.com"})
    assert calls[1] == ("type_text", {"text": "شركات التكنولوجيا في مصر"})
    assert calls[2] == ("click_element", {"index": "search button"})
    assert calls[3] == ("run_js", {"script": "document.title"})
    assert calls[4] == ("scroll", {"direction": "down"})


def test_parse_tool_lines_mixed_json_and_positional():
    from ssd_llm.web import _parse_tool_lines
    content = (
        'TOOL: {"name": "web_search", "arguments": {"query": "ai"}}\n'
        'navigate("https://example.com")\n'
    )
    calls = _parse_tool_lines(content)
    assert ("web_search", {"query": "ai"}) in calls
    assert ("navigate", {"url": "https://example.com"}) in calls


def test_agent_loop_streams_and_executes_tools_early(tmp_path: Path):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from ssd_llm import web as web
    import json as _json
    import threading as _threading

    class MockLLM(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            req = _json.loads(self.rfile.read(n).decode("utf-8"))
            msgs = req["messages"]
            last = msgs[-1] if msgs else {}
            if isinstance(last.get("content"), str) and last["content"].startswith("TOOL_RESULT"):
                delta = "Final answer here."
            else:
                delta = 'TOOL: {"name": "run_python", "arguments": {"code": "print(1)"}}'
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                for ch in delta:
                    chunk = f"data: {_json.dumps({'choices': [{'delta': {'content': ch}}]})}\n\n"
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except OSError:
                pass

    srv = HTTPServer(("127.0.0.1", 0), MockLLM)
    port = srv.server_address[1]
    t = _threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        events = []
        web._run_agent_loop(
            [{"role": "user", "content": "compute something"}],
            {"model": "m.gguf", "temperature": 0.1, "max_tokens": 128},
            port, "key", tmp_path,
            lambda e: events.append(e))
        assert events[0]["type"] == "tool_call"
        assert events[0]["name"] == "run_python"
        assert events[1]["type"] == "tool_result"
        assert "1" in events[1]["preview"]

        def text_of(ev):
            if ev.get("type") == "delta":
                return ev.get("content", "")
            if "choices" in ev:
                return ((ev.get("choices") or [{}])[0].get("delta") or {}).get("content", "")
            return ""

        final = "".join(text_of(e) for e in events)
        assert final == "Final answer here."
        assert not any(e.get("type") == "tool_call" for e in events[2:])
    finally:
        srv.shutdown()


def test_agent_loop_stop_event_aborts_without_tools(tmp_path: Path):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from ssd_llm import web as web
    import json as _json
    import threading as _threading

    hit = {"count": 0}

    class MockLLM(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            hit["count"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                b'data: {"choices": [{"delta": {"content": "unexpected"}}]}\n\n')
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    srv = HTTPServer(("127.0.0.1", 0), MockLLM)
    port = srv.server_address[1]
    t = _threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        events = []
        web.AGENT_STOP_EVENT.clear()
        web.AGENT_STOP_EVENT.set()
        web._run_agent_loop(
            [{"role": "user", "content": "افتح جوجل"}],
            {"model": "m.gguf", "temperature": 0.1, "max_tokens": 128},
            port, "key", tmp_path,
            lambda e: events.append(e))
        web.AGENT_STOP_EVENT.clear()

        def text_of(ev):
            if ev.get("type") == "delta":
                return ev.get("content", "")
            if "choices" in ev:
                return ((ev.get("choices") or [{}])[0].get("delta") or {}).get("content", "")
            return ""

        final = "".join(text_of(e) for e in events)
        assert "[Stopped by user]." in final
        assert not any(e.get("type") == "tool_call" for e in events)
        assert hit["count"] == 0
    finally:
        web.AGENT_STOP_EVENT.clear()
        srv.shutdown()


def test_scan_first_tool_call_incremental():
    from ssd_llm.web import _scan_first_tool_call
    full = 'TOOL: {"name": "web_search", "arguments": {"query": "ai news"}}\n'
    assert _scan_first_tool_call(full) == ("web_search", {"query": "ai news"})
    assert _scan_first_tool_call("thinking first...\n" + full) == \
        ("web_search", {"query": "ai news"})
    # partial JSON must not trigger
    assert _scan_first_tool_call('TOOL: {"name": "navigate", "arguments": {"u') is None
    assert _scan_first_tool_call('TOOL: {"name": "navigate", "arguments":') is None
    assert _scan_first_tool_call("") is None
    # multiple calls -> first one wins
    multi = 'TOOL: {"name": "a", "arguments": {"x": 1}}\n' + full
    assert _scan_first_tool_call(multi) == ("a", {"x": 1})
    # bad JSON between markers is skipped
    bad = 'TOOL: {not json}\n' + full
    assert _scan_first_tool_call(bad) == ("web_search", {"query": "ai news"})
    # JSON strings containing braces are handled
    tricky = 'TOOL: {"name": "type_text", "arguments": {"text": "a} b"}}'
    assert _scan_first_tool_call(tricky) == ("type_text", {"text": "a} b"})


def test_looks_like_tool_intent():
    from ssd_llm.web import _looks_like_tool_intent
    assert _looks_like_tool_intent('navigate("x")')
    assert _looks_like_tool_intent("I will use run_js to read the page")
    assert not _looks_like_tool_intent("Here is the final answer.")


def test_to_index_accepts_list_and_number():
    from ssd_llm.web import _to_index
    assert _to_index(0) == 0
    assert _to_index("3") == 3
    assert _to_index(2.0) == 2
    assert _to_index([1]) == 1
    assert _to_index([2, 3]) == 2
    assert _to_index([]) is None
    assert _to_index("abc") is None
    assert _to_index(-1) is None


def test_trim_old_images_keeps_only_latest():
    from ssd_llm.web import _trim_old_images
    history = [
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:old"}},
            {"type": "text", "text": "TOOL_RESULT:\npage"}]},
        {"role": "user", "content": "plain"},
    ]
    _trim_old_images(history)
    assert history[0]["content"] == "TOOL_RESULT:\npage"
    assert history[1]["content"] == "plain"


def test_browser_tools_present_in_agent_tools():
    names = [t["function"]["name"] for t in AGENT_TOOLS]
    for expected in ("navigate", "browser_snapshot", "click_element",
                     "type_text", "scroll", "run_js", "browser_screenshot"):
        assert expected in names


def test_safe_path_rejects_escape(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(ValueError):
        _safe_path(ws, "../outside.txt")


def test_web_search_returns_string():
    assert isinstance(_web_search("python programming"), str)


def test_norm_args_maps_aliases():
    from ssd_llm.web import _norm_args
    assert _norm_args({"f": "a.txt", "c": "hi"})["path"] == "a.txt"
    assert _norm_args({"f": "a.txt", "c": "hi"})["content"] == "hi"
    assert _norm_args({"d": "C:\\x"})["path"] == "C:\\x"
    assert _norm_args({"el": "3"})["index"] == "3"
    assert _norm_args({"t": "text"})["text"] == "text"
    assert _norm_args({"query": "q", "q": "q2"})["query"] == "q"


def test_clean_index_handles_i_and_hash():
    from ssd_llm.web import _clean_index
    assert _clean_index("i3") == 3
    assert _clean_index("i0") == 0
    assert _clean_index("#2") == 2
    assert _clean_index("5") == 5
    assert _clean_index(7) == 7
    assert _clean_index("search box") == "search box"
    assert _clean_index("") is None
    assert _clean_index(None) is None


def test_clean_url_strips_glued_text():
    from ssd_llm.web import _clean_url
    assert _clean_url("https://entasher.com/ar/eg/s/IT-companies-استكشف أفضل شركات") \
        == "https://entasher.com/ar/eg/s/IT-companies-"
    assert _clean_url("https://x.com/a b") == "https://x.com/a"
    assert _clean_url("https://www.bing.com/search?q=1") == "https://www.bing.com/search?q=1"
    assert _clean_url("not a url") == "not a url"


def test_chat_crud_roundtrip(tmp_path: Path, monkeypatch):
    import ssd_llm.web as web
    monkeypatch.setattr(web, "CHATS_DIR", tmp_path / "chats")
    chat_id = "abc123"
    meta = web.save_chat(chat_id, None,
                         [{"role": "user", "content": "مرحبا"},
                          {"role": "assistant", "content": "أهلا"}],
                         mode="chat")
    assert meta["msg_count"] == 2
    assert meta["title"] == "مرحبا"
    assert meta["id"] == chat_id
    assert [m["id"] for m in web.list_chats()] == [chat_id]
    loaded = web.load_chat(chat_id)
    assert loaded["messages"][0]["content"] == "مرحبا"
    renamed = web.save_chat(chat_id, "Renamed", [{"role": "user", "content": "x"}])
    assert renamed["title"] == "Renamed"
    assert renamed["created"] == meta["created"]
    assert web.delete_chat(chat_id) is True
    assert web.load_chat(chat_id) is None
    assert web.list_chats() == []
    assert web.delete_chat(chat_id) is False


def test_chat_id_from_path_rejects_traversal():
    from ssd_llm.web import _chat_id_from_path
    assert _chat_id_from_path("/api/chats/abc123") == "abc123"
    assert _chat_id_from_path("/api/chats/abc/def") == "abc"
    assert _chat_id_from_path("/api/chats/") is None
    assert _chat_id_from_path("/api/chats/..%2F..%2Fpasswd") is None
    assert _chat_id_from_path("/api/chats/../secret") is None


def test_activity_payload_shape():
    import ssd_llm.web as web
    web.set_activity("tool", tool="run_python", args={"code": "1"}, detail="x")
    payload = web.activity_payload()
    assert payload["state"] == "tool"
    assert payload["tool"] == "run_python"
    assert payload["processes"][0]["name"] == "web"
    assert "pid" in payload["processes"][0]
    web.set_activity("idle")
    assert web.activity_payload()["state"] == "idle"

