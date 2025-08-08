# 카카오 비즈니스 AI 챗봇

GPT-4o를 활용한 카카오 비즈니스 AI 상담 챗봇입니다.

## 주요 기능

- 🤖 **GPT-4o 연동**: temperature 0.2로 일관성 있는 응답 생성
- 📝 **프롬프트 관리**: 데이터베이스 기반 프롬프트 템플릿 관리 및 버전 관리
- 💬 **대화 히스토리**: 사용자별 대화 기록 및 컨텍스트 유지
- ⚡ **비동기 처리**: 카카오 5초 제한 대응을 위한 콜백 패턴
- 📊 **토큰 추적**: OpenAI API 사용량 모니터링
- 🔧 **관리자 API**: 프롬프트 관리를 위한 REST API

## 설치 및 실행

### 1. 환경 설정

```bash
# .env 파일 생성
cp .env.example .env

# 환경변수 설정
# DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/chatdb
# OPENAI_API_KEY=your_openai_api_key_here
```

### 2. 패키지 설치

```bash
pip install -r requirements.txt
```

### 3. 데이터베이스 설정

PostgreSQL 데이터베이스를 준비하고 연결 정보를 .env 파일에 설정하세요.

### 4. 서버 실행

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### 5. Docker 실행 (선택사항)

```bash
docker build -t kakao-ai-chatbot .
docker run -p 8000:8000 --env-file .env kakao-ai-chatbot
```

## API 엔드포인트

### 카카오 스킬 서버
- `POST /skill` - 카카오 챗봇 스킬 엔드포인트

### 관리자 API
- `GET /admin/health` - 상세 헬스체크
- `GET /admin/prompts` - 프롬프트 템플릿 목록 조회
- `POST /admin/prompts` - 새 프롬프트 템플릿 생성
- `GET /admin/prompts/{name}` - 특정 프롬프트 조회
- `POST /admin/prompts/{prompt_id}/activate` - 프롬프트 활성화

### 헬스체크
- `GET /health` - 기본 헬스체크

## 프롬프트 관리 사용법

### 1. 새 프롬프트 생성

```bash
curl -X POST "http://localhost:8000/admin/prompts" \
-H "Content-Type: application/json" \
-d '{
  "name": "customer_service",
  "system_prompt": "당신은 고객 서비스 담당자입니다...",
  "description": "고객 서비스 전용 프롬프트"
}'
```

### 2. 프롬프트 목록 조회

```bash
curl "http://localhost:8000/admin/prompts"
```

### 3. 프롬프트 활성화

```bash
curl -X POST "http://localhost:8000/admin/prompts/{prompt_id}/activate"
```

## 환경변수 설정

| 변수명 | 설명 | 기본값 |
|--------|------|--------|
| `DATABASE_URL` | PostgreSQL 연결 URL | `postgresql+asyncpg://user:pass@localhost:5432/chatdb` |
| `OPENAI_API_KEY` | OpenAI API 키 | 필수 |
| `OPENAI_MODEL` | 사용할 모델 | `gpt-4o` |
| `OPENAI_TEMPERATURE` | 응답 일관성 설정 | `0.2` |
| `OPENAI_MAX_TOKENS` | 최대 토큰 수 | `1000` |
| `SESSION_TIMEOUT_MINUTES` | 세션 타임아웃 (분) | `30` |
| `PORT` | 서버 포트 | `8000` |
| `LOG_LEVEL` | 로그 레벨 | `INFO` |
| `DEBUG` | 디버그 모드 | `false` |

## 데이터베이스 스키마

### 주요 테이블
- `appuser`: 사용자 정보
- `conversation`: 대화 세션
- `message`: 메시지 기록
- `prompttemplate`: 프롬프트 템플릿

## 개발 및 배포

### 로컬 개발
```bash
uvicorn app.main:app --reload
```

### 프로덕션 배포
- Render, Railway, AWS ECS 등에서 Docker 이미지로 배포 가능
- 환경변수를 적절히 설정하여 사용

## 모니터링

- `/admin/health` 엔드포인트에서 시스템 상태 확인 가능
- 로그는 loguru를 통해 구조화되어 출력
- OpenAI API 토큰 사용량이 데이터베이스에 기록됨

## 주의사항

- OpenAI API 키는 반드시 환경변수로 설정
- 프로덕션 환경에서는 적절한 인증/인가 시스템 추가 권장
- 데이터베이스 백업 및 모니터링 시스템 구축 필요
