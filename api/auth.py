import logging
from datetime import datetime, timedelta

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from database.models import ApiKey, User, get_db
from utils.rate_limiter import check_rate_limit, is_locked_out, record_failure_and_check_lockout
from utils.security import constant_time_compare

logger = logging.getLogger(__name__)

bearer_scheme = HTTPBearer(auto_error=False)


async def get_api_key_record(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> ApiKey:
    client_ip = request.client.host if request.client else "unknown"
    fail_key = f"api_auth_fail:{client_ip}"

    # Check brute-force lockout on authentication failures
    locked, retry_after = await is_locked_out(fail_key)
    if locked:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed authentication attempts. Please wait {retry_after} seconds.",
        )

    if not credentials or (credentials.scheme or "").lower() != "bearer":
        await record_failure_and_check_lockout(fail_key, max_failures=20, window_seconds=300, lockout_seconds=600)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing or invalid Bearer API key")

    submitted_key = credentials.credentials
    key = db.query(ApiKey).filter(ApiKey.api_key == submitted_key).first()

    # Uniform failure message to prevent key enumeration
    is_valid = (
        key is not None
        and bool(key.is_active)
        and constant_time_compare(key.api_key, submitted_key)
        and not (key.expires_at and key.expires_at < datetime.utcnow())
    )

    if not is_valid:
        await record_failure_and_check_lockout(fail_key, max_failures=20, window_seconds=300, lockout_seconds=600)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired API key")

    # Key-specific hourly rate limit
    allowed, retry_after = await check_rate_limit(f"apikey:{key.id}", limit=key.rate_limit, window_seconds=3600)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded. Limit is {key.rate_limit} requests per hour. Retry in {retry_after} seconds.",
        )

    key.last_used = datetime.utcnow()
    db.commit()
    return key


def get_current_api_user(key: ApiKey = Depends(get_api_key_record)) -> User:
    return key.user


def get_usage_stats(key: ApiKey) -> dict:
    return {
        "requests_limit": key.rate_limit,
        "window": "1 hour",
        "key_prefix": key.api_key[:6] + "..." if key.api_key else None,
        "last_used": key.last_used.isoformat() if key.last_used else None,
    }
