"""AWS Glue job script: run dvf on a project stored in S3 and upload the reports to S3.

Job arguments (see glue_job.json): ``--project_s3_uri s3://bucket/dvf-project/`` (or
``--config_s3_uri`` for a single file), ``--config dvf.yaml``, ``--reports_s3_uri
s3://bucket/dvf-reports/``, and optionally ``--profile``, ``--only tag:nightly``,
``--fail_on error``, ``--min_score 95``. Install dvf with the job argument
``--additional-python-modules dvf-framework==1.0.0`` (plus extras for your connections).

The job fails when the quality gates fail, after the reports are uploaded. dvf needs Python
3.11 or later: use a Glue 5.0 job. Not verified on AWS.
"""

from __future__ import annotations

from dvf.integrations.aws import glue_main

glue_main()
