# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the scraper

```bash
# Activate the virtual environment first
source .venv/bin/activate

# Full catalog scrape (all programs for the default year)
python -m src.main

# Scrape a specific academic year
python -m src.main --year 2024-2025

# Scrape a single program by its URL slug
python -m src.main --major computer-science

# Control parallelism (default: 10 workers)
python -m src.main --workers 5
```

The output database is written to `data/cua_catalog.db` (gitignored). **`db.setup()` drops and recreates all tables on every run**, so each execution is a full re-scrape, not an incremental update.

## Architecture

The pipeline has four layers:

1. **`src/models.py`** — Pure dataclasses with no logic: `Program` contains a list of `RequirementBlock`s, each of which contains a list of `Course`s. `Course.is_placeholder` distinguishes named course codes (e.g. `BIO 101`) from narrative placeholders (e.g. "Biology Elective").

2. **`src/scraper.py` (`BlockExtractor`)** — Fetches and parses catalog pages from CUA's SmartCatalogIQ site. Uses a BFS crawler (depth ≤ 2) starting from each program URL, following links that look like curriculum/requirement sub-pages while avoiding links to other programs (filtered via `all_slugs`). Parsing works by detecting which header level (`h1`–`h4`) most commonly precedes tables on a page, then linearly scanning tags to group courses and narrative text into `RequirementBlock`s.

3. **`src/database.py` (`DatabaseManager`)** — Persists the scraped `Program` objects into a 3-table SQLite schema: `programs` -> `requirement_blocks` -> `courses`.

4. **`src/main.py`** — Entry point. Discovers all programs by scraping four category index pages (Bachelor, Associate, Minors, Certificates), then fans out with `ThreadPoolExecutor` to scrape each program in parallel.

## Key implementation details

- The scraper sets `extractor.all_slugs` before parallel execution so cross-program link filtering works correctly.
- `BlockExtractor._tmp_seen` is an instance-level set used to deduplicate tables within a single page parse — it is not reset between pages, which could cause issues if the same table HTML appears on multiple pages of the same program.
- The `code_pattern` regex in `BlockExtractor` matches course codes like `BIO 101`, `MATH 3XX`, `HIST NNN`, etc.
- Dependencies: `requests`, `beautifulsoup4`. Both are installed in `.venv`.
