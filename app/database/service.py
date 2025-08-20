from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.models import AppUser, Conversation, Message, PromptTemplate, PromptLog, UserSummary, EventLog
from app.utils.utils import session_expired
from datetime import datetime
from typing import Optional, List
from uuid import UUID
from loguru import logger

async def get_user_name(session: AsyncSession, user_id: str) -> str | None:
    """사용자 이름을 조회합니다. 없으면 None을 반환합니다."""
    try:
        user = await session.get(AppUser, user_id)
        return user.user_name if user else None
    except Exception:
        try:
            await session.rollback()
            user = await session.get(AppUser, user_id)
            return user.user_name if user else None
        except Exception:
            return None

async def upsert_user(session: AsyncSession, user_id: str, user_name: str | None = None) -> AppUser:
    try:
        user = await session.get(AppUser, user_id)
        if not user:
            # 새 사용자 생성 (INSERT)
            logger.info(f"[생성] 새 사용자 생성: {user_id} | 이름: {user_name}")
            user = AppUser(user_id=user_id, user_name=user_name)
            session.add(user)
            try:
                await session.commit()
                logger.info(f"[완료] 새 사용자 생성 완료: {user_id}")
            except Exception:
                await session.rollback()
                raise
            await session.refresh(user)
        elif user_name is not None:  # 이름이 제공되면 업데이트 (UPDATE)
            logger.info(f"[변경] 사용자 이름 변경: {user_id} | '{user.user_name}' -> '{user_name}'")
            user.user_name = user_name
            try:
                await session.commit()
                logger.info(f"[완료] 사용자 이름 변경 완료: {user_id} -> {user_name}")
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
    """가장 최신 대화가 만료된 경우에만 새로 생성.
    기존의 세션 만료 여부를 기반으로 신규 생성을 결정한다.
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
    msg_id: UUID,  # Message와 1:1 관계 (primary key, 필수)
    conv_id,
    model: str | None = None,
    prompt_name: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    messages_json: str = "",
) -> bool:
    """프롬프트 로그를 저장합니다. 성공 여부를 반환합니다."""
    try:
        from uuid import UUID
        conv_uuid = None
        try:
            conv_uuid = conv_id if isinstance(conv_id, UUID) else UUID(str(conv_id))
        except Exception:
            conv_uuid = None
        log = PromptLog(
            conv_id=conv_uuid,
            model=model,
            prompt_name=prompt_name,
            temperature=temperature,
            max_tokens=max_tokens,
            messages_json=messages_json,
            msg_id=msg_id  # primary key로 사용
        )
        session.add(log)
        await session.commit()
        return True
    except Exception:
        # 로깅 실패는 무시하되 실패 상태 반환
        return False

async def save_log_message(
    session: AsyncSession,
    level: str,
    message: str,
    user_id: str | None = None,
    conv_id = None,
    source: str | None = None,
) -> bool:
    """로그 메시지를 저장합니다. 성공 여부를 반환합니다."""
    try:
        from app.database.models import LogMessage
        from uuid import UUID
        
        conv_uuid = None
        try:
            conv_uuid = conv_id if isinstance(conv_id, UUID) else UUID(str(conv_id)) if conv_id else None
        except Exception:
            conv_uuid = None
            
        log_msg = LogMessage(
            level=level,
            message=message,
            user_id=user_id,
            conv_id=conv_uuid,
            source=source
        )
        session.add(log_msg)
        await session.commit()
        return True
    except Exception:
        try:
            await session.rollback()
        except Exception:
            pass
        return False

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
    """새로운 프롬프트 템플릿을 생성합니다"""
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
        # 기존 활성 프롬프트는 비활성화
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
    """지정된 프롬프트 템플릿을 활성화합니다."""
    prompt = await session.get(PromptTemplate, prompt_id)
    if not prompt:
        return False
    
    # 같은 이름의 다른 프롬프트는 비활성화
    stmt = select(PromptTemplate).where(PromptTemplate.name == prompt.name, PromptTemplate.is_active == True)
    try:
        result = await session.execute(stmt)
    except Exception:
        await session.rollback()
        result = await session.execute(stmt)
    existing_prompts = result.scalars().all()
    
    for existing in existing_prompts:
        existing.is_active = False
    
    # 선택된 프롬프트 활성화
    prompt.is_active = True
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return True

## removed response state helpers
