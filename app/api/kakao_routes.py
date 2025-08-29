import asyncio
import json
import random
import time
import traceback
from datetime import datetime, timedelta
from typing import Optional

import httpx
import urllib.request
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession


from app.core.ai_service import ai_service
from app.core.background_tasks import (
    update_last_activity,
)
from app.core.summary import (
    maybe_rollup_user_summary,
)
from app.database.db import get_session
from app.database.models import AppUser, Conversation, Message
from app.database.service import (
    mark_check_question_sent,
    save_log_message,
    update_check_response,
    update_risk_score,
    upsert_user,
    get_active_prompt_name,
    get_risk_state,
)
from app.utils.utils import (
    extract_callback_url,
    extract_user_id,
    remove_markdown,
)
from app.database.service import (
    mark_check_question_sent,
    save_log_message,
    update_check_response,
    update_risk_score,
    upsert_user,
    get_or_create_conversation,
    save_message,
    get_check_question_turn,
    decrement_check_question_turn,
)
from app.risk_mvp import (
    calculate_risk_score,
    should_send_check_question,
    get_check_questions,
    parse_check_response,
    RiskHistory,
    get_check_response_message,
    get_check_response_guidance,
    get_invalid_score_message,
)

# 라우터 정의
router = APIRouter()

# 상수 정의
CHECK_QUESTION_TURN_COUNT = 20
CALLBACK_TIMEOUT = 4.5
AI_GENERATION_TIMEOUT = 1.5
MAX_SIMPLETEXT = 900
MAX_OUTPUTS = 3
SENT_ENDERS = ("...", "…", ".", "!", "?", "。", "！", "？")

# 점수별 프롬프트 매핑
RISK_PROMPT_MAPPING = {
    "critical": "risk_critical",      # 9-10점: 위험도 높음
    "high": "risk_high",             # 7-8점: 위험도 중간
    "medium": "risk_medium",         # 4-6점: 위험도 보통
    "low": "risk_low",               # 1-3점: 위험도 낮음
    "safe": "risk_safe"              # 0점: 안전
}

def get_risk_based_prompt(risk_level: str) -> str:
    """위험도 레벨에 따른 프롬프트 이름을 반환합니다."""
    return RISK_PROMPT_MAPPING.get(risk_level, "default")

# 웰컴 메시지 목록
_WELCOME_MESSAGES = [
    "안녕하세요! 무엇을 도와드릴까요?",
    "반갑습니다! 어떤 이야기를 나누고 싶으신가요?",
    "안녕하세요! 오늘은 어떤 도움이 필요하신가요?",
    "반갑습니다! 편하게 이야기해주세요.",
    "안녕하세요! 무엇이든 물어보세요."
]

# 콜백 처리 유틸리티 함수들
def _hard_wrap_sentence(s: str, limit: int) -> list[str]:
    """한 문장이 limit보다 길면 최대한 공백/줄바꿈 기준으로 부드럽게 쪼갠다."""
    out = []
    u = s.strip()
    while len(u) > limit:
        # 선호도: 줄바꿈 > 공백 > 하드컷
        cut = u.rfind("\n", 0, limit)
        if cut < int(limit * 0.6):
            cut = u.rfind(" ", 0, limit)
        if cut == -1:
            cut = limit
        out.append(u[:cut].rstrip())
        u = u[cut:].lstrip()
    if u:
        out.append(u)
    return out

def split_for_kakao_sentence_safe(text: str, limit: int = MAX_SIMPLETEXT) -> list[str]:
    """
    - 문장 끝(., !, ?, …, 全角句点 등) 또는 빈 줄/줄바꿈 경계를 우선으로 분할
    - 문장이 limit보다 길면 그 문장만 부드럽게 하드랩
    """
    t = remove_markdown(text or "").replace("\r\n", "\n").strip()

    chunks = []
    i, n = 0, len(t)

    while i < n:
        end = min(i + limit, n)
        window = t[i:end]

        if end < n:
            # 1) 문장부호 경계 찾기
            cand = -1
            for p in SENT_ENDERS:
                pos = window.rfind(p)
                cand = max(cand, pos)

            # 2) 문장부호가 너무 앞이면(=너무 작게 잘릴 위험) 줄바꿈/공백 경계도 고려
            nl_pos    = window.rfind("\n")
            space_pos = window.rfind(" ")

            boundary = cand
            if boundary < int(limit * 0.4):
                boundary = max(boundary, nl_pos, space_pos)

            # 3) 경계가 없으면 하드컷
            if boundary == -1:
                boundary = len(window)
            else:
                boundary += 1  # 경계 문자 포함

        else:
            boundary = len(window)

        piece = window[:boundary].rstrip()

        # 만약 "한 문장" 자체가 limit보다 긴 경우엔 부드럽게 랩
        if len(piece) == boundary and (end < n) and boundary == len(window):
            # window 안에 경계가 전혀 없어서 통째로 잘린 케이스
            chunks.extend(_hard_wrap_sentence(piece, limit))
        else:
            if not piece:  # 빈 조각 방지
                piece = t[i:end].strip()
            if piece:
                chunks.append(piece)

        i += len(piece)
        # 경계 이후의 공백/개행 정리
        while i < n and t[i] in (" ", "\n"):
            i += 1

    return [c for c in chunks if c]

def pack_into_max_outputs(parts: list[str], limit: int, max_outputs: int) -> list[str]:
    """
    이미 limit 이하로 분할된 parts를, 개수를 줄이기 위해 앞에서부터
    가능한 만큼 합치되 각 조각이 limit를 넘지 않게 그리디로 포장.
    """
    if len(parts) <= max_outputs:
        return parts

    packed = []
    cur = ""
    for p in parts:
        if not cur:
            cur = p
            continue
        if len(cur) + 1 + len(p) <= limit:
            cur = f"{cur}\n{p}"
        else:
            packed.append(cur)
            cur = p
    if cur:
        packed.append(cur)

    # 그래도 많으면 맨 뒤를 잘라내는 대신, 마지막 아이템에 안내 메시지 추가
    if len(packed) > max_outputs:
        keep = packed[:max_outputs-1]
        keep.append("※ 내용이 길어 일부만 보냈어. '자세히'라고 보내면 이어서 보여줄게!")
        return keep
    return packed

async def _send_callback_response(callback_url: str, text: str, tokens_used: int, request_id: str | None):
    """콜백 URL로 응답을 전송합니다."""
    if not callback_url or not isinstance(callback_url, str) or not callback_url.startswith("http"):
        logger.bind(x_request_id=request_id).error(f"Invalid callback_url: {callback_url!r}")
        return

    parts = split_for_kakao_sentence_safe(text, MAX_SIMPLETEXT)
    parts = pack_into_max_outputs(parts, MAX_SIMPLETEXT, MAX_OUTPUTS)
    outputs = [{"simpleText": {"text": p}} for p in parts]
    
    payload = {
        "version": "2.0",
        "template": {"outputs": outputs},
        "useCallback": True
    }
    headers = {"Content-Type": "application/json; charset=utf-8"}

    # 1) httpx 우선 시도 (에러시 본문도 로깅)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(callback_url, json=payload, headers=headers)
            if resp.status_code >= 400:
                logger.error(f"Callback post failed via httpx: {resp.status_code} {resp.reason_phrase} | body={resp.text}")
            resp.raise_for_status()
            return
    except Exception as e:
        logger.exception(f"Callback post failed via httpx: {e}")

    # 2) urllib 백업 시도 (동일 payload)
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(callback_url, data=data, headers=headers, method="POST")
        # 블로킹이라 스레드로
        def _post_blocking():
            with urllib.request.urlopen(req, timeout=3) as r:
                status = r.status
                if status >= 400:
                    raise RuntimeError(f"urllib callback HTTP {status}")
                return status
        status = await asyncio.to_thread(_post_blocking)
        logger.info(f"Callback posted via urllib, status={status}")
    except Exception as e:
        logger.exception(f"Callback post failed via urllib: {e}")

