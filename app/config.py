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

    # OpenAI 설정
    openai_api_key: Optional[str] = Field(default=None)
    openai_model: str = Field(default="gpt-4o")
    openai_temperature: float = Field(default=0.1, description="AI 응답의 창의성 (낮을수록 일관성 높음)")
    
    # 채팅 응답용 토큰 설정 (적당한 길이 유지)
    openai_max_tokens: int = Field(default=150, description="채팅 응답 길이 (적당한 길이 유지)")
    openai_auto_continue: bool = Field(default=True, description="채팅 자동 이어받기")
    openai_auto_continue_max_segments: int = Field(default=3, description="채팅 이어받기 세그먼트 수 (길게 이어갈 필요 없음)")
    openai_dynamic_max_tokens: bool = Field(default=True, description="채팅 동적 토큰 조정")
    openai_dynamic_max_tokens_cap: int = Field(default=800, description="채팅 최대 토큰 (너무 길게 나오면 안됨)") 
    
    # 요약용 토큰 설정 (완전한 요약 보장)
    openai_summary_max_tokens: int = Field(default=1200, description="요약용 토큰 (중간에 잘리면 안됨)")
    openai_summary_auto_continue: bool = Field(default=True, description="요약은 자동 이어받기 필수")
    openai_summary_auto_continue_max_segments: int = Field(default=8, description="요약은 더 많은 세그먼트 허용")

    # 세션/서버
    session_timeout_minutes: int = Field(default=30)
    summary_turn_window: int = Field(default=10)
    port: int = Field(default=8000)
    log_level: str = Field(default="INFO")
    debug: bool = Field(default=False)

# 전역 설정 인스턴스
settings = Settings()
