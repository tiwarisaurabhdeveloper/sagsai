"""
quota.py — SAGS AI Daily Message Quota
══════════════════════════════════════════════════════════════

Tracks per-user daily message usage in SQLite.
Resets automatically after 1:00 AM each day.

SETUP:
  In your .env file add:
    FREE_DAILY_LIMIT=5     # default 5, change to any number

USAGE in main.py:
  from quota import check_quota, increment_quota, QuotaExceeded
"""

import os
import aiosqlite
from datetime import datetime, date
from fastapi import HTTPException

# ── CONFIG ────────────────────────────────────────────────────────────────────
FREE_DAILY_LIMIT = int(os.getenv("FREE_DAILY_LIMIT", "5"))
QUOTA_DB_PATH    = os.getenv("QUOTA_DB_PATH", "chatbot.db")   # share with main DB


# ── DB INIT ───────────────────────────────────────────────────────────────────
async def init_quota_db():
    async with aiosqlite.connect(QUOTA_DB_PATH) as conn:
        # WAL mode = readers don't block writers, writers don't block readers
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_usage (
                user_id    TEXT NOT NULL,
                usage_date TEXT NOT NULL,
                count      INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, usage_date)
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_daily_usage_user
            ON daily_usage (user_id, usage_date)
        """)
        await conn.commit()


# ── RESET DATE LOGIC ──────────────────────────────────────────────────────────
def _quota_date() -> str:
    """
    Returns the 'quota date' for right now.
    Resets at 01:00 AM:  00:00–00:59 belongs to the PREVIOUS day's quota.
    So users get a full day from 01:00 AM → 00:59 AM next day.
    """
    now = datetime.now()
    if now.hour < 1:                          # midnight → still yesterday's quota
        from datetime import timedelta
        d = (now - timedelta(days=1)).date()
    else:
        d = now.date()
    return d.isoformat()


# ── PUBLIC API ────────────────────────────────────────────────────────────────
async def get_usage(user_id: str) -> int:
    """Return how many messages this user has sent today (quota day)."""
    qdate = _quota_date()
    async with aiosqlite.connect(QUOTA_DB_PATH) as conn:
        async with conn.execute(
            "SELECT count FROM daily_usage WHERE user_id=? AND usage_date=?",
            (user_id, qdate)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else 0


async def check_quota(user_id: str, limit: int = None) -> dict:
    """
    Check whether user_id is within their daily limit.
    Returns dict:  { allowed: bool, used: int, limit: int, remaining: int }
    Does NOT raise — caller decides what to do.
    """
    lim  = limit if limit is not None else FREE_DAILY_LIMIT
    used = await get_usage(user_id)
    return {
        "allowed":   used < lim,
        "used":      used,
        "limit":     lim,
        "remaining": max(0, lim - used),
    }


async def increment_quota(user_id: str) -> int:
    """Atomic increment — safe under concurrent requests for same user."""
    qdate = _quota_date()
    async with aiosqlite.connect(QUOTA_DB_PATH) as conn:
        # Single atomic upsert — no read-then-write race condition
        await conn.execute("""
            INSERT INTO daily_usage (user_id, usage_date, count)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id, usage_date) DO UPDATE SET count = count + 1
        """, (user_id, qdate))
        await conn.commit()
        async with conn.execute(
            "SELECT count FROM daily_usage WHERE user_id=? AND usage_date=?",
            (user_id, qdate)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else 1


class QuotaExceeded(Exception):
    """Raised when a user hits their daily limit."""
    def __init__(self, used: int, limit: int):
        self.used  = used
        self.limit = limit
        super().__init__(f"Daily limit of {limit} messages reached.")


async def enforce_quota(user_id: str, limit: int = None):
    """
    Convenience function — raises QuotaExceeded if limit hit.
    Use this inside your /chat endpoint before invoking the agent.

    Example:
        await enforce_quota(user_id)   # uses FREE_DAILY_LIMIT from .env
    """
    status = await check_quota(user_id, limit)
    if not status["allowed"]:
        raise QuotaExceeded(status["used"], status["limit"])