async def _handle_callback_full(callback_url: str, user_id: str, user_text: str, request_id: str | None):
    """콜백을 통한 전체 응답 처리를 담당합니다."""
    final_text: str = "죄송합니다. 일시적인 오류가 발생했습니다. 다시 한 번 시도해주세요."
    tokens_used: int = 0
    try:
        async for s in get_session():
            try:
                async def _ensure_conv():
                    await upsert_user(s, user_id)
                    return await get_or_create_conversation(s, user_id)
                conv = await asyncio.wait_for(_ensure_conv(), timeout=0.7)
                conv_id_value = str(conv.conv_id)
                try:
                    if user_text:
                        await save_message(s, conv_id_value, "user", user_text, request_id, None, user_id)
                except Exception as save_user_err:
                    logger.bind(x_request_id=request_id).warning(f"Failed to save user message in callback: {save_user_err}")

                final_text, tokens_used = await ai_service.generate_response(
                    session=s,
                    conv_id=conv_id_value,
                    user_input=user_text,
                    prompt_name="온유",  # 콜백에서도 온유 프롬프트 사용
                    user_id=user_id,
                    request_id=request_id
                )
                await save_message(s, conv_id_value, "assistant", final_text, request_id, tokens_used, user_id)
                try:
                    await save_log_message(s, "callback_final_sent", f"Callback final sent: {len(final_text)} chars", str(user_id), conv_id_value, {"tokens": tokens_used, "request_id": request_id})
                except Exception as log_err:
                    logger.warning(f"Callback log save failed: {log_err}")
                try:
                    await maybe_rollup_user_summary(s, user_id, conv_id_value)
                except Exception as summary_err:
                    logger.warning(f"User summary rollup failed: {summary_err}")
                break
            except Exception as inner_e:
                logger.bind(x_request_id=request_id).exception(f"Callback DB/AI error: {inner_e}")
                break

        try:
            await _send_callback_response(callback_url, final_text, tokens_used, request_id)
        except Exception as post_err:
            logger.bind(x_request_id=request_id).exception(f"Callback post failed: {post_err}")
    except Exception as e:
        logger.bind(x_request_id=request_id).exception(f"Callback flow failed: {e}")

async def _handle_callback_flow(session: AsyncSession, user_id: str, user_text: str, callback_url: str, conv_id: str, x_request_id: str):
    """콜백 플로우를 처리합니다."""
    time_left = max(0.2, CALLBACK_TIMEOUT - 0.5)
    
    # 빠른 응답 시도
    try:
        # 로그 저장
        safe_conv_id = conv_id if conv_id and not str(conv_id).startswith("temp_") else None
        try:
            await save_log_message(session, "request_received", "Request received from callback", str(user_id), safe_conv_id, {"source": "callback", "callback": True, "x_request_id": x_request_id})
        except Exception as log_err:
            logger.warning(f"Callback log save failed: {log_err}")

        # 빠른 대화 생성
        try:
            quick_conv_id = await asyncio.wait_for(
                get_or_create_conversation(session, user_id), 
                timeout=min(1.0, time_left - 0.1)
            )
            quick_conv_id = quick_conv_id.conv_id
        except Exception:
            quick_conv_id = f"temp_{user_id}"

        # 빠른 AI 응답 생성
        # request_id가 정의되어 있지 않으므로 x_request_id를 대신 사용합니다.
        quick_text, quick_tokens = await asyncio.wait_for(
            ai_service.generate_response(
                session=session,
                conv_id=quick_conv_id,
                user_input=user_text,
                prompt_name="온유",  # 콜백에서도 온유 프롬프트 사용
                user_id=user_id,
                request_id=x_request_id
            ),
            timeout=time_left,
        )

        # 백그라운드에서 메시지 저장
        async def _persist_quick(user_id: str, user_text: str, reply_text: str, request_id: str | None):
            async for s in get_session():
                try:
                    await upsert_user(s, user_id)
                    conv = await get_or_create_conversation(s, user_id)
                    try:
                        await save_message(s, conv.conv_id, "user", user_text, request_id, None, user_id)
                        await save_message(s, conv.conv_id, "assistant", remove_markdown(reply_text), request_id, quick_tokens, user_id)
                    except Exception:
                        try:
                            await s.rollback()
                        except Exception:
                            pass
                        raise
                    try:
                        await maybe_rollup_user_summary(s, user_id, conv.conv_id)
                    except Exception:
                        pass
                    break
                except Exception as persist_err:
                    try:
                        await s.rollback()
                    except Exception:
                        pass
                    logger.bind(x_request_id=request_id).exception(f"Persist quick path failed: {persist_err}")
                    break

        asyncio.create_task(_persist_quick(user_id, user_text, quick_text, x_request_id))

        try:
            update_last_activity(quick_conv_id)
        except Exception:
            pass
            
        return JSONResponse(content={
            "version": "2.0",
            "template": {"outputs":[{"simpleText":{"text": remove_markdown(quick_text)}}]}
        }, media_type="application/json; charset=utf-8")
        
    except Exception:
        pass

    # 시간 내 미완료시 즉시 콜백 대기 응답 반환
    immediate = {
        "version": "2.0",
        "template": {"outputs":[{"simpleText":{"text":"답변을 생성 중입니다..."}}]},
        "useCallback": True
    }
    
    try:
        safe_conv_id = conv_id if conv_id and not str(conv_id).startswith("temp_") else None
        await save_log_message(session, "callback_waiting_sent", "Callback waiting sent", str(user_id), safe_conv_id, {"source": "callback", "x_request_id": x_request_id})
    except Exception as log_err:
        logger.warning(f"Callback waiting log save failed: {log_err}")

    # 백그라운드에서 전체 응답 처리
    asyncio.create_task(_handle_callback_full(callback_url, user_id, user_text, x_request_id))

    try:
        update_last_activity(f"temp_{user_id}")
    except Exception:
        pass
        
    return JSONResponse(content=immediate, media_type="application/json; charset=utf-8")
    

"""카카오 스킬 관련 라우터"""
import asyncio
import re

import re
from typing import Optional

# ======================================================================
# 이름 추출을 위한 정규식 패턴들 (기초)
# ======================================================================
_NAME_PREFIX_PATTERN = re.compile(
    r'^(내\s*이름은|제\s*이름은|난|나는|저는|전|제|나|저|저를|날|나를)\s*',
    re.IGNORECASE,
)
_NAME_SUFFIX_PATTERN = re.compile(
    r'\s*(라고\s*(부르세요|해주세요|불러주세요)|입니다|이에요|예요|에요|야|이야|합니다|불러|불러줘|잖아|거든|거든요|라니까)\.?$',
    re.IGNORECASE,
)
_NAME_REQUEST_PATTERN = re.compile(r'([가-힣]{2,4})\s*라고\s*불러', re.IGNORECASE)
_KOREAN_NAME_PATTERN = re.compile(r'[가-힣]{2,4}')

# ======================================================================
# 인삿말 & 웰컴 메시지
# ======================================================================
_GREETINGS = {
    "안녕", "ㅎㅇ", "반가워", "하이", "헬로", "hi", "hello",
    "안녕하세요", "안녕하십니까", "반갑습니다", "처음뵙겠습니다",
    "ㅎㅎ", "ㅋㅋ", "ㅎㅎㅎ", "ㅋㅋㅋ", "야", "나온아", "온유야", "넌 누구니",
    "너 누구야", "너는 누구야", "너는 누구니"
}

def get_welcome_messages(prompt_name: str = "온유") -> list[str]:
    return [
        f"안녕~ 난 {prompt_name}야🐥 너는 이름이 뭐야?",
        f"안녕~ 난 {prompt_name}야🐥 내가 뭐라고 부르면 좋을까?",
        f"안녕~ 난 {prompt_name}야🐥 네 이름이 궁금해. 알려줘~!"
    ]

# ======================================================================
# 이름 검증: 금칙어/보통명사/봇이름/허용 문자
# ======================================================================
PROFANITY = {
    "바보","멍청이","등신","미친놈","또라이","십새","병신","개새","쌍놈","개같","변태","찌질이",
    "fuck","shit","bitch","asshole","idiot","moron"
}

COMMON_NON_NAME = {
    "학생","여자","남자","사람","개발자","디자이너","마케터","기획자","교사","선생","선생님","동물","짐승",
    "중학생","고등학생","대학생","취준생","직장인","아이","어른","친구","고객","사용자",
    "엄마","아빠","부모","형","누나","오빠","언니","동생","친구들","너","니","네","너의","네가","니가",
    "이름","직업",
    "학교","회사","집","병원","학원","카페","도서관","교회","역","지하철","버스",
    # 추가 보통명사들
    "직원","사장","부장","과장","대리","주임","사무직","생산직","서비스직","자영업자",
    "대학교","고등학교","중학교","초등학교","유치원","어린이집","과외",
    "아파트","빌라","원룸","오피스텔","상가","건물","시설","장소",
    "음식","음료","커피","차","술","담배","약","의약품","화장품","옷","신발"
}

BOT_NAMES = {"온유","on유","onu","on-u","on-you","onyou"}

# 허용 문자(한글/영문/숫자/중점/하이픈/언더스코어), 길이 1~20
NAME_ALLOWED = re.compile(r"^[가-힣a-zA-Z0-9·\-\_]{1,20}$")

def contains_profanity(text: str) -> bool:
    t = (text or "").lower()
    return any(bad in t for bad in PROFANITY)

def is_common_non_name(s: str) -> bool:
    return (s or "") in COMMON_NON_NAME

def is_bot_name(s: str) -> bool:
    return (s or "") in BOT_NAMES

def clean_name(s: str) -> str:
    s = (s or "").strip()
    # 장식/괄호/따옴표 제거
    s = re.sub(r'[\"\'"()\[\]{}<>~]+', "", s)
    return s.strip()

def is_valid_name(s: str) -> bool:
    if not s:
        return False
    if contains_profanity(s):
        return False
    if is_common_non_name(s):
        return False
    if is_bot_name(s):
        return False
    return bool(NAME_ALLOWED.fullmatch(s))



