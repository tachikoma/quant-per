# KOSPI/KOSDAQ 퀀트 백테스트 엔진

pykrx 기반 KRX 데이터로 가치투자/모멘텀 전략을 검증하는 퀀트 백테스터입니다.

**지원 전략:**
| 전략 | 필터 | 스코어링 | Bias |
|------|------|---------|:----:|
| PBR+멀티팩터 | PBR 하위 30% + PBR≥0 | PER + ROE + 배당 순위 합산 | ⚠️ |
| 모멘텀+저변동성 | 시총/거래대금 | 12개월 모멘텀 + 변동성 | ✅ 없음 |

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
| `PBR_PCTILE` | 0.3 | PBR 하위 백분위 필터 (0.3 = 하위 30%) |
| `MIN_MARKET_CAP` | 200억 | 최소 시가총액 |
| `MAX_MARKET_CAP` | 0 | 최대 시가총액 (0=상한 없음) |
| `MIN_TRADING_VAL` | 10억 | 최소 일 거래대금 |
| `BUY_COST` | 0.015% | 매수 수수료 |
| `SELL_COST` | 0.23% | 매도 수수료 + 증권거래세 |
| `SLIPPAGE` | 0.2% | 슬리피지 (거래대금 기반 동적 적용) |
| `REBALANCE_FREQ` | monthly | 리밸런싱 주기 (monthly / quarterly) |
| `MAX_TURNOVER` | 1.0 | 최대 교체율 (0.5=50%만 교체) |
| `USE_MULTI_FACTOR` | true | 멀티팩터 스코어링 (PER/ROE/배당) 사용 |
| `PER_PCTILE` | 0.2 | PER 하위 백분위 (MF 스코어링용) |
| `ROE_PCTILE` | 0.5 | ROE 상위 백분위 (MF 스코어링용) |
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
   - 시총/거래대금 필터링 → **PBR 백분위 필터** → **멀티팩터 스코어링**으로 종목 선정
   - **거래대금 기반 슬리피지**: 주문금액/일거래대금 × 0.5 (cap=SLIPPAGE)
   - **부분 리밸런싱**: `max_turnover` 이하로만 포트폴리오 교체 (비용 절감)
   - 상장폐지 시 보수적 가정 (원금 10% 회수)

### 스코어링 방식

| 모드 | 유니버스 필터 | 스코어 구성 |
|------|-------------|------------|
| PBR+멀티팩터 | PBR 하위 30% + PBR≥0, 시총 200억~1조, 거래대금≥10억 | `rank(PER) + rank(ROE↓) + rank(DIV↓)` |
| 모멘텀 단독 | 시총/거래대금/우선주 제외만 | `rank(모멘텀↓)` |
| 모멘텀+저변동성 | 시총/거래대금/우선주 제외만 | `rank(모멘텀↓) + rank(변동성)` |
| 모멘텀+Quality | 시총/거래대금/우선주 제외만 | `rank(모멘텀↓) + rank(ROE↓) + rank(DIV↓)` |

4. **KOSPI 벤치마크** (`report.py:benchmark_strategy`)
   - 동일 리밸런싱 기준일의 KOSPI 지수 대비 Alpha 계산
   - KOSPI 캐시: 증분 단일 parquet 파일

## 백테스트 결과

### 전략 1: PBR + 멀티팩터
설정: PBR 하위 30%, PER+ROE+배당 스코어링, 시총 200억~1조, 월별, 30종목

| 지표 | 전략 | KOSPI |
|------|------|-------|
| 누적 수익률 (10년) | **+0.84%** | +341.77% |
| CAGR | **0.08%** | ~16% |
| MDD | -44.12% | - |
| 종목 수 | 평균 29.8 | 30 목표 |

> 강세장에서 저PBR 전략의 한계: KOSPI 대비 크게 언더퍼폼.
> 가격 기반 팩터(모멘텀)와 결합 시 보완 가능.

### 전략 2: 모멘텀 + 저변동성

| 지표 | 전략 | KOSPI |
|------|------|-------|
| 누적 수익률 | **+316.34%** (2008-2026) | +353.81% |
| CAGR | **~8.02%** | ~8.3% |
| MDD | -47.77% | - |

> ✅ 가격 데이터만 사용 — look-ahead bias 0. 항상 30종목 풀채움.

### 권장 자본별 전략

| 자본 | 추천 전략 | 비고 |
|:----:|:---------:|------|
| 1,000만원↑ | 모멘텀+저변동성 | bias-free, 30종목 분산 가능 |
| 소액 | PBR+멀티팩터 | 저가주 위주 |
