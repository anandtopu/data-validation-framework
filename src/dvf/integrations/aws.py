"""AWS Glue: a job entry point that reads the configuration from S3 and writes reports to S3.

The job script (see ``examples/orchestrators/aws/glue_dvf_job.py``) is two lines::

    from dvf.integrations.aws import glue_main
    glue_main()

Job arguments (Glue passes them as ``--name value``; unknown arguments are ignored):

=================== =============================================================================
``--project_s3_uri`` ``s3://bucket/prefix/``: a project folder (configuration, data, plugins),
                     downloaded to a temporary folder
``--config``         configuration file, relative to the project folder (default ``dvf.yaml``)
``--config_s3_uri``  ``s3://bucket/key.yaml``: a single configuration file instead of a project
``--reports_s3_uri`` ``s3://bucket/prefix/``: reports are uploaded to ``<prefix>/<run_id>/``
``--profile``        environment profile
``--only``           selectors, separated by commas (``tag:pr,table:orders``)
``--fail_on``        ``info``, ``warning``, ``error`` (default), or ``critical``
``--min_score``      minimum quality score
=================== =============================================================================

The job fails (raises :class:`~dvf.integrations.common.QualityGateError`) when the run fails its
gates, after the reports are uploaded; a Step Functions ``glue:startJobRun.sync`` state then
fails too. ``boto3`` is preinstalled in Glue. dvf needs Python 3.11 or later, so use a Glue job
type that runs it (Glue 5.0 Spark jobs do). This module is verified by unit tests with a
stand-in S3 client, not on AWS.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dvf.core.enums import Severity
from dvf.core.errors import ConfigError
from dvf.integrations.common import DvfRunSummary, enforce_gate, run_validation

log = logging.getLogger(__name__)


def split_s3_uri(uri: str) -> tuple[str, str]:
    """``s3://bucket/key`` as ``(bucket, key)``."""
    parts = urlsplit(uri)
    if parts.scheme != "s3" or not parts.netloc:
        raise ConfigError(f"Expected an s3://bucket/key URI, got '{uri}'.")
    return parts.netloc, parts.path.lstrip("/")


def _client(client: Any) -> Any:
    if client is not None:
        return client
    try:
        boto3 = importlib.import_module("boto3")
    except ImportError as exc:
        raise ConfigError(
            "Reading from and writing to S3 needs boto3.",
            hint="pip install 'dvf-framework[aws]' (boto3 is preinstalled in AWS Glue).",
        ) from exc
    return boto3.client("s3")


def download_prefix(uri: str, folder: Path, *, client: Any = None) -> int:
    """Download every object under ``uri`` into ``folder``, keeping relative paths."""
    s3 = _client(client)
    bucket, prefix = split_s3_uri(uri)
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    count = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            key = str(item["Key"])
            relative = key[len(prefix) :]
            if not relative or relative.endswith("/"):
                continue
            target = (folder / relative).resolve()
            if not target.is_relative_to(folder.resolve()):
                raise ConfigError(f"S3 key '{key}' would be written outside {folder}.")
            target.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, key, str(target))
            count += 1
    return count


def upload_folder(folder: Path, uri: str, *, client: Any = None) -> list[str]:
    """Upload every file under ``folder`` to ``uri``; return the S3 URIs written."""
    s3 = _client(client)
    bucket, prefix = split_s3_uri(uri)
    prefix = prefix.rstrip("/")
    written: list[str] = []
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        key = "/".join(filter(None, [prefix, path.relative_to(folder).as_posix()]))
        s3.upload_file(str(path), bucket, key)
        written.append(f"s3://{bucket}/{key}")
    return written


def parser() -> argparse.ArgumentParser:
    """The job arguments of :func:`glue_main`."""
    p = argparse.ArgumentParser(prog="dvf-glue", description="Run dvf in an AWS Glue job.")
    p.add_argument("--project_s3_uri")
    p.add_argument("--config", default="dvf.yaml")
    p.add_argument("--config_s3_uri")
    p.add_argument("--reports_s3_uri")
    p.add_argument("--profile")
    p.add_argument("--only", default="")
    p.add_argument("--fail_on", default="error", choices=[s.value for s in Severity])
    p.add_argument("--min_score", type=float)
    return p


def glue_main(argv: Sequence[str] | None = None, *, client: Any = None) -> DvfRunSummary:
    """Run a configuration from S3, upload the reports, and fail the job by the gates.

    The returned summary's ``report_dir``, ``results_path``, and ``reports`` are S3 URIs (the
    local copies are deleted with the temporary folder), or empty without ``--reports_s3_uri``.
    """
    options, _ = parser().parse_known_args(list(sys.argv[1:] if argv is None else argv))
    if not options.project_s3_uri and not options.config_s3_uri:
        raise ConfigError("Give --project_s3_uri (a project folder) or --config_s3_uri (a file).")
    s3 = _client(client)
    with tempfile.TemporaryDirectory(prefix="dvf-glue-") as tmp:
        work = Path(tmp)
        project = work / "project"
        project.mkdir()
        if options.project_s3_uri:
            count = download_prefix(options.project_s3_uri, project, client=s3)
            log.info("downloaded %d file(s) from %s", count, options.project_s3_uri)
            config = project / options.config
        else:
            bucket, key = split_s3_uri(options.config_s3_uri)
            config = project / Path(key).name
            s3.download_file(bucket, key, str(config))
        if not config.is_file():
            raise ConfigError(f"The configuration {options.config} was not found in the project.")
        _, summary = run_validation(
            config,
            profile=options.profile,
            only=options.only,
            output_dir=work / "reports",
            history=False,
            fail_on=options.fail_on,
            min_score=options.min_score,
        )
        sys.stdout.write(summary.message() + "\n")
        local = summary.report_dir
        summary = replace(summary, report_dir=None, results_path=None, reports=())
        if options.reports_s3_uri and local is not None and Path(local).is_dir():
            target = options.reports_s3_uri.rstrip("/") + "/" + summary.run_id
            written = upload_folder(Path(local), target, client=s3)
            sys.stdout.write(f"uploaded {len(written)} report file(s) to {target}/\n")
            results = f"{target}/results.json"
            summary = replace(
                summary,
                report_dir=target,
                results_path=results if results in written else None,
                reports=tuple(written),
            )
    return enforce_gate(summary)


__all__ = ["download_prefix", "glue_main", "parser", "split_s3_uri", "upload_folder"]
