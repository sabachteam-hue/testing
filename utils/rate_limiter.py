"""Rate limiter with Redis support and an in-memory thread-safe fallback.

Provides:
- Sliding-window request rate limiting
- Brute-force failure tracking with temporary lockout
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict
from typing import Optional

logger = logging.getLogger(__name__)

_REDIS_URL = os.getenv("REDIS_URL", "").strip() or None
_redis_client = None
_redis_init_attempted = False

# In-memory storage structures (used when Redis is not configured or unavailable)
# _memory_windows: key -> list of float timestamps
_memory_windows: dict[str, list[float]] = defaultdict(list)
# _memory_failures: key -> list of float timestamps (failed attempts)
_memory_failures: dict[str, list[float]] = defaultdict(list)
# _memory_lockouts: key -> lockout expiry timestamp
_memory_lockouts: dict[str, float] = {}

_mem_lock = asyncio.Lock()


async def get_redis():
    global _redis_client, _redis_init_attempted
    if _redis_client is not None:
        return _redis_client
    if _redis_init_attempted or not _REDIS_URL:
        return None

    _redis_init_attempted = True
    try:
        import redis.asyncio as aioredis
        client = aioredis.from_url(_REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        await client.ping()
        _redis_client = client
        logger.info("Rate limiter connected to Redis.")
        return _redis_client
    except Exception as exc:
        logger.warning(f"Could not connect to Redis for rate limiting ({exc}). Using in-memory fallback.")
        _redis_client = None
        return None


async def check_rate_limit(key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
    """Check if an action keyed by `key` is permitted under `limit` requests per `window_seconds`.
    Returns:
        (allowed: bool, retry_after: int)
    """
    now = time.time()
    r = await get_redis()
    if r is not None:
        try:
            redis_key = f"rl:{key}"
            pipe = r.pipeline()
            # Remove timestamps older than window
            pipe.zremrangebyscore(redis_key, 0, now - window_seconds)
            # Count remaining
            pipe.zcard(redis_key)
            # Add current timestamp
            pipe.zadd(redis_key, {str(now): now})
            # Set TTL
            pipe.expire(redis_key, window_seconds + 5)
            results = await pipe.execute()
            count = results[1]
            if count >= limit:
                # Disallowed: remove the timestamp we just added
                await r.zrem(redis_key, str(now))
                return False, int(window_seconds)
            return True, 0
        except Exception as exc:
            logger.debug(f"Redis rate limit error, falling back to memory: {exc}")

    # In-memory sliding window
    async with _mem_lock:
        cutoff = now - window_seconds
        timestamps = [ts for ts in _memory_windows[key] if ts > cutoff]
        if len(timestamps) >= limit:
            oldest = timestamps[0]
            retry_after = max(int(oldest + window_seconds - now), 1)
            _memory_windows[key] = timestamps
            return False, retry_after

        timestamps.append(now)
        _memory_windows[key] = timestamps
        return True, 0


async def is_locked_out(key: str) -> tuple[bool, int]:
    """Check if key is currently in a temporary lockout period.
    Returns:
        (is_locked: bool, retry_after: int)
    """
    now = time.time()
    r = await get_redis()
    if r is not None:
        try:
            lockout_key = f"lockout:{key}"
            ttl = await r.ttl(lockout_key)
            if ttl > 0:
                return True, int(ttl)
            return False, 0
        except Exception as exc:
            logger.debug(f"Redis lockout check error: {exc}")

    async with _mem_lock:
        lockout_until = _memory_lockouts.get(key, 0)
        if lockout_until > now:
            return True, max(int(lockout_until - now), 1)
        if key in _memory_lockouts:
            del _memory_lockouts[key]
        return False, 0


async def record_failure_and_check_lockout(
    key: str,
    max_failures: int = 5,
    window_seconds: int = 600,
    lockout_seconds: int = 900,
) -> tuple[bool, int]:
    """Record a failed attempt (e.g. bad password) and trigger temporary lockout if threshold reached.
    Returns:
        (is_now_locked: bool, retry_after: int)
    """
    now = time.time()

    # First check if already locked out
    locked, retry_after = await is_locked_out(key)
    if locked:
        return True, retry_after

    r = await get_redis()
    if r is not None:
        try:
            failures_key = f"fail:{key}"
            lockout_key = f"lockout:{key}"
            pipe = r.pipeline()
            pipe.zremrangebyscore(failures_key, 0, now - window_seconds)
            pipe.zadd(failures_key, {str(now): now})
            pipe.zcard(failures_key)
            pipe.expire(failures_key, window_seconds + 5)
            results = await pipe.execute()
            count = results[2]
            if count >= max_failures:
                # Trigger lockout
                await r.setex(lockout_key, lockout_seconds, "1")
                await r.delete(failures_key)
                return True, lockout_seconds
            return False, 0
        except Exception as exc:
            logger.debug(f"Redis record failure error: {exc}")

    async with _mem_lock:
        cutoff = now - window_seconds
        failures = [ts for ts in _memory_failures[key] if ts > cutoff]
        failures.append(now)
        _memory_failures[key] = failures

        if len(failures) >= max_failures:
            _memory_lockouts[key] = now + lockout_seconds
            _memory_failures[key] = []
            return True, lockout_seconds
        return False, 0


async def clear_failures(key: str) -> None:
    """Clear failed attempts and lockouts after successful authentication."""
    r = await get_redis()
    if r is not None:
        try:
            await r.delete(f"fail:{key}", f"lockout:{key}")
        except Exception:
            pass

    async with _mem_lock:
        _memory_failures.pop(key, None)
        _memory_lockouts.pop(key, None)
