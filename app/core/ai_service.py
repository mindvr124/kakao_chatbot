import os
import asyncio
from typing import List, Optional
from openai import AsyncOpenAI
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.database.models import Message, PromptTemplate, Conversation
from app.utils.utils import extract_user_id
from app.config import settings

class AIService:
    def __init__(self):
        # 환경 변수에서 직접 API 키 가져오기
        api_key = os.getenv('OPENAI_API_KEY') or settings.openai_api_key
        if not api_key:
            logger.warning("OpenAI API key not found in environment variables")
            api_key = "dummy_key"  # 임시 키로 초기화
            
        self.client = AsyncOpenAI(
            api_key=api_key
        )
        self.model = settings.openai_model
        self.temperature = settings.openai_temperature
        self.max_tokens = settings.openai_max_tokens
        self.default_system_prompt = """당신은 AI 심리 상담사입니다. 친근하고 공감적인 톤으로 간결하게 답변하세요."""

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
        """OpenAI API용 메시지 배열을 구성합니다. 최근 대화 히스토리를 포함합니다."""
        # 프롬프트 템플릿
        prompt_template = await self.get_active_prompt(session, prompt_name)
        system_prompt = prompt_template.system_prompt if prompt_template else self.default_system_prompt

        messages: List[dict] = [{"role": "system", "content": system_prompt}]

        # 최근 히스토리 포함 (최대 10개 메시지)
        try:
            history = await self.get_conversation_history(session, conv_id, limit=10)
            for m in history:
                role = (m.role or "user").lower()
                if role not in ("user", "assistant", "system"):
                    role = "user"
                messages.append({"role": role, "content": m.content})
        except Exception as e:
            logger.warning(f"Failed to load conversation history for {conv_id}: {e}")

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

    async def generate_simple_response(self, user_input: str) -> str:
        """데이터베이스 없이 간단한 AI 응답을 생성합니다."""
        try:
            # API 키가 없으면 기본 응답
            api_key = os.getenv('OPENAI_API_KEY') or settings.openai_api_key
            if not self.client or not api_key:
                return f"안녕하세요! '{user_input}'에 대해 문의해주셔서 감사합니다. 무엇을 도와드릴까요?"
            
            # 간단한 시스템 프롬프트
            system_prompt = """당신은 친근하고 도움이 되는 AI 상담사입니다. 
한국어로 자연스럽게 대화하고, 사용자의 질문에 정확하고 도움이 되는 답변을 제공하세요."""
            
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_input}
            ]
            
            logger.info(f"Calling OpenAI API for simple response")
            
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=self.max_tokens
            )
            
            ai_response = response.choices[0].message.content
            logger.info(f"Simple OpenAI response generated")
            
            return ai_response
            
        except Exception as e:
            logger.error(f"Error generating simple AI response: {e}")
            return f"안녕하세요! '{user_input}'에 대해 문의해주셔서 감사합니다. 현재 일시적인 문제가 있어 자세한 답변을 드리지 못하지만, 곧 해결될 예정입니다."

# 전역 AI 서비스 인스턴스
ai_service = AIService()
