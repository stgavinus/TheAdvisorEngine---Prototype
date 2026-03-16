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
INDEX_URL = "https://enrollment-services.catholic.edu/announcements/index.html"
HEADERS = {"User-Agent": "CUA-Undergraduate-Scraper/7.2 (gavin@yourdomain.com)"}

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
    """Ensure database tables exist for undergraduate data."""
    DATA_DIR.mkdir(exist_ok=True)
    CSV_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS programs (
            id INTEGER PRIMARY KEY, 
            year TEXT, 
            category TEXT,
            name TEXT, 
            slug TEXT UNIQUE, 
            url TEXT, 
            status TEXT
        );
        CREATE TABLE IF NOT EXISTS requirement_groups (id INTEGER PRIMARY KEY, program_id INTEGER, group_name TEXT);
        CREATE TABLE IF NOT EXISTS group_courses (id INTEGER PRIMARY KEY, group_id INTEGER, code TEXT, title TEXT, credits TEXT, course_url TEXT);
        CREATE TABLE IF NOT EXISTS course_details (
            code TEXT PRIMARY KEY, 
            description TEXT, 
            prerequisites TEXT, 
            cross_listed TEXT, 
            url TEXT
        );
    """)
    conn.commit()
    return conn

def discover_latest_year():
    """Fetch the index page and find the most recent academic year."""
    print(f"🔍 Checking {INDEX_URL} for new catalogs...")
    soup = get_soup(INDEX_URL)
    if not soup: return None
    for h3 in soup.find_all("h3"):
        match = re.search(r"(\d{4}-\d{4})", h3.get_text())
        if match: return match.group(1)
    return None

# --- EXTRACTION LOGIC ---

def extract_requirements_from_page(soup):
    if not soup: return []
    page_reqs = []
    valid_header_pattern = re.compile(r"Year|Fall|Spring|Take |Elective|Core|Major|Minor|Foundation|Curriculum|Sequence|Plan|Requirements|Concentration|Distribution|Liberal Arts|Area|Choices|Sequence|Psychology|Biology|Sociology|History|Honors|Science|Mathematics|Arts|Social|Clinical|Cognitive", re.I)
    exclude_sidebar = re.compile(r"Search|Contents|Links|Navigation", re.I)
    
    for heading in soup.find_all(["h1", "h2", "h3", "h4"]):
        group_name = heading.get_text(strip=True)
        if exclude_sidebar.search(group_name) or not valid_header_pattern.search(group_name):
            continue

        for sibling in heading.find_next_siblings():
            if sibling.name in ["h1", "h2", "h3", "h4"]: break
            if sibling.name == "table":
                for tr in sibling.find_all("tr"):
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

def scrape_program(program_url, program_name, current_slug, all_slugs):
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
            is_req_link = any(k in text for k in ["requirements", "curriculum", "sequence", "plan", "courses", "liberal arts"])
            if sub_url.startswith(program_url.rstrip("/")) or (is_req_link and (any(k in text for k in name_keywords) or current_slug == "liberal-arts-curriculum")):
                if sub_url not in visited_urls: to_visit.append((sub_url, depth + 1))
    return all_requirements, page_text_dump

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
        links = [a.get_text(strip=True) for a in c_h3.find_next_siblings("a")]
        cross_listed = ", ".join(links)
    return {"description": desc, "prerequisites": prereqs, "cross_listed": cross_listed}

def scrape_one_course(code, url):
    soup = get_soup(url)
    info = extract_course_info(soup)
    return (code, info['description'], info['prerequisites'], info['cross_listed'], url) if info else None

def scrape_one_program_task(prog, all_slugs):
    """Unified task for Stage 1 parallelism."""
    reqs, text_dump = scrape_program(prog["url"], prog["name"], prog["slug"], all_slugs)
    unique_reqs = []
    seen = set()
    for r in reqs:
        if (r["group"], r["code"], r["title"]) not in seen:
            seen.add((r["group"], r["code"], r["title"])); unique_reqs.append(r)
    return {"prog": prog, "reqs": unique_reqs, "text_dump": text_dump, "status": "Active" if unique_reqs else "No Data Found"}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", help="Catalog year.")
    parser.add_argument("--skip-ai", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    
    conn = setup_database()
    cur = conn.cursor()
    year = args.year or discover_latest_year()
    if not year: return

    if not args.force:
        cur.execute("SELECT count(*) FROM programs WHERE year = ?", (year,))
        if cur.fetchone()[0] > 0:
            print(f"✅ Data for {year} already exists. Use --force to update.")
            return

    print(f"🚀 Starting CUA Universal Mega-Scraper v7.2 (Full Automation) for Year {year}")
    categories = {
        "University Core": f"{BASE_URL}/en/{year}/undergraduate-announcements/the-undergraduate-curriculum",
        "Bachelor Degrees": f"{BASE_URL}/en/{year}/undergraduate-announcements/undergraduate-programs/bachelor-degree-programs",
        "Associate Degrees": f"{BASE_URL}/en/{year}/undergraduate-announcements/undergraduate-programs/associate-degree-programs",
        "Minors": f"{BASE_URL}/en/{year}/undergraduate-announcements/undergraduate-programs/undergraduate-minors",
        "Certificates": f"{BASE_URL}/en/{year}/undergraduate-announcements/undergraduate-programs/undergraduate-certificates"
    }

    programs = [{"category": "University Core", "name": "Liberal Arts Curriculum (General Education)", "url": categories["University Core"], "slug": "liberal-arts-curriculum"}]
    for cat_name, cat_url in categories.items():
        if cat_name == "University Core": continue 
        soup = get_soup(cat_url)
        if not soup: continue
        for a in soup.find_all("a", href=True):
            if "/undergraduate-programs/" in a["href"] and len(a["href"].split("/")) > 6:
                programs.append({"category": cat_name, "name": a.get_text(strip=True), "url": urljoin(BASE_URL, a["href"]).rstrip("/"), "slug": a["href"].rstrip("/").split("/")[-1]})
    
    all_slugs = [p["slug"] for p in programs]
    print(f"✅ Found {len(programs)} programs. Starting Stage 1 Extraction...\n")

    failed_data = {}
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(scrape_one_program_task, p, all_slugs): p for p in programs}
        for i, future in enumerate(as_completed(futures), 1):
            res = future.result()
            prog = res["prog"]
            
            # Restore the cleaner original UI style WITH COUNTER
            print(f"[{i}/{len(programs)}] → {prog['name']}")
            if res["reqs"]:
                print(f"   ✅ Saved {len(res['reqs'])} rows")
            else:
                print(f"   ⚠️ No requirements found. Logged for AI analysis.")
                failed_data[prog["name"]] = {"url": prog["url"], "page_text": res["text_dump"][:5000]}

            # DB updates
            cur.execute("INSERT OR REPLACE INTO programs (year, category, name, slug, url, status) VALUES (?, ?, ?, ?, ?, ?)", 
                        (year, prog['category'], prog['name'], prog['slug'], prog['url'], res['status']))
            pid = cur.execute("SELECT id FROM programs WHERE slug=?", (prog['slug'],)).fetchone()[0]
            cur.execute("DELETE FROM group_courses WHERE group_id IN (SELECT id FROM requirement_groups WHERE program_id = ?)", (pid,))
            cur.execute("DELETE FROM requirement_groups WHERE program_id = ?", (pid,))
            
            gids = {}
            for r in res["reqs"]:
                if r["group"] not in gids:
                    cur.execute("INSERT INTO requirement_groups (program_id, group_name) VALUES (?, ?)", (pid, r["group"]))
                    gids[r["group"]] = cur.lastrowid
                cur.execute("INSERT INTO group_courses (group_id, code, title, credits, course_url) VALUES (?, ?, ?, ?, ?)", (gids[r["group"]], r["code"], r["title"], r["credits"], r["course_url"]))
            
            if res["reqs"]:
                pd.DataFrame(res["reqs"]).to_csv(CSV_DIR / f"{prog['slug']}.csv", index=False)
            conn.commit()

    print(f"\n🚀 Stage 2 (Parallel Course Details) with 10 workers...")
    cur.execute("SELECT DISTINCT code, course_url FROM group_courses WHERE course_url IS NOT NULL")
    courses = cur.fetchall()
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(scrape_one_course, code, url): code for code, url in courses}
        for i, f in enumerate(as_completed(futures), 1):
            res = f.result()
            if res: cur.execute("INSERT OR REPLACE INTO course_details (code, description, prerequisites, cross_listed, url) VALUES (?,?,?,?,?)", res)
            if i % 100 == 0: 
                print(f"   ✨ Processed {i}/{len(courses)} course details...")
                conn.commit()
    conn.commit()

    df_all = pd.read_sql_query("SELECT p.category, p.name as program, p.status, g.group_name, c.code, c.title, c.credits, c.course_url FROM programs p JOIN requirement_groups g ON g.program_id = p.id JOIN group_courses c ON c.group_id = g.id WHERE p.year = ?", conn, params=(year,))
    df_all.to_csv(DATA_DIR / f"all_requirements_{year}.csv", index=False)
    with open(FAILED_LOG, "w") as f: json.dump(failed_data, f, indent=4)
    conn.close()

    print(f"\n🎉 Scraper Phase Complete!")

    if not args.skip_ai:
        if failed_data:
            print(f"\n🤖 Automatically triggering Stage 3 (AI Diagnosis)...")
            try: subprocess.run([sys.executable, "diagnose_failures.py"], check=True)
            except Exception: pass
        
        print(f"\n🤖 Automatically triggering Stage 4 (AI Narrative Extraction)...")
        try: subprocess.run([sys.executable, "analyze_narratives.py"], check=True)
        except Exception: pass

    print(f"\n🏁 ALL TASKS FINISHED SUCCESSFULLY!")

if __name__ == "__main__":
    main()
