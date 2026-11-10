"""HashiCorp Vault KV secrets engine (v1 and v2): ``${secret:vault:<mount>/<path>#<key>}``.

``vault:secret/dvf/postgres#password`` reads field ``password`` of the secret at ``dvf/postgres`` in
the ``secret/`` mount, the same path ``vault kv get secret/dvf/postgres`` uses. The KV version and
the mount are detected through ``sys/internal/ui/mounts`` (as the Vault CLI does), or set with the
``kv_version`` and ``mount`` settings.

Talks to the Vault HTTP API with the standard library. Authentication: a token (``VAULT_TOKEN``, or
a token file such as ``~/.vault-token``) or AppRole (role ID and secret ID). Namespaces (Vault
Enterprise and HCP Vault) and TLS settings follow the Vault CLI's environment variables.
"""

from __future__ import annotations

import logging
import ssl
from pathlib import Path
from typing import Any, ClassVar, Literal
from urllib.parse import quote

from pydantic import Field, SecretStr, field_validator

from dvf.core.errors import SecretError
from dvf.packs.secrets._base import PackSecretProvider, ProviderSettings
from dvf.packs.secrets._http import HttpClient, HttpStatusError, check_endpoint
from dvf.security.secrets import SecretRef, select_key

_log = logging.getLogger("dvf.secrets.vault")


class VaultSettings(ProviderSettings):
    """Settings of the ``vault`` secret provider. Environment variables follow the Vault CLI."""

    addr: str | None = None
    """Vault address, such as ``https://vault.example.org:8200`` (``VAULT_ADDR``)."""
    namespace: str | None = None
    """Vault Enterprise or HCP namespace (``VAULT_NAMESPACE``)."""
    auth_method: Literal["auto", "token", "approle"] = "auto"
    """``token``, ``approle``, or ``auto``: a token if one is set, else AppRole if a role ID is
    set, else the token file (``DVF_VAULT_AUTH_METHOD``)."""
    token: SecretStr | None = None
    """Vault token (``VAULT_TOKEN``)."""
    token_file: str | None = None
    """File holding a token (``DVF_VAULT_TOKEN_FILE``; default ``~/.vault-token``)."""
    role_id: str | None = None
    """AppRole role ID (``VAULT_ROLE_ID``)."""
    secret_id: SecretStr | None = None
    """AppRole secret ID (``VAULT_SECRET_ID``)."""
    secret_id_file: str | None = None
    """File holding the AppRole secret ID (``DVF_VAULT_SECRET_ID_FILE``)."""
    approle_mount: str = "approle"
    """Mount path of the AppRole auth method (``DVF_VAULT_APPROLE_MOUNT``)."""
    kv_version: Literal[1, 2] | None = None
    """KV engine version; detected when unset (``DVF_VAULT_KV_VERSION``: ``1``, ``2``, or
    ``auto``)."""
    mount: str | None = None
    """Mount path of the KV engine, when detection is not allowed by policy (``DVF_VAULT_MOUNT``)."""
    cacert: str | None = None
    """CA bundle file to verify the server (``VAULT_CACERT``)."""
    capath: str | None = None
    """Folder of CA certificates (``VAULT_CAPATH``)."""
    client_cert: str | None = None
    """Client certificate for mutual TLS (``VAULT_CLIENT_CERT``)."""
    client_key: str | None = None
    """Client key for mutual TLS (``VAULT_CLIENT_KEY``)."""
    skip_verify: bool = False
    """Disable TLS certificate verification (``VAULT_SKIP_VERIFY``). Never use in production."""
    allow_insecure_http: bool = False
    """Allow ``http://`` addresses other than localhost (``DVF_VAULT_ALLOW_INSECURE_HTTP``)."""
    timeout_s: float = Field(default=30.0, gt=0)
    """Request timeout in seconds (``DVF_VAULT_TIMEOUT``)."""

    @field_validator("kv_version", mode="before")
    @classmethod
    def _auto_version(cls, value: Any) -> Any:
        if isinstance(value, str):
            text = value.strip().lower()
            if text in ("", "auto"):
                return None
            if text in ("1", "2"):
                return int(text)
        return value


