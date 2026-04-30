from typing import Any, Optional
from framework.db import session_scope
from framework.models import AuditLog


def write_audit(
    event_type: str,
    *,
    bot_id: Optional[str] = None,
    actor: str = "system",
    payload: Optional[dict[str, Any]] = None,
    note: Optional[str] = None,
) -> int:
    """Append-only audit event. Returns id."""
    with session_scope() as s:
        row = AuditLog(
            event_type=event_type,
            bot_id=bot_id,
            actor=actor,
            payload=payload,
            note=note,
        )
        s.add(row)
        s.flush()
        return row.id
