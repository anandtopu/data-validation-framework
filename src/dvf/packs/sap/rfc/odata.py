"""The ``odata`` backend of ``sap_rfc``: SAP OData v2 and v4 services over HTTP.

It reads entity sets (for example ``A_BusinessPartner`` of ``API_BUSINESS_PARTNER``, or a CDS
view exposed as a service) with the Python standard library only:

* the service ``$metadata`` describes entity sets: properties, Edm types, and keys;
* rows are read in pages with ``$top`` and ``$skip``, ordered by the key (``$orderby``), or by
  following the server's next links (``__next`` in v2, ``@odata.nextLink`` in v4);
* ``options.where`` is passed as ``$filter`` and ``options.fields`` as ``$select``;
* ``sap-client`` is the connection's ``client``, and ``sap-language`` its ``lang``;
* authentication is HTTP basic (``user`` and ``password``) or headers from ``options.headers``
  (for example a bearer token from a secret reference).

Values are converted from their Edm types: v2 ``/Date(ms)/`` and ``PT12H30M00S``, v4 ISO dates and
times, decimals and 64-bit integers sent as strings, and base64 binaries.
"""

from __future__ import annotations

import base64
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from dvf.core.errors import ConnectorError
from dvf.core.types import CanonicalType
from dvf.normalize.canonical import UNKNOWN_DECIMAL_SCALE
from dvf.packs.sap.rfc.backends import (
    RemoteColumn,
    RemoteTable,
    SapRfcBackend,
    TableRead,
    read_columns,
    read_schema,
)

if TYPE_CHECKING:
    from dvf.config.connections import SapRfcConnection

_V2_DATE = re.compile(r"^/Date\((-?\d+)(?:([+-])(\d{4}))?\)/$")
_DURATION = re.compile(r"^-?P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$")
_DEFAULT_TIMEOUT = 300


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def edm_type(edm: str, attrs: Mapping[str, str], version: int) -> tuple[CanonicalType, pa.DataType]:
    """Canonical and Arrow types of an Edm property type."""
    kind = edm.removeprefix("Edm.")
    if kind == "String":
        length = attrs.get("MaxLength", "")
        return CanonicalType.string(int(length) if length.isdigit() else None), pa.string()
    if kind == "Boolean":
        return CanonicalType.boolean(), pa.bool_()
    if kind in ("Byte", "SByte", "Int16"):
        bits = 8 if kind != "Int16" else 16
        return CanonicalType.integer(bits, unsigned=kind == "Byte"), pa.int16()
    if kind == "Int32":
        return CanonicalType.integer(32), pa.int32()
    if kind == "Int64":
        return CanonicalType.integer(64), pa.int64()
    if kind == "Decimal":
        precision_text = attrs.get("Precision", "")
        scale_text = attrs.get("Scale", "")
        precision = min(int(precision_text), 38) if precision_text.isdigit() else 38
        scale = int(scale_text) if scale_text.isdigit() else UNKNOWN_DECIMAL_SCALE
        scale = min(scale, precision)
        return CanonicalType.decimal(precision, scale), pa.decimal128(precision, scale)
    if kind in ("Double", "Single"):
        return CanonicalType.float(64 if kind == "Double" else 32), pa.float64()
    if kind == "DateTime":  # v2: a wall-clock time without zone
        return CanonicalType.timestamp(3), pa.timestamp("ms")
    if kind == "DateTimeOffset":
        return CanonicalType.timestamp(6, tz=True), pa.timestamp("us", tz="UTC")
    if kind == "Date":
        return CanonicalType.date(), pa.date32()
    if kind in ("Time", "TimeOfDay"):
        return CanonicalType.time(6), pa.time64("us")
    if kind == "Binary":
        return CanonicalType.binary(), pa.binary()
    return CanonicalType.string(), pa.string()


def _v2_datetime(text: str, aware: bool) -> datetime:
    match = _V2_DATE.match(text)
    if match is None:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if aware:
            return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        return (
            parsed.replace(tzinfo=None)
            if parsed.tzinfo is None
            else parsed.astimezone(UTC).replace(tzinfo=None)
        )
    millis = int(match.group(1))
    value = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=millis)
    return value if aware else value.replace(tzinfo=None)


def _duration_time(text: str) -> time:
    match = _DURATION.match(text)
    if match is None:
        raise ValueError(text)
    hours = int(match.group(2) or 0)
    minutes = int(match.group(3) or 0)
    seconds = Decimal(match.group(4) or "0")
    whole = int(seconds)
    micros = int((seconds - whole) * 1_000_000)
    return time(hours % 24, minutes, whole, micros)


