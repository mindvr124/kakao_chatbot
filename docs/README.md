# 카카오 비즈니스 AI 챗봇

GPT-4o를 활용한 카카오 비즈니스 AI 상담 챗봇입니다.

## 주요 기능

- 🤖 **GPT-4o 연동**: temperature 0.2로 일관성 있는 응답 생성
- 📝 **프롬프트 관리**: 데이터베이스 기반 프롬프트 템플릿 관리 및 버전 관리
- 💬 **대화 히스토리**: 사용자별 대화 기록 및 컨텍스트 유지
- ⚡ **비동기 AI 처리**: 카카오 5초 제한 대응을 위한 백그라운드 AI 처리 시스템
- 🔄 **자동 재시도**: AI 처리 실패 시 자동 재시도 메커니즘
- 📊 **토큰 추적**: OpenAI API 사용량 모니터링
- 🔧 **관리자 API**: 프롬프트 및 AI 작업 관리를 위한 REST API
- 👥 **사용자 API**: AI 처리 상태 확인 및 최신 응답 조회
- 👤 **스마트 이름 관리**: 사용자 이름 자동 인식 및 변경 기능
- 📋 **통합 로깅 시스템**: 구조화된 로그 및 이벤트 추적

## 🆕 최신 업데이트 (2024년 8월)

### ✨ 새로운 기능
- **사용자 이름 자동 인식**: "내 이름은 ~야", "난 ~야" 등 패턴에서 이름 자동 추출
- **이름 변경 기능**: "다른 이름", "이름 바꿔" 등으로 이름 변경 가능
- **통합 로깅**: PromptLog, EventLog, LogMessage 테이블을 통한 체계적인 로그 관리
- **데이터베이스 스키마 개선**: PromptLog와 Message 테이블 1:1 관계로 통합

### 🔧 기술적 개선
- **SQLAlchemy 비동기 최적화**: expire_on_commit=False 설정으로 ORM 객체 속성 만료 방지
- **로깅 가독성 향상**: 텍스트 프리픽스와 개행문자로 로그 구조화
- **이름 추출 정규식**: 한글 이름 패턴 인식 및 검증 로직 강화

## 비동기 AI 처리 시스템

### 문제 해결
카카오 비즈니스는 요청에 대해 5초 이내 응답을 요구합니다. AI 생성이 느릴 경우 이 제한을 초과하여 실패할 수 있습니다.

### 해결 방법
1. **즉시 응답**: 사용자 요청에 대해 즉시 "AI가 답변을 생성하고 있어요!" 메시지 반환
2. **백그라운드 처리**: AI 처리를 백그라운드에서 비동기로 진행
3. **콜백 전송**: AI 처리 완료 후 callbackUrl로 최종 응답 전송
4. **상태 추적**: AI 처리 상태를 데이터베이스에서 추적
5. **자동 재시도**: 실패 시 최대 3회까지 자동 재시도

### 아키텍처
```
사용자 요청 → 즉시 응답 → AI 작업 생성 → 백그라운드 처리 → 콜백 전송
     ↓              ↓           ↓           ↓           ↓
  5초 제한    카카오 응답   작업 큐     AI 워커     최종 메시지
```

### ⚠️ 중요: 카카오 콜백 설정 필수
카카오는 "임의의 시점에 푸시"가 불가능하며, 스킬 응답은 '즉시응답' 또는 '콜백 응답'만 허용됩니다.

**반드시 설정해야 할 것:**
- 카카오 관리자센터 → 스킬 → 응답 방식: **콜백 활성화**

**하지 말 것:**
- 콜백 OFF 상태에서 장시간 작업 후 임의로 사용자에게 메시지 보내기

## 👤 사용자 이름 관리 시스템

### 🎯 자동 이름 인식
사용자가 다음과 같은 패턴으로 입력하면 자동으로 이름을 인식합니다:

```
"내 이름은 민수야" → 이름: "민수"로 저장
"난 영희야" → 이름: "영희"로 저장  
"저는 철수라고 해요" → 이름: "철수"로 저장
```

### 🔄 이름 변경 기능
기존 사용자는 다음 명령어로 이름을 변경할 수 있습니다:

```
"다른 이름" → 이름 변경 모드로 전환
"이름 바꿔" → 이름 변경 모드로 전환
"이름 변경" → 이름 변경 모드로 전환
```

