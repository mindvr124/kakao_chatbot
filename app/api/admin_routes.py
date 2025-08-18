"""관리자 API 라우터"""
from typing import List
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database.db import get_session
from app.schemas.schemas import (
    PromptTemplateCreate, PromptTemplateResponse, PromptTemplateUpdate,
    AIProcessingTaskResponse, AIProcessingTaskListResponse, RetryAIProcessingTaskResponse
)
from app.database.service import (
    create_prompt_template, get_prompt_templates, get_prompt_template_by_name, activate_prompt_template
)
from app.core.ai_worker import ai_worker
from app.core.ai_processing_service import ai_processing_service
from app.database.models import AIProcessingStatus
from app.config import settings

router = APIRouter(prefix="/admin")


@router.get("/health")
async def admin_health(session: AsyncSession = Depends(get_session)):
    """관리자용 헬스체크"""
    try:
        # DB 연결 테스트
        prompts = await get_prompt_templates(session, active_only=True)
        
        # OpenAI API 확인
        openai_key_configured = bool(settings.openai_api_key)
        
        # AI 워커 상태 확인
        worker_status = await ai_worker.get_worker_status()
        
        return {
            "status": "healthy",
            "database": "connected",
            "active_prompts": len(prompts),
            "openai_configured": openai_key_configured,
            "ai_model": settings.openai_model,
            "temperature": settings.openai_temperature,
            "ai_worker": worker_status
        }
        
    except Exception as e:
        logger.exception("Admin health check failed")
        raise HTTPException(500, f"Health check failed: {str(e)}")


@router.get("/ai-tasks", response_model=AIProcessingTaskListResponse)
async def list_ai_tasks(
    status: str = None,
    limit: int = 50,
    session: AsyncSession = Depends(get_session)
):
    """AI 처리 작업 목록을 조회합니다"""
    # AIProcessingTask 제거로 빈 목록 반환
    return AIProcessingTaskListResponse(tasks=[], total=0)


@router.post("/ai-tasks/{task_id}/retry", response_model=RetryAIProcessingTaskResponse)
async def retry_ai_task(
    task_id: str,
    session: AsyncSession = Depends(get_session)
):
    """실패한 AI 작업을 재시도합니다."""
    # AIProcessingTask 제거로 재시도 기능 비활성화
    raise HTTPException(410, "AI task queue is disabled")


@router.post("/prompts", response_model=PromptTemplateResponse)
async def create_prompt(
    prompt_data: PromptTemplateCreate,
    session: AsyncSession = Depends(get_session)
):
    """새로운 프롬프트 템플릿을 생성합니다"""
    prompt = await create_prompt_template(
        session=session,
        name=prompt_data.name,
        system_prompt=prompt_data.system_prompt,
        description=prompt_data.description,
        user_prompt_template=prompt_data.user_prompt_template,
        created_by="admin"  # 실제로는 인증된 사용자 정보를 사용
    )
    return prompt


@router.get("/prompts", response_model=List[PromptTemplateResponse])
async def list_prompts(
    active_only: bool = True,
    session: AsyncSession = Depends(get_session)
):
    """프롬프트 템플릿 목록을 조회합니다"""
    prompts = await get_prompt_templates(session, active_only=active_only)
    return prompts


@router.put("/prompts/{name}/activate", response_model=PromptTemplateResponse)
async def activate_prompt(
    name: str,
    session: AsyncSession = Depends(get_session)
):
    """프롬프트 템플릿을 활성화합니다."""
    prompt = await activate_prompt_template(session, name)
    if not prompt:
        raise HTTPException(404, "Prompt template not found")
    return prompt