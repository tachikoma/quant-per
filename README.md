# KOSPI/KOSDAQ 퀀트 백테스트 엔진

pykrx 기반 KRX 실거래 데이터 + DART 재무제표로 가치투자/모멘텀 전략을 검증하는 퀀트 백테스터입니다.

**지원 전략:**
| 전략 | 필터 | 스코어링 | Bias |
|------|------|---------|:----:|
| PBR+멀티팩터 | PBR 하위 30% + PBR≥0 | PER + ROE + 배당 순위 합산 | ⚠️ |
| 모멘텀+저변동성 | 시총/거래대금 | 12개월 모멘텀 + 변동성 | ✅ 없음 |
| 카스넬슨 가치투자 | ROIC, D/E, 이자보상, EV/EBITDA | FCF Yield + EV/EBITDA + PER (+선택 3Y CAGR) | ✅ 공시일 lag |

전략은 `.env`에서 블록 주석 전환으로 간편히 스위칭 가능합니다.

## 프로젝트 구조

```
├── backtest.py          # 메인 실행 스크립트
├── config.py            # 한국 휴장일 캘린더 + Config dataclass + .env 로딩
├── engine.py            # 데이터 수집(fetch) + 백테스트 로직(run)
├── dart_data.py         # DART API 연동 + 재무제표 수집/캐싱 + 공시일 lag
├── metrics.py           # 카스넬슨 재무 지표 계산 (ROIC, FCF, EV/EBITDA 등)
├── collect_dart_data.py # DART 재무제표 배치 수집 (일 20,000건 한도 준수)
├── report.py            # KOSPI 벤치마크 비교 + 결과 리포트 출력
├── experiments.py       # 배치 실험 (전략 비교)
├── compare_strategies.py # 캐시 기반 전략 성과 비교 (pykrx 호출 없음)
├── VALIDATION_PLAN.md   # 최종 검증 계획 (Phase 1~7)
├── VALIDATION_REPORT.md # 최종 판정서 (전략 계속/중단 판정)
├── phase1_reproducibility.py  # 재현성 검증 (캐시 전용)
├── phase3_baselines.py  # 기준선·MA200 분해
├── phase4_cost_stress.py # 비용·체결 스트레스
├── phase5_parameter_stability.py # 파라미터 안정성 그리드
├── phase6_oos.py        # OOS 폴드 진단 + 미래 OOS 체크포인트
├── phase7_market_regime.py # 시장조건·레짐/알파 분해 (A2 재검증)
├── pyproject.toml       # 프로젝트 메타데이터 및 의존성
├── .env                 # 전략 파라미터 (KRX_ID, DART_API_KEY 등)
└── .env.sample          # .env 예시 (secret 제외)
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
| `DART_API_KEY` | (카스넬슨 필수) | opendart.fss.go.kr 발급 키 |
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
| `USE_MOMENTUM` | false | 12개월 모멘텀 팩터 사용 |
| `USE_LOW_VOLATILITY` | false | 저변동성 팩터 사용 |
| `EXCLUDE_NEGATIVE_PER` | false | 음수 PER(적자기업) 종목 제외 — **PBR은 true로 재동결 (CAGR +3.55→+5.58, OOS 폴드 4개 전부 양수)** |
| `USE_MARKET_REGIME` | true | KOSPI 200일선 시장 레짐 필터 (false 시 항상 풀투자) |
| `MA_WINDOW` | 200 | 시장 레짐 이동평균 기간 |
| `USE_KATSENELSON` | false | 카스넬슨 가치투자 (DART 재무제표) |
| `MIN_ROIC` | 0.10 | 카스넬슨: ROIC 하한 |
| `MAX_DEBT_EQUITY` | 1.5 | 카스넬슨: 차입금/자본 비율 상한 |
| `MIN_INTEREST_COVERAGE` | 2.0 | 카스넬슨: 이자보상배율 하한 |
| `MAX_EV_EBITDA` | 20.0 | 카스넬슨: EV/EBITDA 상한 (0=미적용) |
| `KATSENELSON_USE_GROWTH` | false | 카스넬슨: 3Y CAGR 성장 스코어 (데이터상 부정적) |
| `KATSENELSON_USE_NCAV` | false | 카스넬슨: NCAV 스코어 (Graham net-net, 성과 저해) |
| `FUNDAMENTAL_LAG_MONTHS` | 0 | 재무데이터 시차 보정 (실험용) |

## 실행

```bash
uv run python backtest.py          # 기본 백테스트
uv run python collect_dart_data.py # DART 재무제표 수집 (카스넬슨 전용)
uv run python experiments.py       # 배치 실험
uv run python compare_strategies.py # 전략별 성과 비교 (캐시 데이터, pykrx 호출 없음)
```

### 최종 검증 (Validation)

`VALIDATION_PLAN.md`의 Phase 1~7 게이트를 통과한 전략만 개선합니다. 전부 캐시 전용 실행:

```bash
uv run python phase1_reproducibility.py          # 재현성 (5개 전략, 2016-2026)
uv run python phase3_baselines.py                # 기준선·MA200 분해
uv run python phase4_cost_stress.py              # 비용·체결 스트레스
uv run python phase5_parameter_stability.py      # 파라미터 안정성 그리드
uv run python phase6_oos.py                      # OOS 폴드 진단
uv run python phase6_oos.py --checkpoint 2026-08-01 2026-12-31  # 미래 OOS (데이터 수집 후)
uv run python phase7_market_regime.py            # 시장조건·레짐/알파 분해
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

