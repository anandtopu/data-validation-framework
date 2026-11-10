# Installation

dvf is published on PyPI as **`dvf-framework`** (the name `dvf` belongs to another project). The command it installs, and the Python package you import, are both `dvf`.

You need Python 3.11, 3.12, 3.13, or 3.14 on Windows, macOS, or Linux. The core package includes DuckDB and PyArrow, so local files, DuckDB, and SQLite work without anything else. Drivers for other systems are optional extras.

## Choose how to install

| You want | Use |
|---|---|
| The `dvf` command, isolated from other Python tools | `uv tool install dvf-framework` or `pipx install dvf-framework` |
| dvf in a project or notebook, to call `dvf.run` or `dvf.validate_dataframe` | `pip install dvf-framework` in a virtual environment, or `uv add dvf-framework` |
| No Python on the machine, or a CI job that runs containers | The Docker images |

### pip

Create a virtual environment first, so dvf's dependencies stay separate from other projects.

On Linux and macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install dvf-framework
dvf --version
```

On Windows (PowerShell):

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install dvf-framework
dvf --version
```

### uv

[uv](https://docs.astral.sh/uv/) installs the command into its own environment and puts `dvf` on your `PATH`:

```bash
uv tool install dvf-framework
uv tool install 'dvf-framework[postgres,polars]'   # with extras
uv tool upgrade dvf-framework
```

To run dvf once without installing it: `uvx --from dvf-framework dvf --version`. In a uv-managed project, `uv add dvf-framework` (or `uv add 'dvf-framework[postgres]'`) adds it as a dependency.

### pipx

```bash
pipx install dvf-framework
pipx install --force 'dvf-framework[postgres,mssql]'   # reinstall with extras
pipx upgrade dvf-framework
```

### Docker

Two images are published to the GitHub Container Registry for `linux/amd64` and `linux/arm64`.

| Image | Contents |
|---|---|
| `ghcr.io/anandtopu/dvf:latest` (or a version, `ghcr.io/anandtopu/dvf:1.2.3`) | Slim: DuckDB, SQLite, and CSV, TSV, Parquet, JSON, and NDJSON files |
| `ghcr.io/anandtopu/dvf:latest-full` (or `ghcr.io/anandtopu/dvf:1.2.3-full`) | Full: adds PostgreSQL, MySQL, Oracle, MongoDB, SQLAlchemy, ODBC (without a driver), Polars, Excel, Delta Lake, Iceberg, and Avro. SQL Server is left out because its driver bundles Microsoft's proprietary ODBC driver; build your own image with it ([docker/README.md](https://github.com/anandtopu/data-validation-framework/blob/main/docker/README.md)) |

The entrypoint is `dvf` and the working directory is `/work`, so mount your project there:

```bash
docker run --rm -v "$PWD:/work" ghcr.io/anandtopu/dvf:latest init demo --example
docker run --rm -v "$PWD:/work" ghcr.io/anandtopu/dvf:latest run demo/dvf.yaml
```

```powershell
docker run --rm -v "${PWD}:/work" ghcr.io/anandtopu/dvf:latest run demo/dvf.yaml
```

The images run as a non-root user (UID 10001). On Linux, add `--user "$(id -u):$(id -g)"` so the reports written to your folder belong to you. Pass credentials with `-e NAME` and reference them as `${secret:env:NAME}`. Proprietary drivers are not included; `docker/README.md` in the repository shows how to add one in your own image, how to build the images yourself, and how to verify their provenance.

## Extras

Install extras with the package, for example `pip install 'dvf-framework[postgres,polars]'`. Each extra adds open-source Python packages only.

| Extra | Installs | For |
|---|---|---|
| (none) | DuckDB, PyArrow, sqlglot, Pydantic, Jinja2, Typer, Rich | `duckdb`, `sqlite`, and `files` connections (CSV, TSV, Parquet, JSON, NDJSON, ORC); the `pushdown` and `duckdb` engines; HTML, JSON, JUnit, and Markdown reports |
| `postgres` | psycopg 3 (binary) | `postgres` connections and the PostgreSQL result sink |
| `mysql` | PyMySQL | `mysql` connections |
| `mssql` | mssql-python | `mssql` connections (SQL Server, Azure SQL) |
| `oracle` | python-oracledb | `oracle` connections, in thin mode (no Oracle client needed) |
| `snowflake` | snowflake-connector-python | `snowflake` connections |
| `bigquery` | google-cloud-bigquery | `bigquery` connections |
| `redshift` | redshift-connector | `redshift` connections |
| `databricks` | databricks-sql-connector | `databricks` connections |
| `spark` | PySpark 4, pandas | the `spark` connection and engine (needs Java, see below) |
| `polars` | Polars | the `polars` engine, and Polars DataFrames in `dvf.validate_dataframe` |
| `pandas` | pandas | pandas DataFrames in `dvf.validate_dataframe` |
| `excel` | openpyxl | Excel files, the Excel report, and mapping sheets for `dvf import-mapping` |
| `delta` | deltalake | Delta Lake tables |
| `iceberg` | PyIceberg | Iceberg tables |
| `avro` | fastavro | Avro files |
| `mongodb` | PyMongo | `mongodb` connections |
| `cassandra` | cassandra-driver | `cassandra` connections |
| `dynamodb` | boto3 | `dynamodb` connections |
| `cosmos` | azure-cosmos, azure-identity | `cosmos` connections |
| `sqlalchemy` | SQLAlchemy | `sqlalchemy` connections (install the dialect's driver too) |
| `odbc` | pyodbc | `odbc` connections (install the database's ODBC driver too) |
| `otel` | OpenTelemetry API and SDK | OpenTelemetry traces and metrics |
| `azure` | azure-identity, azure-keyvault-secrets | Azure Key Vault secrets |
| `aws` | boto3 | AWS Secrets Manager secrets |
| `gcp` | google-cloud-secret-manager | Google Cloud Secret Manager secrets |
| `keyring` | keyring | secrets in the operating system's keyring |
| `all` | every extra above | everything open source |

`dvf plugins list` shows which components are `ready`, `beta` (implemented, not yet verified against a live system), or `declared` (planned). A connection whose extra is missing fails with a message naming the extra to install; other connections are not affected.

## Java for Spark

The `spark` extra installs PySpark, which needs a Java runtime: Java 17 or 21, either on `PATH` or through `JAVA_HOME`. dvf starts Spark only when a Spark connection opens or the Spark engine runs.

- Windows: install a JDK such as Eclipse Temurin 21 (`winget install EclipseAdoptium.Temurin.21.JDK`) and open a new terminal. `winutils.exe` is not needed; the warning Spark prints about it at startup is harmless ([Spark](spark.md)).
- macOS: `brew install openjdk@21`, then follow Homebrew's note to link it or set `JAVA_HOME`.
- Linux: install the distribution's OpenJDK 17 or 21 package, for example `sudo apt install openjdk-21-jre-headless`.

Databricks Connect replaces PySpark, so install it instead of the `spark` extra: `pip install dvf-framework 'databricks-connect==15.4.*'`, matching your cluster's runtime.

## Proprietary drivers

dvf never installs proprietary drivers. Install them yourself where you run dvf; dvf imports them only when a connection needs them and tells you what is missing.

| For | Install |
|---|---|
| `sap_hana` connections | The SAP HANA client for Python: `pip install hdbcli`. Check that your SAP agreement covers your use. |
| `sap_rfc` connections with the `pyrfc` or `custom_fm` backend | The SAP NW RFC SDK from the SAP Support Portal, then PyRFC. See [SAP](sap.md). |
| `odbc` connections | The ODBC driver from the database vendor, registered with the system's ODBC driver manager (unixODBC on Linux and macOS). |
| `sqlalchemy` connections | The DBAPI driver the SQLAlchemy dialect needs, if it is not one of the extras above. |

In Docker, build your own image `FROM` the full image and install the driver there.

## Platform notes

### Windows

- 64-bit Windows 10 or 11 on x86-64. PyArrow publishes no wheels for Windows on ARM, so on ARM devices use an x86-64 Python (it runs under emulation) or WSL.
- In PowerShell and `cmd`, quote extras with double quotes: `pip install "dvf-framework[postgres]"`. Single quotes do not work in `cmd`.
- If PowerShell refuses to run `Activate.ps1`, allow local scripts for your user once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
- Paths in configuration files may use forward slashes, which work everywhere.

### macOS

- macOS 12 or later, on Apple silicon or Intel.
- In zsh, the default shell, quote extras: `pip install 'dvf-framework[postgres]'`, because zsh treats square brackets as a pattern.
- `pyodbc` (the `odbc` extra) needs unixODBC: `brew install unixodbc`.

### Linux

- x86-64 or ARM64 with glibc 2.28 or later (for example RHEL 8, Debian 10, Ubuntu 20.04, or newer).
- Alpine and other musl-based distributions are not supported, because DuckDB publishes no wheels for them. Use the Docker images instead.
- `pyodbc` (the `odbc` extra) needs unixODBC's shared library: `sudo apt install unixodbc` on Debian and Ubuntu, `sudo dnf install unixODBC` on RHEL and Fedora.

## Check the installation

```bash
dvf --version
dvf init demo --example
dvf run demo/dvf.yaml
```

The run finds the seeded defects, exits with code 1 on purpose, and writes `report.html` under `demo/reports/`. On a fresh machine, installing from PyPI and producing the report takes well under five minutes; the project's `nox -s quickstart` session measures it. Continue with [Getting started](getting-started.md).

## Upgrade and uninstall

| Installed with | Upgrade | Uninstall |
|---|---|---|
| pip | `pip install --upgrade dvf-framework` | `pip uninstall dvf-framework` |
| uv tool | `uv tool upgrade dvf-framework` | `uv tool uninstall dvf-framework` |
| pipx | `pipx upgrade dvf-framework` | `pipx uninstall dvf-framework` |
| Docker | `docker pull ghcr.io/anandtopu/dvf:latest` | `docker rmi ghcr.io/anandtopu/dvf:latest` |

dvf follows Semantic Versioning: configuration files and the results schema stay compatible within a major version, and deprecations warn for at least one minor release before anything is removed.
