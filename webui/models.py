import os
import sqlite3

DB_PATH = os.environ.get("DB_PATH", "/data/dns.db")


def get_db(db_path=None):
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path=None):
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            username   TEXT    NOT NULL UNIQUE,
            password   TEXT    NOT NULL,
            is_admin   INTEGER NOT NULL DEFAULT 1,
            created_at TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS dns_records (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT    NOT NULL,
            type       TEXT    NOT NULL,
            value      TEXT    NOT NULL,
            ttl        INTEGER NOT NULL DEFAULT 300,
            priority   INTEGER NOT NULL DEFAULT 0,
            enabled    INTEGER NOT NULL DEFAULT 1,
            created_at TEXT    DEFAULT (datetime('now')),
            updated_at TEXT    DEFAULT (datetime('now')),
            UNIQUE(name, type, value)
        );

        CREATE TRIGGER IF NOT EXISTS dns_records_updated_at
        AFTER UPDATE ON dns_records
        BEGIN
            UPDATE dns_records SET updated_at = datetime('now') WHERE id = NEW.id;
        END;

        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT DEFAULT (datetime('now'))
        );

        INSERT OR IGNORE INTO schema_version (version) VALUES (1);
    """)
    conn.commit()
    conn.close()


# ── Record helpers ─────────────────────────────────────────────────────────────

def get_records_page(conn, page, per_page=50):
    offset = (page - 1) * per_page
    rows = conn.execute(
        "SELECT * FROM dns_records ORDER BY name, type LIMIT ? OFFSET ?",
        (per_page, offset),
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM dns_records").fetchone()[0]
    return rows, total


def get_record(conn, record_id):
    return conn.execute(
        "SELECT * FROM dns_records WHERE id = ?", (record_id,)
    ).fetchone()


def add_record(conn, name, rtype, value, ttl, priority, enabled):
    conn.execute(
        "INSERT INTO dns_records (name, type, value, ttl, priority, enabled) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name, rtype, value, int(ttl), int(priority), 1 if enabled else 0),
    )
    conn.commit()


def update_record(conn, record_id, name, rtype, value, ttl, priority, enabled):
    conn.execute(
        "UPDATE dns_records SET name=?, type=?, value=?, ttl=?, priority=?, enabled=? "
        "WHERE id=?",
        (name, rtype, value, int(ttl), int(priority), 1 if enabled else 0, record_id),
    )
    conn.commit()


def delete_record(conn, record_id):
    conn.execute("DELETE FROM dns_records WHERE id = ?", (record_id,))
    conn.commit()


def toggle_record(conn, record_id):
    conn.execute(
        "UPDATE dns_records SET enabled = 1 - enabled WHERE id = ?", (record_id,)
    )
    conn.commit()


# ── User helpers ───────────────────────────────────────────────────────────────

def get_user_by_username(conn, username):
    return conn.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()


def get_user_by_id(conn, user_id):
    return conn.execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    ).fetchone()


def update_password(conn, user_id, password_hash):
    conn.execute(
        "UPDATE users SET password = ? WHERE id = ?", (password_hash, user_id)
    )
    conn.commit()
