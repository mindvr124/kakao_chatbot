from typing import Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # pydantic v2 설정
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",          # .env에 없는 값은 무시
    )

    # 데이터베이스
    database_url: str = Field(description="PostgreSQL 데이터베이스 연결 문자열")

    # OpenAI - 속도 최적화 설정
    openai_api_key: Optional[str] = Field(default=None)
    openai_model: str = Field(default="gpt-4o")
    openai_temperature: float = Field(default=0.1)  # 빠른 결정
    openai_max_tokens: int = Field(default=150)  # 기본 답변 길이
    openai_auto_continue: bool = Field(default=True)
    openai_auto_continue_max_segments: int = Field(default=3)
    openai_dynamic_max_tokens: bool = Field(default=True)
    openai_dynamic_max_tokens_cap: int = Field(default=800)

    # 세션/서버
    session_timeout_minutes: int = Field(default=30)
    summary_turn_window: int = Field(default=10)
    port: int = Field(default=8000)
    log_level: str = Field(default="INFO")
    debug: bool = Field(default=False)

# 전역 설정 인스턴스
settings = Settings()
