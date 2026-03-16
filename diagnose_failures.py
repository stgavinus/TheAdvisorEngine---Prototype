import json
import requests
import sqlite3
import pandas as pd
from pathlib import Path

# Configuration
DATA_DIR = Path("data")
FAILED_LOG = DATA_DIR / "failed_programs.json"
OUTPUT_REPORT = DATA_DIR / "ai_diagnostic_report.json"
DB_PATH = DATA_DIR / "cua_catalog.db"
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "llama3.2:3b"

def update_database(results):
    """Merge AI results back into the main database and refresh master CSV."""
    if not DB_PATH.exists():
        print(f"⚠️ Could not find database at {DB_PATH}. Skipping merge.")
        return
    
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    print(f"🗄️ Merging AI analysis into database...")
    for name, data in results.items():
        category = data['ai_analysis'].get('category', 'Failed')
        category = category.split('|')[0].split(':')[0].strip()
        cur.execute("UPDATE programs SET status = ? WHERE name = ?", (category, name))
    
    conn.commit()

    # Refresh Master CSV
    print(f"📊 Refreshing master CSV with new statuses...")
    try:
        # Find the catalog year from the database
        year_row = cur.execute("SELECT DISTINCT year FROM programs LIMIT 1").fetchone()
        if year_row:
            year = year_row[0]
            df_all = pd.read_sql_query("""
                SELECT p.name as program, p.status, g.group_name, c.code, c.title, c.credits, c.course_url
                FROM programs p
                JOIN requirement_groups g ON g.program_id = p.id
                JOIN group_courses c ON c.group_id = g.id
                WHERE p.year = ?
                ORDER BY p.name, g.group_name
            """, conn, params=(year,))
            
            master_csv_path = DATA_DIR / f"all_requirements_{year}.csv"
            df_all.to_csv(master_csv_path, index=False)
            print(f"   ✅ Master CSV updated: {master_csv_path}")
    except Exception as e:
        print(f"⚠️ Failed to refresh master CSV: {e}")

    conn.close()

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
        return {"category": "Error", "reason": f"Connection failed: {str(e)}"}

def main():
    if not FAILED_LOG.exists():
        print(f"❌ Could not find {FAILED_LOG}. Run the scraper first!")
        return

    with open(FAILED_LOG, "r") as f:
        failed_programs = json.load(f)

    if not failed_programs:
        print("✅ No failed programs found to analyze.")
        return

    print(f"🤖 Starting AI analysis of {len(failed_programs)} programs using {MODEL}...")
    
    results = {}

    for name, data in failed_programs.items():
        print(f"  → Analyzing: {name}...")
        
        prompt = f"""
        You are an academic registrar. Categorize why this university catalog page has no course tables.
        
        ### Categories:
        - "Discontinued": The text says the major is closed, no longer open, or refers to past announcements.
        - "Advisor-Designed": The text says students build their own plan or work with an advisor.
        - "Text-Based": The text mentions specific courses (e.g., MATH 101) in paragraphs.
        - "Shell Page": There is almost no text or just a generic header.
        - "Other": None of the above.

        ### Examples:
        Text: "This major is closed to new students." -> {{"category": "Discontinued", "reason": "Explicitly states the major is closed."}}
        Text: "Students design their own curriculum with an advisor." -> {{"category": "Advisor-Designed", "reason": "Mention of candidate-designed plan."}}

        ### Task:
        Analyze this text:
        ---
        {data['page_text'][:2000]}
        ---

        Return ONLY a JSON object with two fields: "category" (choose ONE from the list) and "reason" (one sentence).
        """
        
        analysis = ask_llama(prompt)
        results[name] = {
            "url": data['url'],
            "ai_analysis": analysis
        }

    # Save final report
    with open(OUTPUT_REPORT, "w") as f:
        json.dump(results, f, indent=4)

    # Merge results into the SQL database
    update_database(results)

    print(f"\n🎉 AI Analysis Complete!")
    print(f"📂 Report saved to: {OUTPUT_REPORT}")
    
    # Print a quick summary to console
    print("\nSummary of Updates:")
    for name, res in results.items():
        cat = res['ai_analysis'].get('category', 'Unknown')
        print(f" - {name}: {cat}")

if __name__ == "__main__":
    main()
