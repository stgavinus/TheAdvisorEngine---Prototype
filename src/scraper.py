import re
import requests
from bs4 import BeautifulSoup, Tag
from urllib.parse import urljoin
from src.models import Program, RequirementBlock, Course, CourseDetail

HEADERS = {"User-Agent": "CUA-AdvisorEngine/1.0"}
COURSE_CODE_RE = re.compile(r"\b([A-Z]{2,4})\s?(\d{3,4}[A-Z]?)\b")


def _get_soup(url: str) -> BeautifulSoup | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except Exception:
        return None


def _detect_header_level(soup: BeautifulSoup) -> str:
    """Return the header level (h2/h3/h4) that most commonly precedes tables."""
    levels = []
    for table in soup.find_all("table"):
        h = table.find_previous(["h2", "h3", "h4"])
        if h:
            levels.append(h.name)
    if not levels:
        return "h3"
    return max(set(levels), key=levels.count)


def _extract_course_from_row(tds: list[Tag]) -> Course | None:
    """Parse a table row into a Course. Returns None if the row is a header."""
    if len(tds) < 2:
        return None

    raw_code = tds[0].get_text(separator=" ", strip=True).replace("\xa0", " ")
    title    = tds[1].get_text(separator=" ", strip=True)
    credits  = tds[-1].get_text(strip=True) if len(tds) > 2 else ""

    # Deduplicate slash-separated components (e.g. "GER 101 / GER 101" → "GER 101")
    parts = [p.strip() for p in raw_code.split("/") if p.strip()]
    seen: list[str] = []
    for p in parts:
        if p not in seen:
            seen.append(p)
    if len(seen) != len(parts):
        raw_code = " / ".join(seen)

    # Skip header rows
    if any(k in raw_code.lower() for k in ("subject", "course", "number")):
        return None
    if any(k in title.lower() for k in ("title", "credits")):
        return None

    # Course URL — look in code cell first, then title cell
    link = tds[0].find("a") or tds[1].find("a")
    url  = urljoin("https://catholic.smartcatalogiq.com", link["href"]) if link else ""

    is_code = bool(COURSE_CODE_RE.search(raw_code))
    return Course(
        code=raw_code if is_code else "",
        title=title,
        credits=credits,
        url=url,
        is_placeholder=not is_code,
        source="table",
    )


def _extract_narrative_courses(text: str) -> list[Course]:
    """Pull course codes embedded in plain text (e.g. 'students take BIO 101')."""
    courses = []
    for m in COURSE_CODE_RE.finditer(text):
        dept, num = m.group(1), m.group(2)
        courses.append(Course(
            code=f"{dept} {num}",
            title="",
            credits="",
            source="narrative",
        ))
    return courses


def _parse_page(soup: BeautifulSoup, program: Program, seen_tables: set) -> None:
    """
    Linear scan of the main content area.
    Each header starts a new RequirementBlock.
    Tables and text beneath it are added to that block.
    """
    header_level = _detect_header_level(soup)

    main = (
        soup.find(id="main")
        or soup.find(class_="main")
        or soup.find("article")
        or soup.body
        or soup
    )

    current_block: RequirementBlock | None = None

    for tag in main.find_all([header_level, "table", "p", "li"], recursive=True):
        # ── New block ──────────────────────────────────────────────────────────
        if tag.name == header_level:
            title = tag.get_text(strip=True)
            if len(title) < 3:
                continue

            # Reuse existing block if we've seen this heading before (multi-page)
            existing = next((b for b in program.blocks if b.title == title), None)
            if existing:
                current_block = existing
            else:
                current_block = RequirementBlock(title=title)
                program.blocks.append(current_block)
            continue

        if current_block is None:
            continue

        # ── Table ──────────────────────────────────────────────────────────────
        if tag.name == "table":
            table_id = id(tag)
            if table_id in seen_tables:
                continue
            seen_tables.add(table_id)

            # Capture "Take One / Take Two" instructions from caption or preceding <p>
            caption = tag.find("caption")
            if caption:
                current_block.instruction = caption.get_text(strip=True)

            for tr in tag.find_all("tr"):
                tds = tr.find_all("td")
                course = _extract_course_from_row(tds)
                if course:
                    current_block.courses.append(course)

        # ── Paragraph or list item ─────────────────────────────────────────────
        elif tag.name in ("p", "li"):
            # Skip if this tag is just a container for nested p/li (avoid doubling)
            if tag.find(["p", "li"]):
                continue

            text = tag.get_text(" ", strip=True)
            if not text or re.match(r"^\d+\.?\d*$", text):
                continue

            # "Take One", "Take Two", etc. are instructions, not notes
            if re.match(r"^take\s+\w+", text, re.I):
                if not current_block.instruction:
                    current_block.instruction = text
                continue

            # Try to extract inline course codes first
            inline = _extract_narrative_courses(text)
            if inline:
                for c in inline:
                    # Avoid duplicating codes already captured from a table
                    if not any(existing.code == c.code for existing in current_block.courses):
                        current_block.courses.append(c)
            else:
                # Pure narrative — store as notes
                if text not in current_block.notes:
                    current_block.notes += (" " + text if current_block.notes else text)


