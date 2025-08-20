# API 테스트 가이드

## 🚀 빠른 시작

### 1. Postman 컬렉션 가져오기
1. Postman을 열고 `Import` 클릭
2. `postman_collection.json` 파일 선택
3. 컬렉션이 추가되면 `Variables` 탭에서 `base_url`을 확인 (기본값: `http://localhost:8000`)

### 2. 서버 실행
```bash
uvicorn app.main:app --reload
```

## 📋 테스트 순서

### Step 1: 헬스체크
```
GET /health
GET /admin/health
```
- 서버가 정상 동작하는지 확인
- OpenAI API 키 설정 여부 확인
- AI 워커 상태 확인

### Step 2: 사용자 이름 관리 테스트 (새로운 기능)
```
POST /test-name-extraction (이름 추출 테스트)
POST /skill (이름 자동 인식 테스트)
POST /skill (이름 변경 테스트)
```
- 이름 패턴 인식이 올바르게 작동하는지 확인
- 이름 변경 기능이 정상 동작하는지 확인
- 로깅 시스템이 제대로 작동하는지 확인

### Step 3: 비동기 AI 처리 테스트 (콜백 활성화 필수)
```
POST /skill (비동기 AI 처리 + 콜백)
GET /user/ai-status/{task_id} (AI 처리 상태 확인)
GET /user/conversation/{conv_id}/latest-ai-response (최신 AI 응답 조회)
```
- 즉시 응답을 받을 수 있는지 확인
- AI 처리 상태가 올바르게 추적되는지 확인
- 콜백을 통해 최종 응답이 전송되는지 확인

**⚠️ 중요: 테스트 전 카카오 콜백 설정 확인**
- 카카오 관리자센터 → 스킬 → 응답 방식: **콜백 활성화**
- 콜백이 비활성화된 상태에서는 최종 응답을 받을 수 없음

### Step 4: AI 작업 관리 테스트 (관리자)
```
GET /admin/ai-tasks (AI 작업 목록 조회)
POST /admin/ai-tasks/{task_id}/retry (실패한 작업 재시도)
```

### Step 5: 프롬프트 관리 테스트
```
GET /admin/prompts (목록 조회)
POST /admin/prompts (새 프롬프트 생성)
GET /admin/prompts/{name} (특정 프롬프트 조회)
POST /admin/prompts/{id}/activate (프롬프트 활성화)
```

### Step 6: 연속 대화 테스트
같은 `user_id`로 여러 번 요청해서 대화 컨텍스트가 유지되는지 확인

## 🧪 주요 테스트 케이스

### 1. 사용자 이름 관리 테스트 (새로운 기능)

#### 1-1. 이름 추출 테스트
**Request:**
```bash
POST /test-name-extraction
```
```json
{
  "text": "내 이름은 민수야"
}
```

**Expected Response:**
```json
{
  "status": "success",
  "result": {
    "original": "내 이름은 민수야",
    "extracted_name": "민수",
    "cleaned_name": "민수",
    "is_valid": true,
    "patterns_removed": ["내 이름은", "야"],
    "validation_errors": []
  }
}
```

#### 1-2. 이름 자동 인식 테스트
**Request:**
```json
{
  "userRequest": {
    "utterance": "내 이름은 영희야",
    "user": {
      "id": "test-user-123"
    }
  }
}
```

**Expected Response:**
```json
{
  "version": "2.0",
  "template": {
    "outputs": [
      {
        "simpleText": {
          "text": "반가워 영희아(야)! 앞으로 영희(이)라고 부를게🦉"
        }
      }
    ]
  }
}
```

#### 1-3. 이름 변경 테스트
**Request:**
```json
{
  "userRequest": {
    "utterance": "다른 이름",
    "user": {
      "id": "test-user-123"
    }
  }
}
```

**Expected Response:**
```json
{
  "version": "2.0",
  "template": {
    "outputs": [
      {
        "simpleText": {
          "text": "현재 '영희'로 알고 있는데, 어떤 이름으로 바꾸고 싶어?"
        }
      }
    ]
  }
}
```

**이름 변경 완료 후:**
```json
{
  "userRequest": {
    "utterance": "철수",
    "user": {
      "id": "test-user-123"
    }
  }
}
```

**Expected Response:**
```json
{
  "version": "2.0",
  "template": {
    "outputs": [
      {
        "simpleText": {
          "text": "좋아! 앞으로는 '철수'(이)라고 불러줄게~"
        }
      }
    ]
  }
}
```

### 2. 비동기 AI 처리 테스트 (콜백 방식)
**Request:**
```json
{
  "userRequest": {
    "utterance": "안녕하세요, 도움이 필요해요",
    "user": {
      "id": "test-user-123"
    }
  },
  "callbackUrl": "https://your-callback-endpoint.com/callback"
}
```

