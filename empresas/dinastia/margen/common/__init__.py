"""
Dinastia ETL — shared framework (``common``)
============================================

Reusable building blocks for every Dinastia pipeline (``ventas``, ``rotacion``,
``margen``). Adapted from the Mercamio reference pipeline in
``dinastia-etl/_reference/`` (which is PostgreSQL); here the **source** is the
Siesa/Biable ERP on **MySQL 8.0** (`BD_BIABLE01` @ 192.168.30.1) and the
**destination** is **GCP** (BigQuery or Cloud SQL Postgres — not yet decided).

Modules
-------
- ``common.utils``  : config loading (env-expanded YAML), logging, date helpers,
                      JSON report helpers, the stdout ``RECORDS_MARKER`` contract.
- ``common.db``     : MySQL (pymysql) read-only source layer, config-driven.
- ``common.loader`` : abstract GCP loader interface + BigQuery / Cloud SQL stubs.

Promotion note
--------------
This package currently lives under ``ventas/common`` because it is delivered with
the ventas pipeline (the framework template). To share it with ``rotacion`` and
``margen`` without copy-paste, hoist this directory to ``dinastia-etl/common`` and
install it as an editable/namespace package (or add the repo root to
``PYTHONPATH``). It contains **no ventas-specific logic** so the move is clean.
"""

__all__ = ["utils", "db", "loader"]
__version__ = "0.1.0"
