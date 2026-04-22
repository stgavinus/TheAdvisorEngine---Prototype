import os
import sqlite3
import uuid as uuid_mod
import secrets
from pathlib import Path
from flask import Flask, render_template, request, session, redirect, url_for, jsonify, g, abort
from itsdangerous import URLSafeSerializer, BadSignature
from src.logic import LogicEngine
from src import user_db

app = Flask(__name__)

def _load_secret_key() -> str:
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key
    key_file = Path("data/secret_key")
    if key_file.exists():
        return key_file.read_text().strip()
    key = secrets.token_hex(32)
    Path("data").mkdir(exist_ok=True)
    key_file.write_text(key)
    return key

app.secret_key = _load_secret_key()
_signer = URLSafeSerializer(app.secret_key, salt="user-id")

DB_PATH          = Path("data/cua_catalog.db")
SCHEDULE_DB_PATH = Path("data/cua_schedule.db")
engine = LogicEngine(DB_PATH)
user_db.init()


def _build_slash_node() -> dict:
    """Map every slash-separated code component → CourseNode. Computed once at startup."""
    result: dict = {}
    for cat_code, node in engine.courses.items():
        for part in cat_code.split('/'):
            part = part.strip()
            if part not in result or not result[part].credits:
                result[part] = node
    return result


_SLASH_NODE: dict = _build_slash_node()


@app.context_processor
def inject_csrf():
    return {"csrf_token": lambda: session.get("csrf_token", "")}


# ── User identity ───────────────────────────────────────────────────────────────

@app.before_request
def load_user():
    signed = request.cookies.get("user_id")
    g.new_user = not signed
    if signed:
        try:
            uid = _signer.loads(signed)
        except BadSignature:
            uid = str(uuid_mod.uuid4())
            g.new_user = True
    else:
        uid = str(uuid_mod.uuid4())
    g.user_id = uid
    user_db.ensure_user(uid)


@app.before_request
def csrf_protect():
    if request.method == "POST":
        token = session.get("csrf_token")
        if not token:
            abort(403)
        # JSON requests send token in header
        if request.is_json:
            if request.headers.get("X-CSRF-Token") == token:
                return
            abort(403)
        # Form requests send token as hidden field
        if request.form.get("csrf_token") != token:
            abort(403)


@app.before_request
def ensure_csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)


@app.after_request
def set_user_cookie(response):
    if g.get("new_user"):
        signed = _signer.dumps(g.user_id)
        response.set_cookie(
            "user_id", signed,
            max_age=60 * 60 * 24 * 365 * 5,
            samesite="Lax", httponly=True,
        )
    return response


# ── Helpers ────────────────────────────────────────────────────────────────────

def _db():
    if 'db' not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db


def _schedule_db():
    if 'schedule_db' not in g:
        conn = sqlite3.connect(SCHEDULE_DB_PATH)
        conn.row_factory = sqlite3.Row
        g.schedule_db = conn
    return g.schedule_db


