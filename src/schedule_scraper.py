"""
PeopleSoft Class Search scraper for CUA (Catholic University of America).

Session lifecycle
-----------------
1. GET the guest portal URL  →  receives session cookies via 302 redirect.
2. All subsequent navigation is done via POST to the Class Search GBL endpoint.
3. Every POST must carry the current ICSID token (extracted from the last HTML
   response) and a monotonically-increasing ICStateNum counter.

SSL note
--------
Python's bundled LibreSSL on macOS can reject csprd.cua.edu's certificate.
Every request method therefore tries `requests` first and falls back to a
subprocess `curl` call when an SSL error is detected.
"""

import re
import time
import subprocess
import requests
import certifi
from urllib.parse import urlencode, quote
from bs4 import BeautifulSoup

from src.schedule_db import CourseSection, SectionMeeting

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PORTAL_URL = (
    "https://csprd.cua.edu/psp/csprd/GUEST/SA/c/"
    "COMMUNITY_ACCESS.CLASS_SEARCH.GBL?"
)
POST_URL = (
    "https://csprd.cua.edu/psc/csprd/EMPLOYEE/SA/c/"
    "COMMUNITY_ACCESS.CLASS_SEARCH.GBL"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CUA-AdvisorEngine/1.0)",
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Regex helpers
_COURSE_RE  = re.compile(r"\b([A-Z]{2,4})\s+(\d{3,4}[A-Z]?)\b")
_TIME_RE    = re.compile(r"(\d{1,2}:\d{2}[AP]M)\s*[-–]\s*(\d{1,2}:\d{2}[AP]M)")
_DATE_RE    = re.compile(r"(\d{2}/\d{2}/\d{4})\s*[-–]\s*(\d{2}/\d{2}/\d{4})")
_INT_RE     = re.compile(r"\d+")


def _extract_field(body: str, name: str) -> str:
    """
    Extract the value of an HTML <input> field by name, regardless of
    attribute order.  Returns "" if the field is not found.

    Handles all of these (and any other ordering):
        <input name="ICSID" value="TOKEN">
        <input value="TOKEN" name="ICSID">
        <input id="ICSID" name="ICSID" value="TOKEN" type="hidden">
    """
    m = re.search(
        r'<input\b[^>]*\bname=["\']' + re.escape(name) + r'["\'][^>]*/?>',
        body, re.IGNORECASE,
    )
    if not m:
        return ""
    tag = m.group(0)
    v = re.search(r'\bvalue=["\']([^"\']*)["\']', tag, re.IGNORECASE)
    return v.group(1) if v else ""

# Delay between requests within a single session (seconds)
REQUEST_DELAY = 0.15


# ---------------------------------------------------------------------------
# Low-level HTTP helpers
# ---------------------------------------------------------------------------

