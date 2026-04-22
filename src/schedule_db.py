import sqlite3
from pathlib import Path
from dataclasses import dataclass, field
from typing import List


# ---------------------------------------------------------------------------
# Dataclasses for schedule data
# ---------------------------------------------------------------------------

@dataclass
class SectionMeeting:
    days: str = ""
    start_time: str = ""
    end_time: str = ""
    room: str = ""
    start_date: str = ""
    end_date: str = ""


@dataclass
class CourseSection:
    course_code: str
    term_code: str
    section_number: str = ""
    class_number: str = ""
    component: str = ""
    status: str = ""
    instruction_mode: str = ""
    units: str = ""
    consent: str = ""
    enrollment_cap: int = 0
    enrollment_total: int = 0
    available_seats: int = 0
    waitlist_cap: int = 0
    waitlist_total: int = 0
    meetings: List[SectionMeeting] = field(default_factory=list)
    instructors: List[str] = field(default_factory=list)


@dataclass
class CourseCatalogExtra:
    course_code: str
    career: str = ""
    grading_basis: str = ""
    add_consent: str = ""
    academic_group: str = ""
    academic_org: str = ""
    campus: str = ""
    components: str = ""
    units: str = ""
    description: str = ""


# ---------------------------------------------------------------------------
# Database manager
# ---------------------------------------------------------------------------

_SCHEMA = """
    CREATE TABLE IF NOT EXISTS terms (
        code       TEXT PRIMARY KEY,
        name       TEXT NOT NULL,
        is_current INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS course_sections (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        course_code      TEXT    NOT NULL,
        term_code        TEXT    NOT NULL,
        section_number   TEXT,
        class_number     TEXT,
        component        TEXT,
        status           TEXT,
        instruction_mode TEXT,
        units            TEXT,
        consent          TEXT,
        enrollment_cap   INTEGER,
        enrollment_total INTEGER,
        available_seats  INTEGER,
        waitlist_cap     INTEGER,
        waitlist_total   INTEGER,
        scraped_at       TEXT    DEFAULT (datetime('now')),
        UNIQUE(class_number, term_code)
    );

    CREATE TABLE IF NOT EXISTS section_meetings (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        section_id INTEGER REFERENCES course_sections(id),
        days       TEXT,
        start_time TEXT,
        end_time   TEXT,
        room       TEXT,
        start_date TEXT,
        end_date   TEXT
    );

    CREATE TABLE IF NOT EXISTS section_instructors (
        section_id      INTEGER REFERENCES course_sections(id),
        instructor_name TEXT,
        PRIMARY KEY (section_id, instructor_name)
    );

    CREATE TABLE IF NOT EXISTS course_catalog_extra (
        course_code    TEXT PRIMARY KEY,
        career         TEXT,
        grading_basis  TEXT,
        add_consent    TEXT,
        academic_group TEXT,
        academic_org   TEXT,
        campus         TEXT,
        components     TEXT,
        units          TEXT,
        description    TEXT
    );
"""


class ScheduleDatabaseManager:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    def setup(self):
        """Create tables if they don't exist. Safe to call on an existing DB."""
        self.db_path.parent.mkdir(exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        # WAL mode allows concurrent readers and a single writer without
        # "database is locked" errors when multiple term-worker threads
        # write simultaneously.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.commit()
        conn.close()

    # ------------------------------------------------------------------
    # Terms
    # ------------------------------------------------------------------

    def save_terms(self, terms: list[tuple[str, str, int]]):
        """
        Upsert a list of (code, name, is_current) tuples into the terms table.
        """
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executemany("""
                INSERT INTO terms (code, name, is_current)
                VALUES (?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    name       = excluded.name,
                    is_current = excluded.is_current
            """, terms)
            conn.commit()
        finally:
            conn.close()

    def get_terms(self) -> list[tuple[str, str, int]]:
        """Return all (code, name, is_current) rows."""
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT code, name, is_current FROM terms ORDER BY code DESC"
            ).fetchall()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Course sections
    # ------------------------------------------------------------------

    def save_sections(self, sections: list[CourseSection]):
        """
        Insert or replace a batch of CourseSection objects, along with their
        associated meetings and instructors.
        """
        if not sections:
            return

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        try:
            for s in sections:
                # RETURNING id avoids a separate SELECT and eliminates the
                # race condition where two concurrent threads could upsert
                # the same section, then both SELECT the same id and
                # stomp each other's meeting/instructor deletes.
                row = cur.execute("""
                    INSERT INTO course_sections (
                        course_code, term_code, section_number, class_number,
                        component, status, instruction_mode, units, consent,
                        enrollment_cap, enrollment_total, available_seats,
                        waitlist_cap, waitlist_total
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(class_number, term_code) DO UPDATE SET
                        course_code      = excluded.course_code,
                        section_number   = excluded.section_number,
                        component        = excluded.component,
                        status           = excluded.status,
                        instruction_mode = excluded.instruction_mode,
                        units            = excluded.units,
                        consent          = excluded.consent,
                        enrollment_cap   = excluded.enrollment_cap,
                        enrollment_total = excluded.enrollment_total,
                        available_seats  = excluded.available_seats,
                        waitlist_cap     = excluded.waitlist_cap,
                        waitlist_total   = excluded.waitlist_total,
                        scraped_at       = datetime('now')
                    RETURNING id
                """, (
                    s.course_code, s.term_code, s.section_number, s.class_number,
                    s.component, s.status, s.instruction_mode, s.units, s.consent,
                    s.enrollment_cap, s.enrollment_total, s.available_seats,
                    s.waitlist_cap, s.waitlist_total,
                )).fetchone()
                sid = row[0]

                # Replace meetings and instructors for this section on re-save
                cur.execute("DELETE FROM section_meetings WHERE section_id = ?", (sid,))
                cur.execute("DELETE FROM section_instructors WHERE section_id = ?", (sid,))

                for m in s.meetings:
                    cur.execute("""
                        INSERT INTO section_meetings
                            (section_id, days, start_time, end_time, room, start_date, end_date)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (sid, m.days, m.start_time, m.end_time, m.room,
                          m.start_date, m.end_date))

                for name in s.instructors:
                    cur.execute("""
                        INSERT OR IGNORE INTO section_instructors (section_id, instructor_name)
                        VALUES (?, ?)
                    """, (sid, name))

            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Catalog extra
    # ------------------------------------------------------------------

    def save_catalog_extras(self, extras: list[CourseCatalogExtra]):
        """Upsert a list of CourseCatalogExtra objects."""
        if not extras:
            return
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executemany("""
                INSERT INTO course_catalog_extra (
                    course_code, career, grading_basis, add_consent,
                    academic_group, academic_org, campus, components, units, description
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(course_code) DO UPDATE SET
                    career         = excluded.career,
                    grading_basis  = excluded.grading_basis,
                    add_consent    = excluded.add_consent,
                    academic_group = excluded.academic_group,
                    academic_org   = excluded.academic_org,
                    campus         = excluded.campus,
                    components     = excluded.components,
                    units          = excluded.units,
                    description    = excluded.description
            """, [
                (e.course_code, e.career, e.grading_basis, e.add_consent,
                 e.academic_group, e.academic_org, e.campus, e.components,
                 e.units, e.description)
                for e in extras
            ])
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Summary helpers
    # ------------------------------------------------------------------

    def section_count(self) -> int:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM course_sections").fetchone()[0]
        finally:
            conn.close()
