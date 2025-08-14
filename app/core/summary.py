from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from typing import Optional
from loguru import logger

from app.database.models import Message, Conversation, CounselSummary, UserSummary

class SummaryResponse:
    def __init__(self, content: str):
        self.content = content

def _build_summary_prompt(history: str, summary_text: str) -> list[dict]:
    instruction = (
        "다음 상담 대화 기록을 요약하세요. 사용자 이름, 상담 이유, 핵심 내용을 빠짐 없이 중복이 없도록 작성하세요. "
        "기존 요약이 있다면 삭제하지 말고 덧붙여 업데이트하세요. 무의미한 대화나 인사만 있는 경우에는 원문을 그대로 작성하세요."
    )
    user_content = (
        f"[이전 요약]\n{summary_text or ''}\n\n[대화]\n{history}"
    )
    return [
        {"role": "system", "content": "당신은 상담 대화 내용을 정확히 요약하는 비서입니다."},
        {"role": "user", "content": f"{instruction}\n\n{user_content}"},
    ]

async def generate_summary(llm_or_client, history: str, summary_text: str) -> SummaryResponse:
    """LangChain 없이 OpenAI 클라이언트로 요약을 생성합니다.

    llm_or_client: ai_service.client(OpenAI) 또는 openai.AsyncOpenAI/Sync OpenAI 호환 객체
    """
    try:
        # AsyncOpenAI / OpenAI 모두 지원 시도
        messages = _build_summary_prompt(history, summary_text)
        # 동기/비동기 클라이언트 분기 처리
        create_fn = getattr(getattr(llm_or_client, "chat", None), "completions", None)
        if create_fn is None:
            # Responses API만 있을 경우 간단히 chat 호환으로 생성
            responses_create = getattr(llm_or_client, "responses", None)
            if responses_create is None:
                raise RuntimeError("지원되지 않는 LLM 클라이언트 타입입니다.")
            resp = await llm_or_client.responses.create(
                model="gpt-4o-mini",
                input=messages,
            )
            content = getattr(resp, "output_text", "") or "요약을 생성하지 못했습니다."
            return SummaryResponse(content)

        # Async 여부 판별
        if hasattr(create_fn, "create"):
            # async client
            resp = await llm_or_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                temperature=0.2,
                max_tokens=300,
            )
            content = resp.choices[0].message.content
            return SummaryResponse(content)
        else:
            # sync client (fallback)
            resp = llm_or_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                temperature=0.2,
                max_tokens=300,
            )
            content = resp.choices[0].message.content
            return SummaryResponse(content)
    except Exception as e:
        logger.warning(f"generate_summary 실패: {e}")
        return SummaryResponse("요약을 생성하지 못했습니다.")

async def load_user_full_history(session: AsyncSession, user_id: str) -> str:
    if not user_id:
        return ""
    from app.database.models import Conversation as DBConversation
    stmt = (
        select(Message)
        .join(DBConversation, Message.conv_id == DBConversation.conv_id)
        .where(DBConversation.user_id == user_id)
        .order_by(Message.created_at.asc())
    )
    try:
        res = await session.execute(stmt)
    except Exception:
        try:
            await session.rollback()
            res = await session.execute(stmt)
        except Exception as e:
            logger.warning(f"load_user_full_history failed after rollback: {e}")
            return ""
    messages = res.scalars().all()
    lines = []
    for m in messages:
        prefix = "[사용자]" if str(m.role) == "user" else ("[상담사]" if str(m.role) == "assistant" else "[시스템]")
        lines.append(f"{prefix} {m.content}")
    return "\n".join(lines)

async def save_counsel_summary(session: AsyncSession, user_id: str, conv_id, content: str) -> Optional[CounselSummary]:
    """요약을 conv_id 단위로 upsert. 이미 존재하면 내용과 시간만 갱신(덮어쓰기)."""
    if not content or len(content.strip()) < 30:
        return None
    # conv_id로 기존 요약 조회
    stmt = (
        select(CounselSummary)
        .where(CounselSummary.conv_id == conv_id)
        .limit(1)
    )
    try:
        res = await session.execute(stmt)
    except Exception:
        try:
            await session.rollback()
            res = await session.execute(stmt)
        except Exception as e:
            logger.warning(f"save_counsel_summary select failed after rollback: {e}")
            return None
    existing = res.scalar_one_or_none()

    if existing:
        existing.content = content.strip()
        # created_at을 최신으로 갱신(별도 updated_at 컬럼 없이 요구사항 충족)
        from datetime import datetime
        existing.created_at = datetime.utcnow()
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        await session.refresh(existing)
        return existing

    summary = CounselSummary(user_id=user_id, conv_id=conv_id, content=content.strip())
    session.add(summary)
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    await session.refresh(summary)
    return summary

async def get_last_counsel_summary(session: AsyncSession, user_id: str) -> Optional[str]:
    stmt = (
        select(CounselSummary)
        .where(CounselSummary.user_id == user_id)
        .order_by(CounselSummary.created_at.desc())
        .limit(1)
    )
    try:
        res = await session.execute(stmt)
    except Exception:
        try:
            await session.rollback()
            res = await session.execute(stmt)
        except Exception as e:
            logger.warning(f"get_last_counsel_summary failed after rollback: {e}")
            return None
    s = res.scalar_one_or_none()
    return s.content if s else None

