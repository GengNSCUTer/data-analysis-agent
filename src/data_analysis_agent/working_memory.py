"""Small, validated state object for multi-turn Text-to-SQL clarification."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Mapping

from .question_router import QuestionRoute


_DATE_RANGE = re.compile(
    r"(?P<start>20\d{2}-\d{2}-\d{2})\s*(?:至|到|到|-|~|～)\s*"
    r"(?P<end>20\d{2}-\d{2}-\d{2})"
)
_YEAR = re.compile(r"(?<!\d)(20\d{2})(?!\d)")


@dataclass(frozen=True)
class WorkingMemory:
    """Server-owned, JSON-safe state; never inferred from assistant prose."""

    metric_ids: tuple[str, ...] = ()
    time_range: dict[str, str] | None = None
    dimensions: tuple[str, ...] = ()
    filters: tuple[str, ...] = ()
    comparison_baseline: str | None = None
    previous_result_summary: str | None = None
    pending_question: str | None = None
    pending_missing: tuple[str, ...] = ()
    _MAX_TEXT: int = field(default=4000, init=False, repr=False)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "WorkingMemory":
        if not isinstance(value, Mapping):
            return cls()

        def strings(key: str, limit: int = 16) -> tuple[str, ...]:
            raw = value.get(key, ())
            if not isinstance(raw, (list, tuple)):
                return ()
            return tuple(str(item).strip() for item in raw if str(item).strip())[:limit]

        raw_range = value.get("time_range")
        time_range = None
        if isinstance(raw_range, Mapping):
            start = raw_range.get("start")
            end = raw_range.get("end")
            if isinstance(start, str) and isinstance(end, str) and start and end:
                time_range = {"start": start[:32], "end": end[:32]}

        def text(key: str, limit: int = 4000) -> str | None:
            item = value.get(key)
            return item.strip()[:limit] if isinstance(item, str) and item.strip() else None

        return cls(
            metric_ids=strings("metric_ids"),
            time_range=time_range,
            dimensions=strings("dimensions"),
            filters=strings("filters"),
            comparison_baseline=text("comparison_baseline", 120),
            previous_result_summary=text("previous_result_summary", 1000),
            pending_question=text("pending_question"),
            pending_missing=strings("pending_missing"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric_ids": list(self.metric_ids),
            "time_range": dict(self.time_range) if self.time_range else None,
            "dimensions": list(self.dimensions),
            "filters": list(self.filters),
            "comparison_baseline": self.comparison_baseline,
            "previous_result_summary": self.previous_result_summary,
            "pending_question": self.pending_question,
            "pending_missing": list(self.pending_missing),
        }

    def apply(self, question: str, route: QuestionRoute) -> "WorkingMemory":
        """Merge explicit facts from a turn; do not parse arbitrary answer prose."""
        time_range = _extract_time_range(question) or self.time_range
        comparison = _extract_comparison(question) or self.comparison_baseline
        metrics = route.metric_ids or self.metric_ids
        pending_question = question.strip()[: self._MAX_TEXT]
        pending_missing = route.missing if route.state not in {"answerable"} else ()
        if route.state in {"unauthorized", "unsupported"}:
            pending_question = None
            pending_missing = ()
        if route.state == "answerable":
            pending_question = None
        return WorkingMemory(
            metric_ids=tuple(dict.fromkeys(metrics))[:16],
            time_range=time_range,
            dimensions=self.dimensions,
            filters=self.filters,
            comparison_baseline=comparison,
            previous_result_summary=self.previous_result_summary,
            pending_question=pending_question,
            pending_missing=tuple(pending_missing),
        )

    def retrieval_context(self, question: str) -> str:
        """Return a bounded retrieval-only query, never persisted as trace text."""
        parts = [question.strip()]
        if self.pending_question and self.pending_question not in parts[0]:
            parts.append(self.pending_question)
        parts.extend(self.metric_ids)
        if self.time_range:
            parts.extend(self.time_range.values())
        if self.comparison_baseline:
            parts.append(self.comparison_baseline)
        return " ".join(parts)[: self._MAX_TEXT]

    def prompt_context(self) -> str:
        """Render only validated state for the LLM; omit empty fields."""
        lines: list[str] = []
        if self.metric_ids:
            lines.append(f"- 已确认指标：{', '.join(self.metric_ids)}")
        if self.time_range:
            lines.append(
                f"- 已确认时间：{self.time_range['start']} 至 {self.time_range['end']}"
            )
        if self.dimensions:
            lines.append(f"- 已确认维度：{', '.join(self.dimensions)}")
        if self.filters:
            lines.append(f"- 已确认筛选：{'; '.join(self.filters)}")
        if self.comparison_baseline:
            lines.append(f"- 已确认比较基线：{self.comparison_baseline}")
        if not lines:
            return ""
        return "\n### 本轮已确认的结构化分析状态\n" + "\n".join(lines)


def _extract_time_range(question: str) -> dict[str, str] | None:
    match = _DATE_RANGE.search(question)
    if match:
        return {"start": match.group("start"), "end": match.group("end")}
    years = _YEAR.findall(question)
    if len(years) == 1:
        return {"start": f"{years[0]}-01-01", "end": f"{years[0]}-12-31"}
    return None

def _extract_comparison(question: str) -> str | None:
    if "同比" in question:
        return "year_over_year"
    if "环比" in question or "上月" in question or "上个月" in question:
        return "previous_period"
    return None
