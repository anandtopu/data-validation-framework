# dvf: Data Validation Framework

**Configuration-driven data and schema validation for data migrations and data engineering.**

`dvf` validates schemas and data quality, reconciles source and target datasets at scale, and produces detailed, auditable reports. It targets migration projects (SAP, on-premise databases to cloud warehouses, SQL to NoSQL) and data engineering pipelines (PySpark, Databricks, Parquet, Avro, JSON, CSV).

!!! warning "Pre-alpha: Phase 0"
    The configuration system, canonical type system, result model, plugin registry, and CLI skeleton are in place.
    No data is read or compared yet; connectors, checks, comparisons, and reports arrive in Phase 1.

## What works today

| Command | What it does |
|---|---|
| `dvf --version`, `dvf version` | Versions of dvf, its schemas, the plugin API, Python, and the platform |
| `dvf validate-config <file>` | Load and validate a configuration, with file, line, and column for every problem |
| `dvf config show <file>` | The fully resolved configuration, with secrets redacted |
| `dvf schema config`, `dvf schema results` | JSON Schemas for IDE autocomplete and integrations |
| `dvf plugins list` | Every registered component and whether it is ready or declared |

## Design principles

1. Push computation to the data; move rows only to explain a mismatch.
2. Never require a whole table in memory.
3. Configuration first; Python for extending.
4. Everything is a plugin, including the built-ins.
5. One canonical, versioned result model behind every report.
6. Read-only and safe by default; secrets never appear in logs or reports.

Start with [Getting started](getting-started.md), then read [Configuration](configuration.md).
