"""Built-in notifiers: Slack (Block Kit), Microsoft Teams (Adaptive Card), generic webhook (JSON),
and email (SMTP with HTML and plain-text bodies).

A notifier takes its validated configuration and implements ``send(message)``; it raises on
failure (:class:`~dvf.packs.integrations.messages.NotificationError` with ``transient=True`` asks
for a retry). Retries, timeouts across channels, redaction, and logging are the dispatcher's job
(:mod:`dvf.packs.integrations.dispatch`).
"""

from __future__ import annotations

import html
import smtplib
import socket
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from typing import Any, ClassVar

from dvf.config.notifications import (
    EmailNotification,
    GenericWebhookNotification,
    NotificationConfig,
    SlackNotification,
    TeamsNotification,
    WebhookNotification,
)
from dvf.core.enums import Status
from dvf.packs.integrations.http import post_json
from dvf.packs.integrations.messages import FailureSummary, NotificationError, NotificationMessage
from dvf.plugins.base import Notifier

_SLACK_ICON = {
    Status.PASSED: ":white_check_mark:",
    Status.WARNING: ":warning:",
    Status.FAILED: ":x:",
    Status.ERROR: ":rotating_light:",
    Status.SKIPPED: ":fast_forward:",
    Status.WAIVED: ":white_check_mark:",
}
_TEAMS_COLOR = {
    Status.PASSED: "Good",
    Status.WARNING: "Warning",
    Status.FAILED: "Attention",
    Status.ERROR: "Attention",
}
_HTML_COLOR = {
    Status.PASSED: "#1a7f37",
    Status.WARNING: "#9a6700",
    Status.FAILED: "#cf222e",
    Status.ERROR: "#cf222e",
}
_SLACK_SECTION_LIMIT = 2900
_LEGACY_TEAMS_FAILURE = "webhook message delivery failed"


def _is_web_url(value: str | None) -> bool:
    return bool(value) and str(value).lower().startswith(("https://", "http://"))


def _facts(message: NotificationMessage) -> list[tuple[str, str]]:
    facts = [("Status", message.status_label)]
    if message.error is None:
        score = message.quality_score
        facts.append(("Quality score", "n/a" if score is None else f"{score:.1f}"))
        facts.append(("Checks", message.counts_text))
        if message.rows_compared:
            facts.append(("Rows compared", f"{message.rows_compared:,}"))
    facts.append(("Run", message.run_id))
    if message.profile:
        facts.append(("Profile", message.profile))
    if message.duration_ms is not None:
        facts.append(("Duration", f"{message.duration_ms / 1000:.1f}s"))
    return facts


def _failure_detail(failure: FailureSummary) -> str:
    parts = [p for p in (failure.counts_text, failure.message) if p]
    if failure.column_mismatches:
        columns = ", ".join(
            f"{c.column} {c.mismatch_count:,}" for c in failure.column_mismatches[:5]
        )
        parts.append(f"columns: {columns}")
    return " - ".join(parts)


def _sample_lines(failure: FailureSummary) -> list[str]:
    if not failure.sample_rows:
        return []
    lines = [" | ".join(failure.sample_columns)]
    lines += [
        " | ".join("NULL" if v is None else str(v) for v in row) for row in failure.sample_rows
    ]
    return lines


def _baseline_text(message: NotificationMessage) -> str | None:
    b = message.baseline
    if not b:
        return None
    return (
        f"vs run {b['baseline_run_id']}: {b['new_failures']} new, {b['resolved']} resolved, "
        f"{b['persisting']} persisting failures"
    )


