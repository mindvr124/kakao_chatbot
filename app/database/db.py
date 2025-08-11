from sqlmodel import SQLModel
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import NullPool
from .config import settings
import logging

# 로깅 설정
logging.getLogger('sqlalchemy.engine').setLevel(logging.WARNING)
logging.getLogger('sqlalchemy.pool').setLevel(logging.WARNING)

# 데이터베이스 엔진 생성 - 연결 풀 설정 개선
engine: AsyncEngine = create_async_engine(
    settings.database_url,
    #echo=settings.debug,
    pool_pre_ping=True,
    pool_size=10,  # 기본 연결 풀 크기
    max_overflow=20,  # 추가 연결 허용
    pool_timeout=30,  # 연결 대기 시간 (초)
    pool_recycle=3600,  # 연결 재생성 시간 (1시간)
    pool_reset_on_return='commit',  # 세션 반환 시 커밋으로 리셋
)

AsyncSessionLocal = sessionmaker(
    engine, 
    expire_on_commit=False, 
    class_=AsyncSession,
    autocommit=False,
    autoflush=False
)

async def init_db():
    """데이터베이스 초기화"""
    try:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        logging.info("Database initialized successfully")
    except Exception as e:
        logging.error(f"Database initialization failed: {e}")
        raise

async def get_session() -> AsyncSession:
    """데이터베이스 세션 생성 - 컨텍스트 매니저로 안전하게 관리"""
    session = AsyncSessionLocal()
    try:
        yield session
    except Exception as e:
        await session.rollback()
        logging.error(f"Session error: {e}")
        raise
    finally:
        await session.close()

async def close_db():
    """데이터베이스 연결 종료"""
    await engine.dispose()
    logging.info("Database connections closed")
