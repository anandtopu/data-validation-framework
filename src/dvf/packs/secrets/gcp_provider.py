"""Google Cloud Secret Manager: ``${secret:gcpsm:<secret>[#<json-key>]}``.

Forms:

* ``gcpsm:<secret>``: the latest version in the default project
* ``gcpsm:<project>/<secret>`` and ``gcpsm:<project>/<secret>/<version>``
* ``gcpsm:projects/<project>/secrets/<secret>[/versions/<version>]``, and the regional form
  ``projects/<project>/locations/<location>/secrets/<secret>[/versions/<version>]``

Authentication uses Application Default Credentials (``gcloud auth application-default login``,
a service account key in ``GOOGLE_APPLICATION_CREDENTIALS``, workload identity, or the metadata
server).
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from dvf.core.errors import SecretError
from dvf.packs.secrets._base import PackSecretProvider, ProviderSettings, exception_names
from dvf.security.secrets import SecretRef, select_key

_PROJECT_RE = re.compile(r"^[a-z][a-z0-9.:\-]{4,62}[a-z0-9]$|^\d{1,20}$")
_SECRET_RE = re.compile(r"^[A-Za-z0-9_\-]{1,255}$")
_VERSION_RE = re.compile(r"^(latest|\d{1,20}|[A-Za-z][A-Za-z0-9_\-]{0,62})$")
_LOCATION_RE = re.compile(r"^[a-z]+(-[a-z0-9]+)+$")
_FULL_RE = re.compile(
    r"^projects/(?P<project>[^/]+)(?:/locations/(?P<location>[^/]+))?/secrets/(?P<secret>[^/]+)"
    r"(?:/versions/(?P<version>[^/]+))?$"
)


class GoogleSecretManagerSettings(ProviderSettings):
    """Settings of the ``gcpsm`` secret provider."""

    project: str | None = None
    """Default project ID (``DVF_GCPSM_PROJECT``, ``GOOGLE_CLOUD_PROJECT``, ``GCLOUD_PROJECT``,
    ``CLOUDSDK_CORE_PROJECT``)."""
    location: str | None = None
    """Location of regional secrets, such as ``europe-west1`` (``DVF_GCPSM_LOCATION``)."""
    version: str = "latest"
    """Version read when a reference names none."""


class GoogleSecretManagerProvider(PackSecretProvider):
    """Google Cloud Secret Manager, through google-cloud-secret-manager."""

    name = "gcpsm"
    label = "Google Cloud Secret Manager"
    extra = "gcp"
    Settings: ClassVar[type[ProviderSettings]] = GoogleSecretManagerSettings
    ENV: ClassVar[dict[str, tuple[str, ...]]] = {
        "project": (
            "DVF_GCPSM_PROJECT",
            "GOOGLE_CLOUD_PROJECT",
            "GCLOUD_PROJECT",
            "CLOUDSDK_CORE_PROJECT",
        ),
        "location": ("DVF_GCPSM_LOCATION",),
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._clients: dict[str | None, Any] = {}

    @property
    def opts(self) -> GoogleSecretManagerSettings:
        settings: GoogleSecretManagerSettings = self.settings
        return settings

    def resolve(self, ref: SecretRef) -> str:
        name, location = self.version_name(ref.path)
        value = self.cached(name, lambda: self._fetch(name, location))
        return select_key(value, ref, f"Google secret '{name}'")

    def version_name(self, path: str) -> tuple[str, str | None]:
        """The full resource name of the secret version a reference path means, and its location."""
        match = _FULL_RE.match(path)
        if match:
            project, location = match["project"], match["location"]
            secret, version = match["secret"], match["version"] or self.opts.version
        else:
            parts = path.split("/")
            location = self.opts.location
            if len(parts) == 1:
                project, secret, version = self.opts.project or "", parts[0], self.opts.version
            elif len(parts) == 2:
                project, secret, version = parts[0], parts[1], self.opts.version
            elif len(parts) == 3:
                project, secret, version = parts
            else:
                raise SecretError(
                    f"Invalid gcpsm reference '{path}': use <secret>, <project>/<secret>, "
                    "<project>/<secret>/<version>, or projects/<project>/secrets/<secret>."
                )
            if not project:
                raise SecretError(
                    f"No Google Cloud project for secret '{secret}'. Write <project>/<secret>, or "
                    "set GOOGLE_CLOUD_PROJECT or DVF_GCPSM_PROJECT."
                )
        if not _PROJECT_RE.match(project):
            raise SecretError(f"'{project}' is not a valid Google Cloud project ID or number.")
        if not _SECRET_RE.match(secret):
            raise SecretError(f"'{secret}' is not a valid Secret Manager secret ID.")
        if not _VERSION_RE.match(version):
            raise SecretError(f"'{version}' is not a valid Secret Manager version.")
        if location is not None and not _LOCATION_RE.match(location):
            raise SecretError(f"'{location}' is not a valid Google Cloud location.")
        scope = f"projects/{project}" + (f"/locations/{location}" if location else "")
        return f"{scope}/secrets/{secret}/versions/{version}", location

    # ------------------------------------------------------------------ internals
    def _client(self, location: str | None) -> Any:
        with self._lock:
            client = self._clients.get(location)
            if client is None:
                module = self.import_module(
                    "google.cloud.secretmanager", "google-cloud-secret-manager"
                )
                kwargs: dict[str, Any] = {}
                if location:
                    kwargs["client_options"] = {
                        "api_endpoint": f"secretmanager.{location}.rep.googleapis.com"
                    }
                try:
                    client = module.SecretManagerServiceClient(**kwargs)
                except Exception as exc:
                    raise self._error(exc, "(client setup)") from None
                self._clients[location] = client
            return client

    def _fetch(self, name: str, location: str | None) -> str:
        client = self._client(location)
        try:
            response = client.access_secret_version(request={"name": name})
        except Exception as exc:
            raise self._error(exc, name) from None
        data = response.payload.data
        try:
            text = bytes(data).decode("utf-8")
        except UnicodeDecodeError:
            raise SecretError(f"Google secret '{name}' is binary and not UTF-8 text.") from None
        self.protect(text)
        return text

    def _error(self, exc: Exception, name: str) -> SecretError:
        names = exception_names(exc)
        if "DefaultCredentialsError" in names:
            return SecretError(
                "No Google Cloud credentials were found for the gcpsm secret provider. Run "
                "'gcloud auth application-default login' or set GOOGLE_APPLICATION_CREDENTIALS."
            )
        if "NotFound" in names:
            return SecretError(f"Google secret '{name}' was not found.")
        if "PermissionDenied" in names:
            return SecretError(
                f"Access to Google secret '{name}' was denied. Grant the identity "
                "roles/secretmanager.secretAccessor on the secret."
            )
        if "FailedPrecondition" in names:
            return SecretError(
                f"Google secret '{name}' cannot be read (the version is disabled or destroyed)."
            )
        return SecretError(
            f"Google Secret Manager request for '{name}' failed: {type(exc).__name__}: "
            f"{self.detail(exc)}"
        )
