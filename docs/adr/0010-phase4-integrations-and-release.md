# ADR 0010: Phase 4 integrations and the 1.0 release

- **Status:** Accepted
- **Date:** 2026-11-08

## Context

Phase 4 (BUILD_PROMPT.md section 18) adds:

- Docker images, the GitHub Action, and CI templates;
- orchestrator examples (Airflow, Dagster, Prefect, Databricks Workflows, ADF);
- notifications, OpenLineage, and cloud secret providers;
- the full documentation site and a security hardening pass;
- the 1.0 release with Trusted Publishing.

It is accepted when:

- a fresh user can install from PyPI, follow the quickstart, and get a report in under 5 minutes;
- the docs fully cover configuration and plugins;
- the release pipeline is fully automated.

The remaining catalog entries that were still only declared (the `sample` strategy, SCH-07 to SCH-11 and SCH-14, two sinks, and the generic connectors) are also completed here, so 1.0 presents no planned feature as working.

## Decision

1. **Distribution name `dvf-framework`.**
   - The name `dvf` is taken on PyPI by an unrelated project (2018), so dvf is published as `dvf-framework`: `pip install dvf-framework`, with extras such as `pip install 'dvf-framework[postgres]'`.
   - The import package, the command (`dvf`), and the image names are unchanged. `dvf.DISTRIBUTION` holds the name, and install hints in error messages use it.
2. **Packs and command modules, as in Phase 3.** `integrations` holds notifiers, OpenLineage, OpenTelemetry, and the triage interface; `secrets` holds the secret providers; `catalog` holds the completed catalog entries. New commands live in `dvf.cli.tools`, `dvf.cli.integrations`, and `dvf.cli.notify`.
3. **Releases go through a reviewed pull request (release-please).**
   - release-please uses the `simple` strategy with `skip-changelog`. hatch-vcs takes the version from the tag, and CHANGELOG.md stays hand-curated; the GitHub Release notes come from its section.
   - The release workflow builds and checks the sdist and wheel, publishes to PyPI with Trusted Publishing (no tokens) and attestations, pushes multi-arch images to GHCR, attaches CycloneDX SBOMs, creates the GitHub Release, and publishes the docs.
   - Every job declares its own permissions, the workflow default is none, and actions are pinned to commit SHAs.
   - CI never pushes to `main`.
4. **Two images, no proprietary drivers.**
   - `slim` holds the core (DuckDB, files, SQLite). `full` adds open-source drivers and formats.
   - Both run Python 3.13 on Debian trixie, pinned by digest, as non-root UID 10001 with group 0, so they also work under an arbitrary UID. SIGINT is the stop signal, so `docker stop` checkpoints and writes partial results.
   - SQL Server is not in `full`, because `mssql-python` bundles Microsoft's ODBC Driver 18 under Microsoft's license; users add it with a build argument. Spark is not included, because it needs Java.
5. **Integrations never fail a run.**
   - The runner reaches notifications, OpenLineage, OpenTelemetry, and triage through one facade.
   - Delivery failures are logged, retried with backoff when transient, and shown in the CLI output.
   - dvf writes OpenLineage JSON itself (facets from the published schemas) rather than depending on the client library. It uses the global OpenTelemetry providers, so users choose their exporters.
   - Triage is an interface only, off by default and opt-in, and it receives masked, aggregated summaries.
6. **Orchestrator integrations are thin.**
   - `dvf.integrations` modules import their orchestrator only when imported, and all go through one function whose quality gates match `dvf run`.
   - A failed gate fails the task without a retry; execution errors stay retryable.
   - They are verified with stand-in modules, because the orchestrators are not dependencies.
7. **Secrets are resolved once per run and redacted in every form.**
   - Vault and Databricks use their REST APIs through the standard library; the cloud SDKs are optional extras.
   - Redaction also covers URL-encoded and JSON-escaped forms, query tokens, `Authorization` headers, and webhook paths.
   - Strict privacy mode now covers recorded SQL literals, profiles, aggregates, and repro files.
8. **Sampling is Bernoulli selection on a seeded hash of the canonical key.**
   - Every dialect computes the same hash, so both sides select the same keys without exchanging them.
   - Estimates are stratified by sampling class, with Wilson score intervals.
9. **Catalog metadata comes from optional connector methods.**
   - `get_physical_design`, `get_collations`, and `get_object_inventory` return `None` when unavailable, which skips the check with a message.
   - Drift snapshots are stored under `<state_dir>/schemas`.
10. **Reference documentation is generated.**
    - `nox -s docs_reference` writes the configuration, components, results, and CLI reference from the Pydantic models, the registry, and the Typer app. A test fails when they are stale.
    - Every configuration field has a docstring, and a test enforces it.

## Consequences

- **Quickstart.** From a fresh environment, installing the wheel, `dvf init --example`, and `dvf run` took 31.5 seconds on Windows (target: 5 minutes). The PyPI install itself cannot be verified until the first release is published.
- **Release pipeline.** The workflows are written and pinned, but have not run, because the repository has no remote yet. The `OWNER/REPOSITORY` placeholders in URLs must be replaced when it does.
- **Beta components.** Secret providers other than Vault, the Snowflake sink, and orchestrator integrations are verified with fakes or stand-in modules. Real Slack, Teams, and OTLP endpoints were not called.
- **Image size.** The images are about 400 MB (`slim`) and 950 MB (`full`) uncompressed, mostly PyArrow and DuckDB.