class VaultSecretProvider(PackSecretProvider):
    """HashiCorp Vault KV v1 and v2 over the HTTP API (token or AppRole)."""

    name = "vault"
    label = "HashiCorp Vault"
    extra = None
    Settings: ClassVar[type[ProviderSettings]] = VaultSettings
    ENV: ClassVar[dict[str, tuple[str, ...]]] = {
        "addr": ("VAULT_ADDR",),
        "namespace": ("VAULT_NAMESPACE",),
        "auth_method": ("DVF_VAULT_AUTH_METHOD",),
        "token": ("VAULT_TOKEN",),
        "token_file": ("DVF_VAULT_TOKEN_FILE",),
        "role_id": ("VAULT_ROLE_ID",),
        "secret_id": ("VAULT_SECRET_ID",),
        "secret_id_file": ("DVF_VAULT_SECRET_ID_FILE",),
        "approle_mount": ("DVF_VAULT_APPROLE_MOUNT",),
        "kv_version": ("DVF_VAULT_KV_VERSION",),
        "mount": ("DVF_VAULT_MOUNT",),
        "cacert": ("VAULT_CACERT",),
        "capath": ("VAULT_CAPATH",),
        "client_cert": ("VAULT_CLIENT_CERT",),
        "client_key": ("VAULT_CLIENT_KEY",),
        "skip_verify": ("VAULT_SKIP_VERIFY",),
        "allow_insecure_http": ("DVF_VAULT_ALLOW_INSECURE_HTTP",),
        "timeout_s": ("DVF_VAULT_TIMEOUT",),
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._token: str | None = None
        self._http: HttpClient | None = None
        self._addr: str | None = None
        for credential in (self.opts.token, self.opts.secret_id):
            if credential is not None:
                self.protect(credential.get_secret_value())

    @property
    def opts(self) -> VaultSettings:
        settings: VaultSettings = self.settings
        return settings

    def resolve(self, ref: SecretRef) -> str:
        segments = _segments(ref.path)
        mount, rest, version = self._locate(segments)
        location = f"{mount}/{'/'.join(rest)}"
        data = self.cached(
            f"{mount}|{'/'.join(rest)}|{version}", lambda: self._read(mount, rest, version)
        )
        what = f"Vault secret '{location}'"
        if ref.key is None:
            if len(data) == 1:
                return select_key(data, SecretRef(ref.provider, ref.path, next(iter(data))), what)
            known = ", ".join(sorted(data)) or "none"
            raise SecretError(
                f"{what} has {len(data)} keys ({known}); choose one with "
                f"'${{secret:vault:{ref.path}#<key>}}'."
            )
        return select_key(data, ref, what)

    # ------------------------------------------------------------------ connection
    def _client(self) -> tuple[HttpClient, str]:
        if self._http is not None and self._addr is not None:
            return self._http, self._addr
        opts = self.opts
        if not opts.addr:
            raise SecretError(
                "The vault secret provider needs the Vault address: set VAULT_ADDR (for example "
                "https://vault.example.org:8200)."
            )
        addr = check_endpoint(
            opts.addr, what="HashiCorp Vault", allow_http=opts.allow_insecure_http
        )
        context: ssl.SSLContext | None = None
        if addr.lower().startswith("https://"):
            context = self._ssl_context()
        self._http = HttpClient(
            what="HashiCorp Vault",
            ssl_context=context,
            timeout_s=opts.timeout_s,
            redactor=self.context.redactor,
        )
        self._addr = addr
        return self._http, addr

    def _ssl_context(self) -> ssl.SSLContext:
        opts = self.opts
        for label, value in (
            ("VAULT_CACERT", opts.cacert),
            ("VAULT_CLIENT_CERT", opts.client_cert),
        ):
            if value and not self._path(value).is_file():
                raise SecretError(f"The {label} file '{value}' does not exist.")
        if opts.capath and not self._path(opts.capath).is_dir():
            raise SecretError(f"The VAULT_CAPATH folder '{opts.capath}' does not exist.")
        try:
            context = ssl.create_default_context(
                cafile=str(self._path(opts.cacert)) if opts.cacert else None,
                capath=str(self._path(opts.capath)) if opts.capath else None,
            )
            if opts.client_cert:
                context.load_cert_chain(
                    str(self._path(opts.client_cert)),
                    str(self._path(opts.client_key)) if opts.client_key else None,
                )
        except (ssl.SSLError, OSError) as exc:
            raise SecretError(f"Cannot load the Vault TLS settings: {self.detail(exc)}") from None
        if opts.skip_verify:
            _log.warning(
                "TLS certificate verification is disabled for HashiCorp Vault (VAULT_SKIP_VERIFY). "
                "Do not use this outside test environments."
            )
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    def _path(self, value: str) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else self.context.base_dir / path

    def _headers(self, token: str | None = None) -> dict[str, str]:
        headers = {"X-Vault-Request": "true"}
        if token:
            headers["X-Vault-Token"] = token
        if self.opts.namespace:
            headers["X-Vault-Namespace"] = self.opts.namespace.strip("/")
        return headers

    # ------------------------------------------------------------------ authentication
    def _get_token(self) -> str:
        if self._token is not None:
            return self._token
        opts = self.opts
        method = opts.auth_method
        if method == "auto":
            if opts.token is not None:
                method = "token"
            elif opts.role_id:
                method = "approle"
            else:
                method = "token"
        if method == "approle":
            token = self._approle_login()
        elif opts.token is not None:
            token = opts.token.get_secret_value().strip()
        else:
            token = self._token_from_file()
        if not token:
            raise SecretError("The Vault token is empty.")
        self.protect(token)
        self._token = token
        return token

    def _token_from_file(self) -> str:
        configured = self.opts.token_file
        file = self._path(configured) if configured else Path.home() / ".vault-token"
        if not file.is_file():
            raise SecretError(
                "No Vault credentials: set VAULT_TOKEN, a token file (DVF_VAULT_TOKEN_FILE or "
                "~/.vault-token from 'vault login'), or VAULT_ROLE_ID and VAULT_SECRET_ID for "
                "AppRole."
            )
        try:
            return file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SecretError(
                f"Cannot read the Vault token file '{file}': {self.detail(exc)}"
            ) from None

    def _approle_login(self) -> str:
        opts = self.opts
        if not opts.role_id:
            raise SecretError("AppRole authentication needs a role ID: set VAULT_ROLE_ID.")
        secret_id = opts.secret_id.get_secret_value() if opts.secret_id is not None else None
        if secret_id is None and opts.secret_id_file:
            file = self._path(opts.secret_id_file)
            try:
                secret_id = file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise SecretError(
                    f"Cannot read the AppRole secret ID file '{file}': {self.detail(exc)}"
                ) from None
            self.protect(secret_id)
        body: dict[str, str] = {"role_id": opts.role_id}
        if secret_id:
            body["secret_id"] = secret_id
        http, addr = self._client()
        mount = "/".join(quote(s, safe="") for s in _segments(opts.approle_mount))
        try:
            payload = http.request(
                "POST", f"{addr}/v1/auth/{mount}/login", headers=self._headers(), json_body=body
            )
        except HttpStatusError as exc:
            raise SecretError(
                f"Vault AppRole login failed (HTTP {exc.status}): "
                f"{exc.detail or 'check the role ID, the secret ID, and the AppRole mount'}."
            ) from None
        token = (
            (payload or {}).get("auth", {}).get("client_token")
            if isinstance(payload, dict)
            else None
        )
        if not isinstance(token, str) or not token:
            raise SecretError("Vault AppRole login returned no client token.")
        return token

    # ------------------------------------------------------------------ reading
    def _locate(self, segments: list[str]) -> tuple[str, list[str], int]:
        opts = self.opts
        if opts.mount:
            mount_segments = _segments(opts.mount)
            if segments[: len(mount_segments)] != mount_segments or len(segments) <= len(
                mount_segments
            ):
                raise SecretError(
                    f"The Vault path '{'/'.join(segments)}' is not inside the configured mount "
                    f"'{opts.mount}'."
                )
            return "/".join(mount_segments), segments[len(mount_segments) :], opts.kv_version or 2
        if opts.kv_version is not None:
            if len(segments) < 2:
                raise SecretError(f"The Vault path '{segments[0]}' names a mount but no secret.")
            return segments[0], segments[1:], opts.kv_version
        return self.cached(f"mount|{'/'.join(segments)}", lambda: self._detect(segments))

    def _detect(self, segments: list[str]) -> tuple[str, list[str], int]:
        http, addr = self._client()
        path = "/".join(quote(s, safe="") for s in segments)
        try:
            payload = http.request(
                "GET",
                f"{addr}/v1/sys/internal/ui/mounts/{path}",
                headers=self._headers(self._get_token()),
            )
            data = payload.get("data", {}) if isinstance(payload, dict) else {}
            mount = str(data.get("path") or "").strip("/")
            options = data.get("options") or {}
            version = 2 if str(options.get("version", "1")) == "2" else 1
            mount_segments = _segments(mount) if mount else []
            if mount_segments and segments[: len(mount_segments)] == mount_segments:
                rest = segments[len(mount_segments) :]
                if not rest:
                    raise SecretError(f"The Vault path '{mount}' names a mount but no secret.")
                return mount, rest, version
        except HttpStatusError as exc:
            if exc.status not in (400, 403, 404):
                raise self._status_error(exc, "/".join(segments)) from None
        # Detection not allowed by policy: assume the first segment is a KV v2 mount.
        if len(segments) < 2:
            raise SecretError(f"The Vault path '{segments[0]}' names a mount but no secret.")
        return segments[0], segments[1:], 2

    def _read(self, mount: str, rest: list[str], version: int) -> dict[str, Any]:
        http, addr = self._client()
        mount_path = "/".join(quote(s, safe="") for s in _segments(mount))
        secret_path = "/".join(quote(s, safe="") for s in rest)
        api = f"{mount_path}/data/{secret_path}" if version == 2 else f"{mount_path}/{secret_path}"
        location = f"{mount}/{'/'.join(rest)}"
        try:
            payload = http.request(
                "GET", f"{addr}/v1/{api}", headers=self._headers(self._get_token())
            )
        except HttpStatusError as exc:
            raise self._status_error(exc, location) from None
        data = payload.get("data") if isinstance(payload, dict) else None
        if version == 2 and isinstance(data, dict):
            metadata = data.get("metadata") or {}
            if data.get("data") is None and (
                metadata.get("deleted_time") or metadata.get("destroyed")
            ):
                raise SecretError(f"The latest version of Vault secret '{location}' is deleted.")
            data = data.get("data")
        if not isinstance(data, dict):
            raise SecretError(
                f"Vault returned no key-value data for '{location}' (KV v{version}). Check the path, "
                "or set kv_version and mount."
            )
        return data

    def _status_error(self, exc: HttpStatusError, location: str) -> SecretError:
        if exc.status == 404:
            return SecretError(f"Vault secret '{location}' was not found.")
        if exc.status == 403:
            return SecretError(
                f"Vault denied access to '{location}': the token needs the 'read' capability on "
                "it (or the token is invalid or expired)."
            )
        return SecretError(f"Vault request for '{location}' failed: {exc}")


def _segments(path: str) -> list[str]:
    parts = [p for p in path.strip().strip("/").split("/") if p]
    if not parts:
        raise SecretError("The Vault path is empty.")
    for part in parts:
        if part in (".", ".."):
            raise SecretError(f"The Vault path '{path}' must not contain '.' or '..' segments.")
    return parts
