from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.db import get_session
from app.schemas.schemas import simple_text, callback_waiting_response
from app.database.service import upsert_user, get_or_create_conversation, save_message, save_event_log
from app.database.models import AppUser
from app.utils.utils import extract_user_id, extract_callback_url, remove_markdown
from app.core.ai_service import ai_service
from app.core.background_tasks import _save_user_message_background, _save_ai_response_background, update_last_activity, _send_callback_response
from app.core.summary import maybe_rollup_user_summary
from app.main import BUDGET, ENABLE_CALLBACK
import time
import asyncio

"""카카오 스킬 관련 라우터"""
import asyncio
import random
import re

# 이름 추출을 위한 정규식 패턴들
_NAME_PREFIX_PATTERN = re.compile(r'^(내\s*이름은|제\s*이름은|난|나는|저는|전|내|제|나|저)\s*', re.IGNORECASE)
_NAME_SUFFIX_PATTERN = re.compile(r'\s*(입니다|이에요|예요|에요|야|이야|라고\s*해|라고\s*해요|이라고\s*해|이라고\s*해요|합니다|불러|불러줘|라고\s*불러|라고\s*불러줘|이라고\s*불러|이라고\s*불러줘)\.?$', re.IGNORECASE)
_KOREAN_NAME_PATTERN = re.compile(r'[가-힣]{2,4}')

# 웰컴 메시지 템플릿
_WELCOME_MESSAGES = [
    "안녕~ 난 나온이야🦉 너는 이름이 뭐야?",
    "안녕~ 난 나온이야🦉 내가 뭐라고 부르면 좋을까?",
    "안녕~ 난 나온이야🦉 네 이름이 궁금해. 알려줘~!"
]

def extract_korean_name(text: str) -> str | None:
    """사용자 입력에서 한글 이름을 추출합니다."""
    # 입력 정규화
    text = text.strip()
    if not text:
        return None
        
    # 앞뒤 패턴 제거
    text = _NAME_PREFIX_PATTERN.sub('', text)
    text = _NAME_SUFFIX_PATTERN.sub('', text)
    
    # 남은 텍스트에서 한글 이름 패턴 찾기
    match = _KOREAN_NAME_PATTERN.search(text)
    if match:
        return match.group()
    return None
    
router = APIRouter()


# ====== [이름 저장 보조 유틸] =================================================

# 허용 문자(한글/영문/숫자/중점/하이픈/언더스코어), 길이 1~20
NAME_ALLOWED = re.compile(r"^[가-힣a-zA-Z0-9·\-\_]{1,20}$")

def clean_name(s: str) -> str:
    s = s.strip()
    # 양쪽 따옴표/괄호/장식 제거
    s = re.sub(r'[\"\'“”‘’()\[\]{}<>…~]+', "", s)
    return s.strip()

def is_valid_name(s: str) -> bool:
    return bool(NAME_ALLOWED.fullmatch(s))

class PendingNameCache:
    """간단한 in-memory 캐시 (운영에선 Redis/DB 권장)"""
    _store: dict[str, float] = {}
    TTL_SECONDS = 300  # 5분

    @classmethod
    def set_waiting(cls, user_id: str):
        cls._store[user_id] = time.time() + cls.TTL_SECONDS

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
        cls._store.pop(user_id, None)

async def save_user_name(session: AsyncSession, user_id: str, name: str):
    """appuser.user_name 저장/갱신"""
    await upsert_user(session, user_id)
    user = await session.get(AppUser, user_id)
    if user is None:
        user = AppUser(user_id=user_id, user_name=name)
        session.add(user)
    else:
        user.user_name = name
    await session.commit()

def kakao_text(text: str) -> JSONResponse:
    return JSONResponse(
        content={
            "version": "2.0",
            "template": {"outputs": [{"simpleText": {"text": text}}]}
        },
        media_type="application/json; charset=utf-8"
    )

# ====== [스킬 엔드포인트] =====================================================

