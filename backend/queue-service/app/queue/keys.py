"""Redis key names. Per-event keys carry the {event_id} hash tag so all of an
event's keys share one cluster slot (required by the multi-key Lua scripts).
"""

from __future__ import annotations

# Global keys: only ever touched by plain commands, never inside a script.
LIVE_EVENTS = "queue:live_events"  # SET of event_ids that are OPEN or ADMITTING
DRAINING_EVENTS = "queue:draining_events"  # SET of CLOSED event_ids with admissions left
ADMISSION_INDEX = "queue:admission_index"  # HASH admission_id -> event_id
ADMITTER_HEARTBEAT = "queue:admitter:heartbeat"  # STRING server time (ms), short TTL
CONSUMER_HEARTBEAT_PREFIX = "queue:consumer:heartbeat:"  # + queue name; same format


def consumer_heartbeat(queue_name: str) -> str:
    return CONSUMER_HEARTBEAT_PREFIX + queue_name  # one per consumer thread


def _event(event_id: str, suffix: str) -> str:
    return f"queue:{{{event_id}}}:{suffix}"


def config(event_id: str) -> str:
    return _event(event_id, "config")  # HASH cached config


def prequeue(event_id: str) -> str:
    return _event(event_id, "prequeue")  # SET user_ids who joined while OPEN


def shuffled(event_id: str) -> str:
    return _event(event_id, "shuffled")  # STRING flag, set once at release


def seq(event_id: str) -> str:
    return _event(event_id, "seq")  # STRING counter for FIFO order


def waiting(event_id: str) -> str:
    return _event(event_id, "waiting")  # ZSET user_id -> seq


def active(event_id: str) -> str:
    return _event(event_id, "active")  # ZSET admission_id -> admission_expires_at (ms)


def user_admission(event_id: str) -> str:
    return _event(event_id, "user_adm")  # HASH user_id -> admission_id


def admission_prefix(event_id: str) -> str:
    """Prefix scripts append an admission_id to; same hash tag, so same slot."""
    return _event(event_id, "adm:")


def admission(event_id: str, admission_id: str) -> str:
    return admission_prefix(event_id) + admission_id  # HASH admission fields
