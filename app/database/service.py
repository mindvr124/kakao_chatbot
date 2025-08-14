from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.models import AppUser, Conversation, Message, PromptTemplate, PromptLog, UserSummary, EventLog
from app.utils.utils import session_expired
from datetime import datetime
from typing import Optional, List

async def upsert_user(session: AsyncSession, user_id: str) -> AppUser:
    try:
        user = await session.get(AppUser, user_id)
        if not user:
            user = AppUser(user_id=user_id)
            session.add(user)
            try:
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            await session.refresh(user)
        return user
    except Exception:
        try:
            await session.rollback()
        except Exception:
            pass
        raise

async def get_or_create_conversation(session: AsyncSession, user_id: str) -> Conversation:
    """항상 최신 대화를 재사용. 없으면 새로 생성.
    기존의 세션 타임아웃 기반 신규 생성은 제거한다.
    """
    try:
        stmt = (
            select(Conversation)
            .where(Conversation.user_id == user_id)
            .order_by(Conversation.started_at.desc())
            .limit(1)
        )
        try:
            res = await session.execute(stmt)
        except Exception:
            await session.rollback()
            res = await session.execute(stmt)
        conv: Optional[Conversation] = res.scalar_one_or_none()

        if conv is None:
            conv = Conversation(user_id=user_id)
            session.add(conv)
            try:
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            await session.refresh(conv)
        return conv
    except Exception:
        try:
            await session.rollback()
        except Exception:
            pass
        raise

async def save_message(
    session: AsyncSession,
    conv_id,
    role: str,
    content: str,
    request_id: str | None = None,
    tokens: int | None = None,
    user_id: str | None = None,
) -> Message:
    try:
        # user_id가 비었으면 conv_id로 보강 시도
        if not user_id:
            try:
                from uuid import UUID
                from app.database.models import Conversation as DBConversation
                conv_uuid = conv_id if isinstance(conv_id, UUID) else UUID(str(conv_id))
                conv_obj = await session.get(DBConversation, conv_uuid)
                if conv_obj and conv_obj.user_id:
                    user_id = conv_obj.user_id
            except Exception:
                try:
                    await session.rollback()
                    from uuid import UUID
                    from app.database.models import Conversation as DBConversation
                    conv_uuid = conv_id if isinstance(conv_id, UUID) else UUID(str(conv_id))
                    conv_obj = await session.get(DBConversation, conv_uuid)
                    if conv_obj and conv_obj.user_id:
                        user_id = conv_obj.user_id
                except Exception:
                    pass
        msg = Message(
            conv_id=conv_id,
            user_id=user_id,
            role=role,
            content=content,
            request_id=request_id,
            tokens=tokens
        )
        session.add(msg)
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        await session.refresh(msg)
        return msg
    except Exception:
        try:
            await session.rollback()
        except Exception:
            pass
        raise

async def save_prompt_log(
    session: AsyncSession,
    conv_id,
    messages_json: str,
    model: str | None,
    prompt_name: str | None,
    temperature: float | None,
    max_tokens: int | None,
    request_id: str | None = None,
):
    try:
        from uuid import UUID
        conv_uuid = None
        try:
            conv_uuid = conv_id if isinstance(conv_id, UUID) else UUID(str(conv_id))
        except Exception:
            conv_uuid = None
        log = PromptLog(
            conv_id=conv_uuid,
            request_id=request_id,
            model=model,
            prompt_name=prompt_name,
            temperature=temperature,
            max_tokens=max_tokens,
            messages_json=messages_json,
        )
        session.add(log)
        await session.commit()
    except Exception:
        # 로깅 실패는 무시
        pass

