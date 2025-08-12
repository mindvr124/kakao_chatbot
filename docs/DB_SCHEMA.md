## 데이터베이스 스키마 및 저장 흐름 개요

본 문서는 챗봇 서비스의 DB 테이블 구조, 컬럼 의미, 테이블 간 관계, 그리고 `/skill` 처리 시 데이터가 저장되는 흐름(즉시응답/콜백)을 정리합니다.

### 테이블 요약

- AppUser: 사용자 식별자 보관(최초 접속 시 생성)
- Conversation: 사용자별 대화 세션(대화 묶음)
- Message: 대화 내 개별 발화(유저/AI/시스템)
- AIProcessingTask: 비동기 AI 처리/재시도/콜백 상태 추적
- PromptTemplate: 시스템/유저 프롬프트 템플릿(버전/활성화)

---

## AppUser

- **목적**: 카카오 등 외부 플랫폼에서 넘어온 사용자 식별자를 내부에서 일관되게 관리
- **주요 컬럼**
  - `user_id (PK)`: 사용자 고유 식별자
  - `created_at`: 최초 등록 시각

---

## Conversation

- **목적**: 사용자별 대화를 세션 단위로 구분(요약/종료 시각 등 메타 정보 보관)
- **주요 컬럼**
  - `conv_id (PK, UUID)`: 대화 세션 고유 ID
  - `user_id (FK -> AppUser.user_id)`: 대화 소유 사용자 ID
  - `started_at`: 대화 시작 시각
  - `ended_at (nullable)`: 대화 종료 시각
  - `summary (nullable)`: 대화 요약 텍스트

---

## Message

- **목적**: 대화 내 개별 메시지(유저/AI/시스템) 로그 저장
- **주요 컬럼**
  - `msg_id (PK, UUID)`: 메시지 고유 ID
  - `conv_id (FK -> Conversation.conv_id)`: 소속 대화 ID
  - `role (enum)`: `user` | `assistant` | `system`
  - `content`: 메시지 텍스트(유저 입력 또는 AI 응답)
  - `tokens (nullable)`: 토큰 사용량(응답 생성 시)
  - `request_id (nullable)`: 요청 추적용 ID(예: `X-Request-ID`)
  - `created_at`: 생성 시각

---

## AIProcessingTask

- **목적**: 비동기 AI 처리 파이프라인(큐잉/재시도/콜백)의 상태를 추적
- **주요 컬럼**
  - `task_id (PK, UUID)`: 작업 고유 ID
  - `conv_id (FK -> Conversation.conv_id)`: 연관 대화 ID
  - `user_input`: 해당 작업의 원 발화(유저 입력)
  - `status (enum)`: `pending` | `processing` | `completed` | `failed`
  - `request_id (nullable)`: 추적용 요청 ID
  - `error_message (nullable)`: 에러 상세
  - `retry_count` / `max_retries`: 재시도 관리
  - `created_at` / `started_at (nullable)` / `completed_at (nullable)`: 수명주기 타임스탬프
  - `result_message_id (nullable, FK -> Message.msg_id)`: 결과 메시지 참조

---

## PromptTemplate

- **목적**: 챗봇 응답 품질/스타일 제어를 위한 프롬프트 템플릿 관리
- **주요 컬럼**
  - `prompt_id (PK, UUID)`: 템플릿 고유 ID
  - `name (indexed)`: 템플릿 이름(예: `default`)
  - `version (indexed)`: 버전(최신 우선)
  - `system_prompt`: 시스템 프롬프트 문자열
  - `user_prompt_template (nullable)`: 사용자 입력에 삽입할 템플릿
  - `is_active (indexed)`: 활성화 여부
  - `description (nullable)`: 설명
  - `created_at (indexed)` / `created_by (nullable)`: 생성 정보

---

## 테이블 관계(요약)

- `AppUser (1) — (N) Conversation`
- `Conversation (1) — (N) Message`
- `Conversation (1) — (N) AIProcessingTask`
- `AIProcessingTask (1) — (0..1) Message` (결과 메시지 참조)

---

## 저장 흐름(요청 처리)

### 1) 즉시응답 경로(콜백 미사용 또는 비활성화)
1. `/skill` 진입 → 요청 파싱, `user_id`/`utterance` 추출
2. DB 연결 가능 시 `AppUser` upsert → `Conversation` 조회/생성
3. 시간 예산(BUDGET) 내 `AI 응답 생성` 시도 → 성공 시 템플릿 JSON 즉시 반환
4. 메시지 저장(`Message`): 유저 발화/AI 응답은 백그라운드 태스크로 저장(성능 안정)
5. 응답 헤더: `Content-Type: application/json; charset=utf-8`

DB 장애 시: 임시 conv_id로 진행하며 사용자 응답은 계속 즉시 반환(메시지 저장은 생략/실패 로그)

### 2) 콜백 경로(콜백 URL 존재, ENABLE_CALLBACK=True)
1. 5초 제한 내 남은 시간으로 `동기 응답` 1차 시도 → 성공 시 바로 응답
2. 시간이 부족/실패 시, 즉시 `{ "version":"2.0", "useCallback": true, ... }` 반환
3. 백그라운드 태스크에서 순차 처리
   - 독립 DB 세션 확보 → `AppUser` upsert → `Conversation` 생성/조회
   - BUDGET 가드로 `AI 응답 생성`
   - `Message` 저장(assistant)
   - 전역 httpx 클라이언트로 콜백 POST(헤더: `application/json; charset=utf-8`)

---

## 운영 팁

- 시간 예산(BUDGET): 카카오 5초 제한 대비 4.x초 내로 동기 응답을 설계
- 콜백 활성화: `ENABLE_CALLBACK`를 통해 운영 환경별로 on/off 제어
- 추적: `Message.request_id`(X-Request-ID)를 저장하면 트래킹 용이
- 장애 내성: DB 실패 시에도 사용자 응답은 즉시 반환되도록 설계(로그 레벨로 상태 관찰)
- 스펙 준수: 카카오 응답은 반드시 `version: "2.0"` + `template.outputs[].simpleText.text`

---

## 예시: 카카오 응답 형식

### 즉시 응답(일반)
```json
{
  "version": "2.0",
  "template": {
    "outputs": [
      { "simpleText": { "text": "최종 답변" } }
    ]
  }
}
```

### 즉시 응답(콜백 사용)
```json
{
  "version": "2.0",
  "useCallback": true,
  "data": { "text": "답변을 생성 중입니다..." }
}
```

### 콜백 본문
```json
{
  "version": "2.0",
  "template": {
    "outputs": [
      { "simpleText": { "text": "최종 답변" } }
    ]
  }
}
```


