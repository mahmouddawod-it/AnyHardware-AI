from __future__ import annotations

import argparse
from pathlib import Path

from .planner import GiB, discover_machine, plan_run
from .runner import build_command, run


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ssd-llm", description="CPU + RAM + SSD GGUF runner")
    subs = p.add_subparsers(dest="action", required=True)
    subs.add_parser("inspect", help="show the computed safe runtime budget")
    q = subs.add_parser("run", help="run a GGUF model with llama.cpp")
    q.add_argument("--engine", required=True, help="path to llama-cli executable")
    q.add_argument("--model", required=True, help="path to local GGUF file on SSD")
    q.add_argument("--prompt", required=True)
    q.add_argument("--tokens", type=int, default=128)
    q.add_argument("--threads", type=int)
    q.add_argument("--context", type=int)
    q.add_argument("--batch", type=int)
    w = subs.add_parser("web", help="start the web chat UI")
    w.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    w.add_argument("--port", type=int, default=8300, help="bind port (default: 8300)")
    w.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    return p


def main() -> int:
    args = _parser().parse_args()
    if args.action == "web":
        from .web import serve
        return serve(host=args.host, port=args.port, open_browser=not args.no_browser)
    machine = discover_machine()
    plan = plan_run(machine, threads=getattr(args, "threads", None),
                    context=getattr(args, "context", None), batch=getattr(args, "batch", None))
    if args.action == "inspect":
        print(f"CPU threads: {plan.threads}/{machine.logical_cpus}")
        print(f"Available RAM: {machine.available_ram_bytes / GiB:.1f} GiB")
        print(f"OS reserve: {plan.reserved_ram_bytes / GiB:.1f} GiB")
        print(f"Context: {plan.context}; batch: {plan.batch}; GPU layers: 0; mmap: on")
        return 0
    command = build_command(Path(args.engine), Path(args.model), args.prompt, args.tokens, plan)
    return run(command)


if __name__ == "__main__":
    raise SystemExit(main())
