# app/risk_mvp.py
import re
from typing import Dict, List, Tuple, Optional
from collections import deque
from datetime import datetime, timedelta
from loguru import logger

# 공통 플래그
FLAGS = re.IGNORECASE | re.UNICODE

# 점수별 정규식 패턴 정의 (추가 키워드 보강 + 과오탐 완화)
RISK_PATTERNS = {
    10: [  # 직접적, 구체적 자살 의도 및 수단 언급
        re.compile(r"(자\s*살|극단\s*적?\s*선택|생을\s*마감|목숨\s*끊)", FLAGS),
        re.compile(r"(죽고\s*싶(?!\s*지?\s*않)|죽을\s*(래|까)|죽으면\s*편하|죽자|뒤지고\s*싶)", FLAGS),
        re.compile(r"(뛰어\s*내리\w*|투신|목\s*매|유서|유언장|옥상|다리|교각|난간|철로|선로)", FLAGS),
        re.compile(r"(수면제|과다\s*복용|연탄\s*가스|번개탄|질소\s*가스|헬륨\s*가스|배기\s*가스)", FLAGS),
        re.compile(r"(약\s*먹고\s*죽|독극물|청산가리|살충제|농약\s*먹)", FLAGS),
    ],
    7: [   # 간접적 자살 사고 표현 / NSSI
        re.compile(r"(살기\s*싫|살\s*의\s*의미\s*없|사라지고\s*싶|없어지고\s*싶|흔적\s*없이\s*사라지\w*|"
                    r"모든\s*걸\s*끝내고\s*싶|끝내고\s*싶|포기\s*하(?:겠|고|려|자)|"
                    r"살고\s*싶지\s*않|살고\s*싶지않|살고\s*싶진\s*않)", FLAGS),
        re.compile(r"(자해|손목\s*긋|피를\s*내고\s*싶|칼로\s*베|면도날|흉기\s*로)", FLAGS),
    ],
    4: [   # 자존감 저하·학대·왕따 등
        re.compile(r"(무시|모욕|욕설|괴롭힘|왕따|따돌림|때리|폭력|맞았(?:어|다)?|학대|가정\s*폭력|부모\s*맞았)", FLAGS),
        re.compile(r"(쓰레기\s*같|패배자|실패자|망했[다어]|다\s*망쳐|쓸мо\s*없|필요\s*없|가치\s*없|"
                    r"한심|찌질|자괴감|자책|아무것도\s*못\s*해|내\s*탓|내가\s*문제|"
                    r"태어나지\s*말|태어난\s*게|왜\s*낳았(?:어|어요)?|왜\s*태어)", FLAGS),
        re.compile(r"(두려워|무서워|겁나)", FLAGS),
    ],
    2: [   # 일반적 스트레스·우울 신호
        re.compile(r"(힘들|지쳤|지쳐|하기\s*싫|의욕\s*없|기운\s*없|외롭|혼자\s*있고?\s*싶|"
                    r"불안|초조|공황|패닉|불면|잠이\s*안\s*와|잠을\s*못\s*자)", FLAGS),
        re.compile(r"(숨\s*막(?:히|힌다|히는|혀)\w*|가슴\s*답답|답답)", FLAGS),
        re.compile(r"(우울|무기력|번아웃|벅차|버겁|공허|허무|울고\s*싶|울었어|눈물\s*나)", FLAGS),
        re.compile(r"(고립|소외|외톨|귀찮|떨려|떨리|멘붕|멘탈\s*나가)", FLAGS),
    ]
}

# 부정어, 메타언어, 3인칭, 관용어, 과거시제 패턴
# 부정어: 일반 부정(단, '살고싶지않아' 등은 특수 처리)
P_NEG = re.compile(
    r"(않(?:아|아요|았|을|지|고)?|안\s*(?:하|해|할|합니다)|싫(?:어|다|습니다)|원치\s*않|"
    r"생각\s*없(?:어|다)|아니(?:야|다))", FLAGS
)

