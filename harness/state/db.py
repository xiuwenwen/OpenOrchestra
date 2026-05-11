from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class StateDB:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        migrations = Path(__file__).with_name("migrations.sql").read_text(encoding="utf-8")
        with self.connect() as conn:
            conn.executescript(migrations)
            self._upgrade_existing_schema(conn)

    def _upgrade_existing_schema(self, conn: sqlite3.Connection) -> None:
        task_columns = self._table_columns(conn, "tasks")
        if "workflow_type" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN workflow_type TEXT")
        if "configuration" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN configuration TEXT")
        phase_columns = self._table_columns(conn, "phases")
        if "loop_type" not in phase_columns:
            conn.execute("ALTER TABLE phases ADD COLUMN loop_type TEXT")
        if "parent_round_id" not in phase_columns:
            conn.execute("ALTER TABLE phases ADD COLUMN parent_round_id INTEGER")
        if "iteration_id" not in phase_columns:
            conn.execute("ALTER TABLE phases ADD COLUMN iteration_id INTEGER")
        event_columns = self._table_columns(conn, "events")
        if event_columns:
            if "trace_id" not in event_columns:
                conn.execute("ALTER TABLE events ADD COLUMN trace_id TEXT")
            if "span_id" not in event_columns:
                conn.execute("ALTER TABLE events ADD COLUMN span_id TEXT")
            if "parent_span_id" not in event_columns:
                conn.execute("ALTER TABLE events ADD COLUMN parent_span_id TEXT")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                task_id TEXT,
                phase TEXT,
                role TEXT,
                agent_id TEXT,
                round_id INTEGER,
                attempt INTEGER,
                event_type TEXT NOT NULL,
                status TEXT,
                message TEXT,
                trace_id TEXT,
                span_id TEXT,
                parent_span_id TEXT,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_task_id ON events(task_id);
            CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at);
            CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
            CREATE INDEX IF NOT EXISTS idx_phases_task_loop ON phases(task_id, loop_type, parent_round_id, iteration_id);
            """
        )

    def _table_columns(self, conn: sqlite3.Connection, table_name: str) -> set[str]:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {str(row["name"]) for row in rows}
