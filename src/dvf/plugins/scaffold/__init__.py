"""``dvf plugin new <kind> <name>``: scaffold an installable plugin package with tests.

The package registers one component through the entry point group of its kind
(``dvf.checks``, ``dvf.connectors``, ...), declares the plugin API range it supports, and ships
tests that exercise the component through dvf's public API once the package is installed
(``pip install -e '.[test]'`` then ``pytest``). Every generated component works as written; it
is a small, real example meant to be replaced with the author's own logic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from packaging.version import Version

from dvf import __version__
from dvf.core.errors import DvfError
from dvf.plugins.registry import PLUGIN_API_VERSION, PluginKind

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_DISTRIBUTION_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")


class ScaffoldError(DvfError):
    """The plugin package cannot be created (invalid name, existing folder, ...)."""


@dataclass(frozen=True)
class KindSpec:
    """How one component kind is scaffolded."""

    kind: PluginKind
    module: str
    """Module file holding the component, without ``.py``."""
    suffix: str
    """Class name suffix (``Connector``), or empty."""
    summary: str
    usage: str
    """A configuration snippet using the component (``{name}`` is replaced)."""


KINDS: dict[str, KindSpec] = {
    "check": KindSpec(
        PluginKind.CHECK,
        "checks",
        "",
        "A row check: values of a column must match a regular expression.",
        "suites:\n  - name: my_suite\n    # ...\n    tables:\n      - name: customers\n"
        "        # ...\n        checks:\n          - { type: {name}, column: customer_code }\n",
    ),
    "connector": KindSpec(
        PluginKind.CONNECTOR,
        "connector",
        "Connector",
        "A connector reading newline-delimited JSON files, one file per dataset.",
        "connections:\n  landing:\n    type: {name}\n    path: ./data   # holds customers.jsonl\n\n"
        "suites:\n  - name: my_suite\n    source: landing\n    tables:\n"
        "      - name: customers\n        source: { table: customers }\n",
    ),
    "normalizer": KindSpec(
        PluginKind.NORMALIZER,
        "normalizer",
        "Normalizer",
        "A normalizer removing characters (spaces and hyphens by default) before comparing.",
        "tables:\n  - name: customers\n    # ...\n    columns:\n      mappings:\n"
        "        - { source: phone, target: phone, normalize: [{name}] }\n"
        "        - source: iban\n          target: iban\n"
        "          normalize: [{ name: {name}, params: { characters: ' ' } }]\n",
    ),
    "reporter": KindSpec(
        PluginKind.REPORTER,
        "reporter",
        "Reporter",
        "A report format writing one CSV row per check.",
        "reports:\n  formats: [json, html, {name}]\n",
    ),
    "notifier": KindSpec(
        PluginKind.NOTIFIER,
        "notifier",
        "Notifier",
        "A notification channel appending one JSON line per notified run to a file.",
        "notifications:\n  - type: {name}\n    on: [failed, error]\n"
        "    path: ./notifications.jsonl\n",
    ),
    "secret_provider": KindSpec(
        PluginKind.SECRET_PROVIDER,
        "provider",
        "SecretProvider",
        "A secret provider reading secrets from files in a folder (Docker and Kubernetes "
        "secret mounts).",
        "connections:\n  warehouse:\n    type: postgres\n    host: db.example.com\n"
        "    user: dvf_reader\n    password: ${secret:{name}:warehouse_password}\n\n"
        "# The secret files are read from the folder in {env_var} (default /run/secrets).\n",
    ),
    "sink": KindSpec(
        PluginKind.SINK,
        "sink",
        "Sink",
        "A result sink appending every check result to a JSON Lines file.",
        "sinks:\n  - { type: {name}, path: ./results/checks.jsonl }\n",
    ),
}
"""Scaffoldable kinds, by the name used on the command line."""

KIND_ALIASES = {"secret-provider": "secret_provider", "secrets": "secret_provider"}


@dataclass(frozen=True)
class PluginSpec:
    """Everything the templates need."""

    kind: KindSpec
    name: str
    distribution: str
    module: str
    class_name: str
    plugin_api: str
    dvf_requirement: str

    @property
    def env_var(self) -> str:
        """Environment variable naming the secrets folder (secret providers only)."""
        return re.sub(r"[^A-Z0-9]+", "_", self.name.upper()) + "_DIR"

    def variables(self) -> dict[str, str]:
        return {
            "name": self.name,
            "kind": self.kind.kind.value,
            "group": self.kind.kind.entry_point_group,
            "kind_module": self.kind.module,
            "class_name": self.class_name,
            "module": self.module,
            "distribution": self.distribution,
            "plugin_api": self.plugin_api,
            "dvf_requirement": self.dvf_requirement,
            "summary": self.kind.summary,
            "usage": self.kind.usage.replace("{name}", self.name)
            .replace("{env_var}", self.env_var)
            .rstrip("\n"),
            "env_var": self.env_var,
            "staged_import": _staged_import(),
            **_notifier_imports(),
        }


def _staged_import() -> str:
    """The connector template's dvf imports: ``StagedConnector`` from the public API when it
    exports it, else from its module."""
    import dvf.plugins

    if hasattr(dvf.plugins, "StagedConnector"):
        return "from dvf.plugins import ConnectionConfig, StagedConnector, StagedData"
    return "\n".join(
        [
            "from dvf.connectors.staged import StagedConnector, StagedData",
            "from dvf.plugins import ConnectionConfig",
        ]
    )


def _notifier_imports() -> dict[str, str]:
    """The notifier template's imports: the message type from the public API when it exports
    it, else ``Any``."""
    import dvf.plugins

    if hasattr(dvf.plugins, "NotificationMessage"):
        lines = [
            "from typing import ClassVar",
            "",
            "from dvf.plugins import NotificationConfig, NotificationMessage, Notifier",
        ]
        return {"notifier_imports": "\n".join(lines), "message_type": "NotificationMessage"}
    lines = [
        "from typing import Any, ClassVar",
        "",
        "from dvf.plugins import NotificationConfig, Notifier",
    ]
    return {"notifier_imports": "\n".join(lines), "message_type": "Any"}


def normalize_kind(kind: str) -> KindSpec:
    key = KIND_ALIASES.get(kind.strip().lower(), kind.strip().lower().replace("-", "_"))
    if key not in KINDS:
        raise ScaffoldError(f"Unknown plugin kind '{kind}'. Choose one of: {', '.join(KINDS)}.")
    return KINDS[key]


def plugin_api_range(version: str = PLUGIN_API_VERSION) -> str:
    """The plugin API range a new plugin should declare: the current minor (0.x) or major."""
    parsed = Version(version)
    if parsed.major == 0:
        return f">=0.{parsed.minor},<0.{parsed.minor + 1}"
    return f">={parsed.major}.{parsed.minor},<{parsed.major + 1}"


def dvf_requirement(version: str = __version__) -> str:
    """The ``dvf-framework`` version range of the generated package."""
    parsed = Version(version)
    lower = f"{parsed.major}.{parsed.minor}"
    if parsed.is_prerelease or parsed.is_devrelease:
        lower += ".dev0"
    upper = f"{parsed.major + 1}" if parsed.major >= 1 else f"{parsed.major}.{parsed.minor + 1}"
    return f">={lower},<{upper}"


def _camel(text: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[^A-Za-z0-9]+", text) if part)


def plugin_spec(kind: str, name: str, *, distribution: str | None = None) -> PluginSpec:
    """Validate the inputs of ``dvf plugin new`` and derive the package's names."""
    spec = normalize_kind(kind)
    if not _NAME_RE.match(name):
        raise ScaffoldError(
            f"Invalid component name '{name}': use lowercase letters, digits, and underscores, "
            "namespaced with dots (for example 'acme.vat_number_valid')."
        )
    if spec.kind is PluginKind.SECRET_PROVIDER and not _PROVIDER_RE.match(name):
        raise ScaffoldError(
            f"Invalid secret provider name '{name}': it is written in "
            "${secret:<provider>:...} references, so use lowercase letters, digits, and "
            "underscores without dots (for example 'acme_vault')."
        )
    distribution = distribution or "dvf-" + re.sub(r"[._]+", "-", name)
    if not _DISTRIBUTION_RE.match(distribution):
        raise ScaffoldError(f"Invalid package name '{distribution}'.")
    module = re.sub(r"[^a-z0-9_]+", "_", distribution.lower()).strip("_")
    short = _camel(name.rsplit(".", 1)[-1])
    class_name = short if short.endswith(spec.suffix) else short + spec.suffix
    if class_name[:1].isdigit():
        class_name = "Plugin" + class_name
    return PluginSpec(
        kind=spec,
        name=name,
        distribution=distribution,
        module=module,
        class_name=class_name,
        plugin_api=plugin_api_range(),
        dvf_requirement=dvf_requirement(),
    )


