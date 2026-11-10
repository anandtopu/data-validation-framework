"""Databricks secret scopes: ``${secret:databricks:<scope>#<key>}``.

Inside a Databricks notebook or job (``DATABRICKS_RUNTIME_VERSION`` is set), values come from
``dbutils.secrets.get``, so they follow the cluster's identity and are redacted by Databricks too.
Elsewhere the provider calls the Secrets REST API (``GET /api/2.0/secrets/get``) with the host and
credentials from the environment: a personal access token (``DATABRICKS_TOKEN``), OAuth
machine-to-machine credentials of a service principal (``DATABRICKS_CLIENT_ID`` and
``DATABRICKS_CLIENT_SECRET``), or a profile of ``~/.databrickscfg``.

``databricks:<scope>/<key>#<json-key>`` selects one field of a JSON value.
"""

from __future__ import annotations

import base64
import binascii
import configparser
import logging
import re
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import Field, SecretStr

from dvf.core.errors import SecretError
from dvf.packs.secrets._base import PackSecretProvider, ProviderSettings
from dvf.packs.secrets._http import HttpClient, HttpStatusError, check_endpoint
from dvf.security.secrets import SecretRef, select_key

_log = logging.getLogger("dvf.secrets.databricks")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.@\-]{1,128}$")


class DatabricksSecretsSettings(ProviderSettings):
    """Settings of the ``databricks`` secret provider (environment variables follow the
    Databricks unified authentication)."""

    host: str | None = None
    """Workspace URL, such as ``https://adb-123.4.azuredatabricks.net`` (``DATABRICKS_HOST``)."""
    token: SecretStr | None = None
    """Personal access token (``DATABRICKS_TOKEN``)."""
    client_id: str | None = None
    """Service principal application ID for OAuth M2M (``DATABRICKS_CLIENT_ID``)."""
    client_secret: SecretStr | None = None
    """Service principal OAuth secret (``DATABRICKS_CLIENT_SECRET``)."""
    profile: str | None = None
    """Profile of the Databricks configuration file (``DATABRICKS_CONFIG_PROFILE``)."""
    config_file: str | None = None
    """Configuration file (``DATABRICKS_CONFIG_FILE``; default ``~/.databrickscfg``)."""
    use_dbutils: Literal["auto", "always", "never"] = "auto"
    """``auto`` uses ``dbutils.secrets`` when running inside Databricks
    (``DVF_DATABRICKS_USE_DBUTILS``)."""
    allow_insecure_http: bool = False
    """Allow an ``http://`` host other than localhost (test servers only)."""
    timeout_s: float = Field(default=30.0, gt=0)
    """Request timeout in seconds (``DVF_DATABRICKS_TIMEOUT``)."""


