"""Number checker: every number in the agent's answer must be traceable to a tool result.

Allowed sources: SQL results, calculator outputs, and the user's question. A number in the answer
matches a source value if it is that value rounded to the answer's precision (so "$1,234" matches
1234.49 and "12.5%" matches 12.4999), ignoring sign ("spent $40" vs amount -40 for income), with
k/M suffixes expanded. Dates, years and ordinals are not treated as claims.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

NUM = re.compile(r"(?<![\w.])(-?\$?-?)(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*(k|K|M|%)?(?![\w])")
DATE_LIKE = re.compile(
    r"\b\d{4}-\d{2}(?:-\d{2})?\b|\b(?:19|20)\d{2}\b|"
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?\b|"
    r"\b\d{1,2}(?:st|nd|rd|th)\b|\bQ[1-4]\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b",
)
SOURCE_NUM = re.compile(r"-?\d+(?:\.\d+)?(?:e[+-]?\d+)?")


@dataclass
class Claim:
    text: str
    value: float
    decimals: int
    scale: float  # 1, 1e3, 1e6


def claims(answer: str) -> list[Claim]:
    masked = DATE_LIKE.sub(lambda m: " " * len(m.group()), answer)
    out = []
    for m in NUM.finditer(masked):
        whole, frac, suffix = m.group(2).replace(",", ""), m.group(3) or "", m.group(4) or ""
        scale = {"k": 1e3, "K": 1e3, "M": 1e6}.get(suffix, 1.0)
        out.append(Claim(m.group(0).strip(), float(whole + frac), len(frac) - 1 if frac else 0, scale))
    return out


def source_values(texts: list[str]) -> list[float]:
    vals = []
    for t in texts:
        for m in SOURCE_NUM.findall(t.replace(",", "")):
            try:
                vals.append(abs(float(m)))
            except ValueError:
                pass
    return vals


def supported(c: Claim, sources: list[float]) -> bool:
    target = abs(c.value)
    for v in sources:
        x = v / c.scale
        for d in range(c.decimals, c.decimals + 1):
            if round(x, d) == round(target, d) or abs(x - target) <= 0.5 * 10 ** -d + 1e-9:
                return True
    return False


def check(answer: str, tool_outputs: list[str], question: str = "") -> list[str]:
    """Returns the list of unsupported number strings (empty = verified)."""
    sources = source_values(tool_outputs + [question])
    bad = []
    for c in claims(answer):
        if c.value in (0, 1) and c.scale == 1 and c.decimals == 0:
            continue  # "one", "0 charges": trivially safe, too noisy to police
        if not supported(c, sources):
            bad.append(c.text)
    return bad
