"""ê´€ë¦¬ì API ?¼ìš°??""
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
    """ê´€ë¦¬ì???ì„¸ ?¬ìŠ¤ì²´í¬"""
    try:
        # DB ?°ê²° ?ŒìŠ¤??
        prompts = await get_prompt_templates(session, active_only=True)
        
        # OpenAI API ???•ì¸
        openai_key_configured = bool(settings.openai_api_key)
        
        # AI ?Œì»¤ ?íƒœ ?•ì¸
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
    """AI ì²˜ë¦¬ ?‘ì—… ëª©ë¡??ì¡°íšŒ?©ë‹ˆ??"""
    # AIProcessingTask ?œê±°????ë¹?ëª©ë¡ ë°˜í™˜
    return AIProcessingTaskListResponse(tasks=[], total=0)


@router.post("/ai-tasks/{task_id}/retry", response_model=RetryAIProcessingTaskResponse)
async def retry_ai_task(
    task_id: str,
    session: AsyncSession = Depends(get_session)
):
    """?¤íŒ¨??AI ?‘ì—…???¬ì‹œ?„í•©?ˆë‹¤."""
    # AIProcessingTask ?œê±°?????¬ì‹œ??ê¸°ëŠ¥ ë¹„í™œ?±í™”
    raise HTTPException(410, "AI task queue is disabled")


@router.post("/prompts", response_model=PromptTemplateResponse)
async def create_prompt(
    prompt_data: PromptTemplateCreate,
    session: AsyncSession = Depends(get_session)
):
    """?ˆë¡œ???„ë¡¬?„íŠ¸ ?œí”Œë¦¿ì„ ?ì„±?©ë‹ˆ??"""
    prompt = await create_prompt_template(
        session=session,
        name=prompt_data.name,
        system_prompt=prompt_data.system_prompt,
        description=prompt_data.description,
        user_prompt_template=prompt_data.user_prompt_template,
        created_by="admin"  # ?¤ì œë¡œëŠ” ?¸ì¦???¬ìš©???•ë³´ë¥??¬ìš©
    )
    return prompt


@router.get("/prompts", response_model=List[PromptTemplateResponse])
async def list_prompts(
    active_only: bool = True,
    session: AsyncSession = Depends(get_session)
):
    """?„ë¡¬?„íŠ¸ ?œí”Œë¦?ëª©ë¡??ì¡°íšŒ?©ë‹ˆ??"""
    prompts = await get_prompt_templates(session, active_only=active_only)
    return prompts


@router.put("/prompts/{name}/activate", response_model=PromptTemplateResponse)
async def activate_prompt(
    name: str,
    session: AsyncSession = Depends(get_session)
):
    """?„ë¡¬?„íŠ¸ ?œí”Œë¦¿ì„ ?œì„±?”í•©?ˆë‹¤."""
    prompt = await activate_prompt_template(session, name)
    if not prompt:
        raise HTTPException(404, "Prompt template not found")
    return prompt
