"""A small JSON-over-HTTP client for notifiers and the OpenLineage transport (standard library).

Only ``http`` and ``https`` URLs are accepted, redirects are not followed (a webhook that redirects
is misconfigured, and following it could send the payload elsewhere), and error messages never
contain the URL: webhook URLs are secrets. Proxies set in ``HTTPS_PROXY``/``HTTP_PROXY`` apply.
"""

from __future__ import annotations

import http.client
import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

from dvf import __version__
from dvf.packs.integrations.messages import NotificationError

TRANSIENT_HTTP_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
_MAX_BODY = 64 * 1024
_MAX_SNIPPET = 300


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: str
    headers: dict[str, str] = field(default_factory=dict)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _opener(ca_file: str | None) -> urllib.request.OpenerDirector:
    context = ssl.create_default_context(cafile=ca_file) if ca_file else None
    return urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=context))


def retry_after_seconds(value: str | None) -> float | None:
    """Seconds from a ``Retry-After`` header (delta seconds or an HTTP date)."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (moment - datetime.now(UTC)).total_seconds())


def post_json(
    url: str,
    payload: Any,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
    method: str = "POST",
    ca_file: str | None = None,
) -> HttpResponse:
    """Send ``payload`` as JSON and return the response.

    Raises :class:`NotificationError` (``transient`` for network errors, timeouts, HTTP 408, 425,
    429, and 5xx) on failure.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        raise NotificationError("the URL is not valid") from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise NotificationError(
            f"unsupported URL scheme '{scheme or '(none)'}'; use an http or https URL"
        )
    if not host:
        raise NotificationError("the URL has no host")
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    request_headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": f"dvf/{__version__}",
        **(headers or {}),
    }
    try:
        request = urllib.request.Request(  # noqa: S310 - only http(s), checked above
            url, data=body, headers=request_headers, method=method
        )
    except ValueError:
        raise NotificationError("the URL is not valid") from None
    try:
        with _opener(ca_file).open(request, timeout=timeout) as response:
            text = response.read(_MAX_BODY).decode("utf-8", errors="replace")
            return HttpResponse(response.status, text, dict(response.headers.items()))
    except urllib.error.HTTPError as exc:
        raise _http_error(exc) from None
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ssl.SSLError):
            raise NotificationError(f"TLS error: {_reason(reason)}") from None
        raise NotificationError(f"connection failed: {_reason(reason)}", transient=True) from None
    except TimeoutError:
        raise NotificationError(f"timed out after {timeout:g}s", transient=True) from None
    except ssl.SSLError as exc:
        raise NotificationError(f"TLS error: {_reason(exc)}") from None
    except (ConnectionError, http.client.HTTPException) as exc:
        raise NotificationError(
            f"connection failed: {type(exc).__name__}: {exc}", transient=True
        ) from None


def _reason(reason: object) -> str:
    if isinstance(reason, TimeoutError):
        return "timed out"
    text = str(reason)
    return text or type(reason).__name__


def _http_error(exc: urllib.error.HTTPError) -> NotificationError:
    code = exc.code
    try:
        snippet = exc.read(_MAX_SNIPPET * 2).decode("utf-8", errors="replace")
    except Exception:
        snippet = ""
    finally:
        exc.close()
    snippet = " ".join(snippet.split())[:_MAX_SNIPPET]
    message = f"HTTP {code} {exc.reason}"
    if 300 <= code < 400:
        message += ": redirects are not followed; configure the final URL"
    elif snippet:
        message += f": {snippet}"
    retry_after = retry_after_seconds(exc.headers.get("Retry-After") if exc.headers else None)
    return NotificationError(
        message, transient=code in TRANSIENT_HTTP_STATUS, retry_after=retry_after, status=code
    )


__all__ = ["TRANSIENT_HTTP_STATUS", "HttpResponse", "post_json", "retry_after_seconds"]