P_META = re.compile(
    r"(뉴스|기사|드라마|가사|영화|예시|논문|수업|연구|애니|소설|웹툰|유튜브|틱톡|대사|인용|"
    r"캡처|캡쳐|링크|출처|발췌|댓글|커뮤|포럼|밈)", FLAGS
)

P_THIRD = re.compile(
    r"(친구|지인|동료|선배|후배|사람들|누가|그(?:가|녀|분)?|그\s*사람|타인|고객|환자|사용자|"
    r"가족|엄마|아빠|부모|형|누나|오빠|언니|동생|아들|딸)(?:\s|[은는이가의을를]|에게|한테)", FLAGS
)

# 관용적 과장표현(완화): 수식어 + '~죽겠다', '죽을 맛', '맛 죽이네'
P_IDIOM = re.compile(
    r"((웃겨|배고파|졸려|피곤|아파|힘들|심심|추워|더워|어지러워|부끄러워|귀찮)"
    r"\s*죽겠(?:다|네|어|음))|(죽을\s*맛)|(맛\s*죽이(?:네|다))", FLAGS
)

# 과거 회고(완화)
P_PAST = re.compile(
    r"(예전에|한때|옛날에|과거에|어릴\s*때|어렸을\s*때|학창\s*시절|중학생\s*때|고등학생\s*때|군대에서)", FLAGS
)

# 긍정 발화 패턴 (감점 적용)
P_POSITIVE = re.compile(
    r"(괜찮아|괜찮|나아졌어|덜\s*힘들|고마워|좋아졌어|살아야지|살만\s*하|희망이\s*생|"
    r"내일은\s*괜찮|얘기하니\s*마음이\s*가벼|죽고\s*싶지\s*않아|그럴\s*생각\s*없어|"
    r"이겨낼|버텨볼게|이제\s*좀\s*괜찮은\s*것\s*같아|도움이\s*됐어)", FLAGS
)

