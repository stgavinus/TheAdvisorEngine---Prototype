"""
Schedule scraper — pulls section data from CUA's PeopleSoft Class Search.

This orchestrator:
  1. Opens a single PeopleSoft session to discover available terms and subjects.
  2. Filters terms to those on or before Spring 2027 (term code ≤ 1272).
  3. Fans out across term × subject pairs using a ThreadPoolExecutor.
     Each worker thread creates its own PeopleSoft session so that session
     state (ICSID, ICStateNum, cookies) is never shared.
  4. Persists all scraped sections to data/cua_schedule.db.

Usage:
    python -m src.schedule_main                    # scrape all terms up to Spring 2027
    python -m src.schedule_main --term 1258        # scrape a specific term only
    python -m src.schedule_main --list-terms       # print available terms and exit
    python -m src.schedule_main --subject CSC      # scrape one subject across all terms
    python -m src.schedule_main --workers 3        # control parallelism (default: 3)
"""

import argparse
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.schedule_db import ScheduleDatabaseManager
from src.schedule_scraper import PeopleSoftSession

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH = Path("data/cua_schedule.db")

# PeopleSoft term codes are 4-digit numbers.  Spring 2027 is 1272.
# We keep any term whose code (as an integer) is <= this value.
MAX_TERM_CODE = 1272

# Batch size for DB writes — flush after this many sections
WRITE_BATCH_SIZE = 200


# ---------------------------------------------------------------------------
# Worker function (runs in its own thread)
# ---------------------------------------------------------------------------

def _scrape_term_worker(
    term_code: str,
    term_name: str,
    subjects: list[str],
    db: ScheduleDatabaseManager,
    progress_state: dict,
    total_jobs: int,
    start_time: float,
) -> int:
    """
    Open ONE PeopleSoft session and scrape every subject for a single term.
    Reusing the session avoids the expensive initialise() per subject.

    Returns the total number of sections scraped for this term.
    """
    session = PeopleSoftSession()
    try:
        if not session.initialise():
            print(f"  [WARN] Could not initialise session for {term_name}")
            return 0
    except Exception as e:
        print(f"  [ERROR] Could not initialise session for {term_name}: {e}")
        return 0

    term_total = 0
    for subject in subjects:
        count = 0
        try:
            sections = session.search_subject(term_code, subject)
            if sections:
                db.save_sections(sections)
            count = len(sections)
            term_total += count
        except Exception as e:
            print(f"  [ERROR] {term_name} / {subject}: {e}")
            try:
                new_session = PeopleSoftSession()
                if new_session.initialise():
                    session = new_session
                else:
                    print(f"  [WARN] Session reinit failed for {term_name} — skipping remaining subjects")
                    break
            except Exception as reinit_e:
                print(f"  [ERROR] Session reinit error for {term_name}: {reinit_e} — skipping remaining subjects")
                break

        progress_state["completed"] += 1
        completed = progress_state["completed"]
        elapsed = time.time() - start_time
        rate = completed / elapsed if elapsed > 0 else 0
        remaining = (total_jobs - completed) / rate if rate > 0 else 0
        print(
            f"  [{completed:>{len(str(total_jobs))}}/{total_jobs}]"
            f"  {term_name:<30}  {subject:<6}  {count:>3} sections"
            f"  (ETA: {remaining / 60:.1f} min)"
        )

    return term_total


# ---------------------------------------------------------------------------
# Term / subject discovery (uses a single shared session)
# ---------------------------------------------------------------------------

