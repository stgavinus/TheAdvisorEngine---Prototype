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
HEADERS = {"User-Agent": "CUA-Requirements-Scraper/2.0 (gavin@yourdomain.com)"}

def get_soup(url: str) -> BeautifulSoup:
    time.sleep(0.7)
    r = requests.get(url, headers=HEADERS)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")

def create_db():
    conn = sqlite3.connect("cua_catalog.db")
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

def scrape_requirements(program_url: str):
    soup = get_soup(program_url)
    requirements = []

    # HELPER: Actual scraping logic
    def extract_from_soup(s, url):
        found_any = False
        # Find any heading that looks like a requirement group
        for heading in s.find_all(["h2", "h3", "h4"]):
            group_name = heading.get_text(strip=True)
            if not re.search(r"Year|Fall|Spring|Take |Elective|Core|Major Requirements|Program Requirements|Foundation|Curriculum", group_name, re.I):
                continue

            # Look for the next table after this heading
            table = heading.find_next("table")
            if not table:
                continue
            
            # Ensure this table belongs to THIS heading (not a later one)
            prev_h = table.find_previous(["h2", "h3", "h4"])
            if prev_h != heading:
                continue

            rows = table.find_all("tr")
            for tr in rows: 
                tds = tr.find_all("td")
                if len(tds) < 3:
                    continue
                
                # Dynamic column picking: Credits is usually last
                code_td = tds[0]
                title_td = tds[1]
                credits_td = tds[-1]

                code = code_td.get_text(strip=True)
                title = title_td.get_text(strip=True)
                credits = credits_td.get_text(strip=True)

                # Skip header rows
                if not code or title.lower().startswith("title") or code.lower().startswith("course"):
                    continue

                link = code_td.find("a") or title_td.find("a")
                course_url = urljoin(BASE_URL, link["href"]) if link and link.get("href") else None

                requirements.append({
                    "group": group_name,
                    "code": code,
                    "title": title,
                    "credits": credits,
                    "course_url": course_url
                })
                found_any = True
        return found_any

    # Try scraping the main page first
    has_data = extract_from_soup(soup, program_url)

    # If no data found, check for a "Requirements" sub-link
    if not has_data:
        req_link = soup.find("a", string=re.compile(r"Requirements", re.I))
        if req_link and req_link.get("href"):
            sub_url = urljoin(program_url, req_link["href"])
            # Only follow if it's actually a sub-page (avoids loops)
            if sub_url != program_url:
                sub_soup = get_soup(sub_url)
                extract_from_soup(sub_soup, sub_url)
    
    return requirements

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", default="2025-2026")
    args = parser.parse_args()

    conn = create_db()
    cur = conn.cursor()

    print(f"Fetching all programs for {args.year}...")
    index_url = f"{BASE_URL}/en/{args.year}/undergraduate-announcements/undergraduate-programs/bachelor-degree-programs"
    soup = get_soup(index_url)

    all_links = soup.find_all("a", href=True)
    seen_urls = set()
    programs = []

    for a in all_links:
        href = a["href"]
        # Filter for actual program pages and remove duplicates
        if "bachelor-degree-programs/" in href and len(href.split("/")) > 6:
            full_url = urljoin(BASE_URL, href)
            if full_url not in seen_urls:
                seen_urls.add(full_url)
                name = a.get_text(strip=True)
                slug = href.rstrip("/").split("/")[-1]
                programs.append({"name": name, "url": full_url, "slug": slug})

    print(f"Found {len(programs)} unique programs. Scraping requirements...\n")
    Path("csvs").mkdir(exist_ok=True)

    for prog in programs:
        print(f"→ {prog['name']}")
        reqs = scrape_requirements(prog["url"])

        cur.execute("INSERT OR REPLACE INTO programs (year, name, slug, url) VALUES (?, ?, ?, ?)",
                    (args.year, prog["name"], prog["slug"], prog["url"]))
        cur.execute("SELECT id FROM programs WHERE slug=?", (prog["slug"],))
        program_id = cur.fetchone()[0]

        cur.execute("DELETE FROM requirement_groups WHERE program_id = ?", (program_id,))

        group_ids = {}
        for r in reqs:
            if r["group"] not in group_ids:
                cur.execute("INSERT INTO requirement_groups (program_id, group_name) VALUES (?, ?)",
                            (program_id, r["group"]))
                group_ids[r["group"]] = cur.lastrowid
            cur.execute("INSERT INTO group_courses (group_id, code, title, credits, course_url) VALUES (?, ?, ?, ?, ?)",
                        (group_ids[r["group"]], r["code"], r["title"], r["credits"], r["course_url"]))

        df = pd.DataFrame(reqs)
        if not df.empty:
            df.to_csv(f"csvs/{prog['slug']}.csv", index=False)
            print(f"   ✅ {len(df)} rows saved")
        else:
            print("   ⚠️  No tables found on this page")

        conn.commit()

    # Master CSV
    df_all = pd.read_sql_query("""
        SELECT p.name as program, g.group_name, c.code, c.title, c.credits, c.course_url
        FROM programs p
        JOIN requirement_groups g ON g.program_id = p.id
        JOIN group_courses c ON c.group_id = g.id
        WHERE p.year = ?
        ORDER BY p.name, g.group_name
    """, conn, params=(args.year,))
    
    df_all.to_csv(f"all_requirements_{args.year}.csv", index=False)

    print("\n🎉 FINISHED!")
    print(f"   Total programs: {len(programs)}")
    print(f"   Total requirement rows: {len(df_all)}")
    print(f"   → all_requirements_{args.year}.csv")
    print(f"   → csvs/ folder (one file per major)")
    conn.close()

if __name__ == "__main__":
    main()