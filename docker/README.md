# Container images

Two images are published to the GitHub Container Registry for every release, for `linux/amd64` and `linux/arm64`:

| Image | Tags | Contents |
|---|---|---|
| `ghcr.io/anandtopu/dvf` | `1.2.3`, `1.2`, `1`, `latest` | Slim: the core package. DuckDB, SQLite, and CSV, TSV, Parquet, JSON, and NDJSON files. |
| `ghcr.io/anandtopu/dvf` | `1.2.3-full`, `1.2-full`, `1-full`, `latest-full` | Full: the slim image plus the open-source drivers and formats: PostgreSQL, MySQL, Oracle (thin mode), MongoDB, SQLAlchemy, ODBC (unixODBC without a driver), Polars, Excel, Delta Lake, Iceberg, and Avro. |

Pre-releases are tagged with their exact version only.

Both images:

- run `dvf` as the entrypoint (`docker run IMAGE run dvf.yaml` runs `dvf run dvf.yaml`), with `--help` as the default command;
- run as the non-root user `dvf` (UID 10001, group 0), and also work under any other UID;
- use `/work` as the working directory, for mounting a project;
- install their Python packages from `uv.lock`, so an image matches the lockfile of the commit it was built from;
- stop cleanly on `docker stop`: the stop signal is `SIGINT`, which dvf turns into an interrupted run with checkpoints and exit code 2, ready for `dvf run --resume`.

Release images carry OCI labels and annotations (version, source commit, license), a BuildKit SBOM and SLSA provenance in the registry, and a GitHub build provenance attestation (`gh attestation verify oci://ghcr.io/anandtopu/dvf:1.2.3 --owner anandtopu`).

## Run a project

Mount the folder that holds `dvf.yaml` at `/work`:

```bash
docker run --rm -v "$PWD:/work" ghcr.io/anandtopu/dvf:latest run dvf.yaml
```

On Linux, add `--user "$(id -u):$(id -g)"` so the reports written to the mounted folder belong to you. Docker Desktop on Windows and macOS does not need it.

```powershell
docker run --rm -v "${PWD}:/work" ghcr.io/anandtopu/dvf:latest run dvf.yaml
```

Pass credentials as environment variables and reference them in the configuration with `${secret:env:NAME}`:

```bash
docker run --rm -v "$PWD:/work" -e PG_PASSWORD ghcr.io/anandtopu/dvf:latest-full run dvf.yaml
```

To reach a database on the Docker host, use `--network host` on Linux, or `host.docker.internal` as the host name on Docker Desktop.

## Add a proprietary driver

Proprietary drivers are never included. Build your own image on top of the full one and install them there, for example the SAP HANA client:

```dockerfile
FROM ghcr.io/anandtopu/dvf:1.2.3-full
COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /usr/local/bin/uv
USER root
RUN uv pip install --python /opt/dvf/bin/python --no-cache hdbcli
USER 10001:0
```

The image's Python environment is `/opt/dvf`; it has no pip, so the example borrows uv from its image. A vendor ODBC driver goes into the system with the vendor's packages (for example `msodbcsql18` for `pyodbc`), registered in `/etc/odbcinst.ini`. Check that the driver's license allows your use and redistribution.

## Build locally

Build from the repository root, not from this folder:

```bash
docker build -f docker/Dockerfile.slim --build-arg DVF_VERSION=1.2.3 -t dvf:slim .
docker build -f docker/Dockerfile.full --build-arg DVF_VERSION=1.2.3 -t dvf:full .
```

Podman works the same way (`podman build ...`). `uv run nox -s docker` builds both images and runs the seeded example in each; `uv run nox -s docker -- slim` builds one. It uses Docker, or Podman when Docker is not installed (`DVF_CONTAINER_ENGINE` chooses); `DVF_CONTAINER_NETWORK=host` passes `--network host` to builds and runs where container networking needs it.

| Build argument | Default | Purpose |
|---|---|---|
| `DVF_VERSION` | `0.0.0+container` | The package version. The version normally comes from the git tag, and `.git` is not part of the build context. |
| `VCS_REF`, `BUILD_DATE`, `SOURCE_URL` | empty or placeholder | OCI labels. The release workflow sets them. |
| `DVF_EXTRAS` (full only) | the open-source extras listed above | The pip extras to install, separated by spaces. |

To build an image with a different set of extras, for example only PostgreSQL and Snowflake:

```bash
docker build -f docker/Dockerfile.full --build-arg DVF_EXTRAS="postgres snowflake" -t dvf:custom .
```

## Notes

- The base images (Python 3.13 on Debian trixie, and uv) are pinned by tag and digest in the `FROM` lines, so Dependabot's `docker` updates can refresh them.
- The images are about 400 MB (slim) and 950 MB (full) uncompressed. Most of it is PyArrow and DuckDB, and in the full image also Polars and Delta Lake.
- Spark is not included, because it needs a Java runtime. For Spark, use `pip install 'dvf-framework[spark]'` in an image that has Java 17 or 21.
- SQL Server is not in the full image: its driver, `mssql-python`, ships Microsoft's ODBC Driver 18 libraries inside its wheel, under Microsoft's license terms, and dvf never bundles proprietary drivers. Build your own image with `--build-arg DVF_EXTRAS="postgres mssql"` (or any list including `mssql`) once you accept that license.
