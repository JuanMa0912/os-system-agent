"""
common.db — MySQL (Siesa/Biable ERP) read-only source layer.

The Mercamio references use PostgreSQL via ``psycopg2``
(``_reference/ventas/fruver_ventas_rango.py`` connects with
``psycopg2.connect`` and loads with ``execute_values``). Dinastia's source is
**MySQL 8.0** (`BD_BIABLE01` @ 192.168.30.1), so extraction is rewritten on
``pymysql``.

Design rules (from CLAUDE.md):
- **Read-only**: SELECT only. When ``enforce_read_only`` is set we open each
  session with ``SET SESSION TRANSACTION READ ONLY`` so an accidental write
  fails closed.
- **No hardcoded secrets**: the config dict is built from the env-expanded YAML
  (:class:`common.utils.PipelineConfig`), never from literals in code.
- **Timeouts everywhere**: ``connect_timeout`` and ``read_timeout`` are always
  passed to the driver.
- **Streaming**: :meth:`MySQLSource.stream` uses an unbuffered server-side
  cursor so the heavy "rentabilidad por línea" query does not load the whole
  result set into RAM.

Rows come back as ``dict`` (column name -> value); no pandas dependency.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional, Sequence


def _pymysql():
    """Lazy import of the driver so this module (and ``MySQLConfig`` / dry-run
    wiring) can be used without pymysql installed. A real connection needs it;
    ``uv sync`` installs it on the box (see requirements.txt)."""
    try:
        import pymysql  # noqa: WPS433 (intentional local import)
        return pymysql
    except ImportError as exc:  # pragma: no cover - surfaced at runtime on the box
        raise ImportError(
            "pymysql is required to connect to the MySQL source. Install it with "
            "`uv sync` (or `pip install pymysql`)."
        ) from exc


@dataclass(frozen=True)
class MySQLConfig:
    """Connection parameters for the ERP MySQL source. No secrets are stored on
    disk; ``user`` / ``password`` arrive already expanded from ``${ENV}``."""

    host: str
    database: str
    user: str
    password: str = field(repr=False)  # never echo the password
    port: int = 3306
    charset: str = "utf8mb4"
    connect_timeout: int = 15
    read_timeout: int = 600
    enforce_read_only: bool = True
    app_name: str = "dinastia_etl"

    @classmethod
    def from_config(cls, config, prefix: str = "source.mysql") -> "MySQLConfig":
        """Build from a :class:`common.utils.PipelineConfig` section.

        Required keys under ``prefix``: ``host``, ``database``, ``user``,
        ``password``. ``password``/``user`` should be ``${ENV_VAR}`` in the YAML.
        """
        section = config.section(prefix)
        missing = [k for k in ("host", "database", "user", "password") if not section.get(k)]
        if missing:
            raise ValueError(
                f"MySQL source config incomplete under '{prefix}': missing {missing}. "
                f"Set them in the YAML (secrets via ${{ENV_VAR}})."
            )
        return cls(
            host=str(section["host"]),
            database=str(section["database"]),
            user=str(section["user"]),
            password=str(section["password"]),
            port=int(section.get("port", 3306)),
            charset=str(section.get("charset", "utf8mb4")),
            connect_timeout=int(section.get("connect_timeout", 15)),
            read_timeout=int(section.get("read_timeout", 600)),
            enforce_read_only=bool(section.get("enforce_read_only", True)),
            app_name=str(section.get("app_name", "dinastia_etl")),
        )

    def masked(self) -> str:
        """Human-readable, secret-free description for logs."""
        return f"{self.user}@{self.host}:{self.port}/{self.database} (charset={self.charset})"


class MySQLSource:
    """Thin, read-only wrapper over a pymysql connection to the ERP."""

    def __init__(self, cfg: MySQLConfig):
        self.cfg = cfg

    # -- connection ---------------------------------------------------------
    def _connect(self):
        pymysql = _pymysql()
        conn = pymysql.connect(
            host=self.cfg.host,
            port=self.cfg.port,
            user=self.cfg.user,
            password=self.cfg.password,
            database=self.cfg.database,
            charset=self.cfg.charset,
            connect_timeout=self.cfg.connect_timeout,
            read_timeout=self.cfg.read_timeout,
            autocommit=True,  # SELECT-only workload; no explicit txn management
            program_name=self.cfg.app_name,
        )
        if self.cfg.enforce_read_only:
            # Fail closed: any accidental INSERT/UPDATE/DELETE in this session errors.
            with conn.cursor() as cur:
                cur.execute("SET SESSION TRANSACTION READ ONLY")
        return conn

    @contextmanager
    def connection(self) -> Iterator[Any]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    # -- queries ------------------------------------------------------------
    def fetch_all(self, sql: str, params: Optional[Sequence[Any]] = None) -> list[dict]:
        """Run a SELECT and return all rows as ``list[dict]``.

        Use for small/medium result sets (dimension lookups, counts). For the
        heavy fact query prefer :meth:`stream`.
        """
        cursors = _pymysql().cursors
        with self.connection() as conn:
            with conn.cursor(cursors.DictCursor) as cur:
                cur.execute(sql, params or ())
                return list(cur.fetchall())

    def fetch_one(self, sql: str, params: Optional[Sequence[Any]] = None) -> Optional[dict]:
        cursors = _pymysql().cursors
        with self.connection() as conn:
            with conn.cursor(cursors.DictCursor) as cur:
                cur.execute(sql, params or ())
                return cur.fetchone()

    def count(self, sql: str, params: Optional[Sequence[Any]] = None) -> int:
        """``SELECT count(*) FROM (<sql>) t`` — used by ``--dry-run`` row checks."""
        wrapped = f"SELECT count(*) AS n FROM (\n{sql}\n) AS _dinastia_dryrun"
        row = self.fetch_one(wrapped, params)
        return int(row["n"]) if row else 0

    def stream(
        self,
        sql: str,
        params: Optional[Sequence[Any]] = None,
        batch_size: int = 5000,
    ) -> Iterator[list[dict]]:
        """Yield batches of rows using an unbuffered server-side cursor.

        Memory stays flat regardless of result size — the right tool for the
        line-profitability fact extraction over a date range.
        """
        cursors = _pymysql().cursors
        with self.connection() as conn:
            with conn.cursor(cursors.SSDictCursor) as cur:
                cur.execute(sql, params or ())
                while True:
                    batch = cur.fetchmany(batch_size)
                    if not batch:
                        break
                    yield list(batch)


def build_source(config, prefix: str = "source.mysql") -> MySQLSource:
    """Convenience factory: PipelineConfig -> MySQLSource."""
    return MySQLSource(MySQLConfig.from_config(config, prefix))