def convert_value(value: Any, edm: str) -> Any:
    """Convert a JSON value of Edm type ``edm`` to a Python value."""
    if value is None:
        return None
    kind = edm.removeprefix("Edm.")
    if kind == "Decimal":
        return Decimal(str(value))
    if kind in ("Int64", "Int32", "Int16", "Byte", "SByte"):
        return int(value)
    if kind in ("Double", "Single"):
        return float(value)
    if kind == "Boolean":
        return value if isinstance(value, bool) else str(value).lower() == "true"
    if kind == "DateTime":
        return _v2_datetime(str(value), aware=False)
    if kind == "DateTimeOffset":
        return _v2_datetime(str(value), aware=True)
    if kind == "Date":
        return date.fromisoformat(str(value)[:10])
    if kind == "Time":
        return _duration_time(str(value))
    if kind == "TimeOfDay":
        return time.fromisoformat(str(value))
    if kind == "Binary":
        return base64.b64decode(str(value))
    return value if isinstance(value, str) else str(value)


class ODataBackend(SapRfcBackend):
    """SAP OData v2 and v4 services (Gateway, S/4HANA APIs, CDS views) over HTTP."""

    name = "odata"
    summary = "OData v2 and v4 services and CDS views over HTTP (standard library only)"

    def __init__(self, config: SapRfcConnection) -> None:
        super().__init__(config)
        self._metadata: ET.Element | None = None
        self._version: int | None = config.odata_version
        self._types: dict[str, dict[str, str]] = {}

    @classmethod
    def validate_config(cls, config: SapRfcConnection) -> None:
        if not config.url:
            raise ValueError("backend 'odata' needs 'url' (the OData service root)")
        if not config.url.lower().startswith(("http://", "https://")):
            raise ValueError("'url' must start with http:// or https://")

    # ------------------------------------------------------------------ HTTP
    @property
    def root(self) -> str:
        assert self.config.url is not None
        return self.config.url.rstrip("/")

    def _headers(self, accept: str) -> dict[str, str]:
        headers = {"Accept": accept}
        cfg = self.config
        if cfg.user is not None and cfg.password is not None:
            token = f"{cfg.user}:{cfg.password.get_secret_value()}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(token).decode("ascii")
        extra = cfg.options.get("headers") or {}
        if isinstance(extra, Mapping):
            headers.update({str(k): str(v) for k, v in extra.items()})
        return headers

    def _context(self) -> ssl.SSLContext | None:
        if self.root.lower().startswith("https://") and not self.config.verify_tls:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            return context
        return None

    def _with_sap_params(self, url: str) -> str:
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        query.setdefault("sap-client", self.config.client)
        if self.config.lang:
            query.setdefault("sap-language", self.config.lang)
        encoded = urllib.parse.urlencode(query, quote_via=urllib.parse.quote, safe="$,()'")
        return urllib.parse.urlunsplit(parts._replace(query=encoded))

    def get(self, url: str, accept: str = "application/json") -> bytes:
        """GET ``url`` (absolute or relative to the service root) and return the body."""
        absolute = urllib.parse.urljoin(self.root + "/", url)
        if not absolute.lower().startswith(("http://", "https://")):
            raise ConnectorError(f"Refusing to fetch a non-HTTP OData URL: {absolute}")
        request = urllib.request.Request(  # noqa: S310 - only http(s), checked above
            self._with_sap_params(absolute), headers=self._headers(accept), method="GET"
        )
        timeout = self.config.query_timeout_s or _DEFAULT_TIMEOUT
        try:
            with urllib.request.urlopen(  # noqa: S310 - only http(s), checked above
                request, timeout=timeout, context=self._context()
            ) as reply:
                body: bytes = reply.read()
                return body
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read()[:300].decode("utf-8", errors="replace")
            finally:
                exc.close()
            raise ConnectorError(
                f"OData request failed with HTTP {exc.code}: {detail}",
                hint="Check the service URL, the entity set name, and the user's authorizations.",
            ) from exc
        except urllib.error.URLError as exc:
            raise ConnectorError(f"Could not reach the OData service: {exc.reason}") from exc

    # ------------------------------------------------------------------ metadata
    def metadata(self) -> ET.Element:
        if self._metadata is None:
            body = self.get("$metadata", accept="application/xml")
            try:
                # The configured service's metadata; ElementTree resolves no external entities.
                self._metadata = ET.fromstring(body)  # noqa: S314
            except ET.ParseError as exc:
                raise ConnectorError(f"The OData $metadata document is not XML: {exc}") from exc
            if self._version is None:
                self._version = 4 if self._metadata.get("Version", "").startswith("4") else 2
        return self._metadata

    @property
    def version(self) -> int:
        self.metadata()
        return self._version or 2

    def _entity_sets(self) -> dict[str, str]:
        sets: dict[str, str] = {}
        for element in self.metadata().iter():
            if _local(element.tag) == "EntitySet":
                sets[element.get("Name", "")] = element.get("EntityType", "")
        return sets

    def _entity_type(self, qualified: str) -> ET.Element | None:
        short = qualified.rsplit(".", 1)[-1]
        for element in self.metadata().iter():
            if _local(element.tag) == "EntityType" and element.get("Name") == short:
                return element
        return None

    def describe(self, table: str) -> RemoteTable | None:
        sets = self._entity_sets()
        name = (
            table if table in sets else next((s for s in sets if s.upper() == table.upper()), None)
        )
        if name is None:
            return None
        entity = self._entity_type(sets[name])
        if entity is None:
            raise ConnectorError(f"The OData metadata has no entity type {sets[name]}.")
        keys = {ref.get("Name", "") for ref in entity.iter() if _local(ref.tag) == "PropertyRef"}
        columns: list[RemoteColumn] = []
        types: dict[str, str] = {}
        for prop in entity:
            if _local(prop.tag) != "Property":
                continue
            prop_name = prop.get("Name", "")
            edm = prop.get("Type", "Edm.String")
            attrs = {k.rsplit("}", 1)[-1]: v for k, v in prop.attrib.items()}
            canonical, arrow = edm_type(edm, attrs, self.version)
            native = edm + (f"({attrs['MaxLength']})" if "MaxLength" in attrs else "")
            label = attrs.get("label")
            columns.append(
                RemoteColumn(
                    name=prop_name,
                    native_type=native,
                    canonical=canonical,
                    arrow=arrow,
                    key=prop_name in keys,
                    nullable=attrs.get("Nullable", "true").lower() != "false",
                    description=label,
                )
            )
            types[prop_name] = edm
        self._types[name] = types
        return RemoteTable(name=name, columns=tuple(columns))

    def list_tables(self, pattern: str) -> list[str]:
        import fnmatch

        return sorted(
            s for s in self._entity_sets() if fnmatch.fnmatchcase(s.upper(), pattern.upper())
        )

    # ------------------------------------------------------------------ reading
    def _page_url(self, table: RemoteTable, request: TableRead, skip: int, top: int) -> str:
        columns = read_columns(table, request)
        params: dict[str, str] = {}
        if request.fields is not None:
            params["$select"] = ",".join(c.name for c in columns)
        if request.where:
            params["$filter"] = request.where
        if table.primary_key:
            params["$orderby"] = ",".join(table.primary_key)
        params["$top"] = str(top)
        if skip:
            params["$skip"] = str(skip)
        if self.version == 2:
            params["$format"] = "json"
        query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote, safe="$,()'")
        return f"{urllib.parse.quote(table.name)}?{query}"

    def _page(self, url: str) -> tuple[list[dict[str, Any]], str | None]:
        try:
            payload = json.loads(self.get(url))
        except json.JSONDecodeError as exc:
            raise ConnectorError(f"The OData service did not return JSON: {exc}") from exc
        if "d" in payload:  # v2
            body = payload["d"]
            if isinstance(body, list):
                return body, None
            return list(body.get("results", [])), body.get("__next")
        return list(payload.get("value", [])), payload.get("@odata.nextLink")

    def read(self, table: RemoteTable, request: TableRead) -> Iterator[pa.RecordBatch]:
        if table.name not in self._types:
            described = self.describe(table.name)
            if described is not None:
                table = described
        types = self._types.get(table.name, {})
        schema = read_schema(table, request)
        names = schema.names
        page_size = self.config.page_size
        url: str | None = self._page_url(table, request, 0, page_size)
        skip = 0
        total = 0
        while url is not None:
            rows, next_link = self._page(url)
            if request.max_rows is not None:
                rows = rows[: max(0, request.max_rows - total)]
            if rows:
                columns = {
                    n: [convert_value(r.get(n), types.get(n, "Edm.String")) for r in rows]
                    for n in names
                }
                yield pa.RecordBatch.from_pydict(
                    {n: pa.array(columns[n], schema.field(n).type) for n in names}, schema=schema
                )
            total += len(rows)
            if request.max_rows is not None and total >= request.max_rows:
                return
            if next_link:
                url = next_link
            elif len(rows) >= page_size:
                skip += page_size
                url = self._page_url(table, request, skip, page_size)
            else:
                url = None


__all__ = ["ODataBackend", "convert_value", "edm_type"]
