# Security and privacy

dvf reads production data and credentials, and writes reports that people share. This page covers how to give it secrets, which database permissions it needs, how it protects personal data, and what it trusts. To report a vulnerability, follow `SECURITY.md` at the root of the repository; do not open a public issue.

## Secrets

Write a secret as a reference, never as a value:

```yaml
connections:
  erp:
    type: postgres
    host: erp-db.internal
    database: erp
    user: dvf_reader
    password: ${secret:vault:secret/dvf/erp#password}
```

The form is `${secret:<provider>:<path>#<key>}`. What `<path>` and `#<key>` mean depends on the provider.

- Each reference is resolved once per configuration load (one run), and a provider fetches a secret once even when several keys of it are used.
- Every resolved value is added to a redactor. Logs, `dvf config show`, results (including the configuration snapshot and the command line), reports, error messages, and notifications pass through it. URL-encoded and JSON-escaped forms of a value are masked too, and so are passwords in connection URLs (`postgresql://user:password@host`), credential query parameters (`?sig=`, `&access_token=`), `PWD=` and `Password=` in connection strings, `Authorization` headers, and Slack, Teams, and Discord webhook paths.
- Values shorter than 4 characters are not redacted (they would mask ordinary text). Use longer secrets.
- Error messages from providers name the secret and the reason, never the value or the credential used to fetch it.
- `dvf validate-config` does not fetch secrets unless you pass `--resolve-secrets`, so it can run in CI without access to the secret store.

### Providers

`dvf plugins list --kind secret_provider` shows each provider's status. `ready` means verified against a live service; `beta` means unit-tested against fakes and the vendor's own test stubs, not yet against the live service.

| Provider | Service | Install | Status |
|---|---|---|---|
| `env` | Environment variables, then the project's `.env` file | included | ready |
| `dotenv` | A `.env` file (local development only) | included | ready |
| `vault` | HashiCorp Vault KV v1 and v2 | included (standard library HTTPS) | ready |
| `keyring` | Windows Credential Manager, macOS Keychain, Secret Service | `dvf-framework[keyring]` | beta |
| `awssm` | AWS Secrets Manager | `dvf-framework[aws]` | beta |
| `azurekv` | Azure Key Vault | `dvf-framework[azure]` | beta |
| `gcpsm` | Google Cloud Secret Manager | `dvf-framework[gcp]` | beta |
| `databricks` | Databricks secret scopes | included (standard library HTTPS) | beta |

