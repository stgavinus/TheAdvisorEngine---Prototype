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
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_URL = "https://catholic.smartcatalogiq.com"
HEADERS = {"User-Agent": "CUA-Universal-Scraper/5.0 (gavin@yourdomain.com)"}

# Output Configuration
DATA_DIR = Path("data")
DB_PATH = DATA_DIR / "cua_catalog.db"
CSV_DIR = DATA_DIR / "csvs"
FAILED_LOG = DATA_DIR / "failed_programs.json"

def get_soup(url: str) -> BeautifulSoup:
    """Fetch URL and return BeautifulSoup object with a small delay."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except Exception:
        return None

def setup_database():
    """Reset and create the database tables."""
    DATA_DIR.mkdir(exist_ok=True)
    CSV_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        DROP TABLE IF EXISTS group_courses;
        DROP TABLE IF EXISTS requirement_groups;
        DROP TABLE IF EXISTS programs;
        DROP TABLE IF EXISTS course_details;
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
        CREATE TABLE course_details (
            code TEXT PRIMARY KEY, 
            description TEXT, 
            prerequisites TEXT, 
            cross_listed TEXT, 
            url TEXT
        );
    """)
    conn.commit()
    return conn

# --- STAGE 1: MAJOR REQUIREMENTS ---

def extract_requirements_from_page(soup):
    if not soup: return []
    page_reqs = []
    for heading in soup.find_all(["h1", "h2", "h3", "h4"]):
        group_name = heading.get_text(strip=True)
        if not re.search(r"Year|Fall|Spring|Take |Elective|Core|Major|Minor|Foundation|Curriculum|Sequence|Plan|Requirements", group_name, re.I): continue
        table = heading.find_next("table")
        if not table or table.find_previous(["h1", "h2", "h3", "h4"]) != heading: continue
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 3: continue
            code_td, title_td, credits_td = tds[0], tds[1], tds[-1]
            code = code_td.get_text(strip=True).replace("\xa0", " ").strip()
            title = title_td.get_text(strip=True).strip()
            credits = credits_td.get_text(strip=True).strip()
            if not code or any(k in code.lower() for k in ["subject", "number"]): continue
            if any(k in title.lower() for k in ["title", "credits"]): continue
            link = code_td.find("a") or title_td.find("a")
            course_url = urljoin(BASE_URL, link["href"]) if link and link.get("href") else None
            page_reqs.append({"group": group_name, "code": code, "title": title, "credits": credits, "course_url": course_url})
    return page_reqs

def scrape_major(program_url, program_name, current_slug, all_slugs):
    all_requirements, visited_urls, to_visit = [], set(), [(program_url, 0)]
    name_keywords = set(re.findall(r"\w{4,}", program_name.lower()))
    page_text_dump = ""
    while to_visit:
        url, depth = to_visit.pop(0)
        if url.rstrip("/") in visited_urls or depth > 3: continue
        visited_urls.add(url.rstrip("/"))
        soup = get_soup(url)
        if not soup: continue
        if depth == 0:
            main = soup.find(id="main") or soup.find(class_="main") or soup.find("article")
            page_text_dump = main.get_text(separator="\n", strip=True) if main else soup.get_text(separator="\n", strip=True)
        all_requirements.extend(extract_requirements_from_page(soup))
        for a in soup.find_all("a", href=True):
            text, sub_url = a.get_text(strip=True).lower(), urljoin(url, a["href"]).split("#")[0].rstrip("/")
            if "/en/" not in sub_url or any(sub_url.endswith(f"/{s}") for s in all_slugs if s != current_slug): continue
            is_req_link = any(k in text for k in ["requirements", "curriculum", "sequence", "plan", "courses"])
            if sub_url.startswith(program_url.rstrip("/")) or (is_req_link and any(k in text for k in name_keywords)):
                if sub_url not in visited_urls: to_visit.append((sub_url, depth + 1))
    return all_requirements, page_text_dump

# --- STAGE 2: COURSE DETAILS (PARALLEL) ---

def extract_course_info(soup):
    if not soup: return None
    desc, prereqs, cross_listed = "", "", ""
    main = soup.find(id="main")
    if main:
        for p in main.find_all("p", recursive=False):
            p_text = p.get_text(strip=True)
            if p_text and not any(k in p_text.lower() for k in ["prerequisite", "credits", "equivalent"]):
                desc = p_text; break
    p_div = soup.find("div", class_="sc_prereqs")
    if p_div: prereqs = " ".join([e.get_text(strip=True) for e in p_div.children if e.name != 'h3']).strip()
    c_h3 = soup.find("h3", string=re.compile(r"Cross Listed Courses", re.I))
    if c_h3:
        links = []
        for sib in c_h3.next_siblings:
            if sib.name in ["h3", "div"]: break
            if sib.name == "a": links.append(sib.get_text(strip=True))
        cross_listed = ", ".join(links)
    return {"description": desc, "prerequisites": prereqs, "cross_listed": cross_listed}

