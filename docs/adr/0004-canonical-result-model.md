# ADR 0004: One canonical, versioned result model

- **Status:** Accepted
- **Date:** 2026-11-05

## Context

`dvf` produces many outputs: HTML, JSON, JUnit XML, Markdown, Excel, extracts, database sinks, and notifications. If each format computed its own view of the run, they would drift apart, and re-rendering a report would require re-running validations.

## Decision

- A single Pydantic model hierarchy, `RunResult` → `SuiteResult` → `DatasetPairResult` → `CheckResult`, in `dvf.models.results`, is the only output of a run.
- The model is serialized as JSON and published as JSON Schema in `schemas/results.schema.json`.
- The results schema has its own SemVer version (`RESULTS_SCHEMA_VERSION`), independent of the package version. Additive changes bump the minor version; removals or renames bump the major version and require a migrator.
- Every check result carries identity, outcome, evidence, reproduction details (queries, normalization, engine), timing, errors, waiver, and free-form `details` and `metadata` maps.
- Reporters receive a `RunResult` and nothing else. `dvf report --from results.json` (Phase 1) re-renders any format without touching data.

## Consequences

- Adding a field to a report means adding it to the model first.
- The `details` and `metadata` maps give plugins and configuration a supported place for custom information.
- A golden-file test keeps the committed schema in sync with the models.
