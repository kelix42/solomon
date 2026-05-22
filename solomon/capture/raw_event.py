"""RawEvent — the standardized object every captured event becomes.

Every channel adapter (Gmail, Twilio, Plaud, voice notes, webhooks, plus
the synthesizer for CLI sessions) outputs one of these. From the
conductor's point of view, all events look the same.

See Part 2 of the design doc.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass
class RawEvent:
    id: str
    source: str
    received_at: datetime
    participants: List[str] = field(default_factory=list)
    raw_content: str = ""
    channel_metadata: Dict[str, Any] = field(default_factory=dict)

    def to_db_row(self, tenant_id: str, salience_score: Optional[float] = None, private: bool = False) -> Dict[str, Any]:
        return {
            "event_id": self.id,
            "tenant_id": tenant_id,
            "source": self.source,
            "received_at": self.received_at,
            "participants": json.dumps(self.participants),
            "raw_content": self.raw_content,
            "channel_metadata": json.dumps(self.channel_metadata),
            "salience_score": salience_score,
            "private": private,
            "processed_at": datetime.now(timezone.utc),
        }

    def to_json(self) -> str:
        d = asdict(self)
        d["received_at"] = self.received_at.isoformat()
        return json.dumps(d)


def raw_event_from_message(event: Any) -> Optional[RawEvent]:
    """Convert a Hermes MessageEvent (from pre_gateway_dispatch) into a
    RawEvent. Returns None if the event doesn't look like something we
    can sensibly process.

    Hermes MessageEvent shape can change across versions — this function
    is defensive and pulls fields with getattr/dict.get fallbacks so we
    don't crash on a missing attribute.
    """
    if event is None:
        return None

    # Try a few common attribute names, in order of likelihood.
    text = (
        getattr(event, "text", None)
        or getattr(event, "content", None)
        or getattr(event, "message", None)
        or (event.get("text") if isinstance(event, dict) else None)
        or ""
    )
    if not text:
        return None

    source = (
        getattr(event, "platform", None)
        or getattr(event, "source", None)
        or "gateway"
    )

    participants: List[str] = []
    user = getattr(event, "user_id", None) or getattr(event, "from_user", None) or getattr(event, "sender", None)
    if user:
        participants.append(str(user))

    metadata: Dict[str, Any] = {}
    for attr in ("chat_id", "thread_id", "channel_id", "session_id", "message_id"):
        val = getattr(event, attr, None)
        if val is not None:
            metadata[attr] = val

    event_id = (
        getattr(event, "message_id", None)
        or getattr(event, "id", None)
        or f"{source}:{int(time.time()*1000)}:{uuid.uuid4().hex[:8]}"
    )

    return RawEvent(
        id=str(event_id),
        source=str(source),
        received_at=datetime.now(timezone.utc),
        participants=participants,
        raw_content=str(text),
        channel_metadata=metadata,
    )
