from pathlib import Path

from ssd_llm.planner import Machine, plan_run
from ssd_llm.runner import build_command


def test_command_forces_cpu_only(tmp_path: Path):
    engine = tmp_path / "llama-cli.exe"
    model = tmp_path / "model.gguf"
    engine.touch(); model.touch()
    command = build_command(engine, model, "hello", 12, plan_run(Machine(2, 8 * 1024**3)))
    assert command[command.index("-ngl") + 1] == "0"
    assert "--no-mmap" not in command
