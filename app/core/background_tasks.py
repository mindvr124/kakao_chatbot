"""백그라운드 작업 함수들"""
import asyncio
import requests
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database.db import get_session
from app.database.service import save_message, save_event_log
from app.core.ai_processing_service import ai_processing_service
from loguru import logger
import asyncio
from datetime import datetime
from app.core.ai_service import ai_service
from app.core.summary import (
    maybe_rollup_user_summary,
    generate_summary,
    upsert_user_summary_from_text,
    load_user_full_history,
    get_or_init_user_summary,
)
from app.utils.utils import remove_markdown
from app.database.models import Conversation


async def _save_user_message_background(conv_id: str, user_text: str, request_id: str | None, user_id: str | None = None):
    """백그라운드에서 사용자 메시지를 DB에 저장합니다."""
    try:
        logger.bind(x_request_id=request_id).info(f"Saving user message to DB in background")
        
        # 새로운 세션으로 DB 저장
        async for session in get_session():
            # user_id가 없으면 conv에서 조회 시도
            if user_id is None:
                try:
                    conv = await session.get(Conversation, conv_id)
                    if conv:
                        user_id = conv.user_id
                except Exception:
                    try:
                        await session.rollback()
                        conv = await session.get(Conversation, conv_id)
                        if conv:
                            user_id = conv.user_id
                    except Exception:
                        pass
            try:
                await save_message(
                    session=session, 
                    conv_id=conv_id, 
                    role="user", 
                    content=user_text, 
                    request_id=request_id,
                    tokens=None,
                    user_id=user_id,
                )
                try:
                    await save_event_log(session, "message_saved_user", user_id, conv_id, request_id, {"content_len": len(user_text)})
                except Exception:
                    pass
            except Exception:
                try:
                    await session.rollback()
                    await save_message(
                        session=session, 
                        conv_id=conv_id, 
                        role="user", 
                        content=user_text, 
                        request_id=request_id,
                        tokens=None,
                        user_id=user_id,
                    )
                except Exception:
                    raise
            logger.bind(x_request_id=request_id).info(f"User message saved successfully")
            try:
                update_last_activity(conv_id)
            finally:
                break
            
    except Exception as e:
        logger.bind(x_request_id=request_id).exception(f"Failed to save user message in background: {e}")


async def _save_ai_response_background(conv_id: str, final_text: str, tokens_used: int, request_id: str | None, user_id: str | None = None):
    """백그라운드에서 AI 답변을 DB에 저장합니다."""
    try:
        logger.bind(x_request_id=request_id).info(f"Saving AI response to DB in background")
        
        # 새로운 세션으로 DB 저장
        async for session in get_session():
            # user_id가 없으면 conv에서 조회 시도
            if user_id is None:
                try:
                    conv = await session.get(Conversation, conv_id)
                    if conv:
                        user_id = conv.user_id
                except Exception:
                    try:
                        await session.rollback()
                        conv = await session.get(Conversation, conv_id)
                        if conv:
                            user_id = conv.user_id
                    except Exception:
                        pass
            try:
                await save_message(
                    session=session, 
                    conv_id=conv_id, 
                    role="assistant", 
                    content=remove_markdown(final_text), 
                    request_id=request_id,
                    tokens=tokens_used,
                    user_id=user_id,
                )
                try:
                    await save_event_log(session, "message_saved_assistant", user_id, conv_id, request_id, {"tokens": tokens_used})
                except Exception:
                    pass
            except Exception:
                try:
                    await session.rollback()
                    await save_message(
                        session=session, 
                        conv_id=conv_id, 
                        role="assistant", 
                        content=remove_markdown(final_text), 
                        request_id=request_id,
                        tokens=tokens_used,
                        user_id=user_id,
                    )
                except Exception:
                    raise
            logger.bind(x_request_id=request_id).info(f"AI response saved successfully")
            try:
                # conv_id로 user_id 조회 후 사용자 요약 롤업
                try:
                    from app.database.models import Conversation
                    conv = await session.get(Conversation, conv_id)
                    if conv:
                        await maybe_rollup_user_summary(session, conv.user_id)
                except Exception:
                    try:
                        await session.rollback()
                        conv = await session.get(Conversation, conv_id)
                        if conv:
                            await maybe_rollup_user_summary(session, conv.user_id)
                    except Exception:
                        pass
            finally:
                pass
            try:
                update_last_activity(conv_id)
            finally:
                break
            
    except Exception as e:
        logger.bind(x_request_id=request_id).exception(f"Failed to save AI response in background: {e}")


