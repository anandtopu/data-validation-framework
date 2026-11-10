"""Run dvf after ``dbt build``, validating the models dbt built.

    python run_after_dbt_build.py --project-dir path/to/dbt_project
    python run_after_dbt_build.py --project-dir . --skip-dbt     # dbt already ran

Steps:

1. ``dbt build`` in the project (unless ``--skip-dbt``; needs dbt on PATH).
2. If the dvf configuration does not exist yet, generate it from ``target/manifest.json``
   (``dvf dbt import``). Review it once and commit it; later runs reuse it.
3. Run dvf on the models that ``target/run_results.json`` lists as built successfully.
4. Exit with dvf's exit code: 0 passed, 1 quality gates failed, 2 execution errors.

Try it without dbt on the synthetic artifacts in this folder:

    uv run python demo_warehouse.py
    uv run python run_after_dbt_build.py --project-dir . --skip-dbt
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from dvf.integrations.common import run_validation
from dvf.integrations.dbt import built_model_selectors, import_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Run dvf after dbt build.")
    parser.add_argument("--project-dir", type=Path, default=Path())
    parser.add_argument("--config", type=Path, help="dvf configuration (default: dvf.yaml)")
    parser.add_argument("--skip-dbt", action="store_true", help="Do not run dbt build")
    parser.add_argument("--fail-on", default="error")
    parser.add_argument("--min-score", type=float)
    args = parser.parse_args()
    project = args.project_dir.resolve()
    config = args.config or project / "dvf.yaml"
    target = project / "target"

    if not args.skip_dbt:
        dbt = shutil.which("dbt")
        if dbt is None:
            sys.stderr.write("dbt is not on PATH; install dbt or pass --skip-dbt\n")
            return 3
        # dbt's own test failures should not stop dvf from reporting, so the exit code is only
        # shown; run_results.json says which models were built.
        built = subprocess.run([dbt, "build", "--project-dir", str(project)], check=False)  # noqa: S603
        sys.stdout.write(f"dbt build exited with {built.returncode}\n")

    if not config.exists():
        result = import_manifest(target / "manifest.json", output=config)
        sys.stdout.write(f"generated {config} ({result.models} models, {result.checks} checks)\n")

    run_results = target / "run_results.json"
    selectors = built_model_selectors(run_results) if run_results.exists() else []
    if run_results.exists() and not selectors:
        sys.stdout.write("dbt built no models; nothing to validate\n")
        return 0
    _, summary = run_validation(
        config,
        only=selectors,
        output_dir=project / "dvf-reports",
        fail_on=args.fail_on,
        min_score=args.min_score,
    )
    sys.stdout.write(summary.message() + "\n")
    if summary.report_dir:
        sys.stdout.write(f"reports: {summary.report_dir}\n")
    return summary.exit_code


if __name__ == "__main__":
    sys.exit(main())