3. **DART 재무제표 수집** (`dart_data.py` + `collect_dart_data.py`)
   - `corpCode.xml` → corp_code(8) ↔ ticker(6) 매핑 캐싱
   - `fnlttSinglAcntAll.json` → 연간 사업보고서 BS/IS/CF 계정 추출
   - **공시일 기반 look-ahead bias 방지**: 사업보고서(12월 결산)는 다음해 4월 15일부터 사용
   - 일 20,000건 / 분당 1,000회 API 한도 자동 준수

4. **백테스트** (`engine.py:run_backtest`)
   - `start_date`~`end_date` 기간 필터 (기간 밖 데이터 제외)
   - 월간/분기간 리밸런싱: 초일(첫 거래일) 매도 + 매수
   - 보통주만 (우선주 제외)
   - 시총/거래대금 필터링 → 전략별 필터/스코어링으로 종목 선정
   - **거래대금 기반 슬리피지**: 주문금액/일거래대금 × 0.5 (cap=SLIPPAGE)
   - **부분 리밸런싱**: `max_turnover` 이하로만 포트폴리오 교체 (비용 절감)
   - **시장 레짐**: KOSPI 종가 < MA200(=`MA_WINDOW`)이면 전량 현금화, ≥ 이면 정상 리밸런싱 (`USE_MARKET_REGIME=false` 시 비활성)
   - **음수 PER 제외**: `EXCLUDE_NEGATIVE_PER=true` 시 적자기업 제외 (기본 false — 이전 동작 유지)
   - 상장폐지 시 보수적 가정 (원금 10% 회수)

### 스코어링 방식

| 모드 | 유니버스 필터 | 스코어 구성 |
|------|-------------|------------|
| PBR+멀티팩터 | PBR 하위 30% + PBR≥0 + **PER>0(적자 제외)**, 시총 200억~1조, 거래대금≥10억 | `rank(PER) + rank(ROE↓) + rank(DIV↓)` |
| 모멘텀 단독 | 시총/거래대금/우선주 제외만 | `rank(모멘텀↓)` |
| 모멘텀+저변동성 | 시총/거래대금/우선주 제외만 | `rank(모멘텀↓) + rank(변동성)` |
| 카스넬슨 가치투자 | ROIC≥MIN, D/E≤MAX, FCF Yield≥MIN, (이자보상≥MIN, EV/EBITDA≤MAX) | `rank(EBITDA↓) + rank(PER↓) + rank(FCF Yield↑)` + 선택 `rank(3Y CAGR↑)` |

5. **KOSPI 벤치마크** (`report.py:benchmark_strategy`)
   - 동일 리밸런싱 기준일의 KOSPI 지수 대비 Alpha 계산
   - KOSPI 캐시: 증분 단일 parquet 파일

## 백테스트 결과

> 아래는 **최종 검증 기준선(2016-01-01 ~ 2026-06-30, 캐시 전용)** 결과입니다.
> PBR 행은 2026-08-11 재동결(음수 PER 필터 반영) 결과입니다.
> 이전 문서 수치(M2 +8.02%, PBR +0.84%)는 다른 기간·설정(2008-2026, 월별/하위30%) 결과로
> 현재 캐시로 재현 불가 — 검증 과정에서 스테일 문서로 확정.
> 전체 판정 근거는 `VALIDATION_REPORT.md` 참조.

### 전체 기간 성과 (Phase 1~3, 캐시 전용)

| 전략 | CAGR | 누적 | MDD | Sharpe |
|------|------|------|-----|--------|
| 동일유니버스 동일가중 (기준선) | -7.79% | -57.3% | -66.3% | -0.27 |
| M2: 모멘텀+저변동성 | -0.97% | -9.7% | -32.6% | 0.05 |
| K1: 카스넬슨 가치투자 | +0.29% | +3.1% | -30.7% | 0.11 |
| K3: 카스넬슨+저변동성 | +1.11% | +12.3% | -25.1% | 0.16 |
| K2: 카스넬슨+모멘텀 | +1.55% | +17.5% | -33.2% | 0.18 |
| **PBR: 멀티팩터 (재동결)** | **+5.58%** | **+76.7%** | -33.4% | **0.41** |

KOSPI 동기간 **+341.77%** 대비 모든 전략이 절대 열위. 유니버스 동일가중이 -57%로
심각한 음수라 복합 전략은 단순 보유보다 우월하나, KOSPI(대형주 중심) 대비 크게 언더퍼폼.
PBR의 절대 열위는 종목선택 알파 부재가 아니라 **유니버스(중소형·KOSDAQ 편중) 구성** 때문
(2026-08-11 A2 재검증: 종목선택알파는 4폴드 전부 양수, 평균 +57pp).

### 최종 판정 요약 (VALIDATION_REPORT.md)

- **개발 중단**: M2/K1/K2/K3 — OOS 폴드(비겹침 2~3년×4)에서 4개 중 3개 음수, 알파의 시대의존.
  M2는 MA200 레짐 필터 의존, K1은 파라미터 plateau 부재.
- **조건부 계속 → 2026-08-11 재검증으로 OOS 충족**: PBR — `EXCLUDE_NEGATIVE_PER=true`
  반영(재동결)으로 CAGR +3.55→+5.58% 및 **OOS 폴드 4개 전부 양수** 전환.
  MA200 독립적인 종목선택 알파 확인. 잔여 게이트: 미래 OOS 체크포인트 확정
  (데이터 축적 후 재실행, `results/phase6_oos_checkpoint.csv` 현재 예비 +5.51%).