async def save_event_log(
    session: AsyncSession,
    event_type: str,
    user_id: str | None = None,
    conv_id = None,
    request_id: str | None = None,
    details: dict | None = None,
):
    try:
        from uuid import UUID
        conv_uuid = None
        try:
            conv_uuid = conv_id if isinstance(conv_id, UUID) else UUID(str(conv_id)) if conv_id else None
        except Exception:
            conv_uuid = None
        log = EventLog(
            event_type=event_type,
            user_id=user_id,
            conv_id=conv_uuid,
            request_id=request_id,
            details_json=(__import__('json').dumps(details, ensure_ascii=False) if details else None),
        )
        session.add(log)
        await session.commit()
    except Exception:
        # 기존 세션이 중단 상태일 수 있으므로 롤백 후 별도 세션으로 재시도
        try:
            await session.rollback()
        except Exception:
            pass
        try:
            from app.database.db import get_session
            async for s in get_session():
                try:
                    from uuid import UUID
                    conv_uuid = None
                    try:
                        conv_uuid = conv_id if isinstance(conv_id, UUID) else UUID(str(conv_id)) if conv_id else None
                    except Exception:
                        conv_uuid = None
                    log = EventLog(
                        event_type=event_type,
                        user_id=user_id,
                        conv_id=conv_uuid,
                        request_id=request_id,
                        details_json=(__import__('json').dumps(details, ensure_ascii=False) if details else None),
                    )
                    s.add(log)
                    await s.commit()
                    break
                except Exception:
                    try:
                        await s.rollback()
                    except Exception:
                        pass
                    break
        except Exception:
            pass
        return

# 프롬프트 관리 함수들
async def create_prompt_template(
    session: AsyncSession,
    name: str,
    system_prompt: str,
    description: str | None = None,
    user_prompt_template: str | None = None,
    created_by: str | None = None
) -> PromptTemplate:
    """새로운 프롬프트 템플릿을 생성합니다."""
    # 기존 프롬프트가 있다면 비활성화
    existing_stmt = select(PromptTemplate).where(PromptTemplate.name == name, PromptTemplate.is_active == True)
    try:
        existing_result = await session.execute(existing_stmt)
    except Exception:
        await session.rollback()
        existing_result = await session.execute(existing_stmt)
    existing_prompts = existing_result.scalars().all()
    
    # 새 버전 번호 계산
    version = 1
    if existing_prompts:
        max_version = max(p.version for p in existing_prompts)
        version = max_version + 1
        # 기존 활성 프롬프트들 비활성화
        for prompt in existing_prompts:
            prompt.is_active = False
    
    # 새 프롬프트 생성
    new_prompt = PromptTemplate(
        name=name,
        version=version,
        system_prompt=system_prompt,
        user_prompt_template=user_prompt_template,
        description=description,
        created_by=created_by,
        is_active=True
    )
    
    session.add(new_prompt)
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    await session.refresh(new_prompt)
    return new_prompt

async def get_prompt_templates(session: AsyncSession, active_only: bool = True) -> List[PromptTemplate]:
    """프롬프트 템플릿 목록을 가져옵니다."""
    stmt = select(PromptTemplate).order_by(PromptTemplate.name, PromptTemplate.version.desc())
    if active_only:
        stmt = stmt.where(PromptTemplate.is_active == True)
    
    try:
        result = await session.execute(stmt)
    except Exception:
        await session.rollback()
        result = await session.execute(stmt)
    return result.scalars().all()

async def get_prompt_template_by_name(session: AsyncSession, name: str) -> Optional[PromptTemplate]:
    """이름으로 활성 프롬프트 템플릿을 가져옵니다."""
    stmt = (
        select(PromptTemplate)
        .where(PromptTemplate.name == name, PromptTemplate.is_active == True)
        .order_by(PromptTemplate.version.desc())
        .limit(1)
    )
    try:
        result = await session.execute(stmt)
    except Exception:
        await session.rollback()
        result = await session.execute(stmt)
    return result.scalar_one_or_none()

async def activate_prompt_template(session: AsyncSession, prompt_id: str) -> bool:
    """특정 프롬프트 템플릿을 활성화합니다."""
    prompt = await session.get(PromptTemplate, prompt_id)
    if not prompt:
        return False
    
    # 같은 이름의 다른 프롬프트들 비활성화
    stmt = select(PromptTemplate).where(PromptTemplate.name == prompt.name, PromptTemplate.is_active == True)
    try:
        result = await session.execute(stmt)
    except Exception:
        await session.rollback()
        result = await session.execute(stmt)
    existing_prompts = result.scalars().all()
    
    for existing in existing_prompts:
        existing.is_active = False
    
    # 선택한 프롬프트 활성화
    prompt.is_active = True
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return True

## removed response state helpers