def _trim(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------- Slack


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_payload(message: NotificationMessage, mention: str | None = None) -> dict[str, Any]:
    """A Slack incoming-webhook payload with Block Kit blocks and a plain-text fallback."""
    icon = ":test_tube:" if message.test else _SLACK_ICON.get(message.status, "")
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": _trim(f"{icon} {message.title}".strip(), 150),
                "emoji": True,
            },
        }
    ]
    if mention:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": mention}})
    fields = [
        {"type": "mrkdwn", "text": _trim(f"*{name}*\n{_slack_escape(value)}", 1900)}
        for name, value in _facts(message)
    ]
    blocks.append({"type": "section", "fields": fields[:10]})
    if message.error:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": _trim(f"*Error*\n{_slack_escape(message.error)}", 2900),
                },
            }
        )
    if message.failures:
        lines = ["*Top failures*"]
        for failure in message.failures:
            line = f"{_SLACK_ICON.get(failure.status, '')} `{_slack_escape(failure.check_id)}`"
            detail = _failure_detail(failure)
            if detail:
                line += f" - {_slack_escape(detail)}"
            lines.append(line)
            if failure.hints:
                lines.append(f"    _{_slack_escape(failure.hints[0])}_")
            samples = _sample_lines(failure)
            if samples:
                lines.append("```" + _slack_escape("\n".join(samples)) + "```")
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": _trim("\n".join(lines), _SLACK_SECTION_LIMIT)},
            }
        )
    context: list[str] = []
    if message.more_failures:
        context.append(f"... and {message.more_failures} more")
    baseline = _baseline_text(message)
    if baseline:
        context.append(_slack_escape(baseline))
    if message.report_url and _is_web_url(message.report_url):
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Open report"},
                        "url": message.report_url,
                    }
                ],
            }
        )
    elif message.link:
        context.append(f"Report: `{_slack_escape(message.link)}`")
    context.append(f"dvf {_slack_escape(message.dvf_version)}")
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": _trim(t, 2900)} for t in context[:10]],
        }
    )
    fallback = f"{message.title} ({message.counts_text})"
    if mention:
        fallback = f"{mention} {fallback}"
    return {"text": fallback, "blocks": blocks}


# ---------------------------------------------------------------------- Teams


def teams_payload(message: NotificationMessage) -> dict[str, Any]:
    """A Teams message with one Adaptive Card (version 1.4), as Workflows webhooks expect."""
    body: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": message.title,
            "size": "Large",
            "weight": "Bolder",
            "color": "Default" if message.test else _TEAMS_COLOR.get(message.status, "Default"),
            "wrap": True,
        },
        {
            "type": "FactSet",
            "facts": [{"title": name, "value": value} for name, value in _facts(message)],
        },
    ]
    if message.error:
        body.append(
            {"type": "TextBlock", "text": message.error, "color": "Attention", "wrap": True}
        )
    if message.failures:
        body.append(
            {
                "type": "TextBlock",
                "text": "Top failures",
                "weight": "Bolder",
                "spacing": "Medium",
                "wrap": True,
            }
        )
        for failure in message.failures:
            items: list[dict[str, Any]] = [
                {
                    "type": "TextBlock",
                    "text": f"{failure.status.value.upper()} {failure.check_id}",
                    "weight": "Bolder",
                    "wrap": True,
                }
            ]
            detail = _failure_detail(failure)
            if detail:
                items.append({"type": "TextBlock", "text": detail, "isSubtle": True, "wrap": True})
            if failure.hints:
                items.append(
                    {"type": "TextBlock", "text": failure.hints[0], "isSubtle": True, "wrap": True}
                )
            samples = _sample_lines(failure)
            if samples:
                items.append(
                    {
                        "type": "TextBlock",
                        "text": "\n\n".join(samples),
                        "fontType": "Monospace",
                        "wrap": True,
                    }
                )
            body.append({"type": "Container", "spacing": "Small", "items": items})
    notes: list[str] = []
    if message.more_failures:
        notes.append(f"... and {message.more_failures} more")
    baseline = _baseline_text(message)
    if baseline:
        notes.append(baseline)
    if message.link and not _is_web_url(message.report_url):
        notes.append(f"Report: {message.link}")
    notes.append(f"dvf {message.dvf_version}")
    body.append(
        {
            "type": "TextBlock",
            "text": "\n\n".join(notes),
            "isSubtle": True,
            "size": "Small",
            "wrap": True,
        }
    )
    card: dict[str, Any] = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "msteams": {"width": "Full"},
        "body": body,
    }
    if message.report_url and _is_web_url(message.report_url):
        card["actions"] = [
            {"type": "Action.OpenUrl", "title": "Open report", "url": message.report_url}
        ]
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": card,
            }
        ],
    }


# ---------------------------------------------------------------------- email


def email_subject(message: NotificationMessage, prefix: str) -> str:
    return f"{prefix} {message.title}".strip()


