# Security policy

## Supported versions

Until version 1.0, only the latest release receives security fixes.

## Reporting a vulnerability

Do not open a public issue. Use GitHub's private vulnerability reporting ("Report a vulnerability" on the Security tab) or email `<security-contact@example.org>` (replace before publishing).

Include the affected version, a description of the issue, and steps to reproduce. You will receive an acknowledgement within 5 working days and a status update at least every 14 days until the issue is resolved.

## Security design

- `dvf` is read-only against source and target systems by default.
- Secrets are referenced (`${secret:provider:reference}`), never stored inline, and are redacted from logs, reports, and `dvf config show` output.
- `dvf` sends no telemetry.
- Loading local plugins (`plugins.paths`, `plugins.modules`, `--plugins`) executes project code. Treat configuration files as trusted code and review them like code.
- Custom SQL in configuration is executed as written. Restrict who can change configuration in shared environments.
