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
from app.database.models import AIProcessingTask, AIProcessingStatus
from app.config import settings

router = APIRouter(prefix="/admin")


@router.get("/health")
async def admin_health(session: AsyncSession = Depends(get_session)):
    """관리자용 상세 헬스체크"""
    try:
        # DB 연결 테스트
        prompts = await get_prompt_templates(session, active_only=True)
        
        # OpenAI API 키 확인
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
    """AI 처리 작업 목록을 조회합니다."""
    try:
        stmt = select(AIProcessingTask)
        if status:
            try:
                status_enum = AIProcessingStatus(status)
                stmt = stmt.where(AIProcessingTask.status == status_enum)
            except ValueError:
                raise HTTPException(400, f"Invalid status: {status}")
        
        stmt = stmt.order_by(AIProcessingTask.created_at.desc()).limit(limit)
        result = await session.execute(stmt)
        tasks = result.scalars().all()
        
        return AIProcessingTaskListResponse(
            tasks=[
                AIProcessingTaskResponse(
                    task_id=task.task_id,
                    conv_id=task.conv_id,
                    status=task.status,
                    user_input=task.user_input[:100] + "..." if len(task.user_input) > 100 else task.user_input,
                    retry_count=task.retry_count,
                    created_at=task.created_at,
                    started_at=task.started_at,
                    completed_at=task.completed_at,
                    error_message=task.error_message,
                    result_message_id=task.result_message_id
                )
                for task in tasks
            ],
            total=len(tasks)
        )
        
    except Exception as e:
        logger.exception("Failed to list AI tasks")
        raise HTTPException(500, f"Failed to list AI tasks: {str(e)}")


@router.post("/ai-tasks/{task_id}/retry", response_model=RetryAIProcessingTaskResponse)
async def retry_ai_task(
    task_id: str,
    session: AsyncSession = Depends(get_session)
):
    """실패한 AI 작업을 재시도합니다."""
    try:
        # 작업 조회
        stmt = select(AIProcessingTask).where(AIProcessingTask.task_id == task_id)
        result = await session.execute(stmt)
        task = result.scalar_one_or_none()
        
        if not task:
            raise HTTPException(404, "Task not found")
        
        if task.status != AIProcessingStatus.FAILED:
            raise HTTPException(400, "Only failed tasks can be retried")
        
        # 재시도 설정
        task.status = AIProcessingStatus.PENDING
        task.retry_count = 0
        task.error_message = None
        task.started_at = None
        task.completed_at = None
        
        await session.commit()
        
        logger.info(f"Retrying AI task: {task_id}")
        return RetryAIProcessingTaskResponse(message="Task queued for retry", task_id=task.task_id)
        
    except Exception as e:
        logger.exception(f"Failed to retry AI task {task_id}")
        raise HTTPException(500, f"Failed to retry task: {str(e)}")


@router.post("/prompts", response_model=PromptTemplateResponse)
async def create_prompt(
    prompt_data: PromptTemplateCreate,
    session: AsyncSession = Depends(get_session)
):
    """새로운 프롬프트 템플릿을 생성합니다."""
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
    """프롬프트 템플릿 목록을 조회합니다."""
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
