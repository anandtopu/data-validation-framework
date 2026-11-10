"""Notification configuration models (BUILD_PROMPT.md section 12.8).

Each channel type registers one of these models. Webhook URLs, passwords, and header values are
secrets: give them as ``${secret:...}`` references, never inline.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    Field,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    SecretStr,
)

from dvf.config.base import Identifier, StrictModel
from dvf.core.enums import Status


class NotificationConfig(StrictModel):
    """Settings shared by every notification channel."""

    type: str
    """Channel type, for example ``teams``, ``slack``, ``email``, or ``webhook``."""
    name: Identifier | None = None
    """Channel name, used by ``dvf notify test --channel`` and in logs. Defaults to the type, with
    ``-2``, ``-3``, ... appended when a type is used more than once."""
    on: list[Status] = Field(default_factory=lambda: [Status.FAILED, Status.ERROR], min_length=1)
    """Run statuses that trigger a notification. An interrupted run counts as ``error``."""
    enabled: bool = True
    """Set to false to stop notifying this channel without removing it."""
    options: dict[str, Any] = Field(default_factory=dict)
    """Additional channel-specific settings."""
    report_url: str | None = None
    """Link to the report shown in messages, for example
    ``https://ci.example.com/dvf/${run.id}/report.html``. Without it, messages show the local
    report path as text."""
    max_failures: NonNegativeInt = Field(default=5, le=50)
    """Failed and errored checks listed in a message (the worst first)."""
    counts_only: bool = False
    """Send statuses, check IDs, and counts only: no messages, hints, or samples. Always the case
    in strict privacy mode (``defaults.privacy.mode: strict``)."""
    include_samples: bool = False
    """Include masked sample rows of the listed failures. Off by default; never in strict privacy
    mode or with ``counts_only``."""
    max_sample_rows: PositiveInt = Field(default=3, le=20)
    """Sample rows per failure when ``include_samples`` is on."""
    timeout_s: PositiveFloat = Field(default=10.0, le=300)
    """Timeout of one delivery attempt, in seconds."""
    retries: NonNegativeInt = Field(default=2, le=10)
    """Retries after a transient failure (a network error, HTTP 429, or a 5xx response)."""
    retry_backoff_s: NonNegativeFloat = Field(default=1.0, le=60)
    """First retry delay in seconds; it doubles on each retry. A ``Retry-After`` header wins, up
    to 30 seconds."""
    ca_file: str | None = None
    """A PEM file of certificate authorities to trust for HTTPS or SMTP TLS (relative to the
    configuration), for servers with an internal CA."""


class WebhookNotification(NotificationConfig):
    """A chat or HTTP webhook (Teams, Slack, or generic)."""

    webhook: SecretStr
    """Webhook URL. Treated as a secret because the URL grants posting rights."""


class SlackNotification(WebhookNotification):
    """A Slack incoming webhook; messages use Block Kit."""

    mention: str | None = None
    """Text put before the message, for example ``<!here>`` or ``<!subteam^S0123ABC>``."""


class TeamsNotification(WebhookNotification):
    """A Microsoft Teams Workflows (Power Automate) webhook; messages are Adaptive Cards."""


class GenericWebhookNotification(WebhookNotification):
    """Any HTTP endpoint that accepts a JSON run summary."""

    method: Literal["POST", "PUT"] = "POST"
    """HTTP method used to send the JSON run summary."""
    headers: dict[str, SecretStr] = Field(default_factory=dict)
    """Extra HTTP headers, such as ``Authorization``. Values are treated as secrets."""


class EmailNotification(NotificationConfig):
    """Email through SMTP, with HTML and plain-text bodies."""

    recipients: list[str] = Field(min_length=1)
    """Email addresses the message is sent to."""
    sender: str | None = None
    """``From`` address; defaults to ``username``, or ``dvf@<host name>``."""
    smtp_host: str
    """SMTP server host name."""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    """SMTP server port. The default suits ``starttls``; implicit TLS (``ssl``) usually uses 465."""
    username: str | None = None
    """Log in with this user (and ``password``) after the connection is secured."""
    password: SecretStr | None = None
    """Password of ``username``, never sent over an unencrypted connection. Use a ``${secret:...}``
    reference; it is treated as a secret."""
    use_tls: bool = True
    """Upgrade the connection with STARTTLS. Kept for compatibility; ``security`` wins when set."""
    security: Literal["starttls", "ssl", "none"] | None = None
    """``starttls`` (port 587), ``ssl`` (implicit TLS, port 465), or ``none`` (unencrypted, for
    local relays only). Defaults to ``starttls``, or ``none`` when ``use_tls`` is false."""
    subject_prefix: str = "[dvf]"
    """The subject is ``<prefix> <project>: <STATUS>``."""
    attach_summary: bool = False
    """Attach the Markdown summary (``summary.md``) of the run."""

    @property
    def effective_security(self) -> Literal["starttls", "ssl", "none"]:
        if self.security is not None:
            return self.security
        return "starttls" if self.use_tls else "none"
