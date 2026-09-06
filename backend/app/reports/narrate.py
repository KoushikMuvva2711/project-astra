"""Turning a report's data into Astraea's prose.

The split is the point: `builders.py` computes, this narrates. The model is
given the finished numbers and asked to phrase them; it never sees the raw
tables and has no opportunity to compute anything.

When no model is available, or the model produces something unusable, the
report still renders — deterministically, from the same data. A report is a
document of record, so "no narration" must degrade to plain text rather than to
nothing.
"""

from __future__ import annotations

import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.provider import Message
from app.llm.registry import get_registry
from app.logging_config import get_logger
from app.reports.builders import Report, Section

log = get_logger(__name__)

# Astraea's briefing cap, from docs/agents/astraea.md. Enforced here as a slice
# rather than as a prompt instruction, because "at most three" is a rule the
# data layer can guarantee and a prompt can only request.
BRIEFING_ITEMS = 3

NARRATOR_PROMPT = """
You are Astraea, chief of staff to one person. You are writing a short report.

You are calm, strategic, and extremely economical with words. Lead with the
conclusion. Never build up to a point.

Rules:
- Every figure is given to you below. Quote it exactly as written. Never
  calculate, never estimate, never round, and never introduce a number that is
  not in the data.
- If a section says nothing significant happened, say that plainly and stop.
  Do not manufacture content to fill space.
- No markdown, no bullet lists, no headers. This is read aloud.
- No preamble. No "here's your report". Start with the most important thing.
- No exclamation marks. Bad news is delivered in the same register as good news.
""".strip()


def render_plain(report: Report, *, limit: int | None = None) -> str:
    """Deterministic rendering. The fallback, and the correctness baseline."""
    sections = report.top(limit) if limit else sorted(report.sections, key=lambda s: s.priority)
    if not sections:
        return "Nothing needing you today."
    return " ".join(s.headline for s in sections)


def _facts_for_model(sections: list[Section]) -> str:
    """The data the narrator is allowed to use, and nothing else."""
    return json.dumps(
        [{"topic": s.key, "summary": s.headline, "detail": s.data} for s in sections],
        default=str,
        indent=2,
    )


async def narrate(
    db: AsyncSession,
    report: Report,
    *,
    limit: int | None = None,
    max_tokens: int = 320,
) -> dict:
    """Narrate a report, falling back to plain rendering on any problem."""
    sections = report.top(limit) if limit else sorted(report.sections, key=lambda s: s.priority)
    plain = render_plain(report, limit=limit)

    if not sections:
        return {"text": plain, "narrated": False, "sections": []}

    registry = get_registry()
    messages = [
        Message(role="system", content=NARRATOR_PROMPT),
        Message(
            role="user",
            content=(
                f"Write the {report.kind.replace('_', ' ')} from this data. "
                f"Cover at most {len(sections)} items.\n\n{_facts_for_model(sections)}"
            ),
        ),
    ]

    try:
        response = await registry.complete(
            messages,
            role="reasoning",
            max_tokens=max_tokens,
            temperature=0.3,
            session=db,
            agent="astraea",
            purpose="report",
        )
        text = (response.text or "").strip()
    except Exception as exc:
        log.warning("report.narration_failed", kind=report.kind, error=str(exc))
        return {"text": plain, "narrated": False, "sections": [s.key for s in sections]}

    if not _is_usable(text, sections):
        log.info("report.narration_rejected", kind=report.kind)
        return {"text": plain, "narrated": False, "sections": [s.key for s in sections]}

    return {"text": text, "narrated": True, "sections": [s.key for s in sections]}


def _is_usable(text: str, sections: list[Section]) -> bool:
    """Reject narration that is empty, leaked JSON, or invented a figure.

    The last check is the important one. Every digit run in the narration must
    appear somewhere in the source data — a report that introduces a number
    nobody computed is exactly the failure the whole design guards against, and
    it is far more damaging in a report than in conversation because reports are
    read as settled fact.
    """
    import re

    stripped = text.strip()
    if len(stripped) < 10:
        return False
    if stripped.startswith(("{", "[")) or '"arguments"' in stripped:
        return False

    source = json.dumps(
        [{"h": s.headline, "d": s.data} for s in sections], default=str
    )
    source_digits = set(re.findall(r"\d+", re.sub(r"[,\s]", "", source)))

    for number in re.findall(r"\d+", re.sub(r"[,\s]", "", stripped)):
        # Short runs are ordinals, dates, and counts that legitimately appear in
        # prose ("three things", "the 1st"). Only meaningful figures are checked.
        if len(number) >= 3 and number not in source_digits:
            log.warning("report.invented_figure", figure=number)
            return False
    return True
