# SAP

The SAP pack validates data that comes from or goes to SAP ECC and S/4HANA. It provides:

- two connectors: `sap_hana` (database level) and `sap_rfc` (application level, through RFC or OData);
- ABAP dictionary (DDIC) schemas: ABAP types, lengths, keys, field texts, and foreign keys;
- nine normalizers for SAP storage conventions: ALPHA keys, material numbers, NUMC, DATS, TIMS, flags, language keys, currency amounts, and quantities;
- business object templates: Business Partner (CVI) links, GL balances and trial balance, open items, and the material master.

| Component | Status |
|---|---|
| SAP normalizers | `ready`: parity-tested on DuckDB, SQLite, PostgreSQL, Polars, and Spark |
| `sap_hana` connector, `hana` SQL dialect | `beta`: unit-tested with a fake `hdbcli`, not yet run against a live HANA system |
| `sap_rfc` connector (`pyrfc`, `custom_fm`, `odata` backends) | `beta`: unit-tested against a simulated RFC_READ_TABLE and a local OData server |
| `sap_rfc` backend `nwrfcsdk` | `declared`: selecting it raises an error that names the alternatives |

## Choosing how to connect

Use the first option your license and landscape allow:

1. **HANA database access** (`sap_hana`). Counts, control totals, and checksums run inside HANA, and only differing rows move. Your SAP license must allow direct database access (runtime licenses often do not; full-use licenses usually do).
2. **Extracted files.** ODP, SLT, Datasphere, or BW exports to Parquet or CSV, read with the `files` connector. The SAP normalizers work on extracted data as on the database.
3. **RFC or OData** (`sap_rfc`). Rows travel through the application server and are staged locally, so this suits configuration tables, master data, and filtered reads more than full transaction tables.

## Installing SAP drivers

dvf never installs, depends on, or bundles SAP software. Install what you need yourself:

| Connector | Driver | How to install |
|---|---|---|
| `sap_hana` | SAP HANA client for Python (`hdbcli`) | `pip install hdbcli`, or the SAP HANA Client package from SAP. It is distributed under the SAP Developer License; check that your SAP agreement covers your use. |
| `sap_rfc`, backends `pyrfc` and `custom_fm` | PyRFC and the SAP NW RFC SDK | Download the NW RFC SDK from the SAP Support Portal (an S-user is needed), set `SAPNWRFC_HOME`, then build and install PyRFC from its GitHub repository. SAP stopped maintaining PyRFC in January 2026 and has not named a replacement. |
| `sap_rfc`, backend `odata` | none | Uses the Python standard library. |

When a driver is missing, the connection fails with a message that says which package is needed and how to get it.

## HANA database access: `sap_hana`

```yaml
connections:
  ecc:
    type: sap_hana
    host: ${env:SAP_HANA_HOST}
    port: 30215
    database_name: S4D            # the tenant, on multitenant systems
    user: ${env:SAP_HANA_USER}
    password: ${secret:env:SAP_HANA_PASSWORD}
    schema: SAPSR3                # the SAP schema (SAPHANADB on newer systems)
    client: "100"                 # filter client-dependent tables to client 100
    ddic: true                    # describe tables from the ABAP dictionary
```

