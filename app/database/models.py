from typing import Optional
from sqlmodel import SQLModel, Field, Relationship
from datetime import datetime
from uuid import uuid4, UUID
from enum import Enum

class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"

class AIProcessingStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

class AppUser(SQLModel, table=True):
    user_id: str = Field(primary_key=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)

class Conversation(SQLModel, table=True):
    conv_id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: str = Field(foreign_key="appuser.user_id", index=True)
    started_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    ended_at: Optional[datetime] = Field(default=None)
    summary: Optional[str] = None

class Message(SQLModel, table=True):
    msg_id: UUID = Field(default_factory=uuid4, primary_key=True)
    conv_id: UUID = Field(foreign_key="conversation.conv_id", index=True)
    user_id: Optional[str] = Field(default=None, foreign_key="appuser.user_id", index=True)
    role: MessageRole = Field(index=True)
    content: str
    tokens: Optional[int] = None
    request_id: Optional[str] = Field(default=None, index=True)  # X-Request-ID 등
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)

## Removed: AIProcessingTask (unused)

class PromptTemplate(SQLModel, table=True):
    prompt_id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(index=True)  # 프롬프트 이름 (예: "상담봇_기본", "FAQ_응답")
    version: int = Field(default=1, index=True)  # 버전 관리
    system_prompt: str  # 시스템 프롬프트
    user_prompt_template: Optional[str] = None  # 사용자 입력 템플릿 (옵션)
    is_active: bool = Field(default=True, index=True)  # 활성화 여부
    description: Optional[str] = None  # 프롬프트 설명
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    created_by: Optional[str] = None  # 생성자

## Removed: ResponseState (unused)

class CounselSummary(SQLModel, table=True):
    """대화 요약 저장 테이블"""
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: str = Field(foreign_key="appuser.user_id", index=True)
    conv_id: UUID = Field(foreign_key="conversation.conv_id", index=True)
    content: str
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)

class PromptLog(SQLModel, table=True):
    """모델 호출 시 최종 프롬프트(메시지 배열)와 파라미터를 저장"""
    log_id: UUID = Field(default_factory=uuid4, primary_key=True)
    conv_id: UUID | None = Field(default=None, foreign_key="conversation.conv_id", index=True)
    request_id: str | None = Field(default=None, index=True)
    model: str | None = None
    prompt_name: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    messages_json: str  # JSON 직렬화된 messages
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)

class UserSummary(SQLModel, table=True):
    """사용자 단위 누적 요약 및 롤업 윈도우 상태 (단일 정의)"""
    user_id: str = Field(primary_key=True, foreign_key="appuser.user_id")
    summary: Optional[str] = None
    last_message_created_at: Optional[datetime] = Field(default=None, index=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow)