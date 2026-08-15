from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from database.models import ApiKey, User, get_db


bearer_scheme = HTTPBearer(auto_error=False)
_usage: dict[int, list[datetime]] = defaultdict(list)


def get_api_key_record(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> ApiKey:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Bearer API key")
    key = db.query(ApiKey).filter(ApiKey.api_key == credentials.credentials, ApiKey.is_active.is_(True)).first()
    if not key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    if key.expires_at and key.expires_at < datetime.utcnow():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API key expired")

    cutoff = datetime.utcnow() - timedelta(hours=1)
    _usage[key.id] = [ts for ts in _usage[key.id] if ts >= cutoff]
    if len(_usage[key.id]) >= key.rate_limit:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Rate limit exceeded")
    _usage[key.id].append(datetime.utcnow())

    key.last_used = datetime.utcnow()
    db.commit()
    return key


def get_current_api_user(key: ApiKey = Depends(get_api_key_record)) -> User:
    return key.user


def get_usage_stats(key: ApiKey) -> dict:
    cutoff = datetime.utcnow() - timedelta(hours=1)
    _usage[key.id] = [ts for ts in _usage[key.id] if ts >= cutoff]
    return {
        "requests_used": len(_usage[key.id]),
        "requests_limit": key.rate_limit,
        "period_reset": (cutoff + timedelta(hours=1)).isoformat(),
    }
