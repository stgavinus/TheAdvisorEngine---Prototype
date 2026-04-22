"""
Schedule units enricher — fetches units (credit hours) for every course in the
schedule DB by clicking through PeopleSoft class detail pages.

The class search results page does NOT include units; they only appear on the
per-section detail page reachable by clicking a class number link.  This script
uses the same PeopleSoft session machinery as the main scraper to:

  1. Query the schedule DB for course_codes with missing units.
  2. Group them by (term_code, subject) so each PeopleSoft session handles one
     subject at a time — minimising session churn.
  3. For each subject/term, search for the subject, map section indices to course
     codes, then click through one detail page per unique course code to read
     "Units: N".
  4. Update ALL sections for that (term_code, course_code) with the found units.

Navigation pattern per course
------------------------------
  search results → MTG_CLASS_NBR$idx → detail page → breadcrumb back
  → blank form → select term → re-search → next MTG_CLASS_NBR$idx → ...

Usage
-----
    python -m src.schedule_enrich                # all terms
    python -m src.schedule_enrich --term 1268    # Fall 2026 only
    python -m src.schedule_enrich --subject HIST # one subject across all terms
"""

import argparse
import re
import sqlite3
import sys
import time
from pathlib import Path

from bs4 import BeautifulSoup

from src.schedule_db import ScheduleDatabaseManager
from src.schedule_scraper import PeopleSoftSession, _unwrap_xml_response

DB_PATH = Path("data/cua_schedule.db")

_UNITS_RE = re.compile(r"Units\s+(\d+(?:\.\d+)?)\s+units?", re.I)
_CODE_RE  = re.compile(r"([A-Z]{2,6})\s+(\d{3,4}[A-Z]?)\b")


def _parse_idx_map(html: str) -> dict[int, tuple[str, str]]:
    """
    Parse a PeopleSoft search results page and return a dict mapping
    global section index → (course_code, class_number).
    These indices correspond to the MTG_CLASS_NBR$N link ids.
    """
    html = _unwrap_xml_response(html)
    soup = BeautifulSoup(html, "html.parser")
    course_divs = soup.find_all(
        "div", {"id": re.compile(r"^win\d+divSSR_CLSRSLT_WRK_GROUPBOX2\$\d+")}
    )
    idx = 0
    result: dict[int, tuple[str, str]] = {}
    for cdiv in course_divs:
        gp = cdiv.find("div", {"id": re.compile(r"GROUPBOX2GP\$\d+")})
        raw = gp.get_text(" ", strip=True) if gp else ""
        m = _CODE_RE.match(raw)
        code = f"{m.group(1)} {m.group(2)}" if m else ""
        sec_tables = cdiv.find_all(
            "table", {"id": re.compile(r"SSR_CLSRCH_MTG1\$scroll\$\d+")}
        )
        for tbl in sec_tables:
            for tr in tbl.find_all("tr")[1:]:
                cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
                if len(cells) >= 2 and re.fullmatch(r"\d{3,8}", cells[1].strip()):
                    result[idx] = (code, cells[1].strip())
                    idx += 1
    return result


def _parse_units_from_detail(html: str) -> str:
    """Extract the units value from a class detail page, e.g. '3' or '1.5'."""
    html = _unwrap_xml_response(html)
    text = BeautifulSoup(html, "html.parser").get_text(" ")
    m = _UNITS_RE.search(text)
    return m.group(1) if m else ""


def _do_search(session: PeopleSoftSession, term_code: str, subject: str) -> str:
    """
    Perform a full term-select + search sequence from a blank form state.
    Returns the raw search results HTML.
    """
    # Select term (advances state and populates subject dropdown)
    session._post_raw({
        "ICAction":                         "CLASS_SRCH_WRK2_STRM$35$",
        "CLASS_SRCH_WRK2_INSTITUTION$31$":  "CRDNL",
        "CLASS_SRCH_WRK2_STRM$35$":         term_code,
    })
    # Submit search
    body = session._post_raw({
        "ICAction":                             "CLASS_SRCH_WRK2_SSR_PB_CLASS_SRCH",
        "CLASS_SRCH_WRK2_INSTITUTION$31$":      "CRDNL",
        "CLASS_SRCH_WRK2_STRM$35$":             term_code,
        "SSR_CLSRCH_WRK_SUBJECT_SRCH$0":        subject,
        "SSR_CLSRCH_WRK_SSR_OPEN_ONLY$chk$3":   "Y",
    })
    # Auto-confirm if PeopleSoft shows a "many results" dialog
    if "#ICSave" in body and "GROUPBOX2" not in body:
        body = session._post_raw({"ICAction": "#ICSave"})
    return body


