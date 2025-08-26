#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
risk_mvp.py의 체크 질문 응답 처리 기능 테스트 스크립트
"""

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), 'app'))

from risk_mvp import RiskHistory, process_check_question_response, is_check_question_response

def test_check_question_response():
    """체크 질문 응답 처리 기능을 테스트합니다."""
    print("=== 체크 질문 응답 처리 테스트 시작 ===\n")
    
    # RiskHistory 인스턴스 생성
    risk_history = RiskHistory()
    
    print(f"초기 상태:")
    print(f"  - last_check_score: {risk_history.last_check_score}")
    print(f"  - check_question_turn_count: {risk_history.check_question_turn_count}")
    print(f"  - can_send_check_question: {risk_history.can_send_check_question()}")
    print()
    
    # 체크 질문 발송 시뮬레이션
    print("1. 체크 질문 발송...")
    risk_history.mark_check_question_sent()
    print(f"  - check_question_turn_count: {risk_history.check_question_turn_count}")
    print(f"  - can_send_check_question: {risk_history.can_send_check_question()}")
    print()
    
    # 체크 질문 응답 처리 테스트
    test_responses = [
        "5",
        "8점",
        "3 점",
        "10",
        "안녕하세요",  # 잘못된 응답
        "15",  # 범위 초과
        "0"
    ]
    
    for i, response in enumerate(test_responses, 1):
        print(f"{i}. 응답 처리: '{response}'")
        
        # 응답이 체크 질문 응답인지 확인
        is_response = is_check_question_response(response)
        print(f"   - 체크 질문 응답 여부: {is_response}")
        
        if is_response:
            # 응답 처리
            score, message = process_check_question_response(response, risk_history)
            print(f"   - 파싱된 점수: {score}")
            print(f"   - 대응 메시지: {message[:50]}...")
            print(f"   - last_check_score: {risk_history.last_check_score}")
            print(f"   - check_question_turn_count: {risk_history.check_question_turn_count}")
            print(f"   - can_send_check_question: {risk_history.can_send_check_question()}")
        else:
            print(f"   - 체크 질문 응답이 아님")
        
        print()
    
    # 상태 초기화 테스트
    print("상태 초기화 테스트:")
    risk_history.reset_check_question_state()
    print(f"  - last_check_score: {risk_history.last_check_score}")
    print(f"  - check_question_turn_count: {risk_history.check_question_turn_count}")
    print(f"  - can_send_check_question: {risk_history.can_send_check_question()}")
    print()
    
    print("=== 테스트 완료 ===")

if __name__ == "__main__":
    test_check_question_response()
