"""Apache Spark SQL dialect (Spark 4.x; the connector and engine need PySpark 4.0 or later).

Spark SQL is the language of the Databricks runtime, so this dialect subclasses
:class:`~dvf.sql.databricks.DatabricksDialect` and changes only what real Spark showed to
differ. Every canonical form is verified against the Python reference on a local Spark session
(``tests/parity/test_spark_parity.py``), with ANSI mode on (the Spark 4 default) and off:

* Identifiers are quoted with backticks. String literals escape quotes and backslashes with a
  backslash (Spark concatenates adjacent literals, so ``'it''s'`` reads ``its``). This needs the
  default ``spark.sql.parser.escapedStringLiterals=false``; the connector refuses sessions that
  turn it on.
* ``TIMESTAMP`` is an instant shown in the session time zone, and ``TIMESTAMP_NTZ`` is a wall-clock
  time. dvf's sessions use ``spark.sql.session.timeZone=UTC``, and timestamp canonicalization
  relies on it. Named time zones (``normalization.timezone``) use ``convert_timezone``, which
  resolves non-existent local times like Python's ``zoneinfo``.
* ``UPPER`` applies full case mappings (``ß`` becomes ``SS``). Strings it lengthens are upper-cased
  one character at a time, with characters whose upper case is several characters mapped like
  :func:`~dvf.normalize.canonical.simple_upper`. ``LOWER`` needs only the capital sigma and
  dotted capital I pre-mapping inherited from the Databricks dialect.
* There is no Unicode normalization function. Spark's ``reflect`` could call ICU's normalizer, and
  matches Python exactly, but ``reflect`` is non-deterministic and fails inside join and
  ``EXISTS`` conditions, so canonicalization skips Unicode normalization on Spark and the plan
  notes say so (:meth:`SparkDialect.supports_unicode_form` is false).
* Spark has no usable ``TIME`` type; time canonicalization parses the value's text.
* Decimal-to-string casts are plain (no exponent), and ``ROUND`` rounds half away from zero, as
  the reference does. Under ANSI mode a value that does not fit ``DECIMAL(38, scale)`` raises an
  error instead of becoming NULL.

Known differences from the Python reference: characters whose case pairs are newer than Python's
Unicode tables (Spark's ICU case mappings may follow a newer Unicode version), and floating-point
text (``CAST(double AS STRING)`` writes ``1.0E10`` where Python writes ``10000000000.0``; floats
are compared numerically, so this matters only for floating-point keys).
"""

from __future__ import annotations

from dvf.sql.databricks import DatabricksDialect


class SparkDialect(DatabricksDialect):
    """Spark SQL as run by open-source Apache Spark 4 (local, standalone, YARN, Kubernetes)."""

    name = "spark"


DIALECT = SparkDialect()
