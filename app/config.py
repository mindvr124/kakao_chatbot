from typing import Optional
from pydantic import Field, AliasChoices
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # pydantic v2 ?�정
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",          # .env???�의 ????값이 ?�어??무시
    )

    # ?�이?�베?�스
    database_url: str = Field(
        default="postgresql+asyncpg://aicounselor_user:your_secure_password_here@223.130.146.105:5432/aicounselor",
        validation_alias=AliasChoices("DATABASE_URL"),
    )

    # OpenAI - ?�도 최적???�정
    openai_api_key: Optional[str] = Field(default=None, validation_alias=AliasChoices("OPENAI_API_KEY"))
    openai_model: str = Field(default="gpt-4o-mini", validation_alias=AliasChoices("OPENAI_MODEL"))  # ??빠른 모델
    openai_temperature: float = Field(default=0.1, validation_alias=AliasChoices("OPENAI_TEMPERATURE"))  # ??빠른 결정
    openai_max_tokens: int = Field(default=150, validation_alias=AliasChoices("OPENAI_MAX_TOKENS"))  # 기본 ?�답 길이
    openai_auto_continue: bool = Field(default=True, validation_alias=AliasChoices("OPENAI_AUTO_CONTINUE"))
    openai_auto_continue_max_segments: int = Field(default=3, validation_alias=AliasChoices("OPENAI_AUTO_CONTINUE_MAX_SEGMENTS"))
    openai_dynamic_max_tokens: bool = Field(default=True, validation_alias=AliasChoices("OPENAI_DYNAMIC_MAX_TOKENS"))
    openai_dynamic_max_tokens_cap: int = Field(default=800, validation_alias=AliasChoices("OPENAI_DYNAMIC_MAX_TOKENS_CAP"))

    # ?�션/?�버
    session_timeout_minutes: int = Field(default=30, validation_alias=AliasChoices("SESSION_TIMEOUT_MINUTES"))
    summary_turn_window: int = Field(default=10, validation_alias=AliasChoices("SUMMARY_TURN_WINDOW"))
    port: int = Field(default=8000, validation_alias=AliasChoices("PORT"))
    log_level: str = Field(default="INFO", validation_alias=AliasChoices("LOG_LEVEL"))
    debug: bool = Field(default=False, validation_alias=AliasChoices("DEBUG"))

# ?�역 ?�정 ?�스?�스
settings = Settings()