| Option | Default | Meaning |
|---|---|---|
| `host` | required | HANA host name. |
| `port` | `30015` | SQL port: `3<instance>15` (single container), `3<instance>13` (system database), `3<instance>41` and up (tenants), `443` (HANA Cloud). |
| `database_name` | none | Tenant database (hdbcli `databaseName`). |
| `user`, `password` | none | Database credentials. Use a secret reference for the password. |
| `schema` | none | Schema for unqualified table names (`currentSchema`). |
| `client` | none | SAP client (three digits). Client-dependent tables are filtered to it. |
| `client_column` | detected | Client column to filter on, for every table that has it. |
| `encrypt` | `true` | TLS. |
| `ssl_validate_certificate` | `true` | Validate the server certificate. |
| `ddic` | `false` | Read ABAP types, lengths, keys, and texts from the dictionary (see [DDIC schemas](#ddic-schemas)). |
| `ddic_schema` | `schema` | Schema of the dictionary tables, if different. |
| `ddic_language` | `E` | SAP language key of dictionary texts (`E` English, `D` German). |
| `query_timeout_s` | run default | Per-statement timeout (`setquerytimeout`). |
| `options` | `{}` | Further `hdbcli` connect properties, passed through (for example `compress: true`). |

dvf only runs `SELECT` statements; give it a database user with read privileges on the SAP schema and nothing more.

### Client (MANDT) filtering

With `client` set, every client-dependent table is read as `SELECT * FROM table WHERE <client column> = '<client>'`, so you do not repeat `MANDT = '100'` in filters. dvf finds the client column:

1. `client_column`, when set (only tables that have that column are filtered);
2. with `ddic: true`, the dictionary's client-type (`CLNT`) key field, for tables the dictionary marks client-dependent;
3. otherwise, a first column named `MANDT`, `CLIENT`, `RCLNT`, or `MANDANT` with three characters.

Query datasets are not rewritten: write the client condition into the query. To read all clients of one table, set `options: {client_filter: false}` on its dataset.

### The `hana` SQL dialect

The dialect generates HANA SQL for counts, control totals, hashes, and checks. Things to know:

- There is no Unicode normalization function, so text is compared without it and the plan notes say so.
- Row hashes use `HASH_MD5` over HANA's internal CESU-8 encoding, which equals UTF-8 except for characters beyond U+FFFF (emoji, some CJK extensions): text holding them hashes differently than on other databases.
- `TIME` holds whole seconds; time zone conversion uses `LOCALTOUTC`, which needs HANA's time zone tables.
- Regular expressions use `LIKE_REGEXPR` (PCRE).

SAP stores ABAP `DATS`, `TIMS`, `NUMC`, and flags as `NVARCHAR`. Use the SAP normalizers to compare them with typed columns.

## RFC and OData access: `sap_rfc`

```yaml
connections:
  ecc_rfc:
    type: sap_rfc
    backend: pyrfc
    ashost: sapecc.example.com
    sysnr: "00"
    client: "100"
    user: ${env:SAP_USER}
    password: ${secret:env:SAP_PASSWORD}
    page_size: 50000

  s4_api:
    type: sap_rfc
    backend: odata
    url: https://s4.example.com/sap/opu/odata/sap/API_BUSINESS_PARTNER
    client: "100"
    user: ${env:SAP_USER}
    password: ${secret:env:SAP_PASSWORD}
```

| Option | Default | Meaning |
|---|---|---|
| `backend` | `pyrfc` | `pyrfc`, `custom_fm`, `odata`, a plugin's backend, or `nwrfcsdk` (declared, not implemented). `dvf sap backends` lists them. |
| `ashost`, `sysnr` | none | Application server and instance number (direct logon). |
| `mshost`, `sysid`, `msserv`, `group` | none | Message server logon (load balancing). |
| `saprouter` | none | SAProuter string. |
| `client` | required | Logon client. Client-dependent tables are read in it; there is no MANDT condition to write. |
| `user`, `password` | none | Logon credentials (HTTP basic authentication for `odata`). |
| `lang` | `EN` | Logon language. |
| `read_function` | `RFC_READ_TABLE` | Function module that reads tables. `custom_fm` needs your module here. |
| `row_width` | `512` | Characters per row of the read function. Set your module's width for `custom_fm`. |
| `delimiter` | `""` | Field delimiter sent to the read function (only custom modules that require one need it). |
| `page_size` | `50000` | Rows per call (`ROWCOUNT`, or OData `$top`). |
| `ddic_source` | `fieldinfo` | Describe tables with `DDIF_FIELDINFO_GET`, or with `dd03l` (DD03L read through the read function). |
| `foreign_keys` | `false` | Also read foreign keys (DD05S, DD08L). |
| `url` | none | OData service root (`odata` backend). |
| `odata_version` | detected | `2` or `4`. |
| `verify_tls` | `true` | Validate the OData service certificate. |
| `options` | `{}` | PyRFC logon parameters, passed through; for `odata`, `headers:` adds HTTP headers (for example `Authorization: ${secret:env:SAP_TOKEN}`). |

Datasets name a table (or, with `odata`, an entity set). These dataset options are pushed to SAP:

```yaml
source:
  table: BSEG
  options:
    fields: [BUKRS, BELNR, GJAHR, BUZEI, WRBTR, WAERS]   # read only these
    where: "BUKRS = '1000' AND GJAHR = '2025'"           # evaluated by SAP (Open SQL, or OData $filter)
    max_rows: 100000
  filter: "WRBTR <> 0"                                   # applied to the staged rows (DuckDB SQL)
```

SQL `query` datasets are not supported; connect to the database with `sap_hana` for SQL.

Values keep SAP's internal form: customer numbers with leading zeros, `DATS` as `YYYYMMDD`, amounts with the decimals of their field. The RFC user needs `S_RFC` for `RFC_READ_TABLE`, `DDIF_FIELDINFO_GET`, and `RFC_SYSTEM_INFO` (or your module), and table authorizations (`S_TABU_DIS` or `S_TABU_NAM`) for the tables you read.

### RFC_READ_TABLE limits and how dvf handles them

`RFC_READ_TABLE` is available on every system, but it was not built for bulk reads:

| Limit | What dvf does |
|---|---|
| Each row is one 512-character field; wider requests fail with `DATA_BUFFER_EXCEEDED`. | A first call with `NO_DATA = 'X'` returns every field's width without reading rows. Fields are packed into chunks that fit `row_width`; each chunk repeats the key fields, and chunks are joined on the key. A chunk the function still rejects is split in two. A single field wider than the row, or keys that do not fit, fail with a message pointing to the alternatives. |
| No `ORDER BY`: rows may come back in another order on each call. | Chunks of one page must return the same keys, and no key may repeat across pages; otherwise the read fails instead of returning wrong data. Raise `page_size` to read in fewer calls, or narrow the read with `options.where`. |
| `OPTIONS` lines hold 72 characters. | Conditions are split between tokens, never inside a literal. A literal longer than 72 characters cannot be sent. |
| Numbers are written as text: a trailing minus sign (`123.45-`), floats in E notation, raw fields in hexadecimal. | Fields are cut at the offsets the function returns (so a delimiter inside a value cannot shift columns) and converted by DDIC type. |
| Strings of unlimited length (`STRG`, `RSTR`) cannot be read. | They are left out of the default field list; naming one in `options.fields` is an error. |
| Every call runs in a dialog or RFC work process. | Prefer `sap_hana` or extracted files for large tables. |

Mitigations, from the most effective:

1. Read the database (`sap_hana`) or extracted files instead.
2. Use a custom read module (`backend: custom_fm`): a copy of RFC_READ_TABLE with a wider work area (for example 8000 characters, or a string), the same parameters (`QUERY_TABLE`, `DELIMITER`, `NO_DATA`, `ROWSKIPS`, `ROWCOUNT`, `OPTIONS`, `FIELDS`) and tables (`FIELDS`, `DATA` with field `WA`). Set `read_function` and `row_width`.
3. Read fewer fields (`options.fields`) and fewer rows (`options.where`).
4. Use an OData service or CDS view (`backend: odata`).

### OData services and CDS views

The `odata` backend reads entity sets of OData v2 and v4 services, such as the S/4HANA APIs or CDS views exposed as services:

- the service `$metadata` gives the properties, their Edm types, and the key;
- rows are read in pages of `page_size`, ordered by the key, following the server's next links when it sends them;
- `options.fields` becomes `$select`, `options.where` becomes `$filter` (OData syntax: `CompanyCode eq '1000'`);
- every request carries `sap-client` and `sap-language`.

v2 `/Date(...)/` and `PT12H30M00S` values, v4 ISO dates and times, decimals and 64-bit integers sent as strings, and base64 binaries are converted to typed columns.

### Adding a backend

A plugin can add a backend: subclass `dvf.packs.sap.rfc.SapRfcBackend` (implement `describe` and `read`, and `call` for function modules), then register it with `dvf.packs.sap.rfc.register_rfc_backend("name", MyBackend)` or the entry point group `dvf.sap_rfc_backends`. Connections select it with `backend: name`.

## DDIC schemas

Both connectors can describe tables from the ABAP dictionary:

| Table | Used for |
|---|---|
| DD02L, DD02T | Table class, client dependence, delivery class, description |
| DD03L | Fields: position, key flag, ABAP type, length, decimals, data element, currency and unit references |
| DD04L | Type and length of fields whose DD03L row leaves them empty |
| DD04T | Field descriptions (from the data element) |
| DD05S, DD08L | Foreign keys: check tables and field pairs |

`sap_hana` reads these tables with SQL when `ddic: true`; `sap_rfc` uses `DDIF_FIELDINFO_GET` (or DD03L with `ddic_source: dd03l`) and DD05S/DD08L with `foreign_keys: true`.

Schemas show the ABAP type and length as the native type (`CHAR(10)`, `CURR(13,2)`, `DATS(8)`). The canonical type, which comparisons use, is the type of the stored values: `DATS` and `TIMS` are text, because SAP stores them as `YYYYMMDD` and `HHMMSS`, and the `sap_dats` and `sap_tims` normalizers turn them into dates and times. Keys come from the dictionary (and from `SYS.CONSTRAINTS` on HANA). Foreign keys and currency and unit references are available to plugins through `connector.ddic_table(name)` (`sap_hana`) and `connector.remote_table(name).ddic` (`sap_rfc`); schema checks do not use them yet.

## SAP normalizers

Add normalizers to keys and column mappings. They apply wherever the column type fits (a string normalizer leaves a typed target column alone), so the same list can stand on both sides of a mapping.

```yaml
keys:
  - { source: KUNNR, target: CUSTOMER_ID, normalize: [sap_alpha] }
columns:
  mappings:
    - { source: ERDAT, target: CREATED_DATE, normalize: [sap_dats] }
    - { source: WRBTR, target: AMOUNT, normalize: [{sap_curr: {currency_column: WAERS}}] }
```

Common rules:

- **Trailing spaces.** SAP `CHAR` values are blank-padded on some databases. The normalizers ignore trailing spaces (U+0020) when they interpret a value. For plain `CHAR` columns without a normalizer, set `defaults.normalization.trim: rtrim` (or add `rtrim` to a mapping).
- Text is matched exactly (case-sensitively), and "digits" means `0` to `9` only.
- Values a normalizer does not convert pass through unchanged, unless the table below says otherwise.
- Conversions that could fail run only on validated values, inside `CASE`, so an invalid value never aborts a query.
- On Oracle, empty strings are NULL; this affects blanks (see `sap_bool`'s `null_as_false`).

### `sap_alpha`: ALPHA conversion exit

For keys such as KUNNR, LIFNR, MATNR, AUFNR, VBELN, and BELNR. Only digits-only values convert (spaces around them are ignored); `ABC-1` or `12 34` stay as they are, as in SAP.

| Parameter | Default | Meaning |
|---|---|---|
| `mode` | `strip` | `strip` removes leading zeros (all zeros become `0`); `pad` pads with zeros to `length`, like `CONVERSION_EXIT_ALPHA_INPUT`. `{sap_alpha: pad}` is a shorthand. |
| `length` | `10` | Field length for `pad`. Digits-only values longer than it keep their significant digits. |

| Input | `strip` | `{mode: pad, length: 10}` |
|---|---|---|
| `0000012345` | `12345` | `0000012345` |
| ` 12345 ` | `12345` | `0000012345` |
| `0000000000` | `0` | `0000000000` |
| `ABC-1` | `ABC-1` | `ABC-1` |

`strip` is the length-independent form: use it when one side stores the numbers without zeros (or as integers converted to text).

### `sap_matnr_length`: material numbers from ECC and S/4HANA

ECC stores MATNR in 18 characters, S/4HANA in 40. S/4HANA still pads digits-only material numbers to 18 unless its lexicographical setting is on. This normalizer strips leading zeros from digits-only material numbers and pads them to `length`, so `000000000000012345`, a 40-character zero-padded number, and `12345` all compare equal.

| Parameter | Default | Meaning |
|---|---|---|
| `length` | `18` | `18` or `40`. `{sap_matnr_length: 40}` is a shorthand. |
| `mode` | `pad` | `pad`, or `strip` for the length-independent form. |

With the lexicographical setting on, leading zeros are significant (`00123` and `123` are different materials): do not normalize those keys.

### `sap_numc`: numeric text

NUMC fields hold digits only, zero-padded to the field length (POSNR, GJAHR, MONAT, BUZEI). Spaces around the digits are ignored, and a blank value is SAP's initial value, all zeros.

| Parameter | Default | Meaning |
|---|---|---|
| `length` | none | With a length, values are padded to it (`{sap_numc: 6}`: `10` becomes `000010`). Without one, leading zeros are stripped (`000010` becomes `10`), which compares NUMC text with numbers. |
| `invalid` | `keep` | Values with other characters: `keep` them unchanged, or turn them into `null`. |

### `sap_dats`: dates

`YYYYMMDD` text to DATE. `00000000`, blank, and NULL become NULL (SAP's initial date). Years 0001 to 9999 and real calendar days (leap years included) are valid. Integer columns (`20240131` stored as a number) are read as eight-digit text.

| Parameter | Default | Meaning |
|---|---|---|
| `invalid` | `null` | What invalid values (`20230230`, `2023AB01`, a wrong length) become: `null`, or `sentinel`. |
| `sentinel` | `0001-01-01` | Date for invalid values when `invalid: sentinel`. |

Invalid dates never abort a query: the SQL checks the digits, month, and day with string comparisons first and converts only values that passed. With `invalid: null`, an invalid source date equals a NULL target; choose `invalid: sentinel` to see them as differences. A `regex` check on the raw column (`pattern: "^[0-9]{8}$"`) lists values with a wrong length or characters, though not impossible days such as `20230230`.

### `sap_tims`: times

`HHMMSS` text to TIME (whole seconds). Blank and NULL become NULL. `000000` and up to `235959` are valid; `240000`, `12AB00`, or a wrong length become NULL without aborting the query. Integer columns are read as six-digit text.

| Parameter | Default | Meaning |
|---|---|---|
| `zero_as_null` | `true` | Treat `000000` (SAP's initial time) as NULL. It is also midnight: set `false` when the field holds real midnight times. |

### `sap_bool`: flags

`X` is true and blank is false.

| Parameter | Default | Meaning |
|---|---|---|
| `true_values` | `[X]` | Values that mean true, after removing trailing spaces. |
| `false_values` | `[""]` | Values that mean false; `""` stands for blank (empty or spaces only). |
| `other` | `null` | Other values (`x`, `Y`, `1`): `null`, so they differ from a boolean target, or `false` (ABAP's `flag = abap_true` test). |
| `null_as_false` | `false` | Treat NULL as false. HANA holds NULL in columns added to a table after rows existed, and Oracle stores blanks as NULL. |

A lowercase `x` is not true unless you list it: `{sap_bool: {true_values: [X, x]}}`.

### `sap_spras`: language keys

SAP's internal one-character language keys (SPRAS) to the two-character codes of table T002 (LAISO), or back.

| Parameter | Default | Meaning |
|---|---|---|
| `direction` | `to_iso` | `to_iso` (`E` becomes `EN`) or `to_sap` (`EN` and `en` become `E`). `{sap_spras: to_sap}` is a shorthand. |
| `overrides` | `{}` | Entries that replace or extend the built-in table, as `{internal: code}`, for example `{Z: Z1}`. |
| `unknown` | `keep` | Values not in the table: `keep` them unchanged, or `null`. |

Values already in the other form pass through, so the normalizer can stand on both sides. Internal keys are case-sensitive (`a` is Afrikaans, `A` Arabic). The built-in table holds SAP's delivered languages: `0` SR, `1` ZH, `2` TH, `3` KO, `4` RO, `5` SL, `6` HR, `7` MS, `8` UK, `9` ET, `A` AR, `B` HE, `C` CS, `D` DE, `E` EN, `F` FR, `G` EL, `H` HU, `I` IT, `J` JA, `K` DA, `L` PL, `M` ZF (traditional Chinese), `N` NL, `O` NO, `P` PT, `Q` SK, `R` RU, `S` ES, `T` TR, `U` FI, `V` SV, `W` BG, `X` LT, `Y` LV, `a` AF, `b` IS, `c` CA, `d` SH (Serbian, Latin script), `i` ID. Compare with your system: `SELECT SPRAS, LAISO FROM T002`.

### `sap_curr`: currency amounts

SAP stores amounts with the decimals of the amount field (2), whatever the currency: 100 JPY is stored as `1.00`, and 1.234 KWD as `12.34`. `sap_curr` multiplies the stored value by 10 to the power of (field decimals minus currency decimals) on the side that holds SAP-internal amounts.

| Parameter | Default | Meaning |
|---|---|---|
| `currency_column` | none | Column of the same row holding the currency key, on the source side (for example `WAERS`). |
| `target_currency_column` | `currency_column` | The currency column on the target side, when it has another name. |
| `currency` | none | A constant currency for every row. Set this or `currency_column`. |
| `side` | `source` | The side that stores internal amounts: `source`, `target`, or `both` (SAP to SAP). |
| `decimals` | `{}` | Currency decimals (TCURX.CURRDEC) that replace or extend the built-in table, for example `{CLF: 4}`. |
| `default_decimals` | `2` | Decimals of currencies not in the table (SAP's rule). |
| `field_decimals` | `2` | Decimals of the amount field's domain. |

| Stored | Currency | Result |
|---|---|---|
| `1.00` | JPY | `100.000` |
| `12.34` | KWD | `1.234` |
| `12.34` | EUR | `12.340` |
| `12.34` | blank, unknown, or NULL | `12.340` |

Currencies are matched exactly after removing trailing spaces. The built-in table follows ISO 4217 minor units for currencies without two decimals: 0 for BIF, CLP, DJF, GNF, ISK, JPY, KMF, KRW, PYG, RWF, UGX, UYI, VND, VUV, XAF, XOF, XPF and the legacy ADP, BEF, ESP, ITL, LUF, PTE, TRL; 3 for BHD, IQD, JOD, KWD, LYD, OMR, TND. Your system's TCURX is what counts: compare it with `SELECT CURRKEY, CURRDEC FROM TCURX` and add the differences to `decimals`.

The currency column must be in the same dataset. When it lives in a header table (BKPF for BSEG), use a `query` dataset that joins it. Results have the scale of the largest currency decimals (3 by default). On SQLite, amounts are multiplied in floating point and stay exact up to about 15 significant digits.

### `sap_quan`: quantities

Rounds quantities to the decimals of their unit of measure (T006.DECAN), half away from zero.

| Parameter | Default | Meaning |
|---|---|---|
| `unit_column` | none | Column of the same row holding the unit, on the source side (for example `MEINS`). |
| `target_unit_column` | `unit_column` | The unit column on the target side. |
| `unit` | none | A constant unit for every row. Set this or `unit_column`. |
| `decimals` | `{}` | Decimals per unit, for example `{ST: 0, KG: 3}`. Read yours with `SELECT MSEHI, DECAN FROM T006`. |
| `default_decimals` | none | Decimals for units not in `decimals`; unset leaves those values unrounded. |
| `side` | `both` | Sides to round. |

Units are matched exactly after removing trailing spaces; list commercial codes too (`PC` as well as `ST`) when the target holds them.

## Business object templates

The pack ships configuration fragments for common SAP reconciliations:

| Template | What it compares |
|---|---|
| `cvi_business_partner` | Customers (KNA1) and vendors (LFA1) against their Business Partner links (CVI_CUST_LINK, CVI_VEND_LINK): counts, unlinked records, links without a BUT000 partner |
| `gl_balances` | GL balances per company code, fiscal year, period, and account (FAGLFLEXA against ACDOCA by default), within a tolerance, plus a trial balance check on each side |
| `open_items` | Customer (BSID) and vendor (BSIK) open items line by line per company code, with ALPHA, DATS, and currency normalizers |
| `material_master` | MARA, MARC per plant, and MVKE per sales organization, with material numbers normalized for the 18 to 40 character change |

List them and copy one next to your configuration:

```bash
dvf sap templates                          # names, descriptions, and the variables each needs
dvf sap copy-template gl_balances --to sap
```

Then extend it and set its variables:

```yaml
version: 1
extends: [sap/gl_balances.yaml]
project: { name: ecc-to-s4-finance }
vars:
  sap_connection: ecc
  target_connection: s4
  source_table: FAGLFLEXA
  target_table: ACDOCA
  ledger: "0L"
  company_codes: "'1000', '2000'"
  fiscal_year: "2025"
  amount_tolerance: 0.005
connections:
  ecc: { type: sap_hana, host: ..., schema: SAPSR3, client: "100" }
  s4:  { type: sap_hana, host: ..., schema: SAPHANADB, client: "100" }
```

Each template's header explains its variables. Templates hold whole suites; lists replace lists when files merge, so add your own suites in a second extended file or copy and edit the template. The `gl_balances` template compares SQL queries, so both of its connections must run SQL (`sap_hana`, or a database holding extracted tables). With `sap_rfc` sources, move filters into `options.where`.

## Known limits

- The connectors have not yet been verified against a live SAP system; report problems with the SQL or RFC calls dvf sends (`dvf explain` shows the SQL).
- Staged `sap_rfc` schemas do not carry field descriptions into reports; `connector.remote_table(name)` has them.
- RFC reads without a stable order fail when pages overlap rather than risk duplicates; very large tables need `sap_hana`, extracts, a custom read module, or OData.
- Foreign keys from DD05S/DD08L are read but not yet used by schema checks.