@router.post("/skill")
@router.post("/skill/")
async def skill_endpoint(
    request: Request,
    session: AsyncSession = Depends(get_session)
):
    # 최우선 로그 - 요청이 들어왔다는 것만 확인
    print(f"=== SKILL REQUEST RECEIVED ===")
    logger.info("=== SKILL REQUEST RECEIVED ===")
    
    try:
        # 1) 헤더 추적자
        x_request_id = request.headers.get("X-Request-ID") or request.headers.get("X-Request-Id")
        logger.bind(x_request_id=x_request_id).info("Incoming skill request")

        # 전체 요청 시간 추적 (카카오 5초 제한 준수)
        t0 = time.perf_counter()

        try:
            body_dict = await request.json()
            if not isinstance(body_dict, dict):
                body_dict = {}
        except Exception as parse_err:
            logger.warning(f"JSON parse failed: {parse_err}")
            body_dict = {}
        
        # 서버가 받은 데이터 로깅
        logger.bind(x_request_id=x_request_id).info(f"Received body: {body_dict}")
        
        user_id = extract_user_id(body_dict)
        logger.bind(x_request_id=x_request_id).info(f"Extracted user_id: {user_id}")

        # 폴백: user_id가 비어있으면 익명 + X-Request-ID 사용
        if not user_id:
            anon_suffix = x_request_id or "unknown"
            user_id = f"anonymous:{anon_suffix}"
            logger.bind(x_request_id=x_request_id).warning(f"user_id missing. fallback -> {user_id}")

        callback_url = extract_callback_url(body_dict)
        logger.bind(x_request_id=x_request_id).info(f"Extracted callback_url: {callback_url}")

        # 2) 사용자 발화 추출
        user_text = (body_dict.get("userRequest") or {}).get("utterance", "")
        trace_id = x_request_id
        if not isinstance(user_text, str):
            user_text = str(user_text or "")
        if not user_text:
            user_text = "안녕하세요"
        user_text_stripped = user_text.strip()

        # ====== [이름 플로우: 최우선 인터셉트] ==================================
        # 2-1) '/이름' 명령만 온 경우 → 다음 발화를 이름으로 받기
        if user_text_stripped == "/이름":
            PendingNameCache.set_waiting(user_id)
            try:
                await save_event_log(session, "name_wait_start", user_id, None, x_request_id, None)
            except Exception:
                pass
            return kakao_text("불리고 싶은 이름을 입력해줘! 그럼 나온이가 꼭 기억할게~")

        # 2-2) '/이름 xxx' 형태 → 즉시 저장 시도
        if user_text_stripped.startswith("/이름 "):
            raw = user_text_stripped[len("/이름 "):]
            cand = clean_name(raw)
            if not is_valid_name(cand):
                return kakao_text("이름 형식은은 한글/영문 1~20자로 입력해줘!\n예) 민수, Yeonwoo")
            try:
                await save_user_name(session, user_id, cand)
                try:
                    await save_event_log(session, "name_saved", user_id, None, x_request_id, {"name": cand, "mode": "slash_inline"})
                except Exception:
                    pass
                return kakao_text(f"예쁜 이름이다! 앞으로는 {cand}(이)라고 불러줄게~")
            except Exception as name_err:
                logger.bind(x_request_id=x_request_id).exception(f"save_user_name failed: {name_err}")
                return kakao_text("앗, 이름을 저장하는 중에 문제가 생겼나봐. 잠시 후 다시 시도해줘!")

        # 2-3) 이전에 '/이름'을 받은 뒤 다음 발화가 온 경우 → 해당 발화를 이름으로 간주
        if PendingNameCache.is_waiting(user_id):
            # 취소 지원
            if user_text_stripped in ("취소", "그만", "cancel", "Cancel"):
                PendingNameCache.clear(user_id)
                try:
                    await save_event_log(session, "name_wait_cancel", user_id, None, x_request_id, None)
                except Exception:
                    pass
                return kakao_text("좋아, 다음에 다시 알려줘!")

            cand = clean_name(user_text_stripped)
            if not is_valid_name(cand):
                return kakao_text("이름 형식은 한글/영문 1~20자로 입력해줘!\n예) 민수, Yeonwoo")

            try:
                await save_user_name(session, user_id, cand)
                PendingNameCache.clear(user_id)
                try:
                    await save_event_log(session, "name_saved", user_id, None, x_request_id, {"name": cand, "mode": "followup"})
                except Exception:
                    pass
                return kakao_text(f"이름 예쁘다! 앞으로는 {cand}(이)라고 불러줄게~")
            except Exception as name_err:
                logger.bind(x_request_id=x_request_id).exception(f"save_user_name failed: {name_err}")
                return kakao_text("앗, 이름을 저장하는 중에 문제가 생겼나봐. 잠시 후 다시 시도해줘!")

        # ====== [이름 플로우 끝: 이하 기존 로직 유지] ===========================

        ENABLE_CALLBACK = True   # 기존 설정 사용하던 값에 맞춰주세요
        BUDGET = 4.5             # 기존 타임아웃에 맞춰 조정

        if ENABLE_CALLBACK and callback_url and isinstance(callback_url, str) and callback_url.startswith("http"):
            elapsed = time.perf_counter() - t0
            time_left = max(0.2, 4.5 - elapsed)
            try:
                try:
                    await save_event_log(session, "request_received", user_id, None, x_request_id, {"callback": True})
                except Exception:
                    pass

                async def _ensure_quick_conv():
                    await upsert_user(session, user_id)
                    conv = await get_or_create_conversation(session, user_id)
                    return conv.conv_id

                try:
                    quick_conv_id = await asyncio.wait_for(_ensure_quick_conv(), timeout=min(1.0, time_left - 0.1))
                except Exception:
                    quick_conv_id = f"temp_{user_id}"

                quick_text, quick_tokens = await asyncio.wait_for(
                    ai_service.generate_response(
                        session=session,
                        conv_id=quick_conv_id,
                        user_input=user_text,
                        prompt_name="default",
                        user_id=user_id
                    ),
                    timeout=time_left,
                )

                async def _persist_quick(user_id: str, user_text: str, reply_text: str, request_id: str | None):
                    async for s in get_session():
                        try:
                            await upsert_user(s, user_id)
                            conv = await get_or_create_conversation(s, user_id)
                            try:
                                await save_message(s, conv.conv_id, "user", user_text, trace_id, None, user_id)
                                await save_message(s, conv.conv_id, "assistant", remove_markdown(reply_text), trace_id, quick_tokens, user_id)
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
                await save_event_log(session, "callback_waiting_sent", user_id, None, x_request_id, None)
            except Exception:
                pass

            import re

            MAX_SIMPLETEXT = 900   # 카카오 안전 마진
            MAX_OUTPUTS    = 3     # 한 번에 보낼 simpleText 개수

            _SENT_ENDERS = ("...", "…", ".", "!", "?", "。", "！", "？")

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
                        for p in _SENT_ENDERS:
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
                if not callback_url or not isinstance(callback_url, str) or not callback_url.startswith("http"):
                    logger.bind(x_request_id=request_id).error(f"Invalid callback_url: {callback_url!r}")
                    return

                import json, httpx, urllib.request

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
                                    await save_message(s, conv_id_value, "user", user_text, trace_id, None, user_id)
                            except Exception as save_user_err:
                                logger.bind(x_request_id=request_id).warning(f"Failed to save user message in callback: {save_user_err}")

                            final_text, tokens_used = await ai_service.generate_response(
                                session=s,
                                conv_id=conv_id_value,
                                user_input=user_text,
                                prompt_name="default",
                                user_id=user_id
                            )
                            await save_message(s, conv_id_value, "assistant", final_text, trace_id, tokens_used, user_id)
                            try:
                                await save_event_log(s, "callback_final_sent", user_id, conv_id_value, request_id, {"tokens": tokens_used})
                            except Exception:
                                pass
                            try:
                                await maybe_rollup_user_summary(s, user_id, conv_id_value)
                            except Exception:
                                pass
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

            asyncio.create_task(_handle_callback_full(callback_url, user_id, user_text, x_request_id))

            try:
                update_last_activity(f"temp_{user_id}")
            except Exception:
                pass
            return JSONResponse(content=immediate, media_type="application/json; charset=utf-8")

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

        # 5) AI 답변
        try:
            logger.info(f"Generating AI response for: {user_text}")
            try:
                final_text, tokens_used = await asyncio.wait_for(
                    ai_service.generate_response(
                        session=session,
                        conv_id=conv_id,
                        user_input=user_text,
                        prompt_name="default",
                        user_id=user_id
                    ),
                    timeout=BUDGET,
                )
            except asyncio.TimeoutError:
                logger.warning("AI generation timeout. Falling back to canned message.")
                final_text, tokens_used = ("답변 생성이 길어졌어요. 잠시만 기다려주세요.", 0)
            logger.info(f"AI response generated: {final_text[:50]}...")
            try:
                await save_event_log(session, "message_generated", user_id, conv_id, x_request_id, {"tokens": tokens_used})
            except Exception:
                pass
            
            try:
                if not str(conv_id).startswith("temp_"):
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
        safe_text = "일시적인 오류가 발생했어요. 다시 한 번 시도해 주세요"
        return JSONResponse(content={
            "version": "2.0",
            "template": {"outputs":[{"simpleText":{"text": safe_text}}]}
        }, media_type="application/json; charset=utf-8")


