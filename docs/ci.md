# CI/CD

dvf runs in any CI system that has Python 3.11 or later: install `dvf-framework`, run `dvf run`, publish the reports, and let the exit code decide. This page covers the GitHub Action, the templates for GitLab CI, Azure DevOps, Jenkins, and GitHub Actions, and the PR and nightly modes they share.

| File | For |
|---|---|
| `action.yml` (repository root) | The dvf GitHub Action |
| `ci-templates/github-workflow.yml` | A GitHub Actions workflow using the action |
| `ci-templates/gitlab-ci.yml` | GitLab CI/CD |
| `ci-templates/azure-pipelines.yml` | Azure DevOps Pipelines |
| `ci-templates/Jenkinsfile` | Jenkins (declarative pipeline) |
| `examples/ci/dvf.yaml` | A configuration with a PR suite and a nightly suite |

!!! note "What is verified"
    The action's scripts and the scripts of the GitLab and Azure DevOps templates were run locally in Git Bash against `examples/ci/dvf.yaml`, and `.github/workflows/action-selftest.yml` runs the action on Linux and Windows runners with dvf installed from the checkout. The templates have not been run on GitLab, Azure DevOps, or Jenkins themselves: dvf's unit tests check that they parse and contain the publishing steps and both modes.

## Exit codes and quality gates

`dvf run` ends with one of these exit codes, and every template fails the job on any code but 0:

| Code | Meaning |
|---|---|
| `0` | Passed (warnings allowed unless `--strict`) |
| `1` | Quality gates failed: a check at or above `--fail-on` failed, or the quality score is below `--min-score` |
| `2` | Execution error (connection, query, or timeout), an errored check, or an interrupted run |
| `3` | Configuration or usage error |

The gates:

- `--fail-on` (default `error`): the lowest severity of a failed check that fails the run. `--fail-on critical` tolerates failed `error` checks; `--fail-on warning` fails on failed `warning` checks too.
- `--min-score`: the lowest acceptable quality score, the share of evaluated checks that passed (0 to 100).
- `--strict`: warnings fail the run as well.

## PR mode and nightly mode

Pull requests need fast feedback; full row-level comparisons belong in a nightly run. dvf selects the checks of each mode by tag with `--only tag:...`. Selectors match tags on suites, tables, and individual checks; several `--only` of the same kind are alternatives, and different kinds must all match.

| Mode | Runs | Selector in the templates |
|---|---|---|
| PR | Row counts, control totals (aggregates), schema checks, and cheap data quality checks | `tag:pr` |
| Nightly | Everything, including `row_hash`, `hash_bucket`, `full_diff`, and referential integrity | `tag:nightly` |

`examples/ci/dvf.yaml` shows one way to arrange a configuration: a `fast` suite tagged `[pr, nightly]` whose tables compare with `strategies: [row_count, aggregate]` and `on_mismatch: null` (no rows leave the databases), and a `full` suite tagged `[nightly]` with the row-level strategies.

```bash
dvf run examples/ci/dvf.yaml --only tag:pr        # PR mode
dvf run examples/ci/dvf.yaml --only tag:nightly   # nightly mode
```

Other ways to scope PR mode:

- Tag single checks: `- {type: not_null, columns: [id], tags: [pr]}` joins PR mode even in a nightly-only table.
- Combine kinds: `--only tag:pr --only check:compare.row_count --only check:compare.aggregate` runs only counts and control totals of the tables tagged `pr`.
- Give the PR job a profile (`--profile ci`) whose connections point at a smaller copy of the data.

The example's data has seeded defects, so both modes exit with 1.

## Publishing results

Every run writes `results.json` and, as requested with `--format`, `junit.xml` (one test case per check, for CI test tabs), `summary.md` (a Markdown summary below 60,000 characters, for job summaries and pull request comments), and `report.html`. The templates write them to a fixed folder (`--output-dir dvf-reports`) so later steps know where they are.

| System | JUnit | Markdown summary | Reports |
|---|---|---|---|
| GitHub Actions | optional check run (`mikepenz/action-junit-report`) | job summary; optional pull request comment (`gh pr comment`) | workflow artifact |
| GitLab | merge request test report (`artifacts:reports:junit`) | job log; optional merge request note (`dvf:mr-comment`) | job artifact, exposed in the merge request as "dvf report" |
| Azure DevOps | Tests tab (`PublishTestResults@2`) | run summary (`##vso[task.uploadsummary]`) | pipeline artifact |
| Jenkins | JUnit plugin (`junit`) | build log; build description shows the quality score | archived artifacts; optional HTML Publisher |

## The GitHub Action

```yaml
- uses: actions/checkout@v7
- uses: anandtopu/data-validation-framework@v1
  id: dvf
  with:
    config: dvf.yaml
    only: tag:pr
    extras: postgres
    fail-on: error
    min-score: "95"
  env:
    WAREHOUSE_PASSWORD: ${{ secrets.WAREHOUSE_PASSWORD }}
```

`@v1` follows the newest 1.x release; pin a release tag (`@v1.0.0`) or a commit SHA when runs must be reproducible. The action:

