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

### Step 2: 기본 챗봇 테스트
```
POST /skill (콜백 없음)
```
- 즉시 AI 응답을 받을 수 있음
- 대화 히스토리가 저장되는지 확인

### Step 3: 프롬프트 관리 테스트
```
GET /admin/prompts (목록 조회)
POST /admin/prompts (새 프롬프트 생성)
GET /admin/prompts/{name} (특정 프롬프트 조회)
POST /admin/prompts/{id}/activate (프롬프트 활성화)
```

### Step 4: 연속 대화 테스트
같은 `user_id`로 여러 번 요청해서 대화 컨텍스트가 유지되는지 확인

## 🧪 주요 테스트 케이스

### 1. 기본 동작 테스트
**Request:**
```json
{
  "userRequest": {
    "utterance": "안녕하세요, 도움이 필요해요",
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
          "text": "안녕하세요! 무엇을 도와드릴까요?"
        }
      }
    ]
  }
}
```

### 2. 콜백 테스트
**Request:**
```json
{
  "userRequest": {
    "utterance": "제품 문의드립니다",
    "user": {
      "id": "test-user-456"
    }
  },
  "callbackUrl": "https://httpbin.org/post"
}
```

**Immediate Response:**
```json
{
  "version": "2.0",
  "template": {
    "outputs": [
      {
        "simpleText": {
          "text": "답변을 생성 중입니다… 잠시만 기다려 주세요."
        }
      }
    ]
  }
}
```

### 3. 프롬프트 생성 테스트
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

## 📊 모니터링

### 로그 확인
```bash
# 서버 로그에서 다음 정보 확인 가능
- X-Request-ID 추적
- OpenAI API 호출 상태
- 토큰 사용량
- 에러 상세 정보
```

### 데이터베이스 확인
```sql
-- 사용자 확인
SELECT * FROM appuser;

-- 대화 세션 확인  
SELECT * FROM conversation ORDER BY started_at DESC;

-- 메시지 확인 (토큰 사용량 포함)
SELECT * FROM message ORDER BY created_at DESC;

-- 프롬프트 템플릿 확인
SELECT * FROM prompttemplate WHERE is_active = true;
```

## 🎯 성능 테스트

### 동시 요청 테스트
여러 사용자가 동시에 요청할 때의 성능 확인:

```bash
# Apache Bench 예시
ab -n 100 -c 10 -T application/json -p test_payload.json http://localhost:8000/skill
```

### 응답 시간 측정
- 콜백 없는 요청: 보통 2-5초 (OpenAI API 응답 시간에 따라)
- 콜백 있는 요청: 즉시 응답 (200ms 이내)

이제 카카오 콜백을 기다리지 않고도 모든 기능을 완전히 테스트할 수 있습니다! 🚀
