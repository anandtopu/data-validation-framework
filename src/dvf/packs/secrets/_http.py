"""A small HTTPS JSON client on the standard library, for the Vault and Databricks providers.

Requests go only to the endpoint the configuration names. Redirects are refused (a redirect would
forward the credential header to another host), plain HTTP is refused except to the local machine
unless explicitly allowed, and credentials in URLs are rejected. Error messages carry the status
and the service's own error text, redacted, and never the request headers.
"""

from __future__ import annotations

import ipaddress
import json
import ssl
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

from dvf.core.errors import SecretError
from dvf.security.redaction import Redactor
from dvf.security.secrets import safe_detail

_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain"})


class HttpStatusError(SecretError):
    """The service answered with an error status."""

    def __init__(self, message: str, status: int, detail: str, error_code: str | None) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail
        self.error_code = error_code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def is_loopback(host: str) -> bool:
    """True for ``localhost`` and loopback IP addresses."""
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def check_endpoint(url: str, *, what: str, allow_http: bool) -> str:
    """Validate a service base URL and return it without a trailing slash."""
    parsed = urlsplit(url.strip())
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise SecretError(f"The {what} address '{url}' is not an http(s) URL with a host name.")
    if parsed.username is not None or parsed.password is not None:
        raise SecretError(
            f"The {what} address must not contain credentials; use the provider's token settings."
        )
    if parsed.query or parsed.fragment:
        raise SecretError(f"The {what} address must not contain a query string or fragment.")
    if parsed.scheme == "http" and not allow_http and not is_loopback(parsed.hostname):
        raise SecretError(
            f"Refusing to send credentials to {what} over plain HTTP ({parsed.hostname}). "
            "Use an https:// address, or set allow_insecure_http for a test server."
        )
    return url.strip().rstrip("/")


@dataclass
class HttpClient:
    """Sends JSON requests to one service."""

    what: str
    """The service name used in messages, for example ``"HashiCorp Vault"``."""
    ssl_context: ssl.SSLContext | None = None
    timeout_s: float = 30.0
    redactor: Redactor | None = None

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Any = None,
        form_body: Mapping[str, str] | None = None,
        query: Mapping[str, str] | None = None,
    ) -> Any:
        """Send a request and return the decoded JSON body (``None`` for an empty body)."""
        if query:
            url = f"{url}?{urlencode(query)}"
        data: bytes | None = None
        all_headers = {"Accept": "application/json", "User-Agent": "dvf-secrets"}
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            all_headers["Content-Type"] = "application/json"
        elif form_body is not None:
            data = urlencode(form_body).encode("ascii")
            all_headers["Content-Type"] = "application/x-www-form-urlencoded"
        all_headers.update(headers or {})
        if urlsplit(url).scheme not in ("https", "http"):
            raise SecretError(f"{self.what}: refusing a request to a non-HTTP URL.")
        # The scheme is checked just above, so file: and custom schemes cannot be opened.
        request = urllib.request.Request(  # noqa: S310
            url, data=data, headers=all_headers, method=method
        )
        handlers: list[urllib.request.BaseHandler] = [_NoRedirect()]
        if self.ssl_context is not None:
            handlers.append(urllib.request.HTTPSHandler(context=self.ssl_context))
        opener = urllib.request.build_opener(*handlers)
        host = urlsplit(url).netloc
        try:
            with opener.open(request, timeout=self.timeout_s) as response:
                body: bytes = response.read()
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
            except OSError:
                raw = b""
            finally:
                exc.close()
            status = exc.code
            if 300 <= status < 400:
                raise HttpStatusError(
                    f"{self.what} at {host} answered with a redirect ({status}), which dvf does "
                    "not follow because it would forward credentials. Point the address at the "
                    "server that answers directly (for Vault, the active node).",
                    status,
                    "",
                    None,
                ) from None
            detail, code = self._error_detail(raw)
            message = f"{self.what} returned HTTP {status}"
            raise HttpStatusError(
                f"{message}: {detail}" if detail else f"{message}.", status, detail, code
            ) from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason: Any = getattr(exc, "reason", exc)
            raise SecretError(
                f"Cannot reach {self.what} at {host}: {safe_detail(reason, self.redactor)}"
            ) from None
        if not body.strip():
            return None
        try:
            return json.loads(body)
        except ValueError:
            raise SecretError(
                f"{self.what} at {host} returned a response that is not JSON."
            ) from None

    def _error_detail(self, raw: bytes) -> tuple[str, str | None]:
        try:
            payload = json.loads(raw) if raw.strip() else None
        except ValueError:
            payload = None
        if isinstance(payload, Mapping):
            code = payload.get("error_code")
            errors = payload.get("errors")
            if isinstance(errors, list) and errors:
                text = "; ".join(str(e) for e in errors)
            else:
                text = str(payload.get("message") or payload.get("error_description") or "")
                text = text or str(payload.get("error") or "")
            return safe_detail(text, self.redactor) if text else "", (str(code) if code else None)
        return "", None
