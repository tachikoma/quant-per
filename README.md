# KOSPI/KOSDAQ 퀀트 백테스트 엔진

pykrx 기반 KRX 데이터로 가치투자/모멘텀 전략을 검증하는 퀀트 백테스터입니다.

**지원 전략:**
| 전략 | 팩터 | Bias | 추천 자본 |
|------|------|:----:|:--------:|
| PER+멀티팩터 | PER + PBR + ROE + 배당 | ⚠️ | 100만↑ |
| 모멘텀+저변동성 | 12개월 모멘텀 + 변동성 | ✅ 없음 | 1,000만↑ |

두 전략은 `.env`에서 블록 주석 전환으로 간편히 스위칭 가능합니다.

## 프로젝트 구조

```
├── backtest.py      # 메인 실행 스크립트
├── config.py        # 한국 휴장일 캘린더 + Config dataclass + .env 로딩
├── engine.py        # 데이터 수집(fetch) + 백테스트 로직(run)
├── report.py        # KOSPI 벤치마크 비교 + 결과 리포트 출력
├── pyproject.toml   # 프로젝트 메타데이터 및 의존성
├── .env             # 전략 파라미터 (KRX_ID, PER 범위, 자본금 등)
└── .env.sample      # .env 예시 (secret 제외)
```

## 설치

```bash
uv sync
```

## 설정

`.env` 파일로 모든 전략 파라미터를 제어합니다. `.env.sample`을 복사해 생성하세요:

```bash
cp .env.sample .env
```

`.env.sample`은 git에 추적되나 `.env`는 `.gitignore`에 등록되어 로컬에만 보관됩니다.

`.env`는 **[A] 공통 설정** + **[B] 전략 선택** 블록으로 구성되어 있습니다. 전략 간 전환은 해당 블록의 주석을 바꾸면 됩니다.

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `KRX_ID` / `KRX_PW` | (필수) | pykrx KRX 로그인 |
| `BACKTEST_START_DATE` | 2008-01-01 | 백테스트 시작일 |
| `BACKTEST_END_DATE` | (마지막 영업일) | 백테스트 종료일 (미설정 시 자동) |
| `INITIAL_CAPITAL` | 100000000 | 초기 투자금 (원) |
| `N_STOCKS` | 30 | 포트폴리오 종목 수 |
| `PER_MIN` / `PER_MAX` | 0.01 / 4.00 | PER 필터 (전략1: 0~4, 전략2: 0~99) |
| `MIN_MARKET_CAP` | 500억 | 최소 시가총액 |
| `MIN_TRADING_VAL` | 10억 | 최소 일 거래대금 |
| `BUY_COST` | 0.015% | 매수 수수료 |
| `SELL_COST` | 0.23% | 매도 수수료 + 증권거래세 |
| `SLIPPAGE` | 0.2% | 슬리피지 (거래대금 기반 동적 적용) |
| `REBALANCE_FREQ` | monthly | 리밸런싱 주기 (monthly / quarterly) |
| `MAX_TURNOVER` | 1.0 | 최대 교체율 (0.5=50%만 교체) |
| `USE_MULTI_FACTOR` | true | 멀티팩터 스코어링 사용 여부 |
| `PBR_MAX` | 1.5 | PBR 상한 (멀티팩터) |
| `ROE_MIN` | 5% | ROE 하한 (멀티팩터) |
| `USE_MOMENTUM` | false | 12개월 모멘텀 팩터 사용 |
| `MOMENTUM_WINDOW` | 12 | 모멘텀 측정 기간 (개월) |
| `USE_LOW_VOLATILITY` | false | 저변동성 팩터 사용 |
| `FUNDAMENTAL_LAG_MONTHS` | 0 | 재무데이터 시차 보정 (실험용) |

## 실행

```bash
uv run python backtest.py
```

## 엔진 동작 방식

