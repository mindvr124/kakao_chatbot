from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from collections import defaultdict
from typing import Optional, Dict

from app.risk_mvp import calculate_risk_score, should_send_check_question, get_check_questions, parse_check_response, get_risk_level, RiskHistory, get_check_response_message, get_check_response_guidance
from app.database.db import get_session
from app.schemas.schemas import simple_text, callback_waiting_response
from app.database.service import upsert_user, get_or_create_conversation, save_message, save_log_message, get_or_create_risk_state, update_risk_score, mark_check_question_sent, update_check_response
from app.database.models import AppUser
from app.utils.utils import extract_user_id, extract_callback_url, remove_markdown
from app.core.ai_service import ai_service
from app.core.background_tasks import _save_user_message_background, _save_ai_response_background, update_last_activity, _send_callback_response
from app.core.summary import maybe_rollup_user_summary
from app.main import BUDGET, ENABLE_CALLBACK
import time
import asyncio

"""카카오 스킬 관련 라우터"""
import asyncio
import random
import re

# 이름 추출을 위한 정규식 패턴들
_NAME_PREFIX_PATTERN = re.compile(r'^(내\s*이름은|제\s*이름은|난|나는|저는|전|내|제|나|저|나를를)\s*', re.IGNORECASE)
_NAME_SUFFIX_PATTERN = re.compile(r'\s*(입니다|이에요|예요|에요|야|이야|라고\s*해|라고\s*해요|이라고\s*해|이라고\s*해요|합니다|불러|불러줘|라고\s*불러|라고\s*불러줘|이라고\s*불러|이라고\s*불러줘)\.?$', re.IGNORECASE)
_NAME_REQUEST_PATTERN = re.compile(r'([가-힣]{2,4})\s*라고\s*불러', re.IGNORECASE)
_KOREAN_NAME_PATTERN = re.compile(r'[가-힣]{2,4}')

# 웰컴 메시지 템플릿
_WELCOME_MESSAGES = [
    "안녕~ 난 나온이야🦉 너는 이름이 뭐야?",
    "안녕~ 난 나온이야🦉 내가 뭐라고 부르면 좋을까?",
    "안녕~ 난 나온이야🦉 네 이름이 궁금해. 알려줘~!"
]

# 인삿말 패턴
_GREETINGS = {
    "안녕", "ㅎㅇ", "반가워", "하이", "헬로", "hi", "hello",
    "안녕하세요", "안녕하십니까", "반갑습니다", "처음뵙겠습니다",
    "ㅎㅎ", "ㅋㅋ", "ㅎㅎㅎ", "ㅋㅋㅋ", "야", "나온아", "넌 누구니",
    "너 누구야", "너는 누구야", "너는 누구니"
}

def extract_korean_name(text: str) -> str | None:
    """사용자 입력에서 한글 이름을 추출합니다."""
    # 입력 정규화
    text = text.strip()
    if not text:
        return None
    
    # 1) "나를 마에다라고 불러줘" 같은 명시적 패턴 우선 확인
    name_request_match = _NAME_REQUEST_PATTERN.search(text)
    if name_request_match:
        extracted_name = name_request_match.group(1)  # 그룹 1에서 이름 추출
        logger.info(f"\n[명시패턴] '나를 ~라고 불러' 패턴에서 이름 추출: '{extracted_name}'")
        return extracted_name
        
    # 2) 기존 패턴으로 fallback
    # 앞뒤 패턴 제거
    text = _NAME_PREFIX_PATTERN.sub('', text)
    text = _NAME_SUFFIX_PATTERN.sub('', text)
    
    # 남은 텍스트에서 한글 이름 패턴 찾기
    match = _KOREAN_NAME_PATTERN.search(text)
    if match:
        return match.group()
    return None

def test_name_extraction(text: str) -> dict:
    """이름 추출 테스트용 함수"""
    logger.info(f"\n[테스트] 이름 추출 테스트: '{text}'")
    
    # 패턴 제거 테스트
    text_after_prefix = _NAME_PREFIX_PATTERN.sub('', text)
    text_after_suffix = _NAME_SUFFIX_PATTERN.sub('', text_after_prefix)
    text_cleaned = text_after_suffix.strip()
    
    # 한글 이름 패턴 매치
    name_match = _KOREAN_NAME_PATTERN.search(text_cleaned)
    extracted_name = name_match.group() if name_match else None
    
    # 정리된 이름
    cleaned_name = clean_name(extracted_name) if extracted_name else None
    is_valid = is_valid_name(cleaned_name) if cleaned_name else False
    
    result = {
        'original': text,
        'after_prefix_removal': text_after_prefix,
        'after_suffix_removal': text_after_suffix,
        'cleaned_text': text_cleaned,
        'extracted_name': extracted_name,
        'cleaned_name': cleaned_name,
        'is_valid': is_valid
    }
    
    # 핵심 결과만 간단하게 로깅
    if extracted_name:
        logger.info(f"\n[성공] 이름 추출 성공: '{extracted_name}' -> '{cleaned_name}' (유효: {is_valid})")
    else:
        logger.info(f"\n[실패] 이름 추출 실패: '{text}'")
    
    return result
    
router = APIRouter()


# ====== [이름 저장 보조 유틸] =================================================

# 허용 문자(한글/영문/숫자/중점/하이픈/언더스코어), 길이 1~20
NAME_ALLOWED = re.compile(r"^[가-힣a-zA-Z0-9·\-\_]{1,20}$")

def clean_name(s: str) -> str:
    s = s.strip()
    # 양쪽 따옴표/괄호/장식 제거
    s = re.sub(r'[\"\'“”‘’()\[\]{}<>…~]+', "", s)
    return s.strip()

def is_valid_name(s: str) -> bool:
    return bool(NAME_ALLOWED.fullmatch(s))

class PendingNameCache:
    """간단한 in-memory 캐시 (운영에선 Redis/DB 권장)"""
    _store: dict[str, float] = {}
    TTL_SECONDS = 300  # 5분

    @classmethod
    def set_waiting(cls, user_id: str):
        cls._store[user_id] = time.time() + cls.TTL_SECONDS
        logger.info(f"\n[대기] 이름 대기 상태 설정: {user_id}")

    @classmethod
    def is_waiting(cls, user_id: str) -> bool:
        exp = cls._store.get(user_id)
        if not exp:
            return False
        if time.time() > exp:
            try:
                del cls._store[user_id]
            except Exception:
                pass
            return False
        return True

    @classmethod
    def clear(cls, user_id: str):
        was_waiting = user_id in cls._store
        cls._store.pop(user_id, None)
        if was_waiting:
            logger.info(f"\n[해제] 이름 대기 상태 해제: {user_id}")