1. sets up Python (`python-version`, default 3.12) and installs `dvf-framework` with the `extras` into a virtual environment, with uv (default) or pip;
2. runs `dvf run` with the inputs below, always adding `--output-dir` and, for the job summary, `--format markdown`;
3. appends `summary.md` to the job summary and annotates up to 10 failed or errored checks;
4. sets the outputs and uploads the report folder as an artifact;
5. fails the step when dvf exited with a non-zero code (unless `fail-step: false`).

### Inputs

| Input | Default | Meaning |
|---|---|---|
| `config` | `dvf.yaml` | Configuration file |
| `profile` | | Environment profile |
| `only` | | Selectors, separated by spaces, commas, or new lines (`tag:pr`) |
| `fail-on` | `error` | Gate severity: `info`, `warning`, `error`, or `critical` |
| `min-score` | | Minimum quality score |
| `strict` | `false` | Warnings fail the run |
| `formats` | `html junit markdown` | Report formats besides JSON |
| `output-dir` | `dvf-reports` | Report folder |
| `args` | | More `dvf run` arguments, split on spaces, for example `--parallel 4 --set defaults.sample_size=10` |
| `extras` | | Extras, separated by commas, for example `postgres,excel` |
| `version` | | `1.2.3`, a specifier such as `>=1.2,<2`, or `latest`; empty installs the version of the action's tag (`v1.2.3` installs 1.2.3, `v1` the newest 1.x, a branch or SHA the latest release) |
| `install-from` | | Install from a local path instead of PyPI (self-tests) |
| `installer` | `uv` | `uv` or `pip` |
| `python-version` | `3.12` | Python for dvf (3.11 or later) |
| `setup-python` | `true` | Set up Python with `actions/setup-python`; `false` uses the runner's `python` |
| `step-summary` | `true` | Append `summary.md` to the job summary |
| `annotations` | `true` | Annotate failed and errored checks |
| `upload-artifact` | `true` | Upload the report folder |
| `artifact-name` | `dvf-reports` | Artifact name (make it unique per job in a matrix) |
| `retention-days` | | Artifact retention |
| `fail-step` | `true` | Fail the step on a non-zero exit code |
| `working-directory` | `.` | Folder to run in |

### Outputs

| Output | Meaning |
|---|---|
| `exit-code` | `dvf run`'s exit code |
| `passed` | `true` when the exit code is 0 |
| `status` | Run status: `passed`, `warning`, `failed`, `error`, or `skipped` |
| `quality-score` | Quality score, empty when no check was evaluated |
| `run-id` | dvf run ID |
| `results-path` | Path of `results.json` |
| `report-dir` | Report folder |
| `dvf-version` | Installed dvf version |

With `fail-step: false` the job continues and later steps can decide, for example `if: steps.dvf.outputs.quality-score < 90`.

The action runs on Linux, macOS, and Windows runners (its scripts use bash, which Windows runners provide).

## Adapting the templates

Each template starts with a block of variables:

| Variable | Default | Meaning |
|---|---|---|
| `DVF_VERSION` | `1.0.0` | dvf version from PyPI (pin it; upgrade deliberately) |
| `DVF_EXTRAS` | `postgres` | Extras for your connections |
| `DVF_CONFIG` | `dvf.yaml` | Configuration file |
| `DVF_PROFILE` | | Environment profile |
| `DVF_PR_ONLY`, `DVF_NIGHTLY_ONLY` | `tag:pr`, `tag:nightly` | Selectors of the two modes, separated by spaces; empty runs everything |
| `DVF_FAIL_ON`, `DVF_MIN_SCORE` | `error`, none | Quality gates |
| `DVF_EXTRA_ARGS` | | More `dvf run` arguments |
| `DVF_REPORT_DIR` | `dvf-reports` | Report folder |

Then:

1. **Install.** The templates use `python:3.12-slim` images or the CI system's Python task. To use the dvf container image instead, run the job in it and drop the install step. Proprietary drivers (for example Oracle Instant Client or SAP libraries) must be added to the image or agent.
2. **Credentials.** Store them in the CI system's secret store (masked GitLab variables, Azure DevOps variable groups linked to Key Vault, Jenkins credentials, GitHub secrets), expose them as environment variables, and reference them from the configuration with `${secret:env:NAME}`. `dvf validate-config` does not need them, so it can run in any job.
3. **Modes.**
    - **GitLab**: `dvf:pr` runs in merge request pipelines and `dvf:nightly` in scheduled pipelines; create the schedule under Build > Pipeline schedules. `DVF_MODE=pr` or `nightly` runs a mode by hand.
    - **Azure DevOps**: pull requests run PR mode (add the pipeline as a build validation policy) and the `schedules` cron runs nightly mode; the `mode` parameter chooses by hand.
    - **Jenkins**: timer-triggered builds run nightly mode and other builds PR mode; the `DVF_MODE` parameter chooses by hand. `DVF_GATE_RESULT=UNSTABLE` marks failed gates as unstable instead of failed.
    - **GitHub**: the schedule runs nightly mode and pull requests PR mode; `workflow_dispatch` chooses by hand.
4. **Pull request comments.** GitLab's `dvf:mr-comment` needs `DVF_GITLAB_TOKEN`, a project access token with the `api` scope. The GitHub step uses the workflow token, which cannot comment on pull requests from forks.

## Validating configurations in CI

`dvf validate-config dvf.yaml` checks a configuration without connecting: missing environment variables are warnings and secrets are not fetched. Run it on every push; add `--strict` to treat warnings as errors.
