import sqlite3
from pathlib import Path

_DB = Path("data/users.db")


def init():
    Path("data").mkdir(exist_ok=True)
    conn = sqlite3.connect(_DB)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id         TEXT PRIMARY KEY,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS user_programs (
            user_id      TEXT REFERENCES users(id),
            program_slug TEXT,
            added_at     TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, program_slug)
        );
        CREATE TABLE IF NOT EXISTS user_completed (
            user_id     TEXT REFERENCES users(id),
            course_code TEXT,
            PRIMARY KEY (user_id, course_code)
        );
    """)
    conn.commit()
    conn.close()


def ensure_user(uid: str):
    conn = sqlite3.connect(_DB)
    conn.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (uid,))
    conn.commit()
    conn.close()


def get_programs(uid: str) -> list[str]:
    conn = sqlite3.connect(_DB)
    rows = conn.execute(
        "SELECT program_slug FROM user_programs WHERE user_id=? ORDER BY added_at", (uid,)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def add_program(uid: str, slug: str):
    conn = sqlite3.connect(_DB)
    conn.execute(
        "INSERT OR IGNORE INTO user_programs (user_id, program_slug) VALUES (?,?)", (uid, slug)
    )
    conn.commit()
    conn.close()


def remove_program(uid: str, slug: str):
    conn = sqlite3.connect(_DB)
    conn.execute(
        "DELETE FROM user_programs WHERE user_id=? AND program_slug=?", (uid, slug)
    )
    conn.commit()
    conn.close()


def get_completed(uid: str) -> set:
    conn = sqlite3.connect(_DB)
    rows = conn.execute(
        "SELECT course_code FROM user_completed WHERE user_id=?", (uid,)
    ).fetchall()
    conn.close()
    return {r[0] for r in rows}


def toggle_completed(uid: str, code: str, checked: bool):
    conn = sqlite3.connect(_DB)
    if checked:
        conn.execute(
            "INSERT OR IGNORE INTO user_completed (user_id, course_code) VALUES (?,?)", (uid, code)
        )
    else:
        conn.execute(
            "DELETE FROM user_completed WHERE user_id=? AND course_code=?", (uid, code)
        )
    conn.commit()
    conn.close()


def clear_completed(uid: str):
    conn = sqlite3.connect(_DB)
    conn.execute("DELETE FROM user_completed WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()
