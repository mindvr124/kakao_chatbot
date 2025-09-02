from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from typing import Optional
from loguru import logger

from app.database.models import Message, Conversation, UserSummary
from app.database.service import save_log_message
from app.database.models import Conversation as DBConversation

class SummaryResponse:
    def __init__(self, content: str):
        self.content = content

def _build_summary_prompt(history: str, summary_text: str, user_name: str) -> list[dict]:
    instruction = (
        f"""다음은 현재 진행중인 상담의 대화 기록입니다. 다음 조건을 지켜주세요.
        내담자의 이름은 {user_name}입니다.
        1. 내담자의 현재 심리 상태, 상담 이유와 그에 관련된 핵심 내용을 사실에 기반하여 중복이 없도록 요약해 주세요.
        2. 이전 요약 대화에서 중요한 내용은 삭제하지 말고 덧붙여서 요약해 주세요.
        3. 대화 주제가 많이 변경된 경우, 오래된 요약이 현재처럼 보이지 않도록 수정해 주세요.
        4. 이전 요약에 비교해서 현재 사용자의 심리 상태가 변화했는지 체크해 주세요.
        5. 무의미한 대화나 인사만 있는 경우 요약하지 마세요.
        6. 내담자의 이름은 요약에 저장하지 마세요. 요약 안에 내담자의 이름이 있다면 삭제해 주세요.
        7. 내담자의 이름을 제외한 이름 혹은 명칭은 요약에 포함해 주세요."""
    )
    user_content = (
        f"[이전 요약]\n{summary_text or ''}\n\n[대화]\n{history}"
    )
    return [
        {"role": "system", "content": "당신은 상담 대화 내용을 정확히 요약하는 비서입니다"},
        {"role": "user", "content": f"{instruction}\n\n{user_content}"},
    ]

async def generate_summary(llm_or_client, history: str, summary_text: str, user_name: str = "사용자") -> SummaryResponse:
    try:
        # AsyncOpenAI / OpenAI 모두 지원하도록
        messages = _build_summary_prompt(history, summary_text, user_name)
        # 동기/비동기 클라이언트 분기 처리
        create_fn = getattr(getattr(llm_or_client, "chat", None), "completions", None)
        if create_fn is None:
            # Responses API를 쓸 경우 간단히 chat 반환으로 생성
            responses_create = getattr(llm_or_client, "responses", None)
            if responses_create is None:
                raise RuntimeError("지원되지 않는 LLM 클라이언트 타입입니다.")
            resp = await llm_or_client.responses.create(
                model="gpt-4o",
                input=messages,
            )
            content = getattr(resp, "output_text", "") or "요약을 생성하지 못했습니다"
            return SummaryResponse(content)

        # Async 여부 구별
        if hasattr(create_fn, "create"):
            # async client
            from app.config import settings
            resp = await llm_or_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                temperature=0.2,
                max_tokens=settings.openai_summary_max_tokens,  # 설정에서 동적으로 가져오기
            )
            content = resp.choices[0].message.content
            return SummaryResponse(content)
        else:
            # sync client (fallback)
            from app.config import settings
            resp = llm_or_client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                temperature=0.2,
                max_tokens=settings.openai_summary_max_tokens,  # 설정에서 동적으로 가져오기
            )
            content = resp.choices[0].message.content
            return SummaryResponse(content)
    except Exception as e:
        logger.warning(f"generate_summary 실패: {e}")
        return SummaryResponse("요약을 생성하지 못했습니다")

