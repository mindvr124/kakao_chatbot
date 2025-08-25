# app/risk_mvp.py
import re
from typing import Dict, List, Tuple, Optional
from collections import deque
from datetime import datetime, timedelta
from loguru import logger

# 점수별 정규식 패턴 정의
RISK_PATTERNS = {
    10: [  # 직접적, 구체적 자살 의도 및 수단 언급
        re.compile(r"(자살|목숨\s*끊|삶\s*끝내|죽으면\s*편하|뛰어내리다|수면제|옥상|약\s*먹|과다\s*복용|유서|죽고\s*싶|뒤지고\s*싶)"),
    ],
    7: [   # 간접적 자살 사고 표현
        re.compile(r"(죽고\s*싶|살기\s*싫|사라지고\s*싶|없어지고\s*싶|흔적\s*없이|끝내고\s*싶|포기할래|의미\s*없|살고\s*싶지\s*않|살고\s*싶지않|살고\s*싶진\s*않)"),
    ],
    4: [   # 자존감 저하·학대·왕따 등
        re.compile(r"(쓸모\s*없|필요\s*없|잘못된\s*사람|아무것도\s*못\s*해|내\s*탓|내가\s*문제|맞았어|괴롭힘|왕따|따돌림|욕설|부모\s*맞았|무서워|때리|때려|몽둥이|폭력)")
    ],
    2: [   # 일반적 스트레스·우울 신호
        re.compile(r"(힘들|지쳤|하기\s*싫|의욕\s*없|기운\s*없|혼자\s*있고\s*싶|외롭|숨\s*막힌다|우울|무기력|숨막\s*)"),
    ]
}

# 부정어, 메타언어, 3인칭, 관용어, 과거시제 패턴
# 부정어: 일반적인 부정 표현 (하지만 "살고싶지않아" 같은 위험 표현은 제외)
P_NEG = re.compile(r"(죽고\s*싶지\s*않|죽고\s*싶진\s*않)")
P_META = re.compile(r"(뉴스|기사|드라마|가사|영화|예시|논문|수업|연구|애니|소설설)")
P_THIRD = re.compile(r"(친구|사람들|누가|그[가녀])")
P_IDIOM = re.compile(r"(죽을맛|웃겨\s*죽|맛\s*죽이)")
P_PAST = re.compile(r"(예전에|한때|옛날에|과거에)")

# 긍정 발화 패턴 (감점 적용)
P_POSITIVE = re.compile(r"(괜찮아|괜찮|나아졌어|덜\s*힘들|고마워|좋아졌어|살아야지|희망이\s*생|내일은\s*괜찮|얘기\s*마음이\s*가벼|죽고\s*싶지\s*않아|그럴\s*생각\s*없어|이제\s*좀\s*괜찮은\s*것\s*같아)")