@router.post("/welcome")
async def welcome_skill(request: Request, session: AsyncSession = Depends(get_session)):
    """웰컴 스킬: 사용자 이름을 받아서 저장합니다."""
    try:
        # 1) 요청 처리
        x_request_id = request.headers.get("X-Request-ID") or request.headers.get("X-Request-Id")
        logger.bind(x_request_id=x_request_id).info("Incoming welcome skill request")
        
        try:
            body = await request.json()
            if not isinstance(body, dict):
                body = {}
        except Exception as parse_err:
            logger.warning(f"JSON parse failed: {parse_err}")
            body = {}
            
        # 2) 사용자 ID 추출
        logger.info(f"Welcome skill request body: {body}")  # 전체 요청 바디 로깅
        user_id = extract_user_id(body)
        logger.info(f"Extracted user_id from welcome skill: {user_id}")  # 추출된 user_id 로깅
        if not user_id:
            anon_suffix = x_request_id or "unknown"
            user_id = f"anonymous:{anon_suffix}"
            logger.warning(f"No user_id in welcome skill, using fallback: {user_id}")
            
        # 3) 사용자 발화 추출
        user_text = (body.get("userRequest") or {}).get("utterance", "")
        if not isinstance(user_text, str):
            user_text = str(user_text or "")
            
        # 4) 이름 추출 및 저장 시도 (skill과 동일한 로직)
        user_text_stripped = user_text.strip()
        
        # 이름 추출 시도 (skill과 동일한 패턴 매칭)
        name = None
        
        # 앞뒤 패턴 제거
        text = _NAME_PREFIX_PATTERN.sub('', user_text_stripped)
        text = _NAME_SUFFIX_PATTERN.sub('', text)
        text = text.strip()
        
        # 남은 텍스트에서 한글 이름 패턴 찾기
        if text:
            match = _KOREAN_NAME_PATTERN.search(text)
            if match:
                name = match.group()
        
        if name:
            # 이름이 추출되면 형식 검사 후 저장
            cand = clean_name(name)
            if is_valid_name(cand):
                try:
                    await save_user_name(session, user_id, cand)
                    try:
                        await save_event_log(session, "name_saved", user_id, None, x_request_id, {"name": cand, "mode": "welcome"})
                    except Exception:
                        pass
                    response_text = f"반가워 {cand}아(야)! 앞으로 {cand}(이)라고 부를게🦉"
                except Exception as e:
                    logger.bind(x_request_id=x_request_id).exception(f"save_user_name failed in welcome: {e}")
                    response_text = random.choice(_WELCOME_MESSAGES)
            else:
                # 이름 형식이 맞지 않으면 웰컴 메시지
                response_text = random.choice(_WELCOME_MESSAGES)
        else:
            # 이름이 없으면 웰컴 메시지
            response_text = random.choice(_WELCOME_MESSAGES)
            
        return JSONResponse(content={
            "version": "2.0",
            "template": {"outputs": [{"simpleText": {"text": response_text}}]}
        }, media_type="application/json; charset=utf-8")
        
    except Exception as e:
        logger.exception(f"Error in welcome skill: {e}")
        return JSONResponse(content={
            "version": "2.0",
            "template": {"outputs": [{"simpleText": {"text": random.choice(_WELCOME_MESSAGES)}}]}
        }, media_type="application/json; charset=utf-8")

@router.post("/test-skill")
async def test_skill_endpoint(request: Request):
    """디버깅용 테스트 엔드포인트 - 받은 데이터를 그대로 반환"""
    try:
        body = await request.json()
        print(f"TEST SKILL - Received: {body}")
        logger.info(f"TEST SKILL - Received: {body}")
        
        return {"status": "test_success", "received_data": body}
    except Exception as e:
        print(f"TEST SKILL - Error: {e}")
        return {"error": str(e)}


@router.post("/test-callback")
async def test_callback_endpoint(request: Request):
    """콜백 테스트용 엔드포인트 - 받은 콜백 데이터를 로깅"""
    try:
        body = await request.json()
        print(f"CALLBACK TEST - Received: {body}")
        logger.info(f"CALLBACK TEST - Received: {body}")
        
        return {"status": "callback_received", "data": body}
    except Exception as e:
        print(f"CALLBACK TEST - Error: {e}")
        return {"error": str(e)}
