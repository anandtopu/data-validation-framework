"""File and lakehouse pack: more file formats and file-level checks.

The formats (ORC, Delta Lake, Iceberg, Avro, Excel, fixed width, XML, and compressed or
transcoded text) are read by the ``files`` connector (:class:`dvf.connectors.duckdb.FileConnector`)
through :mod:`dvf.packs.files.formats`. This module registers the file-level checks
(:mod:`dvf.packs.files.checks`). ``register`` is called by :func:`dvf.packs.register_packs`.
Optional libraries (deltalake, pyiceberg, fastavro, openpyxl, fsspec) are imported only when a
dataset needs them.
"""

from __future__ import annotations

from dvf.plugins.registry import PluginKind, Registry


def _summary(doc: str | None) -> str:
    """The first paragraph of a docstring, on one line."""
    paragraph = (doc or "").strip().split(2 * chr(10), 1)[0]
    return " ".join(paragraph.split())


def register(registry: Registry) -> None:
    """Register the file-level checks (verified against local files: ``stable``)."""
    from dvf.packs.files.checks import FILE_CHECKS

    for name, implementation in FILE_CHECKS.items():
        config_model = implementation.Config  # type: ignore[attr-defined]
        registry.register(
            PluginKind.CHECK,
            name,
            implementation,
            description=_summary(config_model.__doc__),
            config_model=config_model,
            origin="builtin",
        )
