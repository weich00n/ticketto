"""Reads the state the invariants need from the services' MySQL schemas and Redis instances.

Read-only: SELECTs and Redis GET/ZRANGE only. Connection strings come from the environment
(see __main__.py), so the same code runs locally against docker compose and in the VPC.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pymysql
import redis

from .invariants import ActiveHold, CategoryState, ConfirmedAdmission, QueuePool


def mysql(url: str) -> pymysql.connections.Connection:
    """mysql://user:password@host:port/database"""
    from urllib.parse import unquote, urlparse

    u = urlparse(url)
    return pymysql.connect(host=u.hostname, port=u.port or 3306, user=unquote(u.username or ""),
                           password=unquote(u.password or ""), database=u.path.lstrip("/"),
                           ssl={"ssl": {}} if "ssl=true" in (u.query or "") else None,
                           cursorclass=pymysql.cursors.DictCursor, autocommit=True)


def _utc(value: datetime) -> datetime:
    # The services store naive UTC datetimes.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


# ---------- reservation ----------


def reservation_categories(db, r: redis.Redis, event_id: str) -> list[CategoryState]:
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT ci.category_id, ci.total_quantity, ci.sold_quantity,
                   COALESCE(SUM(CASE WHEN h.status = 'CONFIRMED' THEN h.quantity END), 0) AS confirmed_qty,
                   COALESCE(SUM(CASE WHEN h.status = 'ACTIVE' THEN h.quantity END), 0) AS active_qty
            FROM category_inventory ci
            LEFT JOIN hold h ON h.event_id = ci.event_id AND h.category_id = ci.category_id
            WHERE ci.event_id = %s
            GROUP BY ci.category_id, ci.total_quantity, ci.sold_quantity
            """,
            (event_id,),
        )
        rows = cur.fetchall()
    out = []
    for row in rows:
        raw = r.get(f"avail:{{{event_id}}}:{row['category_id']}")
        out.append(CategoryState(
            event_id=event_id, category_id=row["category_id"], total=int(row["total_quantity"]),
            sold=int(row["sold_quantity"]), confirmed_qty=int(row["confirmed_qty"]),
            active_qty=int(row["active_qty"]), redis_available=None if raw is None else int(raw),
        ))
    return out


def reservation_active_holds(db, event_id: str) -> list[ActiveHold]:
    with db.cursor() as cur:
        cur.execute("SELECT hold_id, hold_expires_at FROM hold WHERE event_id = %s AND status = 'ACTIVE'",
                    (event_id,))
        return [ActiveHold(row["hold_id"], _utc(row["hold_expires_at"])) for row in cur.fetchall()]


def reservation_confirmed_admissions(db, event_id: str) -> list[ConfirmedAdmission]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT admission_id, confirmed_at FROM hold WHERE event_id = %s AND status = 'CONFIRMED'",
            (event_id,),
        )
        return [ConfirmedAdmission(row["admission_id"], _utc(row["confirmed_at"])) for row in cur.fetchall()]


# ---------- queue ----------


def queue_pool(db, r: redis.Redis, event_id: str) -> QueuePool:
    with db.cursor() as cur:
        cur.execute("SELECT active_pool_capacity FROM queue_configs WHERE event_id = %s", (event_id,))
        row = cur.fetchone()
    capacity = int(row["active_pool_capacity"]) if row else 0
    members = r.zrange(f"queue:{{{event_id}}}:active", 0, -1, withscores=True)
    active = {
        (m.decode() if isinstance(m, bytes) else m): datetime.fromtimestamp(score / 1000, tz=timezone.utc)
        for m, score in members
    }
    return QueuePool(event_id=event_id, capacity=capacity, active=active)