Provider settings come from environment variables (the process environment first, then the project's `.env` file). Most follow the vendor's own CLI, so a machine already set up for the vendor's tools usually needs nothing more.

### `env` and `dotenv`

```yaml
password: ${secret:env:PG_PASSWORD}             # environment, then .env next to the config
password: ${secret:dotenv:PG_PASSWORD}          # the project's .env only
password: ${secret:dotenv:secrets/uat.env#PG_PASSWORD}   # another .env file
```

Keep `.env` files out of version control. They are for local development; use a secret store for shared environments.

### HashiCorp Vault (`vault`)

```yaml
password: ${secret:vault:secret/dvf/erp#password}   # field "password" of secret/dvf/erp
api_key:  ${secret:vault:secret/dvf/api}            # the only field of a one-field secret
```

The path is the one `vault kv get` uses: the mount followed by the secret's path. dvf asks Vault which mount the path is in and whether it is KV v1 or v2 (`sys/internal/ui/mounts`, as the Vault CLI does). If your policy does not allow that lookup, dvf assumes the first path segment is a KV v2 mount; set `DVF_VAULT_MOUNT` and `DVF_VAULT_KV_VERSION` for other layouts. Without `#<key>`, a secret with one field returns that field; a secret with several fields is an error that lists the field names.

| Variable | Meaning |
|---|---|
| `VAULT_ADDR` | Vault address, for example `https://vault.example.org:8200` (required) |
| `VAULT_TOKEN` | Token. Without it, dvf uses AppRole when `VAULT_ROLE_ID` is set, else the token file |
| `DVF_VAULT_TOKEN_FILE` | Token file (default `~/.vault-token`, written by `vault login`) |
| `VAULT_ROLE_ID`, `VAULT_SECRET_ID`, `DVF_VAULT_SECRET_ID_FILE` | AppRole credentials (the secret ID directly or from a file) |
| `DVF_VAULT_APPROLE_MOUNT` | AppRole auth mount (default `approle`) |
| `DVF_VAULT_AUTH_METHOD` | `auto` (default), `token`, or `approle` |
| `VAULT_NAMESPACE` | Vault Enterprise or HCP Vault namespace |
| `VAULT_CACERT`, `VAULT_CAPATH` | CA bundle or folder to verify the server |
| `VAULT_CLIENT_CERT`, `VAULT_CLIENT_KEY` | Client certificate for mutual TLS |
| `VAULT_SKIP_VERIFY` | Disable certificate verification (logs a warning; never in production) |
| `DVF_VAULT_KV_VERSION`, `DVF_VAULT_MOUNT` | Skip detection: KV version (`1`, `2`) and mount path |
| `DVF_VAULT_TIMEOUT` | Request timeout in seconds (default 30) |
| `DVF_VAULT_ALLOW_INSECURE_HTTP` | Allow an `http://` address other than localhost |

dvf refuses to send a token over plain HTTP except to localhost (unless allowed), refuses redirects (a redirect would forward the token to another host; point `VAULT_ADDR` at a node that answers directly), and rejects `.` and `..` path segments.

Least-privilege policy for KV v2:

```hcl
path "secret/data/dvf/*" {
  capabilities = ["read"]
}
```

For KV v1, grant `read` on `secret/dvf/*` instead.

### OS keyring (`keyring`)

```yaml
password: ${secret:keyring:dvf-erp#dvf_reader}   # service "dvf-erp", user "dvf_reader"
```

Store the value with `keyring set dvf-erp dvf_reader` (the `keyring` command comes with the extra). Without `#<username>`, dvf uses `DVF_KEYRING_USERNAME`, or the first credential the backend has for the service. This suits developer workstations; CI and servers should use a secret store.

### AWS Secrets Manager (`awssm`)

```yaml
password: ${secret:awssm:prod/erp/db#password}     # field of a JSON secret (RDS secrets are JSON)
token:    ${secret:awssm:arn:aws:secretsmanager:eu-west-1:123456789012:secret:prod/api-AbCdEf}
```

Credentials come from the standard boto3 chain: environment variables, shared config and SSO profiles, container and instance roles. The region comes from the ARN, else `DVF_AWSSM_REGION`, `AWS_REGION`, or `AWS_DEFAULT_REGION`.

| Variable | Meaning |
|---|---|
| `DVF_AWSSM_REGION` | Region (an ARN's region wins) |
| `DVF_AWSSM_PROFILE` | Named profile (boto3 also reads `AWS_PROFILE`) |
| `DVF_AWSSM_ENDPOINT_URL` | Custom endpoint, for VPC endpoints or test servers |

Least-privilege IAM policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:eu-west-1:123456789012:secret:prod/erp/*"
    }
  ]
}
```

Add `kms:Decrypt` on the key when the secret uses a customer managed KMS key.

### Azure Key Vault (`azurekv`)

```yaml
password: ${secret:azurekv:kv-migration#sap-hana-password}    # vault "kv-migration", secret "sap-hana-password"
password: ${secret:azurekv:kv-migration/db-config#password}   # field of a JSON secret
password: ${secret:azurekv:kv-migration/sap-hana-password/0123456789abcdef0123456789abcdef}  # a version
```

The vault is a name (`https://<name>.vault.azure.net`) or a full `https://` vault URL. Authentication uses azure-identity's `DefaultAzureCredential` (environment variables, workload identity, managed identity, Azure CLI, Azure Developer CLI, PowerShell).

| Variable | Meaning |
|---|---|
| `DVF_AZUREKV_CREDENTIAL` | `default`, or one credential type: `environment`, `managed_identity`, `workload_identity`, `cli`, `developer_cli`, `powershell` |
| `DVF_AZUREKV_CLIENT_ID` | Client ID of a user-assigned managed identity |
| `DVF_AZUREKV_VAULT_SUFFIX` | DNS suffix for sovereign clouds, such as `vault.azure.cn` or `vault.usgovcloudapi.net` |

Least privilege: assign the identity the **Key Vault Secrets User** role on the vault (or on individual secrets), or, for vaults that use access policies, the `Get` secret permission only.

### Google Cloud Secret Manager (`gcpsm`)

```yaml
password: ${secret:gcpsm:erp-db-password}                     # latest version, default project
password: ${secret:gcpsm:my-project/erp-db-password/3}        # project, secret, version
token:    ${secret:gcpsm:projects/my-project/secrets/api-config#token}   # field of a JSON secret
```

Authentication uses Application Default Credentials (`gcloud auth application-default login`, a service account key in `GOOGLE_APPLICATION_CREDENTIALS`, workload identity, or the metadata server). The regional form `projects/<p>/locations/<l>/secrets/<s>` uses the regional endpoint.

| Variable | Meaning |
|---|---|
| `DVF_GCPSM_PROJECT` | Default project (also `GOOGLE_CLOUD_PROJECT`, `GCLOUD_PROJECT`, `CLOUDSDK_CORE_PROJECT`) |
| `DVF_GCPSM_LOCATION` | Location of regional secrets, such as `europe-west1` |

Least privilege: grant `roles/secretmanager.secretAccessor` on the individual secrets, not the project.

### Databricks secret scopes (`databricks`)

```yaml
password: ${secret:databricks:migration#erp-password}        # scope "migration", key "erp-password"
password: ${secret:databricks:migration/erp-config#password} # field of a JSON value
```

Inside a Databricks notebook or job, values come from `dbutils.secrets.get`, under the cluster's identity. Elsewhere dvf calls the Secrets REST API with the workspace URL and credentials of the Databricks unified authentication:

| Variable | Meaning |
|---|---|
| `DATABRICKS_HOST` | Workspace URL |
| `DATABRICKS_TOKEN` | Personal access token |
| `DATABRICKS_CLIENT_ID`, `DATABRICKS_CLIENT_SECRET` | OAuth machine-to-machine credentials of a service principal |
| `DATABRICKS_CONFIG_PROFILE`, `DATABRICKS_CONFIG_FILE` | A profile of `~/.databrickscfg` (host, token, or client ID and secret) |
| `DVF_DATABRICKS_USE_DBUTILS` | `auto` (default), `always`, or `never` |
| `DVF_DATABRICKS_TIMEOUT` | Request timeout in seconds (default 30) |

Least privilege: give the principal `READ` on the scope only, for example `databricks secrets put-acl migration dvf-validators READ`.

### Writing your own provider

See [Plugins](plugins.md#secret-providers). Subclass `dvf.plugins.SecretProvider`, raise `SecretError` with a message that never contains the value, and call `self.context.protect(value)` for any credential the provider uses to authenticate, so it is redacted too.

## Least-privilege database access

dvf only reads. Give it a dedicated identity that can read the schemas you validate and nothing else, and keep that identity's credentials in a secret store. Where the database supports it, dvf also makes its sessions read-only (`read_only: true`, the default): PostgreSQL (`default_transaction_read_only`), MySQL (`SET SESSION TRANSACTION READ ONLY`), Oracle 23ai (`READ_ONLY` sessions), DuckDB and SQLite files (opened read-only), and ODBC and SQLAlchemy connections where the driver supports it. Permissions remain the real control.

**PostgreSQL**

```sql
CREATE ROLE dvf_reader LOGIN PASSWORD '<from your secret store>';
GRANT CONNECT ON DATABASE erp TO dvf_reader;
GRANT USAGE ON SCHEMA sales TO dvf_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA sales TO dvf_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA sales GRANT SELECT ON TABLES TO dvf_reader;
ALTER ROLE dvf_reader SET default_transaction_read_only = on;
-- PostgreSQL 14+, all schemas: GRANT pg_read_all_data TO dvf_reader;
```

**SQL Server and Azure SQL**

```sql
CREATE LOGIN dvf_reader WITH PASSWORD = '<from your secret store>';
USE erp;
CREATE USER dvf_reader FOR LOGIN dvf_reader;
GRANT SELECT ON SCHEMA::sales TO dvf_reader;     -- or: ALTER ROLE db_datareader ADD MEMBER dvf_reader;
```

On Azure SQL, prefer a Microsoft Entra identity (`CREATE USER [dvf-app] FROM EXTERNAL PROVIDER;`) over a SQL login.

**Oracle**

```sql
CREATE USER dvf_reader IDENTIFIED BY "<from your secret store>";
GRANT CREATE SESSION TO dvf_reader;
GRANT READ ON sales.orders TO dvf_reader;                  -- per table; READ cannot lock rows
GRANT FLASHBACK ON sales.orders TO dvf_reader;             -- only for as_of (Flashback Query)
-- Oracle 23ai, a whole schema: GRANT SELECT ANY TABLE ON SCHEMA sales TO dvf_reader;
```

**MySQL**

```sql
CREATE USER 'dvf_reader'@'%' IDENTIFIED BY '<from your secret store>' REQUIRE SSL;
GRANT SELECT ON sales.* TO 'dvf_reader'@'%';
```

**Snowflake**

```sql
CREATE ROLE dvf_reader;
GRANT USAGE ON WAREHOUSE validation_wh TO ROLE dvf_reader;
GRANT USAGE ON DATABASE erp TO ROLE dvf_reader;
GRANT USAGE ON SCHEMA erp.sales TO ROLE dvf_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA erp.sales TO ROLE dvf_reader;
GRANT SELECT ON FUTURE TABLES IN SCHEMA erp.sales TO ROLE dvf_reader;
GRANT SELECT ON ALL VIEWS IN SCHEMA erp.sales TO ROLE dvf_reader;
CREATE USER dvf_svc TYPE = SERVICE DEFAULT_ROLE = dvf_reader RSA_PUBLIC_KEY = '<public key>';
GRANT ROLE dvf_reader TO USER dvf_svc;
```

Use key-pair authentication for the service user, and a small dedicated warehouse so validation cannot starve other workloads.

**BigQuery**

Grant the service account `roles/bigquery.dataViewer` on the datasets to validate (not the project), `roles/bigquery.jobUser` on the project that runs the queries, and `roles/bigquery.readSessionUser` on that project if you use the BigQuery Storage Read API.

**Amazon Redshift**

```sql
CREATE USER dvf_reader PASSWORD '<from your secret store>';
GRANT USAGE ON SCHEMA sales TO dvf_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA sales TO dvf_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA sales GRANT SELECT ON TABLES TO dvf_reader;
```

Prefer IAM authentication (temporary credentials) over a database password.

**Databricks (Unity Catalog)**

```sql
GRANT USE CATALOG ON CATALOG main TO `dvf-validators`;
GRANT USE SCHEMA ON SCHEMA main.sales TO `dvf-validators`;
GRANT SELECT ON SCHEMA main.sales TO `dvf-validators`;
```

Also give the group `CAN USE` on the SQL warehouse. Use a service principal with OAuth rather than a personal access token.

**MongoDB**

```javascript
db.getSiblingDB("admin").createUser({
  user: "dvf_reader",
  pwd: passwordPrompt(),
  roles: [{ role: "read", db: "crm" }]
})
```

dvf runs only `find`, `aggregate`, `listCollections`, and metadata commands.

**SAP HANA**

```sql
CREATE USER DVF_READER PASSWORD "<from your secret store>" NO FORCE_FIRST_PASSWORD_CHANGE;
GRANT SELECT ON SCHEMA SAPABAP1 TO DVF_READER;
-- or per table: GRANT SELECT ON "SAPABAP1"."KNA1" TO DVF_READER;
```

The `ddic` options read the ABAP dictionary tables (`DD02L`, `DD03L`, `DD04L`) in the same schema, which the schema grant covers.

## Personal data

Migration data is often regulated, and reports are widely shared. The `privacy` settings under `defaults` (or a suite's `defaults`) control what leaves the process:

```yaml
defaults:
  privacy:
    mode: standard           # or strict
    mask_pii: true           # protect tagged and detected values in samples
    pii_columns: ["*email*", "*phone*", tax_id, iban]
    detect_patterns: true    # also protect values that look like personal data, in any column
    masking: mask            # mask, hash, or tokenize
    hash_key: ${secret:vault:secret/dvf/privacy#hash_key}   # for masking: hash
```

### Tagging and detection

A column is personal data when its name matches `pii_columns` (glob patterns, case-insensitive) or its mapping says `pii: true`:

```yaml
columns:
  mappings:
    - { source: SMTP_ADDR, target: email, pii: true }
```

Sample values of these columns are always protected. With `detect_patterns: true` (the default), text values in any column are also protected when they look like an email address, a phone number (international `+` numbers and common grouped formats), a national identifier (US SSN, UK National Insurance number, Aadhaar), a payment card number (13 to 19 digits that pass the Luhn check), or an IBAN (with a valid check digit). Detection is a safety net, not a guarantee: tag the columns you know.

The minimum and maximum of a tagged column are hidden in profile and aggregate results.

### Masking modes

| `masking` | A protected value is written as | Use it when |
|---|---|---|
| `mask` (default) | `***`; detected values keep a non-identifying hint: `j***@example.com`, `***58` (last two digits of a phone number), `***1111` (last four of a card or IBAN) | Reports are read by people who do not need the values |
| `hash` | `hash:` and 16 hex characters of a keyed HMAC-SHA256 | You need to see that two values are equal (source vs target, or across rows). With `hash_key` set, digests are stable across runs; without it, a random key per run is used |
| `tokenize` | `tok_000042`, the same token for the same value within a run | Readable equality within one report; tokens are never stored |

Hashes use a key, so they cannot be reversed by hashing guesses unless the key is known; keep `hash_key` in a secret store.

### Strict mode

`mode: strict` keeps every raw value inside the process:

- no samples (failing rows, key samples, mismatch samples) and no extracts;
- literal values are removed from recorded SQL in results, reports, and repro files (string literals become `'***'`, and numbers in `IN` lists and comparisons become `0`), so repro SQL shows the query's shape but cannot be run as is;
- minimum and maximum values are hidden in profiles and aggregate comparisons;
- non-numeric observed values are masked, also in the check's message;
- notifications carry counts only.

Strict mode still shows what the configuration says (table, column, and check names, filters, `group_by` and partition labels, which are values of the columns you chose to group by) and aggregates (counts, sums, ratios). Do not group by personal columns in strict suites.

### Sampling per check

`sample_size` on a table sets how many failing rows each check keeps (default 50). A check can override it; `sample_size: 0` turns its samples off:

```yaml
checks:
  - { type: regex, column: national_id, pattern: '^\d{9}$', sample_size: 0 }
```

## What dvf writes

| Where | What | Protection |
|---|---|---|
| `reports.output_dir` | Results JSON, HTML, Markdown, JUnit, Excel; extracts; repro files | Samples masked by `privacy`; secrets redacted; text in Excel is never written as a formula |
| `defaults.execution.state_dir` (`.dvf`) | Checkpoints, saved results, history, baselines | Same results as above; checkpoints and saved results are written with owner-only permissions on Linux and macOS |
| `defaults.execution.temp_dir` (default: the system temporary folder) | Spill files and staged data of NoSQL, RFC, and file sources | Raw data; each run uses a new folder created with owner-only permissions on Linux and macOS, removed when the run ends |

Extracts contain the values of failing rows (masked like samples). The default format is Parquet; if you choose CSV, import the files rather than opening them by double-click in a spreadsheet, which may interpret cells that start with `=` as formulas. On shared machines, restrict access to the report and state folders (for example `umask 077` on Linux).

File names are derived from check IDs and run IDs with every character other than letters, digits, `.`, `_`, `-`, `+`, and `=` replaced, so data or names cannot write outside these folders. Run IDs given with `--run-id` or `--resume` must be letters, digits, `.`, `_`, and `-`.

## What dvf trusts

- **Configuration is code.** Custom SQL (`query:`, `filter:`, `where:`, `source_expr:`, `custom_sql` checks) runs exactly as written, with the connection's permissions. Restrict who can change configuration in shared environments, and review it like code.
- **Plugins are code.** `plugins.paths`, `plugins.modules`, `--plugins`, and installed packages that register `dvf.*` entry points run Python in the dvf process. Load only plugins you trust.
- **Report templates are code.** Files in `reports.templates_dir` are Jinja2 templates rendered with access to the results; treat them like plugins.
- **Data is untrusted.** Values from sources and targets are quoted per dialect when dvf writes them into SQL (key lists, partition bounds), HTML-escaped in reports (autoescaping is always on, and links with schemes such as `javascript:` are shown as text), written as text in Excel, and never used to build file names.
- **Services are untrusted.** dvf talks only to the endpoints in the configuration and the environment. The Vault, Databricks, and notification clients refuse redirects; the SAP OData client follows next links and redirects only on the configured service's own host and port.

dvf sends no telemetry. Data leaves the machine only to the systems you configure: connections, sinks, notification channels, OpenLineage, and telemetry exporters.

## Supply chain

- Dependencies are locked in `uv.lock`; Dependabot proposes updates.
- `nox -s audit` runs `pip-audit` on the locked dependencies (runtime with every extra, then everything), and CI runs it on every change.
- CodeQL scans the code; the OpenSSF Scorecard runs on the repository.
- Each release publishes a CycloneDX SBOM (`nox -s sbom`) and is uploaded to PyPI with Trusted Publishing and PEP 740 attestations; container images carry build provenance.
- Proprietary drivers (SAP `hdbcli`, PyRFC, the NW RFC SDK) are never dependencies; install them from the vendor.
