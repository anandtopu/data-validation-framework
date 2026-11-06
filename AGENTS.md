# Notes for AI coding agents and new contributors

- Read `ARCHITECTURE.md` first, then the ADRs in `docs/adr/`. `BUILD_PROMPT.md` is the full product specification and phase plan.
- Run everything through `uv run nox` (or `uv run pytest`, `uv run ruff check`, `uv run mypy`). Do not add bash-only scripts; Windows is a first-class platform.
- Public API: `dvf.api`, `dvf.plugins`, `dvf.models`. Keep everything else internal.
- Built-in components register in `src/dvf/plugins/builtins.py`. Configuration models live in `src/dvf/config/`.
- After changing configuration or result models, run `uv run nox -s schemas` and commit `schemas/*.json`.
- Never present stubs as working features. A declared component without an implementation must show as `declared` in `dvf plugins list`.
- Use synthetic data in tests. Never commit secrets.