def scrape_one_course(code, url):
    soup = get_soup(url)
    info = extract_course_info(soup)
    if info:
        return (code, info['description'], info['prerequisites'], info['cross_listed'], url)
    return None

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", default="2025-2026")
    parser.add_argument("--skip-ai", action="store_true", help="Skip the AI diagnostic step")
    args = parser.parse_args()
    
    conn = setup_database()
    cur = conn.cursor()

    print(f"🚀 Starting CUA Mega-Scraper v5 for Catalog Year {args.year}")
    print(f"📂 Output directory: {DATA_DIR}\n")

    # 1. Fetch Index
    index_soup = get_soup(f"{BASE_URL}/en/{args.year}/undergraduate-announcements/undergraduate-programs/bachelor-degree-programs")
    if not index_soup:
        print("❌ Could not reach index page.")
        return

    programs = []
    all_slugs = []
    for a in index_soup.find_all("a", href=True):
        if "bachelor-degree-programs/" in a["href"] and len(a["href"].split("/")) > 6:
            url = urljoin(BASE_URL, a["href"]).rstrip("/")
            if url not in [p["url"] for p in programs]:
                slug = a["href"].rstrip("/").split("/")[-1]
                programs.append({"name": a.get_text(strip=True), "url": url, "slug": slug})
                all_slugs.append(slug)
    
    print(f"✅ Found {len(programs)} unique programs. Extraction Stage 1 (Majors) starting...\n")

    failed_data = {}
    for i, prog in enumerate(programs, 1):
        print(f"[{i}/{len(programs)}] → {prog['name']}")
        reqs, text_dump = scrape_major(prog["url"], prog["name"], prog["slug"], all_slugs)
        
        # Deduplicate
        unique_reqs = []
        seen = set()
        for r in reqs:
            if (r["group"], r["code"], r["title"]) not in seen:
                seen.add((r["group"], r["code"], r["title"]))
                unique_reqs.append(r)
        
        status = "Active" if unique_reqs else "No Data Found"
        cur.execute("INSERT OR REPLACE INTO programs (year, name, slug, url, status) VALUES (?, ?, ?, ?, ?)", (args.year, prog['name'], prog['slug'], prog['url'], status))
        pid = cur.execute("SELECT id FROM programs WHERE slug=?", (prog['slug'],)).fetchone()[0]
        
        gids = {}
        for r in unique_reqs:
            if r["group"] not in gids:
                cur.execute("INSERT INTO requirement_groups (program_id, group_name) VALUES (?, ?)", (pid, r["group"]))
                gids[r["group"]] = cur.lastrowid
            cur.execute("INSERT INTO group_courses (group_id, code, title, credits, course_url) VALUES (?, ?, ?, ?, ?)", (gids[r["group"]], r["code"], r["title"], r["credits"], r["course_url"]))
        
        if unique_reqs:
            pd.DataFrame(unique_reqs).to_csv(CSV_DIR / f"{prog['slug']}.csv", index=False)
            print(f"   ✅ Saved {len(unique_reqs)} rows")
        else:
            failed_data[prog["name"]] = {"url": prog["url"], "page_text": text_dump[:5000]}
            print(f"   ⚠️ Logged for AI analysis")
        conn.commit()

    # 2. Deep Scrape Courses
    print(f"\n🚀 Stage 2 (Parallel Course Details) starting...")
    cur.execute("SELECT DISTINCT code, course_url FROM group_courses WHERE course_url IS NOT NULL")
    courses_to_scrape = cur.fetchall()
    
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(scrape_one_course, code, url): code for code, url in courses_to_scrape}
        for i, f in enumerate(as_completed(futures), 1):
            res = f.result()
            if res:
                cur.execute("INSERT OR REPLACE INTO course_details (code, description, prerequisites, cross_listed, url) VALUES (?, ?, ?, ?, ?)", res)
            if i % 100 == 0: 
                print(f"   ✨ Processed {i}/{len(courses_to_scrape)} course details...")
                conn.commit()
    conn.commit()

    # 3. Master Export
    df_all = pd.read_sql_query("SELECT p.name as program, p.status, g.group_name, c.code, c.title, c.credits, c.course_url FROM programs p JOIN requirement_groups g ON g.program_id = p.id JOIN group_courses c ON c.group_id = g.id WHERE p.year = ?", conn, params=(args.year,))
    df_all.to_csv(DATA_DIR / f"all_requirements_{args.year}.csv", index=False)
    with open(FAILED_LOG, "w") as f: json.dump(failed_data, f, indent=4)
    conn.close()

    print(f"\n🎉 Scraper Phase Complete! Data in '{DATA_DIR}/'")

    # 4. Trigger AI
    if not args.skip_ai and failed_data:
        print(f"\n🤖 Automatically triggering Stage 3 (AI Diagnosis)...")
        try:
            subprocess.run([sys.executable, "diagnose_failures.py"], check=True)
        except Exception as e:
            print(f"❌ AI Stage failed to run: {e}")
    
    print(f"\n🏁 ALL TASKS FINISHED SUCCESSFULLY!")

if __name__ == "__main__":
    main()
