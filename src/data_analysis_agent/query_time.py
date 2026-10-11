"""Explicit absolute date semantics; never parse summaries or infer relative dates."""

from datetime import date, timedelta
import re
from typing import Mapping

DATE_RANGE = re.compile(
    r"(?P<start>20\d{2}-\d{2}-\d{2})\s*(?:至|到|-|~|～)\s*"
    r"(?P<end>20\d{2}-\d{2}-\d{2})"
)
YEAR = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
DATE_LITERAL = re.compile(r"20\d{2}-\d{2}-\d{2}")
EXCLUSIVE_END = re.compile(r"不含结束日|不包含结束日|不包括结束日|截止日不含|左闭右开")


def extract_absolute_range(question: str) -> dict[str, str] | None:
    match = DATE_RANGE.search(question)
    if match:
        try:
            start = date.fromisoformat(match["start"])
            end = date.fromisoformat(match["end"])
        except ValueError:
            return None
        if end < start:
            return None
        if not EXCLUSIVE_END.search(question):
            end += timedelta(days=1)
        if end <= start:
            return None
        return {"start": start.isoformat(), "end": end.isoformat(), "end_semantics": "exclusive"}
    # A single date is not a whole-year filter. Do not broaden an unsupported
    # point-date expression into an entire year just because it contains one.
    if DATE_LITERAL.search(question):
        return None
    years = YEAR.findall(question)
    if len(years) == 1:
        year = int(years[0])
        return {"start": f"{year}-01-01", "end": f"{year + 1}-01-01", "end_semantics": "exclusive"}
    return None


def copy_time_range(value: object) -> dict[str, str] | None:
    """Read new bounds or preserve unmarked legacy bounds without reinterpreting."""
    if not isinstance(value, Mapping):
        return None
    start, end = value.get("start"), value.get("end")
    if not isinstance(start, str) or not isinstance(end, str) or not start or not end:
        return None
    output = {"start": start[:32], "end": end[:32]}
    if value.get("end_semantics") == "exclusive":
        try:
            if date.fromisoformat(start) >= date.fromisoformat(end):
                return None
        except ValueError:
            return None
        output["end_semantics"] = "exclusive"
    return output


def time_range_instruction(value: Mapping[str, str]) -> str:
    if value.get("end_semantics") == "exclusive":
        return f"{value['start']} <= 指标时间 < {value['end']}（含开始日，不含结束日）"
    return f"{value['start']} 至 {value['end']}（旧版端点未确认，需重新确认范围）"
