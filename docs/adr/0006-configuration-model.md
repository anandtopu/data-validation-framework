# ADR 0006: Configuration model and loading pipeline

- **Status:** Accepted
- **Date:** 2026-11-05

## Context

Configuration is the main user interface. It must support several file formats, environments, secrets, hundreds of tables, and plugin-provided types, and it must report mistakes precisely. The illustrative config in `BUILD_PROMPT.md` section 6.2 was a sketch to be finalized in Phase 0.

## Decision

1. **Formats:** YAML (primary), JSON, TOML, `.properties`, and `.ini`. YAML and `.properties` errors report file, line, and column; JSON, TOML, and INI errors report the dotted path. Duplicate YAML keys are errors.
2. **Layering**, lowest to highest: model defaults, `extends:` files, the main file, profile (in-file `profiles:` section, then sibling `<stem>.<profile>.<ext>`), sibling `<stem>.local.<ext>`, `DVF__` environment variables (double underscore separates path segments), and `--set` overrides. Mappings merge recursively; lists replace.
3. **Interpolation:** `${env:NAME}`, `${env:NAME:-default}`, `${var:name}`, `${secret:provider:reference}`, and deferred `${run.id}`, `${run.date}`, `${run.timestamp}`, `${run.profile}`. `$${` escapes a literal `${`. A value that is exactly one `${var:...}` keeps the variable's type.
4. **Inheritance is resolved at load time.** `defaults` flows into each suite (`suites[].defaults`), and the suite's effective defaults flow into each table. Tables therefore carry effective `engine`, `severity`, `sample_size`, `normalization`, `comparison`, and `schema_checks`. Checks without `severity` or `side` receive them from their table and suite.
5. **Polymorphic sections** (`connections`, `checks`, `notifications`) are validated by the config model registered for each entry's `type`. Unknown types are errors with suggestions.
6. **Version field.** `version: 1` is required. `dvf config migrate` will upgrade older versions when a version 2 exists.
7. **Identifiers.** Suite and table names match `^[A-Za-z0-9_-]+$` because they form check IDs (`suite.table.check`).
8. **Lenient validation.** `dvf validate-config` reports missing environment variables as warnings and does not fetch secrets unless `--resolve-secrets` is passed, so it can run in CI without credentials. `--strict` turns warnings into failures.

### Changes from the illustrative config in BUILD_PROMPT.md

- Time travel moved from `comparison.snapshot.target_as_of` to `as_of` on each dataset (`source.as_of`, `target.as_of`), because each side has its own snapshot.
- Pattern tables use `pattern: {source: "...", target: "..."}` so both sides are explicit.
- `keys: auto` is stored as `discover_keys: true` with an empty `keys` list in the resolved config.
- Normalizers can take parameters: `normalize: [rtrim, {round: {digits: 2}}]`.

## Consequences

- The resolved configuration (`dvf config show`) is verbose because every table carries its effective settings, but downstream code never re-implements inheritance.
- Plugin authors get validation, error locations, and JSON Schema autocomplete for free by providing a config model.
