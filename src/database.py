import sqlite3
from pathlib import Path
from src.models import Program, CourseDetail

class DatabaseManager:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    def setup(self):
        self.db_path.parent.mkdir(exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS programs (
                id       INTEGER PRIMARY KEY,
                year     TEXT NOT NULL,
                name     TEXT NOT NULL,
                slug     TEXT NOT NULL,
                category TEXT NOT NULL,
                url      TEXT NOT NULL,
                status   TEXT NOT NULL DEFAULT 'COMPLETE',
                UNIQUE(slug, year)
            );

            CREATE TABLE IF NOT EXISTS requirement_blocks (
                id          INTEGER PRIMARY KEY,
                program_id  INTEGER NOT NULL REFERENCES programs(id),
                title       TEXT NOT NULL,
                instruction TEXT NOT NULL DEFAULT '',
                notes       TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS courses (
                id             INTEGER PRIMARY KEY,
                block_id       INTEGER NOT NULL REFERENCES requirement_blocks(id),
                code           TEXT NOT NULL DEFAULT '',
                title          TEXT NOT NULL,
                credits        TEXT NOT NULL DEFAULT '',
                url            TEXT NOT NULL DEFAULT '',
                is_placeholder BOOLEAN NOT NULL DEFAULT 0,
                source         TEXT NOT NULL DEFAULT 'table'
            );

            CREATE TABLE IF NOT EXISTS course_details (
                code          TEXT PRIMARY KEY,
                description   TEXT NOT NULL DEFAULT '',
                prerequisites TEXT NOT NULL DEFAULT '',
                cross_listed  TEXT NOT NULL DEFAULT '',
                url           TEXT NOT NULL DEFAULT ''
            );
        """)
        conn.commit()
        conn.close()

    def save_program(self, program: Program):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO programs (year, name, slug, category, url, status)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(slug, year) DO UPDATE SET
                    name     = excluded.name,
                    category = excluded.category,
                    url      = excluded.url,
                    status   = excluded.status
            """, (program.year, program.name, program.slug,
                  program.category, program.url, program.status))

            pid = cur.execute(
                "SELECT id FROM programs WHERE slug = ? AND year = ?",
                (program.slug, program.year)
            ).fetchone()[0]

            # Replace blocks and courses for this program on re-run
            cur.execute(
                "DELETE FROM courses WHERE block_id IN "
                "(SELECT id FROM requirement_blocks WHERE program_id = ?)", (pid,)
            )
            cur.execute("DELETE FROM requirement_blocks WHERE program_id = ?", (pid,))

            for block in program.blocks:
                cur.execute("""
                    INSERT INTO requirement_blocks (program_id, title, instruction, notes)
                    VALUES (?, ?, ?, ?)
                """, (pid, block.title, block.instruction, block.notes))
                bid = cur.lastrowid

                for c in block.courses:
                    cur.execute("""
                        INSERT INTO courses
                            (block_id, code, title, credits, url, is_placeholder, source)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (bid, c.code, c.title, c.credits,
                          c.url, c.is_placeholder, c.source))

            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"DB Error saving {program.name}: {e}")
        finally:
            conn.close()

    def save_course_details(self, details: list[CourseDetail]):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        try:
            cur.executemany("""
                INSERT INTO course_details (code, description, prerequisites, cross_listed, url)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    description   = excluded.description,
                    prerequisites = excluded.prerequisites,
                    cross_listed  = excluded.cross_listed,
                    url           = excluded.url
            """, [(d.code, d.description, d.prerequisites, d.cross_listed, d.url)
                  for d in details])
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"DB Error saving course details: {e}")
        finally:
            conn.close()

    def get_course_urls(self) -> list[tuple[str, str]]:
        """Return (code, url) pairs for all courses that have a detail page URL."""
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT DISTINCT code, url FROM courses WHERE url != '' AND code != ''"
            ).fetchall()
        finally:
            conn.close()