def discover_terms_and_subjects(
    filter_term: str | None = None,
    filter_subject: str | None = None,
) -> tuple[list[tuple[str, str]], list[str]]:
    """
    Open one PeopleSoft session to:
      - Fetch the list of available terms.
      - Fetch the list of available subjects for the first (most recent) term.

    Returns (terms, subjects).  Both lists are filtered if --term / --subject
    were supplied on the command line.
    """
    print("Connecting to CUA PeopleSoft to discover terms and subjects...")
    session = PeopleSoftSession()
    if not session.initialise():
        print("ERROR: Could not connect to PeopleSoft. Check network and try again.")
        sys.exit(1)

    # --- Terms ---------------------------------------------------------------
    raw_terms = session.get_terms()
    if not raw_terms:
        print("ERROR: No terms returned from PeopleSoft.")
        sys.exit(1)

    # Filter to terms up to and including Spring 2027
    terms = [
        (code, name) for code, name in raw_terms
        if code.isdigit() and int(code) <= MAX_TERM_CODE
    ]
    if filter_term:
        terms = [(c, n) for c, n in terms if c == filter_term]
        if not terms:
            print(f"ERROR: Term code '{filter_term}' not found in available terms.")
            sys.exit(1)

    print(f"  Found {len(terms)} term(s) (of {len(raw_terms)} total)")

    # --- Subjects ------------------------------------------------------------
    # Subjects are relatively stable across terms; fetch them once using the
    # most recent term in our filtered list.
    latest_term = terms[0][0]
    print(f"  Fetching subjects for term {latest_term}...")
    subjects = session.get_subjects(latest_term)

    if not subjects:
        print("ERROR: No subjects returned from PeopleSoft — cannot scrape without a subject list.")
        sys.exit(1)

    if filter_subject:
        subjects = [s for s in subjects if s.upper() == filter_subject.upper()]
        if not subjects:
            print(f"ERROR: Subject '{filter_subject}' not found.")
            sys.exit(1)

    print(f"  Found {len(subjects)} subject(s)")
    return terms, subjects


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="CUA PeopleSoft schedule scraper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--term",
        metavar="CODE",
        help="Scrape only this term (4-digit PeopleSoft code, e.g. 1258)",
    )
    parser.add_argument(
        "--subject",
        metavar="SUBJ",
        help='Scrape only this subject abbreviation (e.g. "CSC")',
    )
    parser.add_argument(
        "--list-terms",
        action="store_true",
        help="Print available terms (filtered to <= Spring 2027) and exit",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=6,
        help="Number of parallel workers / PeopleSoft sessions (default: 6)",
    )
    args = parser.parse_args()

    # --- Handle --list-terms ------------------------------------------------
    if args.list_terms:
        session = PeopleSoftSession()
        if not session.initialise():
            print("ERROR: Could not connect to PeopleSoft.")
            sys.exit(1)
        raw_terms = session.get_terms()
        print("\nAvailable terms (up to Spring 2027):")
        for code, name in raw_terms:
            if code.isdigit() and int(code) <= MAX_TERM_CODE:
                print(f"  {code}  {name}")
        return

    # --- Database setup -----------------------------------------------------
    db = ScheduleDatabaseManager(DB_PATH)
    db.setup()
    print(f"Database initialised at {DB_PATH}")

    # --- Discovery ----------------------------------------------------------
    terms, subjects = discover_terms_and_subjects(
        filter_term=args.term,
        filter_subject=args.subject,
    )

    # Persist term metadata
    db.save_terms([(code, name, 0) for code, name in terms])

    total_jobs = len(terms) * len(subjects)
    print(
        f"\nScraping {len(terms)} term(s) × {len(subjects)} subject(s) "
        f"= {total_jobs} subject/term combos, {args.workers} term(s) in parallel..."
    )

    # --- Parallel scrape — one worker per term ------------------------------
    # Each worker opens ONE session and loops through all subjects for that term,
    # avoiding the per-subject session initialisation overhead.
    total_sections = 0
    start_time = time.time()
    progress_state = {"completed": 0}

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                _scrape_term_worker,
                tc, tn, subjects, db, progress_state, total_jobs, start_time,
            ): (tc, tn)
            for tc, tn in terms
        }

        for future in as_completed(futures):
            tc, tn = futures[future]
            try:
                count = future.result()
            except Exception as e:
                print(f"  [ERROR] term {tn}: {e}")
                count = 0
            total_sections += count

    # --- Summary ------------------------------------------------------------
    elapsed_total = time.time() - start_time
    db_count = db.section_count()
    print(
        f"\nDone in {elapsed_total / 60:.1f} min."
        f"  {total_sections} sections scraped this run,"
        f" {db_count} total in database."
        f"\nDatabase: {DB_PATH.resolve()}"
    )


if __name__ == "__main__":
    main()