async def save_user_name(session: AsyncSession, user_id: str, name: str):
    """appuser.user_name 저장/갱신 (INSERT 또는 UPDATE)"""
    logger.info(f"\n[저장] 이름 저장 시도: {user_id} -> {name}")
    
    # upsert_user는 사용자가 없으면 INSERT, 있으면 UPDATE를 수행
    user = await upsert_user(session, user_id, name)
    
    # 이미 commit이 되었으므로 추가 commit 불필요
    operation = 'INSERT' if not user.user_name else 'UPDATE'
    logger.info(f"\n[완료] 이름 저장 완료: {user_id} -> {name} ({operation})")
    
    # 이름 변경 완료 로그 저장
    try:
        success = await save_log_message(
            session=session,
            level="INFO",
            message=f"사용자 이름이 '{name}'으로 변경되었습니다.",
            user_id=user_id,
            source="name_update"
        )
        
        if success:
            logger.info(f"\n[로그] 이름 변경 로그 저장 완료: {user_id}")
        else:
            logger.warning(f"\n[경고] 이름 변경 로그 저장 실패: {user_id}")
            
    except Exception as e:
        logger.error(f"\n[오류] 이름 변경 로그 저장 중 오류: {e}")

def kakao_text(text: str) -> JSONResponse:
    return JSONResponse(
        content={
            "version": "2.0",
            "template": {"outputs": [{"simpleText": {"text": text}}]}
        },
        media_type="application/json; charset=utf-8"
    )

# 사용자별 위험도 히스토리 관리
_RISK_HISTORIES: Dict[str, RiskHistory] = {}

