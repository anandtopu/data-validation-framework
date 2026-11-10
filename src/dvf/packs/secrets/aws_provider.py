"""AWS Secrets Manager: ``${secret:awssm:<name-or-arn>[#<json-key>]}``.

Credentials come from the standard boto3 chain (environment, shared config and SSO profiles,
container and instance roles). The region comes from the ARN, the ``region`` setting, or the AWS
environment. ``#<json-key>`` selects one field of a JSON secret, such as the ``password`` of an RDS
secret.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from dvf.core.errors import SecretError
from dvf.packs.secrets._base import PackSecretProvider, ProviderSettings, exception_names
from dvf.security.secrets import SecretRef, select_key

_ARN_RE = re.compile(r"^arn:aws[a-z-]*:secretsmanager:(?P<region>[a-z0-9-]+):\d{12}:secret:.+$")
_NAME_RE = re.compile(r"^[A-Za-z0-9/_+=.@\-]{1,512}$")


class AWSSecretsManagerSettings(ProviderSettings):
    """Settings of the ``awssm`` secret provider."""

    region: str | None = None
    """AWS region (``DVF_AWSSM_REGION``, ``AWS_REGION``, ``AWS_DEFAULT_REGION``). An ARN's region
    wins."""
    profile: str | None = None
    """Named profile from the AWS config files (``DVF_AWSSM_PROFILE``; boto3 also reads
    ``AWS_PROFILE``)."""
    endpoint_url: str | None = None
    """Custom endpoint, for VPC endpoints or test servers (``DVF_AWSSM_ENDPOINT_URL``)."""
    version_stage: str | None = None
    """Version stage to read (default ``AWSCURRENT``)."""
    version_id: str | None = None
    """A specific version ID to read."""


class AWSSecretsManagerProvider(PackSecretProvider):
    """AWS Secrets Manager, through boto3."""

    name = "awssm"
    label = "AWS Secrets Manager"
    extra = "aws"
    Settings: ClassVar[type[ProviderSettings]] = AWSSecretsManagerSettings
    ENV: ClassVar[dict[str, tuple[str, ...]]] = {
        "region": ("DVF_AWSSM_REGION", "AWS_REGION", "AWS_DEFAULT_REGION"),
        "profile": ("DVF_AWSSM_PROFILE",),
        "endpoint_url": ("DVF_AWSSM_ENDPOINT_URL",),
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._clients: dict[str | None, Any] = {}

    @property
    def opts(self) -> AWSSecretsManagerSettings:
        settings: AWSSecretsManagerSettings = self.settings
        return settings

    def resolve(self, ref: SecretRef) -> str:
        secret_id = ref.path
        arn = _ARN_RE.match(secret_id)
        if arn is None and not _NAME_RE.match(secret_id):
            raise SecretError(
                f"'{secret_id}' is not a valid AWS Secrets Manager secret name or ARN."
            )
        region = arn["region"] if arn else self.opts.region
        what = f"AWS secret '{secret_id}'"
        document = self.cached(f"{region}|{secret_id}", lambda: self._fetch(secret_id, region))
        return select_key(document, ref, what)

    # ------------------------------------------------------------------ internals
    def _client(self, region: str | None) -> Any:
        with self._lock:
            client = self._clients.get(region)
            if client is None:
                client = self._new_client(region)
                self._clients[region] = client
            return client

    def _new_client(self, region: str | None) -> Any:
        boto3 = self.import_module("boto3", "boto3")
        try:
            session = boto3.session.Session(profile_name=self.opts.profile, region_name=region)
            kwargs: dict[str, Any] = {}
            if self.opts.endpoint_url:
                kwargs["endpoint_url"] = self.opts.endpoint_url
            return session.client("secretsmanager", **kwargs)
        except Exception as exc:
            raise self._error(exc, "(client setup)", region) from None

    def _fetch(self, secret_id: str, region: str | None) -> str:
        request: dict[str, Any] = {"SecretId": secret_id}
        if self.opts.version_id:
            request["VersionId"] = self.opts.version_id
        if self.opts.version_stage:
            request["VersionStage"] = self.opts.version_stage
        client = self._client(region)
        try:
            response = client.get_secret_value(**request)
        except Exception as exc:
            raise self._error(exc, secret_id, region) from None
        text = response.get("SecretString")
        if text is None:
            binary = response.get("SecretBinary")
            if binary is None:
                raise SecretError(f"AWS secret '{secret_id}' has no value.")
            try:
                text = bytes(binary).decode("utf-8")
            except UnicodeDecodeError:
                raise SecretError(
                    f"AWS secret '{secret_id}' is binary and not UTF-8 text."
                ) from None
        self.protect(text)
        return str(text)

    def _error(self, exc: Exception, secret_id: str, region: str | None) -> SecretError:
        names = exception_names(exc)
        where = f" in region {region}" if region else ""
        if "NoRegionError" in names:
            return SecretError(
                "No AWS region is configured for the awssm secret provider. Set AWS_REGION, "
                "DVF_AWSSM_REGION, or use the secret's ARN."
            )
        if "NoCredentialsError" in names or "PartialCredentialsError" in names:
            return SecretError(
                "No AWS credentials were found for the awssm secret provider. Configure a profile "
                "(aws configure / aws sso login), environment variables, or an instance role."
            )
        if "ProfileNotFound" in names:
            return SecretError(f"AWS profile '{self.opts.profile}' does not exist.")
        if "ClientError" in names:
            error = getattr(exc, "response", {}).get("Error", {})
            code = str(error.get("Code", ""))
            if code == "ResourceNotFoundException":
                return SecretError(f"AWS secret '{secret_id}' was not found{where}.")
            if code in ("AccessDeniedException", "AccessDenied"):
                return SecretError(
                    f"Access to AWS secret '{secret_id}' was denied{where}. The identity needs "
                    "secretsmanager:GetSecretValue on it (and kms:Decrypt on its key)."
                )
            if code == "DecryptionFailure":
                return SecretError(
                    f"AWS could not decrypt secret '{secret_id}'. The identity needs kms:Decrypt "
                    "on the secret's KMS key."
                )
            message = self.detail(str(error.get("Message", "")))
            return SecretError(
                f"AWS Secrets Manager error for '{secret_id}'{where}: {code}: {message}"
            )
        return SecretError(
            f"AWS Secrets Manager request for '{secret_id}'{where} failed: "
            f"{type(exc).__name__}: {self.detail(exc)}"
        )
