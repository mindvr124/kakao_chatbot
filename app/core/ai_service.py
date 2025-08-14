import os
import asyncio
from typing import List, Optional
from uuid import UUID
from openai import AsyncOpenAI
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.database.models import Message, PromptTemplate, Conversation
from app.utils.utils import extract_user_id
from app.config import settings
from app.core.summary import get_last_counsel_summary, get_or_init_user_summary, maybe_rollup_user_summary
from app.database.service import save_prompt_log
import json
from app.core.observability import traceable
from app.utils.utils import remove_markdown

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
        self.default_system_prompt = """당신은 전문 AI 심리상담가입니다. 친근하고 공감적인 톤으로 간결하게 답변하세요. 지금까지의 대화 내용과 아래 요약을 우선 참고하여, 맥락에 맞게 대화를 이어가세요."""

    @traceable
    async def get_active_prompt(self, session: AsyncSession, prompt_name: str = "default") -> Optional[PromptTemplate]:
        """활성화된 프롬프트 템플릿을 가져옵니다."""
        stmt = (
            select(PromptTemplate)
            .where(PromptTemplate.name == prompt_name)
            .where(PromptTemplate.is_active == True)
            .order_by(PromptTemplate.version.desc())
            .limit(1)
        )
        try:
            result = await session.execute(stmt)
            return result.scalar_one_or_none()
        except Exception as e:
            try:
                await session.rollback()
            except Exception:
                pass
            try:
                result = await session.execute(stmt)
                return result.scalar_one_or_none()
            except Exception:
                logger.warning(f"get_active_prompt failed after rollback: {e}")
                return None

    @traceable
    async def get_conversation_history(self, session: AsyncSession, conv_id) -> List[Message]:
        """해당 conv_id의 전체 메시지를 시간순으로 반환합니다. UUID가 아니면 빈 리스트 반환."""
        try:
            if isinstance(conv_id, UUID):
                conv_uuid = conv_id
            else:
                conv_uuid = UUID(str(conv_id))
        except Exception:
            return []

        stmt = (
            select(Message)
            .where(Message.conv_id == conv_uuid)
            .order_by(Message.created_at.asc())
        )
        try:
            result = await session.execute(stmt)
        except Exception as e:
            try:
                await session.rollback()
                result = await session.execute(stmt)
            except Exception:
                logger.warning(f"get_conversation_history failed after rollback: {e}")
                return []
        messages = result.scalars().all()
        return messages

    @traceable
    async def build_messages(self, session: AsyncSession, conv_id, user_input: str, prompt_name: str = "default", user_id: Optional[str] = None) -> List[dict]:
        """시스템 프롬프트 + (이전 요약) + 전체 히스토리 + 현재 사용자 입력을 구축합니다."""
        prompt_template = await self.get_active_prompt(session, prompt_name)
        system_prompt = prompt_template.system_prompt if prompt_template else self.default_system_prompt

        messages: List[dict] = [{"role": "system", "content": system_prompt}]
        # 맥락 활용 지시를 명시적으로 추가
        messages.append({
            "role": "system",
            "content": "아래의 이전 요약(있다면)과 대화 기록을 참고하여, 맥락에 맞는 답변을 제공하세요."
        })

        # conv_id가 UUID일 때만 대화/요약 조회
        conv_uuid: UUID | None = None
        try:
            conv_uuid = conv_id if isinstance(conv_id, UUID) else UUID(str(conv_id))
        except Exception:
            conv_uuid = None

        # 사용자 요약 조회 (있으면 사용, 없으면 폴백 없음도 허용)
        # 항상 appuser의 user_id를 우선 사용 (conv_id는 보조)
        target_user_id: Optional[str] = user_id
        conversation: Conversation | None = None
        if not target_user_id and conv_uuid is not None:
            conversation = await session.get(Conversation, conv_uuid)
            target_user_id = conversation.user_id if conversation else None

        has_user_summary = False
        user_summary_text: Optional[str] = None
        if target_user_id:
            try:
                us = await get_or_init_user_summary(session, target_user_id)
                if us and us.summary:
                    user_summary_text = us.summary
                    has_user_summary = True
            except Exception:
                has_user_summary = False

        if has_user_summary and user_summary_text:
            messages.append({
                "role": "system",
                "content": f"이전 상담 요약:\n{user_summary_text}"
            })

        # 히스토리 구성 규칙
        # - 요약이 없으면: 대화 시작부터 누적하여 최대 20 메시지 전송
        # - 요약이 있으면: 최근 3턴(=6 메시지)만 전송
        history_messages: List[Message] = await self.get_conversation_history(session, conv_uuid or conv_id)
        if has_user_summary:
            max_pairs = 3
            max_msgs = max_pairs * 2
            if len(history_messages) > max_msgs:
                history_messages = history_messages[-max_msgs:]
        else:
            max_window = getattr(settings, "summary_turn_window", 20)
            if len(history_messages) > max_window:
                history_messages = history_messages[-max_window:]
        for m in history_messages:
            # Enum은 value로 안전하게 추출
            role_value = getattr(m.role, "value", None) or (m.role if isinstance(m.role, str) else str(m.role))
            role_value = str(role_value).lower()
            if role_value not in ("user", "assistant", "system"):
                # 예상치 못한 값 방어: 기본은 user/assistant로 폴백
                role_value = "user" if "user" in role_value else "assistant"
            messages.append({"role": role_value, "content": m.content})

        # 현재 사용자 입력 추가
        messages.append({"role": "user", "content": user_input})

        return messages

    @traceable
    async def generate_response(self, session: AsyncSession, conv_id, user_input: str, prompt_name: str = "default", user_id: Optional[str] = None) -> tuple[str, int]:
        """Chat Completions에 전체 히스토리와 요약(있다면)을 포함해 응답을 생성합니다."""
        try:
            messages = await self.build_messages(session, conv_id, user_input, prompt_name, user_id)

            logger.info(f"Calling OpenAI Chat Completions with {len(messages)} messages")

            # 프롬프트 로깅 (비차단)
            try:
                messages_json = json.dumps(messages, ensure_ascii=False)
                await save_prompt_log(session, conv_id, messages_json, self.model, prompt_name, self.temperature, self.max_tokens, None)
            except Exception:
                pass

            # 동적 max_tokens 산정
            max_tokens = self.max_tokens
            if settings.openai_dynamic_max_tokens:
                try:
                    # 매우 단순한 휴리스틱: 입력 길이 기반 스케일링
                    user_len = sum(len(m.get("content") or "") for m in messages)
                    scaled = min(settings.openai_dynamic_max_tokens_cap, max(self.max_tokens, int(user_len * 0.1)))
                    max_tokens = scaled
                except Exception:
                    pass

            # 1차 호출
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=max_tokens,
            )

            content = response.choices[0].message.content or ""
            tokens_used = response.usage.total_tokens if response.usage else 0

            # 잘림(unfinished) 감지 및 이어쓰기
            def _is_truncated(resp) -> bool:
                try:
                    finish_reason = resp.choices[0].finish_reason
                    return str(finish_reason).lower() in ("length", "content_filter")
                except Exception:
                    return False

            accumulated = content
            segments = 0
            last_resp = response
            while settings.openai_auto_continue and _is_truncated(last_resp) and segments < settings.openai_auto_continue_max_segments:
                segments += 1
                follow_messages = messages + [
                    {"role": "assistant", "content": accumulated[-2000:]},
                    {"role": "user", "content": "이어서 계속 작성해 주세요."},
                ]
                last_resp = await self.client.chat.completions.create(
                    model=self.model,
                    messages=follow_messages,
                    temperature=self.temperature,
                    max_tokens=max_tokens,
                )
                more = last_resp.choices[0].message.content or ""
                accumulated += ("\n" + more if more else "")
                try:
                    tokens_used += last_resp.usage.total_tokens if last_resp.usage else 0
                except Exception:
                    pass

            content = accumulated
            # 최종 전송 전 마크다운 제거
            try:
                content = remove_markdown(content)
            except Exception:
                pass

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
