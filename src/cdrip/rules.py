"""Which tracker rules are actually checked, and which only look checked.

A rule number appearing in a docstring proves nothing - it may be a passing
mention, a "see also", or a check that has since been gutted. But the reverse
direction is reliable and worth automating: a rule in ``docs/red-rules.md``
that appears *nowhere* in the source is definitely not implemented.

That asymmetry is the point of this module. It answers "what have we not got
to yet?" mechanically, so the answer does not depend on someone remembering,
and so the gap list shrinks visibly as rules get covered.

It deliberately does not try to score how *well* a rule is implemented. That
is a judgement call, and a tool that claimed to make it would be trusted more
than it deserves.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

RULE_RE = re.compile(r"\b2\.[123]\.[0-9]+(?:\.[0-9]+)*\b")
_HEADING_RE = re.compile(r"^##\s+(2\.[123]\.[0-9]+(?:\.[0-9]+)*)\s*$")

DOC_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "docs", "red-rules.md",
)

# Rules that are real but cannot be checked from here, with the reason. Being
# explicit about these keeps them out of the "not done yet" list, where they
# would be permanent noise.
NOT_MECHANICAL: dict[str, str] = {
    "2.2.6": "vinyl rip speed - not verifiable from the files",
    "2.2.8": "torrent inactivity - a property of the tracker, not the release",
    "2.3.5": "[REQ] in the title - belongs to the upload form, not the files",
    "2.3.9": "analog source lineage - free text a human must write",
    "2.3.10": "soundboard source lineage - free text a human must write",
    "2.3.17": "classical composer naming - needs editorial judgement",
    "2.3.6.1": "first-uploader capitalisation precedence - tracker state",
}

# Rules about lossy formats. cdrip only produces lossless CD rips, so these
# are out of scope rather than outstanding.
LOSSY_PREFIXES = ("2.2.9",)


@dataclass(frozen=True)
class Rule:
    number: str
    text: str

    @property
    def lossy_only(self) -> bool:
        return self.number.startswith(LOSSY_PREFIXES)

    @property
    def not_mechanical(self) -> str | None:
        return NOT_MECHANICAL.get(self.number)

    @property
    def is_heading(self) -> bool:
        """Section headings ("Lossless rules") are not rules to implement."""
        return len(self.text) < 40 and not self.text.endswith(".")


def load(path: str | None = None) -> dict[str, Rule]:
    """Parse ``docs/red-rules.md``."""
    path = path or DOC_PATH
    if not os.path.isfile(path):
        raise FileNotFoundError(
            "%s not found - the rules doc is what makes this a diff rather "
            "than a guess." % path
        )
    rules: dict[str, Rule] = {}
    current = None
    body: list[str] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            heading = _HEADING_RE.match(line.strip())
            if heading:
                if current:
                    rules[current] = Rule(current, " ".join(body).strip())
                current = heading.group(1)
                body = []
            elif current and line.strip():
                body.append(line.strip())
    if current:
        rules[current] = Rule(current, " ".join(body).strip())
    return rules


def referenced(roots: list[str]) -> set[str]:
    """Every rule number mentioned anywhere in the given source trees."""
    found: set[str] = set()
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in (".git", "__pycache__", ".venv", "node_modules")]
            for name in filenames:
                if not name.endswith((".py", ".md", ".toml", ".cfg")):
                    continue
                full = os.path.join(dirpath, name)
                # The rules doc lists every rule by definition; counting it
                # would make everything look implemented.
                if os.path.abspath(full) == os.path.abspath(DOC_PATH):
                    continue
                try:
                    with open(full, encoding="utf-8", errors="replace") as fh:
                        found |= set(RULE_RE.findall(fh.read()))
                except OSError:
                    continue
    return found


def sort_key(number: str) -> list[int]:
    return [int(part) for part in number.split(".")]


@dataclass
class Coverage:
    covered: list[Rule]
    gaps: list[Rule]
    lossy_only: list[Rule]
    not_mechanical: list[Rule]
    headings: list[Rule]

    @property
    def in_scope(self) -> int:
        return len(self.covered) + len(self.gaps)


def coverage(roots: list[str], path: str | None = None) -> Coverage:
    rules = load(path)
    seen = referenced(roots)

    covered, gaps, lossy, manual, headings = [], [], [], [], []
    for number in sorted(rules, key=sort_key):
        rule = rules[number]
        if rule.is_heading:
            headings.append(rule)
        elif rule.lossy_only:
            lossy.append(rule)
        elif rule.not_mechanical:
            manual.append(rule)
        elif number in seen:
            covered.append(rule)
        else:
            gaps.append(rule)
    return Coverage(covered=covered, gaps=gaps, lossy_only=lossy,
                    not_mechanical=manual, headings=headings)