def email_text(message: NotificationMessage) -> str:
    lines = [message.title, "=" * min(len(message.title), 72), ""]
    width = max(len(name) for name, _ in _facts(message))
    lines += [f"{name.ljust(width)}  {value}" for name, value in _facts(message)]
    if message.error:
        lines += ["", "Error:", message.error]
    if message.failures:
        lines += ["", "Top failures:"]
        for failure in message.failures:
            lines.append(f"- {failure.status.value.upper()} {failure.check_id}")
            detail = _failure_detail(failure)
            if detail:
                lines.append(f"  {detail}")
            lines += [f"  hint: {hint}" for hint in failure.hints[:1]]
            lines += [f"    {row}" for row in _sample_lines(failure)]
        if message.more_failures:
            lines.append(f"... and {message.more_failures} more")
    baseline = _baseline_text(message)
    if baseline:
        lines += ["", baseline]
    if message.link:
        lines += ["", f"Report: {message.link}"]
    lines += ["", f"Sent by dvf {message.dvf_version}."]
    return "\n".join(lines) + "\n"


def email_html(message: NotificationMessage) -> str:
    esc = html.escape
    color = "#1f2328" if message.test else _HTML_COLOR.get(message.status, "#1f2328")
    facts = "".join(
        f'<tr><td style="padding:2px 12px 2px 0;color:#59636e">{esc(name)}</td>'
        f"<td>{esc(value)}</td></tr>"
        for name, value in _facts(message)
    )
    parts = [
        '<!DOCTYPE html><html><body style="font-family:Segoe UI,Helvetica,Arial,sans-serif;'
        'font-size:14px;color:#1f2328">',
        f'<h2 style="margin:0 0 12px;color:{color}">{esc(message.title)}</h2>',
        f'<table style="border-collapse:collapse">{facts}</table>',
    ]
    if message.error:
        parts.append(f'<p style="color:#cf222e">{esc(message.error)}</p>')
    if message.failures:
        rows = []
        for failure in message.failures:
            detail = esc(_failure_detail(failure))
            if failure.hints:
                detail += f'<br><span style="color:#59636e">{esc(failure.hints[0])}</span>'
            samples = _sample_lines(failure)
            if samples:
                detail += f'<pre style="margin:4px 0">{esc(chr(10).join(samples))}</pre>'
            rows.append(
                f'<tr><td style="padding:4px 8px;color:{_HTML_COLOR.get(failure.status, "#1f2328")}">'
                f"{esc(failure.status.value.upper())}</td>"
                f'<td style="padding:4px 8px"><code>{esc(failure.check_id)}</code></td>'
                f'<td style="padding:4px 8px">{detail}</td></tr>'
            )
        parts += [
            '<h3 style="margin:16px 0 8px">Top failures</h3>',
            '<table style="border-collapse:collapse" border="1" cellspacing="0" '
            'bordercolor="#d0d7de">',
            '<tr><th style="padding:4px 8px;text-align:left">Status</th>'
            '<th style="padding:4px 8px;text-align:left">Check</th>'
            '<th style="padding:4px 8px;text-align:left">Details</th></tr>',
            *rows,
            "</table>",
        ]
        if message.more_failures:
            parts.append(f"<p>... and {message.more_failures} more</p>")
    baseline = _baseline_text(message)
    if baseline:
        parts.append(f"<p>{esc(baseline)}</p>")
    if message.report_url and _is_web_url(message.report_url):
        parts.append(f'<p><a href="{esc(message.report_url, quote=True)}">Open the report</a></p>')
    elif message.link:
        parts.append(f"<p>Report: <code>{esc(message.link)}</code></p>")
    parts.append(
        f'<p style="color:#59636e;font-size:12px">Sent by dvf {esc(message.dvf_version)}.</p>'
        "</body></html>"
    )
    return "".join(parts)


def build_email(message: NotificationMessage, config: EmailNotification) -> EmailMessage:
    sender = config.sender or (
        config.username if config.username and "@" in config.username else None
    )
    sender = sender or f"dvf@{socket.gethostname() or 'localhost'}"
    domain = parseaddr(sender)[1].rpartition("@")[2] or "localhost"
    email = EmailMessage()
    email["Subject"] = email_subject(message, config.subject_prefix)
    email["From"] = sender
    email["To"] = ", ".join(config.recipients)
    email["Date"] = formatdate(localtime=True)
    email["Message-ID"] = make_msgid(domain=domain)
    email.set_content(email_text(message))
    email.add_alternative(email_html(message), subtype="html")
    if message.summary_markdown:
        email.add_attachment(
            message.summary_markdown.encode("utf-8"),
            maintype="text",
            subtype="markdown",
            filename="summary.md",
        )
    return email


