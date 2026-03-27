# 정부 지원사업 공고 자동수집 + AI 매칭 파이프라인 계획서

작성일: 2026-03-09

---

## 1. 프로젝트 개요

### 목표
- 국내 주요 정부 기관 포털에서 지원사업 공고를 자동으로 수집
- 기업/사용자 프로필을 기반으로 적합한 공고를 AI가 자동 매칭
- 마감일, 분야, 지원 조건 등을 정형화하여 검색·필터 가능하게 저장

### 핵심 가치
- 매일 수십 개 사이트를 수작업으로 확인하는 비효율 제거
- 조건에 맞는 공고를 놓치지 않도록 자동 알림
- AI 매칭으로 "우리 회사에 맞는 공고"를 빠르게 추려냄

---

## 2. 수집 대상 포털

| 포털명 | URL | 비고 |
|--------|-----|------|
| 기업마당 | bizinfo.go.kr | 중소벤처기업부 산하, API 제공 |
| K-Startup | k-startup.go.kr | 창업진흥원, API 제공 |
| 중소벤처기업부 | mss.go.kr | 크롤링 필요 |
| 소상공인진흥공단 | semas.or.kr | 크롤링 필요 |
| 창업넷 | changupnet.go.kr | 크롤링 필요 |
| 과학기술정보통신부 | msit.go.kr | 크롤링 필요 |
| TIPS | tips.or.kr | 크롤링 필요 |
| 연구개발특구진흥재단 | innopolis.or.kr | 크롤링 필요 |

> 우선순위: 기업마당(API) → K-Startup(API) → 나머지(크롤링)

---

## 3. 시스템 아키텍처

```
┌─────────────────────────────────────────────────────────┐
│                     수집 레이어                          │
│  ┌───────────┐  ┌───────────┐  ┌───────────────────┐   │
│  │  API 수집  │  │  크롤러   │  │  스케줄러(Cron)   │   │
│  │(bizinfo,  │  │(Playwright│  │  매일 06:00 실행  │   │
│  │ k-startup)│  │/requests) │  └───────────────────┘   │
│  └─────┬─────┘  └─────┬─────┘                          │
└────────┼──────────────┼──────────────────────────────── ┘
         ▼              ▼
┌─────────────────────────────────────────────────────────┐
│                   정규화 레이어                          │
│  - 공고 데이터 정형화 (title, deadline, budget, 분야 등) │
│  - 중복 제거 (URL 또는 공고 ID 기준)                    │
│  - 텍스트 전처리 (HTML → 순수 텍스트)                   │
└────────────────────────┬────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────────┐
│                    저장 레이어                           │
│  ┌─────────────────┐    ┌───────────────────────────┐  │
│  │  PostgreSQL      │    │  벡터 DB (pgvector)       │  │
│  │  (공고 원본 저장) │    │  (임베딩 기반 유사도 검색) │  │
│  └─────────────────┘    └───────────────────────────┘  │
└────────────────────────┬────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────────┐
│                   AI 매칭 레이어                         │
│  1. 공고 임베딩 생성 (Claude / text-embedding 모델)     │
│  2. 기업 프로필 임베딩 생성                             │
│  3. 코사인 유사도 계산 → Top-K 후보 선정               │
│  4. Claude로 최종 적합도 판단 + 이유 설명              │
└────────────────────────┬────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────────┐
│                   알림 레이어                            │
│  - 이메일 (SMTP / SendGrid)                             │
│  - Slack Webhook                                        │
│  - (옵션) 웹 대시보드                                   │
└─────────────────────────────────────────────────────────┘
```

---

## 4. 데이터 모델

### 공고 (Announcement)
```sql
CREATE TABLE announcements (
    id              UUID PRIMARY KEY,
    source          VARCHAR(50),       -- 출처 포털
    source_id       VARCHAR(100),      -- 원본 ID (중복 방지)
    title           TEXT NOT NULL,
    description     TEXT,
    category        VARCHAR(100),      -- 분야 (R&D, 창업, 수출, 융자 등)
    target          TEXT,              -- 지원 대상 (업종, 규모)
    budget          BIGINT,            -- 지원 금액 (원)
    deadline        DATE,              -- 공고 마감일
    apply_url       TEXT,              -- 신청 링크
    raw_html        TEXT,              -- 원본 HTML
    embedding       vector(1536),      -- 임베딩 벡터
    collected_at    TIMESTAMPTZ DEFAULT now(),
    is_active       BOOLEAN DEFAULT TRUE
);
```

### 기업 프로필 (CompanyProfile)
```sql
CREATE TABLE company_profiles (
    id              UUID PRIMARY KEY,
    name            VARCHAR(200),
    industry        VARCHAR(100),      -- 업종
    employee_count  INTEGER,
    annual_revenue  BIGINT,
    founded_year    INTEGER,
    region          VARCHAR(50),       -- 소재지 (시도)
    keywords        TEXT[],            -- 관심 키워드
    description     TEXT,              -- 사업 소개
    embedding       vector(1536)
);
```

### 매칭 결과 (MatchResult)
```sql
CREATE TABLE match_results (
    id              UUID PRIMARY KEY,
    company_id      UUID REFERENCES company_profiles(id),
    announcement_id UUID REFERENCES announcements(id),
    similarity      FLOAT,             -- 벡터 유사도 점수
    ai_score        FLOAT,             -- Claude 평가 점수 (0~1)
    ai_reason       TEXT,              -- 매칭 이유 설명
    notified        BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT now()
);
```

