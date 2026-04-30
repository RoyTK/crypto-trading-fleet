"""Alert taxonomy and routing matrix.

Severity → channels:
  P0: Twilio SMS + Discord ping + Telegram
  P1: Discord ping + Telegram
  P2: Discord (no ping)
  P3: daily digest (collected, not pushed)

Source of truth lives here. Bots and framework code never make routing
decisions themselves — they just emit `AlertEvent(severity=..., ...)`.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class Severity(str, Enum):
    P0 = "p0"
    P1 = "p1"
    P2 = "p2"
    P3 = "p3"


@dataclass
class AlertEvent:
    severity: Severity
    title: str
    body: str
    bot_id: Optional[str] = None
    event_type: str = "generic"
    metadata: dict[str, Any] = field(default_factory=dict)


CHANNEL_MATRIX: dict[Severity, list[str]] = {
    Severity.P0: ["twilio", "discord_ping", "telegram"],
    Severity.P1: ["discord_ping", "telegram"],
    Severity.P2: ["discord"],
    Severity.P3: ["digest"],
}


def channels_for(severity: Severity) -> list[str]:
    return list(CHANNEL_MATRIX.get(severity, []))
