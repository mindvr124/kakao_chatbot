import asyncio
from datetime import datetime
from typing import Optional, Tuple
from sqlmodel import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger
import uuid

from app.database.models import AIProcessingTask, AIProcessingStatus, Message
from app.core.ai_service import ai_service
from app.utils.utils import remove_markdown

class AIProcessingService:
    """AI 처리 작업을 비동기로 관리하는 서비스"""
    
    def __init__(self):
        self.processing_tasks = {}  # 메모리상의 작업 상태 추적
        
    async def create_processing_task(
        self, 
        session: AsyncSession, 
        conv_id: str, 
        user_input: str, 
        request_id: Optional[str] = None
    ) -> AIProcessingTask:
        """AI 처리 작업을 생성합니다."""
        task = AIProcessingTask(
            conv_id=conv_id,
            user_input=user_input,
            status=AIProcessingStatus.PENDING,
            request_id=request_id
        )
        
        session.add(task)
        await session.commit()
        await session.refresh(task)
        
        # 메모리에 작업 상태 저장
        self.processing_tasks[task.task_id] = {
            "status": AIProcessingStatus.PENDING,
            "conv_id": conv_id,
            "user_input": user_input
        }
        
        logger.info(f"Created AI processing task: {task.task_id}")
        return task
    
    async def start_processing(
        self, 
        session: AsyncSession, 
        task_id: str
    ) -> bool:
        """AI 처리 작업을 시작합니다."""
        try:
            # DB 상태 업데이트
            stmt = (
                update(AIProcessingTask)
                .where(AIProcessingTask.task_id == task_id)
                .values(
                    status=AIProcessingStatus.PROCESSING,
                    started_at=datetime.utcnow()
                )
            )
            await session.execute(stmt)
            await session.commit()
            
            # 메모리 상태 업데이트
            if task_id in self.processing_tasks:
                self.processing_tasks[task_id]["status"] = AIProcessingStatus.PROCESSING
            
            logger.info(f"Started AI processing task: {task_id}")
            return True
            
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
        """AI 처리 작업을 완료합니다."""
        try:
            # DB 상태 업데이트
            stmt = (
                update(AIProcessingTask)
                .where(AIProcessingTask.task_id == task_id)
                .values(
                    status=AIProcessingStatus.COMPLETED,
                    completed_at=datetime.utcnow(),
                    result_message_id=result_message_id
                )
            )
            await session.execute(stmt)
            await session.commit()
            
            # 메모리에서 작업 제거
            if task_id in self.processing_tasks:
                del self.processing_tasks[task_id]
            
            logger.info(f"Completed AI processing task: {task_id}")
            return True
            
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
        """AI 처리 작업을 실패 처리합니다."""
        try:
            # 재시도 여부 결정
            if should_retry:
                # 현재 재시도 횟수 확인
                stmt = select(AIProcessingTask).where(AIProcessingTask.task_id == task_id)
                result = await session.execute(stmt)
                task = result.scalar_one()
                
                if task and task.retry_count < task.max_retries:
                    # 재시도 가능한 경우
                    stmt = (
                        update(AIProcessingTask)
                        .where(AIProcessingTask.task_id == task_id)
                        .values(
                            status=AIProcessingStatus.PENDING,
                            retry_count=task.retry_count + 1,
                            error_message=error_message,
                            started_at=None,
                            completed_at=None
                        )
                    )
                    await session.execute(stmt)
                    await session.commit()
                    
                    # 메모리 상태 업데이트
                    if task_id in self.processing_tasks:
                        self.processing_tasks[task_id]["status"] = AIProcessingStatus.PENDING
                    
                    logger.info(f"Retrying AI processing task: {task_id} (attempt {task.retry_count + 1})")
                    return True
                else:
                    # 재시도 불가능한 경우
                    stmt = (
                        update(AIProcessingTask)
                        .where(AIProcessingTask.task_id == task_id)
                        .values(
                            status=AIProcessingStatus.FAILED,
                            error_message=error_message,
                            completed_at=datetime.utcnow()
                        )
                    )
                    await session.execute(stmt)
                    await session.commit()
                    
                    # 메모리에서 작업 제거
                    if task_id in self.processing_tasks:
                        del self.processing_tasks[task_id]
                    
                    logger.error(f"AI processing task failed permanently: {task_id}")
                    return False
            else:
                # 재시도하지 않는 경우
                stmt = (
                    update(AIProcessingTask)
                    .where(AIProcessingTask.task_id == task_id)
                    .values(
                        status=AIProcessingStatus.FAILED,
                        error_message=error_message,
                        completed_at=datetime.utcnow()
                    )
                )
                await session.execute(stmt)
                await session.commit()
                
                # 메모리에서 작업 제거
                if task_id in self.processing_tasks:
                    del self.processing_tasks[task_id]
                
                logger.error(f"AI processing task failed: {task_id}")
                return False
                
        except Exception as e:
            logger.error(f"Failed to handle AI processing failure {task_id}: {e}")
            return False
    
    async def get_task_status(
        self, 
        session: AsyncSession, 
        task_id: str
    ) -> Optional[AIProcessingTask]:
        """작업 상태를 조회합니다."""
        stmt = select(AIProcessingTask).where(AIProcessingTask.task_id == task_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()
    
    async def process_ai_task(
        self, 
        session: AsyncSession, 
        task_id: str,
        prompt_name: str = "default"
    ) -> Tuple[bool, Optional[str], Optional[int]]:
        """AI 작업을 실제로 처리합니다."""
        try:
            # 작업 정보 조회
            task = await self.get_task_status(session, task_id)
            if not task:
                return False, "Task not found", None
            
            # 처리 시작
            await self.start_processing(session, task_id)
            
            # AI 응답 생성
            final_text, tokens_used = await ai_service.generate_response(
                session=session,
                conv_id=task.conv_id,
                user_input=task.user_input,
                prompt_name=prompt_name
            )
            
            # AI 응답을 DB에 저장
            ai_message = Message(
                conv_id=task.conv_id,
                role="assistant",
                content=remove_markdown(final_text),
                request_id=task.request_id,
                tokens=tokens_used
            )
            
            session.add(ai_message)
            await session.commit()
            await session.refresh(ai_message)
            
            # 작업 완료 처리
            await self.complete_processing(
                session, 
                task_id, 
                str(ai_message.msg_id),
                tokens_used
            )
            
            logger.info(f"Successfully processed AI task: {task_id}")
            return True, final_text, tokens_used
            
        except Exception as e:
            error_msg = f"AI processing failed: {str(e)}"
            logger.error(f"Task {task_id} failed: {error_msg}")
            
            # 실패 처리 (재시도 가능)
            await self.fail_processing(session, task_id, error_msg, should_retry=True)
            return False, error_msg, None

# 전역 AI 처리 서비스 인스턴스
ai_processing_service = AIProcessingService()
