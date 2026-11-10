"""Packaging and release checks.

The fast tests read pyproject.toml, the Dockerfiles, and the release configuration. The slow
tests build the sdist and wheel with ``uv build`` into a temporary folder and inspect them, as
PyPI and a first-time user would see them.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import re
import shutil
import subprocess
import tarfile
import tomllib
import zipfile
from dataclasses import dataclass
from email.message import Message
from email.parser import Parser
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

import dvf

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src" / "dvf"
PYPROJECT: dict[str, Any] = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text("utf-8"))
PROJECT: dict[str, Any] = PYPROJECT["project"]
EXTRAS: dict[str, list[str]] = PROJECT["optional-dependencies"]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

# Drivers whose licenses do not allow bundling; users install them (AGENTS.md).
PROPRIETARY = {"hdbcli", "pyrfc", "teradatasql", "sqlanydb"}
# Never in the container images either: mssql-python bundles Microsoft's proprietary ODBC Driver
# 18. Users may still install the `mssql` extra themselves.
NOT_IN_IMAGES = PROPRIETARY | {"mssql-python"}
# The open-source extras the full container image installs (BUILD_PROMPT.md section 12.5).
FULL_IMAGE_EXTRAS = {
    "postgres",
    "mysql",
    "oracle",
    "mongodb",
    "sqlalchemy",
    "odbc",
    "polars",
    "excel",
    "delta",
    "iceberg",
    "avro",
}
SHA_PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def _requirement_names(requirements: list[str]) -> set[str]:
    return {canonicalize_name(Requirement(text).name) for text in requirements}


def _load_yaml(path: Path) -> dict[Any, Any]:
    data = yaml.safe_load(path.read_text("utf-8"))
    assert isinstance(data, dict)
    return data


def _uses(workflow: dict[Any, Any]) -> list[str]:
    found = []
    for job in workflow["jobs"].values():
        if "uses" in job:  # a reusable workflow call
            found.append(job["uses"])
        found.extend(step["uses"] for step in job.get("steps", []) if "uses" in step)
    return found


def _steps_using(workflow: dict[Any, Any], action: str) -> list[dict[str, Any]]:
    return [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith(f"{action}@")
    ]


# ---------------------------------------------------------------- project metadata


def test_distribution_name_matches_the_package_constant() -> None:
    assert PROJECT["name"] == dvf.DISTRIBUTION == "dvf-framework"


def test_the_dvf_command_is_the_console_script() -> None:
    assert PROJECT["scripts"] == {"dvf": "dvf.cli.app:main"}


def test_version_comes_from_git_tags() -> None:
    assert "version" in PROJECT["dynamic"]
    assert PYPROJECT["tool"]["hatch"]["version"]["source"] == "vcs"


def test_the_all_extra_covers_every_other_extra() -> None:
    (requirement,) = (Requirement(text) for text in EXTRAS["all"])
    assert canonicalize_name(requirement.name) == PROJECT["name"]
    missing = set(EXTRAS) - {"all"} - requirement.extras
    assert not missing, f"add these extras to 'all' (open-source only): {sorted(missing)}"
    assert requirement.extras <= set(EXTRAS)


def test_no_extra_installs_a_proprietary_driver() -> None:
    for extra, requirements in EXTRAS.items():
        assert not _requirement_names(requirements) & PROPRIETARY, extra
    # Databricks Connect replaces pyspark, so it is installed by users instead.
    assert "databricks-connect" not in _requirement_names(
        [r for extra in EXTRAS.values() for r in extra if not r.startswith(PROJECT["name"])]
    )


# ---------------------------------------------------------------- containers


@pytest.mark.parametrize("variant", ["slim", "full"])
def test_dockerfile_is_locked_pinned_and_non_root(variant: str) -> None:
    text = (REPO_ROOT / "docker" / f"Dockerfile.{variant}").read_text("utf-8")
    base_images = re.findall(r"^FROM (\S+) AS \w+$", text, flags=re.MULTILINE)
    assert len(base_images) == 3  # uv, builder, runtime
    assert all(re.search(r":[\w.-]+@sha256:[0-9a-f]{64}$", image) for image in base_images)
    assert len(set(base_images[1:])) == 1  # builder and runtime share the Python image
    assert "uv sync --locked --no-dev" in text
    assert re.search(r"^USER 10001:0$", text, flags=re.MULTILINE)
    assert re.search(r"^WORKDIR /work$", text, flags=re.MULTILINE)
    assert '\nENTRYPOINT ["dvf"]\n' in text
    assert "STOPSIGNAL SIGINT" in text
    for label in ("title", "version", "revision", "source", "licenses"):
        assert f"org.opencontainers.image.{label}=" in text


def test_full_image_installs_the_open_source_extras() -> None:
    text = (REPO_ROOT / "docker" / "Dockerfile.full").read_text("utf-8")
    match = re.search(r'^ARG DVF_EXTRAS="([^"]+)"$', text, flags=re.MULTILINE)
    assert match
    extras = set(match.group(1).split())
    assert extras == FULL_IMAGE_EXTRAS
    assert extras <= set(EXTRAS)
    for extra in extras:
        assert not _requirement_names(EXTRAS[extra]) & NOT_IN_IMAGES


def test_slim_image_installs_no_extras() -> None:
    text = (REPO_ROOT / "docker" / "Dockerfile.slim").read_text("utf-8")
    assert "--extra" not in text
    assert "--all-extras" not in text


def test_dockerignore_admits_only_the_build_inputs() -> None:
    lines = [
        line.strip()
        for line in (REPO_ROOT / ".dockerignore").read_text("utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines[0] == "*"
    admitted = {line[1:] for line in lines if line.startswith("!")}
    assert admitted == {"pyproject.toml", "uv.lock", "README.md", "LICENSE", "NOTICE", "src/"}


# ---------------------------------------------------------------- release automation


def test_release_please_configuration() -> None:
    config = json.loads((REPO_ROOT / "release-please-config.json").read_text("utf-8"))
    manifest = json.loads((REPO_ROOT / ".release-please-manifest.json").read_text("utf-8"))
    assert config["release-type"] == "simple"  # hatch-vcs owns the version; no file to bump
    assert config["include-v-in-tag"] is True
    assert config["include-component-in-tag"] is False
    assert set(config["packages"]) == set(manifest) == {"."}
    assert config["packages"]["."]["package-name"] == PROJECT["name"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["."])


def test_release_workflow_uses_trusted_publishing_without_tokens() -> None:
    workflow = _load_yaml(WORKFLOWS / "release.yml")
    assert workflow["permissions"] == {}
    publish = workflow["jobs"]["publish-pypi"]
    assert publish["environment"]["name"] == "pypi"
    assert publish["permissions"] == {"id-token": "write", "attestations": "write"}
    (step,) = _steps_using(workflow, "pypa/gh-action-pypi-publish")
    assert not {"password", "user", "repository-url"} & set(step.get("with", {}))
    secrets = set(re.findall(r"secrets\.(\w+)", (WORKFLOWS / "release.yml").read_text("utf-8")))
    assert secrets <= {"GITHUB_TOKEN"}


def test_release_workflow_attests_distributions_and_images() -> None:
    workflow = _load_yaml(WORKFLOWS / "release.yml")
    attest = _steps_using(workflow, "actions/attest")
    subjects = {
        step["with"].get("subject-path") or step["with"].get("subject-name") for step in attest
    }
    assert "dist/*" in subjects
    assert any(str(subject).startswith("${{ steps.image.outputs.name") for subject in subjects)
    (push,) = _steps_using(workflow, "docker/build-push-action")
    assert push["with"]["platforms"] == "linux/amd64,linux/arm64"
    assert push["with"]["push"] is True


def test_every_release_job_declares_its_permissions() -> None:
    workflow = _load_yaml(WORKFLOWS / "release.yml")
    for name, job in workflow["jobs"].items():
        assert "permissions" in job, name


@pytest.mark.parametrize("name", ["release.yml", "docs.yml", "scorecard.yml"])
def test_actions_are_pinned_to_commit_shas(name: str) -> None:
    for uses in _uses(_load_yaml(WORKFLOWS / name)):
        assert uses.startswith("./") or SHA_PINNED.match(uses), f"{name}: {uses}"


@functools.cache
def _noxfile() -> ModuleType:
    """noxfile.py as a module, loaded once (nox warns when sessions register twice)."""
    spec = importlib.util.spec_from_file_location("dvf_noxfile", REPO_ROOT / "noxfile.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHANGELOG = """# Changelog

