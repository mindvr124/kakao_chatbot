#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
risk_mvp.py의 체크 질문 응답 처리 기능 테스트 스크립트
데이터베이스 연동 기능도 포함
"""

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), 'app'))

from risk_mvp import RiskHistory, process_check_question_response, is_check_question_response

def test_check_question_response():
    """체크 질문 응답 처리 기능을 테스트합니다."""
    print("=== 체크 질문 응답 처리 테스트 시작 ===\n")

    # RiskHistory 인스턴스 생성 (데이터베이스 없이)
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

def test_database_integration():
    """데이터베이스 연동 기능을 테스트합니다."""
    print("=== 데이터베이스 연동 테스트 시작 ===\n")
    
    try:
        # 데이터베이스 세션과 사용자 ID가 있는 경우 테스트
        user_id = "test_user_123"
        
        # 실제 데이터베이스 세션 생성 시도
        try:
            from app.database.db import get_session
            db_session = None
            # 동기적으로 세션 생성 시도
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # 이미 실행 중인 루프가 있으면 새로 생성
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                
                async def get_session_sync():
                    async for session in get_session():
                        return session
                    return None
                
                db_session = loop.run_until_complete(get_session_sync())
            except Exception as loop_error:
                print(f"이벤트 루프 생성 실패: {loop_error}")
                db_session = None
        except Exception as db_error:
            print(f"데이터베이스 연결 실패: {db_error}")
            db_session = None
        
        print(f"데이터베이스 연동 테스트:")
        print(f"  - user_id: {user_id}")
        print(f"  - db_session: {db_session}")
        if db_session:
            print(f"  - 데이터베이스 세션 연결 성공")
        else:
            print(f"  - 데이터베이스 세션이 없어서 메모리에서만 동작")
        print()
        
        # RiskHistory 인스턴스 생성
        if db_session:
            risk_history = RiskHistory(user_id=user_id, db_session=db_session)
            print(f"RiskHistory 생성 완료 (데이터베이스 연동):")
        else:
            risk_history = RiskHistory(user_id=user_id)
            print(f"RiskHistory 생성 완료 (메모리만):")
        
        print(f"  - user_id: {risk_history.user_id}")
        print(f"  - check_question_turn_count: {risk_history.check_question_turn_count}")
        print()
        
        # 체크 질문 발송 테스트
        print("체크 질문 발송 테스트:")
        risk_history.mark_check_question_sent()
        print(f"  - check_question_turn_count: {risk_history.check_question_turn_count}")
        print(f"  - can_send_check_question: {risk_history.can_send_check_question()}")
        print()
        
        # 턴 추가 테스트 (카운트다운)
        print("턴 추가 테스트 (카운트다운):")
        for i in range(5):
            risk_history.add_turn(f"테스트 턴 {i+1}")
            print(f"  - 턴 {i+1}: check_question_turn_count = {risk_history.check_question_turn_count}")
        print()
        
        print("=== 데이터베이스 연동 테스트 완료 ===")
        
    except Exception as e:
        print(f"데이터베이스 연동 테스트 실패: {e}")
        print("=== 데이터베이스 연동 테스트 실패 ===")

if __name__ == "__main__":
    test_check_question_response()
    print("\n" + "="*50 + "\n")
    test_database_integration()