class DatabricksSecretProvider(PackSecretProvider):
    """Databricks secret scopes: dbutils.secrets inside Databricks, else the REST API."""

    name = "databricks"
    label = "Databricks secrets"
    extra = None
    Settings: ClassVar[type[ProviderSettings]] = DatabricksSecretsSettings
    ENV: ClassVar[dict[str, tuple[str, ...]]] = {
        "host": ("DATABRICKS_HOST",),
        "token": ("DATABRICKS_TOKEN",),
        "client_id": ("DATABRICKS_CLIENT_ID",),
        "client_secret": ("DATABRICKS_CLIENT_SECRET",),
        "profile": ("DATABRICKS_CONFIG_PROFILE",),
        "config_file": ("DATABRICKS_CONFIG_FILE",),
        "use_dbutils": ("DVF_DATABRICKS_USE_DBUTILS",),
        "timeout_s": ("DVF_DATABRICKS_TIMEOUT",),
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._dbutils: Any = None
        self._auth: tuple[HttpClient, str, str] | None = None
        for credential in (self.opts.token, self.opts.client_secret):
            if credential is not None:
                self.protect(credential.get_secret_value())

    @property
    def opts(self) -> DatabricksSecretsSettings:
        settings: DatabricksSecretsSettings = self.settings
        return settings

    def resolve(self, ref: SecretRef) -> str:
        scope, key, json_key = _parse(ref)
        what = f"Databricks secret '{scope}/{key}'"
        value = self.cached(f"{scope}/{key}", lambda: self._fetch(scope, key))
        return select_key(value, SecretRef(ref.provider, ref.path, json_key), what)

    # ------------------------------------------------------------------ dispatch
    def _fetch(self, scope: str, key: str) -> str:
        mode = self.opts.use_dbutils
        in_databricks = self.context.env("DATABRICKS_RUNTIME_VERSION") is not None
        if mode == "always" or (mode == "auto" and in_databricks):
            dbutils = self._get_dbutils()
            if dbutils is not None:
                return self._from_dbutils(dbutils, scope, key)
            if mode == "always":
                raise SecretError(
                    "dbutils is not available: use_dbutils is 'always' but this process is not a "
                    "Databricks notebook or job."
                )
        return self._from_rest(scope, key)

    def _get_dbutils(self) -> Any:
        if self._dbutils is not None:
            return self._dbutils
        try:
            import IPython  # type: ignore[import-not-found,unused-ignore]

            shell = IPython.get_ipython()
            candidate = shell.user_ns.get("dbutils") if shell is not None else None
            if candidate is not None:
                self._dbutils = candidate
                return candidate
        except Exception as exc:
            _log.debug("No dbutils in the IPython namespace: %s", type(exc).__name__)
        try:
            from pyspark.dbutils import DBUtils  # type: ignore[import-not-found,unused-ignore]
            from pyspark.sql import SparkSession

            spark = SparkSession.getActiveSession()
            if spark is not None:
                self._dbutils = DBUtils(spark)
                return self._dbutils
        except Exception as exc:
            _log.debug("pyspark.dbutils is not available: %s", type(exc).__name__)
        return None

    def _from_dbutils(self, dbutils: Any, scope: str, key: str) -> str:
        try:
            value = dbutils.secrets.get(scope=scope, key=key)
        except Exception as exc:
            raise SecretError(
                f"dbutils.secrets.get failed for scope '{scope}', key '{key}': {self.detail(exc)}"
            ) from None
        if value is None:
            raise SecretError(f"Databricks secret '{scope}/{key}' has no value.")
        text = str(value)
        self.protect(text)
        return text

    # ------------------------------------------------------------------ REST
    def _from_rest(self, scope: str, key: str) -> str:
        http, host, token = self._authenticate()
        try:
            payload = http.request(
                "GET",
                f"{host}/api/2.0/secrets/get",
                headers={"Authorization": f"Bearer {token}"},
                query={"scope": scope, "key": key},
            )
        except HttpStatusError as exc:
            raise self._status_error(exc, scope, key) from None
        encoded = payload.get("value") if isinstance(payload, dict) else None
        if not isinstance(encoded, str):
            raise SecretError(f"Databricks returned no value for secret '{scope}/{key}'.")
        try:
            text = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            raise SecretError(
                f"Databricks secret '{scope}/{key}' is not UTF-8 text (it may be binary)."
            ) from None
        self.protect(text)
        return text

    def _authenticate(self) -> tuple[HttpClient, str, str]:
        if self._auth is not None:
            return self._auth
        host, token, client_id, client_secret = self._credentials()
        if not host:
            raise SecretError(
                "The databricks secret provider needs the workspace URL outside Databricks: set "
                "DATABRICKS_HOST (and DATABRICKS_TOKEN, or DATABRICKS_CLIENT_ID and "
                "DATABRICKS_CLIENT_SECRET), or a profile in ~/.databrickscfg."
            )
        if "://" not in host:
            host = f"https://{host}"
        host = check_endpoint(host, what="Databricks", allow_http=self.opts.allow_insecure_http)
        http = HttpClient(
            what="Databricks", timeout_s=self.opts.timeout_s, redactor=self.context.redactor
        )
        if token:
            pass
        elif client_id and client_secret:
            token = self._oauth_token(http, host, client_id, client_secret)
        else:
            raise SecretError(
                "No Databricks credentials: set DATABRICKS_TOKEN, or DATABRICKS_CLIENT_ID and "
                "DATABRICKS_CLIENT_SECRET for a service principal."
            )
        self.protect(token)
        self._auth = (http, host, token)
        return self._auth

    def _credentials(self) -> tuple[str | None, str | None, str | None, str | None]:
        opts = self.opts
        host = opts.host
        token = opts.token.get_secret_value() if opts.token else None
        client_id = opts.client_id
        client_secret = opts.client_secret.get_secret_value() if opts.client_secret else None
        if host and (token or (client_id and client_secret)):
            return host, token, client_id, client_secret
        profile = self._profile()
        if profile:
            host = host or profile.get("host")
            token = token or profile.get("token")
            client_id = client_id or profile.get("client_id")
            client_secret = client_secret or profile.get("client_secret")
            for value in (token, client_secret):
                self.protect(value)
        return host, token, client_id, client_secret

    def _profile(self) -> dict[str, str] | None:
        configured = self.opts.config_file
        file = Path(configured).expanduser() if configured else Path.home() / ".databrickscfg"
        if not file.is_file():
            if configured or self.opts.profile:
                raise SecretError(f"The Databricks configuration file '{file}' does not exist.")
            return None
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(file, encoding="utf-8")
        except (configparser.Error, OSError) as exc:
            raise SecretError(
                f"Cannot read the Databricks configuration file '{file}': {type(exc).__name__}"
            ) from None
        name = self.opts.profile or "DEFAULT"
        if name != "DEFAULT" and not parser.has_section(name):
            known = ", ".join(parser.sections()) or "none"
            raise SecretError(
                f"Profile '{name}' is not in the Databricks configuration file (profiles: {known})."
            )
        section = parser[name]
        return {k: v for k, v in section.items() if v}

    def _oauth_token(self, http: HttpClient, host: str, client_id: str, secret: str) -> str:
        basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode("ascii")
        self.protect(basic)
        try:
            payload = http.request(
                "POST",
                f"{host}/oidc/v1/token",
                headers={"Authorization": f"Basic {basic}"},
                form_body={"grant_type": "client_credentials", "scope": "all-apis"},
            )
        except HttpStatusError as exc:
            raise SecretError(
                f"Databricks OAuth token request failed (HTTP {exc.status}): "
                f"{exc.detail or 'check the service principal client ID and secret'}."
            ) from None
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise SecretError("Databricks OAuth returned no access token.")
        return token

    def _status_error(self, exc: HttpStatusError, scope: str, key: str) -> SecretError:
        where = f"secret '{key}' in scope '{scope}'"
        if exc.status == 404 or exc.error_code == "RESOURCE_DOES_NOT_EXIST":
            return SecretError(f"Databricks {where} was not found.")
        if exc.status in (401, 403) or exc.error_code == "PERMISSION_DENIED":
            return SecretError(
                f"Databricks denied access to {where}: the identity needs READ permission on the "
                "scope (or the token is invalid or expired)."
            )
        return SecretError(f"Databricks request for {where} failed: {exc}")


def _parse(ref: SecretRef) -> tuple[str, str, str | None]:
    parts = [p for p in ref.path.split("/") if p]
    if len(parts) == 2:
        scope, key, json_key = parts[0], parts[1], ref.key
    elif len(parts) == 1 and ref.key:
        scope, key, json_key = parts[0], ref.key, None
    else:
        raise SecretError(
            f"Invalid databricks reference '{ref}': use <scope>#<key> or <scope>/<key>[#<json-key>]."
        )
    for label, value in (("scope", scope), ("key", key)):
        if not _NAME_RE.match(value):
            raise SecretError(
                f"'{value}' is not a valid Databricks secret {label} (letters, digits, '-', '_', "
                "'.', '@'; at most 128 characters)."
            )
    return scope, key, json_key
