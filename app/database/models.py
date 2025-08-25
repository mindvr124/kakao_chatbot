from typing import Optional
from sqlmodel import SQLModel, Field, Relationship
from datetime import datetime
from zoneinfo import ZoneInfo
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
    user_name: Optional[str] = Field(default=None)
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
    request_id: Optional[str] = Field(default=None, index=True)  # X-Request-ID 값
    created_at: datetime = Field(default_factory=lambda: datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None), index=True)

## Removed: AIProcessingTask (unused)

class PromptTemplate(SQLModel, table=True):
    prompt_id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(index=True)  # 프롬프트 이름 (예: "상담사 기본", "FAQ_답변")
    version: int = Field(default=1, index=True)  # 버전 관리
    system_prompt: str  # 시스템 프롬프트
    user_prompt_template: Optional[str] = None  # 사용자 입력 템플릿(옵션)
    is_active: bool = Field(default=True, index=True)  # 활성화 여부
    description: Optional[str] = None  # 프롬프트 설명
    created_at: datetime = Field(default_factory=lambda: datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None), index=True)
    created_by: Optional[str] = None  # 생성자

## Removed: ResponseState, CounselSummary (unused)

class PromptLog(SQLModel, table=True):
    """모델 호출 시 최종 프롬프트(메시지 배열)와 파라미터를 저장"""
    conv_id: UUID | None = Field(default=None, foreign_key="conversation.conv_id", index=True)
    model: str | None = None
    prompt_name: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    messages_json: str  # JSON 직렬화된 messages
    created_at: datetime = Field(default_factory=lambda: datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None), index=True)
    msg_id: UUID = Field(primary_key=True, foreign_key="message.msg_id")  # Message와 1:1 관계

class LogMessage(SQLModel, table=True):
    """로그 메시지 저장 테이블"""
    log_id: UUID = Field(default_factory=uuid4, primary_key=True)
    level: str = Field(index=True)  # INFO, WARNING, ERROR, DEBUG
    message: str
    user_id: str | None = Field(default=None, index=True)
    conv_id: UUID | None = Field(default=None, foreign_key="conversation.conv_id", index=True)
    source: str | None = None  # 어느 모듈에서 발생한 로그인지
    created_at: datetime = Field(default_factory=lambda: datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None), index=True)

class EventLog(SQLModel, table=True):
    """운영 이벤트 로깅 테이블(요청 수신, 메시지 저장, 콜백, 요약 성공/실패 등)"""
    event_id: UUID = Field(default_factory=uuid4, primary_key=True)
    event_type: str = Field(index=True)
    user_id: Optional[str] = Field(default=None, foreign_key="appuser.user_id", index=True)
    conv_id: Optional[UUID] = Field(default=None, foreign_key="conversation.conv_id", index=True)
    request_id: Optional[str] = Field(default=None, index=True)
    details_json: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None), index=True)

class UserSummary(SQLModel, table=True):
    """사용자 단위 누적 요약 및 롤업 진도와 상태 (파일 단위)"""
    user_id: str = Field(primary_key=True, foreign_key="appuser.user_id")
    summary: Optional[str] = None
    last_message_created_at: Optional[datetime] = Field(default=None, index=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class RiskState(SQLModel, table=True):
    """사용자별 자살위험도 상태 테이블"""
    user_id: str = Field(primary_key=True, foreign_key="appuser.user_id")
    score: int = Field(default=0)  # 현재 위험도 점수 (0-100)
    last_updated: datetime = Field(default_factory=lambda: datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None))
    check_question_sent: bool = Field(default=False)  # 체크 질문 발송 여부
    last_check_score: Optional[int] = Field(default=None)  # 마지막 체크 질문 응답 점수
    created_at: datetime = Field(default_factory=lambda: datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None))
