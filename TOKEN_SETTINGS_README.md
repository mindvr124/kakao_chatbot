# 🎯 토큰 설정 가이드

## 📋 개요

이 시스템은 **요약**과 **채팅**을 구분하여 각각에 최적화된 토큰 설정을 제공합니다.

## 🔧 설정 항목

### 📝 요약용 토큰 설정 (완전한 요약 보장)

```python
# 요약용 토큰 설정 (완전한 요약 보장)
openai_summary_max_tokens: int = 1200        # 요약용 토큰 (중간에 잘리면 안됨)
openai_summary_auto_continue: bool = True    # 요약은 자동 이어받기 필수
openai_summary_auto_continue_max_segments: int = 8  # 요약은 더 많은 세그먼트 허용
```

**목적**: 요약이 중간에 잘리지 않도록 완전한 내용을 보장
- **max_tokens**: 1200 (충분한 길이)
- **auto_continue**: True (자동 이어받기 필수)
- **max_segments**: 8 (많은 세그먼트 허용)

### 💬 채팅 응답용 토큰 설정 (적당한 길이 유지)

```python
# 채팅 응답용 토큰 설정 (적당한 길이 유지)
openai_max_tokens: int = 150                 # 채팅 응답 길이 (적당한 길이 유지)
openai_auto_continue: bool = True            # 채팅 자동 이어받기
openai_auto_continue_max_segments: int = 3   # 채팅 이어받기 세그먼트 수 (길게 이어갈 필요 없음)
openai_dynamic_max_tokens: bool = True       # 채팅 동적 토큰 조정
openai_dynamic_max_tokens_cap: int = 800     # 채팅 최대 토큰 (너무 길게 나오면 안됨)
```

**목적**: 채팅 응답이 너무 길어지지 않도록 적당한 길이 유지
- **max_tokens**: 150 (적당한 길이)
- **auto_continue**: True (자동 이어받기)
- **max_segments**: 3 (적은 세그먼트)
- **dynamic_max_tokens**: True (동적 조정)
- **dynamic_cap**: 800 (최대 제한)

## 🚀 자동 감지 시스템

### 요약 요청 감지 조건

다음 키워드가 포함된 경우 자동으로 요약용 토큰 설정을 사용합니다:

- `"요약"` - 요약 관련 요청
- `"summary"` - 프롬프트 이름에 포함
- `"롤업"` - 롤업 요약
- `"병합"` - 요약 병합
- `"중복 없이"` - 요약 정리

### 채팅 요청 감지

위의 요약 조건에 해당하지 않는 모든 요청은 채팅용 토큰 설정을 사용합니다.

## 📊 설정 권장사항

### 🎯 요약용 (완전성 우선)
- **max_tokens**: 1000-1500 (충분한 길이)
- **auto_continue**: True (필수)
- **max_segments**: 6-10 (많은 세그먼트)

### 💬 채팅용 (적당한 길이 우선)
- **max_tokens**: 100-300 (적당한 길이)
- **auto_continue**: True (선택사항)
- **max_segments**: 2-4 (적은 세그먼트)
- **dynamic_cap**: 500-1000 (최대 제한)

## 🔍 모니터링

### 로그 확인

요약 요청이 감지되면 다음과 같은 로그가 출력됩니다:

```
[INFO] 요약 요청 감지: max_tokens=1200, auto_continue=True, max_segments=8
```

### 성능 모니터링

- **요약**: 토큰 사용량이 높아도 완전성 보장
- **채팅**: 토큰 사용량을 적당히 유지하여 응답 속도 향상

## ⚠️ 주의사항

1. **요약용 토큰이 너무 높으면**: 비용 증가, 응답 속도 저하
2. **채팅용 토큰이 너무 낮으면**: 응답이 너무 짧아짐
3. **auto_continue 세그먼트가 너무 많으면**: 무한 루프 위험

## 🛠️ 환경 변수 설정

`.env` 파일에서 다음과 같이 설정할 수 있습니다:

```bash
# 요약용 토큰 설정
OPENAI_SUMMARY_MAX_TOKENS=1200
OPENAI_SUMMARY_AUTO_CONTINUE=true
OPENAI_SUMMARY_AUTO_CONTINUE_MAX_SEGMENTS=8

# 채팅용 토큰 설정
OPENAI_MAX_TOKENS=150
OPENAI_AUTO_CONTINUE=true
OPENAI_AUTO_CONTINUE_MAX_SEGMENTS=3
OPENAI_DYNAMIC_MAX_TOKENS=true
OPENAI_DYNAMIC_MAX_TOKENS_CAP=800
```