# ---------------------------------------------------------------------- notifiers


class ChannelNotifier(Notifier):
    """Base of the built-in notifiers: holds the channel's configuration."""

    Config: ClassVar[type[NotificationConfig]] = NotificationConfig

    def __init__(self, config: NotificationConfig) -> None:
        self.config = config

    def send(self, message: NotificationMessage) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def _post(self, url: str, payload: Any, **kwargs: Any) -> str:
        response = post_json(
            url, payload, timeout=self.config.timeout_s, ca_file=self.config.ca_file, **kwargs
        )
        return response.body


class SlackNotifier(ChannelNotifier):
    """Slack incoming webhook (Block Kit message)."""

    Config = SlackNotification

    def send(self, message: NotificationMessage) -> None:
        config = self.config
        assert isinstance(config, WebhookNotification)
        mention = config.mention if isinstance(config, SlackNotification) else None
        self._post(config.webhook.get_secret_value(), slack_payload(message, mention))


class TeamsNotifier(ChannelNotifier):
    """Microsoft Teams Workflows webhook (Adaptive Card); also accepted by legacy connectors."""

    Config = TeamsNotification

    def send(self, message: NotificationMessage) -> None:
        config = self.config
        assert isinstance(config, WebhookNotification)
        body = self._post(config.webhook.get_secret_value(), teams_payload(message))
        # Legacy Office 365 connectors answer HTTP 200 with an error text instead of a status.
        if _LEGACY_TEAMS_FAILURE in body.lower():
            raise NotificationError(
                f"Teams rejected the message: {' '.join(body.split())[:300]}",
                transient="429" in body,
            )


class WebhookNotifier(ChannelNotifier):
    """Generic HTTP webhook: the run summary as JSON (``schema: dvf.notification/v1``)."""

    Config = GenericWebhookNotification

    def send(self, message: NotificationMessage) -> None:
        config = self.config
        assert isinstance(config, WebhookNotification)
        headers: dict[str, str] = {}
        method = "POST"
        if isinstance(config, GenericWebhookNotification):
            headers = {k: v.get_secret_value() for k, v in config.headers.items()}
            method = config.method
        self._post(
            config.webhook.get_secret_value(), message.to_dict(), headers=headers, method=method
        )


class EmailNotifier(ChannelNotifier):
    """Email through SMTP (STARTTLS, implicit TLS, or a plain local relay)."""

    Config = EmailNotification

    def send(self, message: NotificationMessage) -> None:
        config = self.config
        assert isinstance(config, EmailNotification)
        send_email(config, build_email(message, config))


def send_email(config: EmailNotification, email: EmailMessage) -> None:
    security = config.effective_security
    if config.username and security == "none":
        raise NotificationError(
            "refusing to send the SMTP password over an unencrypted connection; "
            "set security to starttls or ssl"
        )
    context = ssl.create_default_context(cafile=config.ca_file) if security != "none" else None
    client: smtplib.SMTP
    if security == "ssl":
        client = smtplib.SMTP_SSL(
            config.smtp_host, config.smtp_port, timeout=config.timeout_s, context=context
        )
    else:
        client = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=config.timeout_s)
    with client:
        client.ehlo()
        if security == "starttls":
            if not client.has_extn("starttls"):
                raise NotificationError(
                    "the SMTP server does not offer STARTTLS; set security to ssl, or to none "
                    "for a trusted local relay"
                )
            client.starttls(context=context)
            client.ehlo()
        if config.username:
            password = config.password.get_secret_value() if config.password else ""
            client.login(config.username, password)
        refused = client.send_message(email)
    if refused:
        raise NotificationError(f"the SMTP server refused recipients: {', '.join(sorted(refused))}")


BUILTIN_NOTIFIERS: dict[str, type[ChannelNotifier]] = {
    "slack": SlackNotifier,
    "teams": TeamsNotifier,
    "webhook": WebhookNotifier,
    "email": EmailNotifier,
}

__all__ = [
    "BUILTIN_NOTIFIERS",
    "ChannelNotifier",
    "EmailNotifier",
    "SlackNotifier",
    "TeamsNotifier",
    "WebhookNotifier",
    "build_email",
    "email_html",
    "email_text",
    "send_email",
    "slack_payload",
    "teams_payload",
]
