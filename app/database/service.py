from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database.models import AppUser, Conversation, Message, PromptTemplate, PromptLog, UserSummary, RiskState
from app.utils.utils import session_expired
from datetime import datetime
from typing import Optional, List, Any
from uuid import UUID
from loguru import logger
from zoneinfo import ZoneInfo
import logging

logger = logging.getLogger(__name__)

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
            logger.info(f"\n[생성] 새 사용자 생성: {user_id} | 이름: {user_name}")
            user = AppUser(user_id=user_id, user_name=user_name)
            session.add(user)
            try:
                await session.commit()
                logger.info(f"\n[완료] 새 사용자 생성 완료: {user_id}")
            except Exception:
                await session.rollback()
                raise
            await session.refresh(user)
        elif user_name is not None:  # 이름이 제공되면 업데이트 (UPDATE)
            logger.info(f"\n[변경] 사용자 이름 변경: {user_id} | '{user.user_name}' -> '{user_name}'")
            user.user_name = user_name
            try:
                await session.commit()
                logger.info(f"\n[완료] 사용자 이름 변경 완료: {user_id} -> {user_name}")
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
            # 새 conversation 생성
            conv = Conversation(user_id=user_id)
            session.add(conv)
            try:
                await session.commit()
                logger.info(f"[CONV] 새 conversation 생성 완료: user_id={user_id}, conv_id={conv.conv_id}")
            except Exception as commit_err:
                logger.error(f"[CONV] conversation 생성 커밋 실패: {commit_err}")
                await session.rollback()
                raise
            await session.refresh(conv)
            logger.info(f"[CONV] conversation refresh 완료: conv_id={conv.conv_id}")
        else:
            logger.info(f"[CONV] 기존 conversation 사용: conv_id={conv.conv_id}")
            
        return conv
    except Exception as e:
        logger.error(f"[CONV] get_or_create_conversation 실패: {e}")
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
        # conv_id가 None이면 재조회 시도
        if conv_id is None and user_id:
            try:
                from app.database.models import Conversation as DBConversation
                from sqlalchemy import select
                stmt = select(DBConversation).where(DBConversation.user_id == user_id).order_by(DBConversation.created_at.desc()).limit(1)
                result = await session.execute(stmt)
                conv_obj = result.scalar_one_or_none()
                if conv_obj:
                    conv_id = conv_obj.conv_id
                    logger.info(f"[SAVE_MESSAGE] conv_id 재조회 완료: {conv_id}")
                else:
                    logger.warning(f"[SAVE_MESSAGE] 사용자의 대화 세션을 찾을 수 없음: {user_id}")
                    raise ValueError(f"No conversation found for user: {user_id}")
            except Exception as e:
                logger.error(f"[SAVE_MESSAGE] conv_id 재조회 실패: {e}")
                raise ValueError(f"Failed to retrieve conv_id for user: {user_id}")
        
        # temp_로 시작하는 conv_id는 처리하지 않음
        if conv_id and str(conv_id).startswith("temp_"):
            logger.warning(f"[SAVE_MESSAGE] Skipping temp conv_id: {conv_id}")
            raise ValueError(f"Cannot save message for temporary conversation: {conv_id}")
        
        # conv_id를 UUID로 변환
        from uuid import UUID
        conv_uuid = conv_id if isinstance(conv_id, UUID) else UUID(str(conv_id))
        
        # user_id가 비었으면 conv_id로 보강 시도
        if not user_id:
            try:
                from app.database.models import Conversation as DBConversation
                conv_obj = await session.get(DBConversation, conv_uuid)
                if conv_obj and conv_obj.user_id:
                    user_id = conv_obj.user_id
            except Exception:
                try:
                    await session.rollback()
                    conv_obj = await session.get(DBConversation, conv_uuid)
                    if conv_obj and conv_obj.user_id:
                        user_id = conv_obj.user_id
                except Exception:
                    pass
        
        msg = Message(
            conv_id=conv_uuid,  # UUID로 변환된 값 사용
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
        # conv_id를 UUID로 변환
        conv_uuid = conv_id if isinstance(conv_id, UUID) else UUID(str(conv_id)) if conv_id else None
        
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
    conv_id: UUID | str | None = None,
    source: Any | None = None,
) -> bool:
    """로그 메시지를 저장합니다. 별도 세션을 사용하여 기존 트랜잭션과 충돌하지 않습니다."""
    try:
        from app.database.models import LogMessage
        from uuid import UUID
        
        # conv_id 검증 및 정리
        conv_uuid = None
        try:
            # temp_ 접두사가 있거나 문자열로 변환할 수 없는 경우 None으로 처리
            if conv_id and isinstance(conv_id, str) and conv_id.startswith("temp_"):
                conv_uuid = None
                logger.info(f"[LOG] temp_ 접두사 감지, conv_id를 None으로 설정: {conv_id}")
            elif conv_id:
                conv_uuid = conv_id if isinstance(conv_id, UUID) else UUID(str(conv_id))
            else:
                conv_uuid = None
        except Exception as conv_error:
            logger.warning(f"[LOG] conv_id 변환 실패, None으로 설정: {conv_id}, error: {conv_error}")
            conv_uuid = None
            
        # user_id를 문자열로 변환 (UUID 객체인 경우)
        user_id_str = str(user_id) if user_id else None
        
        log_msg = LogMessage(
            level=level,
            message=message,
            user_id=user_id_str,
            conv_id=conv_uuid,
            source=source
        )
        
        # 별도 세션을 사용하여 로그 메시지 저장
        from app.database.db import get_session
        async for s in get_session():
            try:
                s.add(log_msg)
                await s.commit()
                logger.info(f"[LOG] 로그 메시지 저장 성공: level={level}, user_id={user_id_str}, conv_id={conv_uuid}")
                return True
            except Exception as commit_error:
                logger.warning(f"save_log_message 커밋 실패: {commit_error}")
                try:
                    await s.rollback()
                except Exception:
                    pass
                break
                
        # 별도 세션 실패 시 기존 세션에 추가 시도 (fallback)
        try:
            session.add(log_msg)
            # 기존 세션은 호출자가 관리하므로 커밋하지 않음
            logger.info(f"[LOG] fallback으로 기존 세션에 로그 추가: level={level}, user_id={user_id_str}, conv_id={conv_uuid}")
            return True
        except Exception as fallback_error:
            logger.error(f"save_log_message fallback 실패: {fallback_error}")
            return False
            
    except Exception as e:
        logger.error(f"save_log_message 전체 실패: {e}")
        return False



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

