"""백그라운드 작업 함수들"""
import asyncio
import requests
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from .db import get_session
from .service import save_message
from .ai_processing_service import ai_processing_service


async def _save_user_message_background(conv_id: str, user_text: str, request_id: str | None):
    """백그라운드에서 사용자 메시지를 DB에 저장합니다."""
    try:
        logger.bind(x_request_id=request_id).info(f"Saving user message to DB in background")
        
        # 새로운 세션으로 DB 저장
        async for session in get_session():
            await save_message(
                session=session, 
                conv_id=conv_id, 
                role="user", 
                content=user_text, 
                request_id=request_id
            )
            logger.bind(x_request_id=request_id).info(f"User message saved successfully")
            break
            
    except Exception as e:
        logger.bind(x_request_id=request_id).exception(f"Failed to save user message in background: {e}")


async def _save_ai_response_background(conv_id: str, final_text: str, tokens_used: int, request_id: str | None):
    """백그라운드에서 AI 응답을 DB에 저장합니다."""
    try:
        logger.bind(x_request_id=request_id).info(f"Saving AI response to DB in background")
        
        # 새로운 세션으로 DB 저장
        async for session in get_session():
            await save_message(
                session=session, 
                conv_id=conv_id, 
                role="assistant", 
                content=final_text, 
                request_id=request_id,
                tokens=tokens_used
            )
            logger.bind(x_request_id=request_id).info(f"AI response saved successfully")
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
        response = requests.post(callback_url, json=callback_data, timeout=10)
        print(f"Callback sent: {response.status_code}")
        logger.info(f"Callback sent successfully: status={response.status_code}")
        return response.status_code == 200
    except Exception as e:
        print(f"Callback failed: {e}")
        logger.error(f"Failed to send callback to {callback_url}: {e}")
        return False


async def _send_callback_response(callback_url: str, message: str, tokens: int, request_id: str | None):
    """콜백 URL로 응답을 전송합니다. (기존 비동기 방식 유지)"""
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
    """콜백을 통해 AI 처리를 수행하고 결과를 전송합니다."""
    try:
        logger.bind(x_request_id=request_id).info(f"Starting AI processing with callback for task: {task_id}")
        
        # 새로운 세션으로 AI 처리
        async for session in get_session():
            success, result, tokens = await ai_processing_service.process_ai_task(
                session, task_id, "default"
            )
            
            if success:
                logger.bind(x_request_id=request_id).info(f"AI processing completed for task: {task_id}, sending callback")
                
                # 콜백으로 최종 응답 전송
                await _send_callback_response(callback_url, result, tokens, request_id)
            else:
                logger.bind(x_request_id=request_id).error(f"AI processing failed for task: {task_id}: {result}")
                
                # 실패 시에도 콜백으로 에러 메시지 전송
                error_message = "죄송합니다. AI 응답 생성에 실패했습니다. 잠시 후 다시 시도해주세요."
                await _send_callback_response(callback_url, error_message, 0, request_id)
            break
            
    except Exception as e:
        logger.bind(x_request_id=request_id).exception(f"AI processing error for task {task_id}: {e}")
        
        # 예외 발생 시에도 콜백으로 에러 메시지 전송
        try:
            error_message = "죄송합니다. 일시적인 오류가 발생했습니다. 잠시 후 다시 시도해주세요."
            await _send_callback_response(callback_url, error_message, 0, request_id)
        except Exception as callback_error:
            logger.bind(x_request_id=request_id).exception(f"Failed to send error callback: {callback_error}")


async def _process_ai_background(task_id: str, request_id: str | None):
    """백그라운드에서 AI 처리를 수행합니다."""
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
