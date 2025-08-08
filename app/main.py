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

from .db import init_db, get_session
from .schemas import (
    KakaoBody, simple_text, PromptTemplateCreate, PromptTemplateResponse, PromptTemplateUpdate,
    card_response, quick_reply_response, callback_waiting_response
)
from .service import (
    upsert_user, get_or_create_conversation, save_message,
    create_prompt_template, get_prompt_templates, get_prompt_template_by_name, activate_prompt_template
)
from .utils import extract_user_id, extract_callback_url
from .ai_service import ai_service

app = FastAPI(title="Kakao AI Chatbot (FastAPI)")

@app.on_event("startup")
async def on_startup():
    await init_db()
    logger.info("DB initialized.")
    
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

@app.get("/health")
async def health():
    return {"ok": True}

@app.post("/test-skill")
async def test_skill_endpoint(request: Request):
    """테스트용 엔드포인트 - 받은 데이터를 그대로 반환"""
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
    # 콜백 완전 비활성화 (관리자센터에서 콜백 OFF 운용)
    callback_url = None

    # 2) 유저/대화 upsert
    await upsert_user(session, user_id)
    conv = await get_or_create_conversation(session, user_id)

    # 3) 유저 발화 저장
    user_text = kakao.userRequest.get("utterance", "") if kakao.userRequest else ""
    await save_message(session, conv.conv_id, role="user", content=user_text, request_id=x_request_id)

    # 4) 콜백 여부에 따른 응답 분기
    if callback_url:
        # 콜백이 있는 경우: 즉시 콜백 대기 응답 + 비동기 처리
        asyncio.create_task(_handle_callback(callback_url, conv.conv_id, user_text, x_request_id, session_maker=get_session))
        
        # 콜백 대기 응답
        immediate = callback_waiting_response("🤖 AI가 답변을 생성하고 있어요!\n잠시만 기다려 주세요...")
        return JSONResponse(content=immediate)
        
    else:
        # 콜백이 없는 경우: 즉시 AI 응답 생성 후 반환
        try:
            final_text, tokens_used = await ai_service.generate_response(
                session=session, 
                conv_id=conv.conv_id, 
                user_input=user_text,
                prompt_name="default"
            )
            
            # AI 응답 저장
            await save_message(
                session=session, 
                conv_id=conv.conv_id, 
                role="assistant", 
                content=final_text, 
                request_id=x_request_id,
                tokens=tokens_used
            )
            
        except Exception as e:
            logger.bind(x_request_id=x_request_id).exception(f"AI generation failed: {e}")
            final_text = "죄송합니다. 일시적인 오류가 발생했습니다. 잠시 후 다시 시도해주세요."
            
        # 일반 템플릿 응답 반환
        return JSONResponse(content={
            "version": "2.0",
            "template": {"outputs":[{"simpleText":{"text": final_text}}]}
        })

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

# 프롬프트 관리 API 엔드포인트들
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

@app.get("/admin/health")
async def admin_health(session: AsyncSession = Depends(get_session)):
    """관리자용 상세 헬스체크"""
    try:
        # DB 연결 테스트
        prompts = await get_prompt_templates(session, active_only=True)
        
        # OpenAI API 키 확인
        from .config import settings
        openai_key_configured = bool(settings.openai_api_key)
        
        return {
            "status": "healthy",
            "database": "connected",
            "active_prompts": len(prompts),
            "openai_configured": openai_key_configured,
            "ai_model": ai_service.model,
            "temperature": ai_service.temperature
        }
    except Exception as e:
        logger.exception("Health check failed")
        return {
            "status": "unhealthy",
            "error": str(e)
        }