class RiskHistory:
    """사용자별 위험도 대화 히스토리를 관리하는 클래스"""
    def __init__(self, max_turns: int = 20, user_id: str = None, db_session = None):
        self.turns = deque(maxlen=max_turns)
        self.max_turns = max_turns
        self.last_updated = datetime.now()
        self.check_question_turn_count = 0
        self.last_check_score = None
        self.user_id = user_id
        self.db_session = db_session
        logger.info(f"[RISK_HISTORY] RiskHistory 객체 생성: user_id={user_id}, check_question_turn_count={self.check_question_turn_count}")
    def add_turn(self, text: str) -> Dict:
        """새로운 턴을 추가하고 위험도를 분석합니다."""
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
        current_turn_score = turn_analysis['score']
        cumulative_score = self.get_cumulative_score()
        logger.info(f"[RISK] 턴 추가: 현재 {current_turn_score}점, 누적 {cumulative_score}점 (턴 {len(self.turns)}/20)")
        return turn_analysis
    def get_cumulative_score(self) -> int:
        """최근 턴들의 누적 위험도 점수를 계산합니다. 시간 기반 감점 없이 순수 누적만 적용."""
        if not self.turns:
            return 0
        recent_turns = list(self.turns)[-5:] if len(self.turns) > 5 else list(self.turns)
        turn_details = [f"턴{i+1}: {turn['score']}점" for i, turn in enumerate(recent_turns)]
        if len(self.turns) > 5:
            logger.info(f"[RISK] 최근 5턴 점수: {' | '.join(turn_details)} (총 {len(self.turns)}턴)")
        else:
            logger.info(f"[RISK] 턴별 점수: {' | '.join(turn_details)}")
        raw_total = sum(turn['score'] for turn in self.turns)
        final_score = max(0, min(100, raw_total))
        logger.info(f"[RISK] 누적 점수: {raw_total} → {final_score}점")
        return final_score
    def get_risk_trend(self) -> str:
        """위험도 변화 추세를 분석합니다."""
        if len(self.turns) < 2:
            return "stable"
        recent_scores = [turn['score'] for turn in list(self.turns)[-5:]]
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
        self.check_question_turn_count = 20  # 20턴 카운트다운 시작
        logger.info(f"[RISK_HISTORY] 체크 질문 발송 기록: {old_count} -> {self.check_question_turn_count} (호출 스택: {self._get_caller_info()})")
        if self.user_id and self.db_session:
            try:
                import asyncio
                from app.database.service import mark_check_question_sent
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                loop.run_until_complete(mark_check_question_sent(self.db_session, self.user_id))
            except Exception as e:
                logger.error(f"[RISK_HISTORY] DB 체크 질문 발송 기록 실패: {e}")
    def _get_caller_info(self) -> str:
        """호출자 정보를 반환합니다."""
        import inspect
        try:
            frame = inspect.currentframe()
            caller_frame = frame.f_back
            if caller_frame:
                filename = caller_frame.f_code.co_filename
                function = caller_frame.f_code.co_name
                line = caller_frame.f_lineno
                return f"{filename}:{function}:{line}"
        except:
            pass
        return "unknown"
    def sync_with_database(self):
        """데이터베이스와 메모리 상태를 동기화합니다."""
        if self.user_id and self.db_session:
            try:
                import asyncio
                from app.database.service import get_check_question_turn
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                db_turn = loop.run_until_complete(get_check_question_turn(self.db_session, self.user_id))
                logger.info(f"[RISK_HISTORY] DB 동기화 시도: 현재={self.check_question_turn_count}, DB={db_turn}")
                if db_turn != self.check_question_turn_count:
                    old_count = self.check_question_turn_count
                    self.check_question_turn_count = db_turn
                    logger.info(f"[RISK_HISTORY] DB 동기화 완료: {old_count} -> {self.check_question_turn_count}")
            except Exception as e:
                logger.error(f"[RISK_HISTORY] DB 동기화 실패: {e}")
    def can_send_check_question(self) -> bool:
        """체크 질문을 발송할 수 있는지 확인합니다."""
        can_send = self.check_question_turn_count == 0
        logger.info(f"[RISK_HISTORY] 체크 질문 발송 가능 여부: check_question_turn_count={self.check_question_turn_count}, can_send={can_send}")
        return can_send
    def process_check_question_response(self, response_text: str) -> Optional[int]:
        """체크 질문 응답을 처리하고 점수를 저장합니다."""
        logger.info(f"[RISK_HISTORY] 체크 질문 응답 처리 시작: '{response_text}'")
        parsed_score = parse_check_response(response_text)
        if parsed_score is not None:
            self.last_check_score = parsed_score
            logger.info(f"[RISK_HISTORY] 체크 질문 응답 점수 저장: {parsed_score}")
        else:
            logger.info(f"[RISK_HISTORY] 체크 질문 응답 파싱 실패")
        return parsed_score
    def reset_check_question_state(self):
        """체크 질문 관련 상태를 초기화합니다."""
        old_score = self.last_check_score
        old_count = self.check_question_turn_count
        self.last_check_score = None
        self.check_question_turn_count = 0
        logger.info(f"[RISK_HISTORY] 체크 질문 상태 초기화: last_check_score={old_score}->None, check_question_turn_count={old_count}->0")
    def _analyze_single_turn(self, text: str) -> Dict:
        """단일 텍스트의 위험도를 분석합니다."""
        if not text:
            return {'score': 0, 'flags': {}, 'evidence': []}
        text_lower = text.strip().lower()
        logger.info(f"[RISK] 입력: '{text[:30]}...'")
        flags = self._get_flags(text_lower)
        # 메타언어, 3인칭, 관용어가 포함된 경우 점수 계산 제외
        if flags["meta"] or flags["third"] or flags["idiom"]:
            logger.info(f"[RISK] 메타/3인칭/관용어로 점수 계산 제외")
            return {'score': 0, 'flags': flags, 'evidence': []}
        total_score = 0
        evidence = []
        matched_positions = set()
        # 각 점수별 정규식 패턴 검사 (높은 점수부터 순서대로)
        for score in sorted(RISK_PATTERNS.keys(), reverse=True):
            for pattern in RISK_PATTERNS[score]:
                matches = list(pattern.finditer(text_lower))
                if matches:
                    for match in matches:
                        start, end = match.start(), match.end()
                        matched_text = match.group()
                        if any(start < pos_end and end > pos_start for pos_start, pos_end in matched_positions):
                            continue
                        # 특별한 위험 표현은 부정어가 있어도 점수 부여
                        special_danger_patterns = ["살고싶지않", "살고싶지 않", "살고싶진 않"]
                        is_special_danger = any(pat in matched_text for pat in special_danger_patterns)
                        # 부정어 포함 시 차감(특수 위험 표현 제외)
                        actual_score = 0 if (flags["neg"] and not is_special_danger) else score
                        # 과거시제 포함 시 7점 이상은 2점 감점
                        if flags["past"] and score >= 7:
                            old_score = actual_score
                            actual_score = max(0, actual_score - 2)
                        if actual_score > 0:
                            total_score += actual_score
                            evidence.append({
                                "keyword": matched_text,
                                "score": actual_score,
                                "original_score": score,
                                "excerpt": self._get_context(text_lower, start, end)
                            })
                            matched_positions.add((start, end))
                            break
        # 긍정 발화 감점
        if P_POSITIVE.search(text_lower):
            evidence.append({
                "keyword": "긍정_발화",
                "score": -2,
                "original_score": -2,
                "excerpt": "긍정적인 발화로 인한 감점"
            })
            final_score = total_score + (-2)
            logger.info(f"[RISK] 긍정 발화 감지: -2점, 최종 턴 점수={final_score}")
        else:
            final_score = total_score
        if evidence:
            keywords = [f"{ev['keyword']}({ev['score']}점)" for ev in evidence if ev['keyword'] != '긍정_발화']
            if keywords:
                logger.info(f"[RISK] 키워드 감지: {', '.join(keywords)} → {final_score}점")
            else:
                logger.info(f"[RISK] 점수: {final_score}점")
        else:
            logger.info(f"[RISK] 점수: {final_score}점")
        return {
            'score': final_score,
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
    """
    if risk_history:
        turn_analysis = risk_history.add_turn(text)
        cumulative_score = risk_history.get_cumulative_score()
        return cumulative_score, turn_analysis['flags'], risk_history.get_recent_evidence()
    else:
        if not text:
            return 0, {}, []
        text_lower = text.strip().lower()
        flags = _get_flags(text_lower)
        if flags["meta"] or flags["third"] or flags["idiom"]:
            return 0, flags, []
        total_score = 0
        evidence = []
        for score, patterns in RISK_PATTERNS.items():
            for pattern in patterns:
                matches = pattern.finditer(text_lower)
                for match in matches:
                    matched_text = match.group()
                    special_danger_patterns = ["살고싶지않", "살고싶지 않", "살고싶진 않"]
                    is_special_danger = any(pat in matched_text for pat in special_danger_patterns)
                    actual_score = 0 if (flags["neg"] and not is_special_danger) else score
                    if flags["past"] and score >= 7:
                        actual_score = max(0, actual_score - 2)
                    if actual_score > 0:
                        total_score += actual_score
                        evidence.append({
                            "keyword": matched_text,
                            "score": actual_score,
                            "original_score": score,
                            "excerpt": _get_context(text_lower, match.start(), match.end())
                        })
        if P_POSITIVE.search(text_lower):
            evidence.append({
                "keyword": "긍정_발화",
                "score": -2,
                "original_score": -2,
                "excerpt": "긍정적인 발화로 인한 감점"
            })
            final_score = 0
            logger.info(f"[RISK] 긍정 발화 감지: -2점, 턴 점수=0")
        else:
            final_score = total_score
        if evidence:
            keywords = [f"{ev['keyword']}({ev['score']}점)" for ev in evidence if ev['keyword'] != '긍정_발화']
            if keywords:
                logger.info(f"[RISK] 키워드 감지: {', '.join(keywords)} → {final_score}점")
            else:
                logger.info(f"[RISK] 점수: {final_score}점")
        else:
            logger.info(f"[RISK] 점수: {final_score}점")
        return final_score, flags, evidence

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
    if score < 8:
        logger.info(f"[CHECK_CONDITION] 점수 조건 미충족: {score} < 8")
        return False
    logger.info(f"[CHECK_CONDITION] 점수 조건 충족: {score} >= 8")
    if risk_history:
        can_send = risk_history.can_send_check_question()
        logger.info(f"[CHECK_CONDITION] RiskHistory 조건 확인: can_send={can_send}, check_question_turn_count={risk_history.check_question_turn_count}")
        if can_send:
            logger.info(f"[CHECK_CONDITION] can_send가 True이므로 체크 질문 발송 가능")
            return True
        logger.info(f"[CHECK_CONDITION] can_send가 False이므로 체크 질문 발송 불가")
        return False
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
    text_clean = text.strip().lower()
    exact_match = re.match(r'^(0|1|2|3|4|5|6|7|8|9|10)$', text_clean)
    if exact_match:
        score = int(exact_match.group())
        logger.info(f"[PARSE_DEBUG] 정확한 점수 매칭: {score}")
        return score
    point_pattern = re.match(r'^(\d+)\s*점?$', text_clean)
    if point_pattern:
        score = int(point_pattern.group(1))
        logger.info(f"[PARSE_DEBUG] 점수 패턴 매칭: {score}점")
        if 0 <= score <= 10:
            logger.info(f"[PARSE_DEBUG] 유효한 점수 확인: {score}")
            return score
        else:
            logger.info(f"[PARSE_DEBUG] 점수 범위 초과: {score} (0-10 범위 아님)")
    numbers = re.findall(r'\b([0-9]|10)\b', text_clean)
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

# get_risk_level 제거: 위험도 레벨 문자열 기반 로직 미사용

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
        return "다시 한 번 0부터 10까지 숫자로 알려줄래? 예: 0, 1, 2 ..."

def process_check_question_response(response_text: str, risk_history: RiskHistory = None) -> Tuple[Optional[int], str]:
    """
    체크 질문 응답을 처리하고 점수와 대응 메시지를 반환합니다.
    
    Args:
        response_text: 사용자의 체크 질문 응답 텍스트
        risk_history: 위험도 히스토리 객체 (제공시 상태 업데이트)
    
    Returns:
        Tuple[Optional[int], str]: (파싱된 점수, 대응 메시지)
    """
    logger.info(f"[PROCESS_CHECK] 체크 질문 응답 처리 시작: '{response_text}', risk_history={risk_history is not None}")
    
    # 점수 파싱
    parsed_score = parse_check_response(response_text)
    
    if parsed_score is not None:
        # RiskHistory가 제공된 경우 상태 업데이트
        if risk_history:
            risk_history.process_check_question_response(response_text)
            logger.info(f"[PROCESS_CHECK] RiskHistory 상태 업데이트 완료: last_check_score={risk_history.last_check_score}")
        
        # 대응 메시지 생성
        response_message = get_check_response_message(parsed_score)
        
        logger.info(f"[PROCESS_CHECK] 체크 질문 응답 처리 완료: score={parsed_score}, message_length={len(response_message)}")
        
        return parsed_score, response_message
    else:
        # 파싱 실패시 재질문 메시지
        invalid_message = get_invalid_score_message()
        
        logger.info(f"[PROCESS_CHECK] 체크 질문 응답 파싱 실패: 재질문 메시지 반환")
        
        return None, invalid_message

def is_check_question_response(text: str) -> bool:
    """
    텍스트가 체크 질문에 대한 응답인지 확인합니다.
    
    Args:
        text: 확인할 텍스트
        
    Returns:
        bool: 체크 질문 응답이면 True, 아니면 False
    """
    if not text:
        return False
    
    # 점수 파싱 시도
    parsed_score = parse_check_response(text)
    
    # 파싱이 성공하면 체크 질문 응답으로 간주
    is_response = parsed_score is not None
    
    logger.info(f"[CHECK_RESPONSE_DETECT] 체크 질문 응답 감지: text='{text[:20]}...', is_response={is_response}, parsed_score={parsed_score}")
    
    return is_response