class RiskHistory:
    """사용자별 위험도 대화 히스토리를 관리하는 클래스"""
    
    def __init__(self, max_turns: int = 20):
        self.max_turns = max_turns
        self.turns: deque = deque(maxlen=max_turns)
        self.last_updated = datetime.now()
        self.check_question_turn_count = 0  # 체크 질문 발동 후 턴 카운트
    
    def add_turn(self, text: str, timestamp: datetime = None) -> Dict:
        """새로운 턴을 추가하고 위험도를 분석합니다."""
        if timestamp is None:
            timestamp = datetime.now()
        
        # 현재 턴 분석
        turn_analysis = self._analyze_single_turn(text)
        
        # 턴 정보 저장
        turn_data = {
            'text': text,
            'timestamp': timestamp,
            'score': turn_analysis['score'],
            'flags': turn_analysis['flags'],
            'evidence': turn_analysis['evidence']
        }
        
        logger.info(f"[RISK_HISTORY] 턴 추가: text='{text[:30]}...', score={turn_analysis['score']}, turns_before={len(self.turns)}")
        
        self.turns.append(turn_data)
        self.last_updated = timestamp
        
        logger.info(f"[RISK_HISTORY] 턴 추가 완료: turns_after={len(self.turns)}, total_score={self.get_cumulative_score()}")
        
        # 체크 질문 발동 후 턴 카운트 증가
        if self.check_question_turn_count > 0:
            self.check_question_turn_count += 1
        
        return turn_analysis
    
    def get_cumulative_score(self) -> int:
        """최근 턴들의 누적 위험도 점수를 계산합니다. 시간 기반 감점 없이 순수 누적만 적용."""
        if not self.turns:
            logger.info(f"[RISK_HISTORY] 누적 점수 계산: 턴이 없음 -> 0")
            return 0
        
        # 시간 기반 감점 없이 순수 누적 점수만 계산
        total_score = sum(turn['score'] for turn in self.turns)
        final_score = min(100, total_score)
        
        logger.info(f"[RISK_HISTORY] 누적 점수 계산 완료: raw_total={total_score}, final_score={final_score}, turns_count={len(self.turns)}")
        return final_score
    
    def get_risk_trend(self) -> str:
        """위험도 변화 추세를 분석합니다."""
        if len(self.turns) < 2:
            return "stable"
        
        recent_scores = [turn['score'] for turn in list(self.turns)[-3:]]
        
        if len(recent_scores) >= 2:
            if recent_scores[-1] > recent_scores[-2]:
                return "increasing"
            elif recent_scores[-1] < recent_scores[-2]:
                return "decreasing"
        
        return "stable"
    
    def get_recent_evidence(self, max_items: int = 5) -> List[Dict]:
        """최근 증거들을 수집합니다."""
        evidence = []
        for turn in reversed(list(self.turns)):
            evidence.extend(turn['evidence'])
            if len(evidence) >= max_items:
                break
        return evidence[:max_items]
    
    def mark_check_question_sent(self):
        """체크 질문이 발송되었음을 기록합니다."""
        self.check_question_turn_count = 1  # 1부터 시작 (다음 턴부터 카운트)
    
    def can_send_check_question(self) -> bool:
        """체크 질문을 발송할 수 있는지 확인합니다."""
        # 체크 질문 발동 후 20턴이 지나지 않았으면 발송 불가
        if self.check_question_turn_count > 0 and self.check_question_turn_count <= 20:
            return False
        return True
    
    def _analyze_single_turn(self, text: str) -> Dict:
        """단일 텍스트의 위험도를 분석합니다."""
        if not text:
            return {'score': 0, 'flags': {}, 'evidence': []}
        
        text_lower = text.strip().lower()
        flags = self._get_flags(text_lower)
        
        # 메타언어, 3인칭, 관용어가 포함된 경우 점수 계산 제외
        if flags["meta"] or flags["third"] or flags["idiom"]:
            return {'score': 0, 'flags': flags, 'evidence': []}
        
        total_score = 0
        evidence = []
        
        # 각 점수별 정규식 패턴 검사
        for score, patterns in RISK_PATTERNS.items():
            for pattern in patterns:
                matches = pattern.finditer(text_lower)
                for match in matches:
                    matched_text = match.group()
                    
                    # 특별한 위험 표현은 부정어가 있어도 점수 부여
                    special_danger_patterns = ["살고싶지않", "살고싶지 않", "살고싶진 않"]
                    is_special_danger = any(pattern in matched_text for pattern in special_danger_patterns)
                    
                    # 부정어가 포함된 경우 점수 차감 (단, 특별한 위험 표현은 제외)
                    actual_score = 0 if (flags["neg"] and not is_special_danger) else score
                    
                    # 과거시제가 포함된 경우 자살 관련 키워드 점수 차감
                    if flags["past"] and score >= 7:  # 7점 이상(자살 관련)만 차감
                        actual_score = max(0, actual_score - 2)
                    
                    if actual_score > 0:
                        total_score += actual_score
                        evidence.append({
                            "keyword": matched_text,
                            "score": actual_score,
                            "original_score": score,
                            "excerpt": self._get_context(text_lower, match.start(), match.end())
                        })
        
        # 긍정 발화 감점 적용 (-2점, 최저 0점)
        if P_POSITIVE.search(text_lower):
            total_score = max(0, total_score - 2)
            evidence.append({
                "keyword": "긍정_발화",
                "score": -2,
                "original_score": -2,
                "excerpt": "긍정적인 발화로 인한 감점"
            })
            logger.info(f"[RISK_HISTORY] 긍정 발화 감점 적용: -2점, 최종 점수={total_score}")
        
        return {
            'score': total_score,
            'flags': flags,
            'evidence': evidence
        }
    
    def _get_flags(self, text: str) -> Dict[str, bool]:
        """텍스트에서 특수 플래그들을 탐지합니다."""
        return {
            "neg": bool(P_NEG.search(text)),
            "meta": bool(P_META.search(text)),
            "third": bool(P_THIRD.search(text)),
            "idiom": bool(P_IDIOM.search(text)),
            "past": bool(P_PAST.search(text)),
            "positive": bool(P_POSITIVE.search(text))
        }
    
    def _get_context(self, text: str, start: int, end: int, context_chars: int = 10) -> str:
        """키워드 주변 문맥을 추출합니다."""
        try:
            context_start = max(0, start - context_chars)
            context_end = min(len(text), end + context_chars)
            return text[context_start:context_end].strip()
        except:
            return text[start:end] if start < len(text) and end <= len(text) else ""