**Immediate Response (5초 이내):**
```json
{
  "version": "2.0",
  "template": {
    "outputs": [
      {
        "simpleText": {
          "text": "🤖 AI가 답변을 생성하고 있어요!\n잠시만 기다려 주세요..."
        }
      }
    ]
  }
}
```

**AI 처리 상태 확인:**
```bash
# 응답에서 task_id를 추출하여 상태 확인
GET /user/ai-status/{task_id}
```

**Expected Status Response:**
```json
{
  "task_id": "uuid",
  "status": "completed",
  "ai_response": "안녕하세요! 무엇을 도와드릴까요?",
  "tokens_used": 150,
  "created_at": "2024-01-01T00:00:00",
  "completed_at": "2024-01-01T00:00:05"
}
```

**콜백 응답 확인:**
- AI 처리 완료 후 callbackUrl로 최종 응답이 POST 전송됨
- 콜백 응답은 카카오 플랫폼을 통해 사용자에게 전달됨

### 3. AI 작업 목록 조회 테스트 (관리자)
**Request:**
```bash
GET /admin/ai-tasks
GET /admin/ai-tasks?status=completed
GET /admin/ai-tasks?status=failed
```

**Expected Response:**
```json
{
  "tasks": [
    {
      "task_id": "uuid",
      "conv_id": "uuid",
      "status": "completed",
      "user_input": "안녕하세요, 도움이 필요해요",
      "retry_count": 0,
      "created_at": "2024-01-01T00:00:00",
      "started_at": "2024-01-01T00:00:01",
      "completed_at": "2024-01-01T00:00:05",
      "error_message": null,
      "result_message_id": "uuid"
    }
  ],
  "total": 1
}
```

### 4. 실패한 작업 재시도 테스트
**Request:**
```bash
POST /admin/ai-tasks/{task_id}/retry
```

**Expected Response:**
```json
{
  "message": "Task queued for retry",
  "task_id": "uuid"
}
```

### 5. 최신 AI 응답 조회 테스트
**Request:**
```bash
GET /user/conversation/{conv_id}/latest-ai-response
```

**Expected Response:**
```json
{
  "message_id": "uuid",
  "content": "AI가 생성한 답변 내용...",
  "created_at": "2024-01-01T00:00:05",
  "tokens": 150
}
```

### 6. 프롬프트 생성 테스트
**Request:**
```json
{
  "name": "customer_service",
  "system_prompt": "당신은 전문적인 고객 서비스 상담사입니다...",
  "description": "고객 서비스 전용 상담봇 프롬프트"
}
```

## 🔧 환경변수 설정 확인

테스트 전에 다음 환경변수가 설정되어 있는지 확인하세요:

```bash
# .env 파일 예시
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/chatdb
OPENAI_API_KEY=sk-your-openai-api-key-here
OPENAI_MODEL=gpt-4o
OPENAI_TEMPERATURE=0.2
SESSION_TIMEOUT_MINUTES=30
```

## 🐛 트러블슈팅

### 1. OpenAI API 에러
- `.env` 파일에 `OPENAI_API_KEY`가 올바르게 설정되어 있는지 확인
- API 키에 충분한 크레딧이 있는지 확인

### 2. 데이터베이스 연결 에러
- PostgreSQL이 실행 중인지 확인
- `DATABASE_URL`이 올바른지 확인
- 데이터베이스가 존재하는지 확인

### 3. 400 에러 (user_id not found)
- 요청 body에 `userRequest.user.id`가 포함되어 있는지 확인

### 4. AI 워커 관련 에러
- `/admin/health`에서 AI 워커 상태 확인
- 서버 로그에서 AI 워커 시작/중지 메시지 확인

### 5. AI 작업 상태 확인 에러
- `task_id`가 올바른지 확인
- 데이터베이스에 `aiprocessingtask` 테이블이 생성되었는지 확인

### 6. 이름 관리 관련 에러 (새로운 기능)
- `AppUser` 테이블에 `user_name` 컬럼이 생성되었는지 확인
- `EventLog`와 `LogMessage` 테이블이 생성되었는지 확인
- 로그에서 이름 변경 관련 메시지 확인

## 📊 모니터링

### 로그 확인
```bash
# 서버 로그에서 다음 정보 확인 가능
- X-Request-ID 추적
- AI 워커 상태 및 작업 처리 현황
- OpenAI API 호출 상태
- 토큰 사용량
- 에러 상세 정보
- 재시도 횟수 및 상태
- 사용자 이름 변경 이벤트 (새로운 기능)
- 구조화된 로그 메시지 (새로운 기능)
```

