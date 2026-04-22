import os
import sqlite3
from pathlib import Path

_DB = Path("data/users.db")


def init():
    Path("data").mkdir(exist_ok=True)
    conn = sqlite3.connect(_DB)
    os.chmod(_DB, 0o600)
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
        CREATE TABLE IF NOT EXISTS user_sections (
            user_id      TEXT REFERENCES users(id),
            class_number TEXT NOT NULL,
            term_code    TEXT NOT NULL,
            PRIMARY KEY (user_id, class_number, term_code)
        );
        CREATE TABLE IF NOT EXISTS user_plan_courses (
            user_id     TEXT REFERENCES users(id),
            course_code TEXT NOT NULL,
            added_at    TEXT DEFAULT (datetime('now')),
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


def get_planned_sections(uid: str, term_code: str) -> list[str]:
    """Return class_numbers the user has planned for a given term."""
    conn = sqlite3.connect(_DB)
    rows = conn.execute(
        "SELECT class_number FROM user_sections WHERE user_id=? AND term_code=?",
        (uid, term_code),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def add_planned_section(uid: str, class_number: str, term_code: str):
    conn = sqlite3.connect(_DB)
    conn.execute(
        "INSERT OR IGNORE INTO user_sections (user_id, class_number, term_code) VALUES (?,?,?)",
        (uid, class_number, term_code),
    )
    conn.commit()
    conn.close()


def get_plan_courses(uid: str) -> list[str]:
    conn = sqlite3.connect(_DB)
    rows = conn.execute(
        "SELECT course_code FROM user_plan_courses WHERE user_id=? ORDER BY added_at",
        (uid,),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def add_plan_course(uid: str, course_code: str):
    conn = sqlite3.connect(_DB)
    conn.execute(
        "INSERT OR IGNORE INTO user_plan_courses (user_id, course_code) VALUES (?,?)",
        (uid, course_code),
    )
    conn.commit()
    conn.close()


def remove_plan_course(uid: str, course_code: str):
    conn = sqlite3.connect(_DB)
    conn.execute(
        "DELETE FROM user_plan_courses WHERE user_id=? AND course_code=?",
        (uid, course_code),
    )
    conn.commit()
    conn.close()


def remove_planned_section(uid: str, class_number: str, term_code: str):
    conn = sqlite3.connect(_DB)
    conn.execute(
        "DELETE FROM user_sections WHERE user_id=? AND class_number=? AND term_code=?",
        (uid, class_number, term_code),
    )
    conn.commit()
    conn.close()