# ======================================================================
# 이름 후보 선택기 & 정정 트리거
# ======================================================================
RE_DISPUTE_TRIGGER = re.compile(r"내가\s*기억하는\s*네\s*이름은", re.IGNORECASE)

CORRECTION_PATTERNS = [
    re.compile(r'(?:그거\s*아니고|아니,\s*|아니야|틀렸고|정정)\s*([가-힣]{2,4})'),
    re.compile(r'(?:내\s*이름(?:은)?|이름은)\s*([가-힣]{2,4})'),
    re.compile(r'([가-힣]{2,4})\s*라고\s*(?:해|불러)(?:줘|주세요)?')
]

EXPLICIT_PATTERNS = [
    re.compile(r'(?P<name>[가-힣]{2,4})\s*라고\s*(불러(?:줘|주세요)?|해(?:요|줘)?|부르세요)'),
    re.compile(r'(?:^|[\s,])(내|제)\s*이름(?:은)?\s*(?P<name>[가-힣]{2,4})\s*(?:이야|야|입니다|예요|에요)?'),
    re.compile(r'(?:^|[\s,])(난|나는|전|저는|나)\s+(?P<name>[가-힣]{2,4})\s*(?:이야|야|라고\s*해(?:요)?)?'),
]

def strip_suffixes(s: str) -> str:
    """
    이름에서 어미/조사를 제거합니다.
    예: "민수야" → "민수", "지현이야" → "지현"
    ※ 주의: '민정이'처럼 '이' 자체가 이름일 수 있으므로 **단독 '이'$는 제거하지 않습니다.**
    """
    if not s:
        return ""
    suffix_patterns = [
        r'(야|이야|입니다|이에요|예요|에요|임|잖아|거든요?|라니까|라고요|라네|래요|맞아)$'
    ]
    result = s
    for pattern in suffix_patterns:
        result = re.sub(pattern, '', result)
    return result.strip()

def extract_simple_name(text: str) -> Optional[str]:
    """
    간단한 이름 추출 함수(대화 중 자동 추출 제거 버전):
      - 정정/명시/단독 이름만 허용
    """
    t = (text or "").strip()

    # A) 정정(교정)
    for pat in CORRECTION_PATTERNS:
        m = pat.search(t)
        if m:
            cand = strip_suffixes(clean_name(m.group(1)))
            if is_valid_name(cand) and not contains_profanity(cand) and not is_common_non_name(cand):
                return cand

    # B) 명시
    for pat in EXPLICIT_PATTERNS:
        m = pat.search(t)
        if m:
            grp = m.groupdict().get("name") or m.group(1)
            cand = strip_suffixes(clean_name(grp))
            if is_valid_name(cand) and not contains_profanity(cand) and not is_common_non_name(cand):
                return cand

    # C) 단독 이름 (대기/슬래시 흐름에서만 호출됨)
    m = re.fullmatch(r'\s*([가-힣]{2,4})\s*', t)
    if m:
        cand = strip_suffixes(clean_name(m.group(1)))
        if is_valid_name(cand) and not contains_profanity(cand) and not is_common_non_name(cand):
            return cand

    return None

def check_name_with_josa(name: str) -> tuple[bool, str]:
    """
    이름의 마지막 글자 '이' 모호성 질문 필요 여부 판단.
    '민정이' 케이스면 질문: "'민정'(이)야? 아니면 '민정이'야?"
    """
    if not name or len(name) < 2:
        return False, ""
    last_char = name[-1]
    if last_char == '이':
        base_name = name[:-1]  # '이' 제거
        question = f"'{base_name}'(이)야? 아니면 '{name}'야?"
        return True, question
    return False, ""

# ----------------------------------------------------------------------
# 간단 in-memory 캐시 (운영은 Redis/DB 권장)
# ----------------------------------------------------------------------
class PendingNameCache:
    _store: dict[str, float] = {}
    TTL_SECONDS = 300  # 5분

    @classmethod
    def set_waiting(cls, user_id: str):
        cls._store[user_id] = time.time() + cls.TTL_SECONDS
        logger.info(f"[대기] 이름 대기 상태 설정: {user_id}")

    @classmethod
    def is_waiting(cls, user_id: str) -> bool:
        exp = cls._store.get(user_id)
        if not exp:
            return False
        if time.time() > exp:
            try:
                del cls._store[user_id]
            except Exception:
                pass
            return False
        return True

    @classmethod
    def clear(cls, user_id: str):
        was_waiting = user_id in cls._store
        cls._store.pop(user_id, None)
        if was_waiting:
            logger.info(f"[해제] 이름 대기 상태 해제: {user_id}")

class JosaDisambCache:
    _store: dict[str, float] = {}
    TTL_SECONDS = 180  # 3분

    @classmethod
    def set_pending(cls, user_id: str):
        cls._store[user_id] = time.time() + cls.TTL_SECONDS
        logger.info(f"[대기] '이' 모호성 확인 대기: {user_id}")

    @classmethod
    def is_pending(cls, user_id: str) -> bool:
        exp = cls._store.get(user_id)
        if not exp:
            return False
        if time.time() > exp:
            cls._store.pop(user_id, None)
            return False
        return True

    @classmethod
    def clear(cls, user_id: str):
        if user_id in cls._store:
            cls._store.pop(user_id, None)
            logger.info(f"[해제] '이' 모호성 대기 해제: {user_id}")

# ----------------------------------------------------------------------
# DB 저장 & 카카오 응답
# ----------------------------------------------------------------------
async def save_user_name(session: AsyncSession, user_id: str, name: str):
    logger.info(f"[저장] 이름 저장: {user_id} -> {name}")
    user = await upsert_user(session, user_id, name)
    operation = 'INSERT' if not user.user_name else 'UPDATE'
    logger.info(f"[완료] 이름 저장 완료: {user_id} -> {name} ({operation})")
    try:
        await save_log_message(
            session=session,
            level="INFO",
            message=f"사용자 이름이 '{name}'으로 변경되었습니다.",
            user_id=user_id,
            source="name_update"
        )
    except Exception as e:
        logger.error(f"[오류] 이름 변경 로그 저장 중 오류: {e}")

def kakao_text(text: str) -> JSONResponse:
    return JSONResponse(
        content={
            "version": "2.0",
            "template": {"outputs": [{"simpleText": {"text": text}}]}
        },
        media_type="application/json; charset=utf-8"
    )

# (레거시 위험도 히스토리 — skill_endpoint에서 참조하면 전역 선언 필요)
_RISK_HISTORIES: dict[str, "RiskHistory"] = {}