---

## 5. AI 매칭 로직

### 단계 1: 임베딩 유사도 필터링
- 기업 프로필 텍스트 → 임베딩 벡터 생성
- pgvector로 코사인 유사도 Top-20 후보 추출
- 빠른 1차 필터링 (비용 절감)

### 단계 2: Claude 정밀 평가
```
System: 당신은 정부 지원사업 전문가입니다.
        아래 기업 정보와 공고를 비교하여 적합도를 0~10점으로 평가하고,
        매칭 이유를 2~3문장으로 설명하세요.

User:   [기업 프로필]
        [공고 전문]
```
- Top-20 후보 중 점수 7점 이상만 최종 매칭으로 저장
- 비용 최적화: 임베딩 1차 필터 후 Claude 호출

### 단계 3: 알림 발송
- 일 1회 (매일 오전 7시) 배치 처리
- 새 공고 + 높은 매칭 점수(7점↑) 조건 충족 시 발송

---

## 6. 기술 스택

| 구분 | 선택 | 이유 |
|------|------|------|
| 언어 | Python 3.12 | 크롤링·AI 생태계 풍부 |
| 크롤링 | Playwright + httpx | JS 렌더링 사이트 대응 |
| 스케줄러 | APScheduler (또는 Cron) | 경량, 설정 간단 |
| DB | PostgreSQL + pgvector | 관계형 + 벡터 통합 |
| ORM | SQLAlchemy 2.0 | async 지원 |
| AI | Claude API (claude-sonnet-4-6) | 매칭 평가·이유 생성 |
| 임베딩 | Claude + text-embedding | 벡터 생성 |
| 알림 | smtplib / Slack Webhook | 설정 간단 |
| 패키지 관리 | uv | 빠른 의존성 관리 |

---

## 7. 프로젝트 디렉토리 구조

```
gov-support-pipeline/
├── PLAN.md
├── pyproject.toml
├── .env.example
├── alembic/                  # DB 마이그레이션
├── src/
│   ├── collectors/           # 수집기
│   │   ├── base.py           # 추상 기반 클래스
│   │   ├── bizinfo.py        # 기업마당 API
│   │   ├── kstartup.py       # K-Startup API
│   │   └── crawler.py        # 범용 크롤러 (Playwright)
│   ├── models/               # SQLAlchemy 모델
│   │   ├── announcement.py
│   │   ├── company.py
│   │   └── match.py
│   ├── pipeline/
│   │   ├── normalizer.py     # 데이터 정규화
│   │   ├── embedder.py       # 임베딩 생성
│   │   └── matcher.py        # AI 매칭 로직
│   ├── notifier/
│   │   ├── email.py
│   │   └── slack.py
│   ├── scheduler.py          # 스케줄러 진입점
│   └── config.py             # 환경변수 설정
└── tests/
    ├── test_collectors.py
    ├── test_matcher.py
    └── test_normalizer.py
```

---

## 8. 개발 단계 (Phase)

### Phase 1: 수집 기반 구축 (1~2주)
- [ ] PostgreSQL + pgvector 환경 설정
- [ ] 기업마당 API 수집기 구현 및 테스트
- [ ] K-Startup API 수집기 구현 및 테스트
- [ ] 데이터 정규화 로직 구현
- [ ] 중복 제거 로직 구현

### Phase 2: AI 매칭 파이프라인 (2~3주)
- [ ] 임베딩 생성 모듈 구현
- [ ] pgvector 유사도 검색 구현
- [ ] Claude 매칭 평가 프롬프트 개발·테스트
- [ ] 기업 프로필 입력 인터페이스 구현

### Phase 3: 알림·자동화 (1주)
- [ ] 이메일 알림 구현
- [ ] Slack Webhook 알림 구현
- [ ] APScheduler 스케줄러 설정
- [ ] 크롤러 추가 (mss.go.kr 등)

### Phase 4: 안정화 (1주)
- [ ] 에러 처리·재시도 로직 강화
- [ ] 로깅 시스템 구축
- [ ] 테스트 커버리지 확보
- [ ] (옵션) 간단한 웹 대시보드

---

## 9. 주요 리스크 및 대응

| 리스크 | 대응 방안 |
|--------|-----------|
| 사이트 구조 변경으로 크롤러 깨짐 | 수집 실패 시 알림, 모듈화로 빠른 수정 |
| Claude API 비용 초과 | 임베딩 1차 필터로 호출 수 최소화 |
| 공고 중복 수집 | source + source_id 복합 유니크 키 |
| robots.txt / 차단 | 요청 간격 조절, API 우선 사용 |
| 마감일 지난 공고 누적 | 매일 is_active 업데이트 배치 실행 |

---

## 10. 성공 지표

- 수집: 일 100건 이상 신규 공고 수집
- 매칭: 기업당 일 평균 5~10건 적합 공고 추천
- 정확도: 사용자 피드백 기준 매칭 만족도 70% 이상
- 안정성: 스케줄러 가동률 99% 이상

---

> 다음 단계: 이 계획서를 검토 후 Phase 1부터 구현 시작
