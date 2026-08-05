"""
common.loader — pluggable GCP destination.

In the Mercamio references the destination is hardcoded PostgreSQL
(``_reference/ventas/fruver_ventas_rango.py`` -> ``execute_values`` upsert;
``_reference/margen/cargar_margen.py`` -> ``COPY``). For Dinastia the **GCP
target is not decided yet** (BigQuery vs Cloud SQL Postgres), so loading is
hidden behind the :class:`GcpLoader` interface and selected at runtime from
config (``target.type``).

Both concrete loaders are **STUBS**: they carry the client-init skeleton and the
config keys they will need, but their write path raises ``NotImplementedError``
with a precise TODO. Nothing here connects anywhere until a target is chosen and
the stubs are filled — see ``SCHEMA_NEEDS.md`` ("GCP target question").

GCP client libraries are imported **lazily** (inside methods) so this module
imports cleanly even when neither ``google-cloud-bigquery`` nor the Cloud SQL
connector is installed.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence


class WriteMode(str, Enum):
    """How a batch of rows is written to the target."""
    APPEND = "append"                    # straight insert (no dedupe)
    REPLACE_BY_DATE = "replace_by_date"  # delete the (date[,co]) partition, then insert (margen-style)
    UPSERT = "upsert"                    # merge on the primary/business key (fruver-style)


@dataclass(frozen=True)
class TargetSchema:
    """Describes the destination table so a loader can create/merge it.

    ``columns`` preserves order and maps column name -> portable type token
    ('string' | 'int' | 'numeric' | 'date' | 'timestamp' | 'time' | 'bool').
    Concrete loaders translate these to BigQuery / Postgres types.
    """
    table: str
    columns: dict[str, str]
    primary_key: Sequence[str]
    partition_field: str | None = None  # e.g. 'fecha' for BigQuery day-partitioning


class GcpLoader(ABC):
    """Destination-agnostic loader. Implementations: BigQuery, Cloud SQL PG."""

    def __init__(self, options: Mapping[str, Any]):
        self.options = dict(options)

    @abstractmethod
    def ensure_target(self, schema: TargetSchema) -> None:
        """Create the dataset/schema and table if they do not exist (idempotent)."""

    @abstractmethod
    def load_batch(
        self,
        rows: Sequence[Mapping[str, Any]],
        schema: TargetSchema,
        mode: WriteMode = WriteMode.UPSERT,
    ) -> int:
        """Write one batch of rows; return the number of rows written."""

    def load(
        self,
        rows: Sequence[Mapping[str, Any]],
        schema: TargetSchema,
        mode: WriteMode = WriteMode.UPSERT,
        batch_size: int = 5000,
    ) -> int:
        """Chunk ``rows`` and call :meth:`load_batch` per chunk; return total."""
        total = 0
        for i in range(0, len(rows), batch_size):
            total += self.load_batch(rows[i : i + batch_size], schema, mode)
        return total

    def close(self) -> None:  # optional cleanup hook
        pass


# ---------------------------------------------------------------------------
# BigQuery (STUB)
# ---------------------------------------------------------------------------
class BigQueryLoader(GcpLoader):
    """Load into BigQuery.

    Expected config (``target.bigquery`` in the YAML)::

        project:          ${GCP_PROJECT}
        dataset:          dinastia_ventas
        table:            rentabilidad_linea
        location:         US
        credentials_env:  GOOGLE_APPLICATION_CREDENTIALS   # path to SA json
        write_mode:       upsert

    Auth uses Application Default Credentials from ``credentials_env``.
    """

    def _client(self):  # lazy import so the module loads without the lib
        from google.cloud import bigquery  # noqa: F401  (import-time check)
        return bigquery.Client(
            project=self.options.get("project"),
            location=self.options.get("location"),
        )

    def ensure_target(self, schema: TargetSchema) -> None:
        raise NotImplementedError(
            "TODO(bigquery): create dataset+table if missing.\n"
            "  1. client.create_dataset(dataset, exists_ok=True)\n"
            "  2. map TargetSchema.columns -> bigquery.SchemaField list\n"
            "  3. day-partition on TargetSchema.partition_field (e.g. 'fecha')\n"
            "  Blocked on: confirming BigQuery is the chosen target (SCHEMA_NEEDS.md)."
        )

    def load_batch(self, rows, schema, mode=WriteMode.UPSERT) -> int:
        raise NotImplementedError(
            "TODO(bigquery): implement the write path.\n"
            "  - APPEND         -> client.insert_rows_json(table, rows)  (or load job)\n"
            "  - REPLACE_BY_DATE-> load job with WRITE_TRUNCATE on the date partition\n"
            "  - UPSERT         -> load rows into a temp table, then MERGE on\n"
            "                      TargetSchema.primary_key into the target.\n"
            "  Decide idempotency strategy (BigQuery has no ON CONFLICT).\n"
            "  Blocked on: GCP target decision + final schema (SCHEMA_NEEDS.md)."
        )


# Portable type token -> PostgreSQL column type.
_PG_TYPES = {
    "string": "text",
    "int": "bigint",
    "numeric": "numeric",
    "date": "date",
    "timestamp": "timestamptz",
    "time": "time",
    "bool": "boolean",
}


# ---------------------------------------------------------------------------
# Cloud SQL for PostgreSQL
# ---------------------------------------------------------------------------
class CloudSqlPostgresLoader(GcpLoader):
    """Load into Cloud SQL for PostgreSQL over a direct connection (the Dinastia
    box public IP is whitelisted in the instance's Authorized networks). Mirrors
    the Mercamio destination, so the ``ON CONFLICT`` upsert / day-replace patterns
    from the references carry over. Credentials come from ``${GCP_CLOUDSQL_*}`` env
    via the config — never hardcoded.

    Expected config (``target.cloudsql_postgres`` in the YAML)::

        host:     ${GCP_CLOUDSQL_HOST}      # instance public IP
        port:     5432
        database: ${GCP_CLOUDSQL_DB}
        user:     ${GCP_CLOUDSQL_USER}
        password: ${GCP_CLOUDSQL_PASSWORD}
        sslmode:  require
        schema:   public
        table:    ventas
        write_mode: replace_by_date

    ``REPLACE_BY_DATE`` deletes each ``(pk[0], partition_field)`` partition ONCE
    per run, then appends — so re-running a date range wipes and reloads it
    cleanly (idempotent) without one batch clobbering a previous batch's rows.
    """

    def __init__(self, options):
        super().__init__(options)
        self._conn = None
        self._cleared_partitions: set = set()  # (empresa, fecha) already deleted this run

    # -- driver / connection ------------------------------------------------
    def _psycopg2(self):
        try:
            import psycopg2
            from psycopg2.extras import execute_values
            return psycopg2, execute_values
        except ImportError as exc:  # pragma: no cover - surfaced on the box
            raise ImportError(
                "psycopg2 is required for the Cloud SQL Postgres loader. "
                "Install it with `uv pip install -r requirements.txt` (psycopg2-binary)."
            ) from exc

    def _conn_or_open(self):
        if self._conn is None or getattr(self._conn, "closed", 1):
            psycopg2, _ = self._psycopg2()
            o = self.options
            missing = [k for k in ("host", "database", "user", "password") if not o.get(k)]
            if missing:
                raise ValueError(
                    f"Cloud SQL config incomplete: missing {missing}. Set "
                    f"target.cloudsql_postgres.* (secrets via ${{GCP_CLOUDSQL_*}})."
                )
            self._conn = psycopg2.connect(
                host=str(o["host"]),
                port=int(o.get("port", 5432)),
                dbname=str(o["database"]),
                user=str(o["user"]),
                password=str(o["password"]),
                sslmode=str(o.get("sslmode", "require")),
                connect_timeout=int(o.get("connect_timeout", 15)),
                application_name=str(o.get("app_name", "dinastia_etl")),
            )
        return self._conn

    def _qualified(self, schema: TargetSchema) -> str:
        ns = self.options.get("schema", "public")
        table = self.options.get("table") or schema.table
        return f'"{ns}"."{table}"'

    # -- ensure target ------------------------------------------------------
    def ensure_target(self, schema: TargetSchema) -> None:
        ns = self.options.get("schema", "public")
        pk = set(schema.primary_key)
        cols_ddl = []
        for name, token in schema.columns.items():
            pg_type = _PG_TYPES.get(token, "text")
            not_null = " NOT NULL" if name in pk else ""
            cols_ddl.append(f'    "{name}" {pg_type}{not_null}')
        body = ",\n".join(cols_ddl)
        if schema.primary_key:
            pk_cols = ", ".join(f'"{c}"' for c in schema.primary_key)
            body += f',\n    PRIMARY KEY ({pk_cols})'
        create_table = f'CREATE TABLE IF NOT EXISTS {self._qualified(schema)} (\n{body}\n);'
        conn = self._conn_or_open()
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{ns}";')
            cur.execute(create_table)
        conn.commit()

    # -- load ---------------------------------------------------------------
    def load_batch(self, rows, schema, mode=WriteMode.UPSERT) -> int:
        if not rows:
            return 0
        _, execute_values = self._psycopg2()
        cols = list(schema.columns.keys())
        table = self._qualified(schema)
        conn = self._conn_or_open()
        with conn.cursor() as cur:
            if mode is WriteMode.REPLACE_BY_DATE:
                self._clear_partitions(cur, table, schema, rows)
                self._insert(cur, execute_values, table, cols, rows)
            elif mode is WriteMode.UPSERT:
                self._upsert(cur, execute_values, table, cols, schema.primary_key, rows)
            else:  # APPEND
                self._insert(cur, execute_values, table, cols, rows)
        conn.commit()
        return len(rows)

    def _clear_partitions(self, cur, table, schema, rows) -> None:
        """Delete each (empresa, partition) once per run (delete-then-insert)."""
        emp_col = schema.primary_key[0] if schema.primary_key else None
        date_col = schema.partition_field
        if not date_col:
            return
        for r in rows:
            emp = r.get(emp_col) if emp_col else None
            dt = r.get(date_col)
            key = (emp, dt)
            if key in self._cleared_partitions:
                continue
            if emp_col:
                cur.execute(
                    f'DELETE FROM {table} WHERE "{emp_col}" = %s AND "{date_col}" = %s',
                    (emp, dt),
                )
            else:
                cur.execute(f'DELETE FROM {table} WHERE "{date_col}" = %s', (dt,))
            self._cleared_partitions.add(key)

    def _insert(self, cur, execute_values, table, cols, rows) -> None:
        collist = ", ".join(f'"{c}"' for c in cols)
        values = [[r.get(c) for c in cols] for r in rows]
        execute_values(cur, f'INSERT INTO {table} ({collist}) VALUES %s', values, page_size=1000)

    def _upsert(self, cur, execute_values, table, cols, pk, rows) -> None:
        pkset = set(pk)
        collist = ", ".join(f'"{c}"' for c in cols)
        conflict = ", ".join(f'"{c}"' for c in pk)
        updates = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in cols if c not in pkset)
        sql = (
            f'INSERT INTO {table} ({collist}) VALUES %s '
            f'ON CONFLICT ({conflict}) DO UPDATE SET {updates}'
        )
        values = [[r.get(c) for c in cols] for r in rows]
        execute_values(cur, sql, values, page_size=1000)

    def close(self) -> None:
        if self._conn is not None and not getattr(self._conn, "closed", 1):
            self._conn.close()
        self._conn = None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
_LOADERS = {
    "bigquery": ("target.bigquery", BigQueryLoader),
    "cloudsql_postgres": ("target.cloudsql_postgres", CloudSqlPostgresLoader),
}


def build_loader(config) -> GcpLoader:
    """Select and construct the loader from ``target.type`` in the config.

    Raises a clear error if the target has not been decided yet.
    """
    target_type = config.get("target.type")
    if not target_type:
        raise ValueError(
            "target.type is not set. Choose 'bigquery' or 'cloudsql_postgres' "
            "in the config once the GCP destination is decided (SCHEMA_NEEDS.md)."
        )
    if target_type not in _LOADERS:
        raise ValueError(
            f"Unknown target.type={target_type!r}. Expected one of {list(_LOADERS)}."
        )
    section_key, loader_cls = _LOADERS[target_type]
    return loader_cls(config.section(section_key))
