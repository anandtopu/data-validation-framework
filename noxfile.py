"""Development tasks. Run ``uv run nox`` for the default sessions (lint, typecheck, tests)."""

from __future__ import annotations

from pathlib import Path

import nox

nox.options.sessions = ["lint", "typecheck", "tests"]

PYTHONS = ["3.11", "3.12", "3.13", "3.14"]


@nox.session(venv_backend="none")
def lint(session: nox.Session) -> None:
    """Ruff lint and format check."""
    session.run("uv", "run", "ruff", "check", ".")
    session.run("uv", "run", "ruff", "format", "--check", ".")


@nox.session(venv_backend="none")
def fmt(session: nox.Session) -> None:
    """Apply Ruff fixes and formatting."""
    session.run("uv", "run", "ruff", "check", "--fix", ".")
    session.run("uv", "run", "ruff", "format", ".")


@nox.session(venv_backend="none")
def typecheck(session: nox.Session) -> None:
    """mypy in strict mode."""
    session.run("uv", "run", "mypy")


@nox.session(venv_backend="none")
def tests(session: nox.Session) -> None:
    """pytest with coverage on the project environment's Python."""
    session.run("uv", "run", "pytest", "--cov", "--cov-report=term-missing", *session.posargs)


@nox.session(venv_backend="none")
def tests_all(session: nox.Session) -> None:
    """pytest on every supported Python, each in an isolated environment."""
    for python in PYTHONS:
        session.run("uv", "run", "--isolated", "--python", python, "pytest", *session.posargs)


@nox.session(venv_backend="none")
def integration(session: nox.Session) -> None:
    """Integration tests (PostgreSQL). Set DVF_TEST_POSTGRES_URL, or have Docker available."""
    session.run("uv", "run", "pytest", "-m", "integration", "-v", *session.posargs)


@nox.session(venv_backend="none")
def example(session: nox.Session) -> None:
    """Scaffold the seeded example in a temporary folder and run it end to end."""
    import tempfile

    folder = tempfile.mkdtemp(prefix="dvf-example-")
    session.run("uv", "run", "dvf", "init", f"{folder}/demo", "--example")
    session.run("uv", "run", "dvf", "run", f"{folder}/demo/dvf.yaml", success_codes=[1])


@nox.session(venv_backend="none")
def benchmark(session: nox.Session) -> None:
    """Performance benchmarks (10 million rows by default; set DVF_BENCH_ROWS to change it).

    Fails if a benchmark misses its target or is more than 15% slower than the stored baseline
    (``.benchmarks/baseline.json``); pass ``-- --update`` to record a new baseline.
    """
    update = "--update" in session.posargs
    args = [a for a in session.posargs if a != "--update"]
    session.env["DVF_BENCHMARK"] = "1"
    results = Path(".benchmarks") / "results.json"
    results.unlink(missing_ok=True)
    session.run("uv", "run", "pytest", "tests/benchmarks", "-v", "--no-cov", *args)
    compare = [
        "uv",
        "run",
        "python",
        "tests/benchmarks/compare.py",
        str(results),
        ".benchmarks/baseline.json",
    ]
    session.run(*compare, *(["--update"] if update else []))


@nox.session(venv_backend="none")
def schemas(session: nox.Session) -> None:
    """Regenerate the committed JSON Schemas."""
    session.run("uv", "run", "dvf", "schema", "config", "--output", "schemas/config.schema.json")
    session.run("uv", "run", "dvf", "schema", "results", "--output", "schemas/results.schema.json")


@nox.session(venv_backend="none")
def docs(session: nox.Session) -> None:
    """Build the documentation site in strict mode."""
    session.run("uv", "run", "--group", "docs", "mkdocs", "build", "--strict")


# ---------------------------------------------------------------- release, packaging, containers
# These sessions need only uv, plus Docker or Podman for `docker`. The workflows run them with
# `uvx nox@<version> -s <session>`, so they import nothing beyond nox and the standard library
# and never rely on the project environment being synced.

