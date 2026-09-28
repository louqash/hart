"""DuckDB connection manager for the hart database.

Thread safety
-------------
DuckDB's Python API allows a single connection to be used from multiple threads
concurrently -- each thread gets its own implicit transaction.  The ``Database``
class therefore does **not** add an external lock.  If you need strict
serialisation across threads (e.g. for schema migrations), coordinate at the
application level.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

logger = logging.getLogger(__name__)

# Module-level singleton registry.
_instances: dict[str, Database] = {}


class Database:
    """Manages a single DuckDB database file (or in-memory instance).

    Parameters
    ----------
    db_path:
        Filesystem path for the database file.  Use ``":memory:"`` for an
        ephemeral in-memory database (useful for tests).
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path: str = str(db_path)
        self._conn: duckdb.DuckDBPyConnection | None = None
        self._schema_initialised: bool = False

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> Database:
        """Open (or re-open) the DuckDB connection and ensure the schema exists.

        Returns *self* for fluent usage::

            db = Database("hart.duckdb").connect()
        """
        if self._conn is not None:
            return self

        logger.info("Opening DuckDB database at %s", self._db_path)
        self._conn = duckdb.connect(self._db_path)

        if not self._schema_initialised:
            from hart.storage.schema import init_schema

            init_schema(self)
            self._schema_initialised = True

        return self

    @property
    def connection(self) -> duckdb.DuckDBPyConnection:
        """Return the underlying DuckDB connection, connecting lazily."""
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        return self._conn

    def cursor(self) -> Database:
        """Return a :class:`Database` view backed by a new DuckDB cursor.

        A cursor is a separate connection to the same database instance, so
        each thread (web request, job, MCP tool call) can use its own view
        without sharing one connection object across threads.  Closing the
        view closes only the cursor.
        """
        view = Database.__new__(Database)
        view._db_path = self._db_path
        view._conn = self.connection.cursor()
        view._schema_initialised = True
        view._is_cursor = True
        return view

    def close(self) -> None:
        """Close the connection (idempotent)."""
        if self._conn is not None:
            if not getattr(self, "_is_cursor", False):
                logger.info("Closing DuckDB database at %s", self._db_path)
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> Database:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def execute(self, sql: str, params: list[Any] | None = None) -> None:
        """Execute a SQL statement (DDL / DML) without returning results."""
        conn = self.connection
        if params:
            conn.execute(sql, params)
        else:
            conn.execute(sql)

    def executemany(self, sql: str, params_seq: list[list[Any] | tuple[Any, ...]]) -> None:
        """Execute a parameterised statement for every row in *params_seq*.

        This is the preferred method for bulk inserts.
        """
        self.connection.executemany(sql, params_seq)

    def fetchall(self, sql: str, params: list[Any] | None = None) -> list[tuple[Any, ...]]:
        """Run *sql* and return all rows as a list of tuples."""
        conn = self.connection
        if params:
            return conn.execute(sql, params).fetchall()
        return conn.execute(sql).fetchall()

    def fetchone(self, sql: str, params: list[Any] | None = None) -> tuple[Any, ...] | None:
        """Run *sql* and return the first row, or ``None``."""
        conn = self.connection
        if params:
            return conn.execute(sql, params).fetchone()
        return conn.execute(sql).fetchone()

    def fetchdf(self, sql: str, params: list[Any] | None = None) -> pd.DataFrame:
        """Run *sql* and return results as a :class:`pandas.DataFrame`."""
        conn = self.connection
        if params:
            return conn.execute(sql, params).fetchdf()
        return conn.execute(sql).fetchdf()

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def path(self) -> str:
        """Return the database file path."""
        return self._db_path

    @property
    def schema_version(self) -> int | None:
        """Return the latest applied schema version, or ``None`` if unknown."""
        try:
            row = self.fetchone("SELECT MAX(version) FROM schema_version")
            return row[0] if row else None
        except duckdb.CatalogException:
            return None

    def __repr__(self) -> str:
        state = "open" if self._conn is not None else "closed"
        return f"<Database path={self._db_path!r} state={state}>"


# ---------------------------------------------------------------------------
# Module-level singleton accessor
# ---------------------------------------------------------------------------


def get_database(path: str | Path) -> Database:
    """Return a shared :class:`Database` instance for *path*.

    If an instance for this path has already been created it is returned
    directly (singleton pattern).  The connection is opened lazily on first
    query.
    """
    key = str(Path(path).resolve()) if str(path) != ":memory:" else ":memory:"
    if key not in _instances:
        _instances[key] = Database(path).connect()
    return _instances[key]