async def get_latest_ai_response(session: AsyncSession, conv_id: UUID) -> str | None:
    """대화에서 가장 최근 AI 응답 조회"""
    try:
        from app.database.models import Message, MessageRole
        
        # 가장 최근 AI 응답 조회
        result = await session.execute(
            select(Message.content)
            .where(Message.conv_id == conv_id, Message.role == MessageRole.ASSISTANT)
            .order_by(Message.created_at.desc())
            .limit(1)
        )
        
        latest_response = result.scalar_one_or_none()
        if latest_response:
            logger.debug(f"\n[조회] 최근 AI 응답: {latest_response[:50]}...")
        return latest_response
        
    except Exception as e:
        logger.warning(f"\n[경고] 최근 AI 응답 조회 실패: {e}")
        return None

async def get_or_create_risk_state(session: AsyncSession, user_id: str) -> RiskState:
    """사용자의 위험도 상태를 조회하거나 생성합니다."""
    try:
        logger.info(f"[RISK_DB] get_or_create_risk_state 시작: user_id={user_id}")
        
        # 먼저 AppUser가 존재하는지 확인하고, 없다면 생성
        user = await session.get(AppUser, user_id)
        if not user:
            logger.info(f"[RISK_DB] AppUser가 존재하지 않음, 새로 생성: {user_id}")
            user = AppUser(user_id=user_id)
            session.add(user)
            try:
                await session.commit()
                await session.refresh(user)
                logger.info(f"[RISK_DB] AppUser 생성 완료: {user_id}")
            except Exception as user_error:
                logger.error(f"[RISK_DB] AppUser 생성 실패: {user_error}")
                await session.rollback()
                raise
        else:
            logger.info(f"[RISK_DB] AppUser 이미 존재: {user_id}")
        
        # RiskState 조회 또는 생성
        risk_state = await session.get(RiskState, user_id)
        if not risk_state:
            logger.info(f"[RISK_DB] RiskState가 존재하지 않음, 새로 생성: {user_id}")
            risk_state = RiskState(user_id=user_id, score=0)
            session.add(risk_state)
            try:
                await session.commit()
                logger.info(f"[RISK_DB] RiskState 생성 완료: {user_id}")
            except Exception as risk_error:
                logger.error(f"[RISK_DB] RiskState 생성 실패: {risk_error}")
                await session.rollback()
                raise
            await session.refresh(risk_state)
        else:
            logger.info(f"[RISK_DB] RiskState 이미 존재: {user_id}, score={risk_state.score}")
        
        return risk_state
        
    except Exception as e:
        logger.error(f"[RISK_DB] get_or_create_risk_state 전체 실패: {e}")
        try:
            await session.rollback()
        except Exception:
            pass
        raise

