"""관리자 API ?�우??""
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
    """관리자???�세 ?�스체크"""
    try:
        # DB ?�결 ?�스??
        prompts = await get_prompt_templates(session, active_only=True)
        
        # OpenAI API ???�인
        openai_key_configured = bool(settings.openai_api_key)
        
        # AI ?�커 ?�태 ?�인
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
    """AI 처리 ?�업 목록??조회?�니??"""
    # AIProcessingTask ?�거????�?목록 반환
    return AIProcessingTaskListResponse(tasks=[], total=0)


@router.post("/ai-tasks/{task_id}/retry", response_model=RetryAIProcessingTaskResponse)
async def retry_ai_task(
    task_id: str,
    session: AsyncSession = Depends(get_session)
):
    """?�패??AI ?�업???�시?�합?�다."""
    # AIProcessingTask ?�거?????�시??기능 비활?�화
    raise HTTPException(410, "AI task queue is disabled")


@router.post("/prompts", response_model=PromptTemplateResponse)
async def create_prompt(
    prompt_data: PromptTemplateCreate,
    session: AsyncSession = Depends(get_session)
):
    """?�로???�롬?�트 ?�플릿을 ?�성?�니??"""
    prompt = await create_prompt_template(
        session=session,
        name=prompt_data.name,
        system_prompt=prompt_data.system_prompt,
        description=prompt_data.description,
        user_prompt_template=prompt_data.user_prompt_template,
        created_by="admin"  # ?�제로는 ?�증???�용???�보�??�용
    )
    return prompt


@router.get("/prompts", response_model=List[PromptTemplateResponse])
async def list_prompts(
    active_only: bool = True,
    session: AsyncSession = Depends(get_session)
):
    """?�롬?�트 ?�플�?목록??조회?�니??"""
    prompts = await get_prompt_templates(session, active_only=active_only)
    return prompts


@router.put("/prompts/{name}/activate", response_model=PromptTemplateResponse)
async def activate_prompt(
    name: str,
    session: AsyncSession = Depends(get_session)
):
    """?�롬?�트 ?�플릿을 ?�성?�합?�다."""
    prompt = await activate_prompt_template(session, name)
    if not prompt:
        raise HTTPException(404, "Prompt template not found")
    return prompt
