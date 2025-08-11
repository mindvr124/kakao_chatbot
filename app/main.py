import os
import asyncio
from typing import List
from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import JSONResponse
from loguru import logger
import sys

# 로거 설정 강화 (Render에서 보이도록)
logger.remove()  # 기본 핸들러 제거
logger.add(sys.stdout, level="INFO", format="{time} | {level} | {message}")
from sqlmodel import SQLModel
from sqlalchemy.ext.asyncio import AsyncSession
import httpx
import requests

from .db import init_db, get_session, close_db
from .schemas import (
    KakaoBody, simple_text, PromptTemplateCreate, PromptTemplateResponse, PromptTemplateUpdate,
    card_response, quick_reply_response, callback_waiting_response,
    AIProcessingTaskResponse, AIProcessingStatusResponse, AIProcessingTaskListResponse, RetryAIProcessingTaskResponse
)
from .service import (
    upsert_user, get_or_create_conversation, save_message,
    create_prompt_template, get_prompt_templates, get_prompt_template_by_name, activate_prompt_template
)
from .utils import extract_user_id, extract_callback_url
from .ai_service import ai_service
from .ai_processing_service import ai_processing_service
from .ai_worker import ai_worker
from sqlalchemy import select

app = FastAPI(title="Kakao AI Chatbot (FastAPI)")

@app.on_event("startup")
async def on_startup():
    await init_db()
    logger.info("DB initialized.")
    
    # AI 워커 시작
    await ai_worker.start()
    logger.info("AI Worker started.")
    
    # 기본 프롬프트 템플릿 생성 (없을 경우)
    async for session in get_session():
        existing_prompt = await get_prompt_template_by_name(session, "default")
        if not existing_prompt:
            await create_prompt_template(
                session=session,
                name="default",
                system_prompt="""당신은 카카오 비즈니스 AI 상담사입니다. 
다음 원칙을 따라 응답해주세요:

1. 친근하고 전문적인 톤으로 대화하세요
2. 사용자의 질문에 정확하고 도움이 되는 답변을 제공하세요  
3. 모르는 내용은 솔직히 모른다고 하고, 추가 도움을 제안하세요
4. 답변은 간결하면서도 충분한 정보를 포함하세요
5. 한국어로 자연스럽게 대화하세요""",
                description="기본 상담봇 프롬프트",
                created_by="system"
            )
            logger.info("Default prompt template created")
        break

@app.on_event("shutdown")
async def on_shutdown():
    # AI 워커 중지
    await ai_worker.stop()
    logger.info("AI Worker stopped.")
    
    # 데이터베이스 연결 종료
    await close_db()
    logger.info("Database connections closed.")

@app.get("/health")
async def health():
    """기본 헬스체크"""
    return {"ok": True}

@app.get("/")
async def root():
    return {"ok": True, "service": "kakao_chatbot"}

@app.post("/test-callback")
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
    
# =============================================================================
# 카카오 스킬 엔드포인트
# =============================================================================

@app.post("/test-skill")
async def test_skill_endpoint(request: Request):
    """디버깅용 테스트 엔드포인트 - 받은 데이터를 그대로 반환"""
    try:
        body = await request.json()
        print(f"TEST ENDPOINT - Received: {body}")
        logger.info(f"TEST ENDPOINT - Received: {body}")
        
        # user_id 추출 테스트
        user_id = body.get("userRequest", {}).get("user", {}).get("id")
        print(f"TEST ENDPOINT - Extracted user_id: {user_id}")
        
        return {
            "received_data": body,
            "extracted_user_id": user_id,
            "data_keys": list(body.keys()) if isinstance(body, dict) else "not_dict"
        }
    except Exception as e:
        print(f"TEST ENDPOINT - Error: {e}")
        return {"error": str(e)}

from fastapi.responses import JSONResponse