## [Unreleased]

- Nothing yet.

## [1.1.0](https://github.com/o/r/compare/v1.0.0...v1.1.0) (2026-12-01)

### Added

- A feature.

## [1.0.0] - 2026-11-20

- First release.
"""


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("1.1.0", "### Added\n\n- A feature.\n"),
        ("v1.0.0", "- First release.\n"),
        ("0.9.0", None),
        ("Unreleased", None),
    ],
)
def test_changelog_section_for_release_notes(version: str, expected: str | None) -> None:
    assert _noxfile().changelog_section(CHANGELOG, version) == expected


def test_noxfile_distribution_name() -> None:
    assert _noxfile().DISTRIBUTION == dvf.DISTRIBUTION


# ---------------------------------------------------------------- built distributions


@dataclass(frozen=True)
class Built:
    wheel: Path
    sdist: Path
    source_files: frozenset[str]  # package files present before the build, relative to src/


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Built:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    source_files = frozenset(
        path.relative_to(SRC.parent).as_posix()
        for path in SRC.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
        and path.name != "_version.py"
    )
    out = tmp_path_factory.mktemp("dist")
    subprocess.run(  # noqa: S603 - fixed arguments
        [uv, "build", "--out-dir", str(out)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        timeout=600,
    )
    (wheel,) = out.glob("*.whl")
    (sdist,) = out.glob("*.tar.gz")
    return Built(wheel, sdist, source_files)


def _wheel_text(built: Built, suffix: str) -> str:
    with zipfile.ZipFile(built.wheel) as archive:
        (name,) = (n for n in archive.namelist() if n.endswith(f".dist-info/{suffix}"))
        return archive.read(name).decode("utf-8")


def _metadata(built: Built) -> Message:
    return Parser().parsestr(_wheel_text(built, "METADATA"))


@pytest.mark.slow
def test_wheel_metadata(built: Built) -> None:
    meta = _metadata(built)
    assert meta["Metadata-Version"]
    assert meta["Name"] == "dvf-framework"
    Version(meta["Version"])  # a valid PEP 440 version
    assert meta["Requires-Python"] == ">=3.11"
    assert meta["License-Expression"] == "Apache-2.0"
    assert set(meta.get_all("License-File") or []) == {"LICENSE", "NOTICE"}
    assert meta["Description-Content-Type"] == "text/markdown"
    assert "dvf" in (meta.get_payload() or "")
    assert "Typing :: Typed" in (meta.get_all("Classifier") or [])


@pytest.mark.slow
def test_wheel_declares_the_extras_and_core_dependencies(built: Built) -> None:
    meta = _metadata(built)
    provided = {canonicalize_name(extra) for extra in meta.get_all("Provides-Extra") or []}
    assert provided == {canonicalize_name(extra) for extra in EXTRAS}
    requires = [Requirement(text) for text in meta.get_all("Requires-Dist") or []]
    core = {
        canonicalize_name(r.name)
        for r in requires
        if r.marker is None or "extra" not in str(r.marker)
    }
    assert _requirement_names(PROJECT["dependencies"]) <= core
    assert not {canonicalize_name(r.name) for r in requires} & PROPRIETARY


@pytest.mark.slow
def test_wheel_entry_point(built: Built) -> None:
    entry_points = _wheel_text(built, "entry_points.txt")
    section = entry_points.split("[console_scripts]", 1)[1].split("[", 1)[0]
    assert re.search(r"^dvf\s*=\s*dvf\.cli\.app:main$", section, flags=re.MULTILINE)


@pytest.mark.slow
def test_wheel_is_pure_python(built: Built) -> None:
    wheel = _wheel_text(built, "WHEEL")
    assert "Root-Is-Purelib: true" in wheel
    assert "Tag: py3-none-any" in wheel


@pytest.mark.slow
def test_wheel_contains_every_package_file(built: Built) -> None:
    with zipfile.ZipFile(built.wheel) as archive:
        names = set(archive.namelist())
    missing = built.source_files - names
    assert not missing, f"not packaged: {sorted(missing)[:10]}"
    # Package data that must never go missing.
    assert "dvf/py.typed" in names
    assert "dvf/reporting/templates/report.html.j2" in names
    sap_templates = {n for n in names if re.fullmatch(r"dvf/packs/sap/templates/[\w-]+\.yaml", n)}
    assert len(sap_templates) >= 4


@pytest.mark.slow
def test_wheel_has_no_stray_files(built: Built) -> None:
    with zipfile.ZipFile(built.wheel) as archive:
        names = archive.namelist()
    top_level = {name.split("/", 1)[0] for name in names}
    (dist_info,) = top_level - {"dvf"}
    assert dist_info.startswith("dvf_framework-") and dist_info.endswith(".dist-info")
    assert not [n for n in names if "__pycache__" in n or n.endswith((".pyc", ".pyo"))]


@pytest.mark.slow
def test_sdist_contents(built: Built) -> None:
    with tarfile.open(built.sdist) as archive:
        names = {name.split("/", 1)[1] for name in archive.getnames() if "/" in name}
    for required in (
        "PKG-INFO",
        "pyproject.toml",
        "README.md",
        "CHANGELOG.md",
        "LICENSE",
        "NOTICE",
        "src/dvf/py.typed",
        "src/dvf/reporting/templates/report.html.j2",
        "src/dvf/cli/app.py",
    ):
        assert required in names, required
    assert not [n for n in names if n.startswith((".venv/", ".dvf/", "reports/", ".git/"))]
