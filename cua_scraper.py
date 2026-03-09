import argparse
import requests
import sqlite3
import re
import time
from pathlib import Path
from bs4 import BeautifulSoup
import pandas as pd
from urllib.parse import urljoin

BASE_URL = "https://catholic.smartcatalogiq.com"
HEADERS = {"User-Agent": "CUA-Requirements-Scraper/3.0 (gavin@yourdomain.com)"}

# Paths relative to the script location
SCRIPT_DIR = Path(__file__).parent
DB_PATH = SCRIPT_DIR / "cua_catalog.db"
CSV_DIR = SCRIPT_DIR / "csvs"

def get_soup(url: str) -> BeautifulSoup:
    """Fetch URL and return BeautifulSoup object with a small delay."""
    time.sleep(0.3)
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except Exception:
        return None

def create_db():
    """Reset and create the database tables."""
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        DROP TABLE IF EXISTS group_courses;
        DROP TABLE IF EXISTS requirement_groups;
        DROP TABLE IF EXISTS programs;
        CREATE TABLE programs (id INTEGER PRIMARY KEY, year TEXT, name TEXT, slug TEXT UNIQUE, url TEXT);
        CREATE TABLE requirement_groups (id INTEGER PRIMARY KEY, program_id INTEGER, group_name TEXT);
        CREATE TABLE group_courses (id INTEGER PRIMARY KEY, group_id INTEGER, code TEXT, title TEXT, credits TEXT, course_url TEXT);
    """)
    conn.commit()
    return conn

def extract_requirements_from_page(soup: BeautifulSoup):
    """Extract requirement groups and courses from a single page soup."""
    if not soup: return []
    page_reqs = []
    
    for heading in soup.find_all(["h1", "h2", "h3", "h4"]):
        group_name = heading.get_text(strip=True)
        
        # Keywords common in requirement headers
        valid_header = re.search(r"Year|Fall|Spring|Take |Elective|Core|Major|Minor|Foundation|Curriculum|Sequence|Plan of Study|Requirements|Concentration|Distribution", group_name, re.I)
        if not valid_header:
            continue

        table = heading.find_next("table")
        if not table:
            continue
        
        prev_h = table.find_previous(["h1", "h2", "h3", "h4"])
        if prev_h != heading:
            continue

        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 3:
                continue
            
            code_td = tds[0]
            title_td = tds[1]
            credits_td = tds[-1]

            code = code_td.get_text(strip=True).replace("\xa0", " ").strip()
            title = title_td.get_text(strip=True).strip()
            credits = credits_td.get_text(strip=True).strip()

            if not code or any(k in code.lower() for k in ["subject", "course number", "number"]):
                continue
            if any(k in title.lower() for k in ["title", "credits"]):
                continue

            link = code_td.find("a") or title_td.find("a")
            course_url = urljoin(BASE_URL, link["href"]) if link and link.get("href") else None

            page_reqs.append({
                "group": group_name,
                "code": code,
                "title": title,
                "credits": credits,
                "course_url": course_url
            })
    return page_reqs

def scrape_program_requirements(program_url: str, program_name: str, current_slug: str, all_slugs: list):
    """Scrape a program by visiting the main page and following likely requirement sub-links recursively."""
    all_requirements = []
    visited_urls = set()
    to_visit = [(program_url, 0)] # (url, depth)
    
    # Get keywords from the program name (e.g., "Education", "Studies", "Nursing")
    # Filter out common small words
    name_keywords = set(re.findall(r"\w{4,}", program_name.lower()))
    
    while to_visit:
        current_url, depth = to_visit.pop(0)
        norm_url = current_url.rstrip("/")
        if norm_url in visited_urls or depth > 3:
            continue
        visited_urls.add(norm_url)

        soup = get_soup(current_url)
        if not soup:
            continue
            
        new_reqs = extract_requirements_from_page(soup)
        all_requirements.extend(new_reqs)
        
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True).lower()
            href = a["href"]
            sub_url = urljoin(current_url, href).split("#")[0].rstrip("/")
            
            # Check for requirement keywords
            has_req_word = any(k in text for k in ["requirements", "curriculum", "sequence", "plan of study", "courses", "liberal arts", "general education"])
            # Check if it mentions a word from the program name
            mentions_program = any(k in text for k in name_keywords)
            
            if "/en/" in sub_url:
                # SMARTER SAFETY:
                # 1. Block if it's explicitly the main page of another major
                is_other_major = False
                for other_slug in all_slugs:
                    if other_slug == current_slug: continue
                    if sub_url.endswith(f"/{other_slug}"):
                        is_other_major = True
                        break
                if is_other_major: continue
                
                # 2. Follow if it's a child of current major OR (has_req_word AND mentions_program)
                is_child = sub_url.startswith(program_url.rstrip("/"))
                
                if is_child or (has_req_word and mentions_program):
                    if sub_url not in visited_urls:
                        to_visit.append((sub_url, depth + 1))

    return all_requirements

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", default="2025-2026")
    args = parser.parse_args()

    conn = create_db()
    cur = conn.cursor()

    print(f"🚀 Starting Scraper v3 (Full Precision Mode) for Catalog Year {args.year}")
    print(f"📂 Output directory: {SCRIPT_DIR}\n")

    CSV_DIR.mkdir(exist_ok=True)

    index_url = f"{BASE_URL}/en/{args.year}/undergraduate-announcements/undergraduate-programs/bachelor-degree-programs"
    print(f"🔍 Fetching program index...")
    soup = get_soup(index_url)
    if not soup:
        print("❌ Could not reach index page.")
        return

    seen_urls = set()
    programs = []
    all_slugs = []
    
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "bachelor-degree-programs/" in href and len(href.split("/")) > 6:
            full_url = urljoin(BASE_URL, href).rstrip("/")
            if full_url not in seen_urls:
                seen_urls.add(full_url)
                name = a.get_text(strip=True)
                slug = href.rstrip("/").split("/")[-1]
                programs.append({"name": name, "url": full_url, "slug": slug})
                all_slugs.append(slug)

    print(f"✅ Found {len(programs)} unique programs. Beginning extraction...\n")

    for i, prog in enumerate(programs, 1):
        print(f"[{i}/{len(programs)}] → {prog['name']}")
        reqs = scrape_program_requirements(prog["url"], prog["name"], prog["slug"], all_slugs)

        unique_reqs = []
        seen_reqs = set()
        for r in reqs:
            key = (r["group"], r["code"], r["title"])
            if key not in seen_reqs:
                seen_reqs.add(key)
                unique_reqs.append(r)

        cur.execute("INSERT OR REPLACE INTO programs (year, name, slug, url) VALUES (?, ?, ?, ?)",
                    (args.year, prog["name"], prog["slug"], prog["url"]))
        cur.execute("SELECT id FROM programs WHERE slug=?", (prog["slug"],))
        program_id = cur.fetchone()[0]

        cur.execute("DELETE FROM group_courses WHERE group_id IN (SELECT id FROM requirement_groups WHERE program_id = ?)", (program_id,))
        cur.execute("DELETE FROM requirement_groups WHERE program_id = ?", (program_id,))

        group_ids = {}
        for r in unique_reqs:
            if r["group"] not in group_ids:
                cur.execute("INSERT INTO requirement_groups (program_id, group_name) VALUES (?, ?)",
                            (program_id, r["group"]))
                group_ids[r["group"]] = cur.lastrowid
            
            cur.execute("""INSERT INTO group_courses (group_id, code, title, credits, course_url) 
                           VALUES (?, ?, ?, ?, ?)""",
                        (group_ids[r["group"]], r["code"], r["title"], r["credits"], r["course_url"]))

        df = pd.DataFrame(unique_reqs)
        if not df.empty:
            df.to_csv(CSV_DIR / f"{prog['slug']}.csv", index=False)
            print(f"   ✅ Saved {len(df)} rows")
        else:
            print(f"   ⚠️ No requirements found (Check: {prog['url']})")

        conn.commit()

    print(f"\n📊 Consolidating all data...")
    df_all = pd.read_sql_query("""
        SELECT p.name as program, g.group_name, c.code, c.title, c.credits, c.course_url
        FROM programs p
        JOIN requirement_groups g ON g.program_id = p.id
        JOIN group_courses c ON c.group_id = g.id
        WHERE p.year = ?
        ORDER BY p.name, g.group_name
    """, conn, params=(args.year,))
    
    master_csv_path = SCRIPT_DIR / f"all_requirements_{args.year}.csv"
    df_all.to_csv(master_csv_path, index=False)

    print(f"\n🎉 SUCCESS!")
    print(f"   Total rows captured: {len(df_all)}")
    print(f"   Database: {DB_PATH}")
    print(f"   Master CSV: {master_csv_path}")
    print(f"   Individual CSVs: {CSV_DIR}/")
    
    conn.close()

if __name__ == "__main__":
    main()
