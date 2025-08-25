#!/usr/bin/env python3
"""
PostgreSQL 데이터베이스 연결 테스트 스크립트
"""

import asyncio
import sys
import os

# 프로젝트 루트 디렉토리를 Python 경로에 추가
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

async def test_postgresql_connection():
    """PostgreSQL 데이터베이스 연결을 테스트합니다."""
    
    print("=== PostgreSQL 데이터베이스 연결 테스트 ===\n")
    
    try:
        # 1. 설정 확인
        from app.config import settings
        print(f"1. 데이터베이스 URL: {settings.database_url}")
        
        # 2. PostgreSQL 연결 확인
        if "postgresql" not in settings.database_url.lower():
            print("   ❌ PostgreSQL 연결 문자열이 아닙니다!")
            print("   📝 .env 파일에 DATABASE_URL을 설정하세요")
            return
        
        # 3. 데이터베이스 초기화 테스트
        from app.database.db import init_db
        print("\n2. PostgreSQL 데이터베이스 초기화 시도...")
        
        success = await init_db()
        if success:
            print("   ✅ PostgreSQL 데이터베이스 초기화 성공!")
        else:
            print("   ❌ PostgreSQL 데이터베이스 초기화 실패")
            print("   🔍 연결 문자열과 데이터베이스 접근 권한을 확인하세요")
            return
        
        # 4. 세션 생성 테스트
        from app.database.db import get_session
        print("\n3. PostgreSQL 세션 생성 테스트...")
        
        async for session in get_session():
            try:
                # PostgreSQL 특화 쿼리 실행
                from sqlalchemy import text
                result = await session.execute(text("SELECT version()"))
                row = result.fetchone()
                print(f"   ✅ PostgreSQL 세션 생성 성공!")
                print(f"   📊 PostgreSQL 버전: {row[0]}")
                break
            except Exception as e:
                print(f"   ❌ PostgreSQL 세션 테스트 실패: {e}")
                break
        
        # 5. risk_state 테이블 생성 테스트
        print("\n4. risk_state 테이블 생성 테스트...")
        try:
            from app.database.models import RiskState
            from sqlmodel import select
            
            async for session in get_session():
                try:
                    # 테이블 존재 확인
                    result = await session.execute(select(RiskState).limit(1))
                    print("   ✅ risk_state 테이블 접근 성공")
                    break
                except Exception as e:
                    print(f"   ❌ risk_state 테이블 접근 실패: {e}")
                    break
        
        except Exception as e:
            print(f"   ❌ risk_state 모델 임포트 실패: {e}")
        
        # 6. 데이터 저장 테스트
        print("\n5. PostgreSQL 데이터 저장 테스트...")
        try:
            from app.database.service import get_or_create_risk_state, update_risk_score
            
            async for session in get_session():
                try:
                    # 테스트 사용자 생성
                    test_user_id = "test_user_001"
                    risk_state = await get_or_create_risk_state(session, test_user_id)
                    print(f"   ✅ risk_state 생성 성공: user_id={risk_state.user_id}, score={risk_state.score}")
                    
                    # 점수 업데이트 테스트
                    await update_risk_score(session, test_user_id, 25)
                    print(f"   ✅ 점수 업데이트 성공: score=25")
                    
                    # 최종 상태 확인
                    final_state = await session.get(RiskState, test_user_id)
                    print(f"   📊 최종 상태: score={final_state.score}, last_updated={final_state.last_updated}")
                    
                    break
                except Exception as e:
                    print(f"   ❌ PostgreSQL 데이터 저장 테스트 실패: {e}")
                    break
        
        except Exception as e:
            print(f"   ❌ 서비스 함수 임포트 실패: {e}")
        
        print("\n" + "="*60)
        print("🎯 PostgreSQL 데이터베이스 연결 테스트 완료!")
        print("="*60)
        
    except Exception as e:
        print(f"❌ 테스트 실행 실패: {e}")
        import traceback
        traceback.print_exc()

async def main():
    """메인 함수"""
    await test_postgresql_connection()

if __name__ == "__main__":
    asyncio.run(main())