async def handle_name_flow(
    session: AsyncSession, 
    user_id: str, 
    user_text: str, 
    x_request_id: str,
    conv_id: str | None = None
) -> Optional[JSONResponse]:
    """
    이름 관련 플로우를 처리합니다.
    
    Returns:
        JSONResponse: 이름 관련 응답이 필요한 경우
        None: 이름 관련 처리가 필요없는 경우 (정상 대화 진행)
    """
    try:
        user = await session.get(AppUser, user_id)
        user_name = user.user_name if user else None
        is_waiting = PendingNameCache.is_waiting(user_id)
        
        logger.info(f"\n[상태] 사용자 상태: {user_id} | 이름: {user_name} | 대기중: {is_waiting}")
        logger.info(f"\n[입력] 사용자 입력: '{user_text}'")
        
        # ====== [이름 없는 사용자 처리] ==================================
        if user is None or user.user_name is None:
            # 이름을 기다리는 중이었다면 이름 저장 시도
            if PendingNameCache.is_waiting(user_id):
                logger.info(f"\n[처리] 이름 입력 처리 중: '{user_text}'")
                
                # 이름 추출 테스트 실행
                test_result = test_name_extraction(user_text)
                
                name = test_result['extracted_name']
                if name:
                    cand = test_result['cleaned_name']
                    if test_result['is_valid']:
                        logger.info(f"\n[검증] 이름 검증 통과: '{cand}', 저장 시작...")
                        
                        try:
                            await save_user_name(session, user_id, cand)
                            PendingNameCache.clear(user_id)
                            try:
                                await save_log_message(session, "name_saved", str(user_id), conv_id, x_request_id, {"source": "name_flow", "name": cand, "mode": "first_chat"})
                            except Exception:
                                pass
                            return kakao_text(f"반가워 {cand}아(야)! 앞으로 {cand}(이)라고 부를게🦉")
                        except Exception as e:
                            logger.bind(x_request_id=x_request_id).exception(f"[오류] 이름 저장 실패: {e}")
                            PendingNameCache.clear(user_id)
                    else:
                        logger.warning(f"[형식] 이름 형식 오류: '{cand}'")
                        return kakao_text("이름 형식은 한글/영문 1~20자로 입력해줘!\n예) 민수, Yeonwoo")
                else:
                    logger.info(f"\n[추출] 이름 추출 실패: '{user_text}'")
                    return kakao_text("불리고 싶은 이름을 알려줘! 그럼 나온이가 꼭 기억할게~")
            
            # 인삿말이 오면 웰컴 메시지로 응답
            elif any(greeting in user_text.lower() for greeting in _GREETINGS):
                logger.info(f"\n[인사] 인삿말 감지: '{user_text}' -> 이름 대기 상태 설정")
                PendingNameCache.set_waiting(user_id)
                try:
                    await save_log_message(session, "name_wait_start", user_id, None, x_request_id, None)
                except Exception:
                    pass
                return kakao_text(random.choice(_WELCOME_MESSAGES))
            else:
                # 이름을 물어보는 메시지 전송
                logger.info(f"\n[질문] 인삿말 아님: '{user_text}' -> 이름 대기 상태 설정")
                PendingNameCache.set_waiting(user_id)
                try:
                    await save_log_message(session, "name_wait_start", user_id, None, x_request_id, None)
                except Exception:
                    pass
                return kakao_text("안녕! 처음 보네~ 나는 나온이야 🦉\n불리고 싶은 이름을 알려주면, 앞으로 그렇게 불러줘!")
        
        # ====== [이름 플로우: 최우선 인터셉트] ==================================
        # 대화 세션 생성 (이름 플로우에서 필요)
        try:
            conv = await get_or_create_conversation(session, user_id)
        except Exception as e:
            logger.warning(f"\n[경고] 대화 세션 생성 실패: {e}")
            conv = None
        
        # 2-1) '/이름' 명령만 온 경우 → 다음 발화를 이름으로 받기
        if user_text == "/이름":
            PendingNameCache.set_waiting(user_id)
            try:
                await save_log_message(session, "name_wait_start", user_id, None, x_request_id, None)
            except Exception:
                pass
            return kakao_text("불리고 싶은 이름을 입력해줘! 그럼 나온이가 꼭 기억할게~")
        
        # 2-1.5) 사용자 발화에 '이름'이 들어가고 AI가 이름을 요청한 경우 → 이름 대기 상태 설정
        # 먼저 이름 대기 상태인지 확인 (이전 요청에서 설정된 경우)
        if PendingNameCache.is_waiting(user_id):
            logger.info(f"\n[대기] 이름 대기 상태에서 입력 처리: '{user_text}'")
            
            # 취소 지원
            if user_text in ("취소", "그만", "아냐", "아니야", "됐어", "아니"):
                PendingNameCache.clear(user_id)
                try:
                    await save_log_message(session, "name_wait_cancel", user_id, None, x_request_id, None)
                except Exception:
                    pass
                return kakao_text("좋아, 다음에 다시 알려줘!")
            
            # 이름 변경 처리 (기존 사용자 + 새 사용자 모두)
            cand = clean_name(user_text)
            if not is_valid_name(cand):
                return kakao_text("이름 형식은 한글/영문 1~20자로 입력해줘!\n예) 민수, Yeonwoo")
            
            try:
                await save_user_name(session, user_id, cand)
                PendingNameCache.clear(user_id)
                try:
                    await save_log_message(session, "name_saved", user_id, None, x_request_id, {"name": cand, "mode": "ai_name_request"})
                except Exception:
                    pass
                return kakao_text(f"이름 예쁘다! 앞으로는 '{cand}'(이)라고 불러줄게~")
            except Exception as name_err:
                logger.bind(x_request_id=x_request_id).exception(f"save_user_name failed: {name_err}")
                PendingNameCache.clear(user_id)
                return kakao_text("앗, 이름을 저장하는 중에 문제가 생겼나봐. 잠시 후 다시 시도해줘!")
        
        # 2-1.6) 사용자 발화에 '이름'이 들어가고 AI가 이름을 요청한 경우 → 이름 대기 상태 설정
        if user and user.user_name and "이름" in user_text and conv:
            logger.info(f"\n[검사] 사용자 발화에 '이름' 포함: '{user_text}'")
            
            # AI 응답을 먼저 생성
            try:
                # AI 응답 생성
                ai_response, tokens_used = await ai_service.generate_response(
                    session=session,
                    conv_id=conv.conv_id,
                    user_input=user_text,
                    prompt_name="default",
                    user_id=user_id
                )
                
                logger.info(f"\n[AI생성] AI 응답 생성: {ai_response[:100]}...")
                
                # AI 응답에서 이름 요청 패턴 확인
                name_request_patterns = ["불리고 싶은", "뭐라고 부르면", "이름이 뭐", "이름 알려줘"]
                matched_patterns = [pattern for pattern in name_request_patterns if pattern in ai_response]
                
                if matched_patterns:
                    logger.info(f"\n[감지] 이름 요청 패턴 발견: {matched_patterns}")
                    
                    # 이름 대기 상태 설정 - 다음 사용자 입력을 이름으로 받기
                    PendingNameCache.set_waiting(user_id)
                    try:
                        await save_log_message(session, "name_change_request", user_id, None, x_request_id, {
                            "current_name": user.user_name, 
                            "trigger": "ai_name_request",
                            "matched_patterns": matched_patterns,
                            "ai_response": ai_response[:200]
                        })
                    except Exception:
                        pass
                    
                    # AI 응답을 데이터베이스에 저장 (중복 저장 방지)
                    try:
                        if not str(conv.conv_id).startswith("temp_") and conv.conv_id:
                            # 사용자 메시지 저장
                            await save_message(session, conv.conv_id, "user", user_text, x_request_id, None, user_id)
                            # AI 응답 저장
                            await save_message(session, conv.conv_id, "assistant", ai_response, x_request_id, tokens_used, user_id)
                            logger.info(f"\n[저장] 이름 변경 요청 대화 저장 완료: conv_id={conv.conv_id}")
                    except Exception as save_err:
                        logger.warning(f"\n[경고] 대화 저장 실패: {save_err}")
                    
                    # AI 응답을 그대로 반환
                    return JSONResponse(content={
                        "version": "2.0",
                        "template": {"outputs":[{"simpleText":{"text": ai_response}}]}
                    }, media_type="application/json; charset=utf-8")
                else:
                    logger.info(f"\n[감지] 이름 요청 패턴 없음 - 일반 대화로 진행")
                    
                    # AI 응답을 데이터베이스에 저장 (중복 저장 방지)
                    try:
                        if not str(conv.conv_id).startswith("temp_") and conv.conv_id:
                            # 사용자 메시지 저장
                            await save_message(session, conv.conv_id, "user", user_text, x_request_id, None, user_id)
                            # AI 응답 저장
                            await save_message(session, conv.conv_id, "assistant", ai_response, x_request_id, tokens_used, user_id)
                            logger.info(f"\n[저장] 일반 대화 저장 완료: conv_id={conv.conv_id}")
                    except Exception as save_err:
                        logger.warning(f"\n[경고] 대화 저장 실패: {save_err}")
                    
                    # AI 응답을 그대로 반환
                    return JSONResponse(content={
                        "version": "2.0",
                        "template": {"outputs":[{"simpleText":{"text": ai_response}}]}
                    }, media_type="application/json; charset=utf-8")
                    
            except Exception as e:
                logger.warning(f"\n[경고] AI 응답 생성 중 오류: {e}")
                # AI 생성 실패 시 fallback으로 진행
                
            # AI 응답 확인 실패 또는 패턴 불일치 시 기존 로직으로 fallback
            # 더 유연한 패턴 매칭: "다른 이름"이 포함된 모든 표현
            if ("이름" in user_text and "다른" in user_text) or \
               ("이름" in user_text and "바꿔" in user_text) or \
               ("이름" in user_text and "바꿀" in user_text) or \
               ("이름" in user_text and "변경" in user_text) or \
               user_text in ["다른이름", "다른 이름", "이름 바꿔", "이름 바꿀래", "이름 바꾸고 싶어"]:
                logger.info(f"\n[fallback] 명시적 이름 변경 요청 감지")
                current_name = user.user_name
                PendingNameCache.set_waiting(user_id)
                try:
                    await save_log_message(session, "name_change_request", user_id, None, x_request_id, {"current_name": current_name, "trigger": "explicit_request"})
                except Exception:
                    pass
                return kakao_text(f"현재 '{current_name}'으로 알고 있는데, 어떤 이름으로 바꾸고 싶어?")
        
        # 2-1.7) "~라고 불러줘" 패턴에서 이름 추출 (모든 사용자 발화에서 검사)
        if user and conv and not PendingNameCache.is_waiting(user_id):
            # "~라고 불러줘" 패턴 검사
            name_request_match = _NAME_REQUEST_PATTERN.search(user_text)
            if name_request_match:
                extracted_name = name_request_match.group(1)  # 그룹 1에서 이름 추출
                logger.info(f"\n[패턴감지] '~라고 불러' 패턴에서 이름 추출: '{extracted_name}'")
                
                if extracted_name and is_valid_name(extracted_name):
                    # 현재 저장된 이름과 다른 경우에만 저장
                    if user.user_name != extracted_name:
                        # commit 전에 user_name 값을 미리 복사 (expire_on_commit 방지)
                        old_name = user.user_name
                        try:
                            await save_user_name(session, user_id, extracted_name)
                            try:
                                await save_log_message(session, "name_auto_extracted", user_id, None, x_request_id, {
                                    "old_name": old_name,
                                    "new_name": extracted_name,
                                    "trigger": "pattern_detection"
                                })
                            except Exception:
                                pass
                            logger.info(f"\n[패턴저장] 이름 패턴 저장 완료: '{old_name}' -> '{extracted_name}'")
                        except Exception as e:
                            logger.warning(f"\n[경고] 이름 패턴 저장 실패: {e}")
                    else:
                        logger.info(f"\n[패턴감지] 이미 동일한 이름: '{extracted_name}'")
                else:
                    logger.warning(f"\n[패턴감지] 추출된 이름이 유효하지 않음: '{extracted_name}'")

        # 2-2) '/이름 xxx' 형태 → 즉시 저장 시도
        if user_text.startswith("/이름 "):
            raw = user_text[len("/이름 "):]
            cand = clean_name(raw)
            if not is_valid_name(cand):
                return kakao_text("이름 형식은은 한글/영문 1~20자로 입력해줘!\n예) 민수, Yeonwoo")
            try:
                await save_user_name(session, user_id, cand)
                try:
                    await save_log_message(session, "name_saved", user_id, None, x_request_id, {"name": cand, "mode": "slash_inline"})
                except Exception:
                    pass
                return kakao_text(f"예쁜 이름이다! 앞으로는 {cand}(이)라고 불러줄게~")
            except Exception as name_err:
                logger.bind(x_request_id=x_request_id).exception(f"save_user_name failed: {name_err}")
                return kakao_text("앗, 이름을 저장하는 중에 문제가 생겼나봐. 잠시 후 다시 시도해줘!")

        # 이름 관련 처리가 필요없는 경우
        return None
        
    except Exception as e:
        logger.bind(x_request_id=x_request_id).exception(f"Failed to handle name flow: {e}")
        return None

