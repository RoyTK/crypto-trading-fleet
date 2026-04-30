"""Alert emission helper.

Bots, framework jobs, and ops scripts call `emit_alert(...)` to publish an
AlertEvent onto Redis. The alerting dispatcher picks it up and routes per
the taxonomy. Bots have no knowledge of routing.
"""
import json
from typing import Any, Optional
import redis

from framework.config import get_settings
from framework.audit import write_audit
from framework.logging_setup import get_logger
from monitoring.alerting.taxonomy import Severity

log = get_logger(__name__)

EMIT_CHANNEL = "alerts:emit"


def emit_alert(
    *,
    severity: Severity | str,
    title: str,
    body: str = "",
    bot_id: Optional[str] = None,
    event_type: str = "generic",
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    sev = severity.value if isinstance(severity, Severity) else severity
    payload = {
        "severity": sev,
        "title": title,
        "body": body,
        "bot_id": bot_id,
        "event_type": event_type,
        "metadata": metadata or {},
    }
    settings = get_settings()
    try:
        r = redis.Redis.from_url(settings.redis_url)
        r.publish(EMIT_CHANNEL, json.dumps(payload))
    except Exception:
        log.warning("alert_emit_failed", title=title)
    write_audit(
        "alert_emitted",
        bot_id=bot_id,
        payload={"severity": sev, "title": title, "event_type": event_type},
    )
