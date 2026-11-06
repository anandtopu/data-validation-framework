# Contributing to dvf

Thanks for helping. This guide covers setting up a development environment on Windows, macOS, or Linux and the standards a change must meet.

## Set up

1. Install Python 3.11 or later and [uv](https://docs.astral.sh/uv/).
2. Clone the repository and install all dependency groups:

   ```bash
   uv sync --all-groups
   ```

3. Install the pre-commit hooks:

   ```bash
   uv run pre-commit install
   ```

## Everyday commands

All tasks run through [nox](https://nox.thea.codes/) so they behave the same on every operating system.

| Command | Purpose |
|---|---|
| `uv run nox` | Lint, type check, and test (the default sessions) |
| `uv run nox -s lint` | Ruff lint and format check |
| `uv run nox -s typecheck` | mypy in strict mode |
| `uv run nox -s tests` | pytest with coverage on the current Python |
| `uv run nox -s tests_all` | pytest on every supported Python that is installed |
| `uv run nox -s integration` | PostgreSQL integration tests (uses `DVF_TEST_POSTGRES_URL`, or Docker through testcontainers) |
| `uv run nox -s example` | Scaffold the seeded example and run it end to end |
| `uv run nox -s schemas` | Regenerate `schemas/*.json` after changing config or result models |
| `uv run nox -s docs` | Build the documentation site in strict mode |

## Standards for a change

- **Tests.** New behaviour comes with tests. Coverage must stay at or above the threshold in `pyproject.toml`.
- **Types.** The package is checked with `mypy --strict`.
- **Style.** Ruff formats and lints the code; do not hand-format.
- **Public API.** Only `dvf.api`, `dvf.plugins`, and `dvf.models` are public. Changes to them, to the configuration schema, or to the result schema need an ADR in `docs/adr/` and a CHANGELOG entry.
- **Parity.** A normalizer or canonical form needs SQL (per dialect), Polars, and Python implementations, and a case in `tests/parity`. The Python reference defines the result.
- **Acceptance.** `tests/e2e` injects known defects; a change must keep every count and classification exact.
- **Integration tests** are marked `integration`. Without Docker, point them at any PostgreSQL 13+ server with `DVF_TEST_POSTGRES_URL=postgresql://user:password@host:port/db`; they skip when neither is available.
- **Schemas.** If you change configuration or result models, run `uv run nox -s schemas` and commit the regenerated files. A test fails if they are out of date.
- **No secrets or real data.** Use synthetic data only. Never commit credentials, even in examples.
- **Proprietary drivers.** Do not vendor or bundle them. Reference them as optional, user-installed dependencies.
- **Commits.** Use [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`).

## Adding a plugin to the core

1. Put the configuration model in `src/dvf/config/` (connections, checks, or notifications) and the implementation in its layer package.
2. Register it in `src/dvf/plugins/builtins.py`.
3. Add tests, regenerate schemas, and document it.

## Reporting bugs and requesting features

Use the issue templates. For security issues, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.