def calculate_risk_score(text: str, risk_history: RiskHistory = None) -> Tuple[int, Dict[str, bool], List[Dict]]:
    """
    텍스트의 자살위험도를 점수로 계산합니다.
    
    Args:
        text: 분석할 텍스트
        risk_history: 위험도 히스토리 객체 (제공시 누적 점수 반환)
    
    Returns:
        Tuple[int, Dict[str, bool], List[Dict]]: (점수, 플래그, 증거)
    """
    if risk_history:
        # 히스토리를 고려한 누적 분석
        turn_analysis = risk_history.add_turn(text)
        cumulative_score = risk_history.get_cumulative_score()
        
        return cumulative_score, turn_analysis['flags'], risk_history.get_recent_evidence()
    else:
        # 단일 텍스트 분석 (기존 방식)
        if not text:
            return 0, {}, []
        
        text_lower = text.strip().lower()
        flags = _get_flags(text_lower)
        
        # 메타언어, 3인칭, 관용어가 포함된 경우 점수 계산 제외
        if flags["meta"] or flags["third"] or flags["idiom"]:
            return 0, flags, []
        
        total_score = 0
        evidence = []
        
        # 각 점수별 정규식 패턴 검사
        for score, patterns in RISK_PATTERNS.items():
            for pattern in patterns:
                matches = pattern.finditer(text_lower)
                for match in matches:
                    matched_text = match.group()
                    
                    # 특별한 위험 표현은 부정어가 있어도 점수 부여
                    special_danger_patterns = ["살고싶지않", "살고싶지 않", "살고싶진 않"]
                    is_special_danger = any(pattern in matched_text for pattern in special_danger_patterns)
                    
                    # 부정어가 포함된 경우 점수 차감 (단, 특별한 위험 표현은 제외)
                    actual_score = 0 if (flags["neg"] and not is_special_danger) else score
                    
                    # 과거시제가 포함된 경우 자살 관련 키워드 점수 차감
                    if flags["past"] and score >= 7:  # 7점 이상(자살 관련)만 차감
                        actual_score = max(0, actual_score - 2)
                    
                    if actual_score > 0:
                        total_score += actual_score
                        evidence.append({
                            "keyword": matched_text,
                            "score": actual_score,
                            "original_score": score,
                            "excerpt": _get_context(text_lower, match.start(), match.end())
                        })
        
        # 긍정 발화 감점 적용 (-2점, 최저 0점)
        if P_POSITIVE.search(text_lower):
            total_score = max(0, total_score - 2)
            evidence.append({
                "keyword": "긍정_발화",
                "score": -2,
                "original_score": -2,
                "excerpt": "긍정적인 발화로 인한 감점"
            })
            logger.info(f"[RISK_HISTORY] 긍정 발화 감점 적용: -2점, 최종 점수={total_score}")
        
        return total_score, flags, evidence

def _get_flags(text: str) -> Dict[str, bool]:
    """텍스트에서 특수 플래그들을 탐지합니다."""
    return {
        "neg": bool(P_NEG.search(text)),
        "meta": bool(P_META.search(text)),
        "third": bool(P_THIRD.search(text)),
        "idiom": bool(P_IDIOM.search(text)),
        "past": bool(P_PAST.search(text))
    }

def _get_context(text: str, start: int, end: int, context_chars: int = 10) -> str:
    """키워드 주변 문맥을 추출합니다."""
    try:
        context_start = max(0, start - context_chars)
        context_end = min(len(text), end + context_chars)
        return text[context_start:context_end].strip()
    except:
        return text[start:end] if start < len(text) and end <= len(text) else ""

def should_send_check_question(score: int, risk_history: RiskHistory = None) -> bool:
    """체크 질문을 발송해야 하는지 판단합니다."""
    # 기본 점수 조건 확인
    if score < 8:
        return False
    
    # RiskHistory가 제공된 경우 추가 조건 확인
    if risk_history:
        return risk_history.can_send_check_question()
    
    # RiskHistory가 없는 경우 기본 점수 조건만 확인
    return True

