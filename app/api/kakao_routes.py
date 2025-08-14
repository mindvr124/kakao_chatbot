"""카카오 스킬 관련 라우터"""
import asyncio
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.db import get_session
from app.schemas.schemas import simple_text, callback_waiting_response
from app.database.service import upsert_user, get_or_create_conversation, save_message
from app.utils.utils import extract_user_id, extract_callback_url, remove_markdown
from app.core.ai_service import ai_service
from app.core.background_tasks import _save_user_message_background, _save_ai_response_background, update_last_activity, _send_callback_response
from app.core.summary import maybe_rollup_user_summary
from app.main import http_client, BUDGET, ENABLE_CALLBACK
import time
import asyncio

router = APIRouter()


@router.post("/skill")
@router.post("/skill/")
async def skill_endpoint(
    request: Request,
    session: AsyncSession = Depends(get_session)
):
    # 최우선 로그 - 요청이 들어왔다는 것부터 확인
    print(f"=== SKILL REQUEST RECEIVED ===")
    logger.info("=== SKILL REQUEST RECEIVED ===")
    
    try:
        # 1) 헤더 추적값
        x_request_id = request.headers.get("X-Request-ID") or request.headers.get("X-Request-Id")
        logger.bind(x_request_id=x_request_id).info("Incoming skill request")

        # 전체 요청 시간 추적 (카카오 5초 제한 대비)
        t0 = time.perf_counter()

        try:
            body_dict = await request.json()
            if not isinstance(body_dict, dict):
                body_dict = {}
        except Exception as parse_err:
            # JSON 파싱 실패 시에도 빈 바디로 진행해 400 방지 + 로깅 강화
            logger.warning(f"JSON parse failed: {parse_err}")
            body_dict = {}
        
        # 디버깅: 받은 데이터 로깅
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
        # 3) 유저 발화 추출 (Optional userRequest 방어)
        user_text = (body_dict.get("userRequest") or {}).get("utterance", "")
        # trace_id는 X-Request-ID만 사용 (메모리/대화 히스토리 기능 롤백)
        trace_id = x_request_id
        if not isinstance(user_text, str):
            user_text = str(user_text or "")
        if not user_text:
            # 카카오 스펙 검사 시 빈 발화로 호출될 수 있어 기본값 제공
            user_text = "안녕하세요"

        if ENABLE_CALLBACK and callback_url and isinstance(callback_url, str) and callback_url.startswith("http"):
            # 하이브리드: 4.5초 내 완료 시 즉시 최종 응답, 아니면 콜백 대기 응답 후 콜백 2회(대기/최종)
            elapsed = time.perf_counter() - t0
            time_left = max(0.2, 4.5 - elapsed)
            try:
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
                            await save_message(s, conv.conv_id, "user", user_text, trace_id, None, user_id)
                            await save_message(s, conv.conv_id, "assistant", remove_markdown(reply_text), trace_id, quick_tokens, user_id)
                            try:
                                await maybe_rollup_user_summary(s, user_id, conv.conv_id)
                            except Exception:
                                pass
                            break
                        except Exception as persist_err:
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

            # 시간 내 미완료 → 즉시 콜백 대기 응답 반환, 백그라운드에서 '대기 콜백' → '최종 콜백' 순으로 전송
            immediate = callback_waiting_response("답변을 생성 중입니다...")

            async def _handle_callback_full(callback_url: str, user_id: str, user_text: str, request_id: str | None):
                final_text: str = "죄송합니다. 일시적인 오류가 발생했습니다. 잠시 후 다시 시도해주세요."
                tokens_used: int = 0
                try:
                    # 내부에서 독립 세션으로 모든 무거운 작업 처리
                    async for s in get_session():
                        try:
                            # DB 작업은 타임아웃 가드로 감쌉니다 (카카오 5초 제한 보호)
                            async def _ensure_conv():
                                await upsert_user(s, user_id)
                                return await get_or_create_conversation(s, user_id)
                            conv = await asyncio.wait_for(_ensure_conv(), timeout=0.7)
                            # 사용자 메시지 먼저 저장
                            try:
                                if user_text:
                                    await save_message(s, conv.conv_id, "user", user_text, trace_id, None, user_id)
                            except Exception as save_user_err:
                                logger.bind(x_request_id=request_id).warning(f"Failed to save user message in callback: {save_user_err}")
                            # AI 생성에 BUDGET 가드
                            try:
                                final_text, tokens_used = await asyncio.wait_for(
                                    ai_service.generate_response(
                                        session=s,
                                        conv_id=conv.conv_id,
                                        user_input=user_text,
                                        prompt_name="default",
                                        user_id=user_id
                                    ),
                                    timeout=BUDGET,
                                )
                            except asyncio.TimeoutError:
                                logger.bind(x_request_id=request_id).warning("AI generation timeout in callback; using fallback message")
                                final_text = "답변 생성이 지연되어 간단히 안내드려요 🙏"
                                tokens_used = 0
                            await save_message(s, conv.conv_id, "assistant", final_text, trace_id, tokens_used, user_id)
                            try:
                                await maybe_rollup_user_summary(s, user_id, conv.conv_id)
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
                except asyncio.TimeoutError:
                    # AI 타임아웃 시 간단 안내로 콜백
                    try:
                        if http_client is not None:
                            payload = {
                                "version": "2.0",
                                "template": {"outputs": [{"simpleText": {"text": "답변 생성이 지연되어 간단히 안내드려요 🙏"}}]}
                            }
                            headers = {"Content-Type": "application/json; charset=utf-8"}
                            await http_client.post(callback_url, json=payload, headers=headers)
                    except Exception:
                        pass
                except Exception as e:
                    logger.bind(x_request_id=request_id).exception(f"Callback flow failed: {e}")

            asyncio.create_task(_handle_callback_full(callback_url, user_id, user_text, x_request_id))

            try:
                update_last_activity(f"temp_{user_id}")
            except Exception:
                pass
            return JSONResponse(content=immediate, media_type="application/json; charset=utf-8")

        # 4) 즉시응답 경로만 DB 작업 수행 (콜백 비활성화거나 콜백 URL 없음)
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

        # 5) 콜백이 아닌 경우: AI 응답 생성 (BUDGET 적용)
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
                final_text, tokens_used = ("잠시만요! 답변 생성이 길어져 간단히 안내드려요 🙏", 0)
            logger.info(f"AI response generated: {final_text[:50]}...")
            
            # 메시지 저장 시도 (DB 장애 등으로 temp일 수 있음)
            try:
                if not str(conv_id).startswith("temp_"):
                    # 기존 방식: conv_id가 유효할 때 바로 저장
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
            final_text = "죄송합니다. 일시적인 오류가 발생했습니다. 잠시 후 다시 시도해주세요."
            
            return JSONResponse(content={
                "version": "2.0",
                "template": {"outputs":[{"simpleText":{"text": final_text}}]}
            }, media_type="application/json; charset=utf-8")
        
    except Exception as e:
        logger.exception(f"Error in skill endpoint: {e}")
        # 카카오 스펙 준수 기본 본문과 함께 200 OK로 내려 400 회피
        safe_text = "일시적인 오류가 발생했어요. 잠시 후 다시 시도해 주세요."
        return JSONResponse(content={
            "version": "2.0",
            "template": {"outputs":[{"simpleText":{"text": safe_text}}]}
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
