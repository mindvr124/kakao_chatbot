from datetime import datetime, timedelta
from app.config import settings
import re

def session_expired(last_time: datetime, now: datetime | None = None) -> bool:
    now = now or datetime.utcnow()
    return (now - last_time) > timedelta(minutes=settings.session_timeout_minutes)

def extract_user_id(kakao_body: dict) -> str:
    """카카오 요청 바디에서 user_id를 방어적으로 추출합니다.
    우선순위: userRequest.user.id → userRequest.user.properties.plusfriendUserKey → appUserId → kakaoId → 그 외 폴백
    """
    user_request = (kakao_body or {}).get("userRequest") or {}

    # 1) userRequest.user.id
    user = user_request.get("user") or {}
    if isinstance(user, dict):
        user_id = (user.get("id")
                   or (user.get("properties") or {}).get("plusfriendUserKey"))
        if user_id:
            return str(user_id)

    # 2) 루트 혹은 기타 위치에서 대체 키
    for key in ("appUserId", "kakaoId"):
        candidate = (kakao_body or {}).get(key) or user_request.get(key)
        if candidate:
            return str(candidate)

    # 3) 마지막 수단: 요청의 X-Request-ID로 익명 ID 구성 (호출부에서 전달)
    # 여기서는 비워두고 호출부에서 폴백하도록 빈 문자열 반환
    return ""

def extract_callback_url(kakao_body: dict) -> str | None:
    """콜백 URL을 방어적으로 추출합니다. 플랫폼/버전에 따라 위치가 다를 수 있어 깊은 탐색을 수행합니다."""
    body = kakao_body or {}

    # 1) 자주 쓰이는 상위 레벨 경로 우선
    common = (
        body.get("callbackUrl")
        or (body.get("userRequest") or {}).get("callbackUrl")
        or (body.get("action") or {}).get("callbackUrl")
        or (body.get("context") or {}).get("callbackUrl")
        or (body.get("bot") or {}).get("callbackUrl")
    )
    if isinstance(common, str) and common.strip():
        return common

    # 2) 일부 구현에서 action.clientExtra/params 등에 중첩될 수 있음 → 깊은 탐색
    def _deep_find_callback_url(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(k, str) and k.lower() in ("callbackurl",):
                    if isinstance(v, str) and v.strip():
                        return v
                found = _deep_find_callback_url(v)
                if found:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = _deep_find_callback_url(item)
                if found:
                    return found
        return None

    deep = _deep_find_callback_url(body)

    # 디버깅 로그
    try:
        print(f"DEBUG: Searching for callbackUrl in keys: {list(body.keys())}")
        if deep:
            print(f"DEBUG: Found callbackUrl (deep): {deep}")
        else:
            print(f"DEBUG: No callbackUrl found. Full body: {body}")
    except Exception:
        pass

    return deep

def remove_markdown(text: str) -> str:
    """LLM 응답에서 마크다운 문법을 간단히 제거합니다."""
    if not isinstance(text, str):
        return text
    # 코드 블록 제거
    text = re.sub(r"```[\s\S]*?```", "", text)
    # 인라인 코드
    text = re.sub(r"`([^`]*)`", r"\1", text)
    # 굵게/기울임
    text = re.sub(r"(\*|_){1,3}([^*_]+)\1{1,3}", r"\2", text)
    # 제목(#)
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
    # 링크 문법 [text](url)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return text.strip()
