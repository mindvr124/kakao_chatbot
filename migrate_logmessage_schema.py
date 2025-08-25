#!/usr/bin/env python3
"""
LogMessage 테이블 스키마 마이그레이션 스크립트
source 컬럼을 VARCHAR에서 JSONB로 변경
"""

import asyncio
import asyncpg
import os
from dotenv import load_dotenv

load_dotenv()

async def migrate_logmessage_schema():
    """LogMessage 테이블의 source 컬럼을 JSONB로 변경"""
    
    # 데이터베이스 연결
    database_url = os.getenv('DATABASE_URL')
    if not database_url:
        print("❌ DATABASE_URL 환경변수가 설정되지 않았습니다.")
        return
    
    try:
        # PostgreSQL 연결
        conn = await asyncpg.connect(database_url)
        print("✅ 데이터베이스 연결 성공")
        
        # 1. 기존 source 컬럼 백업 (필요시)
        print("📋 기존 source 데이터 백업 중...")
        backup_result = await conn.fetch("""
            SELECT log_id, source 
            FROM logmessage 
            WHERE source IS NOT NULL AND source != ''
        """)
        print(f"📊 백업된 레코드 수: {len(backup_result)}")
        
        # 2. source 컬럼 타입을 JSONB로 변경
        print("🔧 source 컬럼을 JSONB로 변경 중...")
        await conn.execute("""
            ALTER TABLE logmessage 
            ALTER COLUMN source TYPE JSONB USING 
            CASE 
                WHEN source IS NULL THEN NULL
                WHEN source = '' THEN NULL
                ELSE source::JSONB
            END
        """)
        print("✅ source 컬럼을 JSONB로 변경 완료")
        
        # 3. 테이블 구조 확인
        print("📋 변경된 테이블 구조 확인 중...")
        table_info = await conn.fetch("""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns 
            WHERE table_name = 'logmessage' AND column_name = 'source'
        """)
        
        for row in table_info:
            print(f"   {row['column_name']}: {row['data_type']} (NULL 허용: {row['is_nullable']})")
        
        print("🎉 마이그레이션 완료!")
        
    except Exception as e:
        print(f"❌ 마이그레이션 실패: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        if 'conn' in locals():
            await conn.close()
            print("🔌 데이터베이스 연결 종료")

if __name__ == "__main__":
    asyncio.run(migrate_logmessage_schema())
