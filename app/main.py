"""메인 FastAPI 애플리케이션"""
import sys
from fastapi import FastAPI
from loguru import logger

# 로거 설정
logger.remove()  # 기본 핸들러 제거
logger.add(sys.stdout, level="INFO", format="{time} | {level} | {message}")

from .db import init_db, close_db
from .ai_worker import ai_worker
from .service import create_prompt_template, get_prompt_template_by_name
from .db import get_session

# 라우터 import
from .kakao_routes import router as kakao_router
from .admin_routes import router as admin_router
from .user_routes import router as user_router

app = FastAPI(title="Kakao AI Chatbot (FastAPI)")

# 라우터 등록
app.include_router(kakao_router)
app.include_router(admin_router)
app.include_router(user_router)


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