### 데이터베이스 확인
```sql
-- 사용자 및 이름 확인
SELECT user_id, user_name, created_at FROM appuser;

-- 대화 세션 확인  
SELECT * FROM conversation ORDER BY started_at DESC;

-- 메시지 확인 (토큰 사용량 포함)
SELECT * FROM message ORDER BY created_at DESC;

-- 프롬프트 템플릿 확인
SELECT * FROM prompttemplate WHERE is_active = true;

-- AI 처리 작업 상태 확인
SELECT * FROM aiprocessingtask ORDER BY created_at DESC;

-- 실패한 AI 작업 확인
SELECT * FROM aiprocessingtask WHERE status = 'failed';

-- 재시도 횟수별 작업 현황
SELECT status, retry_count, COUNT(*) 
FROM aiprocessingtask 
GROUP BY status, retry_count;

-- 이름 변경 이벤트 로그 확인 (새로운 기능)
SELECT event_type, user_id, details_json, created_at 
FROM eventlog 
WHERE event_type LIKE 'name_%' 
ORDER BY created_at DESC;

-- 일반 로그 메시지 확인 (새로운 기능)
SELECT level, message, user_id, source, created_at 
FROM logmessage 
WHERE level = 'INFO' 
ORDER BY created_at DESC;

-- 프롬프트 로그 확인 (새로운 스키마)
SELECT msg_id, conv_id, model, prompt_name, created_at 
FROM promptlog 
ORDER BY created_at DESC;
```

## 🎯 성능 테스트

### 동시 요청 테스트
여러 사용자가 동시에 요청할 때의 성능 확인:

```bash
# Apache Bench 예시
ab -n 100 -c 10 -T application/json -p test_payload.json http://localhost:8000/skill
```

### 응답 시간 측정
- **즉시 응답**: 200ms 이내 (카카오 5초 제한 준수)
- **AI 처리**: 백그라운드에서 진행 (사용자 응답 시간과 무관)
- **상태 확인**: 100ms 이내
- **이름 관리**: 100ms 이내 (새로운 기능)

### AI 워커 성능 테스트
```bash
# AI 워커 상태 확인
GET /admin/health

# 동시에 여러 AI 작업 생성하여 병렬 처리 확인
# 여러 사용자로 동시 요청
```

## 🔄 비동기 처리 흐름 테스트

### 1. 전체 흐름 테스트
1. 사용자 요청 전송 (`POST /skill`)
2. 즉시 응답 확인 (5초 이내)
3. AI 작업 상태 확인 (`GET /user/ai-status/{task_id}`)
4. 완료 후 최신 응답 확인 (`GET /user/conversation/{conv_id}/latest-ai-response`)

### 2. 에러 처리 테스트
1. 잘못된 API 키로 테스트하여 AI 처리 실패 유도
2. 실패한 작업 상태 확인
3. 재시도 기능 테스트
4. 최대 재시도 횟수 초과 시 영구 실패 상태 확인

### 3. 워커 재시작 테스트
1. 서버 재시작
2. AI 워커 자동 시작 확인
3. 대기 중인 작업 처리 확인

### 4. 이름 관리 흐름 테스트 (새로운 기능)
1. 이름 자동 인식 테스트 (`POST /skill` with name pattern)
2. 이름 변경 요청 테스트 (`POST /skill` with "다른 이름")
3. 새 이름 입력 테스트 (`POST /skill` with new name)
4. 이벤트 로그 및 로그 메시지 확인

## 🆕 새로운 기능 테스트 체크리스트

### ✅ 이름 관리 기능
- [ ] 이름 패턴 인식 (`/이름`, "내 이름은 ~야", "난 ~야" 등)
- [ ] 이름 변경 요청 ("다른 이름", "이름 바꿔" 등)
- [ ] 이름 검증 (한글/영문 1~20자)
- [ ] 이름 변경 취소 ("취소", "그만" 등)

### ✅ 로깅 시스템
- [ ] 이벤트 로그 저장 (`EventLog` 테이블)
- [ ] 일반 로그 메시지 저장 (`LogMessage` 테이블)
- [ ] 구조화된 로그 출력 (텍스트 프리픽스, 개행문자)
- [ ] 이름 변경 관련 로그 추적

### ✅ 데이터베이스 스키마
- [ ] `AppUser.user_name` 필드 정상 동작
- [ ] `PromptLog`와 `Message` 1:1 관계
- [ ] `CounselSummary` 테이블 제거 확인
- [ ] 새로운 로깅 테이블들 정상 생성

### ✅ API 엔드포인트
- [ ] `/test-name-extraction` 정상 동작
- [ ] 이름 관리 관련 `/skill` 응답 정상
- [ ] 기존 API들 정상 동작 유지

이제 카카오 5초 제한을 우회하는 비동기 AI 처리 시스템과 사용자 이름 관리 기능을 완전히 테스트할 수 있습니다! 🚀
