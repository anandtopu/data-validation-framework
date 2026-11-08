"""Discovery and generation tools: ``discover``, ``import-mapping``, ``import-contract``,
``export-contract``, ``profile``, and ``suggest``.

Each module turns metadata (catalogs, mapping sheets, data contracts, or column profiles) into
configuration. Generated files are written with :mod:`dvf.discovery.yamlwriter`, which keeps
comments, so every guess dvf makes (a fuzzy match, a type risk, a suggested check) carries its
evidence next to it.
"""

from __future__ import annotations
