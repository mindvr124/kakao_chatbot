"""메인 FastAPI 애플리케이션"""
import sys
import time
import asyncio
import httpx
from fastapi import FastAPI
from loguru import logger
import logging

# SQLAlchemy 로깅 레벨 설정 (쿼리문 안 뜨게)
logging.getLogger('sqlalchemy.engine').setLevel(logging.WARNING)
logging.getLogger('sqlalchemy.pool').setLevel(logging.WARNING)
logging.getLogger('sqlalchemy.dialects').setLevel(logging.WARNING)
logging.getLogger('sqlalchemy.orm').setLevel(logging.WARNING)

# 로거 설정
logger.remove()  # 기본 핸들러 제거
logger.add(sys.stdout, level="INFO", format="{time} | {level} | {message}")

# 요청 시간 계산 (카카오 블록은 5초 제한, 이전 마진) 및 전역 HTTPX 클라이언트 선언
BUDGET: float = 4.0
ENABLE_CALLBACK: bool = True
http_client: httpx.AsyncClient | None = None

from app.database.db import init_db, close_db, get_session
from app.core.ai_worker import ai_worker
from app.database.service import create_prompt_template, get_prompt_template_by_name
from app.core.background_tasks import ensure_watcher_started

app = FastAPI(title="Kakao AI Chatbot (FastAPI)")

@app.on_event("startup")
async def on_startup():
    # 전역 HTTP 클라이언트는 DB 성공/실패와 무관하게 먼저 준비
    global http_client
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=2.0, read=3.0, write=3.0, pool=5.0),
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
    )

    # DB가 실패해도 서버는 뜨게
    try:
        await init_db()
        logger.info("DB initialized.")

        # 기본 프롬프트 템플릿 생성 (없을 경우)
        try:
            async for session in get_session():
                existing_prompt = await get_prompt_template_by_name(session, "default")
                if not existing_prompt:
                    await create_prompt_template(
                        session=session,
                        name="default",
                        system_prompt="""당신은 전문 AI 심리상담가입니다. 
                        다음 규칙을 따라 답변해주세요:

                        1. 친근하고 공감적인 말로 대화하세요
                        2. 사용자의 질문에 정확하고 도움이 되는 답변을 제공하세요
                        3. 모르는 내용은 솔직히 모른다고 하고, 추가 질문을 제안하세요
                        4. 답변은 간결하면서도 충분한 정보를 포함하세요
                        5. 한국어로 자연스럽게 대화하세요
                        6. 지금까지의 대화 내용을 이전 요약을 먼저 참고하여 맥락에 맞게 대화를 이어가세요""",
                        description="기본 상담사 프롬프트",
                        created_by="system"
                    )
                    logger.info("Default prompt template created")
                break
        except Exception as e:
            logger.warning(f"Failed to create default prompt template: {e}")
    except Exception as e:
        logger.error(f"Failed to initialize DB: {e}")
        logger.info("Server will continue without database connection")

    # AI 워커 시작 (DB와 무관)
    await ai_worker.start()
    logger.info("AI Worker started.")
    # 세션 비활성 와처 시작
    try:
        await ensure_watcher_started()
    except Exception as e:
        logger.warning(f"Failed to start session watcher: {e}")


@app.on_event("shutdown")
async def on_shutdown():
    # AI 워커 중지
    await ai_worker.stop()
    logger.info("AI Worker stopped.")

    # 전역 HTTP 클라이언트 종료
    global http_client
    if http_client:
        await http_client.aclose()

    # 데이터베이스 연결 종료
    try:
        await close_db()
        logger.info("Database connections closed.")
    except Exception as e:
        logger.error(f"Error during shutdown: {e}")


# 라우터 import 및 등록 (이벤트 핸들러 선언 뒤에 등록해도 무방)
from app.api.kakao_routes import router as kakao_router
from app.api.admin_routes import router as admin_router
from app.api.user_routes import router as user_router

app.include_router(kakao_router)
app.include_router(admin_router)
app.include_router(user_router)


@app.get("/health")
async def health():
    """기본 헬스체크"""
    return {"ok": True}


@app.get("/")
async def root():
    return {"ok": True, "service": "kakao_chatbot"}


@app.post("/")
async def root_post():
    """루트 경로 POST 요청 처리 (잘못 된 엔드포인트)"""
    return {"error": "Please use /skill endpoint for Kakao chatbot requests"}