async def load_user_full_history(session: AsyncSession, user_id: str) -> str:
    if not user_id:
        return ""
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
    """사용자 단위 롤업 요약 (10개 기준). 조건 만족 시에만 요약 업데이트.
    - 누계된 마지막 롤업 이후 신규 메시지가 MAX_TURNS(기본 10) 이상일때
    - 포함 내용: 최근 MAX_TURNS 메시지 기반으로 기존 요약과 병합
    """
    from app.config import settings
    MAX_TURNS = getattr(settings, "summary_turn_window", 10)

    # user_id 기준 사용자 메시지만 조회 (AI 응답 제외)
    from app.database.models import Conversation as DBConversation
    stmt = (
        select(Message)
        .join(DBConversation, Message.conv_id == DBConversation.conv_id)
        .where(DBConversation.user_id == user_id)
        .where(Message.role == "USER")  # 사용자 메시지만 카운트
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

    # 사용자 요약 메타 정보
    us = await get_or_init_user_summary(session, user_id)
    last_ptr = us.last_message_created_at

    # 처리할 조건 계산: 마지막 체인지 이후 신규 메시지 수
    if last_ptr is None:
        new_count = len(msgs)
    else:
        new_count = sum(1 for m in msgs if m.created_at and m.created_at > last_ptr)

    if new_count < MAX_TURNS:
        # 이벤트 로그(스킵)
        try:
            await save_log_message(session, "summary_rollup_skipped", "Summary rollup skipped", str(user_id), None, {"new_count": new_count, "need": MAX_TURNS})
        except Exception:
            pass
        logger.info(f"[SUMMARY] 10턴 요약 스킵: 현재 {new_count}개, 필요 {MAX_TURNS}개 (user_id={user_id})")
        return

    # 최근 10턴 대화 조회: 최근 10개 사용자 메시지와 그에 대응하는 AI 응답
    # 1단계: 최근 10개 사용자 메시지의 시간 범위 확인
    user_msg_stmt = (
        select(Message)
        .join(DBConversation, Message.conv_id == DBConversation.conv_id)
        .where(DBConversation.user_id == user_id)
        .where(Message.role == "USER")
        .order_by(Message.created_at.desc())
        .limit(MAX_TURNS)
    )
    try:
        user_msg_res = await session.execute(user_msg_stmt)
        recent_user_msgs = list(user_msg_res.scalars().all())
    except Exception:
        try:
            await session.rollback()
            user_msg_res = await session.execute(user_msg_stmt)
            recent_user_msgs = list(user_msg_res.scalars().all())
        except Exception as e:
            logger.warning(f"Recent user messages query failed: {e}")
            return
    
    if not recent_user_msgs:
        logger.warning(f"No recent user messages found for user_id={user_id}")
        return
    
    # 2단계: 최근 10개 사용자 메시지의 시간 범위로 전체 대화 조회
    oldest_user_msg_time = recent_user_msgs[-1].created_at
    recent_stmt = (
        select(Message)
        .join(DBConversation, Message.conv_id == DBConversation.conv_id)
        .where(DBConversation.user_id == user_id)
        .where(Message.created_at >= oldest_user_msg_time)
        .order_by(Message.created_at.asc())  # 시간순 정렬
    )
    try:
        recent_res = await session.execute(recent_stmt)
        recent_msgs = list(recent_res.scalars().all())
    except Exception:
        try:
            await session.rollback()
            recent_res = await session.execute(recent_stmt)
            recent_msgs = list(recent_res.scalars().all())
        except Exception as e:
            logger.warning(f"Recent conversation query failed: {e}")
            return
    
    # 최근 10턴 대화로 요약 생성
    recent = recent_msgs
    existing_summary = (us.summary or "").strip()
    
    logger.info(f"[SUMMARY] 10턴 요약 시작: {len(recent)}개 메시지, 기존 요약 길이 {len(existing_summary)}자 (user_id={user_id})")

    # 프롬프트 구성 및 요약 생성
    history_text = []
    for m in recent:
        tag = "[사용자]" if str(m.role) == "user" else ("[상담사]" if str(m.role) == "assistant" else "[시스템]")
        history_text.append(f"{tag} {m.content}")
    history_text = "\n".join(history_text)

    from app.core.ai_service import ai_service
    try:
        prompt = (
            "아래 최근 대화(MAX_TURNS)와 기존 사용자 요약을 중복 없이 병합하여 새로운 사용자 요약으로 업데이트하세요.\n"
            "- 기존 요약의 중요한 내용은 유지\n- 중복 문장 제거\n- 핵심만 간결히\n"
            f"\n[기존 사용자 요약]\n{existing_summary}\n\n[최근 대화]\n{history_text}"
        )
        merged_text, _, _ = await ai_service.generate_response(session, None, prompt, "default", user_id, None)
    except Exception as e:
        logger.warning(f"롤업 요약 생성 실패: {e}")
        try:
            await save_log_message(session, "summary_rollup_failed", "Summary rollup failed", str(user_id), None, {"error": str(e)[:300]})
        except Exception:
            pass
        return

    # ?�용???�약�?마�?�?메시지 ?�간 갱신
    us.summary = (merged_text or existing_summary).strip()
    us.last_message_created_at = msgs[-1].created_at if msgs else us.last_message_created_at
    from datetime import datetime
    us.updated_at = datetime.now()
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    try:
        await save_log_message(session, "summary_rollup_saved", "Summary rollup saved", str(user_id), None, {"len": len(us.summary or ""), "used_msgs": len(recent)})
    except Exception:
        pass
    
    logger.info(f"[SUMMARY] 10턴 요약 완료: {len(us.summary or '')}자, {len(recent)}개 메시지 사용 (user_id={user_id})")

async def upsert_user_summary_from_text(
    session: AsyncSession,
    user_id: str,
    summary_text: str,
) -> None:
    """UserSummary를 텍스트로 업데이트.
    - last_message_created_at 포인터 갱신
    """
    if not summary_text:
        return
    us = await get_or_init_user_summary(session, user_id)
    # 최근 메시지 포인터 갱신 (carryover 관련 컬럼 제거)
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
    us.updated_at = datetime.now()
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise


