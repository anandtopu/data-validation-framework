"""Sends notifications: picks channels by run status, retries transient failures with backoff,
redacts secrets from errors, and never lets a channel fail the run.

Channels are sent at the same time (up to four at once); each attempt has the channel's
``timeout_s``, and a transient failure (network error, HTTP 408/425/429/5xx, SMTP 4xx) is retried
``retries`` times with exponential backoff, honoring ``Retry-After`` up to 30 seconds.
"""

from __future__ import annotations

import http.client
import logging
import platform
import smtplib
import ssl
import time
import urllib.error
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from pydantic import BaseModel, SecretStr

from dvf import __version__
from dvf.config.notifications import EmailNotification, NotificationConfig
from dvf.core.enums import Status
from dvf.packs.integrations.http import TRANSIENT_HTTP_STATUS
from dvf.packs.integrations.messages import (
    MessageContext,
    NotificationError,
    NotificationMessage,
    build_message,
    crash_message,
    trigger_status,
)
from dvf.plugins.registry import PluginKind, Registry
from dvf.security.redaction import MASK, global_redactor

if TYPE_CHECKING:
    from dvf.config.models import DvfConfig
    from dvf.models.results import RunResult

log = logging.getLogger(__name__)

MAX_RETRY_DELAY_S = 30.0
_MAX_WORKERS = 4
_MIN_URL_PART = 16


@dataclass(frozen=True)
class Channel:
    """A configured notification channel and its resolved name."""

    name: str
    config: NotificationConfig


@dataclass(frozen=True)
class Delivery:
    """The outcome of sending one message to one channel."""

    channel: str
    type: str
    ok: bool
    attempts: int = 0
    duration_ms: float = 0.0
    error: str | None = None
    """What went wrong, with secrets redacted."""

    def describe(self) -> str:
        if self.ok:
            return f"notification '{self.channel}' ({self.type}) sent"
        tries = f" after {self.attempts} attempts" if self.attempts > 1 else ""
        return f"notification '{self.channel}' ({self.type}) failed{tries}: {self.error}"


def channels(config: DvfConfig) -> list[Channel]:
    """Every configured channel with a unique name: its ``name``, else its type (``slack``,
    ``slack-2``, ...)."""
    reserved = {n.name for n in config.notifications if n.name}
    used: set[str] = set()
    result: list[Channel] = []
    for notification in config.notifications:
        base = notification.name or notification.type
        name, i = base, 1
        while name in used or (notification.name is None and name in reserved):
            i += 1
            name = f"{base}-{i}"
        used.add(name)
        result.append(Channel(name, notification))
    return result


def is_transient(exc: BaseException) -> bool:
    """Whether a failed delivery is worth retrying."""
    flag = getattr(exc, "transient", None)
    if isinstance(flag, bool):
        return flag
    if isinstance(exc, smtplib.SMTPResponseException):
        return 400 <= exc.smtp_code < 500
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in TRANSIENT_HTTP_STATUS
    if isinstance(exc, ssl.SSLError):
        return False
    return isinstance(
        exc, TimeoutError | ConnectionError | urllib.error.URLError | http.client.HTTPException
    )


def secret_values(model: BaseModel) -> list[str]:
    """Every secret value in a configuration model (``SecretStr`` fields, also in dicts and
    nested models), so they can be redacted from errors."""
    values: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, SecretStr):
            secret = value.get_secret_value()
            if secret:
                values.append(secret)
        elif isinstance(value, BaseModel):
            for name in type(value).model_fields:
                walk(getattr(value, name))
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list | tuple):
            for item in value:
                walk(item)

    walk(model)
    return [*values, *(part for v in values for part in _url_parts(v))]


def _url_parts(value: str) -> list[str]:
    """The path and query of a secret URL, which servers may echo back without the host."""
    try:
        parts = urlsplit(value)
    except ValueError:
        return []
    if parts.scheme.lower() not in ("http", "https"):
        return []
    return [p for p in (parts.path, parts.query) if len(p) >= _MIN_URL_PART]


def redact(text: str, secrets: Sequence[str]) -> str:
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, MASK)
    return global_redactor().redact_text(text)


class Dispatcher:
    """Delivers messages to channels."""

    def __init__(
        self,
        registry: Registry,
        base_dir: Path | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.registry = registry
        self.base_dir = base_dir
        self._sleep = sleep

    def _resolved(self, config: NotificationConfig) -> NotificationConfig:
        if config.ca_file and self.base_dir is not None and not Path(config.ca_file).is_absolute():
            return config.model_copy(update={"ca_file": str(self.base_dir / config.ca_file)})
        return config

    def _notifier(self, config: NotificationConfig) -> Any:
        entry = self.registry.find(PluginKind.NOTIFIER, config.type)
        if entry is None:
            raise _ChannelSetupError(f"unknown notifier type '{config.type}'")
        if not entry.ready:
            raise _ChannelSetupError(
                f"notifier type '{config.type}' is declared but not implemented in this version"
            )
        notifier = entry.implementation(config)
        if not callable(getattr(notifier, "send", None)):
            raise _ChannelSetupError(f"notifier '{config.type}' has no send(message) method")
        return notifier

    def deliver(self, channel: Channel, message: NotificationMessage) -> Delivery:
        """Send ``message`` to ``channel``, retrying transient failures. Never raises."""
        config = self._resolved(channel.config)
        secrets = secret_values(config)
        redactor = global_redactor()
        for secret in secrets:
            redactor.add(secret)
        started = time.perf_counter()
        attempts = 0
        try:
            notifier = self._notifier(config)
        except Exception as exc:
            return self._done(channel, False, attempts, started, _error_text(exc), secrets)
        while True:
            attempts += 1
            try:
                notifier.send(message)
            except Exception as exc:
                error = _error_text(exc)
                if attempts > config.retries or not is_transient(exc):
                    return self._done(channel, False, attempts, started, error, secrets)
                delay = getattr(exc, "retry_after", None)
                if not isinstance(delay, int | float):
                    delay = config.retry_backoff_s * 2 ** (attempts - 1)
                delay = min(float(delay), MAX_RETRY_DELAY_S)
                log.info(
                    "notification '%s' attempt %d failed (%s); retrying in %.1fs",
                    channel.name,
                    attempts,
                    redact(error, secrets),
                    delay,
                )
                self._sleep(delay)
            else:
                return self._done(channel, True, attempts, started, None, secrets)

    @staticmethod
    def _done(
        channel: Channel,
        ok: bool,
        attempts: int,
        started: float,
        error: str | None,
        secrets: Sequence[str],
    ) -> Delivery:
        delivery = Delivery(
            channel=channel.name,
            type=channel.config.type,
            ok=ok,
            attempts=attempts,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            error=None if error is None else redact(error, secrets),
        )
        if ok:
            log.info("%s in %.0f ms", delivery.describe(), delivery.duration_ms)
        else:
            log.warning("%s", delivery.describe())
        return delivery

    def deliver_all(self, items: Sequence[tuple[Channel, NotificationMessage]]) -> list[Delivery]:
        """Send every message, channels in parallel; results in the order given."""
        if not items:
            return []
        if len(items) == 1:
            return [self.deliver(*items[0])]
        workers = min(_MAX_WORKERS, len(items))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dvf-notify") as pool:
            return list(pool.map(lambda item: self.deliver(*item), items))


class _ChannelSetupError(Exception):
    """A channel cannot be used (unknown or unimplemented type, no ``send`` method)."""


def _error_text(exc: BaseException) -> str:
    text = str(exc)
    if isinstance(exc, NotificationError | _ChannelSetupError) and text:
        return text
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


# ---------------------------------------------------------------------- entry points


def _with_summary(
    message: NotificationMessage,
    config: NotificationConfig,
    result: RunResult,
    context: MessageContext,
) -> NotificationMessage:
    if not (isinstance(config, EmailNotification) and config.attach_summary) or message.counts_only:
        return message
    path = context.artifact_path("markdown")
    text: str | None = None
    if path is not None and path.is_file():
        text = path.read_text(encoding="utf-8")
    else:
        from dvf.reporting.base import ReportOptions
        from dvf.reporting.markdown import render_markdown

        text = render_markdown(result, ReportOptions())
    return replace(message, summary_markdown=text)


def notify_run(
    result: RunResult,
    config: DvfConfig,
    registry: Registry,
    context: MessageContext,
    base_dir: Path | None = None,
    *,
    dispatcher: Dispatcher | None = None,
) -> list[Delivery]:
    """Notify every enabled channel whose ``on`` includes the run's status."""
    status = trigger_status(result)
    items: list[tuple[Channel, NotificationMessage]] = []
    for channel in channels(config):
        if not channel.config.enabled or status not in channel.config.on:
            continue
        try:
            message = build_message(result, channel.config, context)
            message = _with_summary(message, channel.config, result, context)
        except Exception as exc:  # building a message must never fail the run
            log.warning("notification '%s' could not be prepared: %s", channel.name, exc)
            continue
        items.append((channel, message))
    return (dispatcher or Dispatcher(registry, base_dir)).deliver_all(items)


def notify_crash(
    run_id: str,
    error: str,
    config: DvfConfig,
    registry: Registry,
    context: MessageContext,
    base_dir: Path | None = None,
    *,
    started_at: datetime | None = None,
    dispatcher: Dispatcher | None = None,
) -> list[Delivery]:
    """Notify channels whose ``on`` includes ``error`` that the run stopped with an error."""
    items = [
        (
            channel,
            crash_message(
                run_id,
                config.project.name,
                error,
                channel.config,
                context,
                started_at=started_at,
            ),
        )
        for channel in channels(config)
        if channel.config.enabled and Status.ERROR in channel.config.on
    ]
    return (dispatcher or Dispatcher(registry, base_dir)).deliver_all(items)


def test_result(config: DvfConfig) -> RunResult:
    """A synthetic, passed run with no checks, used for ``dvf notify test``."""
    from dvf.models.results import RunEnvironment, RunResult

    now = datetime.now(UTC)
    return RunResult(
        run_id=now.strftime("test-%Y%m%dT%H%M%SZ"),
        project=config.project.name,
        status=Status.PASSED,
        started_at=now,
        ended_at=now,
        duration_ms=0.0,
        environment=RunEnvironment(
            dvf_version=__version__,
            python_version=platform.python_version(),
            platform=platform.platform(),
        ),
    )


def notify_test(
    config: DvfConfig,
    registry: Registry,
    base_dir: Path | None = None,
    *,
    channel: str | None = None,
    dispatcher: Dispatcher | None = None,
) -> list[Delivery]:
    """Send a test message to every enabled channel, or to the named channel (even if it is
    disabled). Raises :class:`LookupError` for an unknown channel name."""
    available = channels(config)
    if channel is not None:
        selected = [c for c in available if c.name == channel]
        if not selected:
            names = ", ".join(c.name for c in available) or "none"
            raise LookupError(f"No notification channel named '{channel}'. Channels: {names}.")
    else:
        selected = [c for c in available if c.config.enabled]
    result = test_result(config)
    context = MessageContext.for_run(config, None)
    items = [(c, build_message(result, c.config, context, test=True)) for c in selected]
    return (dispatcher or Dispatcher(registry, base_dir)).deliver_all(items)


__all__ = [
    "Channel",
    "Delivery",
    "Dispatcher",
    "channels",
    "is_transient",
    "notify_crash",
    "notify_run",
    "notify_test",
    "redact",
    "secret_values",
    "test_result",
]
