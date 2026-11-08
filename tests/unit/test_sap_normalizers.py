"""SAP normalizer parameters, side handling, and how comparison plans use them.

Value-level behavior is covered by ``tests/parity/test_sap_parity.py``; these tests cover the
configuration surface and the Normalizer API extension (``for_side``, ``input_columns``).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest

from dvf import api
from dvf.compare.plan import build_plan, pair_columns
from dvf.config.base import NormalizerRef
from dvf.config.models import NormalizationConfig, TableConfig
from dvf.core.datasets import ColumnSchema, DatasetKind, DatasetRef, DatasetSchema
from dvf.core.errors import PluginError
from dvf.core.types import CanonicalType, LogicalType
from dvf.packs.sap.normalizers import SAP_NORMALIZERS, SapCurr, SapQuan
from dvf.plugins.base import Normalizer
from dvf.sql.dialects import get_dialect

STRING = CanonicalType.string()


def make(name: str, params: dict[str, Any] | None = None) -> Normalizer:
    normalizer = SAP_NORMALIZERS[name](params or {})
    normalizer.plugin_name = name
    return normalizer


@pytest.mark.parametrize(
    ("name", "params", "message"),
    [
        ("sap_curr", {}, "exactly one of 'currency' or 'currency_column'"),
        ("sap_curr", {"currency": "JPY", "currency_column": "WAERS"}, "exactly one"),
        ("sap_curr", {"currency": "JPY", "target_currency_column": "X"}, "needs 'currency_column'"),
        ("sap_curr", {"currency": "JPY", "decimals": {"XYZ": 20}}, "between 0 and 14"),
        ("sap_curr", {"currency": "JPY", "side": "left"}, "side"),
        ("sap_quan", {}, "exactly one of 'unit' or 'unit_column'"),
        ("sap_quan", {"unit": "KG", "decimals": {"KG": -1}}, "between 0 and 14"),
        ("sap_bool", {"true_values": ["X"], "false_values": ["X "]}, "both true and false"),
        ("sap_bool", {"true_values": [" "]}, "blank cannot be a true value"),
        ("sap_spras", {"overrides": {"ZZ": "Z1"}}, "one character"),
        ("sap_spras", {"overrides": {"Z": "Z12"}}, "two ASCII characters"),
        ("sap_spras", {"direction": "to_sap", "overrides": {"Z": "EN"}}, "maps to both"),
        ("sap_alpha", {"mode": "both"}, "mode"),
        ("sap_matnr_length", {"length": 20}, "length"),
        ("sap_numc", {"length": 0}, "length"),
        ("sap_dats", {"invalid": "raise"}, "invalid"),
    ],
)
def test_invalid_parameters(name: str, params: dict[str, Any], message: str) -> None:
    with pytest.raises(PluginError, match=message):
        make(name, params)


def test_shorthands_from_configuration() -> None:
    table = TableConfig.model_validate(
        {
            "name": "t",
            "source": {"table": "t"},
            "keys": [
                {"source": "a", "normalize": [{"sap_alpha": "pad"}]},
                {"source": "b", "normalize": [{"sap_numc": 6}]},
                {"source": "c", "normalize": [{"sap_matnr_length": 40}]},
                {"source": "d", "normalize": [{"sap_spras": "to_sap"}]},
            ],
        }
    )
    refs: list[NormalizerRef] = [k.normalize[0] for k in table.keys]
    made = [make(r.name, r.params) for r in refs]
    assert made[0].to_python("123", STRING) == "0000000123"
    assert made[1].to_python("12", STRING) == "000012"
    assert made[2].to_python("12", STRING) == "12".rjust(40, "0")
    assert made[3].to_python("en", STRING) == "E"


def test_side_binding_and_input_columns() -> None:
    curr = make("sap_curr", {"currency_column": "WAERS", "target_currency_column": "CURRENCY"})
    source = curr.for_side("source")
    assert source is not None and source is not curr
    assert source.input_columns() == ("WAERS",)
    assert curr.for_side("target") is None  # target amounts are external by default
    both = make(
        "sap_curr",
        {"currency_column": "WAERS", "target_currency_column": "CURRENCY", "side": "both"},
    )
    target = both.for_side("target")
    assert target is not None and target.input_columns() == ("CURRENCY",)
    constant = make("sap_curr", {"currency": "JPY"})
    assert constant.for_side("source").input_columns() == ()  # type: ignore[union-attr]
    quan = make("sap_quan", {"unit_column": "MEINS", "decimals": {"ST": 0}})
    assert quan.for_side("source") is not None and quan.for_side("target") is not None
    # Built-in normalizers keep the default: both sides, no other columns.
    from dvf.normalize.normalizers import Trim

    trim = Trim()
    assert trim.for_side("target") is trim and trim.input_columns() == ()


def test_applies_to_types() -> None:
    assert make("sap_alpha").applies_to(STRING)
    assert not make("sap_alpha").applies_to(CanonicalType.integer())
    assert make("sap_dats").applies_to(CanonicalType.integer())
    assert not make("sap_dats").applies_to(CanonicalType.date())  # a DATE target is left alone
    assert make("sap_dats").output_type(STRING) == CanonicalType.date()
    assert make("sap_tims").output_type(STRING) == CanonicalType.time(0)
    assert make("sap_bool").output_type(STRING).logical is LogicalType.BOOLEAN
    curr = make("sap_curr", {"currency": "JPY"})
    assert not curr.applies_to(STRING)
    assert curr.output_type(CanonicalType.decimal(13, 2)) == CanonicalType.decimal(38, 3)
    assert make("sap_curr", {"currency": "JPY", "decimals": {"CLF": 4}}).output_type(
        CanonicalType.decimal(13, 2)
    ) == CanonicalType.decimal(38, 4)
    quan = make("sap_quan", {"unit": "KG"})
    assert quan.output_type(CanonicalType.decimal(13, 3)) == CanonicalType.decimal(38, 3)


def test_reference_needs_the_currency_for_column_mode() -> None:
    curr = make("sap_curr", {"currency_column": "WAERS"})
    with pytest.raises(NotImplementedError, match="convert"):
        curr.to_python(Decimal("1.00"), CanonicalType.decimal(13, 2))
    assert isinstance(curr, SapCurr)
    assert curr.decimals_of("JPY ") == 0 and curr.decimals_of("") == 2
    quan = make("sap_quan", {"unit_column": "MEINS", "decimals": {"ST": 0}})
    assert isinstance(quan, SapQuan)
    with pytest.raises(NotImplementedError, match="convert"):
        quan.to_python(Decimal("1"), CanonicalType.decimal(13, 3))
    assert quan.decimals_of(None) is None


def _schema(name: str, columns: dict[str, CanonicalType]) -> DatasetSchema:
    return DatasetSchema(
        ref=DatasetRef(connection="c", kind=DatasetKind.TABLE, name=name),
        columns=tuple(
            ColumnSchema(name=n, native_type=t.logical.value, type=t, ordinal=i + 1)
            for i, (n, t) in enumerate(columns.items())
        ),
    )


def test_plan_applies_sap_curr_on_the_source_only() -> None:
    table = TableConfig.model_validate(
        {
            "name": "bseg",
            "source": {"table": "BSEG"},
            "keys": ["BELNR"],
            "columns": {
                "mappings": [
                    {
                        "source": "WRBTR",
                        "target": "amount",
                        "normalize": [{"sap_curr": {"currency_column": "WAERS"}}],
                    }
                ],
                "exclude": ["WAERS", "currency"],
            },
        }
    )
    source = _schema(
        "BSEG",
        {"BELNR": STRING, "WRBTR": CanonicalType.decimal(13, 2), "WAERS": STRING},
    )
    target = _schema(
        "fact", {"BELNR": STRING, "amount": CanonicalType.decimal(18, 3), "currency": STRING}
    )
    pairing = pair_columns(table, source, target)

    def factory(ref: NormalizerRef) -> Normalizer:
        return make(ref.name, ref.params)

    plan = build_plan(
        table,
        pairing,
        normalization=NormalizationConfig(),
        source_dialect=get_dialect("duckdb"),
        target_dialect=get_dialect("postgres"),
        factory=factory,
    )
    amount = plan.columns[0]
    assert [n.plugin_name for n in amount.source.normalizers] == ["sap_curr"]
    assert amount.target.normalizers == ()
    assert '"WAERS"' in amount.source.normalized
    assert amount.pair_spec.source.scale == 3 and amount.pair_spec.target.scale == 3


def test_comparison_shifts_currency_and_reads_flags(tmp_path: Path) -> None:
    """An ECC-style source and a typed target in DuckDB, compared with duckdb and polars."""
    sap = tmp_path / "sap.duckdb"
    dw = tmp_path / "dw.duckdb"
    with duckdb.connect(str(sap)) as db:
        db.execute(
            "CREATE TABLE BSEG (BELNR VARCHAR, WRBTR DECIMAL(13, 2), WAERS VARCHAR, "
            "XREVERSED VARCHAR, BUDAT VARCHAR, SPRAS VARCHAR)"
        )
        db.execute(
            "INSERT INTO BSEG VALUES "
            "('0000000001', 12.34, 'EUR', 'X', '20240131', 'E'), "
            "('0000000002', 12.34, 'JPY', ' ', '00000000', 'D'), "
            "('0000000003', 12.34, 'KWD', '', '20240230', 'a'), "
            "('0000000004', -0.05, 'BHD ', 'X', '20240229', '1')"
        )
    with duckdb.connect(str(dw)) as db:
        db.execute(
            "CREATE TABLE doc (doc_no BIGINT, amount DECIMAL(18, 3), reversed BOOLEAN, "
            "posted DATE, lang VARCHAR)"
        )
        db.execute(
            "INSERT INTO doc VALUES (1, 12.34, TRUE, DATE '2024-01-31', 'EN'), "
            "(2, 1234, FALSE, NULL, 'DE'), (3, 1.234, FALSE, NULL, 'AF'), "
            "(4, -0.005, TRUE, DATE '2024-02-29', 'ZH')"
        )
    config: dict[str, Any] = {
        "version": 1,
        "project": {"name": "sap-normalizers"},
        "connections": {
            "sap": {"type": "duckdb", "database": str(sap)},
            "dw": {"type": "duckdb", "database": str(dw)},
        },
        "suites": [
            {
                "name": "fi",
                "source": "sap",
                "target": "dw",
                "tables": [
                    {
                        "name": "bseg",
                        "source": {"table": "BSEG"},
                        "target": {"table": "doc"},
                        "keys": [
                            {"source": "BELNR", "target": "doc_no", "normalize": ["sap_alpha"]}
                        ],
                        "columns": {
                            "include": ["WRBTR", "XREVERSED", "BUDAT", "SPRAS"],
                            "mappings": [
                                {
                                    "source": "WRBTR",
                                    "target": "amount",
                                    "normalize": [{"sap_curr": {"currency_column": "WAERS"}}],
                                },
                                {
                                    "source": "XREVERSED",
                                    "target": "reversed",
                                    "normalize": ["sap_bool"],
                                },
                                {"source": "BUDAT", "target": "posted", "normalize": ["sap_dats"]},
                                {"source": "SPRAS", "target": "lang", "normalize": ["sap_spras"]},
                            ],
                        },
                        "schema_checks": [],
                        "comparison": {"strategies": ["row_count", "full_diff"]},
                    }
                ],
            }
        ],
    }
    for engine in ("duckdb", "polars"):
        config["suites"][0]["tables"][0]["engine"] = engine
        loaded = api.load_config_data(config, base_dir=tmp_path, strict=False)
        out = api.run(loaded, write_reports=False, history=False)
        checks = {c.type: c for c in out.result.suites[0].datasets[0].checks}
        assert checks["compare.keys"].status.value == "passed", engine
        assert checks["compare.column_mismatch"].status.value == "passed", (
            engine,
            checks["compare.column_mismatch"].message,
        )
