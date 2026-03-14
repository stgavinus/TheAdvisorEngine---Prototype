import argparse
import requests
import sqlite3
import re
import time
import json
import subprocess
import sys
from pathlib import Path
from bs4 import BeautifulSoup
import pandas as pd
from urllib.parse import urljoin

BASE_URL = "https://catholic.smartcatalogiq.com"
HEADERS = {"User-Agent": "CUA-Requirements-Scraper/4.0 (gavin@yourdomain.com)"}

# Output Configuration
DATA_DIR = Path("data")
DB_PATH = DATA_DIR / "cua_catalog.db"
CSV_DIR = DATA_DIR / "csvs"
FAILED_LOG = DATA_DIR / "failed_programs.json"

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
    DATA_DIR.mkdir(exist_ok=True)
    CSV_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        DROP TABLE IF EXISTS group_courses;
        DROP TABLE IF EXISTS requirement_groups;
        DROP TABLE IF EXISTS programs;
        CREATE TABLE programs (
            id INTEGER PRIMARY KEY, 
            year TEXT, 
            name TEXT, 
            slug TEXT UNIQUE, 
            url TEXT,
            status TEXT
        );
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
        valid_header = re.search(r"Year|Fall|Spring|Take |Elective|Core|Major|Minor|Foundation|Curriculum|Sequence|Plan|Requirements|Concentration|Distribution", group_name, re.I)
        if not valid_header: continue

        table = heading.find_next("table")
        if not table: continue
        
        prev_h = table.find_previous(["h1", "h2", "h3", "h4"])
        if prev_h != heading: continue

        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 3: continue
            
            code_td = tds[0]
            title_td = tds[1]
            credits_td = tds[-1]

            code = code_td.get_text(strip=True).replace("\xa0", " ").strip()
            title = title_td.get_text(strip=True).strip()
            credits = credits_td.get_text(strip=True).strip()

            if not code or any(k in code.lower() for k in ["subject", "course number", "number"]): continue
            if any(k in title.lower() for k in ["title", "credits"]): continue

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
    """Scrape a program recursively with universal safety and discovery logic."""
    all_requirements = []
    visited_urls = set()
    to_visit = [(program_url, 0)]
    name_keywords = set(re.findall(r"\w{4,}", program_name.lower()))
    page_text_dump = ""

    while to_visit:
        current_url, depth = to_visit.pop(0)
        norm_url = current_url.rstrip("/")
        if norm_url in visited_urls or depth > 3: continue
        visited_urls.add(norm_url)

        soup = get_soup(current_url)
        if not soup: continue
            
        if depth == 0:
            main_content = soup.find(id="main") or soup.find(class_="main") or soup.find("article")
            page_text_dump = main_content.get_text(separator="\n", strip=True) if main_content else soup.get_text(separator="\n", strip=True)

        new_reqs = extract_requirements_from_page(soup)
        all_requirements.extend(new_reqs)
        
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True).lower()
            sub_url = urljoin(current_url, a["href"]).split("#")[0].rstrip("/")
            
            is_req_link = any(k in text for k in ["requirements", "curriculum", "sequence", "plan", "courses"])
            is_child = sub_url.startswith(program_url.rstrip("/"))
            
            if "/en/" in sub_url:
                if any(sub_url.endswith(f"/{s}") for s in all_slugs if s != current_slug): continue
                
                mentions_major = any(k in text for k in name_keywords)
                if is_child or (is_req_link and (mentions_major or depth > 0)):
                    if sub_url not in visited_urls:
                        to_visit.append((sub_url, depth + 1))

    return all_requirements, page_text_dump

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", default="2025-2026")
    parser.add_argument("--skip-ai", action="store_true", help="Skip the AI diagnostic step")
    args = parser.parse_args()

    conn = create_db()
    cur = conn.cursor()

    print(f"🚀 Starting Universal Scraper v4 for Catalog Year {args.year}")
    print(f"📂 Data directory: {DATA_DIR}\n")

    index_url = f"{BASE_URL}/en/{args.year}/undergraduate-announcements/undergraduate-programs/bachelor-degree-programs"
    print(f"🔍 Fetching program index...")
    soup = get_soup(index_url)
    if not soup: return

    programs = []
    all_slugs = []
    for a in soup.find_all("a", href=True):
        if "bachelor-degree-programs/" in a["href"] and len(a["href"].split("/")) > 6:
            full_url = urljoin(BASE_URL, a["href"]).rstrip("/")
            if full_url not in [p["url"] for p in programs]:
                slug = a["href"].rstrip("/").split("/")[-1]
                programs.append({"name": a.get_text(strip=True), "url": full_url, "slug": slug})
                all_slugs.append(slug)

    print(f"✅ Found {len(programs)} unique programs. Beginning extraction...\n")

    failed_data = {}
    for i, prog in enumerate(programs, 1):
        print(f"[{i}/{len(programs)}] → {prog['name']}")
        reqs, text_dump = scrape_program_requirements(prog["url"], prog["name"], prog["slug"], all_slugs)

        unique_reqs = []
        seen_reqs = set()
        for r in reqs:
            key = (r["group"], r["code"], r["title"])
            if key not in seen_reqs:
                seen_reqs.add(key)
                unique_reqs.append(r)

        status = "Active" if unique_reqs else "No Data Found"
        cur.execute("INSERT OR REPLACE INTO programs (year, name, slug, url, status) VALUES (?, ?, ?, ?, ?)",
                    (args.year, prog["name"], prog["slug"], prog["url"], status))
        cur.execute("SELECT id FROM programs WHERE slug=?", (prog["slug"],))
        program_id = cur.fetchone()[0]

        cur.execute("DELETE FROM group_courses WHERE group_id IN (SELECT id FROM requirement_groups WHERE program_id = ?)", (program_id,))
        cur.execute("DELETE FROM requirement_groups WHERE program_id = ?", (program_id,))

        group_ids = {}
        for r in unique_reqs:
            if r["group"] not in group_ids:
                cur.execute("INSERT INTO requirement_groups (program_id, group_name) VALUES (?, ?)", (program_id, r["group"]))
                group_ids[r["group"]] = cur.lastrowid
            cur.execute("INSERT INTO group_courses (group_id, code, title, credits, course_url) VALUES (?, ?, ?, ?, ?)",
                        (group_ids[r["group"]], r["code"], r["title"], r["credits"], r["course_url"]))

        if unique_reqs:
            pd.DataFrame(unique_reqs).to_csv(CSV_DIR / f"{prog['slug']}.csv", index=False)
            print(f"   ✅ Saved {len(unique_reqs)} rows")
        else:
            print(f"   ⚠️ No requirements found. Logged for AI analysis.")
            failed_data[prog["name"]] = {"url": prog["url"], "page_text": text_dump[:5000]}
        conn.commit()

    df_all = pd.read_sql_query("SELECT p.name as program, p.status, g.group_name, c.code, c.title, c.credits, c.course_url FROM programs p JOIN requirement_groups g ON g.program_id = p.id JOIN group_courses c ON c.group_id = g.id WHERE p.year = ?", conn, params=(args.year,))
    df_all.to_csv(DATA_DIR / f"all_requirements_{args.year}.csv", index=False)
    with open(FAILED_LOG, "w") as f: json.dump(failed_data, f, indent=4)
    conn.close()

    print(f"\n🎉 Extraction Complete! Data in '{DATA_DIR}/'")

    if not args.skip_ai and failed_data:
        print(f"\n🤖 Automatically triggering AI analysis...")
        try:
            subprocess.run([sys.executable, "diagnose_failures.py"], check=True)
        except Exception as e:
            print(f"❌ AI Diagnostic failed to run: {e}")

if __name__ == "__main__":
    main()
