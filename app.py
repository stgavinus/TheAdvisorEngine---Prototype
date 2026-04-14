import os
import sqlite3
import uuid as uuid_mod
from pathlib import Path
from flask import Flask, render_template, request, session, redirect, url_for, jsonify, g
from src.logic import LogicEngine
from src import user_db

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")

DB_PATH = Path("data/cua_catalog.db")
engine = LogicEngine(DB_PATH)
user_db.init()


# ── User identity ───────────────────────────────────────────────────────────────

@app.before_request
def load_user():
    uid = request.cookies.get("user_id")
    g.new_user = not uid
    if not uid:
        uid = str(uuid_mod.uuid4())
    g.user_id = uid
    user_db.ensure_user(uid)


@app.after_request
def set_user_cookie(response):
    if g.get("new_user"):
        response.set_cookie(
            "user_id", g.user_id,
            max_age=60 * 60 * 24 * 365 * 5,
            samesite="Lax", httponly=True,
        )
    return response


# ── Helpers ────────────────────────────────────────────────────────────────────

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
    conn.close()

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

    plan = engine.next_two_years_multi(completed, program_slugs) if program_slugs else []

    return render_template("my_plan.html",
        programs=programs_data,
        completed=completed,
        eligible=eligible,
        plan=plan,
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
    pid = conn.execute(
        "SELECT id FROM programs WHERE slug=?", (slug,)
    ).fetchone()["id"]

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
    conn.close()

    eligible = engine.eligible_courses(completed, slug)
    audit    = engine.degree_audit(completed, slug)

    return render_template("advisor.html",
        program=prog,
        blocks=blocks,
        completed=completed,
        eligible=eligible,
        audit=audit,
        in_plan=in_plan,
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
    conn.close()

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
    q = request.args.get("q", "").strip().upper()
    if len(q) < 2:
        return jsonify([])
    results = [
        {"code": code, "title": node.title}
        for code, node in engine.courses.items()
        if q in code.upper() or q in (node.title or "").upper()
    ]
    return jsonify(sorted(results, key=lambda x: x["code"])[:25])


@app.route("/api/programs")
def api_programs():
    q = request.args.get("q", "").strip().lower()
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


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5001)