1. **한국 휴장일 캘린더** (`config.py:get_korean_business_days`)
   - 고정 공휴일 9종 (신정, 삼일절, 근로자의날, 어린이날, 현충일, 광복절, 개천절, 한글날, 크리스마스)
   - 음력 공휴일 (설날, 추석, 석가탄신일) — 2018~2026년 포함
   - 대체공휴일 자동 적용
   - `CustomBusinessDay(holidays=...)` 기반

2. **데이터 수집** (`engine.py:fetch_rebalancing_data`)
   - 매월 첫/마지막 거래일만 pinpoint 수집 (API 호출 최소화)
   - KOSPI + KOSDAQ 전 종목
   - pykrx `get_market_cap` + `get_market_fundamental` (PER, PBR, EPS, BPS, DIV)
   - `lag_months` 설정 시 시작일을 앞당겨 과거 펀더멘털 데이터 확보

3. **백테스트** (`engine.py:run_backtest`)
   - 월간/분기간 리밸런싱: 초일(첫 거래일) 매도 + 매수
   - 보통주만 (우선주 제외)
   - 시총/거래대금 필터링 후 **동적 스코어링**으로 종목 선정
   - **거래대금 기반 슬리피지**: 주문금액/일거래대금 × 0.5 (cap=SLIPPAGE)
   - **부분 리밸런싱**: `max_turnover` 이하로만 포트폴리오 교체 (비용 절감)
   - 상장폐지 시 보수적 가정 (원금 10% 회수)

### 스코어링 방식

| 모드 | 스코어 구성 | 비고 |
|------|------------|------|
| PER+멀티팩터 | `rank(PER) + rank(PBR) + rank(ROE↓) + rank(DIV↓)` | PBR≤1.5, ROE≥5% 필터 선적용 |
| 모멘텀 단독 | `rank(모멘텀↓)` | 12개월 수익률 (bias-free) |
| 모멘텀+저변동성 | `rank(모멘텀↓) + rank(변동성)` | 저변동성 팩터로 MDD 방어 |
| 모멘텀+Quality | `rank(모멘텀↓) + rank(PBR) + rank(ROE↓) + rank(DIV↓)` | +2개월 lag |

4. **KOSPI 벤치마크** (`report.py:benchmark_strategy`)
   - 동일 리밸런싱 기준일의 KOSPI 지수 대비 Alpha 계산
   - KOSPI 캐시: 증분 단일 parquet 파일

## 백테스트 결과 (2008-01 ~ 2026-06)

### 전략 1: PER + 멀티팩터
설정: PER 0~4, PBR≤1.5, ROE≥5%, 월별, 30종목, max_turnover=0.5

| 지표 | 전략 | KOSPI |
|------|------|-------|
| 누적 수익률 | **+488.31%** | +353.81% |
| CAGR | **10.06%** | ~8.3% |
| MDD | -46.71% | - |
| 총 거래비용 | 185,750,158 원 | - |

> ⚠️ 4개월 재무데이터 lag 보정 시 CAGR -8.87%로 붕괴 → look-ahead bias 상당함

### 전략 2: 모멘텀 + 저변동성
설정: PER 0~99, 12개월 모멘텀, 저변동성, 월별, 30종목, max_turnover=0.5

| 지표 | 전략 | KOSPI |
|------|------|-------|
| 누적 수익률 | **+316.34%** | +353.81% |
| CAGR | **8.02%** | ~8.3% |
| MDD | -47.77% | - |
| 총 거래비용 | 87,634,318 원 | - |

> ✅ 가격 데이터만 사용 — look-ahead bias 0. 항상 30종목 풀채움.

### 권장 자본별 전략

| 자본 | 추천 전략 | 예상 CAGR | 비고 |
|:----:|:---------:|:---------:|------|
| 100만원 | PER+멀티팩터 | ~10% | 저가주 위주 |
| 1,000만원 | 모멘텀+저변동성 | ~8% | 30종목 분산 가능 |
| 1억↑ | 모멘텀+저변동성 | ~8% | bias-free 안정적 |
