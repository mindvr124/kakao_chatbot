from sqlmodel import SQLModel
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import NullPool
from app.config import settings
import logging

# 로깅 설정
logging.getLogger('sqlalchemy.engine').setLevel(logging.WARNING)
logging.getLogger('sqlalchemy.pool').setLevel(logging.WARNING)

# PostgreSQL 데이터베이스 엔진 생성
def create_database_engine():
    """PostgreSQL 데이터베이스 엔진을 생성합니다."""
    try:
        engine = create_async_engine(
            settings.database_url,
            echo=settings.debug,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=20,
            pool_timeout=30,
            pool_recycle=3600,
            pool_reset_on_return='commit',
            connect_args={"server_settings": {"timezone": "Asia/Seoul"}}
        )
        logging.info("PostgreSQL database engine created successfully")
        return engine
    except Exception as e:
        logging.error(f"Failed to create PostgreSQL database engine: {e}")
        logging.error(f"Database URL: {settings.database_url}")
        raise

# 엔진 생성 시도
try:
    engine: AsyncEngine = create_database_engine()
except Exception as e:
    logging.error(f"PostgreSQL database engine creation failed: {e}")
    engine = None

AsyncSessionLocal = sessionmaker(
    engine, 
    expire_on_commit=False, 
    class_=AsyncSession,
    autocommit=False,
    autoflush=False
)

async def init_db():
    """데이터베이스 초기화"""
    if engine is None:
        logging.error("Cannot initialize database: engine is not available")
        return False
    
    try:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        logging.info("Database initialized successfully")
        return True
    except Exception as e:
        logging.error(f"Database initialization failed: {e}")
        return False

async def get_session() -> AsyncSession:
    """데이터베이스 세션 생성 - 컨텍스트 매니저로 안전하게 관리"""
    if engine is None:
        raise RuntimeError("Database engine is not available. Please check your database configuration.")
    
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
