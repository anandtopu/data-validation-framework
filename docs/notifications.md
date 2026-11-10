# Notifications

dvf can tell people when a run finishes: in Slack, in Microsoft Teams, by email, or by posting JSON to any HTTP endpoint. Each channel chooses the run statuses it cares about. A channel that is down never fails or blocks the run.

## Configure channels

```yaml
notifications:
  - type: slack
    webhook: ${secret:env:SLACK_WEBHOOK_URL}
    on: [failed, error]                 # the default
    report_url: https://ci.example.com/dvf/${run.id}/report.html

  - type: teams
    name: data-ops
    webhook: ${secret:env:TEAMS_WORKFLOW_URL}
    on: [error]

  - type: email
    recipients: [data-qa@example.com]
    smtp_host: smtp.example.com
    username: dvf-bot@example.com
    password: ${secret:env:SMTP_PASSWORD}

  - type: webhook
    webhook: https://hooks.example.com/dvf
    headers:
      Authorization: ${secret:env:HOOK_TOKEN}
```

Webhook URLs, passwords, and header values are secrets: a webhook URL alone lets anyone post to your channel. Give them as `${secret:...}` references. dvf redacts them from errors and logs, including the path of a webhook URL, which some servers echo back in error messages.

Settings shared by every channel:

| Setting | Default | Meaning |
|---|---|---|
| `name` | the type | Channel name for `dvf notify test --channel` and logs; repeated types become `slack-2`, `slack-3`, ... |
| `on` | `[failed, error]` | Run statuses that send a message: `passed`, `warning`, `failed`, `error` |
| `enabled` | `true` | Turn a channel off without removing it |
| `report_url` | none | Link to the report; `${run.id}`, `${run.date}`, `${run.timestamp}`, and `${run.profile}` are filled in. Without it, messages show the local report path as text |
| `max_failures` | `5` | Failed and errored checks listed, worst first |
| `counts_only` | `false` | Statuses, check IDs, and counts only |
| `include_samples` | `false` | Add masked sample rows to the listed failures |
| `max_sample_rows` | `3` | Sample rows per failure with `include_samples` |
| `timeout_s` | `10` | Timeout of one delivery attempt |
| `retries` | `2` | Retries after a transient failure |
| `retry_backoff_s` | `1` | First retry delay; it doubles on each retry. A `Retry-After` header wins, up to 30 seconds |
| `ca_file` | none | PEM file of certificate authorities to trust (internal CAs), relative to the configuration |

## When messages are sent

- At the end of the run, after the results, reports, history, and result sinks are written, so the report path in the message exists.
- Only to enabled channels whose `on` includes the run's status. An interrupted run counts as `error`.
- If the run stops with an error before it has results (for example an unknown report format), channels with `error` in `on` get a short message with the error.
- Channels are sent at the same time. Each attempt has the channel's `timeout_s`. Network errors, timeouts, HTTP 408, 425, 429, and 5xx responses, and SMTP 4xx replies are retried; other failures are not.
- A failed delivery is logged as a warning, which `dvf run` shows, and never changes the exit code. In Python, `dvf.run(...).notifications` lists each delivery with `channel`, `type`, `ok`, `attempts`, `duration_ms`, and `error`.

## What a message contains

- The project, the run status, the quality score, the check counts by status, rows compared, the run ID, the profile, and the duration.
- The worst failures (up to `max_failures`): check ID, status, failing rows out of total rows, dvf's message, its first hint, and mismatch counts per column. A count of further failures follows.
- New, resolved, and persisting failures compared with the baseline run, when history is on.
- A button or link to `report_url`, or the local path of the HTML report.

### Privacy

- Messages never contain sample values unless you set `include_samples: true`. Samples are then masked exactly as in the reports.
- With `counts_only: true`, or in strict privacy mode (`defaults.privacy.mode: strict`), messages carry statuses, check IDs, and counts only: no messages, hints, mismatch classes, or samples. A suite in strict mode applies this to its own checks.
- Email attachments (`attach_summary`) are left out in both cases.

## Slack