### 📝 이름 관리 명령어
- `/이름` - 이름 입력 모드로 전환
- `/이름 홍길동` - 즉시 "홍길동"으로 이름 설정
- `취소` - 이름 변경 모드 취소

### ✅ 이름 검증 규칙
- **길이**: 1~20자
- **문자**: 한글, 영문, 숫자, 중점(·), 하이픈(-), 언더스코어(_)
- **예시**: 민수, Yeonwoo, 김철수, user123

## 설치 및 실행

### 1. 환경 설정

```bash
# .env 파일 생성
cp .env.example .env

# 환경변수 설정
# DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/chatdb
# OPENAI_API_KEY=your_openai_api_key_here
```

### 2. 서버 실행

#### 방법 1: Python 스크립트로 실행 (권장)
```bash
# 프로젝트 루트에서
python run_server.py
```

#### 방법 2: Windows 배치 파일로 실행
```bash
# 프로젝트 루트에서
run_server.bat
```

#### 방법 3: 직접 uvicorn 실행
```bash
# logging.ini 설정 적용
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --log-config logging.ini --access-log false
```

### 3. 로깅 설정

프로젝트에는 `logging.ini` 파일이 포함되어 있어 SQL 쿼리 에코와 uvicorn 로깅을 자동으로 제어합니다:

- **SQLAlchemy**: `echo=False`로 설정하여 쿼리 로그 비활성화
- **Uvicorn**: WARNING 레벨로 설정하여 불필요한 로그 차단
- **Access Log**: CRITICAL 레벨로 설정하여 HTTP 요청 로그 최소화

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
- `POST /skill` - 카카오 챗봇 스킬 엔드포인트 (비동기 AI 처리)
- `POST /welcome` - 웰컴 스킬 엔드포인트

### 테스트 및 디버깅
- `POST /test-skill` - 스킬 테스트 엔드포인트
- `POST /test-callback` - 콜백 테스트 엔드포인트
- `POST /test-name-extraction` - 이름 추출 테스트 엔드포인트

### 사용자 API
- `GET /user/ai-status/{task_id}` - AI 처리 상태 조회
- `GET /user/conversation/{conv_id}/latest-ai-response` - 최신 AI 응답 조회

### 관리자 API
- `GET /admin/health` - 상세 헬스체크 (AI 워커 상태 포함)
- `GET /admin/ai-tasks` - AI 처리 작업 목록 조회
- `POST /admin/ai-tasks/{task_id}/retry` - 실패한 AI 작업 재시도
- `GET /admin/prompts` - 프롬프트 템플릿 목록 조회
- `POST /admin/prompts` - 새 프롬프트 템플릿 생성
- `GET /admin/prompts/{name}` - 특정 프롬프트 조회
- `POST /admin/prompts/{prompt_id}/activate` - 프롬프트 활성화

### 헬스체크
- `GET /health` - 기본 헬스체크

## AI 처리 작업 관리

### 1. 작업 상태 조회

```bash
# 특정 작업의 상태 확인
curl "http://localhost:8000/user/ai-status/{task_id}"

# 응답 예시
{
  "task_id": "uuid",
  "status": "completed",
  "ai_response": "AI가 생성한 답변...",
  "tokens_used": 150,
  "created_at": "2024-01-01T00:00:00",
  "completed_at": "2024-01-01T00:00:05"
}
```

### 2. AI 작업 목록 조회 (관리자)

```bash
# 모든 AI 작업 조회
curl "http://localhost:8000/admin/ai-tasks"

# 특정 상태의 작업만 조회
curl "http://localhost:8000/admin/ai-tasks?status=failed"
```

### 3. 실패한 작업 재시도

```bash
# 실패한 작업 재시도
curl -X POST "http://localhost:8000/admin/ai-tasks/{task_id}/retry"
```

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

## 🎯 카카오 설정 (필수)

### 1. 콜백 활성화
카카오 관리자센터에서 반드시 콜백을 활성화해야 합니다:

1. **카카오 비즈니스 관리자센터** 접속
2. **스킬 관리** → 해당 스킬 선택
3. **응답 방식** → **콜백 활성화** 선택
4. **저장**

### 2. 콜백 URL 설정
- **개발 환경**: `http://localhost:8000/skill` (ngrok 등으로 외부 노출)
- **프로덕션**: 실제 도메인 URL (예: `https://your-domain.com/skill`)

