"""
LogicEngine — discrete-math reasoning layer for the Advisor Engine.

Formal mappings:
  eligible_courses    → Modus Ponens  (P→Q, P ∴ Q)
  why_blocked         → Modus Tollens (P→Q, ¬Q ∴ ¬P)
  prerequisite_chain  → Hypothetical Syllogism / transitivity
  validate_semester   → Biconditionals (co-reqs) + conjunction (credit limit)
  degree_audit        → Universal quantifier (∀ requirements satisfied)
"""

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

COURSE_CODE_RE = re.compile(r"\b([A-Z]{2,4})\s(\d{3,4}[A-Z]?)\b")


# ── Prerequisite parsing ───────────────────────────────────────────────────────

def _parse_prerequisites(raw: str) -> dict:
    """
    Parse a raw prerequisite string into a structured form.

    Returns:
        {
            "groups":     [["CSC 123"], ["CSC 223"]],   # AND of OR-groups
                          each inner list is an OR-group (need any one)
            "exclusions": ["MATH 111", "MATH 122"],     # must NOT have taken
            "has_placement": bool,                      # requires placement test
        }

    Examples:
        "CSC 123 or CSC 223"          → groups=[["CSC 123","CSC 223"]], excl=[]
        "CSC 123 CSC 223"             → groups=[["CSC 123"],["CSC 223"]], excl=[]
        "MATH 108 or MATH 109"        → groups=[["MATH 108","MATH 109"]], excl=[]
        MATH 121 narrative            → best-effort extraction
    """
    result = {"groups": [], "exclusions": [], "has_placement": False}

    if not raw:
        return result

    # Detect placement requirement
    if "placement" in raw.lower():
        result["has_placement"] = True

    # Strip concurrent enrollment clauses — these are co-reqs, not blocking prerequisites
    raw = re.sub(r";?\s*(?:requires?\s+)?concurrent\s+enrollment\s+in\s+[^;]+", "", raw, flags=re.I).strip()

    # Split into "required" and "exclusion" halves on "not open"
    not_open_match = re.split(r";\s*not open", raw, flags=re.I)
    required_text = not_open_match[0]
    exclusion_text = not_open_match[1] if len(not_open_match) > 1 else ""

    # Extract exclusion codes
    result["exclusions"] = COURSE_CODE_RE.findall(exclusion_text)
    result["exclusions"] = [f"{dept} {num}" for dept, num in result["exclusions"]]

    # Parse required section: split on " or " → OR-groups
    # Then within each group, find all course codes (handles AND within a group)
    or_parts = re.split(r"\bor\b", required_text, flags=re.I)

    if len(or_parts) > 1:
        # Explicit OR connectors — all codes across parts form one OR-group
        # e.g. "CSC 123 or CSC 223" → [["CSC 123", "CSC 223"]] (need any one)
        all_or_codes = []
        for part in or_parts:
            all_or_codes.extend(f"{d} {n}" for d, n in COURSE_CODE_RE.findall(part))
        if all_or_codes:
            result["groups"].append(all_or_codes)
    else:
        # No "or" — all codes are AND conditions, each its own mandatory group
        # e.g. "CSC 123 CSC 223" → [["CSC 123"], ["CSC 223"]] (need both)
        codes = [f"{d} {n}" for d, n in COURSE_CODE_RE.findall(required_text)]
        result["groups"] = [[c] for c in codes]

    return result


def _code_completed(code: str, completed: set) -> bool:
    """
    Check if a course code is in the completed set, handling compound codes
    like 'PHIL 201 / HSPH 101 / PHIL 201P' by matching any component.
    Expands both the requirement code AND the completed codes so cross-program
    compound codes (e.g. AI saves 'ENG 101 / ENG 101H / ENG 101C / ENG 101'
    while Vocal Performance requires 'ENG 101 / ENG 101H / ENG 101C') still match.
    """
    if code in completed:
        return True
    code_parts = {p.strip() for p in code.split("/") if p.strip()}
    expanded   = {p.strip() for c in completed for p in c.split("/") if p.strip()}
    return bool(code_parts & expanded)