def _safe_reply_kakao(risk_level: str) -> dict:
    # 위험도 레벨에 따른 안전 응답 생성
    if risk_level == "critical":
        msg = (
            "지금 상황이 매우 심각해 보여. 즉시 도움을 받아야 해.\n"
            "• 자살예방 상담전화 1393 (24시간)\n"
            "• 정신건강 위기상담 1577-0199\n"
            "• 긴급상황: 112/119\n"
            "혼자가 아니야. 지금 당장 연락해줘."
        )
    else:  # high level
        msg = (
            "지금 마음이 많이 힘들어 보여. 혼자가 아니야.\n"
            "• 자살예방 상담전화 1393 (24시간)\n"
            "• 정신건강 위기상담 1577-0199\n"
            "긴급한 상황이면 112/119에 바로 연락해줘."
        )
    return {"version":"2.0","template":{"outputs":[{"simpleText":{"text": msg}}]}}
    
# ====== [스킬 엔드포인트] =====================================================

@router.post("/skill")
@router.post("/skill/")
async def skill_endpoint(request: Request, session: AsyncSession = Depends(get_session)):
    """카카오 스킬 메인 엔드포인트"""
    logger.info("=== SKILL ENDPOINT STARTED ===")
    
    # X-Request-ID 추출 (로깅용)
    x_request_id = request.headers.get("X-Request-ID", "unknown")
    logger.bind(x_request_id=x_request_id).info("Skill endpoint started")
    
    try:
        # 1) 헤더 추적자
        x_request_id = request.headers.get("X-Request-ID") or request.headers.get("X-Request-Id")
        logger.bind(x_request_id=x_request_id).info("Skill request received")

        # 전체 요청 시간 추적 (카카오 5초 제한 준수)
        t0 = time.perf_counter()

        try:
            body_dict = await request.json()
            if not isinstance(body_dict, dict):
                body_dict = {}
        except Exception as parse_err:
            logger.warning(f"JSON parse failed: {parse_err}")
            body_dict = {}
        
        # 서버가 받은 데이터 로깅
        logger.bind(x_request_id=x_request_id).info("Request body received")
        
        user_id = extract_user_id(body_dict)
        logger.bind(x_request_id=x_request_id).info(f"Extracted user_id: {user_id}")
        
        # LogMessage에도 저장
        try:
            await save_log_message(session, "INFO", "SKILL REQUEST RECEIVED", str(user_id), None, {"source": "skill_endpoint"})
        except Exception:
            pass

        # 폴백: user_id가 비어있으면 익명 + X-Request-ID 사용
        if not user_id:
            anon_suffix = x_request_id or "unknown"
            user_id = f"anonymous:{anon_suffix}"
            logger.bind(x_request_id=x_request_id).warning(f"user_id missing. fallback -> anonymous")

        callback_url = extract_callback_url(body_dict)
        logger.bind(x_request_id=x_request_id).info("Callback URL extracted")

        # 2) 사용자 발화 추출
        user_text = (body_dict.get("userRequest") or {}).get("utterance", "")
        trace_id = x_request_id
        if not isinstance(user_text, str):
            user_text = str(user_text or "")
        if not user_text:
            user_text = "안녕하세요"
        user_text_stripped = user_text.strip()

        # ====== [대화 세션 생성] ==============================================
        # 대화 세션을 먼저 생성하여 conv_id 확보
        try:
            conv = await get_or_create_conversation(session, user_id)
            conv_id = conv.conv_id
            logger.info(f"[CONV] 대화 세션 생성/조회 완료: conv_id={conv_id}")
        except Exception as e:
            logger.warning(f"[CONV] 대화 세션 생성 실패: {e}")
            conv_id = None
        
        # ====== [자살위험도 분석] ==============================================
        logger.info(f"[RISK_DEBUG] 위험도 분석 시작: text='{user_text_stripped}'")
        
        # 사용자별 위험도 히스토리 가져오기 (없으면 생성)
        if user_id not in _RISK_HISTORIES:
            # 데이터베이스에서 기존 위험도 점수 복원 시도
            try:
                from app.database.service import get_risk_state
                existing_risk = await get_risk_state(session, user_id)
                if existing_risk and existing_risk.score > 0:
                    # 기존 점수가 있으면 초기 턴으로 복원
                    _RISK_HISTORIES[user_id] = RiskHistory(max_turns=20, decay_factor=0.8)
                    # 기존 점수를 첫 번째 턴으로 추가 (가상의 턴으로 복원)
                    virtual_turn = {
                        'text': f"[복원된_기존_점수:{existing_risk.score}점]",
                        'timestamp': existing_risk.last_updated,
                        'score': existing_risk.score,
                        'flags': {'neg': False, 'meta': False, 'third': False, 'idiom': False, 'past': False},
                        'evidence': [{'keyword': '복원된_점수', 'score': existing_risk.score, 'original_score': existing_risk.score, 'excerpt': '데이터베이스에서_복원'}]
                    }
                    _RISK_HISTORIES[user_id].turns.append(virtual_turn)
                    logger.info(f"[RISK_DEBUG] 기존 점수 복원 완료: user_id={user_id}, score={existing_risk.score}, turns_count={len(_RISK_HISTORIES[user_id].turns)}")
                else:
                    _RISK_HISTORIES[user_id] = RiskHistory(max_turns=20, decay_factor=0.8)
                    logger.info(f"[RISK_DEBUG] 새로운 RiskHistory 객체 생성: user_id={user_id}")
            except Exception as e:
                logger.warning(f"[RISK_DEBUG] 기존 점수 복원 실패: {e}")
                _RISK_HISTORIES[user_id] = RiskHistory(max_turns=20, decay_factor=0.8)
                logger.info(f"[RISK_DEBUG] 새로운 RiskHistory 객체 생성 (복원 실패): user_id={user_id}")
        
        user_risk_history = _RISK_HISTORIES[user_id]
        logger.info(f"[RISK_DEBUG] RiskHistory 객체 확인: {type(user_risk_history)}, max_turns={user_risk_history.max_turns}, turns_count={len(user_risk_history.turns)}")
        
        risk_score, flags, evidence = calculate_risk_score(user_text_stripped, user_risk_history)
        logger.info(f"[RISK_DEBUG] 위험도 계산 결과: score={risk_score}, flags={flags}, evidence={evidence}")
        
        # 누적 점수 계산 (히스토리 기반)
        cumulative_score = user_risk_history.get_cumulative_score()
        logger.info(f"[RISK_DEBUG] 누적 위험도 점수: {cumulative_score}")
        
        # 히스토리 상태 상세 로깅
        logger.info(f"[RISK_DEBUG] 히스토리 상태: turns_count={len(user_risk_history.turns)}, last_updated={user_risk_history.last_updated}")
        if user_risk_history.turns:
            recent_turns = list(user_risk_history.turns)[-3:]  # 최근 3턴
            for i, turn in enumerate(recent_turns):
                logger.info(f"[RISK_DEBUG] 최근 턴 {i+1}: score={turn['score']}, text='{turn['text'][:30]}...'")
        
        risk_level = get_risk_level(cumulative_score)
        logger.info(f"[RISK_DEBUG] 위험도 레벨: {risk_level}")
        
        # 데이터베이스에 누적 위험도 점수 저장
        try:
            logger.info(f"[RISK_SAVE] 누적 위험도 점수 저장 시도: cumulative_score={cumulative_score}, turn_score={risk_score}")
            await update_risk_score(session, user_id, cumulative_score)
            logger.info(f"[RISK_SAVE] 누적 위험도 점수 저장 성공: cumulative_score={cumulative_score}")
        except Exception as e:
            logger.error(f"[RISK_SAVE] 누적 위험도 점수 저장 실패: cumulative_score={cumulative_score}, error={e}")
            import traceback
            logger.error(f"[RISK_SAVE] 상세 에러: {traceback.format_exc()}")
        
        # 위험도 추세 분석
        risk_trend = user_risk_history.get_risk_trend()
        logger.info(f"[RISK] score={risk_score} level={risk_level} trend={risk_trend} flags={flags}")
        
        # 체크 질문 응답인지 확인
        check_score = parse_check_response(user_text_stripped)
        logger.info(f"[CHECK_DEBUG] 체크 응답 파싱 결과: text='{user_text_stripped}', score={check_score}")
        
        if check_score is not None:
            logger.info(f"[CHECK] 체크 질문 응답 감지: {check_score}점")
            try:
                await update_check_response(session, user_id, check_score)
                logger.info(f"[CHECK] 체크 응답 저장 완료: {check_score}점")
                
                # 체크 응답 점수에 따른 대응
                guidance = get_check_response_guidance(check_score)
                logger.info(f"[CHECK] 대응 가이드: {guidance}")
                
                # 9-10점: 즉시 안전 응답
                if check_score >= 9:
                    logger.info(f"[CHECK] 위험도 9-10점: 즉시 안전 응답 발송")
                    try:
                        await save_log_message(session, "check_response_critical",
                                            str(user_id), conv_id,
                                            x_request_id,
                                            {"source": "check_response", "check_score": check_score, "guidance": guidance})
                    except Exception:
                        pass
                    return JSONResponse(content=_safe_reply_kakao("critical"), media_type="application/json; charset=utf-8")
                
                # 7-8점: 안전 안내 메시지
                elif check_score >= 7:
                    logger.info(f"[CHECK] 위험도 7-8점: 안전 안내 메시지 발송")
                    try:
                        await save_log_message(session, "check_response_high_risk",
                                            str(user_id), conv_id,
                                            x_request_id,
                                            {"source": "check_response", "check_score": check_score, "guidance": guidance})
                    except Exception:
                        pass
                    response_message = get_check_response_message(check_score)
                    logger.info(f"[CHECK] 7-8점 응답 메시지: {response_message}")
                    return JSONResponse(content=kakao_text(response_message), media_type="application/json; charset=utf-8")
                
                # 0-6점: 일반 대응 메시지 후 정상 대화 진행
                else:
                    logger.info(f"[CHECK] 위험도 0-6점: 일반 대응 메시지 발송")
                    try:
                        await save_log_message(session, "check_response_normal",
                                            str(user_id), conv_id,
                                            x_request_id,
                                            {"source": "check_response", "check_score": check_score, "guidance": guidance})
                    except Exception:
                        pass
                    # 체크 응답에 대한 대응 메시지를 보내고 정상 대화로 진행
                    response_message = get_check_response_message(check_score)
                    logger.info(f"[CHECK] 0-6점 응답 메시지: {response_message}")
                    # 체크 응답 대응 메시지 전송
                    return JSONResponse(content=kakao_text(response_message), media_type="application/json; charset=utf-8")
                    
            except Exception as e:
                logger.error(f"[CHECK] 체크 응답 저장 실패: {e}")
                import traceback
                logger.error(f"[CHECK] 상세 에러: {traceback.format_exc()}")
        else:
            logger.info(f"[CHECK_DEBUG] 체크 질문 응답이 아님: 일반 대화로 진행")

        # 위험도가 높은 경우 안전 응답 (체크 질문 응답이 아닌 경우에만)
        if check_score is None and risk_level in ("critical", "high"):
            try:
                await save_log_message(session, "risk_trigger",
                                    str(user_id), conv_id,
                                    x_request_id,
                                    {"source": "risk_analysis", "level": risk_level, "score": risk_score, "evidence": evidence[:3]})
            except Exception:
                pass

        # 8점 이상이면 체크 질문 발송 (체크 질문 응답이 아닌 경우에만)
        if check_score is None and should_send_check_question(risk_score, user_risk_history):
            logger.info(f"[CHECK] 체크 질문 발송 조건 충족: risk_score={risk_score}")
            try:
                # RiskHistory에 체크 질문 발송 기록
                user_risk_history.mark_check_question_sent()
                logger.info(f"[CHECK] RiskHistory에 체크 질문 발송 기록 완료")
                
                # 데이터베이스에도 기록
                await mark_check_question_sent(session, user_id)
                logger.info(f"[CHECK] 데이터베이스에 체크 질문 발송 기록 완료")
                
                check_questions = get_check_questions()
                selected_question = random.choice(check_questions)
                logger.info(f"[CHECK] 체크 질문 발송: {selected_question}")
                return kakao_text(selected_question)
            except Exception as e:
                logger.error(f"[CHECK] 체크 질문 발송 실패: {e}")
                import traceback
                logger.error(f"[CHECK] 상세 에러: {traceback.format_exc()}")
        elif check_score is None:
            logger.info(f"[CHECK_DEBUG] 체크 질문 발송 조건 미충족: risk_score={risk_score}, should_send={should_send_check_question(risk_score, user_risk_history)}")

        # ====== [이름 플로우 처리] ==============================================
        # 이름 관련 플로우 처리 (conv_id 전달)
        name_response = await handle_name_flow(session, user_id, user_text_stripped, x_request_id, conv_id)
        if name_response:
            return name_response

        # ====== [이름 플로우 끝: 이하 기존 로직 유지] ===========================

        ENABLE_CALLBACK = True   # 기존 설정 사용하던 값에 맞춰주세요
        BUDGET = 4.5             # 기존 타임아웃에 맞춰 조정

        if ENABLE_CALLBACK and callback_url and isinstance(callback_url, str) and callback_url.startswith("http"):
            elapsed = time.perf_counter() - t0
            time_left = max(0.2, 4.5 - elapsed)
            try:
                try:
                    await save_log_message(session, "request_received", str(user_id), conv_id, x_request_id, {"source": "callback", "callback": True})
                except Exception:
                    pass

                async def _ensure_quick_conv():
                    await upsert_user(session, user_id)
                    conv = await get_or_create_conversation(session, user_id)
                    return conv.conv_id

                try:
                    quick_conv_id = await asyncio.wait_for(_ensure_quick_conv(), timeout=min(1.0, time_left - 0.1))
                except Exception:
                    quick_conv_id = f"temp_{user_id}"

                quick_text, quick_tokens = await asyncio.wait_for(
                    ai_service.generate_response(
                        session=session,
                        conv_id=quick_conv_id,
                        user_input=user_text,
                        prompt_name="default",
                        user_id=user_id
                    ),
                    timeout=time_left,
                )

                async def _persist_quick(user_id: str, user_text: str, reply_text: str, request_id: str | None):
                    async for s in get_session():
                        try:
                            await upsert_user(s, user_id)
                            conv = await get_or_create_conversation(s, user_id)
                            try:
                                await save_message(s, conv.conv_id, "user", user_text, trace_id, None, user_id)
                                await save_message(s, conv.conv_id, "assistant", remove_markdown(reply_text), trace_id, quick_tokens, user_id)
                            except Exception:
                                try:
                                    await s.rollback()
                                except Exception:
                                    pass
                                raise
                            try:
                                await maybe_rollup_user_summary(s, user_id, conv.conv_id)
                            except Exception:
                                pass
                            break
                        except Exception as persist_err:
                            try:
                                await s.rollback()
                            except Exception:
                                pass
                            logger.bind(x_request_id=request_id).exception(f"Persist quick path failed: {persist_err}")
                            break

                asyncio.create_task(_persist_quick(user_id, user_text, quick_text, x_request_id))

                try:
                    update_last_activity(quick_conv_id)
                except Exception:
                    pass
                return JSONResponse(content={
                    "version": "2.0",
                    "template": {"outputs":[{"simpleText":{"text": remove_markdown(quick_text)}}]}
                }, media_type="application/json; charset=utf-8")
            except Exception:
                pass

            # 시간 내 미완료시 즉시 콜백 대기 응답 반환
            immediate = {
                "version": "2.0",
                "template": {"outputs":[{"simpleText":{"text":"답변을 생성 중입니다..."}}]},
                "useCallback": True
            }
            try:
                await save_log_message(session, "callback_waiting_sent", str(user_id), conv_id, x_request_id, {"source": "callback"})
            except Exception:
                pass

            import re

            MAX_SIMPLETEXT = 900   # 카카오 안전 마진
            MAX_OUTPUTS    = 3     # 한 번에 보낼 simpleText 개수

            _SENT_ENDERS = ("...", "…", ".", "!", "?", "。", "！", "？")

            def _hard_wrap_sentence(s: str, limit: int) -> list[str]:
                """한 문장이 limit보다 길면 최대한 공백/줄바꿈 기준으로 부드럽게 쪼갠다."""
                out = []
                u = s.strip()
                while len(u) > limit:
                    # 선호도: 줄바꿈 > 공백 > 하드컷
                    cut = u.rfind("\n", 0, limit)
                    if cut < int(limit * 0.6):
                        cut = u.rfind(" ", 0, limit)
                    if cut == -1:
                        cut = limit
                    out.append(u[:cut].rstrip())
                    u = u[cut:].lstrip()
                if u:
                    out.append(u)
                return out

            def split_for_kakao_sentence_safe(text: str, limit: int = MAX_SIMPLETEXT) -> list[str]:
                """
                - 문장 끝(., !, ?, …, 全角句点 등) 또는 빈 줄/줄바꿈 경계를 우선으로 분할
                - 문장이 limit보다 길면 그 문장만 부드럽게 하드랩
                """
                t = remove_markdown(text or "").replace("\r\n", "\n").strip()

                chunks = []
                i, n = 0, len(t)

                while i < n:
                    end = min(i + limit, n)
                    window = t[i:end]

                    if end < n:
                        # 1) 문장부호 경계 찾기
                        cand = -1
                        for p in _SENT_ENDERS:
                            pos = window.rfind(p)
                            cand = max(cand, pos)

                        # 2) 문장부호가 너무 앞이면(=너무 작게 잘릴 위험) 줄바꿈/공백 경계도 고려
                        nl_pos    = window.rfind("\n")
                        space_pos = window.rfind(" ")

                        boundary = cand
                        if boundary < int(limit * 0.4):
                            boundary = max(boundary, nl_pos, space_pos)

                        # 3) 경계가 없으면 하드컷
                        if boundary == -1:
                            boundary = len(window)
                        else:
                            boundary += 1  # 경계 문자 포함

                    else:
                        boundary = len(window)

                    piece = window[:boundary].rstrip()

                    # 만약 "한 문장" 자체가 limit보다 긴 경우엔 부드럽게 랩
                    if len(piece) == boundary and (end < n) and boundary == len(window):
                        # window 안에 경계가 전혀 없어서 통째로 잘린 케이스
                        chunks.extend(_hard_wrap_sentence(piece, limit))
                    else:
                        if not piece:  # 빈 조각 방지
                            piece = t[i:end].strip()
                        if piece:
                            chunks.append(piece)

                    i += len(piece)
                    # 경계 이후의 공백/개행 정리
                    while i < n and t[i] in (" ", "\n"):
                        i += 1

                return [c for c in chunks if c]

            def pack_into_max_outputs(parts: list[str], limit: int, max_outputs: int) -> list[str]:
                """
                이미 limit 이하로 분할된 parts를, 개수를 줄이기 위해 앞에서부터
                가능한 만큼 합치되 각 조각이 limit를 넘지 않게 그리디로 포장.
                """
                if len(parts) <= max_outputs:
                    return parts

                packed = []
                cur = ""
                for p in parts:
                    if not cur:
                        cur = p
                        continue
                    if len(cur) + 1 + len(p) <= limit:
                        cur = f"{cur}\n{p}"
                    else:
                        packed.append(cur)
                        cur = p
                if cur:
                    packed.append(cur)

                # 그래도 많으면 맨 뒤를 잘라내는 대신, 마지막 아이템에 안내 메시지 추가
                if len(packed) > max_outputs:
                    keep = packed[:max_outputs-1]
                    keep.append("※ 내용이 길어 일부만 보냈어. '자세히'라고 보내면 이어서 보여줄게!")
                    return keep
                return packed

            async def _send_callback_response(callback_url: str, text: str, tokens_used: int, request_id: str | None):
                if not callback_url or not isinstance(callback_url, str) or not callback_url.startswith("http"):
                    logger.bind(x_request_id=request_id).error(f"Invalid callback_url: {callback_url!r}")
                    return

                import json, httpx, urllib.request

                parts = split_for_kakao_sentence_safe(text, MAX_SIMPLETEXT)
                parts = pack_into_max_outputs(parts, MAX_SIMPLETEXT, MAX_OUTPUTS)
                outputs = [{"simpleText": {"text": p}} for p in parts]
                
                payload = {
                    "version": "2.0",
                    "template": {"outputs": outputs},
                    "useCallback": True
                }
                headers = {"Content-Type": "application/json; charset=utf-8"}

                # 1) httpx 우선 시도 (에러시 본문도 로깅)
                try:
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        resp = await client.post(callback_url, json=payload, headers=headers)
                        if resp.status_code >= 400:
                            logger.error(f"Callback post failed via httpx: {resp.status_code} {resp.reason_phrase} | body={resp.text}")
                        resp.raise_for_status()
                        return
                except Exception as e:
                    logger.exception(f"Callback post failed via httpx: {e}")

                # 2) urllib 백업 시도 (동일 payload)
                try:
                    data = json.dumps(payload).encode("utf-8")
                    req = urllib.request.Request(callback_url, data=data, headers=headers, method="POST")
                    # 블로킹이라 스레드로
                    def _post_blocking():
                        with urllib.request.urlopen(req, timeout=3) as r:
                            status = r.status
                            if status >= 400:
                                raise RuntimeError(f"urllib callback HTTP {status}")
                            return status
                    status = await asyncio.to_thread(_post_blocking)
                    logger.info(f"Callback posted via urllib, status={status}")
                except Exception as e:
                    logger.exception(f"Callback post failed via urllib: {e}")

            async def _handle_callback_full(callback_url: str, user_id: str, user_text: str, request_id: str | None):
                final_text: str = "죄송합니다. 일시적인 오류가 발생했습니다. 다시 한 번 시도해주세요."
                tokens_used: int = 0
                try:
                    async for s in get_session():
                        try:
                            async def _ensure_conv():
                                await upsert_user(s, user_id)
                                return await get_or_create_conversation(s, user_id)
                            conv = await asyncio.wait_for(_ensure_conv(), timeout=0.7)
                            conv_id_value = str(conv.conv_id)
                            try:
                                if user_text:
                                    await save_message(s, conv_id_value, "user", user_text, trace_id, None, user_id)
                            except Exception as save_user_err:
                                logger.bind(x_request_id=request_id).warning(f"Failed to save user message in callback: {save_user_err}")

                            final_text, tokens_used = await ai_service.generate_response(
                                session=s,
                                conv_id=conv_id_value,
                                user_input=user_text,
                                prompt_name="default",
                                user_id=user_id
                            )
                            await save_message(s, conv_id_value, "assistant", final_text, trace_id, tokens_used, user_id)
                            try:
                                await save_log_message(s, "callback_final_sent", user_id, conv_id_value, request_id, {"tokens": tokens_used})
                            except Exception:
                                pass
                            try:
                                await maybe_rollup_user_summary(s, user_id, conv_id_value)
                            except Exception:
                                pass
                            break
                        except Exception as inner_e:
                            logger.bind(x_request_id=request_id).exception(f"Callback DB/AI error: {inner_e}")
                            break

                    try:
                        await _send_callback_response(callback_url, final_text, tokens_used, request_id)
                    except Exception as post_err:
                        logger.bind(x_request_id=request_id).exception(f"Callback post failed: {post_err}")
                except Exception as e:
                    logger.bind(x_request_id=request_id).exception(f"Callback flow failed: {e}")

            asyncio.create_task(_handle_callback_full(callback_url, user_id, user_text, x_request_id))

            try:
                update_last_activity(f"temp_{user_id}")
            except Exception:
                pass
            return JSONResponse(content=immediate, media_type="application/json; charset=utf-8")

        # 4) 콜백이 아닌 경우: 기존 즉시 응답 흐름
        try:
            async def _ensure_conv_main():
                try:
                    await upsert_user(session, user_id)
                except Exception:
                    try:
                        await session.rollback()
                        await upsert_user(session, user_id)
                    except Exception as e:
                        logger.warning(f"upsert_user failed after rollback: {e}")
                        raise
                try:
                    return await get_or_create_conversation(session, user_id)
                except Exception:
                    try:
                        await session.rollback()
                        return await get_or_create_conversation(session, user_id)
                    except Exception as e:
                        logger.warning(f"get_or_create_conversation failed after rollback: {e}")
                        raise
            conv = await asyncio.wait_for(_ensure_conv_main(), timeout=1.5)
            conv_id = conv.conv_id
        except Exception as db_err:
            logger.warning(f"DB ops failed in immediate path: {db_err}")
            conv_id = f"temp_{user_id}"

        # 5) AI 답변
        try:
            logger.info(f"Generating AI response for: {user_text}")
            

            try:
                final_text, tokens_used = await asyncio.wait_for(
                    ai_service.generate_response(
                        session=session,
                        conv_id=conv_id,
                        user_input=user_text,
                        prompt_name="default",
                        user_id=user_id
                    ),
                    timeout=BUDGET,
                )
            except asyncio.TimeoutError:
                logger.warning("AI generation timeout. Falling back to canned message.")
                final_text, tokens_used = ("답변 생성이 길어졌어요. 잠시만 기다려주세요.", 0)
            logger.info(f"AI response generated: {final_text[:50]}...")
            

            try:
                await save_log_message(session, "message_generated", str(user_id), conv_id, x_request_id, {"source": "ai_generation", "tokens": tokens_used})
            except Exception:
                pass
            
            try:
                if not str(conv_id).startswith("temp_") and conv_id:
                    async def _save_user_message_background(conv_id, user_text, x_request_id, user_id):
                        async for s in get_session():
                            try:
                                await save_message(s, conv_id, "user", user_text, x_request_id, None, user_id)
                                break
                            except Exception:
                                break

                    async def _save_ai_response_background(conv_id, final_text, tokens_used, x_request_id, user_id):
                        async for s in get_session():
                            try:
                                await save_message(s, conv_id, "assistant", final_text, x_request_id, tokens_used, user_id)
                                break
                            except Exception:
                                break

                    asyncio.create_task(_save_user_message_background(conv_id, user_text, x_request_id, user_id))
                    asyncio.create_task(_save_ai_response_background(conv_id, final_text, 0, x_request_id, user_id))
                else:
                    async def _persist_when_db_ready(user_id: str, user_text: str, reply_text: str, request_id: str | None):
                        async for s in get_session():
                            try:
                                await upsert_user(s, user_id)
                                conv = await get_or_create_conversation(s, user_id)
                                if user_text:
                                    await save_message(s, conv.conv_id, "user", user_text, request_id, None, user_id)
                                await save_message(s, conv.conv_id, "assistant", reply_text, request_id, None, user_id)
                                break
                            except Exception as persist_err:
                                logger.bind(x_request_id=request_id).warning(f"Persist after temp conv failed: {persist_err}")
                                break
                    asyncio.create_task(_persist_when_db_ready(user_id, user_text, final_text, x_request_id))
            except Exception as save_error:
                logger.warning(f"Failed to schedule message persistence: {save_error}")
            
            try:
                update_last_activity(conv_id)
            except Exception:
                pass

            return JSONResponse(content={
                "version": "2.0",
                "template": {"outputs":[{"simpleText":{"text": remove_markdown(final_text)}}]}
            }, media_type="application/json; charset=utf-8")
            
        except Exception as ai_error:
            logger.error(f"AI generation failed: {ai_error}")
            import traceback
            logger.error(f"Full traceback: {traceback.format_exc()}")
            final_text = "죄송합니다. 일시적인 오류가 발생했습니다. 다시 한 번 시도해주세요."
            
            return JSONResponse(content={
                "version": "2.0",
                "template": {"outputs":[{"simpleText":{"text": final_text}}]}
            }, media_type="application/json; charset=utf-8")
        
    except Exception as e:
        logger.exception(f"Error in skill endpoint: {e}")
        
        # LogMessage에도 저장
        try:
            await save_log_message(session, "ERROR", f"Error in skill endpoint: {e}", None, None, {"source": "error"})
        except Exception:
            pass
        safe_text = "일시적인 오류가 발생했어요. 다시 한 번 시도해 주세요"
        return JSONResponse(content={
            "version": "2.0",
            "template": {"outputs":[{"simpleText":{"text": safe_text}}]}
        }, media_type="application/json; charset=utf-8")