DISTRIBUTION = "dvf-framework"
TWINE = "twine@7.0.0"
PIP_AUDIT = "pip-audit@2.10.1"
HATCHLING = ["uvx", "--from", "hatchling>=1.27", "--with", "hatch-vcs>=0.4", "hatchling"]
QUICKSTART_BUDGET_S = 300.0
IMAGE_VARIANTS = ("slim", "full")


def _last_line(output: object) -> str:
    lines = [line.strip() for line in str(output or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _project_version(session: nox.Session) -> str:
    """The version hatch-vcs derives from git, exactly as a build would stamp it."""
    return _last_line(session.run(*HATCHLING, "version", silent=True))


def changelog_section(changelog: str, version: str) -> str | None:
    """Return the body of one version's section in CHANGELOG.md, or None if there is none.

    Accepts Keep a Changelog headings (``## [1.0.0] - 2026-11-20``) and release-please headings
    (``## [1.0.0](https://...) (2026-11-20)`` or ``## 1.0.0 (2026-11-20)``). A section ends at
    the next level-2 heading.
    """
    import re

    wanted = version.removeprefix("v")
    heading = re.compile(r"^##\s+\[?v?(?P<version>[0-9][^\]\s(]*)")
    body: list[str] | None = None
    for line in changelog.splitlines():
        if line.startswith("## "):
            if body is not None:
                break
            match = heading.match(line)
            if match and match.group("version") == wanted:
                body = []
            continue
        if body is not None:
            body.append(line)
    if body is None:
        return None
    return "\n".join(body).strip() + "\n"


@nox.session(venv_backend="none")
def dist(session: nox.Session) -> None:
    """Build the sdist and wheel into dist/ with uv, then check them with twine --strict."""
    import shutil

    shutil.rmtree("dist", ignore_errors=True)
    session.run("uv", "build", "--out-dir", "dist")
    files = sorted(str(p) for p in Path("dist").iterdir() if p.name.endswith((".whl", ".tar.gz")))
    session.run("uvx", TWINE, "check", "--strict", *files)


@nox.session(venv_backend="none")
def quickstart(session: nox.Session) -> None:
    """Acceptance check for a first-time user, timed (budget: 5 minutes).

    Builds the wheel (standing in for PyPI), creates a fresh virtual environment with uv,
    installs the wheel with its dependencies from PyPI, runs ``dvf init <tmp>/demo --example``
    and ``dvf run``, and checks that report.html was written. The uv cache starts empty, so
    dependencies are downloaded as on a first install; pass ``-- --warm`` to reuse the cache
    and ``-- --keep`` to keep the folder. DVF_QUICKSTART_PYTHON selects the Python version
    (default: the one running nox).
    """
    import os
    import shutil
    import sys
    import tempfile
    import time

    warm = "--warm" in session.posargs
    keep = "--keep" in session.posargs
    root = Path(tempfile.mkdtemp(prefix="dvf-quickstart-"))
    env = {} if warm else {"UV_CACHE_DIR": str(root / "uv-cache")}
    python = os.environ.get("DVF_QUICKSTART_PYTHON", f"{sys.version_info[0]}.{sys.version_info[1]}")
    timings: list[tuple[str, float]] = []

    def step(name: str, *args: str, **kwargs: object) -> None:
        started = time.perf_counter()
        session.run(*args, **kwargs)
        timings.append((name, time.perf_counter() - started))

    try:
        step("build the wheel", "uv", "build", "--wheel", "--out-dir", str(root / "dist"))
        wheel = next((root / "dist").glob("*.whl"))
        venv = root / "venv"
        step("create a virtual environment", "uv", "venv", "--python", python, str(venv), env=env)
        step("install dvf", "uv", "pip", "install", "--python", str(venv), str(wheel), env=env)
        scripts = venv / ("Scripts" if os.name == "nt" else "bin")
        dvf = str(scripts / ("dvf.exe" if os.name == "nt" else "dvf"))
        demo = root / "demo"
        step("dvf --version", dvf, "--version")
        step("dvf init --example", dvf, "init", str(demo), "--example")
        # Exit code 1 is the expected outcome: the example has seeded defects.
        step("dvf run", dvf, "run", str(demo / "dvf.yaml"), success_codes=[1])
        reports = sorted(demo.glob("reports/**/report.html"))
        if not reports:
            session.error(f"dvf run wrote no report.html under {demo / 'reports'}")
        session.log(f"report: {reports[-1]}")
    finally:
        if not keep:
            shutil.rmtree(root, ignore_errors=True)

    user_steps = timings[1:]  # a user installs from PyPI and never builds the wheel
    for name, seconds in timings:
        session.log(f"{seconds:7.1f} s  {name}")
    total = sum(seconds for _, seconds in user_steps)
    cache = "warm" if warm else "cold"
    session.log(f"{total:7.1f} s  from a fresh environment to a report ({cache} uv cache)")
    if total > QUICKSTART_BUDGET_S:
        session.error(
            f"the quickstart took {total:.0f} s; the budget is {QUICKSTART_BUDGET_S:.0f} s"
        )


@nox.session(venv_backend="none")
def docker(session: nox.Session) -> None:
    """Build the container images and smoke-test them.

    For each variant (``-- slim`` or ``-- full``; default both): build docker/Dockerfile.<variant>
    as dvf-local:<variant>, check ``dvf --version``, then run ``dvf init /work/demo --example``
    and ``dvf run`` in a mounted folder and expect exit code 1 and a report.html.

    Uses docker, or podman if docker is not installed; DVF_CONTAINER_ENGINE overrides the
    choice. DVF_CONTAINER_NETWORK (for example ``host``) is passed to build and run, and
    DVF_VERSION overrides the version stamped into the image (default: from git).
    """
    import os
    import shutil
    import sys
    import tempfile

    engine = os.environ.get("DVF_CONTAINER_ENGINE") or next(
        (name for name in ("docker", "podman") if shutil.which(name)), None
    )
    if engine is None:
        session.error("Neither docker nor podman is installed (or set DVF_CONTAINER_ENGINE).")
    variants = [arg for arg in session.posargs if arg in IMAGE_VARIANTS] or list(IMAGE_VARIANTS)
    network = os.environ.get("DVF_CONTAINER_NETWORK")
    net = [f"--network={network}"] if network else []
    version = os.environ.get("DVF_VERSION") or _project_version(session)
    revision = _last_line(session.run("git", "rev-parse", "HEAD", silent=True))
    created = _last_line(session.run("git", "log", "-1", "--format=%cI", silent=True))
    # On Linux hosts, run as the calling user so files written to the mount belong to them.
    # The image supports any UID; rootless podman also needs --userns=keep-id for that mapping.
    user: list[str] = []
    if sys.platform.startswith("linux"):
        user = [f"--user={os.getuid()}:{os.getgid()}"]
        if Path(engine).name.startswith("podman"):
            user.append("--userns=keep-id")

    for variant in variants:
        tag = f"dvf-local:{variant}"
        session.run(
            engine, "build", *net,
            "--file", f"docker/Dockerfile.{variant}",
            "--build-arg", f"DVF_VERSION={version}",
            "--build-arg", f"VCS_REF={revision}",
            "--build-arg", f"BUILD_DATE={created}",
            "--build-arg", "SOURCE_URL=",
            "--tag", tag, ".",
        )  # fmt: skip
        reported = _last_line(
            session.run(engine, "run", "--rm", *net, tag, "--version", silent=True)
        )
        if reported != f"dvf {version}":
            session.error(f"{tag} reports {reported!r}, expected 'dvf {version}'")
        folder = Path(tempfile.mkdtemp(prefix=f"dvf-docker-{variant}-"))
        try:
            mount = ["--mount", f"type=bind,source={folder},target=/work"]
            run = [engine, "run", "--rm", *net, *user, *mount, tag]
            session.run(*run, "init", "/work/demo", "--example")
            session.run(*run, "run", "/work/demo/dvf.yaml", success_codes=[1])
            reports = sorted(folder.glob("demo/reports/**/report.html"))
            if not reports:
                session.error(f"{tag}: dvf run wrote no report.html in the mounted folder")
            session.log(f"{tag}: dvf {version}, seeded example ran, report written")
        finally:
            shutil.rmtree(folder, ignore_errors=True)


@nox.session(venv_backend="none")
def sbom(session: nox.Session) -> None:
    """CycloneDX 1.5 SBOMs of the locked dependencies, from uv.lock.

    Writes <output-dir>/dvf-framework-<version>.cdx.json (runtime dependencies) and
    dvf-framework-<version>-all-extras.cdx.json (with every optional extra). Options after
    ``--``: ``--version 1.0.0`` (default: the version from git) and ``--output-dir``
    (default: build/sbom).
    """
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="nox -s sbom --")
    parser.add_argument("--version", default=None)
    parser.add_argument("--output-dir", default="build/sbom")
    options = parser.parse_args(session.posargs)
    version = (options.version or _project_version(session)).removeprefix("v")
    output = Path(options.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    for suffix, extras in (("", []), ("-all-extras", ["--all-extras"])):
        target = output / f"{DISTRIBUTION}-{version}{suffix}.cdx.json"
        session.run(
            "uv", "export", "--frozen", "--no-dev", "--format", "cyclonedx1.5", "--quiet",
            *extras, "--output-file", str(target),
        )  # fmt: skip
        bom = json.loads(target.read_text(encoding="utf-8"))
        # uv leaves the project's own version out, because it is dynamic (from git).
        component = bom.setdefault("metadata", {}).setdefault("component", {})
        component.update(
            {
                "name": DISTRIBUTION,
                "version": version,
                "purl": f"pkg:pypi/{DISTRIBUTION}@{version.replace('+', '%2B')}",
                "licenses": [{"license": {"id": "Apache-2.0"}}],
            }
        )
        target.write_text(json.dumps(bom, indent=2) + "\n", encoding="utf-8")
        session.log(f"wrote {target} ({len(bom.get('components', []))} components)")


@nox.session(venv_backend="none")
def audit(session: nox.Session) -> None:
    """pip-audit of the locked dependencies: runtime with every extra, then everything.

    Exports uv.lock to hashed requirements files and audits them without installing anything.
    Options after ``--`` go to pip-audit, for example ``-- --ignore-vuln GHSA-xxxx-xxxx-xxxx``.
    """
    import shutil
    import tempfile

    folder = Path(tempfile.mkdtemp(prefix="dvf-audit-"))
    try:
        for name, selection in (
            ("the runtime dependencies with every extra", ["--no-dev", "--all-extras"]),
            ("every extra and dependency group", ["--all-groups", "--all-extras"]),
        ):
            requirements = folder / "requirements.txt"
            session.run(
                "uv", "export", "--frozen", "--format", "requirements.txt", "--no-emit-project",
                "--quiet", *selection, "--output-file", str(requirements),
            )  # fmt: skip
            session.log(f"auditing {name}")
            session.run(
                "uvx", PIP_AUDIT, "--disable-pip", "--require-hashes", "--progress-spinner=off",
                "--cache-dir", str(folder / "cache"), "--requirement", str(requirements),
                *session.posargs,
            )  # fmt: skip
    finally:
        shutil.rmtree(folder, ignore_errors=True)


@nox.session(venv_backend="none")
def release_notes(session: nox.Session) -> None:
    """Write one version's CHANGELOG.md section to a file: ``-- 1.0.0 notes.md``.

    The release workflow uses it for the GitHub Release text. Fails if there is no section.
    """
    if len(session.posargs) != 2:
        session.error("usage: nox -s release_notes -- <version> <output-file>")
    version, output = session.posargs
    section = changelog_section(Path("CHANGELOG.md").read_text(encoding="utf-8"), version)
    if section is None:
        session.error(f"CHANGELOG.md has no section for {version}")
    Path(output).write_text(section, encoding="utf-8")
    session.log(f"wrote the notes for {version} to {output}")


# ---------------------------------------------------------------- documentation reference pages


@nox.session(venv_backend="none")
def docs_reference(session: nox.Session) -> None:
    """Regenerate the reference pages (configuration, components, results, CLI) in docs/.

    They are generated from the configuration models, the component registry, and the Typer
    application; tests/unit/test_docs_reference.py fails when a committed page is stale. Pass
    ``-- --check`` to only check.
    """
    session.run("uv", "run", "python", "-m", "dvf.docsgen", *session.posargs)