async def get_or_init_user_summary(session: AsyncSession, user_id: str) -> UserSummary:
    try:
        us = await session.get(UserSummary, user_id)
    except Exception:
        try:
            await session.rollback()
            us = await session.get(UserSummary, user_id)
        except Exception as e:
            logger.warning(f"get_or_init_user_summary get failed after rollback: {e}")
            raise
    if us is None:
        us = UserSummary(user_id=user_id, summary=None, last_message_created_at=None)
        session.add(us)
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        await session.refresh(us)
    return us

async def maybe_rollup_user_summary(
    session: AsyncSession,
    user_id: str,
    new_messages: list[Message] | None = None,
) -> None:
    """사용자 단위 20턴 윈도우 요약을 집계한다.
    - 최근 대화에서 신규 메시지 수가 설정 임계치 이상일 때, 기존 요약과 합쳐서 업데이트
    - 중복 없이 합치도록 프롬프트에 지시
    """
    from app.config import settings
    MAX_TURNS = getattr(settings, "summary_turn_window", 10)

    # user_id 기준 전체 메시지 조회
    from app.database.models import Conversation as DBConversation
    stmt = (
        select(Message)
        .join(DBConversation, Message.conv_id == DBConversation.conv_id)
        .where(DBConversation.user_id == user_id)
        .order_by(Message.created_at.asc())
    )
    try:
        res = await session.execute(stmt)
    except Exception:
        try:
            await session.rollback()
            res = await session.execute(stmt)
        except Exception as e:
            logger.warning(f"maybe_rollup_user_summary select failed after rollback: {e}")
            return
    msgs = res.scalars().all()
    if not msgs:
        return

    # 최근 MAX_TURNS 메시지 (user/assistant 모두 포함)
    recent = msgs[-MAX_TURNS:]

    # 사용자 단위 기존 요약 로드
    us = await get_or_init_user_summary(session, user_id)
    existing_summary = (us.summary or "").strip()

    # 프롬프트 구성 및 요약 생성
    history_text = []
    for m in recent:
        tag = "[사용자]" if str(m.role) == "user" else ("[상담사]" if str(m.role) == "assistant" else "[시스템]")
        history_text.append(f"{tag} {m.content}")
    history_text = "\n".join(history_text)

    from app.core.ai_service import ai_service
    try:
        prompt = (
            "아래 최근 대화(MAX_TURNS)를 기존 사용자 요약과 중복 없이 병합하여 새로운 사용자 요약으로 업데이트하세요.\n"
            "- 기존 요약의 중요한 내용은 유지\n- 중복 문장 제거\n- 핵심만 간결히\n"
            f"\n[기존 사용자 요약]\n{existing_summary}\n\n[최근 대화]\n{history_text}"
        )
        merged_text, _ = await ai_service.generate_response(session, None, prompt, "default", user_id)
    except Exception as e:
        logger.warning(f"롤업 요약 생성 실패: {e}")
        return

    # 사용자 요약과 마지막 메시지 시간 갱신
    us.summary = (merged_text or existing_summary).strip()
    us.last_message_created_at = msgs[-1].created_at
    from datetime import datetime
    us.updated_at = datetime.utcnow()
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise

async def upsert_user_summary_from_text(
    session: AsyncSession,
    user_id: str,
    summary_text: str,
) -> None:
    """CounselSummary 결과를 UserSummary에도 즉시 반영.
    - 마지막 3턴을 carryover로 저장
    - last_message_created_at 포인터 갱신
    """
    if not summary_text:
        return
    us = await get_or_init_user_summary(session, user_id)
    # 최근 메시지 시각만 갱신 (carryover 저장 컬럼 제거)
    from app.database.models import Conversation as DBConversation
    stmt = (
        select(Message)
        .join(DBConversation, Message.conv_id == DBConversation.conv_id)
        .where(DBConversation.user_id == user_id)
        .order_by(Message.created_at.asc())
    )
    try:
        res = await session.execute(stmt)
    except Exception:
        try:
            await session.rollback()
            res = await session.execute(stmt)
        except Exception as e:
            logger.warning(f"upsert_user_summary_from_text select failed after rollback: {e}")
            return
    msgs = list(res.scalars().all())
    us.summary = summary_text.strip()
    us.last_message_created_at = msgs[-1].created_at if msgs else us.last_message_created_at
    from datetime import datetime
    us.updated_at = datetime.utcnow()
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise

async def get_counsel_summary_by_conv(session: AsyncSession, conv_id) -> Optional[CounselSummary]:
    stmt = (
        select(CounselSummary)
        .where(CounselSummary.conv_id == conv_id)
        .limit(1)
    )
    try:
        res = await session.execute(stmt)
    except Exception:
        try:
            await session.rollback()
            res = await session.execute(stmt)
        except Exception as e:
            logger.warning(f"get_counsel_summary_by_conv failed after rollback: {e}")
            return None
    return res.scalar_one_or_none()