def _prereqs_satisfied(parsed: dict, completed: set) -> bool:
    """
    Modus Ponens check: are all prerequisite groups satisfied?
    A group is satisfied if at least one course in it is in completed.
    All groups must be satisfied (AND of OR-groups).
    """
    for group in parsed["groups"]:
        if not any(_code_completed(code, completed) for code in group):
            return False
    # Check exclusions: student must NOT have taken any exclusion course
    for code in parsed["exclusions"]:
        if _code_completed(code, completed):
            return False
    return True


def _missing_groups(parsed: dict, completed: set) -> list:
    """
    Modus Tollens: return the unsatisfied groups that block a course.
    Each entry is an OR-group (list of alternatives) the student hasn't met.
    """
    missing = []
    for group in parsed["groups"]:
        if not any(_code_completed(code, completed) for code in group):
            missing.append(group)
    violated_exclusions = [c for c in parsed["exclusions"] if _code_completed(c, completed)]
    return missing, violated_exclusions


# ── Data loading ───────────────────────────────────────────────────────────────

@dataclass
class CourseNode:
    code: str
    title: str
    credits: str
    description: str
    prerequisites_raw: str
    prerequisites: dict = field(default_factory=dict)  # parsed form
    url: str = ""


@dataclass
class BlockRequirement:
    title: str
    instruction: str        # "Take All", "Take One", "Take Two", etc.
    required_count: int     # parsed from instruction: 0 = all, N = pick N
    course_codes: list      # codes in this block


@dataclass
class ProgramRequirements:
    name: str
    slug: str
    category: str
    year: str
    blocks: list            # list of BlockRequirement


def _parse_instruction(title: str, instruction: str) -> int:
    """
    Parse how many courses are required from a block.
    Returns 0 to mean "all".
    Checks the instruction field first, then falls back to the block title.
    """
    text = (instruction or title or "").lower()
    m = re.search(r"take\s+(one|two|three|four|five|\d+)", text, re.I)
    if not m:
        return 0  # Take All
    word = m.group(1).lower()
    mapping = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
    return mapping.get(word, int(word) if word.isdigit() else 0)


