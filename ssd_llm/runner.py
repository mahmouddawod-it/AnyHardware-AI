from __future__ import annotations

from pathlib import Path
import subprocess

from .planner import RunPlan


def build_command(engine: Path, model: Path, prompt: str, tokens: int, plan: RunPlan) -> list[str]:
    if not engine.is_file():
        raise FileNotFoundError(f"llama.cpp executable not found: {engine}")
    if not model.is_file():
        raise FileNotFoundError(f"GGUF model not found: {model}")
    if tokens < 1:
        raise ValueError("tokens must be at least 1")
    # mmap is llama.cpp's default. We deliberately do not send --no-mmap.
    return [str(engine), "-m", str(model), "-p", prompt, "-n", str(tokens),
            "-ngl", str(plan.gpu_layers), "-t", str(plan.threads),
            "-c", str(plan.context), "-b", str(plan.batch)]


def run(command: list[str]) -> int:
    """Run without a shell so prompts and paths cannot be interpreted as code."""
    return subprocess.run(command, check=False).returncode
