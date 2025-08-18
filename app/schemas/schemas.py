from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from uuid import UUID

# ì¹´ì¹´???¤í‚¬ ?‘ë‹µ ?€?…ë“¤
def simple_text(text: str) -> dict:
    return {
        "version": "2.0",
        "template": {
            "outputs": [{"simpleText": {"text": text}}]
        }
    }

def card_response(title: str, description: str, thumbnail_url: str = None, buttons: list = None) -> dict:
    """ì¹´ë“œ???‘ë‹µ"""
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
    """ë¹ ë¥¸ ?µì¥ ?¬í•¨ ?‘ë‹µ"""
    return {
        "version": "2.0",
        "template": {
            "outputs": [{"simpleText": {"text": text}}],
            "quickReplies": quick_replies
        }
    }

def callback_waiting_response(message: str = "?µë????ì„± ì¤‘ì…?ˆë‹¤...") -> dict:
    """ì½œë°± ?€ê¸??‘ë‹µ"""
    return {
        "version": "2.0",
        "useCallback": True,
        "data": {
            "text": message
        }
    }

class KakaoBody(BaseModel):
    # ì¹´ì¹´?¤ê? ë³´ë‚´??ë°”ë””ë¥??„ë? ëª¨ë¸ë§í•  ?„ìš”???†ìŒ. ?°ëŠ” ë¶€ë¶„ë§Œ!
    # ?¤í‚¬ ?ŒìŠ¤???´ì—??userRequestê°€ ?„ë½?????ˆì–´ Optional ì²˜ë¦¬
    userRequest: Optional[dict] = None
    action: dict | None = None

# ?„ë¡¬?„íŠ¸ ê´€ë¦¬ìš© ?¤í‚¤ë§?
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

# AI ì²˜ë¦¬ ?‘ì—… ê´€???¤í‚¤ë§?
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