def _template(name: str) -> str:
    return (resources.files("dvf.plugins.scaffold") / "templates" / name).read_text(
        encoding="utf-8"
    )


def render(text: str, variables: dict[str, str]) -> str:
    """Replace ``{{ name }}`` placeholders; unknown placeholders are an error."""

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            raise KeyError(f"unknown template placeholder '{key}'")
        return variables[key]

    return re.sub(r"\{\{\s*([a-z_]+)\s*\}\}", replace, text)


def package_files(spec: PluginSpec) -> dict[str, str]:
    """The generated files: ``{relative path: text}``."""
    variables = spec.variables()
    kind = spec.kind.kind.value
    module_dir = f"src/{spec.module}"
    files = {
        "pyproject.toml": "pyproject.toml.tmpl",
        "README.md": "README.md.tmpl",
        ".gitignore": "gitignore.tmpl",
        f"{module_dir}/__init__.py": "__init__.py.tmpl",
        f"{module_dir}/py.typed": "py.typed.tmpl",
        f"{module_dir}/{spec.kind.module}.py": f"{kind}.py.tmpl",
        f"tests/test_{spec.kind.module}.py": f"test_{kind}.py.tmpl",
    }
    return {path: render(_template(template), variables) for path, template in files.items()}


def create_package(
    kind: str,
    name: str,
    output: Path | None = None,
    *,
    distribution: str | None = None,
    force: bool = False,
) -> tuple[PluginSpec, Path, list[Path]]:
    """Write the plugin package into ``output`` (default: a folder named after the package)."""
    spec = plugin_spec(kind, name, distribution=distribution)
    folder = output if output is not None else Path(spec.distribution)
    if folder.exists() and any(folder.iterdir()) and not force:
        raise ScaffoldError(
            f"{folder} already exists and is not empty; choose another --output, or pass "
            "--force to write into it."
        )
    written = []
    for rel, text in package_files(spec).items():
        path = folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)
    return spec, folder, written


__all__ = [
    "KINDS",
    "KindSpec",
    "PluginSpec",
    "ScaffoldError",
    "create_package",
    "dvf_requirement",
    "package_files",
    "plugin_api_range",
    "plugin_spec",
    "render",
]
