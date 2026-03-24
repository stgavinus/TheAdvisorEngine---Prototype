import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin

from src.scraper import ProgramScraper, CourseDetailScraper
from src.database import DatabaseManager
from src.models import CourseDetail

BASE_URL = "https://catholic.smartcatalogiq.com"

CATEGORIES = {
    "Bachelor Degrees": "undergraduate-programs/bachelor-degree-programs",
    "Associate Degrees": "undergraduate-programs/associate-degree-programs",
    "Minors":           "undergraduate-programs/undergraduate-minors",
    "Certificates":     "undergraduate-programs/undergraduate-certificates",
}


def _catalog_base(year: str) -> str:
    return f"{BASE_URL}/en/{year}/undergraduate-announcements"


def discover_programs(year: str) -> list[dict]:
    from src.scraper import _get_soup
    base = _catalog_base(year)
    programs, seen = [], set()

    for category, path in CATEGORIES.items():
        url  = f"{base}/{path}"
        soup = _get_soup(url)
        if not soup:
            print(f"  Could not reach {category} index")
            continue

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/undergraduate-programs/" not in href:
                continue
            if len(href.rstrip("/").split("/")) < 7:
                continue
            full_url = urljoin(BASE_URL, href).rstrip("/")
            if full_url in seen:
                continue
            seen.add(full_url)
            programs.append({
                "name":     a.get_text(strip=True),
                "category": category,
                "url":      full_url,
                "slug":     href.rstrip("/").split("/")[-1],
            })

    return programs


def stage1(programs: list[dict], year: str, workers: int, db: DatabaseManager):
    print(f"\nStage 1: Scraping {len(programs)} programs ({workers} workers)...")
    all_slugs = {p["slug"] for p in programs}
    scraper   = ProgramScraper(all_slugs)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(scraper.scrape, p["name"], p["category"], p["url"], p["slug"], year): p
            for p in programs
        }
        for i, future in enumerate(as_completed(futures), 1):
            p = futures[future]
            try:
                program = future.result()
                db.save_program(program)
                block_count  = len(program.blocks)
                course_count = sum(len(b.courses) for b in program.blocks)
                print(f"  [{i:>3}/{len(programs)}] {program.status:<10} {program.name} "
                      f"({block_count} blocks, {course_count} courses)")
            except Exception as e:
                print(f"  [{i:>3}/{len(programs)}] ERROR  {p['name']}: {e}")


def stage2(workers: int, db: DatabaseManager):
    course_urls = db.get_course_urls()
    if not course_urls:
        print("\nStage 2: No course URLs found, skipping.")
        return

    print(f"\nStage 2: Scraping details for {len(course_urls)} courses ({workers} workers)...")
    scraper = CourseDetailScraper()
    batch: list[CourseDetail] = []
    BATCH_SIZE = 100

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(scraper.scrape, code, url): code for code, url in course_urls}
        for i, future in enumerate(as_completed(futures), 1):
            code = futures[future]
            try:
                detail = future.result()
                if detail:
                    batch.append(detail)
            except Exception as e:
                print(f"  Course {code}: {e}")

            if len(batch) >= BATCH_SIZE:
                db.save_course_details(batch)
                batch.clear()
                print(f"  {i}/{len(course_urls)} course details saved...")

    if batch:
        db.save_course_details(batch)
    print(f"  Done.")


def main():
    parser = argparse.ArgumentParser(description="CUA Advisor Engine scraper")
    parser.add_argument("--year",    default="2025-2026", help="Catalog year (default: 2025-2026)")
    parser.add_argument("--workers", type=int, default=10, help="Parallel workers (default: 10)")
    parser.add_argument("--major",   help="Scrape a single program by slug")
    parser.add_argument("--no-details", action="store_true", help="Skip stage 2 course detail scraping")
    parser.add_argument("--details-only", action="store_true", help="Skip stage 1, run stage 2 only")
    args = parser.parse_args()

    db = DatabaseManager(Path("data/cua_catalog.db"))
    db.setup()

    if not args.details_only:
        programs = discover_programs(args.year)
        if not programs:
            print("No programs found. Check the catalog URL or year.")
            return

        if args.major:
            target = next((p for p in programs if p["slug"] == args.major), None)
            if not target:
                print(f"Slug '{args.major}' not found.")
                return
            programs = [target]

        stage1(programs, args.year, args.workers, db)

    if not args.no_details:
        stage2(args.workers, db)

    print(f"\nDone. Database: data/cua_catalog.db")


if __name__ == "__main__":
    main()
