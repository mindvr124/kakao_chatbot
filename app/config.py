from typing import Optional
from pydantic import Field, AliasChoices
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # pydantic v2 설정
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",          # .env에 정의 안 한 값이 있어도 무시
    )

    # 데이터베이스
    database_url: str = Field(
        default="postgresql+asyncpg://user:pass@localhost:5432/chatdb",
        validation_alias=AliasChoices("DATABASE_URL"),
    )

    # OpenAI
    openai_api_key: Optional[str] = Field(default=None, validation_alias=AliasChoices("OPENAI_API_KEY"))
    openai_model: str = Field(default="gpt-4o", validation_alias=AliasChoices("OPENAI_MODEL"))
    openai_temperature: float = Field(default=0.2, validation_alias=AliasChoices("OPENAI_TEMPERATURE"))
    openai_max_tokens: int = Field(default=1000, validation_alias=AliasChoices("OPENAI_MAX_TOKENS"))

    # 세션/서버
    session_timeout_minutes: int = Field(default=30, validation_alias=AliasChoices("SESSION_TIMEOUT_MINUTES"))
    port: int = Field(default=8000, validation_alias=AliasChoices("PORT"))
    log_level: str = Field(default="INFO", validation_alias=AliasChoices("LOG_LEVEL"))
    debug: bool = Field(default=False, validation_alias=AliasChoices("DEBUG"))

# 전역 설정 인스턴스
settings = Settings()
