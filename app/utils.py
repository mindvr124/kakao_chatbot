from datetime import datetime, timedelta
from .config import settings

def session_expired(last_time: datetime, now: datetime | None = None) -> bool:
    now = now or datetime.utcnow()
    return (now - last_time) > timedelta(minutes=settings.session_timeout_minutes)

def extract_user_id(kakao_body: dict) -> str | None:
    # 일반적으로 userRequest.user.id 형태
    return (
        kakao_body.get("userRequest", {})
        .get("user", {})
        .get("id")
    )

def extract_callback_url(kakao_body: dict) -> str | None:
    # 콜백 활성 블록이면 여기에 들어옴(플랫폼 설정에 따라 위치가 다를 수 있어 방어적으로)
    return (
        kakao_body.get("callbackUrl")
        or kakao_body.get("userRequest", {}).get("callbackUrl")
        or kakao_body.get("action", {}).get("callbackUrl")
    )
