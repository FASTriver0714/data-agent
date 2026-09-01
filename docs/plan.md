# 계획서

원본(전체 맥락): 노션 문서 https://app.notion.com/p/3c649d90b35781fb9e8df9cf16b3fef4
이 파일은 로컬 작업용 요약 + 0단계 측정 결과 기록.

## 0단계 — 프로파일링 결과

측정일: 2026-08-26 / 대상: `db/weblog.db` (`events` 테이블)

### 기본 규모

| 항목 | 값 |
|---|---|
| 총 행 수 | 6,610,750 |
| 기간 | 2019-10-01 00:00:00 ~ 2019-10-31 23:59:58 |
| distinct user_id | 91,653 |
| distinct user_session | 935,455 |
| 세션당 평균 이벤트 수 | 7.07 |

### 스키마

```sql
CREATE TABLE events (
  event_time TEXT,
  event_type TEXT,
  product_id INTEGER,
  category_id TEXT,
  category_code TEXT,
  brand TEXT,
  price REAL,
  user_id INTEGER,
  user_session TEXT
);
CREATE INDEX idx_user ON events(user_id);
CREATE INDEX idx_time ON events(event_time);
CREATE INDEX idx_type ON events(event_type);
CREATE INDEX idx_session ON events(user_session);
```

인덱스 없음: `category_code`, `brand` (해당 컬럼으로 GROUP BY/필터 시 풀스캔 가능성 — 쿼리 작성 시 확인 필요).

### 결측률

| 컬럼 | 결측 행 수 | 결측률 |
|---|---|---|
| category_code | 2,152,844 | 32.57% |
| brand | 1,071,029 | 16.20% |
| user_session | 0 | 0% |

결측 기준: `NULL` 또는 빈 문자열(`''`).

경고 임계치는 미정 (CLAUDE.md 미결정 사항). category_code 32.57%는 카테고리 기준 분석 시 응답에 반드시 언급해야 할 수준으로 보이나, 임계치 자체는 사용자 확인 필요.

### user_session 유효성

- NULL/빈값: 0건 (전량 값 존재)
- 세션 하나에 user_id가 2개 이상 섞인 경우: 0건 → 세션-사용자 매핑은 깨끗함
- 세션 내 이벤트 간 시간 역전(음수 gap): 0건 → 세션 내부는 시간순 정렬 상태
- 세션 내부 이벤트 간 gap: 평균 127.1초, 최대 1,593,014초(약 18.4일)
- gap이 30분(1800초) 초과인 경우: 26,192 / 5,675,295 (0.46%)

해석: `user_session` 컬럼은 구조적으로는 깨끗하지만(단일 사용자, 시간 역전 없음), 일부 세션은 30분 이상 공백을 포함하고 있어 통상적인 "세션 타임아웃 30분" 정의와는 어긋난다. 즉 원본 `user_session` 값을 그대로 세션 경계로 쓸지, 시간 간격 기준으로 재정의할지는 CLAUDE.md의 미결정 사항으로 남아 있고 아직 확정하지 않았다.

### 다음

- [ ] 정답셋 20문항 작성
- [ ] 1단계: `metrics.yaml` 지표 정의 (6개)
