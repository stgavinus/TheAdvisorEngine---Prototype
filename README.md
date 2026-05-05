# TheAdvisorEngine

A web application for CUA students to plan their coursework from a single, clean interface. Students select their program and see its degree requirements alongside their own progress. A prerequisite logic engine tracks which courses are available and why others are blocked. Live class schedule data from Cardinal Students is integrated directly into the planner so students can build a semester schedule without leaving the app.

## Stack

- **Backend:** Python, Flask, SQLite
- **Frontend:** HTML, CSS, JavaScript
- **Scraping:** BeautifulSoup (catalog), custom PeopleSoft session scraper (schedule)

## Project structure

```
app.py                  # Flask application and all routes
src/
  models.py             # Dataclasses: Program, RequirementBlock, Course
  logic.py              # Prerequisite logic engine
  scraper.py            # Catalog scraper (smartcatalogiq)
  schedule_scraper.py   # Schedule scraper (Cardinal Students / PeopleSoft)
  schedule_enrich.py    # Enriches schedule data with catalog metadata
  database.py           # Catalog DB management
  schedule_db.py        # Schedule DB management
  user_db.py            # User account and plan persistence
  main.py               # Catalog scrape entry point
  schedule_main.py      # Schedule scrape entry point
templates/              # Jinja2 HTML templates
static/                 # CSS
data/
  cua_catalog.db        # Scraped degree requirements (all undergraduate programs)
  cua_schedule.db       # Scraped class schedule data
```

## Running the app

```bash
# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Start the server
python app.py
```

Then open `http://localhost:5000` in a browser. The catalog and schedule databases are included in the repo so no scraping is needed to run the app.

## Re-scraping the data

```bash
source .venv/bin/activate

# Re-scrape the full course catalog
python -m src.main

# Re-scrape the class schedule
python -m src.schedule_main
```