def _enrich_subject(
    session: PeopleSoftSession,
    conn: sqlite3.Connection,
    term_code: str,
    subject: str,
    needed_codes: set[str],
) -> dict[str, str]:
    """
    For one (term, subject) pair, fetch units for every course_code in
    `needed_codes` and write them to the DB.  Returns a {code: units} dict.
    """
    found: dict[str, str] = {}

    # Initial search to get results
    html = _do_search(session, term_code, subject)
    idx_map = _parse_idx_map(html)

    if not idx_map:
        return found

    # Build: course_code → first section idx (one detail page per course is enough)
    code_to_idx: dict[str, int] = {}
    for idx, (code, _class_num) in idx_map.items():
        if code in needed_codes and code not in code_to_idx:
            code_to_idx[code] = idx

    for code, sec_idx in code_to_idx.items():
        # Click the class number detail link
        detail_html = session._post_raw({"ICAction": f"MTG_CLASS_NBR${sec_idx}"})
        units = _parse_units_from_detail(detail_html)

        if units:
            found[code] = units
            # Update ALL sections for this course+term in the DB
            conn.execute(
                "UPDATE course_sections SET units = ? WHERE course_code = ? AND term_code = ?",
                (units, code, term_code),
            )
            conn.commit()

        # Navigate back to blank search form for the next iteration
        session._post_raw({"ICAction": "pthnavbccrefanc_CLASS_SEARCH_GBL"})

        if len(code_to_idx) > 1 and code != list(code_to_idx.keys())[-1]:
            # Re-search for the next course (not needed after the last one)
            html = _do_search(session, term_code, subject)
            idx_map = _parse_idx_map(html)
            # Refresh indices in case they changed
            for new_idx, (new_code, _) in idx_map.items():
                if new_code in needed_codes and new_code not in found and new_code not in {
                    c for c in list(code_to_idx.keys())[list(code_to_idx.keys()).index(code)+1:]
                    if c in code_to_idx
                }:
                    code_to_idx[new_code] = new_idx

    return found


def main():
    parser = argparse.ArgumentParser(description="Enrich schedule DB with course units")
    parser.add_argument("--term",    metavar="CODE", help="Only enrich this term code")
    parser.add_argument("--subject", metavar="SUBJ", help="Only enrich this subject")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} not found — run schedule_main first")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")

    # Find all (term_code, course_code) pairs that are missing units
    where = "WHERE (units IS NULL OR units = '')"
    params: list = []
    if args.term:
        where += " AND term_code = ?"
        params.append(args.term)
    if args.subject:
        # subject is the prefix of the course code, e.g. "HIST" matches "HIST 208"
        where += " AND course_code LIKE ?"
        params.append(args.subject.upper() + " %")

    rows = conn.execute(
        f"SELECT DISTINCT term_code, course_code FROM course_sections {where} ORDER BY term_code, course_code",
        params,
    ).fetchall()

    if not rows:
        print("All courses already have units — nothing to do.")
        conn.close()
        return

    # Group by (term_code, subject)
    from collections import defaultdict
    groups: dict[tuple[str, str], set[str]] = defaultdict(set)
    for term_code, course_code in rows:
        subject = course_code.split()[0] if " " in course_code else course_code
        groups[(term_code, subject)].add(course_code)

    total_courses = sum(len(v) for v in groups.values())
    print(f"Found {total_courses} course codes missing units across {len(groups)} subject/term groups")

    updated = 0
    failed  = 0

    for (term_code, subject), needed_codes in sorted(groups.items()):
        print(f"\n  {term_code} / {subject:<8}  ({len(needed_codes)} courses)")

        session = PeopleSoftSession()
        try:
            if not session.initialise():
                print(f"    [WARN] Could not initialise session — skipping {subject}")
                failed += len(needed_codes)
                continue
        except Exception as e:
            print(f"    [ERROR] Session init failed for {subject}: {e}")
            failed += len(needed_codes)
            continue

        try:
            found = _enrich_subject(session, conn, term_code, subject, needed_codes)
            for code, units in found.items():
                print(f"    {code}: {units} units")
            updated += len(found)
            missed = needed_codes - set(found)
            if missed:
                print(f"    [WARN] No units found for: {', '.join(sorted(missed))}")
                failed += len(missed)
        except Exception as e:
            print(f"    [ERROR] {subject}: {e}")
            failed += len(needed_codes)

        time.sleep(0.5)  # brief pause between subject sessions

    conn.close()
    print(f"\nDone.  Updated {updated} course codes, {failed} not found.")


if __name__ == "__main__":
    main()
