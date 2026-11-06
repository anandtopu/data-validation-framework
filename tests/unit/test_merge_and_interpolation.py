from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dvf.config.diagnostics import Code, LocationMap, SourceLocation
from dvf.config.interpolation import Interpolator
from dvf.config.merge import deep_merge, merge_layer, replaced_paths
from dvf.core.enums import DiagnosticLevel
from dvf.plugins.registry import Registry
from dvf.security.redaction import global_redactor
from dvf.security.secrets import SecretContext, SecretResolver


def test_deep_merge_semantics() -> None:
    base = {"a": {"b": 1, "c": [1, 2]}, "d": 1}
    overlay = {"a": {"c": [3], "e": 2}, "f": {"g": 1}}
    assert deep_merge(base, overlay) == {"a": {"b": 1, "c": [3], "e": 2}, "d": 1, "f": {"g": 1}}
    assert base == {"a": {"b": 1, "c": [1, 2]}, "d": 1}  # not mutated
    assert replaced_paths(base, overlay) == [("a", "c")]


_json = st.recursive(
    st.one_of(st.integers(), st.text(max_size=5), st.booleans()),
    lambda children: st.dictionaries(st.text("abc", min_size=1, max_size=2), children, max_size=3),
    max_leaves=10,
)


@given(
    st.dictionaries(st.text("abc", min_size=1, max_size=2), _json, max_size=4),
    st.dictionaries(st.text("abc", min_size=1, max_size=2), _json, max_size=4),
)
def test_merge_properties(base: dict[str, Any], overlay: dict[str, Any]) -> None:
    merged = deep_merge(base, overlay)
    assert set(merged) == set(base) | set(overlay)
    for key, value in overlay.items():
        if not (isinstance(value, dict) and isinstance(base.get(key), dict)):
            assert merged[key] == value
    assert deep_merge(merged, overlay) == merged  # idempotent


def test_merge_layer_tracks_locations() -> None:
    base_locations = LocationMap()
    base_locations.set(("a",), SourceLocation("base", 1))
    base_locations.set(("a", 0), SourceLocation("base", 2))
    base_locations.set(("a", 1), SourceLocation("base", 3))
    overlay_locations = LocationMap()
    overlay_locations.set(("a",), SourceLocation("overlay", 1))
    overlay_locations.set(("a", 0), SourceLocation("overlay", 2))
    merged = merge_layer({"a": [1, 2]}, base_locations, {"a": [9]}, overlay_locations)
    assert merged == {"a": [9]}
    assert base_locations.get(("a", 0)) == SourceLocation("overlay", 2)
    assert base_locations.get(("a", 1)) == SourceLocation("overlay", 1)  # stale item dropped


def _interpolator(
    registry: Registry,
    *,
    environ: dict[str, str] | None = None,
    dotenv: dict[str, str] | None = None,
    strict: bool = True,
    resolve_secrets: bool = True,
) -> Interpolator:
    env = environ or {}
    context = SecretContext(environ=env, dotenv=dotenv or {})
    return Interpolator(
        environ=env,
        dotenv=dotenv or {},
        secrets=SecretResolver(registry, context),
        locations=LocationMap(default_file="cfg.yaml"),
        strict=strict,
        resolve_secrets=resolve_secrets,
    )