@app.post("/skill")
async def skill_endpoint(
    request: Request,
    kakao: KakaoBody,
    session: AsyncSession = Depends(get_session)
):
    # 최우선 로그 - 요청이 들어왔다는 것부터 확인
    print(f"=== SKILL REQUEST RECEIVED ===")
    logger.info("=== SKILL REQUEST RECEIVED ===")
    # 1) 헤더 추적값
    x_request_id = request.headers.get("X-Request-ID") or request.headers.get("X-Request-Id")
    logger.bind(x_request_id=x_request_id).info("Incoming skill request")

    body_dict = kakao.model_dump()
    
    # 디버깅: 받은 데이터 로깅
    logger.bind(x_request_id=x_request_id).info(f"Received body: {body_dict}")
    
    user_id = extract_user_id(body_dict)
    logger.bind(x_request_id=x_request_id).info(f"Extracted user_id: {user_id}")
    
    if not user_id:
        logger.bind(x_request_id=x_request_id).error(f"user_id not found in request. Body structure: {body_dict}")
        raise HTTPException(400, f"user_id not found in request. Received structure: {list(body_dict.keys())}")

    callback_url = extract_callback_url(body_dict)
    logger.bind(x_request_id=x_request_id).info(f"Extracted callback_url: {callback_url}")
    logger.bind(x_request_id=x_request_id).info(f"Full body structure for callback detection: {body_dict}")

    # 2) 유저/대화 upsert
    await upsert_user(session, user_id)
    conv = await get_or_create_conversation(session, user_id)

    # 3) 유저 발화 추출
    user_text = kakao.userRequest.get("utterance", "") if kakao.userRequest else ""
    
    # 사용자 메시지 저장도 백그라운드로 이동 (최대 속도 확보)
    asyncio.create_task(_save_user_message_background(
        conv.conv_id, user_text, x_request_id
    ))
    
    # 4) 즉시 AI 응답 생성 (최대 속도)
    try:
        final_text, tokens_used = await ai_service.generate_response(
            session=session, 
            conv_id=conv.conv_id, 
            user_input=user_text,
            prompt_name="default"
        )
        
        # 먼저 카카오로 응답 전송 (즉시 반환)
        response = JSONResponse(content={
            "version": "2.0",
            "template": {"outputs":[{"simpleText":{"text": final_text}}]}
        })
        
        # 백그라운드에서 AI 응답 저장
        asyncio.create_task(_save_ai_response_background(
            conv.conv_id, final_text, tokens_used, x_request_id
        ))
        
        return response
        
    except Exception as e:
        logger.bind(x_request_id=x_request_id).exception(f"AI generation failed: {e}")
        final_text = "죄송합니다. 일시적인 오류가 발생했습니다. 잠시 후 다시 시도해주세요."
        
        # 에러 응답도 즉시 반환
        return JSONResponse(content={
            "version": "2.0",
            "template": {"outputs":[{"simpleText":{"text": final_text}}]}
        })

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
        payload = {
            "version": "2.0",
            "template": {
                "outputs": [
                    {
                        "simpleText": {
                            "text": message
                        }
                    }
                ]
            }
        }
        
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

