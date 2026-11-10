# Notes for AI coding agents and new contributors

- Read `ARCHITECTURE.md` first, then the ADRs in `docs/adr/`. `BUILD_PROMPT.md` is the full product specification and phase plan.
- Run everything through `uv run nox` (or `uv run pytest`, `uv run ruff check`, `uv run mypy`). Do not add bash-only scripts; Windows is a first-class platform.
- Public API: `dvf.api`, `dvf.plugins`, `dvf.models`, the notebook helper `dvf.debug`, and `dvf.run` / `dvf.validate_dataframe` at the top level. Keep everything else internal.
- Built-in components are declared in `src/dvf/plugins/builtins.py`. Phase 3 and later components live in packs under `src/dvf/packs/<pack>/`, whose `register(registry)` attaches implementations (`dvf.packs.attach`) and registers new components; packs never import optional drivers at import time. New CLI commands go in their own module under `src/dvf/cli/` with a `register(app)` function listed in `_COMMAND_MODULES` in `src/dvf/cli/app.py`.
- Configuration models live in `src/dvf/config/`, or next to a pack's components for pack-specific checks and connections.
- Systems without SQL subclass `StagedConnector` (`src/dvf/connectors/staged.py`); checks that need both datasets of a table subclass `PairCheck`.
- After changing configuration or result models, run `uv run nox -s schemas` and commit `schemas/*.json`.
- After changing configuration models, registered components, or CLI commands, run `uv run nox -s docs_reference` and commit `docs/reference/*.md` and `docs/cli.md`. Every configuration field needs a docstring.
- The PyPI distribution is `dvf-framework` (`dvf.DISTRIBUTION`); install hints use it. The import package and the command stay `dvf`.
- Never present stubs as working features. A declared component without an implementation must show as `declared` in `dvf plugins list`, and an implemented one not yet verified against a live system registers with `maturity="beta"`.
- Spark tests need Java 17 or later and are marked `spark`. Proprietary drivers (SAP `hdbcli`, `pyrfc`, the NW RFC SDK) are never dependencies.
- Use synthetic data in tests. Never commit secrets.