def send_kakao_callback(callback_url, final_answer):
    """카카오 콜백 전송 (동기 방식)"""
    callback_data = {
        "version": "2.0",
        "template": {
            "outputs": [
                {
                    "simpleText": {
                        "text": final_answer
                    }
                }
            ]
        }
    }
    try:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        # 카카오 콜백은 종종 인증이나 이슈가 있을 수 있어, 리다이렉트 검사 옵션을 명시적으로 지정
        response = requests.post(
            callback_url,
            json=callback_data,
            headers=headers,
            timeout=8,
            allow_redirects=True,
        )
        print(f"Callback sent: {response.status_code}")
        logger.info(f"Callback sent successfully: status={response.status_code}")
        return response.status_code == 200
    except Exception as e:
        print(f"Callback failed: {e}")
        logger.error(f"Failed to send callback to {callback_url}: {e}")
        return False


async def _send_callback_response(callback_url: str, message: str, tokens: int, request_id: str | None):
    """콜백 URL로 답변을 전송합니다 (기존 비동기 방식 유지)"""
    try:
        # 새로운 동기 함수 사용
        import asyncio
        success = await asyncio.get_event_loop().run_in_executor(
            None, send_kakao_callback, callback_url, message
        )
        
        if success:
            logger.bind(x_request_id=request_id).info(f"Callback sent successfully, tokens={tokens}")
        else:
            logger.bind(x_request_id=request_id).error(f"Callback failed")
            
    except Exception as e:
        logger.bind(x_request_id=request_id).exception(f"Failed to send callback to {callback_url}: {e}")


async def _process_ai_with_callback(callback_url: str, task_id: str, request_id: str | None):
    """콜백을 통해 AI 처리를 수행하고 결과를 전송합니다"""
    try:
        logger.bind(x_request_id=request_id).info(f"Starting AI processing with callback for task: {task_id}")
        
        # 새로운 세션으로 AI 처리
        async for session in get_session():
            success, result, tokens = await ai_processing_service.process_ai_task(
                session, task_id, "default"
            )
            
            if success:
                logger.bind(x_request_id=request_id).info(f"AI processing completed for task: {task_id}, sending callback")
                
                # 콜백으로 최종 답변 전송
                await _send_callback_response(callback_url, result, tokens, request_id)
            else:
                logger.bind(x_request_id=request_id).error(f"AI processing failed for task: {task_id}: {result}")
                
                # 실패 시에도 콜백으로 에러 메시지 전송
                error_message = "죄송합니다. AI 답변 생성에 실패했습니다. 다시 한 번 시도해주세요."
                await _send_callback_response(callback_url, error_message, 0, request_id)
            break
            
    except Exception as e:
        logger.bind(x_request_id=request_id).exception(f"AI processing error for task {task_id}: {e}")
        
        # 예외 발생 시에도 콜백으로 에러 메시지 전송
        try:
            error_message = "죄송합니다. 일시적인 오류가 발생했습니다. 다시 한 번 시도해주세요."
            await _send_callback_response(callback_url, error_message, 0, request_id)
        except Exception as callback_error:
            logger.bind(x_request_id=request_id).exception(f"Failed to send error callback: {callback_error}")


async def _process_ai_background(task_id: str, request_id: str | None):
    """백그라운드에서 AI 처리를 수행합니다"""
    try:
        logger.bind(x_request_id=request_id).info(f"Starting background AI processing for task: {task_id}")
        
        # 새로운 세션으로 AI 처리
        async for session in get_session():
            success, result, tokens = await ai_processing_service.process_ai_task(
                session, task_id, "default"
            )
            
            if success:
                logger.bind(x_request_id=request_id).info(f"Background AI processing completed for task: {task_id}")
            else:
                logger.bind(x_request_id=request_id).error(f"Background AI processing failed for task: {task_id}: {result}")
            break
            
    except Exception as e:
        logger.bind(x_request_id=request_id).exception(f"Background AI processing error for task {task_id}: {e}")