Create an incoming webhook for a Slack app (Slack's *Incoming Webhooks* feature) and use its URL. dvf posts a Block Kit message: a header with the status, fields with the counts, the top failures, a context line, and an "Open report" button when `report_url` is an `http` or `https` URL.

| Setting | Meaning |
|---|---|
| `webhook` | The incoming webhook URL (a secret) |
| `mention` | Text put first, such as `<!here>` or `<!subteam^S0123ABC>` |

The channel is fixed by the webhook; a `channel` setting is not supported.

## Microsoft Teams

Use a Teams **Workflows** webhook: in the channel, add the workflow "Post to a channel when a webhook request is received" (Power Automate) and copy its URL. dvf posts a message with one Adaptive Card (version 1.4): the title in the status color, a fact set with the counts, the top failures, and an "Open report" action. Workflows answer `202 Accepted`.

Differences from the legacy Office 365 connectors ("Incoming Webhook" connector URLs on `webhook.office.com`), which Microsoft is retiring:

- dvf sends the Adaptive Card message format, which legacy connectors also accept; it never sends the older MessageCard format.
- Legacy connectors can answer HTTP 200 with an error text such as "Webhook message delivery failed ...". dvf treats that text as a failure (and retries it when it mentions HTTP 429).
- Workflows run under the account that created them, and the flow decides where the card is posted; check the flow's run history in Power Automate if a delivery succeeds but no card appears.

| Setting | Meaning |
|---|---|
| `webhook` | The Workflows (or legacy connector) URL, a secret |

## Generic webhook

`type: webhook` posts the run summary as JSON to any endpoint:

```json
{
  "schema": "dvf.notification/v1",
  "event": "run_finished",
  "title": "sales_migration: FAILED",
  "run_id": "20261108T101500Z-3f9a",
  "project": "sales_migration",
  "status": "failed",
  "interrupted": false,
  "profile": "prod",
  "started_at": "2026-11-08T10:15:00+00:00",
  "ended_at": "2026-11-08T10:21:12+00:00",
  "duration_ms": 372000.0,
  "summary": {
    "checks_by_status": {"passed": 41, "failed": 2},
    "quality_score": 95.35,
    "rows_compared": 1250000,
    "suites": 1,
    "datasets": 12
  },
  "top_failures": [
    {
      "check_id": "sales.customers.compare.column_mismatch",
      "status": "failed",
      "severity": "error",
      "type": "compare.column_mismatch",
      "suite": "sales",
      "table": "customers",
      "columns": ["email"],
      "unexpected_count": 38,
      "total_count": 9988,
      "unexpected_pct": 0.38,
      "message": "38 rows differ",
      "hints": ["97% of mismatches are whitespace only; consider normalization.trim (rtrim for blank-padded CHAR columns) or collapse_whitespace"],
      "column_mismatches": [
        {"column": "email", "mismatch_count": 38, "mismatch_pct": 0.38,
         "classifications": {"whitespace_only": 37, "value_changed": 1}}
      ]
    }
  ],
  "more_failures": 1,
  "baseline": {"baseline_run_id": "20261107T101500Z-77aa", "new_failures": 1, "resolved": 0, "persisting": 1},
  "report": {"url": "https://ci.example.com/dvf/20261108T101500Z-3f9a/report.html", "path": "C:/work/reports/2026-11-08/20261108T101500Z-3f9a/report.html"},
  "error": null,
  "counts_only": false,
  "dvf_version": "1.0.0"
}
```

Failures carry `samples` (`columns` and `rows`) only with `include_samples`. `event` is `test` for `dvf notify test`.

| Setting | Default | Meaning |
|---|---|---|
| `webhook` | | The endpoint URL (a secret) |
| `method` | `POST` | `POST` or `PUT` |
| `headers` | none | Extra headers; values are secrets |

Redirects are not followed: configure the final URL.

## Email

dvf sends one message to all `recipients`, with a plain-text and an HTML body.

| Setting | Default | Meaning |
|---|---|---|
| `recipients` | | Addresses |
| `sender` | `username`, else `dvf@<host name>` | `From` address |
| `smtp_host`, `smtp_port` | port `587` | The SMTP server |
| `security` | `starttls` | `starttls` (port 587), `ssl` (implicit TLS, port 465), or `none` (unencrypted, for a trusted local relay) |
| `username`, `password` | none | Log in after the connection is secured |
| `use_tls` | `true` | Older switch for STARTTLS; `use_tls: false` means `security: none`. `security` wins when set |
| `subject_prefix` | `[dvf]` | The subject is `<prefix> <project>: <STATUS>` |
| `attach_summary` | `false` | Attach the Markdown summary (`summary.md`) |
| `ca_file` | none | Trust an internal certificate authority |

The server certificate is always verified. dvf refuses to send a password over an unencrypted connection, and fails when the server does not offer STARTTLS with `security: starttls`. A wrong password is not retried.

## Test your channels

```bash
dvf notify test dvf.yaml                     # every enabled channel
dvf notify test dvf.yaml --channel data-ops  # one channel, even if it is disabled
dvf notify test dvf.yaml --format json
```

The test message says it is a test and carries no run data. Exit codes: 0 every message was delivered, 2 a delivery failed, 3 a configuration problem (or no channels).

## Write a notifier plugin

A notifier is a class with a nested `Config` model and a `send(message)` method:

```python
from pydantic import SecretStr

from dvf.plugins import NotificationConfig, Notifier, register_notifier


@register_notifier("acme.pager")
class PagerNotifier(Notifier):
    """Page the on-call engineer."""

    class Config(NotificationConfig):
        service_key: SecretStr

    def __init__(self, config: Config) -> None:
        self.config = config

    def send(self, message) -> None: ...  # raise an exception if delivery fails
```

- dvf calls `send` only for runs whose status is in `on`, with the same privacy-filtered message the built-in channels get.
- Raise an exception when delivery fails. An exception with `transient = True` is retried with the channel's `retries` and backoff. `SecretStr` fields are redacted from errors and logs.
- The message has `title`, `run_id`, `project`, `status`, `status_label`, `counts_text`, `checks_by_status`, `quality_score`, `rows_compared`, `failures` (each with `check_id`, `status`, `severity`, `counts_text`, `message`, `hints`, `column_mismatches`), `more_failures`, `baseline`, `report_url`, `report_path`, `link`, `error`, `counts_only`, and `test`. `message.to_dict()` returns the generic webhook payload above.
- Load the plugin with `plugins.paths` in the configuration, or publish it in the `dvf.notifiers` entry point group.

`examples/plugins/ticket_notifier.py` is a complete example with two notifiers: `example.jira` opens a Jira issue (REST API v2) and `example.servicenow` opens a ServiceNow incident (Table API). It is an example to copy and adapt, not a built-in: field mappings and deduplication differ between organizations.

## What was verified

The Slack, Teams, and generic webhook notifiers are tested end to end against a local HTTP server, and the email notifier against a local SMTP server (STARTTLS, implicit TLS, and login). The tests check the HTTP and SMTP exchanges and the payloads. Real Slack, Teams, Jira, and ServiceNow endpoints were not called; the payload formats follow their published documentation.