async def update_risk_score(session: AsyncSession, user_id: str, score: int) -> RiskState:
    """사용자의 위험도 점수를 업데이트합니다."""
    try:
        logger.info(f"[RISK_DB] 위험도 점수 업데이트 시작: user_id={user_id}, score={score}")
        
        risk_state = await get_or_create_risk_state(session, user_id)
        logger.info(f"[RISK_DB] RiskState 조회/생성 완료: {risk_state.user_id}")
        
        # 점수 업데이트
        risk_state.score = score
        risk_state.last_updated = datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)
        
        logger.info(f"[RISK_DB] 점수 및 시간 업데이트 완료: score={risk_state.score}, last_updated={risk_state.last_updated}")
        
        try:
            await session.commit()
            logger.info(f"[RISK_DB] 커밋 성공")
        except Exception as commit_error:
            logger.error(f"[RISK_DB] 커밋 실패: {commit_error}")
            await session.rollback()
            raise
        
        await session.refresh(risk_state)
        logger.info(f"[RISK_DB] RiskState 새로고침 완료: 최종 score={risk_state.score}")
        return risk_state
        
    except Exception as e:
        logger.error(f"[RISK_DB] update_risk_score 전체 실패: {e}")
        try:
            await session.rollback()
        except Exception:
            pass
        raise

async def mark_check_question_sent(session: AsyncSession, user_id: str) -> None:
    """체크 질문이 발송되었음을 표시하고 턴 카운트를 20으로 설정합니다."""
    try:
        risk_state = await get_or_create_risk_state(session, user_id)
        risk_state.check_question_sent = True
        risk_state.check_question_turn = 20  # 20턴 카운트다운 시작
        try:
            await session.commit()
            logger.info(f"[RISK_DB] 체크 질문 발송 기록 완료: user_id={user_id}, check_question_turn={risk_state.check_question_turn}")
        except Exception:
            await session.rollback()
            raise
    except Exception:
        try:
            await session.rollback()
        except Exception:
            pass
        raise

async def decrement_check_question_turn(session: AsyncSession, user_id: str) -> None:
    """체크 질문 턴 카운트를 1 감소시킵니다 (0 밑으로 내려가지 않음)."""
    try:
        risk_state = await get_or_create_risk_state(session, user_id)
        old_turn = risk_state.check_question_turn
        risk_state.check_question_turn = max(0, risk_state.check_question_turn - 1)
        
        if old_turn != risk_state.check_question_turn:
            try:
                await session.commit()
                logger.info(f"[RISK_DB] 체크 질문 턴 카운트 감소: user_id={user_id}, {old_turn} -> {risk_state.check_question_turn}")
            except Exception:
                await session.rollback()
                raise
    except Exception:
        try:
            await session.rollback()
        except Exception:
            pass
        raise

async def get_check_question_turn(session: AsyncSession, user_id: str) -> int:
    """사용자의 체크 질문 턴 카운트를 조회합니다."""
    try:
        risk_state = await get_or_create_risk_state(session, user_id)
        return risk_state.check_question_turn
    except Exception:
        return 0

async def reset_check_question_state(session: AsyncSession, user_id: str) -> None:
    """체크 질문 관련 상태를 초기화합니다."""
    try:
        risk_state = await get_or_create_risk_state(session, user_id)
        old_score = risk_state.last_check_score
        old_turn = risk_state.check_question_turn
        
        risk_state.last_check_score = None
        risk_state.check_question_turn = 0
        risk_state.check_question_sent = False
        
        try:
            await session.commit()
            logger.info(f"[RISK_DB] 체크 질문 상태 초기화 완료: user_id={user_id}, last_check_score={old_score}->None, check_question_turn={old_turn}->0")
        except Exception:
            await session.rollback()
            raise
    except Exception:
        try:
            await session.rollback()
        except Exception:
            pass
        raise

async def update_check_response(session: AsyncSession, user_id: str, check_score: int) -> None:
    """체크 질문 응답 점수를 업데이트합니다."""
    try:
        logger.info(f"[RISK_DB] 체크 응답 업데이트 시작: user_id={user_id}, check_score={check_score}")
        
        risk_state = await get_or_create_risk_state(session, user_id)
        logger.info(f"[RISK_DB] RiskState 조회/생성 완료: {risk_state.user_id}")
        
        risk_state.last_check_score = check_score
        risk_state.check_question_sent = False  # 응답을 받았으므로 리셋
        logger.info(f"[RISK_DB] 체크 응답 점수 및 상태 업데이트 완료: last_check_score={risk_state.last_check_score}, check_question_sent={risk_state.check_question_sent}")
        
        try:
            await session.commit()
            logger.info(f"[RISK_DB] 체크 응답 커밋 성공")
        except Exception as commit_error:
            logger.error(f"[RISK_DB] 체크 응답 커밋 실패: {commit_error}")
            await session.rollback()
            raise
        
        await session.refresh(risk_state)
        logger.info(f"[RISK_DB] RiskState 새로고침 완료: 최종 last_check_score={risk_state.last_check_score}")
        
    except Exception as e:
        logger.error(f"[RISK_DB] update_check_response 전체 실패: {e}")
        try:
            await session.rollback()
        except Exception:
            pass
        raise
