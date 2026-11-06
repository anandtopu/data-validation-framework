# ADR 0001: Record architecture decisions

- **Status:** Accepted
- **Date:** 2026-11-05

## Context

`dvf` is meant to live for years, accept outside contributions, and evolve its core without breaking users. Contributors need to know why the framework is shaped the way it is before they change it.

## Decision

We keep architecture decision records in `docs/adr/`, numbered sequentially, in Michael Nygard's format. Any change to a public API, the configuration or result schema, the plugin contract, or a core dependency needs an ADR. Superseded ADRs stay in place with their status updated.

## Consequences

- Reviewers can ask "where is the ADR?" for structural changes.
- The ADR index in `docs/adr/README.md` must be updated with each new record.