# ----------------------------------------------------------------------
# 메인 플로우
# ----------------------------------------------------------------------
async def handle_name_flow(
    session: AsyncSession,
    user_id: str,
    user_text: str,
    x_request_id: str,
    conv_id: Optional[str] = None
) -> Optional[JSONResponse]:

    # 0) '이' 모호성 질문에 대한 **직후 응답** 최우선 처리
    if JosaDisambCache.is_pending(user_id):
        cand = strip_suffixes(clean_name(user_text))
        if not cand or contains_profanity(cand) or is_common_non_name(cand) or is_bot_name(cand) or not is_valid_name(cand):
            PendingNameCache.set_waiting(user_id)  # 계속 대기 유지
            return kakao_text("그건 이름처럼 들리지 않아.\n예) 민수, 지현")
        try:
            await save_user_name(session, user_id, cand)
            PendingNameCache.clear(user_id)
            JosaDisambCache.clear(user_id)
            return kakao_text(f"반가워 {cand}! 앞으로 {cand}(이)라고 부를게🐥")
        except Exception:
            PendingNameCache.set_waiting(user_id)
            JosaDisambCache.clear(user_id)
            return kakao_text("앗, 저장 중 문제가 있었어. 다시 알려줄래?")

    # 1) 기본 상태 읽기
    try:
        prompt_name = await get_active_prompt_name(session)
        logger.info(f"[PROMPT] 활성 프롬프트 이름: {prompt_name}")

        user = await session.get(AppUser, user_id)
        user_name = user.user_name if user else None
        is_waiting = PendingNameCache.is_waiting(user_id)
        logger.info(f"[상태] user={user_id} | 이름={user_name} | 대기중={is_waiting}")
        logger.info(f"[입력] '{user_text}'")

        # 2) 아직 이름 없는 사용자
        if user is None or user.user_name is None:
            if is_waiting:
                raw = clean_name(user_text)
                if contains_profanity(raw) or is_common_non_name(raw) or is_bot_name(raw):
                    return kakao_text("그 이름은 사용할 수 없어.\n한글/영문 1~20자로 예쁜 이름을 알려줘!\n예) 민수, Yeonwoo")

                cand = extract_simple_name(user_text)
                if not cand:
                    return kakao_text("그건 이름처럼 들리지 않아.\n예) 민수, 지현")

                # 마지막 가드
                if contains_profanity(cand) or is_common_non_name(cand) or is_bot_name(cand):
                    return kakao_text("그 이름은 사용할 수 없어.\n한글/영문 1~20자로 예쁜 이름을 알려줘!\n예) 민수, Yeonwoo")
                if not is_valid_name(cand):
                    return kakao_text("이름 형식은 한글/영문 1~20자야.\n예) 민수, Yeonwoo")

                # '이' 모호성 확인
                needs_josa_question, josa_question = check_name_with_josa(cand)
                if needs_josa_question:
                    PendingNameCache.set_waiting(user_id)
                    JosaDisambCache.set_pending(user_id)
                    return kakao_text(josa_question)

                try:
                    await save_user_name(session, user_id, cand)
                    PendingNameCache.clear(user_id)
                    JosaDisambCache.clear(user_id)
                    try:
                        await save_log_message(
                            session, "name_saved", f"Name saved: {cand}",
                            str(user_id), conv_id,
                            {"source": "name_flow", "name": cand, "mode": "first_chat", "x_request_id": x_request_id}
                        )
                    except Exception:
                        pass
                    return kakao_text(f"반가워 {cand}! 앞으로 {cand}(이)라고 부를게🐥")
                except Exception as e:
                    logger.bind(x_request_id=x_request_id).exception(f"[오류] 이름 저장 실패: {e}")
                    PendingNameCache.clear(user_id)
                    JosaDisambCache.clear(user_id)
                    return kakao_text("앗, 저장 중 문제가 있었어. 다시 알려줄래?")

            # 아직 대기 진입 전: 인사/기타
            elif any(g in user_text.lower() for g in _GREETINGS):
                PendingNameCache.set_waiting(user_id)
                try:
                    await save_log_message(session, "name_wait_start", "Name wait started", str(user_id), None, {"x_request_id": x_request_id})
                except Exception:
                    pass
                return kakao_text(random.choice(get_welcome_messages(prompt_name)))
            else:
                PendingNameCache.set_waiting(user_id)
                try:
                    await save_log_message(session, "name_wait_start", "Name wait started", str(user_id), None, {"x_request_id": x_request_id})
                except Exception:
                    pass
                return kakao_text(f"안녕! 처음 보네~ 나는 {prompt_name}야🐥\n불리고 싶은 이름을 알려주면, 앞으로 그렇게 불러줄게!")

        # 3) 이름 있는 사용자
        try:
            conv = await get_or_create_conversation(session, user_id)
        except Exception as e:
            logger.warning(f"[경고] 대화 세션 생성 실패: {e}")
            conv = None

        # 3-1) '/이름' 명령: 대기 진입
        if user_text == "/이름":
            PendingNameCache.set_waiting(user_id)
            try:
                await save_log_message(session, "name_wait_start", "Name wait started", str(user_id), None, {"x_request_id": x_request_id})
            except Exception:
                pass
            return kakao_text(f"불리고 싶은 이름을 입력해줘! 그럼 {prompt_name}가 꼭 기억할게~\n\n💡 팁: 자연스럽게 '내 이름은 민수야'라고 말해도 알아들어요!")

        # 3-2) 이미 대기 상태: 일반 입력 처리
        if PendingNameCache.is_waiting(user_id):
            if user_text in ("취소", "그만", "아냐", "아니야", "됐어", "아니"):
                PendingNameCache.clear(user_id)
                return kakao_text("좋아, 다음에 다시 알려줘!")

            cand = extract_simple_name(user_text)
            if not cand:
                return kakao_text("그건 이름처럼 들리지 않아.\n예) 민수, 지현")

            if contains_profanity(cand) or is_common_non_name(cand) or is_bot_name(cand):
                return kakao_text("그 이름은 사용할 수 없어.\n예) 민수, Yeonwoo")
            if not is_valid_name(cand):
                return kakao_text("이름 형식은 한글/영문 1~20자야.\n예) 민수, Yeonwoo")

            # ★ 모호성 질문 (여기서만)
            needs_josa_question, josa_question = check_name_with_josa(cand)
            if needs_josa_question:
                PendingNameCache.set_waiting(user_id)
                JosaDisambCache.set_pending(user_id)
                return kakao_text(josa_question)

            try:
                await save_user_name(session, user_id, cand)
                PendingNameCache.clear(user_id)
                return kakao_text(f"이름 예쁘다! 앞으로는 '{cand}'(이)라고 불러줄게~")
            except Exception:
                PendingNameCache.clear(user_id)
                return kakao_text("앗, 이름을 저장하는 중에 문제가 생겼나봐. 잠시 후 다시 시도해줘!")

        # 3-3) '/이름 xxx' 즉시 저장
        if user_text.startswith("/이름 "):
            raw = user_text[len("/이름 "):]
            cand = clean_name(raw)

            if contains_profanity(cand) or is_common_non_name(cand) or is_bot_name(cand):
                return kakao_text("그 이름은 사용할 수 없어.\n한글/영문 1~20자로 예쁜 이름을 알려줘!\n예) 민수, Yeonwoo")
            if not is_valid_name(cand):
                return kakao_text("이름 형식은 한글/영문 1~20자야.\n예) 민수, Yeonwoo")

            needs_josa_question, josa_question = check_name_with_josa(cand)
            if needs_josa_question:
                PendingNameCache.set_waiting(user_id)
                JosaDisambCache.set_pending(user_id)
                return kakao_text(josa_question)

            try:
                await save_user_name(session, user_id, cand)
                try:
                    await save_log_message(session, "name_saved", f"Name saved via slash: {cand}", str(user_id), None, {"name": cand, "mode": "slash_inline", "x_request_id": x_request_id})
                except Exception:
                    pass
                return kakao_text(f"예쁜 이름이다! 앞으로는 {cand}(이)라고 불러줄게~")
            except Exception as name_err:
                logger.bind(x_request_id=x_request_id).exception(f"save_user_name failed: {name_err}")
                return kakao_text("앗, 이름을 저장하는 중에 문제가 생겼나봐. 잠시 후 다시 시도해줘!")

        # 이름 관련 처리 없음 → 상위 로직에 위임
        return None

    except Exception as e:
        logger.bind(x_request_id=x_request_id).exception(f"Failed to handle name flow: {e}")
        return None

def _safe_reply_kakao(risk_level: str) -> dict:
    # 위험도 레벨에 따른 안전 응답 생성
    if risk_level == "critical":
        msg = (
            "현재 상태는 위험해 보여. 즉시 도움을 받아야 해.\n"
            "• 자살예방 상담전화 1393 (24시간)\n"
            "• 정신건강 위기상담 1577-0199 (24시간)\n"
            "• 청소년 상담전화 1388 (24시간)\n"
            "• 긴급상황: 112/119\n"
            "넌 혼자가 아니야. 지금 바로 연락해 줘."
        )
    else:  # high level
        msg = (
            "지금 마음이 많이 힘들어 보여. 혼자가 아니야.\n"
            "• 자살예방 상담전화 1393 (24시간)\n"
            "• 정신건강 위기상담 1577-0199\n"
            "• 청소년 상담전화 1388 (24시간)\n"
            "긴급한 상황이면 112/119에 바로 연락해줘."
        )
    return {"version":"2.0","template":{"outputs":[{"simpleText":{"text": msg}}]}}
    
# ====== [스킬 엔드포인트] =====================================================