@app.teardown_appcontext
def close_dbs(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()
    sdb = g.pop('schedule_db', None)
    if sdb is not None:
        sdb.close()


def _get_schedule_terms() -> list[dict]:
    """Return available terms from the schedule DB, most recent first."""
    if not SCHEDULE_DB_PATH.exists():
        return []
    conn = _schedule_db()
    rows = conn.execute(
        "SELECT code, name FROM terms ORDER BY code DESC"
    ).fetchall()
    return [{"code": r["code"], "name": r["name"]} for r in rows]


def _get_sections_for_course(course_code: str, term_code: str) -> list[dict]:
    """Return all sections for a course in a given term, with meetings and instructors.

    course_code may be a slash-separated catalog code like "CSC 113 / CSC 113H";
    we try each component individually.
    """
    if not SCHEDULE_DB_PATH.exists():
        return []
    # Expand slash-separated catalog codes into individual codes
    candidates = [p.strip() for p in course_code.split("/") if p.strip()]
    conn = _schedule_db()
    placeholders = ",".join("?" * len(candidates))
    sections = conn.execute(f"""
        SELECT id, course_code, section_number, class_number, component, status, units
        FROM course_sections
        WHERE course_code IN ({placeholders}) AND term_code = ?
        ORDER BY course_code, section_number
    """, (*candidates, term_code)).fetchall()

    result = []
    for s in sections:
        meetings = conn.execute("""
            SELECT days, start_time, end_time, room, start_date, end_date
            FROM section_meetings WHERE section_id = ?
        """, (s["id"],)).fetchall()
        instructors = conn.execute("""
            SELECT instructor_name FROM section_instructors WHERE section_id = ?
        """, (s["id"],)).fetchall()
        result.append({
            "class_number":   s["class_number"],
            "section_number": s["section_number"],
            "component":      s["component"],
            "status":         s["status"],
            "meetings": [dict(m) for m in meetings],
            "instructors": [r["instructor_name"] for r in instructors],
        })
    return result


def _get_planned_section_objects(uid: str, term_code: str) -> list[dict]:
    """Return full section + meeting data for a user's planned sections in a term."""
    if not SCHEDULE_DB_PATH.exists():
        return []
    class_numbers = user_db.get_planned_sections(uid, term_code)
    if not class_numbers:
        return []

    conn = _schedule_db()
    placeholders = ",".join("?" * len(class_numbers))
    sections = conn.execute(f"""
        SELECT id, course_code, section_number, class_number, component, status, units
        FROM course_sections
        WHERE class_number IN ({placeholders}) AND term_code = ?
    """, (*class_numbers, term_code)).fetchall()

    # Look up course titles from the catalog DB
    course_codes = list({s["course_code"] for s in sections})
    catalog_titles: dict[str, str] = {}
    if DB_PATH.exists() and course_codes:
        cat = _db()
        ph = ",".join("?" * len(course_codes))
        for row in cat.execute(
            f"SELECT code, title FROM courses WHERE code IN ({ph}) AND is_placeholder=0 LIMIT {len(course_codes)*2}",
            course_codes,
        ):
            catalog_titles.setdefault(row["code"], row["title"])

    result = []
    for s in sections:
        meetings = conn.execute("""
            SELECT days, start_time, end_time, room
            FROM section_meetings WHERE section_id = ?
        """, (s["id"],)).fetchall()
        instructors = conn.execute("""
            SELECT instructor_name FROM section_instructors WHERE section_id = ?
        """, (s["id"],)).fetchall()
        result.append({
            "course_code":    s["course_code"],
            "course_title":   catalog_titles.get(s["course_code"], ""),
            "class_number":   s["class_number"],
            "section_number": s["section_number"],
            "component":      s["component"],
            "status":         s["status"],
            "units":          s["units"] or "",
            "meetings": [dict(m) for m in meetings],
            "instructors": [r["instructor_name"] for r in instructors],
        })
    return result


def _plan_context(uid: str, eligible: list[dict]) -> dict:
    """Return a dict with all upcoming-semester plan data.

    Keys:
      plan_courses     — list of {code, title, credits}
      planned_sections — list of scheduled section objects for the current term
      current_term     — term code string
      current_term_name — human-readable term name

    plan_courses credit/title priority:
      1. actual scheduled section units
      2. eligible-list (program-aware)
      3. raw catalog node
    """
    terms             = _get_schedule_terms()
    current_term      = terms[0]["code"] if terms else ""
    current_term_name = terms[0]["name"] if terms else ""
    planned_sections  = _get_planned_section_objects(uid, current_term) if current_term else []

    for sec in planned_sections:
        user_db.add_plan_course(uid, sec["course_code"])

    plan_course_codes = user_db.get_plan_courses(uid)

    # Deduplicate: if both "MUS 325" and "MUS 325 / MUS 324H" are in the plan,
    # keep only the slash-code and drop the plain component.
    part_to_slash = {}
    for code in plan_course_codes:
        parts = [p.strip() for p in code.split("/")]
        if len(parts) > 1:
            for part in parts:
                part_to_slash[part] = code
    plan_course_codes = [c for c in plan_course_codes if c not in part_to_slash]

    eligible_lookup   = {c["code"]: c for c in eligible}
    section_units     = {s["course_code"]: s["units"] for s in planned_sections if s.get("units")}

    slash_node = _SLASH_NODE
    slash_eligible: dict = {}
    for cat_code, info in eligible_lookup.items():
        for part in cat_code.split("/"):
            part = part.strip()
            if part not in slash_eligible or not slash_eligible[part].get("credits"):
                slash_eligible[part] = info

    plan_courses = []
    for code in plan_course_codes:
        node = engine.courses.get(code) or slash_node.get(code)
        info = eligible_lookup.get(code) or slash_eligible.get(code)
        credits = (
            section_units.get(code)
            or (info or {}).get("credits")
            or (node.credits if node else "")
        )
        plan_courses.append({
            "code":    code,
            "title":   (info or {}).get("title") or (node.title if node else ""),
            "credits": credits,
        })

    return {
        "plan_courses":      plan_courses,
        "planned_sections":  planned_sections,
        "current_term":      current_term,
        "current_term_name": current_term_name,
    }


def _completed() -> set:
    return user_db.get_completed(g.user_id)


def _expand_code(code: str) -> set:
    """Return the code itself plus all slash-separated component codes."""
    parts = {p.strip() for p in code.split("/") if p.strip()}
    parts.add(code)
    return parts


@app.template_filter("is_completed")
def is_completed_filter(code: str, completed) -> bool:
    """True if any component of code (or code itself) appears in completed."""
    expanded_completed = {
        part.strip()
        for c in completed
        for part in ([c] + c.split("/"))
        if part.strip()
    }
    return bool(_expand_code(code) & expanded_completed)


def _chain_to_cytoscape(chain: dict, nodes=None, edges=None, visited=None):
    """Recursively convert prerequisite_chain() output to Cytoscape elements."""
    if nodes is None:
        nodes, edges, visited = {}, [], set()

    code = chain.get("code", "")
    if not code or chain.get("cycle"):
        return nodes, edges

    if code not in visited:
        visited.add(code)
        nodes[code] = {"data": {
            "id": code,
            "label": code,
            "title": chain.get("title", "")[:30],
            "credits": chain.get("credits", ""),
        }}
        for group in chain.get("prerequisites", []):
            for prereq in group.get("or_group", []):
                prereq_code = prereq.get("code", "")
                if prereq_code and not prereq.get("cycle"):
                    _chain_to_cytoscape(prereq, nodes, edges, visited)
                    edges.append({"data": {
                        "id": f"{prereq_code}->{code}",
                        "source": prereq_code,
                        "target": code,
                    }})

    return nodes, edges


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    conn = _db()
    rows = conn.execute(
        "SELECT name, slug, category, status FROM programs ORDER BY category, name"
    ).fetchall()

    order = ["Bachelor Degrees", "Associate Degrees", "Minors", "Certificates"]
    categories = {cat: [] for cat in order}
    for row in rows:
        cat = row["category"]
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(dict(row))

    return render_template("index.html", categories=categories)


@app.route("/my-plan")
def my_plan():
    completed     = _completed()
    program_slugs = user_db.get_programs(g.user_id)

    programs_data = []
    for slug in program_slugs:
        if slug not in engine.programs:
            continue
        prog  = engine.programs[slug]
        audit = engine.degree_audit(completed, slug)
        programs_data.append({
            "slug":     slug,
            "name":     prog.name,
            "category": prog.category,
            "audit":    audit,
        })

    # Eligible courses restricted to courses in the user's selected programs
    prog_courses: set = set()
    for slug in program_slugs:
        if slug in engine.programs:
            for block in engine.programs[slug].blocks:
                prog_courses.update(block.course_codes)

    eligible = [
        c for c in engine.eligible_courses(completed)
        if c["code"] in prog_courses
    ] if program_slugs else []

    # Upcoming semester plan
    plan_ctx          = _plan_context(g.user_id, eligible)
    plan_courses      = plan_ctx["plan_courses"]
    planned_sections  = plan_ctx["planned_sections"]
    current_term      = plan_ctx["current_term"]
    current_term_name = plan_ctx["current_term_name"]
    plan_course_codes = [c["code"] for c in plan_courses]

    return render_template("my_plan.html",
        programs=programs_data,
        completed=completed,
        eligible=eligible,
        plan_courses=plan_courses,
        plan_course_codes=plan_course_codes,
        planned_sections=planned_sections,
        current_term=current_term,
        current_term_name=current_term_name,
    )


@app.route("/my-plan/add", methods=["POST"])
def my_plan_add():
    slug = request.form.get("slug", "").strip()
    if slug and slug in engine.programs:
        user_db.add_program(g.user_id, slug)
    return redirect(url_for("my_plan"))


@app.route("/my-plan/remove", methods=["POST"])
def my_plan_remove():
    slug = request.form.get("slug", "").strip()
    user_db.remove_program(g.user_id, slug)
    return redirect(url_for("my_plan"))


@app.route("/advisor/<slug>")
def advisor(slug):
    if slug not in engine.programs:
        return redirect(url_for("index"))

    session["program"] = slug
    in_plan = slug in user_db.get_programs(g.user_id)

    completed = _completed()
    prog = engine.programs[slug]

    conn = _db()
    prog_row = conn.execute(
        "SELECT id, url FROM programs WHERE slug=?", (slug,)
    ).fetchone()
    pid = prog_row["id"]
    catalog_url = prog_row["url"]

    blocks_raw = conn.execute(
        "SELECT id, title, instruction, notes FROM requirement_blocks "
        "WHERE program_id=? ORDER BY id", (pid,)
    ).fetchall()

    blocks = []
    for b in blocks_raw:
        courses = conn.execute(
            "SELECT code, title, credits, is_placeholder "
            "FROM courses WHERE block_id=? ORDER BY id", (b["id"],)
        ).fetchall()
        courses_list = [dict(c) for c in courses]
        done = sum(1 for c in courses_list if c["code"] and is_completed_filter(c["code"], completed))
        blocks.append({
            "title":       b["title"],
            "instruction": b["instruction"],
            "notes":       b["notes"],
            "courses":     courses_list,
            "done":        done,
            "total":       len([c for c in courses_list if c["code"]]),
        })
    # Collect all course codes that appear in this program's blocks
    program_codes = set()
    for b in blocks:
        for c in b["courses"]:
            if c["code"]:
                for part in c["code"].split("/"):
                    program_codes.add(part.strip())

    # Outside courses: completed but not in any program block
    outside_courses = []
    for code in sorted(completed):
        code_parts = {p.strip() for p in code.split("/") if p.strip()}
        if not code_parts & program_codes:
            node = engine.courses.get(code)
            outside_courses.append({
                "code":    code,
                "title":   node.title if node else "",
                "credits": node.credits if node else "",
            })

    eligible = engine.eligible_courses(completed, slug)
    audit    = engine.degree_audit(completed, slug)

    plan_courses = _plan_context(g.user_id, eligible)["plan_courses"]

    return render_template("advisor.html",
        program=prog,
        blocks=blocks,
        completed=completed,
        eligible=eligible,
        audit=audit,
        in_plan=in_plan,
        catalog_url=catalog_url,
        outside_courses=outside_courses,
        plan_courses=plan_courses,
    )


@app.route("/advisor/<slug>/add", methods=["POST"])
def add_course(slug):
    code = request.form.get("course_code", "").strip().upper()
    if code and code in engine.courses:
        user_db.toggle_completed(g.user_id, code, True)
    return redirect(url_for("advisor", slug=slug))


@app.route("/advisor/<slug>/reset", methods=["POST"])
def reset(slug):
    user_db.clear_completed(g.user_id)
    return redirect(url_for("advisor", slug=slug))


@app.route("/course/<path:code>")
def course_detail(code):
    code = code.upper()
    node = engine.courses.get(code)
    if not node:
        return redirect(url_for("index"))

    completed = _completed()
    blocked   = engine.why_blocked(completed, code)

    conn = _db()
    programs_with = conn.execute("""
        SELECT DISTINCT p.name, p.slug
        FROM programs p
        JOIN requirement_blocks b ON b.program_id = p.id
        JOIN courses c ON c.block_id = b.id
        WHERE c.code = ?
        ORDER BY p.name
    """, (code,)).fetchall()

    course_components = _expand_code(code)
    prereq_groups = []
    for group in node.prerequisites.get("groups", []):
        enriched = [
            {
                "code": c,
                "title": engine.courses[c].title if c in engine.courses else "",
                "completed": is_completed_filter(c, completed),
            }
            for c in group
            if c not in course_components
        ]
        if enriched:
            prereq_groups.append(enriched)

    return render_template("course.html",
        node=node,
        blocked=blocked,
        completed=completed,
        prereq_groups=prereq_groups,
        programs_with=[dict(r) for r in programs_with],
        current_program=session.get("program"),
    )


@app.route("/audit/<slug>")
def audit(slug):
    if slug not in engine.programs:
        return redirect(url_for("index"))
    completed = _completed()
    result    = engine.degree_audit(completed, slug)
    prog      = engine.programs[slug]
    for block in result["blocks"]:
        block["remaining_courses"] = [
            {"code": c, "title": engine.courses[c].title if c in engine.courses else ""}
            for c in block["remaining_courses"]
        ]
    return render_template("audit.html", audit=result, program=prog, completed=completed)


# ── API ────────────────────────────────────────────────────────────────────────

def _enrich_why_blocked(result: dict) -> dict:
    """Add course titles to missing_groups and root_causes in a why_blocked result."""
    def title(c):
        node = engine.courses.get(c)
        return node.title if node else ""

    if result.get("missing_groups"):
        result["missing_groups"] = [
            [{"code": c, "title": title(c)} for c in group]
            for group in result["missing_groups"]
        ]
    if result.get("root_causes"):
        for rc in result["root_causes"]:
            if rc.get("missing_groups"):
                rc["missing_groups"] = [
                    [{"code": c, "title": title(c)} for c in group]
                    for group in rc["missing_groups"]
                ]
    return result


@app.route("/api/toggle-course", methods=["POST"])
def toggle_course():
    data    = request.get_json()
    code    = (data.get("code") or "").strip()
    checked = data.get("checked", False)
    if code:
        if checked:
            user_db.toggle_completed(g.user_id, code, True)
        else:
            for c in _expand_code(code):
                user_db.toggle_completed(g.user_id, c, False)
    return jsonify({"ok": True})


@app.route("/api/why-blocked")
def api_why_blocked():
    code = request.args.get("code", "").strip().upper()
    if not code:
        return jsonify({"blocked": False})
    return jsonify(_enrich_why_blocked(engine.why_blocked(_completed(), code)))


@app.route("/api/courses")
def api_courses():
    q = request.args.get("q", "").strip().upper()[:50]
    if len(q) < 2:
        return jsonify([])
    results = [
        {"code": code, "title": node.title}
        for code, node in engine.courses.items()
        if q in code.upper() or q in (node.title or "").upper()
    ]
    return jsonify(sorted(results, key=lambda x: x["code"])[:25])


@app.route("/api/advisor-state/<slug>")
def api_advisor_state(slug):
    if slug not in engine.programs:
        return jsonify({"error": "not found"}), 404
    completed = _completed()
    eligible  = engine.eligible_courses(completed, slug)
    audit     = engine.degree_audit(completed, slug)
    return jsonify({
        "eligible":           eligible,
        "completion_pct":     audit["completion_pct"],
        "graduation_eligible": audit["graduation_eligible"],
        "blocks":             [{"done": b["satisfied_count"], "total": b["required_count"]} for b in audit["blocks"]],
    })


@app.route("/api/programs")
def api_programs():
    q = request.args.get("q", "").strip().lower()[:50]
    if len(q) < 2:
        return jsonify([])
    user_slugs = set(user_db.get_programs(g.user_id))
    results = [
        {"slug": slug, "name": prog.name, "category": prog.category}
        for slug, prog in engine.programs.items()
        if q in prog.name.lower() and slug not in user_slugs
    ]
    return jsonify(sorted(results, key=lambda x: x["name"])[:15])


@app.route("/api/prereq-chain/<path:code>")
def api_prereq_chain(code):
    code      = code.upper()
    chain     = engine.prerequisite_chain(code)
    nodes_map, edges = _chain_to_cytoscape(chain)
    completed = _completed()

    nodes = list(nodes_map.values())
    for n in nodes:
        nid = n["data"]["id"]
        n["data"]["completed"] = nid in completed
        n["data"]["is_root"]   = nid == code

    return jsonify({"nodes": nodes, "edges": edges})


# ── Schedule planner ───────────────────────────────────────────────────────────

@app.route("/schedule")
def schedule():
    terms     = _get_schedule_terms()
    term_code = request.args.get("term") or (terms[0]["code"] if terms else "")
    term_name = next((t["name"] for t in terms if t["code"] == term_code), "")

    completed     = _completed()
    program_slugs = user_db.get_programs(g.user_id)
    planned       = _get_planned_section_objects(g.user_id, term_code)

    # Precompute which course codes have sections in the selected term
    offered_codes: set[str] = set()
    if SCHEDULE_DB_PATH.exists() and term_code:
        sconn = _schedule_db()
        for row in sconn.execute(
            "SELECT DISTINCT course_code FROM course_sections WHERE term_code=?",
            (term_code,),
        ):
            offered_codes.add(row["course_code"])

    # Build per-program block/course data (incomplete courses only) for the left panel
    programs_data = []
    for slug in program_slugs:
        if slug not in engine.programs:
            continue
        prog = engine.programs[slug]
        conn = _db()
        prog_row = conn.execute("SELECT id FROM programs WHERE slug=?", (slug,)).fetchone()
        if not prog_row:
            continue
        blocks_raw = conn.execute(
            "SELECT id, title FROM requirement_blocks WHERE program_id=? ORDER BY id",
            (prog_row["id"],),
        ).fetchall()
        blocks = []
        for b in blocks_raw:
            courses_raw = conn.execute(
                "SELECT code, title, credits FROM courses "
                "WHERE block_id=? AND is_placeholder=0 AND code!='' ORDER BY id",
                (b["id"],),
            ).fetchall()
            incomplete = [dict(c) for c in courses_raw
                          if not is_completed_filter(c["code"], completed)]
            if incomplete:
                blocks.append({"title": b["title"], "courses": incomplete})

        # Batch-fetch prerequisites for all component codes in this program
        component_codes = []
        for b_data in blocks:
            for c in b_data["courses"]:
                component_codes.extend(p.strip() for p in c["code"].split("/") if p.strip())
        prereqs_map: dict[str, str] = {}
        if component_codes:
            ph = ",".join("?" * len(component_codes))
            for row in conn.execute(
                f"SELECT code, prerequisites FROM course_details WHERE code IN ({ph})",
                component_codes,
            ):
                if row["prerequisites"]:
                    prereqs_map[row["code"]] = row["prerequisites"]

        # Annotate each course with offered status and prerequisites
        for b_data in blocks:
            for c in b_data["courses"]:
                parts = [p.strip() for p in c["code"].split("/") if p.strip()]
                c["offered"] = any(p in offered_codes for p in parts)
                prereq = ""
                for p in parts:
                    if p in prereqs_map:
                        raw = prereqs_map[p].strip()
                        for prefix in ("Prerequisite: ", "Prerequisites: ", "Prerequisite(s): "):
                            if raw.lower().startswith(prefix.lower()):
                                raw = raw[len(prefix):]
                                break
                        prereq = raw[:90]
                        break
                c["prereqs"] = prereq
                c["when_offered"] = ""  # filled in below for non-offered courses

        # For non-offered courses, query which terms they have appeared in historically
        non_offered_parts: list[str] = []
        for b_data in blocks:
            for c in b_data["courses"]:
                if not c["offered"]:
                    non_offered_parts.extend(
                        p.strip() for p in c["code"].split("/") if p.strip()
                    )

        history_map: dict[str, list[str]] = {}
        if non_offered_parts and SCHEDULE_DB_PATH.exists():
            ph = ",".join("?" * len(non_offered_parts))
            sconn = _schedule_db()
            for row in sconn.execute(f"""
                SELECT cs.course_code, t.name
                FROM course_sections cs
                JOIN terms t ON t.code = cs.term_code
                WHERE cs.course_code IN ({ph})
                GROUP BY cs.course_code, t.name
                ORDER BY cs.term_code
            """, non_offered_parts):
                history_map.setdefault(row["course_code"], []).append(row["name"])

        for b_data in blocks:
            for c in b_data["courses"]:
                if c["offered"]:
                    continue
                parts = [p.strip() for p in c["code"].split("/") if p.strip()]
                names = [n for p in parts for n in history_map.get(p, [])]
                if not names:
                    c["when_offered"] = "Not offered"
                    continue
                seasons: set[str] = set()
                for name in names:
                    if "Spring" in name:
                        seasons.add("spring")
                    elif "Fall" in name:
                        seasons.add("fall")
                    elif "Summer" in name:
                        seasons.add("summer")
                if len(seasons) == 1:
                    c["when_offered"] = f"Offered in {list(seasons)[0]}"
                elif seasons:
                    c["when_offered"] = "Offered in " + "/".join(sorted(seasons))
                else:
                    c["when_offered"] = "Not offered"

        if blocks:
            programs_data.append({"slug": slug, "name": prog.name, "blocks": blocks})

    # Backfill plan from already-scheduled sections (idempotent)
    for sec in planned:
        user_db.add_plan_course(g.user_id, sec["course_code"])

    # Build plan courses with credits so the JS credit counter doesn't depend on the DOM
    # (some plan courses like electives may not appear in any program block).
    _slash_node = _SLASH_NODE
    plan_courses = []
    for _code in user_db.get_plan_courses(g.user_id):
        _node = engine.courses.get(_code) or _slash_node.get(_code)
        plan_courses.append({"code": _code, "credits": _node.credits if _node else ""})

    return render_template("schedule.html",
        terms=terms,
        term_code=term_code,
        term_name=term_name,
        programs_data=programs_data,
        planned=planned,
        plan_courses=plan_courses,
    )


@app.route("/api/schedule/courses")
def api_schedule_courses():
    q    = request.args.get("q", "").strip().upper()[:20]
    term = request.args.get("term", "").strip()
    if len(q) < 2 or not term or not SCHEDULE_DB_PATH.exists():
        return jsonify([])
    conn = _schedule_db()
    rows = conn.execute("""
        SELECT DISTINCT course_code FROM course_sections
        WHERE term_code = ? AND course_code LIKE ?
        ORDER BY course_code LIMIT 25
    """, (term, f"%{q}%")).fetchall()
    conn.close()
    results = []
    for r in rows:
        code = r["course_code"]
        node = engine.courses.get(code)
        results.append({"code": code, "title": node.title if node else ""})
    return jsonify(results)


@app.route("/api/plan/courses")
def api_plan_courses_get():
    codes = user_db.get_plan_courses(g.user_id)
    slash_node = _SLASH_NODE
    result = []
    for code in codes:
        node = engine.courses.get(code) or slash_node.get(code)
        result.append({"code": code, "credits": node.credits if node else ""})
    return jsonify(result)


@app.route("/api/plan/courses", methods=["POST"])
def api_plan_courses_post():
    data   = request.get_json()
    code   = (data.get("code") or "").strip().upper()
    action = data.get("action", "add")
    if code:
        if action == "remove":
            user_db.remove_plan_course(g.user_id, code)
        else:
            user_db.add_plan_course(g.user_id, code)
    return jsonify({"ok": True})


@app.route("/api/schedule/sections")
def api_schedule_sections():
    course_code = request.args.get("code", "").strip().upper()
    term_code   = request.args.get("term", "").strip()
    if not course_code or not term_code:
        return jsonify([])
    return jsonify(_get_sections_for_course(course_code, term_code))


@app.route("/api/schedule/add", methods=["POST"])
def api_schedule_add():
    data         = request.get_json()
    class_number = (data.get("class_number") or "").strip()
    term_code    = (data.get("term_code") or "").strip()
    if class_number and term_code:
        user_db.add_planned_section(g.user_id, class_number, term_code)
    return jsonify({"ok": True})


@app.route("/api/schedule/remove", methods=["POST"])
def api_schedule_remove():
    data         = request.get_json()
    class_number = (data.get("class_number") or "").strip()
    term_code    = (data.get("term_code") or "").strip()
    if class_number and term_code:
        user_db.remove_planned_section(g.user_id, class_number, term_code)
    return jsonify({"ok": True})


@app.route("/api/schedule/planned")
def api_schedule_planned():
    term_code = request.args.get("term", "").strip()
    if not term_code:
        return jsonify([])
    return jsonify(_get_planned_section_objects(g.user_id, term_code))


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug, host="0.0.0.0", port=5001)
