# Security policy

## Supported versions

| Version | Security fixes |
|---|---|
| 1.x, latest minor release | Yes |
| 1.x, earlier minor releases | Critical fixes only, for 3 months after the next minor release |
| 0.x (pre-release) | No; upgrade to 1.x |

Fixes are released as patch versions. The PyPI distribution is `dvf-framework`; the package and command are `dvf`.

## Reporting a vulnerability

Do not open a public issue. Use GitHub's private vulnerability reporting ("Report a vulnerability" on the Security tab) or email `<security-contact@example.org>` (replace before publishing).

Include the affected version, a description of the issue, and steps to reproduce. Use synthetic data and never send real credentials or personal data. You will receive an acknowledgement within 5 working days and a status update at least every 14 days until the issue is resolved. We coordinate disclosure with you and credit you in the release notes unless you prefer otherwise.

## Threat model

dvf runs where its operator runs it (a laptop, a CI job, a container, a Databricks job), reads source and target systems with credentials it is given, and writes results and reports that are shared with other people.

**Assets.** Database and service credentials; the data being validated, which is often personal or regulated; and the reports, which travel further than the data.

**Trusted inputs.** The configuration and everything it can name are treated as code written by the operator: custom SQL, local plugins, installed plugin packages, report templates, and the environment. Anyone who can change them can run code or SQL with dvf's permissions.

**Untrusted inputs.** Values read from sources and targets, responses from databases and HTTP services (secret stores, SAP OData, notification and lineage endpoints), and results documents loaded for re-rendering.

**What dvf defends against.**

- Data turning into code: values are quoted per SQL dialect, HTML-escaped in reports, written as text (never formulas) in Excel, and never used to build file names; run IDs and check IDs cannot create paths outside the output and state folders.
- Secrets leaking into outputs: resolved secrets and credential-shaped text (URL passwords, tokens in query strings, `PWD=` in connection strings, webhook URLs) are redacted from logs, results, reports, error messages, the recorded command line, and notifications; configuration diagnostics never echo a credential.
- Credentials sent to the wrong host: the Vault, Databricks, and notification clients refuse redirects and plain HTTP to remote hosts; the SAP OData client stays on the configured host.
- Personal data in reports: samples are masked, hashed, or tokenized; `privacy.mode: strict` keeps every raw value out of results, reports, extracts, repro files, and notifications.
- Accidental writes: sessions are read-only where the database supports it, and dvf only runs queries.

**Out of scope.** A malicious configuration, plugin, or template; a compromised host or Python environment; database permissions broader than the documented least-privilege grants; and personal data in values the configuration itself puts in reports (table and column names, filters, `group_by` labels).

## Security design

- `dvf` is read-only against source and target systems by default.
- Secrets are referenced (`${secret:provider:path#key}`), never stored inline, resolved once per run, and redacted everywhere dvf writes text. Providers: environment, `.env`, OS keyring, HashiCorp Vault, AWS Secrets Manager, Azure Key Vault, Google Secret Manager, and Databricks secret scopes.
- `dvf` sends no telemetry and contacts only the endpoints in its configuration and environment.
- Loading local plugins (`plugins.paths`, `plugins.modules`, `--plugins`) executes project code. Treat configuration files as trusted code and review them like code.
- Custom SQL in configuration is executed as written. Restrict who can change configuration in shared environments.
- Dependencies are locked, audited with `pip-audit` in CI, and each release ships a CycloneDX SBOM, PyPI Trusted Publishing attestations, and container provenance.

See [docs/security.md](docs/security.md) for secret provider setup, least-privilege grants per database, privacy modes, and supply-chain details.
