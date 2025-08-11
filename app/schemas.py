from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from uuid import UUID

# 카카오 스킬 응답 타입들
def simple_text(text: str) -> dict:
    return {
        "version": "2.0",
        "template": {
            "outputs": [{"simpleText": {"text": text}}]
        }
    }

def card_response(title: str, description: str, thumbnail_url: str = None, buttons: list = None) -> dict:
    """카드형 응답"""
    card = {
        "title": title,
        "description": description
    }
    if thumbnail_url:
        card["thumbnail"] = {"imageUrl": thumbnail_url}
    if buttons:
        card["buttons"] = buttons
        
    return {
        "version": "2.0",
        "template": {
            "outputs": [{"basicCard": card}]
        }
    }

def quick_reply_response(text: str, quick_replies: list) -> dict:
    """빠른 답장 포함 응답"""
    return {
        "version": "2.0",
        "template": {
            "outputs": [{"simpleText": {"text": text}}],
            "quickReplies": quick_replies
        }
    }

def callback_waiting_response(message: str = "답변을 생성 중입니다...") -> dict:
    """콜백 대기 응답"""
    return {
        "version": "2.0",
        "useCallback": True,
        "data": {
            "text": message
        }
    }

class KakaoBody(BaseModel):
    # 카카오가 보내는 바디를 전부 모델링할 필요는 없음. 쓰는 부분만!
    userRequest: dict
    action: dict | None = None

# 프롬프트 관리용 스키마
class PromptTemplateCreate(BaseModel):
    name: str
    system_prompt: str
    description: Optional[str] = None
    user_prompt_template: Optional[str] = None

class PromptTemplateResponse(BaseModel):
    prompt_id: UUID
    name: str
    version: int
    system_prompt: str
    user_prompt_template: Optional[str]
    is_active: bool
    description: Optional[str]
    created_at: datetime
    created_by: Optional[str]

class PromptTemplateUpdate(BaseModel):
    system_prompt: Optional[str] = None
    description: Optional[str] = None
    user_prompt_template: Optional[str] = None

# AI 처리 작업 관련 스키마
class AIProcessingTaskResponse(BaseModel):
    task_id: UUID
    conv_id: UUID
    status: str
    user_input: str
    retry_count: int
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    result_message_id: Optional[UUID] = None

class AIProcessingStatusResponse(BaseModel):
    task_id: UUID
    status: str
    created_at: datetime
    retry_count: int
    ai_response: Optional[str] = None
    tokens_used: Optional[int] = None
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None

class AIProcessingTaskListResponse(BaseModel):
    tasks: list[AIProcessingTaskResponse]
    total: int

class RetryAIProcessingTaskResponse(BaseModel):
    message: str
    task_id: UUID