def _classify_status(program: Program) -> str:
    real_codes = sum(
        1 for b in program.blocks
        for c in b.courses
        if c.code and not c.is_placeholder
    )
    if real_codes >= 5:
        return "COMPLETE"
    if real_codes > 0:
        return "PARTIAL"
    if any(b.notes for b in program.blocks):
        return "NARRATIVE"
    return "EMPTY"


def _should_follow(url: str, base_url: str, slug: str, all_slugs: set[str]) -> bool:
    """
    Follow a link if:
      - it's on the SmartCatalogIQ platform (/en/ in path)
      - it's a child of the program's own URL, OR
      - it doesn't end in another program's slug (safe to explore)
    Never follow links that land on a different program's page.
    """
    if "/en/" not in url:
        return False
    # Block links that resolve to another known program
    if any(url.rstrip("/").endswith(f"/{s}") for s in all_slugs if s != slug):
        return False
    return url.startswith(base_url)


def detect_subplans(url: str) -> dict:
    """
    Fetch a program's main page and identify sub-plan structure.

    Looks for direct child links whose URL segment contains:
      - "sub-plan"    → a concentration/track students choose from
      - "requirement" → the shared base requirements page

    Returns:
      {
        "base_url": str | None,  # URL of the -requirements sub-page, if found
        "subplans": [{"name": str, "url": str, "slug": str}, ...]
      }
    If no sub-plans are found, returns {"base_url": None, "subplans": []}.
    """
    result: dict = {"base_url": None, "subplans": []}
    soup = _get_soup(url)
    if not soup:
        return result

    base = url.rstrip("/")
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        child = urljoin(url, a["href"]).split("#")[0].rstrip("/")
        if not child.startswith(base + "/"):
            continue
        remainder = child[len(base) + 1:]
        if "/" in remainder:          # not a direct child
            continue
        if child in seen:
            continue
        seen.add(child)

        link_text    = a.get_text(strip=True)
        slug_segment = remainder

        if "sub-plan" in slug_segment.lower():
            # Strip trailing "- Sub-Plan Option" from the link text
            name = re.sub(r"\s*[-–]\s*sub.?plan\s+option\s*$", "", link_text, flags=re.I).strip()
            result["subplans"].append({"name": name, "url": child, "slug": slug_segment})
        elif "requirement" in slug_segment.lower():
            result["base_url"] = child

    return result


class ProgramScraper:
    def __init__(self, all_slugs: set[str]):
        self.all_slugs = all_slugs

    def scrape(self, name: str, category: str, url: str, slug: str, year: str,
               base_url: str = None) -> Program:
        program = Program(name=name, category=category, url=url, slug=slug, year=year)
        seen_urls: set[str]   = set()
        seen_tables: set[int] = set()

        # Scrape shared base requirements first (single page, no BFS)
        if base_url:
            seen_urls.add(base_url.rstrip("/"))
            soup = _get_soup(base_url)
            if soup:
                _parse_page(soup, program, seen_tables)

        # BFS from the program's own URL
        queue = [(url.rstrip("/"), 0)]

        while queue:
            curr_url, depth = queue.pop(0)
            if curr_url in seen_urls or depth > 2:
                continue
            seen_urls.add(curr_url)

            soup = _get_soup(curr_url)
            if not soup:
                continue

            _parse_page(soup, program, seen_tables)

            for a in soup.find_all("a", href=True):
                sub = urljoin(curr_url, a["href"]).split("#")[0].rstrip("/")
                if sub not in seen_urls and _should_follow(sub, url.rstrip("/"), slug, self.all_slugs):
                    queue.append((sub, depth + 1))

        program.status = _classify_status(program)
        return program


class CourseDetailScraper:
    def scrape(self, code: str, url: str) -> CourseDetail | None:
        soup = _get_soup(url)
        if not soup:
            return None

        detail = CourseDetail(code=code, url=url)

        main = soup.find(id="main") or soup.find(class_="main") or soup.find("article")
        if not main:
            return detail

        # Description: first substantial block of text that isn't metadata.
        # Try <p> first, then direct <div> children (some pages use divs).
        SKIP_WORDS = ("prerequisite", "credit", "equivalent", "cross listed")
        for tag in main.find_all(["p", "div"]):
            # Skip divs that contain nested block elements — they're containers, not text
            if tag.name == "div" and tag.find(["p", "div", "table", "ul"]):
                continue
            text = tag.get_text(strip=True)
            if len(text) > 40 and not any(k in text.lower() for k in SKIP_WORDS):
                detail.description = text
                break

        # Prerequisites block — preserve "or"/"and" connectors by using full text
        prereq_div = soup.find("div", class_="sc_prereqs")
        if prereq_div:
            h3 = prereq_div.find("h3")
            if h3:
                h3.extract()
            detail.prerequisites = prereq_div.get_text(separator=" ", strip=True)

        # Cross-listed courses
        cross_h3 = soup.find("h3", string=re.compile(r"cross.listed", re.I))
        if cross_h3:
            detail.cross_listed = ", ".join(
                a.get_text(strip=True) for a in cross_h3.find_next_siblings("a")
            )

        return detail
