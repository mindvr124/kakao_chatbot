import asyncio
from datetime import datetime
from typing import Optional, Tuple
from sqlmodel import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger
import uuid

from app.database.models import AIProcessingStatus, Message
from app.core.ai_service import ai_service
from app.utils.utils import remove_markdown

class AIProcessingService:
    """AI 처리 작업을 비동기로 관리하는 서비스"""
    
    def __init__(self):
        self.processing_tasks = {}  # 메모리상의 작업 상태 추적
        
    async def create_processing_task(self, session: AsyncSession, conv_id: str, user_input: str, request_id: Optional[str] = None):
        logger.info("AI task queue disabled: create_processing_task no-op")
        return None
    
    async def start_processing(
        self, 
        session: AsyncSession, 
        task_id: str
    ) -> bool:
        """AI 처리 작업을 시작합니다"""
        try:
            logger.info("AI task queue disabled: start_processing no-op")
            return False
            
        except Exception as e:
            logger.error(f"Failed to start AI processing task {task_id}: {e}")
            return False
    
    async def complete_processing(
        self, 
        session: AsyncSession, 
        task_id: str, 
        result_message_id: str,
        tokens_used: int = 0
    ) -> bool:
        """AI 처리 작업을 완료합니다"""
        try:
            logger.info("AI task queue disabled: complete_processing no-op")
            return False
            
        except Exception as e:
            logger.error(f"Failed to complete AI processing task {task_id}: {e}")
            return False
    
    async def fail_processing(
        self, 
        session: AsyncSession, 
        task_id: str, 
        error_message: str,
        should_retry: bool = True
    ) -> bool:
        """AI 처리 작업 실패를 처리합니다"""
        try:
            # 재시도 여부 결정
            if should_retry:
                logger.info("AI task queue disabled: fail_processing no-op")
                return False
            else:
                # 재시도하지 않는 경우
                logger.info("AI task queue disabled: fail_processing no-op")
                return False
                
        except Exception as e:
            logger.error(f"Failed to handle AI processing failure {task_id}: {e}")
            return False
    
    async def get_task_status(
        self, 
        session: AsyncSession, 
        task_id: str
    ) -> Optional[object]:
        """작업 상태를 조회합니다"""
        logger.info("AI task queue disabled: get_task_status no-op")
        return None
    
    async def process_ai_task(
        self, 
        session: AsyncSession, 
        task_id: str,
        prompt_name: str = "default"
    ) -> Tuple[bool, Optional[str], Optional[int]]:
        """AI 작업을 실제로 처리합니다"""
        logger.info("AI task queue disabled: process_ai_task no-op")
        return False, "queue disabled", None

# 전역 AI 처리 서비스 인스턴스
ai_processing_service = AIProcessingService()