def _curl_post(url: str, cookies: dict, data: dict) -> str:
    """
    POST via subprocess curl — used as a fallback when requests fails with an
    SSL error.  Returns the response body as a string, or "" on failure.
    """
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    # urlencode handles special characters properly
    form_data = urlencode(data)
    result = subprocess.run(
        [
            "curl", "-s", "-L", "-X", "POST", url,
            "-H", f"Cookie: {cookie_str}",
            "-H", "Content-Type: application/x-www-form-urlencoded",
            "-H", f"User-Agent: {HEADERS['User-Agent']}",
            "--data", form_data,
            "--max-time", "45",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(
            f"  [curl] POST failed (exit {result.returncode}): "
            f"{result.stderr.strip()[:120]}"
        )
        return ""
    return result.stdout


def _curl_get(url: str, cookies: dict = None) -> tuple[str, dict]:
    """
    GET via subprocess curl, returning (body, cookies_dict).
    Follows redirects and collects Set-Cookie headers.
    """
    cmd = [
        "curl", "-s", "-L", "-i", url,
        "-H", f"User-Agent: {HEADERS['User-Agent']}",
        "--max-time", "30",
    ]
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        cmd += ["-H", f"Cookie: {cookie_str}"]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(
            f"  [curl] GET failed (exit {result.returncode}): "
            f"{result.stderr.strip()[:120]}"
        )
        return "", {}
    raw = result.stdout
    # Split headers from body at the final blank line
    parts = raw.split("\r\n\r\n", 1) if "\r\n\r\n" in raw else raw.split("\n\n", 1)
    header_block = parts[0] if len(parts) == 2 else ""
    body = parts[1] if len(parts) == 2 else raw

    # Parse cookies from all Set-Cookie lines in header block
    cookies: dict[str, str] = {}
    for line in header_block.splitlines():
        if line.lower().startswith("set-cookie:"):
            # e.g.  Set-Cookie: PS_TOKEN=abc123; Path=/; HttpOnly
            cookie_part = line.split(":", 1)[1].strip().split(";")[0]
            if "=" in cookie_part:
                k, v = cookie_part.split("=", 1)
                cookies[k.strip()] = v.strip()
    return body, cookies


# ---------------------------------------------------------------------------
# PeopleSoft session
# ---------------------------------------------------------------------------

class PeopleSoftSession:
    """
    Manages a single stateful PeopleSoft Class Search session.

    Each instance keeps track of:
      - session cookies (populated during initialise())
      - ICSID token (refreshed from every response)
      - ICStateNum counter (incremented on every POST)
    """

    def __init__(self):
        self._session    = requests.Session()
        self._session.headers.update(HEADERS)
        self._session.verify = certifi.where()   # fix macOS LibreSSL cert issue
        self._cookies: dict[str, str] = {}   # fallback cookie store for curl
        self._icsid      = ""
        self._state_num  = 1
        self._use_curl      = False   # flipped to True on first SSL error
        self._searched_once = False   # True after any search attempt (with or without results)

    # ------------------------------------------------------------------
    # Session initialisation
    # ------------------------------------------------------------------

    def initialise(self) -> bool:
        """
        Perform the two-step session bootstrap:
          1. GET the GUEST portal URL (without following redirects) to collect
             session cookies.
          2. GET the EMPLOYEE psc URL with those cookies to load the actual
             Class Search form and extract the first ICSID token.

        Returns True on success, False if the site could not be reached.
        """
        # Step 1 — seed cookies from the GUEST portal (do NOT follow redirect)
        try:
            resp = self._session.get(PORTAL_URL, timeout=20, allow_redirects=False)
            self._cookies.update({k: v for k, v in self._session.cookies.items()})
        except requests.exceptions.SSLError:
            print("  [session] SSL error — falling back to curl for all requests")
            self._use_curl = True
            # For curl, do a non-redirecting GET to seed cookies
            result = subprocess.run(
                ["curl", "-s", "-i", "--max-redirs", "0", PORTAL_URL,
                 "-H", f"User-Agent: {HEADERS['User-Agent']}", "--max-time", "20"],
                capture_output=True, text=True,
            )
            for line in result.stdout.splitlines():
                if line.lower().startswith("set-cookie:"):
                    cookie_part = line.split(":", 1)[1].strip().split(";")[0]
                    if "=" in cookie_part:
                        k, v = cookie_part.split("=", 1)
                        self._cookies[k.strip()] = v.strip()
        except Exception as e:
            print(f"  [session] Failed to reach PeopleSoft: {e}")
            return False

        # Step 2 — load the actual Class Search form to get ICSID
        body = ""
        if not self._use_curl:
            try:
                resp = self._session.get(POST_URL, timeout=20)
                body = resp.text
                self._cookies.update({k: v for k, v in self._session.cookies.items()})
            except requests.exceptions.SSLError:
                print("  [session] SSL error on form load — switching to curl")
                self._use_curl = True
            except Exception as e:
                print(f"  [session] Failed to load Class Search form: {e}")
                return False

        if self._use_curl:
            body, extra_cookies = _curl_get(POST_URL, self._cookies)
            self._cookies.update(extra_cookies)

        icsid = _extract_field(body, "ICSID")
        if icsid:
            self._icsid = icsid
            sn = _extract_field(body, "ICStateNum")
            if sn.isdigit():
                self._state_num = int(sn)
            return True

        print("  [session] Could not find ICSID in initial page")
        return False

    # ------------------------------------------------------------------
    # Internal POST helpers
    # ------------------------------------------------------------------

    def _post_raw(self, extra_fields: dict) -> str:
        """
        Send a POST with the standard PeopleSoft form fields merged with
        extra_fields.  Updates ICSID and increments ICStateNum from the
        response.  Returns raw HTML.
        """
        payload = {
            # PeopleSoft ICPanel framework required fields
            "ICType":               "Panel",
            "ICElementNum":         "0",
            "ICStateNum":           str(self._state_num),
            "ICSID":                self._icsid,
            "ICAction":             "",
            "ICModelCancel":        "0",
            "ICXPos":               "0",
            "ICYPos":               "0",
            "ResponsetoDiffFrame":  "-1",
            "TargetFrameName":      "None",
            "FacetPath":            "None",
            "ICFocus":              "",
            "ICSaveWarningFilter":  "0",
            "ICChanged":            "-1",
            "ICSkipPending":        "0",
            "ICAutoSave":           "0",
            "ICResubmit":           "0",
            "ICAJAX":               "0",
            "ICNAVTYPEDROPDOWN":    "0",
            "ICActionPrompt":       "false",
            "ICTypeAheadID":        "",
            "ICBcDomData":          "",
            "ICPanelName":          "",
            "ICFind":               "",
            "ICAddCount":           "",
            "ICAppClsData":         "",
        }
        payload.update(extra_fields)

        time.sleep(REQUEST_DELAY)

        body = ""
        if not self._use_curl:
            try:
                resp = self._session.post(POST_URL, data=payload, timeout=45)
                body = resp.text
                self._cookies.update({k: v for k, v in self._session.cookies.items()})
            except requests.exceptions.SSLError:
                print("  [session] SSL error on POST — switching to curl")
                self._use_curl = True
            except Exception as e:
                print(f"  [session] POST error: {e}")
                return ""

        if self._use_curl:
            body = _curl_post(POST_URL, self._cookies, payload)

        if not body:
            print(f"  [WARN] Empty response from server (ICStateNum={self._state_num})")
            self._state_num += 1
            return ""

        # Sync ICSID and ICStateNum from the response.
        # PeopleSoft can advance its state counter by more than 1 during
        # certain transitions (e.g. "Modify Search"), so we read the value
        # the server embedded in the page rather than blindly incrementing.
        # _extract_field handles any attribute order, not just name-before-value.
        icsid = _extract_field(body, "ICSID")
        if icsid:
            self._icsid = icsid
            sn = _extract_field(body, "ICStateNum")
            if sn.isdigit():
                self._state_num = int(sn)
            else:
                self._state_num += 1
        else:
            self._state_num += 1

        return body

    # ------------------------------------------------------------------
    # Class Search operations
    # ------------------------------------------------------------------

    def get_terms(self) -> list[tuple[str, str]]:
        """
        Click the term dropdown button to populate it, then parse all
        <option> elements from SELECT id="CLASS_SRCH_WRK2_STRM.35.".

        Returns a list of (term_code, term_name) tuples, sorted descending
        (most recent first).
        """
        body = self._post_raw({"ICAction": "CLASS_SRCH_WRK2_STRM$35$"})
        soup = BeautifulSoup(body, "html.parser")

        select = (
            soup.find("select", {"id": "CLASS_SRCH_WRK2_STRM$35$"})
            or soup.find("select", {"id": "CLASS_SRCH_WRK2_STRM.35."})
        )
        if not select:
            # Try any select whose id looks like a term dropdown
            for sel in soup.find_all("select"):
                sid = sel.get("id", "")
                if "STRM" in sid:
                    select = sel
                    break

        if not select:
            return []

        terms = []
        for opt in select.find_all("option"):
            code = opt.get("value", "").strip()
            name = opt.get_text(strip=True)
            if code and code != "0":
                terms.append((code, name))

        # Sort descending so the most recent term is first
        terms.sort(key=lambda x: x[0], reverse=True)
        return terms

    def get_subjects(self, term_code: str) -> list[str]:
        """
        Select a term, then parse the subject dropdown
        (SELECT id="SSR_CLSRCH_WRK_SUBJECT_SRCH$0$" or similar).

        Returns a sorted list of subject abbreviation strings (e.g. ["CSC", ...]).
        """
        body = self._post_raw({
            "ICAction":                    "CLASS_SRCH_WRK2_STRM$35$",
            "CLASS_SRCH_WRK2_STRM$35$":    term_code,
            "CLASS_SRCH_WRK2_INSTITUTION$31$": "CRDNL",
        })
        soup = BeautifulSoup(body, "html.parser")

        # Try several possible id variants PeopleSoft uses
        select = None
        for candidate_id in (
            "SSR_CLSRCH_WRK_SUBJECT_SRCH$0",
            "SSR_CLSRCH_WRK_SUBJECT_SRCH$0$",
            "SSR_CLSRCH_WRK_SUBJECT_SRCH.0.",
            "SUBJECT_SRCH",
        ):
            select = soup.find("select", {"id": candidate_id})
            if select:
                break

        if not select:
            for sel in soup.find_all("select"):
                sid = sel.get("id", "")
                if "SUBJECT" in sid.upper():
                    select = sel
                    break

        if not select:
            return []

        subjects = []
        for opt in select.find_all("option"):
            val = opt.get("value", "").strip()
            if val:
                subjects.append(val)
        return sorted(subjects)

    def search_subject(
        self, term_code: str, subject: str
    ) -> list[CourseSection]:
        """
        Submit a Class Search for (term_code, subject) and parse all returned
        section rows from the results page.

        PeopleSoft requires a two-step form interaction:
          1. Select the term (triggers a form reload that populates the subject
             dropdown and advances the session state).
          2. Submit the search with the subject filled in.

        Returns a (possibly empty) list of CourseSection objects.
        """
        if self._searched_once:
            # After any prior search — results or zero-results — click
            # "Modify Search".  We cannot use the term-select POST here
            # because we could be on a results page OR a zero-results error
            # page, and the term-select action is only valid on the initial
            # blank form.  "Modify Search" works from both.
            self._post_raw({"ICAction": "CLASS_SRCH_WRK2_SSR_PB_MODIFY"})
        else:
            # Very first search in this session — select the term to load
            # the subject dropdown and advance the form state.
            self._post_raw({
                "ICAction":                        "CLASS_SRCH_WRK2_STRM$35$",
                "CLASS_SRCH_WRK2_INSTITUTION$31$": "CRDNL",
                "CLASS_SRCH_WRK2_STRM$35$":        term_code,
            })

        self._searched_once = True

        # Final step — submit the actual search.
        # Include only the companion hidden field ($chk$3) but NOT the checkbox
        # value itself.  In PeopleSoft, omitting a checkbox means unchecked,
        # so this returns ALL sections (open and closed), not just open ones.
        body = self._post_raw({
            "ICAction":                             "CLASS_SRCH_WRK2_SSR_PB_CLASS_SRCH",
            "CLASS_SRCH_WRK2_INSTITUTION$31$":      "CRDNL",
            "CLASS_SRCH_WRK2_STRM$35$":             term_code,
            "SSR_CLSRCH_WRK_SUBJECT_SRCH$0":        subject,
            "SSR_CLSRCH_WRK_SSR_OPEN_ONLY$chk$3":   "Y",
        })

        # PeopleSoft shows a confirmation dialog when >50 results are expected.
        # Detect it by the presence of the OK (#ICSave) button and auto-confirm.
        if "#ICSave" in body and "GROUPBOX2" not in body:
            body = self._post_raw({"ICAction": "#ICSave"})

        return _parse_search_results(body, term_code)


# ---------------------------------------------------------------------------
# HTML result parser
# ---------------------------------------------------------------------------

def _text(tag) -> str:
    """Safe get_text on a possibly-None tag."""
    return tag.get_text(" ", strip=True) if tag else ""


def _first_int(text: str) -> int:
    """Extract the first integer found in text, or 0."""
    m = _INT_RE.search(text)
    return int(m.group()) if m else 0


def _parse_meeting(text: str) -> SectionMeeting | None:
    """
    Parse a free-form meeting string like:
        "MoWeFr 10:00AM - 10:50AM  Pangborn G023  01/13/2025 - 05/10/2025"

    Returns a SectionMeeting, or None if the string looks empty / TBA.
    """
    text = text.strip()
    if not text or text.upper() in ("TBA", "N/A", ""):
        return None

    meeting = SectionMeeting()

    # Days of week — contiguous sequence of day abbreviations before the time
    day_match = re.match(r"^([A-Za-z]+)\s+", text)
    if day_match:
        raw_days = day_match.group(1)
        # Normalise two-letter pairs: Mo Tu We Th Fr Sa Su
        known = re.findall(r"Mo|Tu|We|Th|Fr|Sa|Su", raw_days, re.I)
        meeting.days = "".join(d.capitalize() for d in known) or raw_days

    # Times
    t = _TIME_RE.search(text)
    if t:
        meeting.start_time = t.group(1)
        meeting.end_time   = t.group(2)

    # Dates
    d = _DATE_RE.search(text)
    if d:
        meeting.start_date = d.group(1)
        meeting.end_date   = d.group(2)

    # Room — everything between the time and the date (or end of string)
    # Strip known tokens to isolate the room
    room_text = text
    for pattern in (_TIME_RE, _DATE_RE, re.compile(r"\b(Mo|Tu|We|Th|Fr|Sa|Su)+\b")):
        room_text = pattern.sub(" ", room_text)
    room_text = re.sub(r"\s+", " ", room_text).strip(" -–|")
    if room_text:
        meeting.room = room_text

    return meeting


def _unwrap_xml_response(body: str) -> str:
    """
    PeopleSoft returns two different response formats:

    1. Plain HTML  — used for small result sets and all non-results pages.
    2. XML with CDATA  — used for large result sets (> ~50 sections) after
       the user confirms the "many results" dialog.  The actual page HTML is
       embedded inside one large <![CDATA[...]]> section.

    When we get the XML format, BeautifulSoup's HTML parser misinterprets the
    structure and finds zero course divs.  This function detects the XML format
    and extracts the embedded HTML so callers always see plain HTML.
    """
    if not body.lstrip().startswith("<?xml"):
        return body          # already plain HTML

    # Find the largest CDATA section — it contains the rendered page HTML.
    cdata_blocks = re.findall(r"<!\[CDATA\[(.*?)\]\]>", body, re.DOTALL)
    if not cdata_blocks:
        return body          # malformed XML; let the parser try its luck

    return max(cdata_blocks, key=len)


def _parse_search_results(html: str, term_code: str) -> list[CourseSection]:
    """
    Parse the PeopleSoft Class Search results page into CourseSection objects.

    CUA's PeopleSoft renders results as a list of course "accordion" blocks:
      - One  win0divSSR_CLSRSLT_WRK_GROUPBOX2$N  div  per course (e.g. "CSC 113")
      - Inside each, one  SSR_CLSRCH_MTG1$scroll$M  table  per section
      - Each section table has a header row followed by one data row with columns:
            [Calendar | Class# | Section | Days&Times | Room | Instructor | Dates | Status]

    Large result sets (> ~50 courses, returned after the confirmation dialog)
    arrive as an XML envelope with the page HTML inside a CDATA block.
    _unwrap_xml_response() normalises both formats to plain HTML before parsing.
    """
    html = _unwrap_xml_response(html)
    soup = BeautifulSoup(html, "html.parser")
    sections: list[CourseSection] = []

    # Check for the "no results / refine search" indicator
    if soup.find(id="DERIVED_CLSMSG_ERROR_TEXT"):
        return []

    # Each top-level course block
    course_divs = soup.find_all(
        "div", {"id": re.compile(r"^win\d+divSSR_CLSRSLT_WRK_GROUPBOX2\$\d+")}
    )

    for cdiv in course_divs:
        # Course code is in the GP (header) sub-div: "CSC  113 - Introduction..."
        course_code = ""
        gp_div = cdiv.find(
            "div", {"id": re.compile(r"GROUPBOX2GP\$\d+")}
        )
        course_units = ""
        if gp_div:
            raw = gp_div.get_text(" ", strip=True)
            # Require the second token to be a numeric course number (optionally
            # suffixed with a letter), e.g. "472A", "113", "3XX".  This prevents
            # matching words like "See" or "NEW" that appear in header text.
            m = re.match(r"([A-Z]{2,6})\s+(\d{3,4}[A-Z]?)\b", raw)
            if m:
                course_code = f"{m.group(1)} {m.group(2)}"
            # Units appear in the header as "Units: 3.00" or just "3.00"
            u = re.search(r"[Uu]nits?:?\s*(\d+(?:\.\d+)?)", raw)
            if u:
                course_units = u.group(1)

        if not course_code:
            continue

        # Find every section table inside this course block
        sec_tables = cdiv.find_all(
            "table", {"id": re.compile(r"SSR_CLSRCH_MTG1\$scroll\$\d+")}
        )

        for tbl in sec_tables:
            rows = tbl.find_all("tr")
            # First row is the header; subsequent rows are section data
            for tr in rows[1:]:
                cells = [_text(td) for td in tr.find_all(["td", "th"])]
                # Need at least 7 columns: cal | class# | section | d&t | room | instr | dates | status
                if len(cells) < 7:
                    continue

                # col 1: class number (4-6 digits)
                class_num = cells[1].strip()
                if not re.fullmatch(r"\d{3,8}", class_num):
                    continue

                # col 2: "01-LEC Regular" or "001 Lecture" etc.
                sec_raw = cells[2].strip()
                sec_match = re.match(r"(\S+)\s+(.*)", sec_raw)
                section_number = sec_match.group(1) if sec_match else sec_raw
                component_raw  = sec_match.group(2) if sec_match else ""
                # Strip " Regular" / " Irregular" qualifiers
                component = re.sub(r"\s+(Regular|Irregular)$", "", component_raw, flags=re.I).strip()

                # col 3: days & times "TuTh 9:40AM - 10:55AM"
                days_times = cells[3].strip()
                meeting = _parse_meeting(days_times) if days_times else None

                # col 4: room
                room = cells[4].strip()
                if meeting and room:
                    meeting.room = room

                # col 6: meeting dates "08/25/2025 - 12/13/2025"
                dates = cells[6].strip()
                if meeting and dates:
                    d = _DATE_RE.search(dates)
                    if d:
                        meeting.start_date = d.group(1)
                        meeting.end_date   = d.group(2)

                # col 7: status (may be empty = Open)
                status = cells[7].strip() if len(cells) > 7 else ""

                # col 5: instructors (comma-separated names)
                instructors = []
                instr_raw = cells[5].strip()
                if instr_raw:
                    # Split on " , " (PeopleSoft uses space-comma-space between names)
                    instructors = [n.strip() for n in re.split(r",\s*", instr_raw) if n.strip()]

                section = CourseSection(
                    course_code=course_code,
                    term_code=term_code,
                    section_number=section_number,
                    class_number=class_num,
                    component=component,
                    status=status or "Open",
                    units=course_units,
                    meetings=[meeting] if meeting else [],
                    instructors=instructors,
                )
                sections.append(section)

    return sections
