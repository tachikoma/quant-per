# KOSPI/KOSDAQ 멀티팩터 가치투자 백테스트 엔진

pykrx 기반 KRX 실제 데이터로 멀티팩터 가치투자 전략을 검증하는 퀀트 백테스터입니다.

**핵심 전략:** 저PER + 저PBR + 고ROE + 고배당 멀티팩터 스코어링 + 부분 리밸런싱

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

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `KRX_ID` / `KRX_PW` | (필수) | pykrx KRX 로그인 |
| `BACKTEST_START_DATE` | 2008-01-01 | 백테스트 시작일 |
| `BACKTEST_END_DATE` | (마지막 영업일) | 백테스트 종료일 (미설정 시 자동) |
| `INITIAL_CAPITAL` | 100000000 | 초기 투자금 (원) |
| `N_STOCKS` | 30 | 포트폴리오 종목 수 |
| `PER_MIN` / `PER_MAX` | 0.01 / 4.00 | PER 필터 (0~4 딥밸류 권장) |
| `MIN_MARKET_CAP` | 50000000000 (500억) | 최소 시가총액 (300억 이상 권장) |
| `MIN_TRADING_VAL` | 1000000000 (10억) | 최소 일 거래대금 |
| `BUY_COST` | 0.00015 (0.015%) | 매수 수수료 |
| `SELL_COST` | 0.0023 (0.23%) | 매도 수수료 + 증권거래세 |
| `SLIPPAGE` | 0.002 (0.2%) | 슬리피지 |
| `USE_MULTI_FACTOR` | true | 멀티팩터 스코어링 사용 여부 |
| `PBR_MAX` | 1.5 | PBR 상한 필터 (멀티팩터) |
| `ROE_MIN` | 0.05 (5%) | ROE 하한 필터 (멀티팩터) |
| `REBALANCE_FREQ` | monthly | 리밸런싱 주기 (monthly / quarterly) |
| `MAX_TURNOVER` | 1.0 | 최대 교체율 (0.0~1.0, 0.5=50%만 교체) |

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

3. **백테스트** (`engine.py:run_backtest`)
   - 월간/분기간 리밸런싱: 초일(첫 거래일) 매도 + 매수
   - 보통주만 (우선주 제외)
   - 시총/거래대금/PER/PBR/ROE 필터링 후 멀티팩터 스코어링
   - **멀티팩터 스코어 = rank(PER) + rank(PBR) + rank(ROE 역순) + rank(DIV 역순)**
   - **부분 리밸런싱**: `max_turnover` 이하로만 포트폴리오 교체 (비용 절감)
   - 상장폐지 시 보수적 가정 (원금 10% 회수)

4. **KOSPI 벤치마크** (`report.py:benchmark_strategy`)
   - 동일 리밸런싱 기준일의 KOSPI 지수 대비 Alpha 계산
   - KOSPI 캐시: 증분 단일 parquet 파일

## 백테스트 결과 (2008-01 ~ 2026-06, 최적 전략)

**설정:** PER 0.01~4.00, 멀티팩터, PBR≤1.5, ROE≥5%, 월별, 30종목, max_turnover=0.5

| 지표 | 전략 | KOSPI |
|------|------|-------|
| 누적 수익률 | **+466.01%** | +353.81% |
| CAGR | **9.83%** | ~8.3% |
| MDD | -47.31% | - |
| Alpha | **+112.20%** | - |
| 총 거래비용 | 194,553,303 원 | 0 |
