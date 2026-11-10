"""Example notifier plugins: open a Jira issue or a ServiceNow incident when a dvf run fails.

This file is an example of the notifier plugin API, not a built-in component, and it has been
tested only against local test servers that imitate the two APIs. Copy it into your project and
adapt it: field mappings, issue types, assignment groups, and deduplication differ between
organizations. As written it opens one ticket per notifying run; to update an existing ticket for
a persisting failure instead, search first (Jira JQL, or a ServiceNow table query).

Load it from your configuration (paths are relative to the configuration file)::

    plugins:
      paths: [plugins/ticket_notifier.py]

    notifications:
      - type: example.jira
        name: jira
        on: [failed, error]
        base_url: https://your-company.atlassian.net
        project_key: DQ
        user: dq-bot@example.com
        api_token: ${secret:env:JIRA_API_TOKEN}

      - type: example.servicenow
        name: servicenow
        on: [error]
        instance_url: https://your-instance.service-now.com
        user: dvf.integration
        password: ${secret:env:SERVICENOW_PASSWORD}
        assignment_group: Data Platform

Try it with ``dvf notify test dvf.yaml --channel jira`` (this opens a real ticket).

The plugin API used here: subclass :class:`dvf.plugins.Notifier`, declare a nested ``Config``
model that subclasses :class:`dvf.plugins.NotificationConfig`, take the validated config in
``__init__``, and implement ``send(message)``. dvf calls ``send`` only for runs whose status is in
``on``, with a privacy-filtered message (no sample values unless ``include_samples``; counts only
in strict privacy mode). Raise an exception on failure; an exception with ``transient = True``
is retried with backoff. ``SecretStr`` fields are redacted from errors and logs.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from pydantic import Field, SecretStr

from dvf.plugins import NotificationConfig, Notifier, register_notifier

if TYPE_CHECKING:
    # The message type; see docs/notifications.md for its fields.
    from dvf.plugins import NotificationMessage


class TicketError(Exception):
    """A ticket could not be created. ``transient`` asks dvf to retry."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


def _post(url: str, payload: dict[str, Any], user: str, secret: str, timeout: float) -> Any:
    if urlsplit(url).scheme not in ("https", "http"):
        raise TicketError("the URL must start with https://")
    token = base64.b64encode(f"{user}:{secret}".encode()).decode("ascii")
    request = urllib.request.Request(  # noqa: S310 - only http(s), checked above
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Basic {token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        exc.close()
        raise TicketError(
            f"HTTP {exc.code} {exc.reason}", transient=exc.code == 429 or exc.code >= 500
        ) from None
    except (urllib.error.URLError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        raise TicketError(f"connection failed: {reason}", transient=True) from None
    return json.loads(body) if body.strip() else {}


def describe(message: NotificationMessage) -> str:
    """A plain-text ticket description of a run."""
    lines = [
        f"dvf run {message.run_id} of project {message.project} finished with status "
        f"{message.status_label}.",
        f"Checks: {message.counts_text}.",
    ]
    if message.quality_score is not None:
        lines.append(f"Quality score: {message.quality_score:.1f}.")
    if message.error:
        lines.append(f"Error: {message.error}")
    if message.failures:
        lines.append("")
        lines.append("Top failures:")
        for failure in message.failures:
            detail = " - ".join(p for p in (failure.counts_text, failure.message) if p)
            lines.append(f"* {failure.status.value.upper()} {failure.check_id}: {detail}")
        if message.more_failures:
            lines.append(f"... and {message.more_failures} more")
    if message.link:
        lines.append("")
        lines.append(f"Report: {message.link}")
    return "\n".join(lines)


@register_notifier("example.jira")
class JiraIssueNotifier(Notifier):
    """Open a Jira issue for a failed run (example plugin; Jira REST API v2)."""

    class Config(NotificationConfig):
        base_url: str
        """Your Jira site, for example https://your-company.atlassian.net."""
        project_key: str
        user: str
        """The account's email address (Jira Cloud) or user name."""
        api_token: SecretStr
        """An API token (Jira Cloud) or password. Use a ${secret:...} reference."""
        issue_type: str = "Bug"
        labels: list[str] = Field(default_factory=lambda: ["dvf"])

    def __init__(self, config: Config) -> None:
        self.config = config
        self.last_ticket: str | None = None

    def send(self, message: NotificationMessage) -> None:
        config = self.config
        payload = {
            "fields": {
                "project": {"key": config.project_key},
                "issuetype": {"name": config.issue_type},
                "summary": f"[dvf] {message.title}"[:250],
                "description": describe(message),
                "labels": config.labels,
            }
        }
        created = _post(
            config.base_url.rstrip("/") + "/rest/api/2/issue",
            payload,
            config.user,
            config.api_token.get_secret_value(),
            config.timeout_s,
        )
        self.last_ticket = created.get("key") if isinstance(created, dict) else None


@register_notifier("example.servicenow")
class ServiceNowIncidentNotifier(Notifier):
    """Open a ServiceNow incident for a failed run (example plugin; Table API)."""

    class Config(NotificationConfig):
        instance_url: str
        """For example https://your-instance.service-now.com."""
        user: str
        password: SecretStr
        """Use a ${secret:...} reference."""
        assignment_group: str | None = None
        urgency: int = Field(default=2, ge=1, le=3)
        impact: int = Field(default=2, ge=1, le=3)

    def __init__(self, config: Config) -> None:
        self.config = config
        self.last_ticket: str | None = None

    def send(self, message: NotificationMessage) -> None:
        config = self.config
        payload: dict[str, Any] = {
            "short_description": f"[dvf] {message.title}"[:160],
            "description": describe(message),
            "urgency": str(config.urgency),
            "impact": str(config.impact),
        }
        if config.assignment_group:
            payload["assignment_group"] = config.assignment_group
        created = _post(
            config.instance_url.rstrip("/") + "/api/now/table/incident",
            payload,
            config.user,
            config.password.get_secret_value(),
            config.timeout_s,
        )
        result = created.get("result") if isinstance(created, dict) else None
        self.last_ticket = result.get("number") if isinstance(result, dict) else None
