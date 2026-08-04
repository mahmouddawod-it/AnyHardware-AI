# Contributing

Thanks for helping improve AnyHardware AI!

## Getting started

```powershell
git clone https://github.com/your-username/AnyHardware-AI
cd AnyHardware-AI
py -m pip install -e .
py -m pytest -q
```

## Guidelines

- Keep the **zero-dependency** rule: everything must use the Python standard library.
- Keep browser automation inside `ssd_llm/browser.py` and agent/tool logic in `ssd_llm/web.py`.
- Add or update tests in `tests/` for any behavior change. The suite must pass on any machine
  without a GPU or llama.cpp (they are mocked).
- Run `py -m pytest -q` before submitting.
- Preserve the conservative safety behavior: the planner must never push a machine into swap.
- Follow the existing style: `from __future__ import annotations`, no third-party imports,
  no comments unless they explain a non-obvious decision.

## Reporting issues

Include:

- OS and Python version (`python --version`)
- Whether you use the CLI or the web UI
- The model and `llama.cpp` build you used
- The full error output

## Security

Vulnerabilities should be reported privately, not as a public issue. See [SECURITY.md](SECURITY.md).
