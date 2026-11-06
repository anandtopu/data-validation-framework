"""Notification configuration models."""

from __future__ import annotations

from typing import Any

from pydantic import Field, SecretStr

from dvf.config.base import StrictModel
from dvf.core.enums import Status


class NotificationConfig(StrictModel):
    """Settings shared by every notification channel."""

    type: str
    """Channel type, for example ``teams``, ``slack``, ``email``, or ``webhook``."""
    on: list[Status] = Field(default_factory=lambda: [Status.FAILED, Status.ERROR], min_length=1)
    """Run statuses that trigger a notification."""
    enabled: bool = True
    options: dict[str, Any] = Field(default_factory=dict)


class WebhookNotification(NotificationConfig):
    """A chat or HTTP webhook (Teams, Slack, or generic)."""

    webhook: SecretStr
    """Webhook URL. Treated as a secret because the URL grants posting rights."""


class EmailNotification(NotificationConfig):
    """Email through SMTP."""

    recipients: list[str] = Field(min_length=1)
    sender: str | None = None
    smtp_host: str
    smtp_port: int = Field(default=587, ge=1, le=65535)
    username: str | None = None
    password: SecretStr | None = None
    use_tls: bool = True
