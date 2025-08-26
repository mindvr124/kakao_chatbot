# app/risk_mvp.py
import re
from typing import Dict, List, Tuple, Optional
from collections import deque
from datetime import datetime, timedelta
from loguru import logger

# 점수별 정규식 패턴 정의
RISK_PATTERNS = {
    10: [  # 직접적, 구체적 자살 의도 및 수단 언급
        re.compile(r"(자살|목숨\s*끊|삶\s*끝내|죽을래|죽으면\s*편하|뛰어내|수면제|옥상|약\s*먹|과다\s*복용|유서|죽고\s*싶|뒤지고\s*싶)"),
    ],
    7: [   # 간접적 자살 사고 표현
        re.compile(r"(살기\s*싫|사라지고\s*싶|없어지고\s*싶|흔적\s*없이|끝내고\s*싶|포기\s*할래|포기\s*하|의미\s*없|살고\s*싶지\s*않|살고\s*싶지않|살고\s*싶진\s*않)"),
    ],
    4: [   # 자존감 저하·학대·왕따 등
        re.compile(r"(쓸모\s*없|필요\s*없|잘못된\s*사람|아무것도\s*못\s*해|내\s*탓|내가\s*문제|맞았어|괴롭힘|왕따|따돌림|욕설|부모\s*맞았|무서워|때리|때려|몽둥이|폭력)")
    ],
    2: [   # 일반적 스트레스·우울 신호
        re.compile(r"(힘들|지쳤|하기\s*싫|의욕\s*없|지친다|지쳐|기운\s*없|혼자\s*있고\s*싶|외롭|숨\s*막힌다|우울|무기력|숨막\s*)"),
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
        self.turns = deque(maxlen=max_turns)
        self.max_turns = max_turns
        self.last_updated = datetime.now()
        self.check_question_turn_count = 0
        self.last_check_score = None
    
    def add_turn(self, text: str) -> Dict:
        """새로운 턴을 추가하고 위험도를 분석합니다."""
        # 체크 질문 발송 후 턴 카운트 증가
        if self.check_question_turn_count > 0:
            self.check_question_turn_count += 1
            logger.info(f"[RISK_HISTORY] 체크 질문 발송 후 턴 카운트 증가: {self.check_question_turn_count}")
        
        turn_analysis = self._analyze_single_turn(text)
        
        turn_data = {
            'text': text,
            'timestamp': datetime.now(),
            'score': turn_analysis['score'],
            'flags': turn_analysis['flags'],
            'evidence': turn_analysis['evidence']
        }
        
        self.turns.append(turn_data)
        self.last_updated = datetime.now()
        
        # 현재 턴의 점수와 누적 점수를 모두 로깅
        current_turn_score = turn_analysis['score']
        cumulative_score = self.get_cumulative_score()
        
        logger.info(f"[RISK_HISTORY] 턴 추가 완료: turns_after={len(self.turns)}, current_turn_score={current_turn_score}, cumulative_score={cumulative_score}")
        
        return turn_analysis
    
    def get_cumulative_score(self) -> int:
        """최근 턴들의 누적 위험도 점수를 계산합니다. 시간 기반 감점 없이 순수 누적만 적용."""
        if not self.turns:
            logger.info(f"[RISK_HISTORY] 누적 점수 계산: 턴이 없음 -> 0")
            return 0
        
        # 각 턴의 점수를 상세히 로깅
        turn_details = []
        for i, turn in enumerate(self.turns):
            turn_details.append(f"턴{i+1}: {turn['score']}점('{turn['text'][:20]}...')")
        
        logger.info(f"[RISK_HISTORY] 턴별 점수: {' | '.join(turn_details)}")
        
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
        old_count = self.check_question_turn_count
        self.check_question_turn_count = 1  # 1부터 시작 (다음 턴부터 카운트)
        logger.info(f"[RISK_HISTORY] 체크 질문 발송 기록: {old_count} -> {self.check_question_turn_count}")
    
    def can_send_check_question(self) -> bool:
        """체크 질문을 발송할 수 있는지 확인합니다."""
        # 체크 질문 응답이 완료된 경우 발송하지 않음
        if self.last_check_score is not None:
            logger.info(f"[RISK_HISTORY] 체크 질문 응답 완료 상태: last_check_score={self.last_check_score}, 체크 질문 발송 불가")
            return False
        
        # check_question_turn_count가 0이면 체크 질문 발송 가능
        if self.check_question_turn_count == 0:
            logger.info(f"[RISK_HISTORY] 체크 질문 발송 가능: check_question_turn_count={self.check_question_turn_count} (초기 상태)")
            return True
        
        # 체크 질문 발동 후 20턴이 지나지 않았으면 발송 불가
        if self.check_question_turn_count > 0 and self.check_question_turn_count <= 20:
            # 예외: 5턴 이내 자살 플래그 10점이 넘어가면 발송 가능
            if self.check_question_turn_count <= 5:
                # 최근 5턴에서 10점 이상인 턴이 있는지 확인
                recent_turns = list(self.turns)[-5:]
                high_risk_turns = [turn for turn in recent_turns if turn['score'] >= 10]
                if high_risk_turns:
                    logger.info(f"[RISK_HISTORY] 예외 조건 충족: 5턴 이내 고위험 점수 {len(high_risk_turns)}개 발견, 체크 질문 발송 가능")
                    return True
                else:
                    logger.info(f"[RISK_HISTORY] 5턴 이내 자살 플래그 10점 이상 없음, 체크 질문 발송 불가")
            
            logger.info(f"[RISK_HISTORY] 체크 질문 발송 불가: check_question_turn_count={self.check_question_turn_count} (20턴 제한)")
            return False
        
        # 20턴이 지났으면 발송 가능
        logger.info(f"[RISK_HISTORY] 체크 질문 발송 가능: check_question_turn_count={self.check_question_turn_count} (20턴 경과)")
        return True
    
    def _analyze_single_turn(self, text: str) -> Dict:
        """단일 텍스트의 위험도를 분석합니다."""
        if not text:
            return {'score': 0, 'flags': {}, 'evidence': []}
        
        text_lower = text.strip().lower()
        logger.info(f"[RISK_ANALYSIS] 텍스트 분석 시작: '{text}' -> '{text_lower}'")
        
        flags = self._get_flags(text_lower)
        logger.info(f"[RISK_ANALYSIS] 플래그 분석 결과: {flags}")
        
        # 메타언어, 3인칭, 관용어가 포함된 경우 점수 계산 제외
        if flags["meta"] or flags["third"] or flags["idiom"]:
            logger.info(f"[RISK_ANALYSIS] 메타언어/3인칭/관용어로 인해 점수 계산 제외")
            return {'score': 0, 'flags': flags, 'evidence': []}
        
        total_score = 0
        evidence = []
        matched_positions = set()  # 이미 매칭된 위치를 추적
        
        logger.info(f"[RISK_ANALYSIS] 패턴 매칭 시작: {len(RISK_PATTERNS)}개 점수 레벨")
        
        # 각 점수별 정규식 패턴 검사 (높은 점수부터 순서대로)
        for score in sorted(RISK_PATTERNS.keys(), reverse=True):
            logger.info(f"[RISK_ANALYSIS] {score}점 패턴 검사 시작")
            for pattern in RISK_PATTERNS[score]:
                matches = list(pattern.finditer(text_lower))
                logger.info(f"[RISK_ANALYSIS] {score}점 패턴 '{pattern.pattern}' 매칭 결과: {len(matches)}개")
                
                for match in matches:
                    # 이미 매칭된 위치와 겹치는지 확인
                    start, end = match.start(), match.end()
                    matched_text = match.group()
                    logger.info(f"[RISK_ANALYSIS] 매칭된 텍스트: '{matched_text}' (위치: {start}-{end})")
                    
                    if any(start < pos_end and end > pos_start for pos_start, pos_end in matched_positions):
                        logger.info(f"[RISK_ANALYSIS] 겹치는 매칭으로 건너뛰기: '{matched_text}'")
                        continue  # 겹치는 매칭은 건너뛰기
                    
                    # 특별한 위험 표현은 부정어가 있어도 점수 부여
                    special_danger_patterns = ["살고싶지않", "살고싶지 않", "살고싶진 않"]
                    is_special_danger = any(pattern in matched_text for pattern in special_danger_patterns)
                    
                    # 부정어가 포함된 경우 점수 차감 (단, 특별한 위험 표현은 제외)
                    actual_score = 0 if (flags["neg"] and not is_special_danger) else score
                    logger.info(f"[RISK_ANALYSIS] 점수 계산: original={score}, flags_neg={flags['neg']}, special_danger={is_special_danger}, actual={actual_score}")
                    
                    # 과거시제가 포함된 경우 자살 관련 키워드 점수 차감
                    if flags["past"] and score >= 7:  # 7점 이상(자살 관련)만 차감
                        old_score = actual_score
                        actual_score = max(0, actual_score - 2)
                        logger.info(f"[RISK_ANALYSIS] 과거시제로 인한 차감: {old_score} -> {actual_score}")
                    
                    if actual_score > 0:
                        total_score += actual_score
                        evidence.append({
                            "keyword": matched_text,
                            "score": actual_score,
                            "original_score": score,
                            "excerpt": self._get_context(text_lower, start, end)
                        })
                        # 매칭된 위치 기록
                        matched_positions.add((start, end))
                        logger.info(f"[RISK_ANALYSIS] 점수 추가: {actual_score}점, 누적: {total_score}점")
                        break  # 이 점수 레벨에서는 하나만 매칭
                    else:
                        logger.info(f"[RISK_ANALYSIS] 점수 0으로 계산되어 추가하지 않음")
        
        logger.info(f"[RISK_ANALYSIS] 패턴 매칭 완료: 총 {total_score}점")
        
        # 긍정 발화 감점 적용 (-2점, 최저 0점)
        if P_POSITIVE.search(text_lower):
            before_deduction = total_score
            total_score = max(0, total_score - 2)
            deduction_applied = before_deduction - total_score
            
            evidence.append({
                "keyword": "긍정_발화",
                "score": -deduction_applied,
                "original_score": -2,
                "excerpt": "긍정적인 발화로 인한 감점"
            })
            
            logger.info(f"[RISK_ANALYSIS] 긍정 발화 감점 적용: before={before_deduction}, deduction={deduction_applied}, after={total_score}")
        
        logger.info(f"[RISK_ANALYSIS] 최종 분석 결과: score={total_score}, evidence_count={len(evidence)}")
        
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
    logger.info(f"[CHECK_CONDITION] 체크 질문 발송 조건 확인: score={score}, risk_history={risk_history is not None}")
    
    # 기본 점수 조건 확인
    if score < 8:
        logger.info(f"[CHECK_CONDITION] 점수 조건 미충족: {score} < 8")
        return False
    
    logger.info(f"[CHECK_CONDITION] 점수 조건 충족: {score} >= 8")
    
    # RiskHistory가 제공된 경우 추가 조건 확인
    if risk_history:
        can_send = risk_history.can_send_check_question()
        logger.info(f"[CHECK_CONDITION] RiskHistory 조건 확인: can_send={can_send}, check_question_turn_count={risk_history.check_question_turn_count}")
        
        # can_send가 True면 바로 반환
        if can_send:
            logger.info(f"[CHECK_CONDITION] can_send가 True이므로 체크 질문 발송 가능")
            return True
        
        # can_send가 False이고 check_question_turn_count <= 5일 때 예외 조건 확인
        if risk_history.check_question_turn_count <= 5:
            recent_turns = list(risk_history.turns)[-5:]  # 최근 5턴 확인
            high_risk_turns = [turn for turn in recent_turns if turn['score'] >= 10]
            
            if high_risk_turns:
                high_risk_details = [f"턴{turn['score']}점('{turn['text'][:20]}...')" for turn in high_risk_turns]
                logger.info(f"[CHECK_CONDITION] 예외 조건 충족: 5턴 이내 자살 플래그 10점 이상 {len(high_risk_turns)}턴, 상세: {' | '.join(high_risk_details)}, 체크 질문 강제 발송")
                return True
            else:
                logger.info(f"[CHECK_CONDITION] 예외 조건 미충족: 5턴 이내 자살 플래그 10점 이상 없음")
        
        logger.info(f"[CHECK_CONDITION] can_send가 False이고 예외 조건도 미충족하므로 체크 질문 발송 불가")
        return False
    
    # RiskHistory가 없는 경우 기본 점수 조건만 확인
    logger.info(f"[CHECK_CONDITION] RiskHistory 없음, 기본 점수 조건만 확인")
    return True

def get_check_questions() -> List[str]:
    """체크 질문 목록을 반환합니다."""
    return [
        "지금 마음 상태를 0~10으로 표현해줄래? (0=안정, 10=불안)",
        "네가 얼마나 힘든지 알고 싶어. 지금 상태를 0~10의 숫자로 말해줄래? (0=안정, 10=불안)",
        "네가 얼마나 힘든지 알고 싶어. 0~10의 숫자로 표현해줄 수 있을까? (0=안정, 10=불안)"
    ]

def get_invalid_score_message() -> str:
    """잘못된 점수 입력에 대한 재질문 메시지를 반환합니다."""
    return "다시 한 번 숫자로만 알려줄래? 예: 0, 1, 2 ..."

def parse_check_response(text: str) -> Optional[int]:
    """체크 질문 응답에서 점수를 파싱합니다."""
    import re
    
    logger.info(f"[PARSE_DEBUG] 체크 응답 파싱 시작: text='{text}'")
    
    # 입력 텍스트 정리 (공백 제거, 소문자 변환)
    text_clean = text.strip().lower()
    
    # 정확히 0~10 범위의 정수만 매칭 (단독 숫자 또는 숫자만 포함된 텍스트)
    # 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10만 허용
    exact_match = re.match(r'^(0|1|2|3|4|5|6|7|8|9|10)$', text_clean)
    
    if exact_match:
        score = int(exact_match.group())
        logger.info(f"[PARSE_DEBUG] 정확한 점수 매칭: {score}")
        return score
    
    # "1점", "2점", "1 점" 등의 패턴 매칭
    point_pattern = re.match(r'^(\d+)\s*점?$', text_clean)
    if point_pattern:
        score = int(point_pattern.group(1))
        logger.info(f"[PARSE_DEBUG] 점수 패턴 매칭: {score}점")
        
        # 0~10 범위 검증
        if 0 <= score <= 10:
            logger.info(f"[PARSE_DEBUG] 유효한 점수 확인: {score}")
            return score
        else:
            logger.info(f"[PARSE_DEBUG] 점수 범위 초과: {score} (0-10 범위 아님)")
    
    # 숫자만 추출하여 확인 (fallback)
    numbers = re.findall(r'\b([0-9]|10)\b', text_clean)
    logger.info(f"[PARSE_DEBUG] 정규식 매칭 결과: {numbers}")
    
    if numbers:
        score = int(numbers[0])
        logger.info(f"[PARSE_DEBUG] 추출된 점수: {score}")
        
        # 0~10 범위 검증
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