@router.post("/welcome")
async def welcome_skill(request: Request, session: AsyncSession = Depends(get_session)):
    """웰컴 스킬: 처음 대화를 시작할 때 웰컴 메시지를 보냅니다."""
    try:
        # 1) 요청 처리
        x_request_id = request.headers.get("X-Request-ID") or request.headers.get("X-Request-Id")
        logger.bind(x_request_id=x_request_id).info("Welcome skill request received")
        
        try:
            body = await request.json()
            if not isinstance(body, dict):
                body = {}
        except Exception as parse_err:
            logger.warning(f"JSON parse failed: {parse_err}")
            body = {}
            
        # 2) 사용자 ID 추출
        user_id = extract_user_id(body)
        if not user_id:
            anon_suffix = x_request_id or "unknown"
            user_id = f"anonymous:{anon_suffix}"
            logger.warning("No user_id in welcome skill, using fallback")
            
        # 3) 웰컴 메시지 전송
        response_text = random.choice(_WELCOME_MESSAGES)
        return JSONResponse(content={
            "version": "2.0",
            "template": {"outputs": [{"simpleText": {"text": response_text}}]}
        }, media_type="application/json; charset=utf-8")
        
    except Exception as e:
        logger.exception(f"Error in welcome skill: {e}")
        return JSONResponse(content={
            "version": "2.0",
            "template": {"outputs": [{"simpleText": {"text": random.choice(_WELCOME_MESSAGES)}}]}
        }, media_type="application/json; charset=utf-8")


