import sqlite3
import time
import os
import json

DB_DIR = os.path.expanduser("~/.hydroai")
DB_PATH = os.path.join(DB_DIR, "hydroai.db")
PID_PATH = os.path.join(DB_DIR, "proxy.pid")
SESSION_PATH = os.path.join(DB_DIR, "session.json")


def _ensure_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            tool TEXT NOT NULL DEFAULT 'unknown',
            api TEXT NOT NULL DEFAULT 'other',
            endpoint TEXT NOT NULL DEFAULT '',
            bytes_sent INTEGER NOT NULL DEFAULT 0,
            bytes_received INTEGER NOT NULL DEFAULT 0,
            estimated_input_tokens INTEGER NOT NULL DEFAULT 0,
            estimated_output_tokens INTEGER NOT NULL DEFAULT 0,
            water_ml REAL NOT NULL DEFAULT 0.0,
            duration_sec REAL NOT NULL DEFAULT 0.0,
            session_id TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_usage_timestamp ON usage(timestamp)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_usage_tool ON usage(tool)
    """)
    conn.commit()
    conn.close()


class Storage:
    def __init__(self):
        _ensure_db()
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def record_call(
        self,
        tool: str,
        api: str,
        endpoint: str,
        bytes_sent: int,
        bytes_received: int,
        estimated_input_tokens: int,
        estimated_output_tokens: int,
        water_ml: float,
        duration_sec: float,
        session_id: str = "",
    ):
        self.conn.execute(
            """
            INSERT INTO usage (timestamp, tool, api, endpoint, bytes_sent, bytes_received,
                               estimated_input_tokens, estimated_output_tokens, water_ml,
                               duration_sec, session_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                time.time(),
                tool,
                api,
                endpoint,
                bytes_sent,
                bytes_received,
                estimated_input_tokens,
                estimated_output_tokens,
                water_ml,
                duration_sec,
                session_id,
            ),
        )
        self.conn.commit()

    def get_session_stats(self, session_id: str) -> dict:
        row = self.conn.execute(
            """
            SELECT COUNT(*) as calls,
                   COALESCE(SUM(bytes_sent), 0) as total_bytes_sent,
                   COALESCE(SUM(bytes_received), 0) as total_bytes_received,
                   COALESCE(SUM(water_ml), 0) as total_water_ml,
                   COALESCE(SUM(estimated_input_tokens), 0) as total_input_tokens,
                   COALESCE(SUM(estimated_output_tokens), 0) as total_output_tokens,
                   COALESCE(SUM(duration_sec), 0) as total_duration_sec
            FROM usage WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        return dict(row) if row else {}

    def get_all_stats(self) -> dict:
        row = self.conn.execute(
            """
            SELECT COUNT(*) as calls,
                   COALESCE(SUM(bytes_sent), 0) as total_bytes_sent,
                   COALESCE(SUM(bytes_received), 0) as total_bytes_received,
                   COALESCE(SUM(water_ml), 0) as total_water_ml,
                   COALESCE(SUM(estimated_input_tokens), 0) as total_input_tokens,
                   COALESCE(SUM(estimated_output_tokens), 0) as total_output_tokens,
                   COALESCE(SUM(duration_sec), 0) as total_duration_sec
            FROM usage
            """
        ).fetchone()
        return dict(row) if row else {}

    def get_breakdown(self, column: str = "tool") -> list[dict]:
        rows = self.conn.execute(
            f"""
            SELECT {column} as name,
                   COUNT(*) as calls,
                   COALESCE(SUM(water_ml), 0) as total_water_ml
            FROM usage GROUP BY {column} ORDER BY total_water_ml DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self.conn.close()


def save_pid(pid: int):
    with open(PID_PATH, "w") as f:
        f.write(str(pid))


def load_pid() -> int | None:
    try:
        with open(PID_PATH) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def clear_pid():
    try:
        os.remove(PID_PATH)
    except FileNotFoundError:
        pass


def save_session(session_id: str):
    with open(SESSION_PATH, "w") as f:
        json.dump({"session_id": session_id}, f)


def load_session() -> str | None:
    try:
        with open(SESSION_PATH) as f:
            data = json.load(f)
            return data.get("session_id")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def clear_session():
    try:
        os.remove(SESSION_PATH)
    except FileNotFoundError:
        pass
