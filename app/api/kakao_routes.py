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
from app.main import http_client, BUDGET, ENABLE_CALLBACK
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
            # JSON 파싱 실패 시에도 빈 바디로 진행해 400 방지 + 로깅 강화
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

        # 2) 콜백 요청이면 즉시 응답 후 비동기 콜백 (DB 이전)
        # 3) 사용자 발화 추출 (Optional userRequest 방어)
        user_text = (body_dict.get("userRequest") or {}).get("utterance", "")
        # trace_id는 X-Request-ID를 사용 (메모리 기반 히스토리 기능 롤백)
        trace_id = x_request_id
        if not isinstance(user_text, str):
            user_text = str(user_text or "")
        if not user_text:
            # 카카오 스펙 검증용 빈 발화가 들어올 수 있어 기본값 제공
            user_text = "안녕하세요"

        if ENABLE_CALLBACK and callback_url and isinstance(callback_url, str) and callback_url.startswith("http"):
            # 하이브리드: 4.5초내 완료 시 즉시 최종 응답, 아니면 콜백 대기 응답 후 콜백 2회(대기+최종)
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

            # 시간 내 미완료시 즉시 콜백 대기 응답 반환, 백그라운드에서 '대기 콜백' 후 '최종 콜백' 순으로 전송
            immediate = callback_waiting_response("답변을 생성 중입니다...")
            try:
                await save_event_log(session, "callback_waiting_sent", user_id, None, x_request_id, None)
            except Exception:
                pass

            async def _handle_callback_full(callback_url: str, user_id: str, user_text: str, request_id: str | None):
                final_text: str = "죄송합니다. 일시적인 오류가 발생했습니다. 다시 한 번 시도해주세요."
                tokens_used: int = 0
                try:
                    # 여기서 독립 세션으로 모든 무거운 작업 처리
                    async for s in get_session():
                        try:
                            # DB 작업은 타임아웃 가드로 감쌉니다 (카카오 5초 제한 보호)
                            async def _ensure_conv():
                                await upsert_user(s, user_id)
                                return await get_or_create_conversation(s, user_id)
                            conv = await asyncio.wait_for(_ensure_conv(), timeout=0.7)
                            conv_id_value = str(conv.conv_id)
                            # 사용자 메시지 먼저 저장
                            try:
                                if user_text:
                                    await save_message(s, conv_id_value, "user", user_text, trace_id, None, user_id)
                            except Exception as save_user_err:
                                logger.bind(x_request_id=request_id).warning(f"Failed to save user message in callback: {save_user_err}")
                            # AI 생성: 콜백 경로에서는 충분한 시간으로 생성 (타임아웃 미사용)
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

                    # 최종 콜백 전송 (한 번만)
                    try:
                        await _send_callback_response(callback_url, final_text, tokens_used, request_id)
                    except Exception as post_err:
                        logger.bind(x_request_id=request_id).exception(f"Callback post failed: {post_err}")

                    # 추가 콜백 전송 없음 (한 번만 전송)
                except Exception as e:
                    logger.bind(x_request_id=request_id).exception(f"Callback flow failed: {e}")

            asyncio.create_task(_handle_callback_full(callback_url, user_id, user_text, x_request_id))

            try:
                update_last_activity(f"temp_{user_id}")
            except Exception:
                pass
            return JSONResponse(content=immediate, media_type="application/json; charset=utf-8")

        # 4) 즉시응답 경로로 DB 작업 수행 (콜백 비활성화거나 콜백 URL 없음)
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

        # 5) 콜백이 아닌 경우: AI 답변 생성 (BUDGET 적용)
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
                final_text, tokens_used = ("잠시만요! 답변 생성이 길어져서 간단히 안내드려요", 0)
            logger.info(f"AI response generated: {final_text[:50]}...")
            try:
                await save_event_log(session, "message_generated", user_id, conv_id, x_request_id, {"tokens": tokens_used})
            except Exception:
                pass
            
            # 메시지 저장 시도 (DB 장애 없으면 temp는 없음)
            try:
                if not str(conv_id).startswith("temp_"):
                    # 기존 방식: conv_id가 유효하면 바로 저장
                    asyncio.create_task(_save_user_message_background(
                        conv_id, user_text, x_request_id, user_id
                    ))
                    asyncio.create_task(_save_ai_response_background(
                        conv_id, final_text, 0, x_request_id, user_id
                    ))
                else:
                    # temp_* 인 경우에도 백그라운드에서 정식 conv 생성 후 저장 시도
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
            
            # 액티비티 업데이트
            try:
                update_last_activity(conv_id)
            except Exception:
                pass
            # 카카오로 응답 전송
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
        # 카카오 스펙 준수: 기본 본문과 함께 200 OK를 내려 400 회피
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
        user_id = extract_user_id(body)
        if not user_id:
            anon_suffix = x_request_id or "unknown"
            user_id = f"anonymous:{anon_suffix}"
            
        # 3) 사용자 발화 추출
        user_text = (body.get("userRequest") or {}).get("utterance", "")
        if not isinstance(user_text, str):
            user_text = str(user_text or "")
            
        # 4) 이름 추출 시도
        name = extract_korean_name(user_text)
        if name:
            # 이름이 추출되면 저장
            try:
                user = await session.get(AppUser, user_id)
                if user:
                    user.user_name = name
                    await session.commit()
                    response_text = f"반가워 {name}아(야)! 앞으로 {name}(이)라고 부를게🦉"
                else:
                    response_text = random.choice(_WELCOME_MESSAGES)
            except Exception as e:
                logger.error(f"Failed to save user name: {e}")
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
