"""Base classes for plugin components.

The plugin API is ``0.x`` until 1.0 (ADR 0005). Phase 1 adds the execution methods
(``plan()`` and ``evaluate()`` for checks, ``open()``, ``get_schema()`` and friends for
connectors); in Phase 0 these classes carry metadata and configuration models only.
"""

from __future__ import annotations

from typing import ClassVar

from dvf.core.enums import Dimension


class Component:
    """Common metadata for every component class."""

    plugin_name: ClassVar[str] = ""
    """Set by the registration decorators; may be set explicitly for entry points."""

    dvf_plugin_api: ClassVar[str | None] = None
    """Optional version specifier of the plugin API this component supports, such as ``">=0.1,<0.2"``."""


class Check(Component):
    """Base class for checks.

    Subclasses declare a nested ``Config`` model that subclasses
    :class:`dvf.config.checks.CheckConfig`, and a :attr:`dimension`.
    """

    dimension: ClassVar[Dimension] = Dimension.CUSTOM


class Connector(Component):
    """Base class for connectors. Subclasses declare a nested ``Config`` subclassing ``ConnectionConfig``."""


class Comparator(Component):
    """Base class for comparison strategies."""


class Normalizer(Component):
    """Base class for normalizers (SQL, Polars, and Spark implementations arrive in Phase 1)."""


class Reporter(Component):
    """Base class for report renderers."""


class Notifier(Component):
    """Base class for notification channels. Subclasses declare a nested ``Config`` model."""
