# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Phase 0 foundations:
  - Configuration system: YAML, JSON, TOML, `.properties`, and `.ini`; layering with `extends`, profiles, local overrides, environment variables, and `--set`; interpolation of environment variables, variables, and secrets; inheritance of defaults; diagnostics with file, line, and column.
  - Canonical type system with per-dialect native type parsing (through sqlglot), SAP ABAP dictionary type mapping, and a five-level compatibility classifier.
  - Versioned result model (`RunResult`, `SuiteResult`, `DatasetPairResult`, `CheckResult`) with JSON Schema.
  - Plugin registry with entry points, local plugins, decorators, API version checks, failure isolation, and declared built-ins.
  - pluggy lifecycle hookspecs, with `on_config_loaded` called after configuration loads.
  - Secret providers for environment variables and `.env` files; process-wide redaction.
  - CLI: `version`, `validate-config`, `config show`, `schema config`, `schema results`, `plugins list`.
  - JSON Schemas for configuration and results in `schemas/`.
  - Architecture document and ADRs 0001 to 0006.
  - CI on Windows, macOS, and Linux with Python 3.11 to 3.14; CodeQL; Dependabot.
