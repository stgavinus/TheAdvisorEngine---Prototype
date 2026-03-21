from dataclasses import dataclass, field
from typing import List

@dataclass
class Course:
    code: str           # "BIO 101", or "" if placeholder
    title: str
    credits: str        # kept as str: some are "3-6", "variable", etc.
    url: str = ""       # link to course detail page, if present
    is_placeholder: bool = False  # True for entries like "Biology Elective"
    source: str = "table"         # "table" | "narrative" | "list"

@dataclass
class CourseDetail:
    code: str
    description: str = ""
    prerequisites: str = ""
    cross_listed: str = ""
    url: str = ""

@dataclass
class RequirementBlock:
    title: str
    instruction: str = ""          # e.g. "Take One", "Take Three"
    courses: List[Course] = field(default_factory=list)
    notes: str = ""                # narrative text that has no course codes

@dataclass
class Program:
    name: str
    slug: str
    category: str
    url: str
    year: str
    blocks: List[RequirementBlock] = field(default_factory=list)
    status: str = "COMPLETE"      # COMPLETE | PARTIAL | NARRATIVE | EMPTY
