from dataclasses import dataclass
from typing import Any, List

import psycopg2
from psycopg2 import errors as psycopg2_errors
from psycopg2.extras import RealDictCursor
from psycopg2.pool import SimpleConnectionPool
from tenacity import retry, stop_after_attempt, wait_exponential

from src.exceptions import ConnectionError, PermissionError, QueryError
from src.logger import logger
from src.settings import settings


@dataclass
class QueryResult:
    """Represents a query result with metadata."""

    rows: List[dict[str, Any]]
    count: int
    status: str


class SupabaseClient:
    """Connects to Supabase PostgreSQL database directly."""

    _instance = None  # Singleton instance

    def __init__(self):
        """Initialize the PostgreSQL connection pool."""
        self._pool = None
        self.db_url = self._get_db_url_from_supabase()

    def _get_db_url_from_supabase(self) -> str:
        """Create PostgreSQL connection string from settings."""
        if settings.supabase_project_ref.startswith("127.0.0.1"):
            # Local development
            return f"postgresql://postgres:{settings.supabase_db_password}@{settings.supabase_project_ref}/postgres"

        # Production Supabase
        return (
            f"postgresql://postgres.{settings.supabase_project_ref}:{settings.supabase_db_password}"
            f"@aws-0-us-east-1.pooler.supabase.com:6543/postgres"
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=15),
    )
    def _get_pool(self):
        """Get or create PostgreSQL connection pool with better error handling."""
        if self._pool is None:
            try:
                logger.debug(f"Creating connection pool for: {self.db_url.split('@')[1]}")
                self._pool = SimpleConnectionPool(minconn=1, maxconn=10, cursor_factory=RealDictCursor, dsn=self.db_url)
                # Test the connection
                with self._pool.getconn() as conn:
                    self._pool.putconn(conn)
                logger.info("✓ Created PostgreSQL connection pool")
            except psycopg2.OperationalError as e:
                logger.error(f"Failed to connect to database: {e}")
                raise ConnectionError(f"Could not connect to database: {e}")
            except Exception as e:
                logger.exception("Unexpected error creating connection pool")
                raise ConnectionError(f"Unexpected connection error: {e}")
        return self._pool

    @classmethod
    def create(cls) -> "SupabaseClient":
        """Create and return a configured SupabaseClient instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def close(self):
        """Explicitly close the connection pool."""
        if self._pool is not None:
            try:
                self._pool.closeall()
                self._pool = None
                logger.info("Closed PostgreSQL connection pool")
            except Exception as e:
                logger.error(f"Error closing connection pool: {e}")

    def readonly_query(self, query: str, params: tuple = None) -> QueryResult:
        """Execute a SQL query and return structured results.

        Args:
            query: SQL query to execute
            params: Optional query parameters to prevent SQL injection

        Returns:
            QueryResult containing rows and metadata

        Raises:
            ConnectionError: When database connection fails
            QueryError: When query execution fails (schema or general errors)
            PermissionError: When user lacks required privileges
        """
        if self._pool is None:
            # Reinitialize pool if it was closed
            self._pool = self._get_pool()

        pool = self._get_pool()
        conn = pool.getconn()
        try:
            conn.set_session(readonly=True)
            with conn.cursor() as cur:
                try:
                    cur.execute("BEGIN TRANSACTION READ ONLY")
                    cur.execute(query, params)
                    rows = cur.fetchall() or []
                    status = cur.statusmessage
                    conn.commit()
                    return QueryResult(rows=rows, count=len(rows), status=status)
                except psycopg2_errors.InsufficientPrivilege as e:
                    logger.error(f"Permission denied: {e}")
                    raise PermissionError(f"Access denied: {str(e)}")
                except (psycopg2_errors.UndefinedTable, psycopg2_errors.UndefinedColumn) as e:
                    logger.error(f"Schema error: {e}")
                    raise QueryError(str(e))
                except psycopg2.Error as e:
                    logger.error(f"Database error: {e.pgerror}")
                    raise QueryError(f"Query failed: {str(e)}")
                finally:
                    conn.rollback()  # Always rollback READ ONLY transaction
        finally:
            if pool and conn:
                pool.putconn(conn)
