import json
import requests
import sqlite3
import re
from pathlib import Path

# Configuration
DATA_DIR = Path("data")
DB_PATH = DATA_DIR / "cua_catalog.db"
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "llama3.2:3b"

def ask_llama(prompt):
    """Simple wrapper to talk to local Ollama instance."""
    try:
        payload = {
            "model": MODEL,
            "prompt": prompt,
            "stream": False,
            "format": "json"
        }
        response = requests.post(OLLAMA_URL, json=payload)
        response.raise_for_status()
        return json.loads(response.json()['response'])
    except Exception as e:
        print(f"    ⚠️ AI Error: {e}")
        return None

def get_page_text(url):
    """Fetch raw text from URL for AI analysis."""
    try:
        from bs4 import BeautifulSoup
        r = requests.get(url, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        main = soup.find(id="main") or soup.find(class_="main") or soup.find("article")
        return main.get_text(separator="\n", strip=True) if main else soup.get_text()
    except:
        return ""

def main():
    if not DB_PATH.exists():
        print("❌ Database not found.")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Find programs that are ACTIVE but have suspiciously low row counts (potential narrative requirements)
    print("🔍 Identifying 'Narrative' programs for AI elective extraction...")
    query = """
        SELECT p.id, p.name, p.url, count(c.id) as row_count 
        FROM programs p 
        LEFT JOIN requirement_groups g ON g.program_id = p.id 
        LEFT JOIN group_courses c ON c.group_id = g.id 
        WHERE p.status = 'Active' 
        GROUP BY p.name 
        HAVING row_count < 35
    """
    cur.execute(query)
    candidates = cur.fetchall()

    if not candidates:
        print("✅ No narrative programs found.")
        return

    print(f"🤖 Starting narrative analysis for {len(candidates)} programs...")

    for i, (pid, name, url, count) in enumerate(candidates, 1):
        print(f"[{i}/{len(candidates)}] → Analyzing: {name}...")
        text = get_page_text(url)
        if not text: continue

        prompt = f"""
        You are an academic registrar. Look at this program description and find ELECTIVE COURSES that are mentioned in the text or bulleted lists but are NOT in a table.
        
        Task: 
        1. Identify groups of electives (e.g., "Global Issues", "Regional Focus").
        2. Extract all course codes (e.g., POL 212, SOC 101) mentioned for those groups.
        
        Text:
        ---
        {text[:3000]}
        ---

        Return ONLY a JSON list of objects:
        {{
            "extracted_groups": [
                {{
                    "group_name": "Name of the elective group",
                    "courses": [
                        {{"code": "DEPT 101", "title": "Full Course Title"}}
                    ]
                }}
            ]
        }}
        If no new course codes are found, return {{"extracted_groups": []}}.
        """
        
        result = ask_llama(prompt)
        if not result or not result.get("extracted_groups"):
            continue

        # Insert extracted data into DB
        for group in result["extracted_groups"]:
            g_name = group["group_name"]
            # Create the group if it doesn't exist
            cur.execute("INSERT INTO requirement_groups (program_id, group_name) VALUES (?, ?)", (pid, f"{g_name} (AI Extracted)"))
            gid = cur.lastrowid
            
            for course in group["courses"]:
                cur.execute("""
                    INSERT INTO group_courses (group_id, code, title, credits, course_url) 
                    VALUES (?, ?, ?, ?, ?)
                """, (gid, course.get("code"), course.get("title"), "3", None)) # Default to 3 credits if unknown
        
        print(f"    ✨ AI found {sum(len(g['courses']) for g in result['extracted_groups'])} new elective options.")
        conn.commit()

    conn.close()
    print("\n🎉 Narrative analysis complete. Database updated.")

if __name__ == "__main__":
    main()
