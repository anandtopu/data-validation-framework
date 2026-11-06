from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dvf.config.diagnostics import Code
from dvf.config.formats import (
    parse_key_path,
    parse_scalar,
    read_config_file,
    read_yaml,
    set_path,
)
from dvf.core.errors import ConfigError


def _codes(exc: ConfigError) -> list[Code]:
    return [d.code for d in exc.diagnostics]


class TestYaml:
    def test_locations_are_recorded(self) -> None:
        data, locations = read_yaml("a:\n  b: 1\n  c:\n    - x\n    - y: 2\n", "f.yaml")
        assert data == {"a": {"b": 1, "c": ["x", {"y": 2}]}}
        loc = locations.get(("a", "c", 1, "y"))
        assert loc is not None
        assert (loc.line, loc.column) == (5, 7)
        assert locations.get(("a", "missing")) == locations.get(("a",))

    def test_yaml_12_scalars(self) -> None:
        text = (
            "on: [failed]\nflag: yes\nreal: true\nclient: 0100\nnum: 42\nneg: -7\n"
            "flt: 1.5\nexp: 1e3\nhex: 0x1F\nbase60: 12:30\nwhen: 2027-01-31\nnothing: ~\n"
        )
        data, _ = read_yaml(text, "f.yaml")
        assert data == {
            "on": ["failed"],
            "flag": "yes",
            "real": True,
            "client": "0100",
            "num": 42,
            "neg": -7,
            "flt": 1.5,
            "exp": 1000.0,
            "hex": 31,
            "base60": "12:30",
            "when": dt.date(2027, 1, 31),
            "nothing": None,
        }

    def test_duplicate_keys_are_errors(self) -> None:
        with pytest.raises(ConfigError) as info:
            read_yaml("a: 1\nb:\n  c: 1\n  c: 2\n", "f.yaml")
        (diagnostic,) = info.value.diagnostics
        assert diagnostic.code is Code.DUPLICATE_KEY
        assert diagnostic.path == "b.c"
        assert diagnostic.location is not None and diagnostic.location.line == 4

    def test_merge_keys_are_not_duplicates(self) -> None:
        data, locations = read_yaml("base: &b {x: 1, y: 2}\nchild:\n  <<: *b\n  y: 3\n", "f.yaml")
        assert data["child"] == {"x": 1, "y": 3}
        assert locations.get(("child", "y")) is not None

    def test_syntax_error_has_position(self) -> None:
        with pytest.raises(ConfigError) as info:
            read_yaml("a: [1, 2\nb: 3\n", "f.yaml")
        (diagnostic,) = info.value.diagnostics
        assert diagnostic.code is Code.PARSE_ERROR
        assert diagnostic.location is not None and diagnostic.location.line is not None

    def test_empty_document(self) -> None:
        assert read_yaml("", "f.yaml")[0] == {}


def test_read_json(write: Any) -> None:
    data, _ = read_config_file(write("c.json", '{"a": {"b": [1, 2]}}'))
    assert data == {"a": {"b": [1, 2]}}
    with pytest.raises(ConfigError) as info:
        read_config_file(write("bad.json", '{"a": 1,}'))
    assert info.value.diagnostics[0].location.line == 1  # type: ignore[union-attr]
    with pytest.raises(ConfigError) as info:
        read_config_file(write("dup.json", '{"a": 1, "a": 2}'))
    assert _codes(info.value) == [Code.DUPLICATE_KEY]


def test_read_toml(write: Any) -> None:
    data, _ = read_config_file(write("c.toml", 'version = 1\n[project]\nname = "x"\n'))
    assert data == {"version": 1, "project": {"name": "x"}}
    with pytest.raises(ConfigError) as info:
        read_config_file(write("bad.toml", "version = \n"))
    diagnostic = info.value.diagnostics[0]
    assert diagnostic.code is Code.PARSE_ERROR
    assert diagnostic.location is not None and diagnostic.location.line == 1


def test_read_properties(write: Any) -> None:
    path = write(
        "c.properties",
        """
        # comment
        ! also a comment
        version = 1
        project.name = demo
        suites[0].name = s1
        suites.0.source : src
        suites[0].tags = [a, b]
        long.value = first \\
            second
        escaped\\=key = v
        nothing = null
        """,
    )
    data, locations = read_config_file(path)
    assert data["version"] == 1
    assert data["project"] == {"name": "demo"}
    assert data["suites"] == [{"name": "s1", "source": "src", "tags": ["a", "b"]}]
    assert data["long"] == {"value": "first second"}
    assert data["escaped=key"] == "v"
    assert data["nothing"] is None
    assert locations.get(("suites", 0, "tags")).line == 7  # type: ignore[union-attr]


def test_properties_errors(write: Any) -> None:
    with pytest.raises(ConfigError, match="Expected 'key = value'"):
        read_config_file(write("a.properties", "just text\n"))
    with pytest.raises(ConfigError, match="skips items"):
        read_config_file(write("b.properties", "suites[2].name = x\n"))


def test_read_ini(write: Any) -> None:
    path = write(
        "c.ini",
        """
        [dvf]
        version = 1
        [project]
        name = demo
        [defaults.normalization]
        trim = rtrim
        """,
    )
    data, _ = read_config_file(path)
    assert data == {
        "version": 1,
        "project": {"name": "demo"},
        "defaults": {"normalization": {"trim": "rtrim"}},
    }
    with pytest.raises(ConfigError, match="INI syntax error"):
        read_config_file(write("bad.ini", "no section header\n"))


def test_file_errors(tmp_path: Path, write: Any) -> None:
    with pytest.raises(ConfigError) as info:
        read_config_file(tmp_path / "missing.yaml")
    assert _codes(info.value) == [Code.FILE_NOT_FOUND]
    with pytest.raises(ConfigError) as info:
        read_config_file(write("c.xml", "<x/>"))
    assert _codes(info.value) == [Code.UNSUPPORTED_FORMAT]
    with pytest.raises(ConfigError, match="must be a mapping"):
        read_config_file(write("list.yaml", "- 1\n- 2\n"))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a", ("a",)),
        ("a.b.c", ("a", "b", "c")),
        ("suites[0].tables[12].name", ("suites", 0, "tables", 12, "name")),
        ("suites.0.name", ("suites", 0, "name")),
    ],
)
def test_parse_key_path(text: str, expected: tuple[Any, ...]) -> None:
    assert parse_key_path(text) == expected


@pytest.mark.parametrize("text", ["", "a..b", "a[x]", ".a", "a[0"])
def test_parse_key_path_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        parse_key_path(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10", 10),
        ("1.5", 1.5),
        ("true", True),
        ("False", False),
        ("null", None),
        ("0100", "0100"),
        ("[a, b]", ["a", "b"]),
        ("{x: 1}", {"x": 1}),
        ("[unclosed", "[unclosed"),
        ("STATUS: X", "STATUS: X"),
        ("  padded  ", "padded"),
    ],
)
def test_parse_scalar(text: str, expected: Any) -> None:
    assert parse_scalar(text) == expected


def test_set_path_builds_structure() -> None:
    data: dict[str, Any] = {}
    set_path(data, ("a", "b", 0, "c"), 1)
    set_path(data, ("a", "b", 1), "x")
    set_path(data, ("a", "b", 0, "c"), 2)
    assert data == {"a": {"b": [{"c": 2}, "x"]}}
    with pytest.raises(ValueError, match="not a list"):
        set_path(data, ("a", 0), 1)
    with pytest.raises(ValueError, match="not a mapping"):
        set_path(data, ("a", "b", 1, "z"), 1)


@given(
    st.dictionaries(
        st.from_regex(r"[a-z][a-z0-9_]{0,8}", fullmatch=True), st.integers(), max_size=5
    )
)
def test_properties_round_trip(values: dict[str, int]) -> None:
    from dvf.config.formats import read_properties

    text = "\n".join(f"section.{k} = {v}" for k, v in values.items())
    data, _ = read_properties(text, "p.properties")
    assert data.get("section", {}) == values
