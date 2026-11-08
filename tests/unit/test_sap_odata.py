"""The odata backend of sap_rfc against a local HTTP server serving OData v2 and v4 (synthetic)."""

from __future__ import annotations

import base64
import json
import threading
import urllib.parse
from collections.abc import Iterator
from datetime import UTC, date, datetime, time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest

from dvf.config.connections import SapRfcConnection
from dvf.config.models import DatasetSpec
from dvf.connectors.sap_rfc import SapRfcConnector
from dvf.core.errors import ConnectorError
from dvf.core.types import LogicalType
from dvf.packs.sap.rfc.odata import convert_value

V2_METADATA = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx Version="1.0" xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx"
  xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"
  xmlns:sap="http://www.sap.com/Protocols/SAPData">
  <edmx:DataServices m:DataServiceVersion="2.0">
    <Schema Namespace="API_BUSINESS_PARTNER" xmlns="http://schemas.microsoft.com/ado/2008/09/edm">
      <EntityType Name="A_BusinessPartnerType">
        <Key><PropertyRef Name="BusinessPartner"/></Key>
        <Property Name="BusinessPartner" Type="Edm.String" Nullable="false" MaxLength="10"
          sap:label="Business Partner"/>
        <Property Name="BusinessPartnerName" Type="Edm.String" MaxLength="81"/>
        <Property Name="CreationDate" Type="Edm.DateTime" Precision="0"/>
        <Property Name="CreationTime" Type="Edm.Time" Precision="0"/>
        <Property Name="LastChange" Type="Edm.DateTimeOffset" Precision="7"/>
        <Property Name="Revenue" Type="Edm.Decimal" Precision="15" Scale="2"/>
        <Property Name="Employees" Type="Edm.Int64"/>
        <Property Name="IsBlocked" Type="Edm.Boolean"/>
        <Property Name="Logo" Type="Edm.Binary"/>
      </EntityType>
      <EntityContainer Name="API_BUSINESS_PARTNER_Entities" m:IsDefaultEntityContainer="true">
        <EntitySet Name="A_BusinessPartner" EntityType="API_BUSINESS_PARTNER.A_BusinessPartnerType"/>
      </EntityContainer>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>"""

V4_METADATA = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx">
  <edmx:DataServices>
    <Schema Namespace="com.sap.gateway.srvd.zsd_orders" xmlns="http://docs.oasis-open.org/odata/ns/edm">
      <EntityType Name="OrderType">
        <Key><PropertyRef Name="SalesOrder"/></Key>
        <Property Name="SalesOrder" Type="Edm.String" Nullable="false" MaxLength="10"/>
        <Property Name="OrderDate" Type="Edm.Date"/>
        <Property Name="OrderTime" Type="Edm.TimeOfDay" Precision="0"/>
        <Property Name="NetAmount" Type="Edm.Decimal" Precision="16" Scale="3"/>
        <Property Name="ChangedAt" Type="Edm.DateTimeOffset" Precision="7"/>
      </EntityType>
      <EntityContainer Name="Container">
        <EntitySet Name="SalesOrder" EntityType="com.sap.gateway.srvd.zsd_orders.OrderType"/>
      </EntityContainer>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>"""


def _partners() -> list[dict[str, Any]]:
    rows = []
    for i in range(1, 6):
        rows.append(
            {
                "__metadata": {"type": "API_BUSINESS_PARTNER.A_BusinessPartnerType"},
                "BusinessPartner": f"{i:010d}",
                "BusinessPartnerName": f"Partner {i}",
                "CreationDate": "/Date(1704067200000)/",
                "CreationTime": "PT12H30M05S",
                "LastChange": "/Date(1704067200000+0000)/",
                "Revenue": f"{i}00.5{i}",
                "Employees": str(i * 10),
                "IsBlocked": i == 2,
                "Logo": base64.b64encode(bytes([i])).decode(),
            }
        )
    return rows


class _Handler(BaseHTTPRequestHandler):
    requests: ClassVar[list[dict[str, Any]]] = []

    def log_message(self, format: str, *args: Any) -> None:
        return None

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parts = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(parts.query))
        self.requests.append(
            {"path": parts.path, "query": query, "auth": self.headers.get("Authorization")}
        )
        if parts.path.startswith("/v2/"):
            self._v2(parts.path.removeprefix("/v2/"), query)
        elif parts.path.startswith("/v4/"):
            self._v4(parts.path.removeprefix("/v4/"), query)
        else:
            self._send(b"not found", "text/plain", 404)

    def _v2(self, path: str, query: dict[str, str]) -> None:
        if path == "$metadata":
            self._send(V2_METADATA.encode(), "application/xml")
            return
        if path != "A_BusinessPartner":
            self._send(b'{"error": "no such entity set"}', "application/json", 404)
            return
        rows = _partners()
        if query.get("$filter") == "IsBlocked eq false":
            rows = [r for r in rows if not r["IsBlocked"]]
        skip, top = int(query.get("$skip", 0)), int(query.get("$top", 1000))
        page = rows[skip : skip + top]
        self._send(json.dumps({"d": {"results": page}}).encode(), "application/json")

    def _v4(self, path: str, query: dict[str, str]) -> None:
        if path == "$metadata":
            self._send(V4_METADATA.encode(), "application/xml")
            return
        rows = [
            {
                "SalesOrder": f"{i:010d}",
                "OrderDate": f"2024-01-{i:02d}",
                "OrderTime": "08:15:00",
                "NetAmount": f"{i}.125",
                "ChangedAt": "2024-01-01T10:00:00Z",
            }
            for i in range(1, 8)
        ]
        token = int(query.get("$skiptoken", 0))
        page = rows[token : token + 3]
        body: dict[str, Any] = {"value": page}
        if token + 3 < len(rows):
            body["@odata.nextLink"] = f"SalesOrder?$skiptoken={token + 3}"
        self._send(json.dumps(body).encode(), "application/json")


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _connector(tmp_path: Path, url: str, **extra: Any) -> SapRfcConnector:
    config = SapRfcConnection.model_validate(
        {
            "type": "sap_rfc",
            "backend": "odata",
            "url": url,
            "client": "200",
            "user": "DVF_TEST",
            "password": "synthetic-not-a-secret",
            **extra,
        }
    )
    return SapRfcConnector("odata", config, tmp_path)


