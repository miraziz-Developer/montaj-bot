"""The plan as a chat message: title, summary, NUMBERED timeline (revisions refer to "3-qism"), totals."""

from collections.abc import Sequence

from app.bot import texts
from app.schemas.edit_plan import EditPlan

TELEGRAM_TEXT_LIMIT = 4096
MAX_LISTED_CLIPS = 25
_ROLE_MARK = {"hook": "🎣", "cta": "📣", "outro": "🏁"}


def mmss(seconds: float) -> str:
    total = max(0, round(seconds))
    return f"{total // 60}:{total % 60:02d}"


def _bullets(items: Sequence[str]) -> str:
    return "\n".join(f"• {item.strip()}" for item in items if item.strip())


def timeline_lines(plan: EditPlan) -> list[str]:
    """`1. 0:00–0:03 🎣` per clip, in output-timeline time, in the same order as `plan.clips`."""
    lines, offset = [], 0.0
    for i, clip in enumerate(plan.clips, start=1):
        end = offset + clip.out_duration
        mark = _ROLE_MARK.get(clip.role, "")
        lines.append(f"{i}. {mmss(offset)}–{mmss(end)} {mark}".rstrip())
        offset = end
    return lines


def plan_message(
    plan: EditPlan, changes_uz: Sequence[str] | None = None, unsupported_uz: Sequence[str] | None = None
) -> str:
    """Plain text (no parse_mode): LLM-written titles and summaries can never inject markup."""
    parts: list[str] = []
    if changes_uz and (changes := _bullets(changes_uz)):
        parts.append(f"{texts.PLAN_CHANGES}\n{changes}")
    if unsupported_uz and (unsupported := _bullets(unsupported_uz)):
        parts.append(f"{texts.PLAN_UNSUPPORTED}\n{unsupported}")
    parts.append(texts.PLAN_TITLE.format(title=plan.title.strip() or "Montaj"))
    if plan.human_summary_uz.strip():
        parts.append(plan.human_summary_uz.strip())

    lines = timeline_lines(plan)
    shown = lines[:MAX_LISTED_CLIPS]
    if len(lines) > len(shown):
        shown.append(texts.PLAN_MORE_CLIPS.format(n=len(lines) - len(shown)))
    parts.append(texts.PLAN_TIMELINE + "\n" + "\n".join(shown))
    parts.append(texts.PLAN_TOTAL.format(duration=mmss(plan.total_duration())))
    if plan.watermark.enabled:
        parts.append(texts.PLAN_WATERMARK)
    return "\n\n".join(parts)[:TELEGRAM_TEXT_LIMIT]
