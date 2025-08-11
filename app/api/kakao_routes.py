"""카카오 스킬 관련 라우터"""
import asyncio
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_session
from .schemas import KakaoBody, simple_text
from .service import upsert_user, get_or_create_conversation
from .utils import extract_user_id, extract_callback_url
from .ai_service import ai_service
from .background_tasks import _save_user_message_background, _save_ai_response_background

router = APIRouter()


@router.post("/skill")
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


@router.post("/test-skill")
async def test_skill_endpoint(request: Request):
    """디버깅용 테스트 엔드포인트 - 받은 데이터를 그대로 반환"""
    try:
        body = await request.json()
        print(f"TEST SKILL - Received: {body}")
        logger.info(f"TEST SKILL - Received: {body}")
        
        return {"status": "test_success", "received_data": body}
    except Exception as e:
        print(f"TEST SKILL - Error: {e}")
        return {"error": str(e)}


@router.post("/test-callback")
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
