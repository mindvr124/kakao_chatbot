import asyncio
from typing import Optional
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger
import time

from app.database.models import AIProcessingStatus
from app.core.ai_processing_service import ai_processing_service
from app.database.db import AsyncSessionLocal

class AIWorker:
    """백그라운드에서 AI 작업을 처리하는 워커"""
    
    def __init__(self):
        self.is_running = False
        self.worker_task = None
        self.polling_interval = 2  # 2초마다 새로운 작업 확인
        
    async def start(self):
        """워커를 시작합니다"""
        if self.is_running:
            logger.warning("AI Worker is already running")
            return
            
        self.is_running = True
        self.worker_task = asyncio.create_task(self._worker_loop())
        logger.info("AI Worker started")
    
    async def stop(self):
        """워커를 중지합니다"""
        if not self.is_running:
            return
            
        self.is_running = False
        if self.worker_task:
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                pass
        logger.info("AI Worker stopped")
    
    async def _worker_loop(self):
        """메인 워커 루프"""
        while self.is_running:
            try:
                await self._process_pending_tasks()
                await asyncio.sleep(self.polling_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker loop error: {e}")
                await asyncio.sleep(5)  # 에러 발생 시 5초 대기
    
    async def _process_pending_tasks(self):
        """대기중인 작업들을 처리합니다"""
        session = AsyncSessionLocal()
        try:
            # 이 기능 비활성화로 아무 것도 하지 않음
            return
            
        except Exception as e:
            logger.error(f"Error processing pending tasks: {e}")
        finally:
            await session.close()
    
    async def _process_single_task(self, task_id: str):
        """단일 AI 작업을 처리합니다"""
        session = AsyncSessionLocal()
        try:
            success, result, tokens = await ai_processing_service.process_ai_task(
                session, task_id, "default"
            )
            
            if success:
                logger.info(f"Task {task_id} completed successfully")
            else:
                logger.error(f"Task {task_id} failed: {result}")
                
        except Exception as e:
            logger.error(f"Error processing task {task_id}: {e}")
            # 작업 실패 처리
            await ai_processing_service.fail_processing(
                session, task_id, str(e), should_retry=True
            )
        finally:
            await session.close()
    
    async def get_worker_status(self) -> dict:
        """워커 상태를 반환합니다"""
        return {
            "is_running": self.is_running,
            "polling_interval": self.polling_interval
        }

# 전역 AI 워커 인스턴스
ai_worker = AIWorker()