def get_check_questions() -> List[str]:
    """체크 질문 목록을 반환합니다."""
    return [
        "지금 마음 상태를 0~10으로 표현해줄래? 0은 괜찮음, 10은 당장 위험한 상태야.",
        "너무 힘들어 보여서 확인하고 싶어. 0은 괜찮음, 10은 많이 위험한 상태야.",
        "네가 얼마나 힘든지 알고 싶어. 숫자로 말해줄래? (0=안정, 10=위험)"
    ]

def parse_check_response(text: str) -> Optional[int]:
    """체크 질문 응답에서 점수를 파싱합니다."""
    import re
    
    logger.info(f"[PARSE_DEBUG] 체크 응답 파싱 시작: text='{text}'")
    
    # 숫자만 추출 (0-10 범위)
    numbers = re.findall(r'\b([0-9]|10)\b', text)
    logger.info(f"[PARSE_DEBUG] 정규식 매칭 결과: {numbers}")
    
    if numbers:
        score = int(numbers[0])
        logger.info(f"[PARSE_DEBUG] 추출된 점수: {score}")
        
        if 0 <= score <= 10:
            logger.info(f"[PARSE_DEBUG] 유효한 점수 확인: {score}")
            return score
        else:
            logger.info(f"[PARSE_DEBUG] 점수 범위 초과: {score} (0-10 범위 아님)")
    else:
        logger.info(f"[PARSE_DEBUG] 숫자를 찾을 수 없음")
    
    logger.info(f"[PARSE_DEBUG] 파싱 실패: None 반환")
    return None

def get_risk_level(score: int) -> str:
    """점수에 따른 위험도 레벨을 반환합니다."""
    if score >= 25:
        return "critical"
    elif score >= 15:
        return "high"
    elif score >= 8:
        return "moderate"
    else:
        return "low"

def get_check_response_guidance(check_score: int) -> str:
    """
    체크 질문 응답 점수에 따른 대응 가이드를 반환합니다.
    
    Args:
        check_score: 체크 질문 응답 점수 (0-10)
    
    Returns:
        str: 대응 가이드 메시지
    """
    if check_score == 0:
        return "가벼운 긍정 피드백, 대화 주제 전환 가능"
    elif 1 <= check_score <= 3:
        return "짧은 공감 + 긍정적 강화"
    elif 4 <= check_score <= 6:
        return "공감 + 대처 권유"
    elif 7 <= check_score <= 8:
        return "즉시 안전 안내 메시지 발송, 보호자or전문기관 연결 권유"
    elif 9 <= check_score <= 10:
        return "즉시 기관 안내, 시스템알림"
    else:
        return "알 수 없는 응답"

def get_check_response_message(check_score: int) -> str:
    """
    체크 질문 응답 점수에 따른 구체적인 메시지를 반환합니다.
    
    Args:
        check_score: 체크 질문 응답 점수 (0-10)
    
    Returns:
        str: 대응 메시지
    """
    if check_score == 0:
        return "지금은 괜찮다고 느끼는구나, 다행이야. 이런 안정감이 계속 있으면 좋겠어."
    elif 1 <= check_score <= 3:
        return "조금은 힘들지만 잘 버티고 있네. 그럴 때도 있지만 괜찮아, 잘하고 있어."
    elif 4 <= check_score <= 6:
        return "마음이 꽤 무거울 것 같아. 잠깐 휴식이 필요할 수도 있어.\n혼자서 버티기보다 누군가와 나누면 좀 나아질 수 있어."
    elif 7 <= check_score <= 8:
        return "지금 정말 많이 힘든 것 같아. 혼자 감당하기엔 위험할 수 있어. 너를 도와줄 수 있는 안전한 사람에게 꼭 바로 말해줘."
    elif 9 <= check_score <= 10:
        return "지금 상황이 매우 심각해 보여. 즉시 도움을 받아야 해.\n• 자살예방 상담전화 1393 (24시간)\n• 정신건강 위기상담 1577-0199\n• 긴급상황: 112/119\n혼자가 아니야. 지금 당장 연락해줘."
    else:
        return "응답을 이해하지 못했어. 다시 한 번 0부터 10까지의 숫자로 답해줘."