### 3. 콜백 테스트
콜백이 제대로 작동하는지 확인:

#### 방법 1: 카카오 테스트 채널
- 카카오 테스트 채널에서 메시지 전송
- 즉시 "처리 중" 응답 확인
- 잠시 후 최종 AI 응답 확인

#### 방법 2: 로컬 테스트 (ngrok 사용)
```bash
# 1. ngrok으로 로컬 서버 외부 노출
ngrok http 8000

# 2. 카카오 관리자센터에서 콜백 URL을 ngrok URL로 설정
# 예: https://abc123.ngrok.io/skill

# 3. 테스트 메시지 전송
curl -X POST "https://abc123.ngrok.io/skill" \
-H "Content-Type: application/json" \
-d '{
  "userRequest": {
    "utterance": "테스트 메시지",
    "user": {"id": "test-user"}
  },
  "callbackUrl": "https://abc123.ngrok.io/test-callback"
}'
```

#### 방법 3: 콜백 시뮬레이터
- `/test-callback` 엔드포인트로 콜백 데이터 확인
- 실제 카카오 콜백과 동일한 형식으로 테스트 가능

## 데이터베이스 스키마

### 주요 테이블
- `appuser`: 사용자 정보 (이름 포함)
- `conversation`: 대화 세션
- `message`: 메시지 기록
- `promptlog`: 프롬프트 로그 (Message와 1:1 관계)
- `eventlog`: 이벤트 로그 (이름 변경, 대화 시작 등)
- `logmessage`: 일반 로그 메시지
- `prompttemplate`: 프롬프트 템플릿
- `usersummary`: 사용자 대화 요약
- `aiprocessingtask`: AI 처리 작업 상태 추적

### 🔄 최신 스키마 변경사항
- **PromptLog 테이블**: `log_id` 제거, `msg_id`를 primary key로 사용
- **Message와 PromptLog**: 1:1 관계로 통합
- **CounselSummary 테이블**: 제거됨
- **LogMessage 테이블**: 새로운 통합 로깅 시스템

### AI 처리 작업 상태
- `pending`: 대기 중
- `processing`: 처리 중
- `completed`: 완료
- `failed`: 실패

## 📋 로깅 시스템

### 로그 레벨별 구조화
```
[생성] 새 사용자 생성: user123 | 이름: 민수
[변경] 사용자 이름 변경: user123 | '민수' -> '철수'
[완료] 사용자 이름 변경 완료: user123 -> 철수
[로그] 이름 변경 로그 저장 완료: user123
```

### 로그 테이블
- **PromptLog**: AI 프롬프트 및 응답 로그
- **EventLog**: 사용자 행동 및 시스템 이벤트
- **LogMessage**: 일반 애플리케이션 로그

## 개발 및 배포

### 로컬 개발
```bash
uvicorn app.main:app --reload
```

### 프로덕션 배포
- Render, Railway, AWS ECS 등에서 Docker 이미지로 배포 가능
- 환경변수를 적절히 설정하여 사용

## 모니터링

- `/admin/health` 엔드포인트에서 시스템 상태 및 AI 워커 상태 확인 가능
- `/admin/ai-tasks`에서 AI 처리 작업 현황 모니터링
- 로그는 loguru를 통해 구조화되어 출력
- OpenAI API 토큰 사용량이 데이터베이스에 기록됨

## 성능 최적화

### AI 워커 설정
- 기본적으로 2초마다 새로운 작업을 확인
- 최대 3개 작업을 동시에 병렬 처리
- 실패한 작업은 최대 3회까지 자동 재시도

### 데이터베이스 최적화
- AI 처리 작업 상태에 대한 인덱스 설정
- 메시지 및 작업 테이블의 효율적인 쿼리
- SQLAlchemy 세션 설정 최적화 (`expire_on_commit=False`)

## 주의사항

- OpenAI API 키는 반드시 환경변수로 설정
- 프로덕션 환경에서는 적절한 인증/인가 시스템 추가 권장
- 데이터베이스 백업 및 모니터링 시스템 구축 필요
- AI 워커가 백그라운드에서 지속적으로 실행되므로 리소스 모니터링 필요
- 사용자 이름은 한글/영문 1~20자 제한을 준수해야 함