def test_v2_entity_set_schema_and_values(server: str, tmp_path: Path) -> None:
    _Handler.requests.clear()
    con = _connector(tmp_path, f"{server}/v2/", page_size=2)
    try:
        rel = con.resolve(DatasetSpec(table="A_BusinessPartner"))
        schema = con.get_schema(rel)
        assert schema.primary_key == ("BusinessPartner",)
        types = {c.name: c for c in schema.columns}
        assert types["BusinessPartner"].native_type == "Edm.String(10)"
        assert types["Revenue"].type.logical is LogicalType.DECIMAL
        assert types["Revenue"].type.scale == 2
        assert types["CreationDate"].type.logical is LogicalType.TIMESTAMP
        rows = con.query(f"SELECT * FROM {rel.sql} ORDER BY BusinessPartner").to_pylist()
    finally:
        con.close()
    assert len(rows) == 5
    first = rows[0]
    assert first["Revenue"] == Decimal("100.51")
    assert first["Employees"] == 10
    assert first["CreationDate"] == datetime(2024, 1, 1)
    assert first["CreationTime"] == time(12, 30, 5)
    assert first["Logo"] == b"\x01"
    pages = [r for r in _Handler.requests if r["path"].endswith("A_BusinessPartner")]
    assert [p["query"].get("$skip") for p in pages] == [None, "2", "4"]
    assert all(p["query"]["$orderby"] == "BusinessPartner" for p in pages)
    assert all(p["query"]["$format"] == "json" for p in pages)
    assert all(p["query"]["sap-client"] == "200" for p in pages)
    expected = "Basic " + base64.b64encode(b"DVF_TEST:synthetic-not-a-secret").decode()
    assert all(p["auth"] == expected for p in _Handler.requests)


def test_v2_filter_and_select(server: str, tmp_path: Path) -> None:
    _Handler.requests.clear()
    con = _connector(tmp_path, f"{server}/v2")
    try:
        spec = DatasetSpec(
            table="A_BusinessPartner",
            options={"where": "IsBlocked eq false", "fields": ["BusinessPartner", "Revenue"]},
        )
        rel = con.resolve(spec)
        rows = con.query(f"SELECT * FROM {rel.sql}").to_pylist()
    finally:
        con.close()
    assert len(rows) == 4 and set(rows[0]) == {"BusinessPartner", "Revenue"}
    page = next(r for r in _Handler.requests if r["path"].endswith("A_BusinessPartner"))
    assert page["query"]["$select"] == "BusinessPartner,Revenue"
    assert page["query"]["$filter"] == "IsBlocked eq false"


def test_v4_next_links_and_types(server: str, tmp_path: Path) -> None:
    con = _connector(tmp_path, f"{server}/v4", page_size=3)
    try:
        rel = con.resolve(DatasetSpec(table="SalesOrder"))
        rows = con.query(f"SELECT * FROM {rel.sql} ORDER BY SalesOrder").to_pylist()
        assert con.list_datasets("Sales*") == ["SalesOrder"]
    finally:
        con.close()
    assert len(rows) == 7
    assert rows[0]["OrderDate"] == date(2024, 1, 1)
    assert rows[0]["OrderTime"] == time(8, 15)
    assert rows[0]["NetAmount"] == Decimal("1.125")
    assert rows[0]["ChangedAt"] == datetime(2024, 1, 1, 10, tzinfo=UTC)


def test_unknown_entity_set_and_http_errors(server: str, tmp_path: Path) -> None:
    con = _connector(tmp_path, f"{server}/v2")
    try:
        assert not con.exists(con.resolve(DatasetSpec(table="Nope")))
        with pytest.raises(NotImplementedError, match="cannot call function modules"):
            con.call("RFC_PING")
    finally:
        con.close()
    broken = _connector(tmp_path, f"{server}/missing")
    try:
        with pytest.raises(ConnectorError, match="HTTP 404"):
            broken.get_schema(broken.resolve(DatasetSpec(table="X")))
    finally:
        broken.close()


def test_value_conversion() -> None:
    assert convert_value("/Date(-86400000)/", "Edm.DateTime") == datetime(1969, 12, 31)
    assert convert_value("/Date(0+0060)/", "Edm.DateTimeOffset") == datetime(1970, 1, 1, tzinfo=UTC)
    assert convert_value("PT0H0M1.5S", "Edm.Time") == time(0, 0, 1, 500000)
    assert convert_value("12:00:00.25", "Edm.TimeOfDay") == time(12, 0, 0, 250000)
    assert convert_value(1.5, "Edm.Decimal") == Decimal("1.5")
    assert convert_value("true", "Edm.Boolean") is True
    assert convert_value(None, "Edm.String") is None


def test_url_is_required_and_checked() -> None:
    with pytest.raises(ValueError, match="http"):
        SapRfcConnection.model_validate(
            {"type": "sap_rfc", "backend": "odata", "client": "100", "url": "file:///etc/passwd"}
        )