@router.post("/skill")
@router.post("/skill/")
async def skill_endpoint(request: Request, session: AsyncSession = Depends(get_session)):
    """카카오 스킬 메인 엔드포인트"""
    # X-Request-ID 추출 (로깅용)
    x_request_id = request.headers.get("X-Request-ID") or request.headers.get("X-Request-Id")
    
    logger.bind(x_request_id=x_request_id).info("================================================================================")
    logger.bind(x_request_id=x_request_id).info("========================== SKILL ENDPOINT STARTED ==============================")
    logger.bind(x_request_id=x_request_id).info("================================================================================")
    logger.bind(x_request_id=x_request_id).info("Skill endpoint started")
    logger.bind(x_request_id=x_request_id).info("================================================================================")
    
    try:

        try:
            body_dict = await request.json()
            if not isinstance(body_dict, dict):
                body_dict = {}
        except Exception as parse_err:
            logger.warning(f"JSON parse failed: {parse_err}")
            body_dict = {}
        
        user_id = extract_user_id(body_dict)
        logger.bind(x_request_id=x_request_id).info(f"Extracted user_id: {user_id}")

        # 폴백: user_id가 비어있으면 익명 + X-Request-ID 사용
        if not user_id:
            anon_suffix = x_request_id or "unknown"
            user_id = f"anonymous:{anon_suffix}"
            logger.bind(x_request_id=x_request_id).warning(f"user_id missing. fallback -> anonymous")

        callback_url = extract_callback_url(body_dict)
        logger.bind(x_request_id=x_request_id).info("Callback URL extracted")

        # 사용자 발화 추출
        user_text = (body_dict.get("userRequest") or {}).get("utterance", "")
        if not isinstance(user_text, str):
            user_text = str(user_text or "")
        if not user_text:
            user_text = "안녕하세요"
        user_text_stripped = user_text.strip()

        # ====== [대화 세션 생성] ==============================================
        # 대화 세션을 먼저 생성하여 conv_id 확보 (모든 로깅·저장에서 사용)
        try:
            conv = await get_or_create_conversation(session, user_id)
            conv_id = conv.conv_id
            logger.info(f"[CONV] 대화 세션 생성/조회 완료: conv_id={conv_id}")
        except Exception as e:
            logger.warning(f"[CONV] 대화 세션 생성 실패: {e}")
            conv_id = None
        
        # 로그 저장 (conv_id 유무와 관계없이)
        try:
            await save_log_message(session, "INFO", "SKILL REQUEST RECEIVED", str(user_id), conv_id, {"source": "skill_endpoint"})
        except Exception as log_err:
            logger.warning(f"로그 저장 실패: {log_err}")
        
        # ====== [자살위험도 분석] ==============================================
        logger.info(f"===== [위험도 분석 시작] ==============================================")
        logger.info(f"[RISK] 입력: '{user_text_stripped}'")
        
        # ----- [1단계: RiskHistory 객체 생성] -----
        if user_id not in _RISK_HISTORIES:
            # 데이터베이스에서 기존 위험도 점수 복원 시도
            try:
                existing_risk = await get_risk_state(session, user_id)
                if existing_risk and existing_risk.score > 0:
                    # 기존 점수가 있으면 초기 턴으로 복원
                    _RISK_HISTORIES[user_id] = RiskHistory(max_turns=20, user_id=user_id)
                    # 기존 점수를 첫 번째 턴으로 추가 (가상의 턴으로 복원)
                    virtual_turn = {
                        'text': f"[복원된_기존_점수:{existing_risk.score}점]",
                        'timestamp': datetime.now(),
                        'score': existing_risk.score,
                        'flags': {'neg': False, 'meta': False, 'third': False, 'idiom': False, 'past': False},
                        'evidence': [{'keyword': '복원된_점수', 'score': existing_risk.score, 'original_score': existing_risk.score, 'excerpt': '데이터베이스에서_복원'}]
                    }
                    _RISK_HISTORIES[user_id].turns.append(virtual_turn)
                    logger.info(f"[RISK_DEBUG] 기존 점수 복원 완료: user_id={user_id}, score={existing_risk.score}, turns_count={len(_RISK_HISTORIES[user_id].turns)}")
                else:
                    _RISK_HISTORIES[user_id] = RiskHistory(max_turns=20, user_id=user_id)
                    logger.info(f"[RISK_DEBUG] 새로운 RiskHistory 객체 생성: user_id={user_id}")
            except Exception as e:
                logger.warning(f"[RISK_DEBUG] 기존 점수 복원 실패: {e}")
                _RISK_HISTORIES[user_id] = RiskHistory(max_turns=20, user_id=user_id)
                logger.info(f"[RISK_DEBUG] 새로운 RiskHistory 객체 생성 (복원 실패): user_id={user_id}")
        
        user_risk_history = _RISK_HISTORIES[user_id]
        logger.info(f"----- [1단계 완료: RiskHistory 객체 생성] -----")
        
        # ----- [2단계: DB 동기화] -----
        logger.info(f"----- [2단계: DB 동기화 시작] -----")
        if getattr(user_risk_history, 'user_id', None) is None:
            user_risk_history.user_id = user_id
        
        try:
            db_turn = await get_check_question_turn(session, user_id)
            if user_risk_history.check_question_turn_count != db_turn:
                old_count = user_risk_history.check_question_turn_count
                user_risk_history.check_question_turn_count = db_turn
                logger.info(f"[RISK] DB 동기화: {old_count} → {db_turn}")
        except Exception as e:
            logger.warning(f"[RISK] DB 동기화 실패: {e}")
        
        logger.info(f"----- [2단계 완료: DB 동기화] -----")
        
        # ----- [3단계: 위험도 분석] -----
        logger.info(f"----- [3단계: 위험도 분석 시작] -----")
        if user_risk_history.check_question_turn_count and user_risk_history.check_question_turn_count > 0:
            logger.info(f"[RISK] 체크 질문 쿨다운 중: {user_risk_history.check_question_turn_count}턴 남음. 점수 누적 건너뜀")
            turn_analysis = {'score': 0, 'flags': {}, 'evidence': []}
            risk_score = 0
            flags = {}
            cumulative_score = 0
        else:
            turn_analysis = user_risk_history.add_turn(user_text_stripped)
            risk_score = turn_analysis['score']
            flags = turn_analysis['flags']
            cumulative_score = user_risk_history.get_cumulative_score()
        logger.info(f"----- [3단계 완료: 위험도 분석] -----")
        
        # ----- [4단계: 긴급 위험도 체크] -----
        logger.info(f"----- [4단계: 긴급 위험도 체크 시작] -----")
        logger.info(f"[URGENT_DEBUG] turns 개수: {len(user_risk_history.turns)}")
        logger.info(f"[URGENT_DEBUG] turns 내용: {[turn.get('score', 'N/A') for turn in user_risk_history.turns]}")
        if hasattr(user_risk_history, 'urgent_response_sent') and user_risk_history.urgent_response_sent:
            if hasattr(user_risk_history, 'urgent_response_turn_count'):
                user_risk_history.urgent_response_turn_count -= 1
                if user_risk_history.urgent_response_turn_count <= 0:
                    user_risk_history.urgent_response_sent = False
                    user_risk_history.urgent_response_turn_count = 0
                    logger.info(f"[URGENT] 긴급 응답 플래그 해제 완료")
                else:
                    logger.info(f"[URGENT] 긴급 응답 플래그 카운트다운: {user_risk_history.urgent_response_turn_count}턴 남음")
        
        # 긴급 응답 플래그가 설정되지 않은 경우에만 검출
        # 20턴 카운트 중에도 긴급 안내는 계속 체크 (점수 누적과는 별개)
        if not (hasattr(user_risk_history, 'urgent_response_sent') and user_risk_history.urgent_response_sent):
            if user_risk_history.turns:
                recent_turns = list(user_risk_history.turns)[-5:]
                logger.info(f"[URGENT_DEBUG] 최근 5턴: {[turn.get('score', 'N/A') for turn in recent_turns]}")
                if len(recent_turns) >= 2:
                    high_risk_count = sum(1 for turn in recent_turns if turn['score'] == 10)
                    logger.info(f"[URGENT] 5턴 내 10점 키워드 {high_risk_count}번 감지")
                    
                    if high_risk_count >= 2:
                        logger.info(f"[URGENT] 즉시 긴급 연락처 발송")
                        try:
                            user_id_str = str(user_id) if user_id else "unknown"
                            safe_conv_id = conv_id if conv_id and not str(conv_id).startswith("temp_") else None
                            await save_log_message(session, "urgent_risk_trigger",
                                                f"Urgent risk trigger: 10점 키워드 {high_risk_count}번 (20턴 카운트 중)", user_id_str, safe_conv_id,
                                                {"source": "urgent_risk", "high_risk_count": high_risk_count, "x_request_id": x_request_id})
                        except Exception as e:
                            logger.warning(f"[URGENT] urgent_risk_trigger 로그 저장 실패: {e}")
                        
                        # 긴급 응답 후 무한 반복 방지: 최근 5턴만 제거하고 긴급 응답 플래그 설정
                        try:
                            # 최근 5턴만 제거 (turns.clear() 대신)
                            for _ in range(min(5, len(user_risk_history.turns))):
                                user_risk_history.turns.pop()
                            
                            # 긴급 응답 플래그 설정 (다음 3턴 동안 재검출 방지)
                            user_risk_history.urgent_response_sent = True
                            user_risk_history.urgent_response_turn_count = 3
                            
                            logger.info(f"[URGENT] 최근 5턴 제거 및 긴급 응답 플래그 설정 완료")
                        except Exception as e:
                            logger.warning(f"[URGENT] 턴 제거 및 플래그 설정 실패: {e}")
                        
                        return JSONResponse(content=_safe_reply_kakao("critical"), media_type="application/json; charset=utf-8")
            else:
                logger.info(f"[URGENT_DEBUG] turns가 비어있음 - 긴급 체크 건너뜀")
        
        logger.info(f"----- [4단계 완료: 긴급 위험도 체크] -----")
        
        # ----- [5단계: 데이터베이스 저장] -----
        logger.info(f"----- [5단계: 데이터베이스 저장 시작] -----")
        try:
            # 매 턴마다 update_risk_score 호출 (턴 카운트 중일 때는 내부에서 0으로 초기화)
            await update_risk_score(session, user_id, cumulative_score)
            logger.info(f"[RISK] DB 저장 완료: {cumulative_score}점 (턴 카운트: {user_risk_history.check_question_turn_count})")
        except Exception as e:
            logger.error(f"[RISK] DB 저장 실패: {e}")
        
        logger.info(f"----- [5단계 완료: 데이터베이스 저장] -----")

        # ----- [5.5단계: 체크 질문 턴 카운트 감소] -----
        try:
            if user_risk_history.check_question_turn_count and user_risk_history.check_question_turn_count > 0:
                await decrement_check_question_turn(session, user_id)
                # DB에서 감소된 값을 다시 가져와서 동기화
                db_turn = await get_check_question_turn(session, user_id)
                user_risk_history.check_question_turn_count = db_turn
                logger.info(f"[CHECK] 쿨다운 카운트 감소: 남은 턴 {user_risk_history.check_question_turn_count}")
        except Exception as e:
            logger.warning(f"[CHECK] 쿨다운 카운트 감소 실패: {e}")
        
        # ----- [6단계: 체크 질문 처리] -----
        logger.info(f"----- [6단계: 체크 질문 처리 시작] -----")
        check_score = None
        
        # 체크 질문이 발송된 직후에만 응답 파싱 시도
        if (user_risk_history.check_question_turn_count == 20 and 
            user_risk_history.last_check_score is None):
            check_score = parse_check_response(user_text_stripped)
            logger.info(f"[CHECK] 응답 파싱: {check_score}점")
        
        if check_score is not None:
            logger.info(f"[CHECK] 체크 질문 응답 감지: {check_score}점")
            
            # RiskHistory에 체크 질문 응답 점수 저장
            user_risk_history.last_check_score = check_score
            
            try:
                await update_check_response(session, user_id, check_score)
                logger.info(f"[CHECK] 응답 저장: {check_score}점")
                
                # 체크 응답 점수에 따른 대응
                guidance = get_check_response_guidance(check_score)

                # 체크 질문 응답 후 위험도 점수만 초기화 (turn_count는 유지)
                try:
                    # turns만 초기화 (check_question_turn_count는 유지)
                    if user_id in _RISK_HISTORIES:
                        _RISK_HISTORIES[user_id].turns.clear()
                    
                    # 데이터베이스 점수도 0으로 업데이트
                    await update_risk_score(session, user_id, 0)
                    logger.info(f"[CHECK] 점수 초기화 완료")
                except Exception as e:
                    logger.warning(f"[CHECK] 점수 초기화 실패: {e}")
                
                # 체크 질문 응답 후 turn_count를 20으로 설정하여 20턴 동안 재질문 방지
                user_risk_history.check_question_turn_count = 20
                logger.info(f"[CHECK] 20턴 카운트다운 시작")
                
                # 9-10점: 즉시 안전 응답
                if check_score >= 9:
                    logger.info(f"[CHECK] 9-10점: 즉시 안전 응답")
                    try:
                        # conv_id가 유효한 경우에만 전달
                        safe_conv_id = conv_id if conv_id and not str(conv_id).startswith("temp_") else None
                        await save_log_message(session, "check_response_critical",
                                            f"Check response critical: {check_score}", str(user_id), safe_conv_id,
                                            {"source": "check_response", "check_score": check_score, "guidance": guidance, "x_request_id": x_request_id})
                    except Exception as log_err:
                        logger.warning(f"Critical check response log save failed: {log_err}")
                    
                    # 긴급 연락처 안내 후 점수 0점으로 초기화
                    try:
                        # turns만 초기화 (check_question_turn_count는 유지)
                        if user_id in _RISK_HISTORIES:
                            _RISK_HISTORIES[user_id].turns.clear()
                        
                        # 데이터베이스 점수도 0으로 업데이트
                        await update_risk_score(session, user_id, 0)
                    except Exception as e:
                        logger.warning(f"[CHECK] 긴급 응답 후 점수 초기화 실패: {e}")
                    
                    return JSONResponse(content=_safe_reply_kakao("critical"), media_type="application/json; charset=utf-8")
                
                # 7-8점: 안전 안내 메시지
                elif check_score >= 7:
                    logger.info(f"[CHECK] 7-8점: 안전 안내 메시지")
                    try:
                        # conv_id가 유효한 경우에만 전달 (None이거나 temp_로 시작하면 None)
                        safe_conv_id = conv_id if conv_id and not str(conv_id).startswith("temp_") else None
                        if safe_conv_id:
                            await save_log_message(session, "check_response_high_risk",
                                                f"Check response high risk: {check_score}", str(user_id), safe_conv_id,
                                                {"source": "check_response", "check_score": check_score, "guidance": guidance, "x_request_id": x_request_id})
                        else:
                            logger.info(f"[CHECK] conv_id가 유효하지 않아 로그 저장 건너뜀: conv_id={conv_id}")
                    except Exception as log_err:
                        logger.warning(f"High risk check response log save failed: {log_err}")
                    
                    response_message = get_check_response_message(check_score)
                    logger.info(f"[CHECK] 7-8점 응답 메시지: {response_message}")
                    
                    return kakao_text(response_message)
                
                # 0-6점: 일반 대응 메시지 후 정상 대화 진행
                else:
                    logger.info(f"[CHECK] 0-6점: 일반 대응 메시지")
                    try:
                        # conv_id가 유효한 경우에만 전달 (None이거나 temp_로 시작하면 None)
                        safe_conv_id = conv_id if conv_id and not str(conv_id).startswith("temp_") else None
                        if safe_conv_id:
                            await save_log_message(session, "check_response_normal",
                                                f"Check response normal: {check_score}", str(user_id), safe_conv_id,
                                                {"source": "check_response", "check_score": check_score, "guidance": guidance, "x_request_id": x_request_id})
                        else:
                            logger.info(f"[CHECK] conv_id가 유효하지 않아 로그 저장 건너뜀: conv_id={conv_id}")
                    except Exception as log_err:
                        logger.warning(f"Normal check response log save failed: {log_err}")
                    
                    response_message = get_check_response_message(check_score)
                    logger.info(f"[CHECK] 0-6점 응답 메시지: {response_message}")
                    
                    return kakao_text(response_message)
                    
            except Exception as e:
                logger.error(f"[CHECK] 체크 응답 저장 실패: {e}")
                logger.error(f"[CHECK] 상세 에러: {traceback.format_exc()}")
        else:
            # 체크 질문 응답이 아니거나 유효하지 않은 경우
            # 체크 질문이 발송된 직후에만 무효 응답에 대한 재요청 처리
            if (user_risk_history.check_question_turn_count == 20 and 
                user_risk_history.last_check_score is None):
                # 사용자가 체크 질문에 응답하지 않고 다른 말을 한 경우, 숫자만 재요청
                logger.info(f"[CHECK] 체크 질문 발송 직후 무효 응답 -> 숫자 0~10만 다시 요청")
                response_text = "0~10 중 숫자 하나로만 답해줘!"
                
                # 메시지 테이블에 저장
                try:
                    await save_message(session, conv_id, "assistant", response_text, x_request_id, user_id=user_id)
                    logger.info(f"[메시지저장] 체크 질문 재요청 메시지 저장 완료")
                except Exception as e:
                    logger.warning(f"[메시지저장] 체크 질문 재요청 메시지 저장 실패: {e}")
                
                return kakao_text(response_text)
            else:
                logger.info(f"[CHECK_DEBUG] 체크 질문 응답이 아님: 일반 대화로 진행")
                # 일반 대화로 진행 (AI 응답 생성)
                pass

        logger.info(f"----- [6단계 완료: 체크 질문 처리] -----")
        
        # ====== [체크 질문 발송 및 위험도 처리] ==============================================
        logger.info(f"----- [7단계: 체크 질문 발송 및 위험도 처리 시작] -----")
        # 데이터베이스의 현재 score를 가져와서 체크 질문 발송 여부 결정
        db_score = 0
        try:
            existing_risk = await get_risk_state(session, user_id)
            if existing_risk:
                db_score = existing_risk.score or 0
                logger.info(f"[CHECK_DB] 데이터베이스 현재 score: {db_score}")
            else:
                logger.info(f"[CHECK_DB] 데이터베이스에 risk_state 없음, score=0으로 설정")
        except Exception as e:
            logger.warning(f"[CHECK_DB] 데이터베이스 score 조회 실패: {e}, score=0으로 설정")
            db_score = 0
        
        # 8점 이상이면 체크 질문 발송 (체크 질문 응답이 완료된 경우에는 절대 발송하지 않음)
        # check_score가 None이 아니거나 last_check_score가 None이 아닌 경우는 이미 체크 질문 응답이 처리된 것이므로 발송하지 않음
        # cumulative_score를 사용하여 체크 질문 발송 여부 결정 (메모리 히스토리 기반)
        if (check_score is None and 
            user_risk_history.last_check_score is None and 
            should_send_check_question(cumulative_score, user_risk_history)):
            logger.info(f"[CHECK] 체크 질문 발송 조건 충족: cumulative_score={cumulative_score}, db_score={db_score}")
            try:
                # RiskHistory에 체크 질문 발송 기록
                user_risk_history.mark_check_question_sent()
                logger.info(f"[CHECK] RiskHistory에 체크 질문 발송 기록 완료")
                
                # 새로운 체크 질문 발송 시 이전 응답 점수 리셋
                user_risk_history.last_check_score = None
                logger.info(f"[CHECK] 새로운 체크 질문 발송으로 이전 응답 점수 리셋")
                
                # 데이터베이스에도 기록 (user_id를 문자열로 변환)
                user_id_str = str(user_id) if user_id else "unknown"
                await mark_check_question_sent(session, user_id_str)
                logger.info(f"[CHECK] 데이터베이스에 체크 질문 발송 기록 완료")
                
                # 체크 질문 발송 후 현재 위험도 점수 유지 (0으로 초기화하지 않음)
                logger.info(f"[CHECK] 체크 질문 발송 후 현재 위험도 점수 유지: {cumulative_score}")
                
                check_questions = get_check_questions()
                selected_question = random.choice(check_questions)
                logger.info(f"[CHECK] 체크 질문 발송: {selected_question}")
                
                # 메시지 테이블에 저장
                try:
                    await save_message(session, conv_id, "assistant", selected_question, x_request_id, user_id=user_id)
                    logger.info(f"[메시지저장] 체크 질문 발송 메시지 저장 완료")
                except Exception as e:
                    logger.warning(f"[메시지저장] 체크 질문 발송 메시지 저장 실패: {e}")
                
                return kakao_text(selected_question)
            except Exception as e:
                logger.error(f"[CHECK] 체크 질문 발송 실패: {e}")
                import traceback
                logger.error(f"[CHECK] 상세 에러: {traceback.format_exc()}")
        elif check_score is not None:
            logger.info(f"[CHECK_DEBUG] 체크 질문 응답이 이미 처리됨 (check_score={check_score}): 체크 질문 발송 건너뜀")
        elif user_risk_history.last_check_score is not None:
            logger.info(f"[CHECK_DEBUG] 이전 체크 질문 응답이 있음 (last_check_score={user_risk_history.last_check_score}): 체크 질문 발송 건너뜀")
        else:
            logger.info(f"[CHECK_DEBUG] 체크 질문 발송 조건 미충족: cumulative_score={cumulative_score}, db_score={db_score}")
            logger.info(f"[CHECK_DEBUG] should_send_check_question 결과: {should_send_check_question(cumulative_score, user_risk_history)}")
            logger.info(f"[CHECK_DEBUG] user_risk_history.check_question_turn_count: {user_risk_history.check_question_turn_count}")
            logger.info(f"[CHECK_DEBUG] user_risk_history.can_send_check_question(): {user_risk_history.can_send_check_question()}")

        logger.info(f"----- [7단계 완료: 체크 질문 발송 및 위험도 처리] -----")
        
        # ====== [일반 대화 후 점수 유지] ==============================================
        logger.info(f"----- [8단계: 일반 대화 처리 시작] -----")
        # 일반 대화 후에는 turns와 점수를 유지하여 누적 위험도를 추적
        # check_question_turn_count로 20턴 동안 재질문을 방지
        logger.info(f"[RISK] 일반 대화 완료 후 점수 유지: turns_count={len(user_risk_history.turns)}, check_question_turn_count={user_risk_history.check_question_turn_count}")

        # ★ 0) '이' 모호성 질문(예: "'민정'(이)야? 아니면 '민정이'야?")에 대한 **다음 턴 응답** 최우선 처리
        if JosaDisambCache.is_pending(user_id):
            cand = strip_suffixes(clean_name(user_text_stripped))
            if not (is_valid_name(cand)
                    and not contains_profanity(cand)
                    and not is_common_non_name(cand)
                    and not is_bot_name(cand)):
                PendingNameCache.set_waiting(user_id)
                return kakao_text("그건 이름처럼 들리지 않아.\n예) 민수, 지현")

            try:
                await save_user_name(session, user_id, cand)
                PendingNameCache.clear(user_id)
                JosaDisambCache.clear(user_id)
                return kakao_text(f"반가워 {cand}! 앞으로 {cand}(이)라고 부를게🐥")
            except Exception as e:
                logger.bind(x_request_id=x_request_id).exception(f"[오류] 이름 저장 실패: {e}")
                PendingNameCache.set_waiting(user_id)
                JosaDisambCache.clear(user_id)
                return kakao_text("앗, 저장 중 문제가 있었어. 다시 알려줄래?")
                
        # ====== [이름 처리 로직] ==============================================
        # 이름 없는 사용자 처리
        user = await session.get(AppUser, user_id)
        if user is None or user.user_name is None:
            if PendingNameCache.is_waiting(user_id):
                logger.info(f"[처리] 이름 입력 모드: '{user_text_stripped}'")
                
                raw = clean_name(user_text_stripped)
                if contains_profanity(raw) or is_common_non_name(raw) or is_bot_name(raw):
                    response_text = "그 이름은 사용할 수 없어.\n한글/영문 1~20자로 예쁜 이름을 알려줘!\n예) 민수, Yeonwoo"
                    return kakao_text(response_text)
                
                cand = extract_simple_name(user_text_stripped)
                if not cand:
                    return kakao_text("그건 이름처럼 들리지 않아.\n예) 민수, 지현")
                    
                if cand and is_valid_name(cand):
                    # 조사 질문 확인
                    needs_josa_question, josa_question = check_name_with_josa(cand)
                    if needs_josa_question:
                        # 조사 질문이 필요한 경우 대기 상태로 설정하고 질문 반환
                        PendingNameCache.set_waiting(user_id)
                        JosaDisambCache.set_pending(user_id)
                        return kakao_text(josa_question)
                    
                    try:
                        await save_user_name(session, user_id, cand)
                        PendingNameCache.clear(user_id)
                        return kakao_text(f"반가워 {cand}! 앞으로 {cand}(이)라고 부를게🐥")
                    except Exception as e:
                        logger.bind(x_request_id=x_request_id).exception(f"[오류] 이름 저장 실패: {e}")
                        PendingNameCache.clear(user_id)
                else:
                    response_text = "이름 형식은 한글/영문 1~20자야.\n예) 민수, Yeonwoo"
                    return kakao_text(response_text)
            
            elif any(g in user_text_stripped.lower() for g in _GREETINGS):
                logger.info(f"[인사] 인삿말 감지 → 대기 상태")
                PendingNameCache.set_waiting(user_id)
                prompt_name = await get_active_prompt_name(session)
                return kakao_text(random.choice(get_welcome_messages(prompt_name)))
            
            else:
                logger.info(f"[질문] 이름 요청 → 대기 상태")
                PendingNameCache.set_waiting(user_id)
                prompt_name = await get_active_prompt_name(session)
                return kakao_text(f"안녕! 처음 보네~ 나는 {prompt_name}야🐥\n불리고 싶은 이름을 알려주면, 앞으로 그렇게 불러줘!")
        
        # '/이름' 명령 처리
        if user_text_stripped == "/이름":
            PendingNameCache.set_waiting(user_id)
            prompt_name = await get_active_prompt_name(session)
            return kakao_text(
                f"불리고 싶은 이름을 입력해줘! 그럼 {prompt_name}가 꼭 기억할게~\n\n"
                f"💡 자연스럽게 '내 이름은 민수야'라고 말해도 알아들을 수 있어!"
            )

        # 이름 대기 상태 처리
        if PendingNameCache.is_waiting(user_id):
            logger.info(f"[대기] 이름 대기 상태 입력 처리: '{user_text_stripped}'")

            # 사용자가 취소를 말한 경우
            if user_text_stripped in ("취소", "그만", "아냐", "아니야", "됐어", "아니"):
                PendingNameCache.clear(user_id)
                return kakao_text("좋아, 다음에 다시 알려줘!")

            # 일반 이름 입력 처리 (여기서만 cand를 만든다)
            cand = extract_simple_name(user_text_stripped)
            if not cand:
                return kakao_text("그건 이름처럼 들리지 않아.\n예) 민수, 지현")

            if contains_profanity(cand) or is_common_non_name(cand) or is_bot_name(cand):
                return kakao_text("그 이름은 사용할 수 없어.\n예) 민수, Yeonwoo")

            if not is_valid_name(cand):
                return kakao_text("이름 형식은 한글/영문 1~20자야.\n예) 민수, Yeonwoo")

            # ✅ '민정이' 같은 '이' 모호성 질문
            needs_josa_question, josa_question = check_name_with_josa(cand)
            if needs_josa_question:
                PendingNameCache.set_waiting(user_id)   # 대기 유지
                JosaDisambCache.set_pending(user_id)    # 다음 턴에서 확정 처리
                return kakao_text(josa_question)

            # 최종 저장
            try:
                await save_user_name(session, user_id, cand)
                PendingNameCache.clear(user_id)
                JosaDisambCache.clear(user_id)
                return kakao_text(f"이름 예쁘다! 앞으로는 '{cand}'(이)라고 불러줄게~")
            except Exception as name_err:
                logger.bind(x_request_id=x_request_id).exception(f"save_user_name failed: {name_err}")
                PendingNameCache.clear(user_id)
                JosaDisambCache.clear(user_id)
                return kakao_text("앗, 이름을 저장하는 중에 문제가 생겼나봐. 잠시 후 다시 시도해줘!")


        
        # '/이름 xxx' 즉시 저장
        if user_text_stripped.startswith("/이름 "):
            raw = user_text_stripped[len("/이름 "):]
            cand = clean_name(raw)
            
            if contains_profanity(cand) or is_common_non_name(cand) or is_bot_name(cand):
                return kakao_text("그 이름은 사용할 수 없어.\n한글/영문 1~20자로 예쁜 이름을 알려줘!\n예) 민수, Yeonwoo")
            
            if not is_valid_name(cand):
                return kakao_text("이름 형식은 한글/영문 1~20자야.\n예) 민수, Yeonwoo")
            
            # 조사 질문 확인
            needs_josa_question, josa_question = check_name_with_josa(cand)
            if needs_josa_question:
                # 조사 질문이 필요한 경우 대기 상태로 설정하고 질문 반환
                PendingNameCache.set_waiting(user_id)
                JosaDisambCache.set_pending(user_id)
                return kakao_text(josa_question)
            
            try:
                await save_user_name(session, user_id, cand)
                return kakao_text(f"예쁜 이름이다! 앞으로는 {cand}(이)라고 불러줘~")
            except Exception as name_err:
                logger.bind(x_request_id=x_request_id).exception(f"save_user_name failed: {name_err}")
                return kakao_text("앗, 이름을 저장하는 중에 문제가 생겼나봐. 잠시 후 다시 시도해줘!")

        # ====== [이름 처리 완료: 이하 기존 로직 유지] ===========================

        ENABLE_CALLBACK = True

        # 프롬프트 선택: DB에서 활성화된 프롬프트 자동 감지
        prompt_name = "auto"  # ai_service에서 활성화된 프롬프트 자동 선택
        logger.info(f"[PROMPT] 활성화된 프롬프트 자동 감지 사용: {prompt_name}")

        if ENABLE_CALLBACK and callback_url and isinstance(callback_url, str) and callback_url.startswith("http"):
            return await _handle_callback_flow(session, user_id, user_text, callback_url, conv_id, x_request_id)

        # 4) 콜백이 아닌 경우: 기존 즉시 응답 흐름
        try:
            async def _ensure_conv_main():
                try:
                    await upsert_user(session, user_id)
                except Exception:
                    try:
                        await session.rollback()
                        await upsert_user(session, user_id)
                    except Exception as e:
                        logger.warning(f"upsert_user failed after rollback: {e}")
                        raise
                try:
                    return await get_or_create_conversation(session, user_id)
                except Exception:
                    try:
                        await session.rollback()
                        return await get_or_create_conversation(session, user_id)
                    except Exception as e:
                        logger.warning(f"get_or_create_conversation failed after rollback: {e}")
                        raise
            conv = await asyncio.wait_for(_ensure_conv_main(), timeout=1.5)
            conv_id = conv.conv_id
        except Exception as db_err:
            logger.warning(f"DB ops failed in immediate path: {db_err}")
            conv_id = f"temp_{user_id}"

        logger.info(f"----- [8단계 완료: 일반 대화 처리] -----")
        
        # ====== [AI 응답 생성] ==============================================
        logger.info(f"----- [9단계: AI 응답 생성 시작] -----")
        # 5) AI 답변
        try:
            logger.info(f"Generating AI response for: {user_text}")
            

            try:
                final_text, tokens_used = await asyncio.wait_for(
                    ai_service.generate_response(
                        session=session,
                        conv_id=conv_id,
                        user_input=user_text,
                        prompt_name=prompt_name,
                        user_id=user_id,
                        request_id=x_request_id
                    ),
                    timeout=AI_GENERATION_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning("AI generation timeout. Falling back to canned message.")
                final_text, tokens_used = ("답변 생성이 길어졌어요. 잠시만 기다려주세요.", 0)
            logger.info(f"AI response generated: {final_text[:50]}...")
            

            try:
                # conv_id가 유효한 경우에만 전달
                safe_conv_id = conv_id if conv_id and not str(conv_id).startswith("temp_") else None
                await save_log_message(session, "message_generated", f"AI message generated: {len(final_text)} chars", str(user_id), conv_id, {"source": "ai_generation", "tokens": tokens_used, "x_request_id": x_request_id})
            except Exception as log_err:
                logger.warning(f"AI message log save failed: {log_err}")
            
            try:
                if not str(conv_id).startswith("temp_") and conv_id:
                    async def _save_user_message_background(conv_id, user_text, x_request_id, user_id):
                        async for s in get_session():
                            try:
                                await save_message(s, conv_id, "user", user_text, x_request_id, None, user_id)
                                break
                            except Exception:
                                break

                    async def _save_ai_response_background(conv_id, final_text, tokens_used, x_request_id, user_id):
                        async for s in get_session():
                            try:
                                await save_message(s, conv_id, "assistant", final_text, x_request_id, tokens_used, user_id)
                                break
                            except Exception:
                                break

                    asyncio.create_task(_save_user_message_background(conv_id, user_text, x_request_id, user_id))
                    asyncio.create_task(_save_ai_response_background(conv_id, final_text, 0, x_request_id, user_id))
                else:
                    async def _persist_when_db_ready(user_id: str, user_text: str, reply_text: str, request_id: str | None):
                        async for s in get_session():
                            try:
                                await upsert_user(s, user_id)
                                conv = await get_or_create_conversation(s, user_id)
                                if user_text:
                                    await save_message(s, conv.conv_id, "user", user_text, request_id, None, user_id)
                                await save_message(s, conv.conv_id, "assistant", reply_text, request_id, None, user_id)
                                break
                            except Exception as persist_err:
                                logger.bind(x_request_id=request_id).warning(f"Persist after temp conv failed: {persist_err}")
                                break
                    asyncio.create_task(_persist_when_db_ready(user_id, user_text, final_text, x_request_id))
            except Exception as save_error:
                logger.warning(f"Failed to schedule message persistence: {save_error}")
            
            try:
                update_last_activity(conv_id)
            except Exception:
                pass

            logger.info(f"----- [9단계 완료: AI 응답 생성] -----")
            logger.info(f"===== [위험도 분석 완료] ==============================================")
            
            return JSONResponse(content={
                "version": "2.0",
                "template": {"outputs":[{"simpleText":{"text": remove_markdown(final_text)}}]}
            }, media_type="application/json; charset=utf-8")
            
        except Exception as ai_error:
            logger.error(f"AI generation failed: {ai_error}")
            import traceback
            logger.error(f"Full traceback: {traceback.format_exc()}")
            final_text = "죄송합니다. 일시적인 오류가 발생했습니다. 다시 한 번 시도해주세요."
            
            return JSONResponse(content={
                "version": "2.0",
                "template": {"outputs":[{"simpleText":{"text": final_text}}]}
            }, media_type="application/json; charset=utf-8")
        
    except Exception as e:
        logger.exception(f"Error in skill endpoint: {e}")
        
        # LogMessage에도 저장
        try:
            await save_log_message(session, "ERROR", f"Error in skill endpoint: {e}", None, None, {"source": "error"})
        except Exception as log_err:
            logger.warning(f"Error log save failed: {log_err}")
        safe_text = "일시적인 오류가 발생했어요. 다시 한 번 시도해 주세요"
        return JSONResponse(content={
            "version": "2.0",
            "template": {"outputs":[{"simpleText":{"text": safe_text}}]}
        }, media_type="application/json; charset=utf-8")




@router.post("/welcome")
async def welcome_skill(request: Request, session: AsyncSession = Depends(get_session)):
    """웰컴 스킬: 처음 대화를 시작할 때 웰컴 메시지를 보냅니다."""
    try:
        # 1) 요청 처리
        x_request_id = request.headers.get("X-Request-ID") or request.headers.get("X-Request-Id")
        logger.bind(x_request_id=x_request_id).info("Welcome skill request received")
        
        try:
            body = await request.json()
            if not isinstance(body, dict):
                body = {}
        except Exception as parse_err:
            logger.warning(f"JSON parse failed: {parse_err}")
            body = {}
            
        # 2) 사용자 ID 추출
        user_id = extract_user_id(body)
        if not user_id:
            anon_suffix = x_request_id or "unknown"
            user_id = f"anonymous:{anon_suffix}"
            logger.warning("No user_id in welcome skill, using fallback")
            
        # 3) 웰컴 메시지 전송
        response_text = random.choice(_WELCOME_MESSAGES)
        return JSONResponse(content={
            "version": "2.0",
            "template": {"outputs": [{"simpleText": {"text": response_text}}]}
        }, media_type="application/json; charset=utf-8")
        
    except Exception as e:
        logger.exception(f"Error in welcome skill: {e}")
        # 에러 발생 시에도 기본 웰컴 메시지 반환
        try:
            response_text = random.choice(_WELCOME_MESSAGES)
        except Exception:
            response_text = "안녕하세요! 무엇을 도와드릴까요?"
            
        return JSONResponse(content={
            "version": "2.0",
            "template": {"outputs": [{"simpleText": {"text": response_text}}]}
        }, media_type="application/json; charset=utf-8")



