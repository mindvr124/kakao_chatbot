import os
import asyncio
from typing import List, Optional
from openai import AsyncOpenAI
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from .models import Message, PromptTemplate, Conversation
from .utils import extract_user_id
from .config import settings

class AIService:
    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=settings.openai_api_key
        )
        self.model = settings.openai_model
        self.temperature = settings.openai_temperature
        self.max_tokens = settings.openai_max_tokens
        self.default_system_prompt = """당신은 카카오 비즈니스 AI 상담사입니다. 
다음 원칙을 따라 응답해주세요:

1. 친근하고 전문적인 톤으로 대화하세요
2. 사용자의 질문에 정확하고 도움이 되는 답변을 제공하세요  
3. 모르는 내용은 솔직히 모른다고 하고, 추가 도움을 제안하세요
4. 답변은 간결하면서도 충분한 정보를 포함하세요
5. 한국어로 자연스럽게 대화하세요"""

    async def get_active_prompt(self, session: AsyncSession, prompt_name: str = "default") -> Optional[PromptTemplate]:
        """활성화된 프롬프트 템플릿을 가져옵니다."""
        stmt = (
            select(PromptTemplate)
            .where(PromptTemplate.name == prompt_name)
            .where(PromptTemplate.is_active == True)
            .order_by(PromptTemplate.version.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_conversation_history(self, session: AsyncSession, conv_id, limit: int = 10) -> List[Message]:
        """대화 히스토리를 가져옵니다."""
        stmt = (
            select(Message)
            .where(Message.conv_id == conv_id)
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        messages = result.scalars().all()
        return list(reversed(messages))  # 시간 순으로 정렬

    async def build_messages(self, session: AsyncSession, conv_id, user_input: str, prompt_name: str = "default") -> List[dict]:
        """OpenAI API용 메시지 배열을 구성합니다."""
        # 프롬프트 템플릿 가져오기
        prompt_template = await self.get_active_prompt(session, prompt_name)
        system_prompt = prompt_template.system_prompt if prompt_template else self.default_system_prompt
        
        # 대화 히스토리 가져오기
        history = await self.get_conversation_history(session, conv_id, limit=8)
        
        # 메시지 배열 구성
        messages = [{"role": "system", "content": system_prompt}]
        
        # 히스토리 추가 (최근 4턴 정도만)
        for msg in history[-8:]:  # 최근 8개 메시지 (4턴)
            messages.append({
                "role": msg.role,
                "content": msg.content
            })
        
        # 현재 사용자 입력 추가
        messages.append({"role": "user", "content": user_input})
        
        return messages

    async def generate_response(self, session: AsyncSession, conv_id, user_input: str, prompt_name: str = "default") -> tuple[str, int]:
        """AI 응답을 생성합니다."""
        try:
            messages = await self.build_messages(session, conv_id, user_input, prompt_name)
            
            logger.info(f"Calling OpenAI API with {len(messages)} messages")
            
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=self.max_tokens
            )
            
            content = response.choices[0].message.content
            tokens_used = response.usage.total_tokens if response.usage else 0
            
            logger.info(f"OpenAI response generated, tokens used: {tokens_used}")
            
            return content, tokens_used
            
        except Exception as e:
            logger.error(f"OpenAI API error: {e}")
            return "죄송합니다. 일시적인 오류가 발생했습니다. 잠시 후 다시 시도해주세요.", 0

# 전역 AI 서비스 인스턴스
ai_service = AIService()