@router.post("/test-skill")
async def test_skill_endpoint(request: Request):
    """디버깅용 테스트 엔드포인트 - 받은 데이터를 그대로 반환"""
    try:
        body = await request.json()
        logger.info("TEST SKILL - Request received")
        
        return {"status": "test_success", "received_data": body}
    except Exception as e:
        logger.error(f"TEST SKILL - Error: {e}")
        return {"error": str(e)}


@router.post("/test-callback")
async def test_callback_endpoint(request: Request):
    """콜백 테스트용 엔드포인트 - 받은 콜백 데이터를 로깅"""
    try:
        body = await request.json()
        logger.info("CALLBACK TEST - Request received")
        
        return {"status": "callback_received", "data": body}
    except Exception as e:
        logger.error(f"CALLBACK TEST - Error: {e}")
        return {"error": str(e)}


@router.post("/test-name-extraction")
async def test_name_extraction_endpoint(request: Request):
    """이름 추출 테스트용 엔드포인트"""
    try:
        body = await request.json()
        text = body.get("text", "")
        
        if not text:
            return {"error": "text field is required"}
        
        result = test_name_extraction(text)
        return {"status": "success", "result": result}
        
    except Exception as e:
        logger.exception(f"Name extraction test failed: {e}")
        return {"error": str(e)}
