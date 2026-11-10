#!/usr/bin/env python3
"""Fail-closed OKR loop. Default: read-only dry-run (apart from its JSONL log).

Real mode must be explicitly selected with --execute, after operator approval.
No environment/config/credential reads, no shell execution, no wiki writes.
"""
from dataclasses import dataclass
from pathlib import Path
import re


DEFAULT_ROOT = Path("/mnt/c/MyData/Documents/wuyafang-l")
DEFAULT_HOME = Path("/home/wuyafang/.hermes")


@dataclass(frozen=True)
class KR:
    id: str
    title: str
    status: str
    owner: str
    evidence: str
    context: str = ""


def parse_okr(text):
    """Only KR table rows, never quality-guardrail/initiative/milestone rows."""
    result = []
    sections = re.split(r"(?m)^## (?=O\d)", text)
    for section in sections:
        in_kr_table = False
        for line in section.splitlines():
            if not line.startswith("|"):
                in_kr_table = False
                continue
            if re.match(r"^\|\s*KR\s*\|", line):
                in_kr_table = True
                continue
            if not in_kr_table:
                continue
            if not re.match(r"^\|\s*\*{0,2}KR\d+\.\d+\b", line):
                continue
            cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
            if len(cells) != 8:
                raise ValueError("OKR_SCHEMA: expected eight KR table columns")
            match = re.match(r"\*{0,2}(KR\d+\.\d+)\*{0,2}\s*(.*)", cells[0])
            kr_id, title = match.groups()
            related = [s for s in section.splitlines()
                       if re.match(r"^- I\d", s) and re.search(re.escape(kr_id) + r"(?!\d)", s)]
            result.append(KR(kr_id, title, cells[4], cells[6], cells[7],
                             "\n".join([line] + related)))
    ids = [kr.id for kr in result]
    if len(ids) != 9 or len(set(ids)) != 9:
        raise ValueError("OKR_SCHEMA: expected nine unique KRs")
    return result