# --- 세션 비활성 감시 및 요약 처리 ---
_last_activity_map: dict[str, datetime] = {}
_watcher_task = None
_inactivity_seconds = 120  # 2분

def update_last_activity(conv_id: str | None):
    """유효한 UUID conv_id를 기록. temp_* 접두어는 무시"""
    if not conv_id:
        return
    try:
        from uuid import UUID
        _ = conv_id if isinstance(conv_id, UUID) else UUID(str(conv_id))
    except Exception:
        return
    _last_activity_map[str(conv_id)] = datetime.now()

async def _watch_sessions_loop():
    global _watcher_task
    try:
        while True:
            now = datetime.now()
            stale = [cid for cid, ts in list(_last_activity_map.items()) if (now - ts).total_seconds() > _inactivity_seconds]
            for conv_id in stale:
                try:
                    await _summarize_and_close(conv_id)
                except Exception as e:
                    logger.warning(f"Session summarize failed for {conv_id}: {e}")
                finally:
                    _last_activity_map.pop(conv_id, None)
            await asyncio.sleep(5)
    except asyncio.CancelledError:
        return

async def _summarize_and_close(conv_id: str):
    async for session in get_session():
        try:
            # UUID 캐스팅 보장
            try:
                from uuid import UUID
                conv_uuid = UUID(str(conv_id))
            except Exception:
                return
            try:
                conv: Conversation | None = await session.get(Conversation, conv_uuid)
            except Exception:
                try:
                    await session.rollback()
                    conv = await session.get(Conversation, conv_uuid)
                except Exception as e:
                    logger.warning(f"_summarize_and_close get(Conversation) failed after rollback: {e}")
                    return
            if not conv:
                return
            user_id = conv.user_id
            # 전체 히스토리(user_id 기준) 기반 요약 생성 및 UserSummary에 반영
            try:
                prev_summary = ""
                try:
                    us = await get_or_init_user_summary(session, user_id)
                    prev_summary = us.summary or ""
                except Exception:
                    try:
                        await session.rollback()
                        us = await get_or_init_user_summary(session, user_id)
                        prev_summary = us.summary or ""
                    except Exception:
                        prev_summary = ""
                history_text = await load_user_full_history(session, user_id)
                resp = await generate_summary(ai_service.client, history_text, prev_summary)
                summary_text = resp.content or prev_summary
                try:
                    await upsert_user_summary_from_text(session, user_id, summary_text)
                    try:
                        await save_event_log(session, "summary_saved", user_id, conv_uuid, None, {"len": len(summary_text or "")})
                    except Exception:
                        pass
                except Exception as err:
                    try:
                        await session.rollback()
                        await upsert_user_summary_from_text(session, user_id, summary_text)
                        try:
                            await save_event_log(session, "summary_saved", user_id, conv_uuid, None, {"len": len(summary_text or ""), "after_rollback": True})
                        except Exception:
                            pass
                    except Exception:
                        try:
                            await save_event_log(session, "summary_failed", user_id, conv_uuid, None, {"error": str(err)[:300]})
                        except Exception:
                            pass
                        raise
                logger.info(f"요약 저장 완료 (user_id={user_id}, conv_id={conv_uuid})")
            except Exception as e:
                logger.warning(f"2분 요약 저장 실패: {e}")
                try:
                    await save_event_log(session, "summary_failed", user_id, conv_uuid, None, {"error": str(e)[:300]})
                except Exception:
                    pass
            # 10개 롤업도 병행 시도 (중복 시 최신것 사용)
            try:
                try:
                    await maybe_rollup_user_summary(session, user_id, conv_uuid)
                except Exception:
                    try:
                        await session.rollback()
                        await maybe_rollup_user_summary(session, user_id, conv_uuid)
                    except Exception:
                        pass
            except Exception:
                pass
        finally:
            break

async def ensure_watcher_started():
    global _watcher_task
    if _watcher_task is None or _watcher_task.done():
        _watcher_task = asyncio.create_task(_watch_sessions_loop())
        logger.info("Session watcher started")