async def _handle_callback(callback_url: str, conv_id, user_text: str, x_request_id: str | None, session_maker):
    """
    콜백 유효시간(플랫폼 정책상 매우 짧음) 내에 LLM 호출/비즈니스 로직을 마치고 callbackUrl로 최종 응답 전송.
    세션은 백그라운드 태스크마다 새로 열어야 함(의존성 주입 불가 영역).
    """
    final_text = "죄송합니다. 일시적인 오류가 발생했습니다."
    tokens_used = 0
    
    try:
        # 1) AI 응답 생성
        async for session in session_maker():
            final_text, tokens_used = await ai_service.generate_response(
                session=session, 
                conv_id=conv_id, 
                user_input=user_text,
                prompt_name="default"
            )
            
            # 2) DB에 AI 응답 저장
            await save_message(
                session=session, 
                conv_id=conv_id, 
                role="assistant", 
                content=final_text, 
                request_id=x_request_id,
                tokens=tokens_used
            )
            break
            
    except Exception as e:
        logger.bind(x_request_id=x_request_id).exception(f"AI generation failed: {e}")
        final_text = "죄송합니다. 일시적인 오류가 발생했습니다. 잠시 후 다시 시도해주세요."

    # 3) 카카오 콜백 전송(1회)
    payload = {
        "version": "2.0",
        "template": {"outputs":[{"simpleText":{"text": final_text}}]}
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(callback_url, json=payload)
            logger.bind(x_request_id=x_request_id).info(f"Callback status={resp.status_code}, tokens={tokens_used}")
            resp.raise_for_status()
    except Exception as e:
        logger.bind(x_request_id=x_request_id).exception(f"Callback failed: {e}")

# =============================================================================
# 사용자 API 엔드포인트
# =============================================================================

@app.get("/user/ai-status/{task_id}", response_model=AIProcessingStatusResponse)
async def get_ai_processing_status(
    task_id: str,
    session: AsyncSession = Depends(get_session)
):
    """AI 처리 상태를 조회합니다."""
    try:
        from .models import AIProcessingTask, AIProcessingStatus
        
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
                from .models import Message
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

@app.get("/user/conversation/{conv_id}/latest-ai-response")
async def get_latest_ai_response(
    conv_id: str,
    session: AsyncSession = Depends(get_session)
):
    """대화에서 가장 최근 AI 응답을 조회합니다."""
    try:
        from .models import Message, MessageRole
        
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

# =============================================================================
# 관리자 API 엔드포인트
# =============================================================================

@app.get("/admin/health")
async def admin_health(session: AsyncSession = Depends(get_session)):
    """관리자용 상세 헬스체크"""
    try:
        # DB 연결 테스트
        prompts = await get_prompt_templates(session, active_only=True)
        
        # OpenAI API 키 확인
        from .config import settings
        openai_key_configured = bool(settings.openai_api_key)
        
        # AI 워커 상태 확인
        worker_status = await ai_worker.get_worker_status()
        
        return {
            "status": "healthy",
            "database": "connected",
            "active_prompts": len(prompts),
            "openai_configured": openai_key_configured,
            "ai_model": ai_service.model,
            "temperature": ai_service.temperature,
            "ai_worker": worker_status
        }
    except Exception as e:
        logger.exception("Health check failed")
        return {
            "status": "unhealthy",
            "error": str(e)
        }

@app.get("/admin/ai-tasks", response_model=AIProcessingTaskListResponse)
async def list_ai_tasks(
    status: str = None,
    limit: int = 50,
    session: AsyncSession = Depends(get_session)
):
    """AI 처리 작업 목록을 조회합니다."""
    try:
        from .models import AIProcessingTask, AIProcessingStatus
        
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

@app.post("/admin/ai-tasks/{task_id}/retry", response_model=RetryAIProcessingTaskResponse)
async def retry_ai_task(
    task_id: str,
    session: AsyncSession = Depends(get_session)
):
    """실패한 AI 작업을 재시도합니다."""
    try:
        from .models import AIProcessingTask, AIProcessingStatus
        
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

@app.post("/admin/prompts", response_model=PromptTemplateResponse)
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

@app.get("/admin/prompts", response_model=List[PromptTemplateResponse])
async def list_prompts(
    active_only: bool = True,
    session: AsyncSession = Depends(get_session)
):
    """프롬프트 템플릿 목록을 조회합니다."""
    prompts = await get_prompt_templates(session, active_only=active_only)
    return prompts

@app.get("/admin/prompts/{name}", response_model=PromptTemplateResponse)
async def get_prompt_by_name(
    name: str,
    session: AsyncSession = Depends(get_session)
):
    """이름으로 프롬프트 템플릿을 조회합니다."""
    prompt = await get_prompt_template_by_name(session, name)
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt template not found")
    return prompt

@app.post("/admin/prompts/{prompt_id}/activate")
async def activate_prompt(
    prompt_id: str,
    session: AsyncSession = Depends(get_session)
):
    """특정 프롬프트 템플릿을 활성화합니다."""
    success = await activate_prompt_template(session, prompt_id)
    if not success:
        raise HTTPException(status_code=404, detail="Prompt template not found")
    return {"message": "Prompt template activated successfully"}
