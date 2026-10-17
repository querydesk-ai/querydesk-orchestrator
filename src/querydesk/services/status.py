"""
Status answers (PRD rule 1, user story 5): "status of my requests" lists the
user's open SRs; "any update on SR-1042" reports that one SR.

Read-only: it asks the ticketing service and never records anything. An SR
that belongs to someone else gets the same reply as one that doesn't exist,
so a user can't learn about other people's requests by guessing numbers.
"""

from datetime import datetime

from querydesk.services import catalog
from querydesk.services.decision.signals import sr_number
from querydesk.services.ticketing import client as ticketing

# Enough for a chat bubble; the rest are summarised.
MAX_LISTED = 10

_LABELS = {
    "PENDING": "Pending",
    "APPROVED": "Approved",
    "REJECTED": "Rejected",
    "IN_PROGRESS": "In progress",
    "RESOLVED": "Resolved",
    "REVERTED": "Reverted",
}


def _category_name(category_id: str) -> str:
    try:
        return catalog.get(category_id).name
    except catalog.UnknownCategoryError:
        return category_id


def _updated(updated_at: str | None) -> str:
    if not updated_at:
        return ""
    try:
        when = datetime.fromisoformat(updated_at)
    except ValueError:
        return ""
    return f" (updated {when.strftime('%d %b %H:%M')} UTC)"


def _line(sr: ticketing.SRRecord) -> str:
    status = _LABELS.get(sr.status, sr.status)
    return f"SR {sr.id}: {_category_name(sr.category_id)}, {status}{_updated(sr.updated_at)}"


async def answer(user_id: str, query: str) -> str:
    """The reply to a status question. Raises TicketingError if the service fails."""
    if number := sr_number(query):
        sr = await ticketing.get_sr(number)
        if sr is None or sr.user_id != user_id:
            return f"I can't find SR {number} among your requests."
        return _line(sr)

    srs = await ticketing.list_open_srs(user_id)
    if not srs:
        return "You have no open requests."
    srs.sort(key=lambda sr: sr.updated_at or "", reverse=True)
    noun = "request" if len(srs) == 1 else "requests"
    lines = [f"You have {len(srs)} open {noun}:"]
    lines += [f"- {_line(sr)}" for sr in srs[:MAX_LISTED]]
    if len(srs) > MAX_LISTED:
        lines.append(f"…and {len(srs) - MAX_LISTED} more.")
    return "\n".join(lines)
