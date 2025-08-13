from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from typing import Optional
from loguru import logger

from app.database.models import Message, Conversation, CounselSummary

class SummaryResponse:
    def __init__(self, content: str):
        self.content = content

def _build_summary_prompt(history: str, summary_text: str) -> list[dict]:
    instruction = (
        "다음 상담 대화 기록을 요약하세요. 사용자 이름, 상담 이유, 핵심 내용을 중복 없이 간결하게 작성하세요. "
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

async def load_full_history(session: AsyncSession, conv_id) -> str:
    stmt = (
        select(Message)
        .where(Message.conv_id == conv_id)
        .order_by(Message.created_at.asc())
    )
    res = await session.execute(stmt)
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
    res = await session.execute(stmt)
    existing = res.scalar_one_or_none()

    if existing:
        existing.content = content.strip()
        # created_at을 최신으로 갱신(별도 updated_at 컬럼 없이 요구사항 충족)
        from datetime import datetime
        existing.created_at = datetime.utcnow()
        await session.commit()
        await session.refresh(existing)
        return existing

    summary = CounselSummary(user_id=user_id, conv_id=conv_id, content=content.strip())
    session.add(summary)
    await session.commit()
    await session.refresh(summary)
    return summary

async def get_last_counsel_summary(session: AsyncSession, user_id: str) -> Optional[str]:
    stmt = (
        select(CounselSummary)
        .where(CounselSummary.user_id == user_id)
        .order_by(CounselSummary.created_at.desc())
        .limit(1)
    )
    res = await session.execute(stmt)
    s = res.scalar_one_or_none()
    return s.content if s else None

async def get_counsel_summary_by_conv(session: AsyncSession, conv_id) -> Optional[CounselSummary]:
    stmt = (
        select(CounselSummary)
        .where(CounselSummary.conv_id == conv_id)
        .limit(1)
    )
    res = await session.execute(stmt)
    return res.scalar_one_or_none()