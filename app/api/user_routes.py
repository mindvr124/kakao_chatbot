"""사용자 API 라우터"""
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database.db import get_session
from app.schemas.schemas import AIProcessingStatusResponse
from app.core.ai_processing_service import ai_processing_service
from app.database.models import AIProcessingTask, AIProcessingStatus, Message, MessageRole

router = APIRouter(prefix="/user")


@router.get("/ai-status/{task_id}")
async def get_ai_processing_status(
    task_id: str,
    session: AsyncSession = Depends(get_session)
):
    """AI 처리 상태를 조회합니다."""
    try:
        # 작업 상태 조회
        task = await ai_processing_service.get_task_status(session, task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        
        # 응답 데이터 구성
        response_data = {
            "task_id": task.task_id,
            "status": task.status,
            "created_at": task.created_at,
            "retry_count": task.retry_count
        }
        
        # 상태별 추가 정보
        if task.status == AIProcessingStatus.COMPLETED:
            # 완료된 경우 AI 응답 메시지 조회
            if task.result_message_id:
                stmt = select(Message).where(Message.msg_id == task.result_message_id)
                result = await session.execute(stmt)
                message = result.scalar_one_or_none()
                if message:
                    response_data["ai_response"] = message.content
                    response_data["tokens_used"] = message.tokens
                    response_data["completed_at"] = task.completed_at
        
        elif task.status == AIProcessingStatus.FAILED:
            response_data["error_message"] = task.error_message
            response_data["completed_at"] = task.completed_at
        
        elif task.status == AIProcessingStatus.PROCESSING:
            response_data["started_at"] = task.started_at
        
        return AIProcessingStatusResponse(**response_data)
        
    except Exception as e:
        logger.exception(f"Failed to get AI processing status for task {task_id}")
        raise HTTPException(500, f"Failed to get status: {str(e)}")


@router.get("/conversation/{conv_id}/latest-ai-response")
async def get_latest_ai_response(
    conv_id: str,
    session: AsyncSession = Depends(get_session)
):
    """대화에서 가장 최근 AI 응답을 조회합니다."""
    try:
        # 가장 최근 AI 응답 조회
        stmt = (
            select(Message)
            .where(Message.conv_id == conv_id)
            .where(Message.role == MessageRole.ASSISTANT)
            .order_by(Message.created_at.desc())
            .limit(1)
        )
        
        result = await session.execute(stmt)
        message = result.scalar_one_or_none()
        
        if not message:
            return {"message": "No AI response found for this conversation"}
        
        return {
            "message_id": str(message.msg_id),
            "content": message.content,
            "created_at": message.created_at.isoformat(),
            "tokens": message.tokens
        }
        
    except Exception as e:
        logger.exception(f"Failed to get latest AI response for conversation {conv_id}")
        raise HTTPException(500, f"Failed to get response: {str(e)}")
