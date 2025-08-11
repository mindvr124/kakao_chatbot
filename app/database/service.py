from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession
from .models import AppUser, Conversation, Message, PromptTemplate
from .utils import session_expired
from datetime import datetime
from typing import Optional, List

async def upsert_user(session: AsyncSession, user_id: str) -> AppUser:
    user = await session.get(AppUser, user_id)
    if not user:
        user = AppUser(user_id=user_id)
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user

async def get_or_create_conversation(session: AsyncSession, user_id: str) -> Conversation:
    # 최근 대화 하나 조회
    stmt = (
        select(Conversation)
        .where(Conversation.user_id == user_id)
        .order_by(Conversation.started_at.desc())
        .limit(1)
    )
    res = await session.execute(stmt)
    conv: Optional[Conversation] = res.scalar_one_or_none()

    if conv is None:
        conv = Conversation(user_id=user_id)
        session.add(conv)
        await session.commit()
        await session.refresh(conv)
        return conv

    # 세션 타임아웃 체크
    # 최근 메시지 시간을 기준으로도 가능하지만, 간단히 started_at 사용
    if session_expired(conv.started_at):
        conv = Conversation(user_id=user_id)
        session.add(conv)
        await session.commit()
        await session.refresh(conv)

    return conv

async def save_message(
    session: AsyncSession,
    conv_id,
    role: str,
    content: str,
    request_id: str | None = None,
    tokens: int | None = None,
) -> Message:
    msg = Message(
        conv_id=conv_id,
        role=role,
        content=content,
        request_id=request_id,
        tokens=tokens
    )
    session.add(msg)
    await session.commit()
    await session.refresh(msg)
    return msg

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
    await session.commit()
    await session.refresh(new_prompt)
    return new_prompt

async def get_prompt_templates(session: AsyncSession, active_only: bool = True) -> List[PromptTemplate]:
    """프롬프트 템플릿 목록을 가져옵니다."""
    stmt = select(PromptTemplate).order_by(PromptTemplate.name, PromptTemplate.version.desc())
    if active_only:
        stmt = stmt.where(PromptTemplate.is_active == True)
    
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
    result = await session.execute(stmt)
    return result.scalar_one_or_none()

async def activate_prompt_template(session: AsyncSession, prompt_id: str) -> bool:
    """특정 프롬프트 템플릿을 활성화합니다."""
    prompt = await session.get(PromptTemplate, prompt_id)
    if not prompt:
        return False
    
    # 같은 이름의 다른 프롬프트들 비활성화
    stmt = select(PromptTemplate).where(PromptTemplate.name == prompt.name, PromptTemplate.is_active == True)
    result = await session.execute(stmt)
    existing_prompts = result.scalars().all()
    
    for existing in existing_prompts:
        existing.is_active = False
    
    # 선택한 프롬프트 활성화
    prompt.is_active = True
    await session.commit()
    return True