class LogicEngine:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.courses: dict[str, CourseNode] = {}       # code → CourseNode
        self.programs: dict[str, ProgramRequirements] = {}  # slug → ProgramRequirements
        self._load()

    # ── Loading ────────────────────────────────────────────────────────────────

    def _load(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        # Load all course details
        for row in conn.execute("SELECT * FROM course_details"):
            parsed = _parse_prerequisites(row["prerequisites"])
            self.courses[row["code"]] = CourseNode(
                code=row["code"],
                title="",
                credits="",
                description=row["description"],
                prerequisites_raw=row["prerequisites"],
                prerequisites=parsed,
                url=row["url"],
            )

        # Enrich with title/credits from courses table
        for row in conn.execute("SELECT DISTINCT code, title, credits FROM courses WHERE code != ''"):
            if row["code"] in self.courses:
                self.courses[row["code"]].title = row["title"]
                self.courses[row["code"]].credits = row["credits"]
            else:
                self.courses[row["code"]] = CourseNode(
                    code=row["code"],
                    title=row["title"],
                    credits=row["credits"],
                    description="",
                    prerequisites_raw="",
                    prerequisites=_parse_prerequisites(""),
                )

        # Load programs and their requirement blocks
        for prog_row in conn.execute("SELECT * FROM programs"):
            blocks = []
            for block_row in conn.execute(
                "SELECT * FROM requirement_blocks WHERE program_id = ?", (prog_row["id"],)
            ):
                codes = [
                    r["code"] for r in conn.execute(
                        "SELECT code FROM courses WHERE block_id = ? AND code != ''",
                        (block_row["id"],)
                    )
                ]
                required_count = _parse_instruction(block_row["title"], block_row["instruction"])
                blocks.append(BlockRequirement(
                    title=block_row["title"],
                    instruction=block_row["instruction"],
                    required_count=required_count,
                    course_codes=codes,
                ))
            self.programs[prog_row["slug"]] = ProgramRequirements(
                name=prog_row["name"],
                slug=prog_row["slug"],
                category=prog_row["category"],
                year=prog_row["year"],
                blocks=blocks,
            )

        conn.close()

    # ── Core logic methods ─────────────────────────────────────────────────────

    def eligible_courses(self, completed: set, program_slug: str = None) -> list[dict]:
        """
        Modus Ponens: given completed courses, return all courses the student
        can now take (prerequisites satisfied, not already completed).

        If program_slug given, restricts results to courses in that program.
        """
        candidates = {}

        if program_slug and program_slug in self.programs:
            prog = self.programs[program_slug]
            for block in prog.blocks:
                for code in block.course_codes:
                    candidates[code] = self.courses.get(code)
        else:
            candidates = self.courses

        eligible = []
        for code, node in candidates.items():
            if _code_completed(code, completed):
                continue
            if node is None:
                # No course details — assume no prerequisites, treat as eligible
                eligible.append({"code": code, "title": "", "credits": "", "prerequisites_raw": ""})
                continue
            if _prereqs_satisfied(node.prerequisites, completed):
                eligible.append({
                    "code": code,
                    "title": node.title,
                    "credits": node.credits,
                    "prerequisites_raw": node.prerequisites_raw,
                })

        return sorted(eligible, key=lambda x: x["code"])

    def why_blocked(self, completed: set, course_code: str, _visited: set = None) -> dict:
        """
        Modus Tollens: explain exactly why a student cannot take a course.
        Works backward through the prerequisite chain to find root causes.

        Returns a dict with:
          - blocked: bool
          - already_completed: bool
          - missing_groups: list of OR-groups the student hasn't satisfied
          - violated_exclusions: courses taken that exclude this course
          - root_causes: prerequisite codes the student needs but can't yet take
        """
        if _visited is None:
            _visited = set()
        if course_code in _visited:
            return {"blocked": False, "already_completed": False}
        _visited.add(course_code)

        if course_code in completed:
            return {"blocked": False, "already_completed": True}

        node = self.courses.get(course_code)
        if not node:
            return {"blocked": True, "reason": "Course not found in catalog"}

        missing, violated = _missing_groups(node.prerequisites, completed)

        if not missing and not violated:
            return {"blocked": False, "already_completed": False}

        # Recursively find root causes — the earliest missing prerequisite
        root_causes = []
        for group in missing:
            for prereq_code in group:
                sub = self.why_blocked(completed, prereq_code, set(_visited))
                if sub.get("blocked") and sub.get("missing_groups"):
                    root_causes.append({
                        "code": prereq_code,
                        "missing_groups": sub["missing_groups"],
                    })

        return {
            "blocked": True,
            "already_completed": False,
            "course_code": course_code,
            "course_title": node.title,
            "missing_groups": missing,
            "violated_exclusions": violated,
            "root_causes": root_causes,
        }

    def prerequisite_chain(self, course_code: str, _visited: set = None) -> dict:
        """
        Hypothetical Syllogism / transitivity:
        Build the full ancestor graph for a course.

        Returns a nested dict representing the prerequisite tree.
        Identifies gateway courses (ancestors that unlock the most descendants).
        """
        if _visited is None:
            _visited = set()
        if course_code in _visited:
            return {"code": course_code, "cycle": True}
        _visited.add(course_code)

        node = self.courses.get(course_code)
        if not node:
            return {"code": course_code, "title": "Unknown", "prerequisites": []}

        prereq_trees = []
        for group in node.prerequisites["groups"]:
            group_trees = []
            for prereq_code in group:
                group_trees.append(
                    self.prerequisite_chain(prereq_code, set(_visited))
                )
            prereq_trees.append({"or_group": group_trees})

        return {
            "code": course_code,
            "title": node.title,
            "credits": node.credits,
            "prerequisites": prereq_trees,
        }

    def gateway_courses(self, program_slug: str) -> list[dict]:
        """
        Find the courses in a program that are prerequisites for the most
        other courses — the "gateway" requirements for graduation.
        """
        if program_slug not in self.programs:
            return []

        prog = self.programs[program_slug]
        all_codes = {code for block in prog.blocks for code in block.course_codes}

        unlock_count = {}
        for code in all_codes:
            count = sum(
                1 for other in all_codes
                if other != code and self._is_ancestor(code, other)
            )
            if count > 0:
                unlock_count[code] = count

        return sorted(
            [{"code": c, "title": self.courses[c].title if c in self.courses else "",
              "unlocks": n} for c, n in unlock_count.items()],
            key=lambda x: x["unlocks"],
            reverse=True,
        )

    def _is_ancestor(self, ancestor: str, descendant: str, _visited: set = None) -> bool:
        """Check if ancestor appears anywhere in descendant's prerequisite chain."""
        if _visited is None:
            _visited = set()
        if descendant in _visited:
            return False
        _visited.add(descendant)

        node = self.courses.get(descendant)
        if not node:
            return False
        for group in node.prerequisites["groups"]:
            for prereq in group:
                if prereq == ancestor:
                    return True
                if self._is_ancestor(ancestor, prereq, _visited):
                    return True
        return False

    def validate_semester(self, completed: set, proposed: list, credit_limit: int = 18) -> dict:
        """
        Validate a proposed semester plan.
        Checks:
          1. Prerequisites met for each course (Modus Ponens)
          2. Co-requisites satisfied within the proposed set (Biconditional p↔q)
          3. Credit limit not exceeded (conjunction)

        Returns validation result with specific failure reasons.
        """
        errors = []
        warnings = []

        # Treat proposed courses as available for co-req checking
        available = completed | set(proposed)

        for code in proposed:
            node = self.courses.get(code)
            if not node:
                warnings.append(f"{code}: not found in catalog")
                continue

            # Check prerequisites (proposed courses count as co-reqs)
            if not _prereqs_satisfied(node.prerequisites, available):
                missing, violated = _missing_groups(node.prerequisites, available)
                errors.append({
                    "code": code,
                    "type": "missing_prerequisite",
                    "missing_groups": missing,
                    "violated_exclusions": violated,
                })

        # Check co-requisites (biconditional: p↔q — must have both or neither)
        coreqs = self.detect_corequisites()
        for code in proposed:
            if code in coreqs:
                for partner in coreqs[code]:
                    if partner not in proposed and partner not in completed:
                        errors.append({
                            "code": code,
                            "type": "missing_corequisite",
                            "required_with": partner,
                        })

        # Check credit limit
        total_credits = 0
        for code in proposed:
            node = self.courses.get(code)
            if node and node.credits:
                try:
                    total_credits += int(node.credits.split("-")[0])
                except (ValueError, AttributeError):
                    pass

        if total_credits > credit_limit:
            errors.append({
                "type": "credit_overload",
                "total": total_credits,
                "limit": credit_limit,
            })

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
            "total_credits": total_credits,
        }

    def degree_audit(self, completed: set, program_slug: str) -> dict:
        """
        Universal quantifier check (∀):
        For every requirement block, is the requirement satisfied?

        Returns per-block status and an overall graduation eligibility flag.
        """
        if program_slug not in self.programs:
            return {"error": "Program not found"}

        prog = self.programs[program_slug]
        block_results = []
        total_required = 0
        total_satisfied = 0

        for block in prog.blocks:
            required_count = block.required_count
            if required_count == 0:
                # Take All
                needed = len(block.course_codes)
            else:
                needed = required_count

            satisfied = [c for c in block.course_codes if _code_completed(c, completed)]
            remaining = [c for c in block.course_codes if not _code_completed(c, completed)]
            met = len(satisfied) >= needed

            total_required += needed
            total_satisfied += min(len(satisfied), needed)

            block_results.append({
                "title": block.title,
                "required_count": needed,
                "satisfied_count": len(satisfied),
                "met": met,
                "satisfied_courses": satisfied,
                "remaining_courses": remaining[:10],  # cap for display
                "remaining_total": len(remaining),
            })

        graduation_eligible = all(b["met"] for b in block_results)

        return {
            "program": prog.name,
            "graduation_eligible": graduation_eligible,
            "blocks": block_results,
            "total_required": total_required,
            "total_satisfied": total_satisfied,
            "completion_pct": round(100 * total_satisfied / total_required, 1) if total_required else 0,
        }

    def detect_corequisites(self) -> dict:
        """
        Biconditional detection (p↔q):
        Two courses are co-requisites if they mutually list each other
        as prerequisites. Returns {code: [co-req codes]}.
        """
        coreqs = {}
        for code, node in self.courses.items():
            all_prereqs = [c for group in node.prerequisites["groups"] for c in group]
            for prereq_code in all_prereqs:
                prereq_node = self.courses.get(prereq_code)
                if not prereq_node:
                    continue
                prereq_prereqs = [
                    c for g in prereq_node.prerequisites["groups"] for c in g
                ]
                if code in prereq_prereqs:
                    coreqs.setdefault(code, [])
                    if prereq_code not in coreqs[code]:
                        coreqs[code].append(prereq_code)
        return coreqs

