from typing import Optional, Dict, Any
from sqlmodel import SQLModel, Field, Relationship
from datetime import datetime
from zoneinfo import ZoneInfo
from uuid import uuid4, UUID
import enum as pyenum
from sqlalchemy import Enum as SAEnum, Column
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.mutable import MutableDict

class MessageRole(str, pyenum.Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"

class AIProcessingStatus(str, pyenum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

class LogLevel(str, pyenum.Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    DEBUG = "DEBUG"

class LogSource(str, pyenum.Enum):
    CALLBACK = "callback"
    WORKER = "worker"
    APP = "app"



class AppUser(SQLModel, table=True):
    user_id: str = Field(primary_key=True)
    user_name: str = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None))

class Conversation(SQLModel, table=True):
    conv_id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: str = Field(foreign_key="appuser.user_id", index=True)
    started_at: datetime = Field(default_factory=lambda: datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None), index=True)
    ended_at: datetime = Field(default=None)
    summary: str = Field(default=None)

class Message(SQLModel, table=True):
    msg_id: UUID = Field(default_factory=uuid4, primary_key=True)
    conv_id: UUID = Field(foreign_key="conversation.conv_id", index=True)
    user_id: str = Field(default=None, foreign_key="appuser.user_id", index=True)
    role: MessageRole = Field(
        default=MessageRole.USER,
        sa_column=Column(SAEnum(MessageRole, name="message_role", native_enum=False))
    )
    content: str
    tokens: int = Field(default=None)
    request_id: str = Field(default=None, index=True)  # X-Request-ID 값
    created_at: datetime = Field(default_factory=lambda: datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None), index=True)

## Removed: AIProcessingTask (unused)

class PromptTemplate(SQLModel, table=True):
    prompt_id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(index=True)  # 프롬프트 이름 (예: "상담사 기본", "FAQ_답변")
    version: int = Field(default=1, index=True)  # 버전 관리
    system_prompt: str  # 시스템 프롬프트
    user_prompt_template: str = Field(default=None)  # 사용자 입력 템플릿(옵션)
    is_active: bool = Field(default=True, index=True)  # 활성화 여부
    description: str = Field(default=None)  # 프롬프트 설명
    created_at: datetime = Field(default_factory=lambda: datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None), index=True)
    created_by: str = Field(default=None)  # 생성자

## Removed: ResponseState, CounselSummary (unused)

class PromptLog(SQLModel, table=True):
    """모델 호출 시 최종 프롬프트(메시지 배열)와 파라미터를 저장"""
    conv_id: UUID = Field(default=None, foreign_key="conversation.conv_id", index=True)
    model: str = Field(default=None)
    prompt_name: str = Field(default=None)
    temperature: float = Field(default=None)
    max_tokens: int = Field(default=None)
    messages_json: str  # JSON 직렬화된 messages
    created_at: datetime = Field(default_factory=lambda: datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None), index=True)
    msg_id: UUID = Field(primary_key=True, foreign_key="message.msg_id")  # Message와 1:1 관계

class LogMessage(SQLModel, table=True):
    """로그 메시지 저장 테이블"""
    log_id: UUID = Field(default_factory=uuid4, primary_key=True)
    level: LogLevel = Field(
        default=LogLevel.INFO,
        sa_column=Column(SAEnum(LogLevel, name="log_level", native_enum=False))
    )
    message: str
    user_id: str = Field(default=None, index=True)  # str 타입으로 통일
    conv_id: UUID = Field(default=None, foreign_key="conversation.conv_id", index=True)
    source: Optional[Dict[str, Any]] = Field(
        default=None,
        sa_column=Column(MutableDict.as_mutable(JSONB))
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None), index=True)

class UserSummary(SQLModel, table=True):
    """사용자 단위 누적 요약 및 롤업 진도와 상태 (파일 단위)"""
    user_id: str = Field(primary_key=True, foreign_key="appuser.user_id")
    summary: str = Field(default=None)
    last_message_created_at: datetime = Field(default=None, index=True)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None))

class RiskState(SQLModel, table=True, table_name="riskstate"):
    """사용자별 자살위험도 상태 테이블"""
    user_id: str = Field(primary_key=True, foreign_key="appuser.user_id")
    score: int = Field(default=0)  # 현재 위험도 점수 (0-100)
    last_updated: datetime = Field(default_factory=lambda: datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None))
    check_question_sent: bool = Field(default=False)  # 체크 질문 발송 여부
    last_check_score: int = Field(default=None)  # 마지막 체크 질문 응답 점수
    check_question_turn: int = Field(default=0)  # 체크 질문 턴 카운트 (20부터 0까지)
    created_at: datetime = Field(default_factory=lambda: datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None))