class TestInterpolation:
    def test_env_var_default_and_dotenv(self, registry: Registry) -> None:
        interp = _interpolator(registry, environ={"HOST": "db"}, dotenv={"PORT": "5432"})
        assert interp.interpolate("${env:HOST}") == "db"
        assert interp.interpolate("${env:PORT}") == "5432"
        assert interp.interpolate("${env:MISSING:-fallback}") == "fallback"
        assert interp.interpolate("${env:MISSING:-}") == ""
        assert interp.interpolate("pg://${env:HOST}:${env:PORT}/x") == "pg://db:5432/x"
        assert interp.diagnostics == []

    def test_missing_env_strict_and_lenient(self, registry: Registry) -> None:
        strict = _interpolator(registry)
        assert strict.interpolate({"a": "${env:NOPE}"}) == {"a": "${env:NOPE}"}
        assert [d.level for d in strict.diagnostics] == [DiagnosticLevel.ERROR]
        assert strict.diagnostics[0].code is Code.ENV_MISSING
        assert strict.diagnostics[0].path == "a"
        lenient = _interpolator(registry, strict=False)
        lenient.interpolate("${env:NOPE}")
        assert [d.level for d in lenient.diagnostics] == [DiagnosticLevel.WARNING]

    def test_vars_keep_type_and_cannot_nest(self, registry: Registry) -> None:
        interp = _interpolator(registry, environ={"E": "x"})
        variables = interp.interpolate_vars({"n": 5, "s": "${env:E}", "bad": "${var:n}"})
        assert variables["n"] == 5 and variables["s"] == "x"
        assert interp.diagnostics[0].code is Code.INTERPOLATION_SYNTAX
        interp.variables = variables
        assert interp.interpolate("${var:n}") == 5
        assert interp.interpolate("n=${var:n}") == "n=5"
        interp.interpolate("${var:missing}")
        assert interp.diagnostics[-1].code is Code.VAR_MISSING

    def test_run_placeholders_are_deferred(self, registry: Registry) -> None:
        interp = _interpolator(registry)
        assert interp.interpolate("./r/${run.date}/${run.id}") == "./r/${run.date}/${run.id}"
        interp.interpolate("${run.nope}")
        assert interp.diagnostics[0].code is Code.RUN_PLACEHOLDER_UNKNOWN

    def test_escape_and_syntax_errors(self, registry: Registry) -> None:
        interp = _interpolator(registry)
        assert interp.interpolate("$${env:X} and $${") == "${env:X} and ${"
        assert interp.interpolate("no placeholders") == "no placeholders"
        interp.interpolate("${what:ever}")
        interp.interpolate("${env:bad-name}")
        interp.interpolate("${nocolon}")
        assert {d.code for d in interp.diagnostics} == {Code.INTERPOLATION_SYNTAX}
        assert len(interp.diagnostics) == 3

    def test_non_strings_pass_through(self, registry: Registry) -> None:
        interp = _interpolator(registry)
        assert interp.interpolate([1, True, None, 2.5]) == [1, True, None, 2.5]

    def test_secrets(self, registry: Registry) -> None:
        interp = _interpolator(registry, environ={"PW": "s3cret-value"})
        assert interp.interpolate("${secret:env:PW}") == "s3cret-value"
        assert "s3cret-value" in global_redactor()
        interp.interpolate("${secret:envv:PW}")
        assert interp.diagnostics[-1].code is Code.SECRET_PROVIDER_UNKNOWN
        assert "Did you mean 'env'" in interp.diagnostics[-1].message
        interp.interpolate("${secret:env:MISSING}")
        assert interp.diagnostics[-1].code is Code.SECRET_UNRESOLVED
        interp.interpolate("${secret:azurekv:vault#name}")
        assert "declared but not implemented" in interp.diagnostics[-1].message
        interp.interpolate("${secret:no-colon}")
        assert interp.diagnostics[-1].code is Code.INTERPOLATION_SYNTAX

    def test_secrets_not_resolved_when_disabled(self, registry: Registry) -> None:
        interp = _interpolator(registry, resolve_secrets=False)
        assert interp.interpolate("${secret:env:PW}") == "${secret:env:PW}"
        assert interp.diagnostics == []
        interp.interpolate("${secret:nope:x}")
        assert interp.diagnostics[0].code is Code.SECRET_PROVIDER_UNKNOWN


@pytest.mark.parametrize("value", ["plain", "${", "$$", "{env:X}"])
def test_strings_without_placeholders(registry: Registry, value: str) -> None:
    interp = _interpolator(registry)
    assert interp.interpolate(value) == value
    assert interp.diagnostics == []
