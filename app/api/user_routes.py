"""사용자 API 라우터"""
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database.db import get_session
from app.schemas.schemas import AIProcessingStatusResponse
from app.core.ai_processing_service import ai_processing_service
from app.database.models import AIProcessingStatus, Message, MessageRole

router = APIRouter(prefix="/user")


@router.get("/ai-status/{task_id}")
async def get_ai_processing_status(
    task_id: str,
    session: AsyncSession = Depends(get_session)
):
    """AI 처리 상태를 조회합니다."""
    # AIProcessingTask 제거됨 → 고정 응답
    return AIProcessingStatusResponse(task_id=task_id, status="disabled", created_at=None, retry_count=0)


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
