"""Result sinks: write every run's results as database tables, for dashboards.

Each sink writes three tables, named with the configured ``table_prefix`` (default ``dvf_``):
``runs`` (one row per run), ``checks`` (one row per check result), and ``column_mismatches`` (one
row per mismatching column of a comparison). Writing a run again replaces its rows. See
:mod:`dvf.sinks._schema` for the columns.

Internal package: sinks are configured under ``sinks:`` and resolved through the plugin registry.
"""
