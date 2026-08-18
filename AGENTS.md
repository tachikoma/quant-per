# quant-per

KOSPI/KOSDAQ 퀀트 백테스트 엔진 (pykrx + DART 기반).

## 명령어

```bash
uv sync                      # 의존성 설치
cp .env.sample .env          # 설정 생성 후 KRX_ID/PW/DART_API_KEY 입력
uv run python backtest.py    # 백테스트 실행
uv run python collect_dart_data.py  # DART 재무제표 수집 (카스넬슨 전용)
uv run python experiments.py # 배치 실험 실행
uv run python compare_strategies.py  # 전략별 성과 비교 (캐시 기반, pykrx 호출 없음)
uv run pytest                # 전체 테스트
uv run pytest tests/test_engine.py::TestBasicBacktest -v  # 특정 테스트
uv run ruff check            # 린트
uv run ruff format           # 포매팅
```

### 최종 검증 (Validation, 캐시 전용)

전략을 개선하기 전에 `VALIDATION_PLAN.md`의 Phase 1~7 게이트를 순서대로 통과해야 한다.
**판정 결과(2026-08-10)**: M2/K1/K2/K3 개발 중단, PBR만 조건부 계속.
**재검증(2026-08-11)**: PBR `EXCLUDE_NEGATIVE_PER=true` 재동결로 OOS 폴드 4개 전부
양수 + 폴드 간 종목선택 알파 일관 확인 — 절대 열위 원인은 유니버스(중소형 편중).
**Phase 2 완료(2026-08-18)**: 결측 종목 평가(4% 미만, PBR 영향 없음), 체결 시점 비교(
v2 데이터 필요로 별도 작업 판정), report.py KOSPI 벤치마크 버그 수정.
미래 OOS 체크포인트 재실행: PBR +2.62% (2026-08~18, 2.5개월).
전체 근거는 `VALIDATION_REPORT.md`.

```bash
uv run python phase1_reproducibility.py          # 재현성 (5개 전략, 2016-01~2026-06)
uv run python phase3_baselines.py                # 기준선·MA200 분해 (동일유니버스 vs 전략)
uv run python phase4_cost_stress.py              # 비용·체결 스트레스
uv run python phase5_parameter_stability.py      # 파라미터 안정성 그리드
uv run python phase6_oos.py                      # OOS 폴드 진단
uv run python phase6_oos.py --checkpoint START END  # 미래 OOS (데이터 수집 후, 재튜닝 금지)
uv run python phase7_market_regime.py            # 시장조건·레짐/알파 분해
```

- 카스넬슨 계열(K1/K2/K3) 실행은 ~3분/전략, 전체 phase 재실행은 ~25분 소요
- 백그라운드 실행 권장: `nohup uv run python phaseN_*.py > results/phaseN_run.txt 2>&1 &`

### CLI 플래그

```bash
uv run python backtest.py --clear-cache   # 캐시 전체 삭제
uv run python backtest.py --no-cache      # 재다운로드
uv run python collect_dart_data.py --years 2019 2020 2021 2022 2023  # 연도 지정
uv run python collect_dart_data.py --limit 100   # 테스트용 소량 수집
uv run python collect_dart_data.py --force-refresh  # 캐시 무시 재수집
```

## 아키텍처

```
backtest.py → Config.from_env() → fetch_rebalancing_data() → run_backtest() → benchmark_strategy() → print_report()
                                        ↓ use_katsenelson=True 시
                                   merge_dart_financials() (dart_data.py)
```

- flat layout, 모든 .py 파일은 루트
- `run_backtest`는 `start_date`~`end_date`로 기간 필터링 후 백테스트 (기간 밖 데이터 제외, 빈 기간 시 ValueError)
- 검증 워크플로우: `phase1~6_*.py` → `results/*.csv` (캐시 전용, pykrx 호출 없음)
- 설정은 `.env`에서 python-dotenv 로딩
- pykrx로 KRX 실거래 데이터 수집, `.cache/backtest/market_data/{YYYY-MM}.parquet`에 캐싱
- KOSPI 벤치마크는 `.cache/backtest/kospi.parquet` 증분 캐싱
- KOSPI 이동평균 시장 레짐 필터 (`MA_WINDOW` 기본 200): 종가 < MA이면 전량 현금화, ≥ 이면 정상 리밸런싱 (`_fetch_kospi_for_ma()` → `.cache/backtest/kospi_ma.parquet`, `USE_MARKET_REGIME=false` 시 비활성)
- DART 재무제표: corp_code(8)↔ticker(6) 매핑 `.cache/backtest/dart_corp_codes.parquet`, 연간 재무제표 `.cache/backtest/dart_data/{corp_code}.parquet`

## DART (카스넬슨 전략)

- `dart_data.py`: DART OpenAPI 연동 (corpCode.xml 매핑, fnlttSinglAcntAll 재무제표)
- `metrics.py`: 재무 지표 계산 (ROIC, FCF, FCF Yield, EV/EBITDA, NCAV, D/E, 이자보상, ROE, 이익안정성)
- `collect_dart_data.py`: 배치 수집, 일 20,000건 / 분당 1,000회 한도 자동 준수 (일 19,500건 후 KST 자정 대기)
- **공시일 look-ahead bias 방지**: 사업보고서(12월 결산)는 다음해 4/15부터 사용 (`available_from()`)
- 카스넬슨 품질 필터: ROIC≥MIN_ROIC, D/E≤MAX_DEBT_EQUITY, FCF Yield≥MIN, (이자보상≥MIN, EV/EBITDA≤MAX — 데이터 있을 때만)
- 카스넬슨 스코어 (Q-G-V 3요소): `rank(EV/EBITDA↓) + rank(PER↓) + rank(FCF Yield↑)` + 선택적으로 `rank(3Y CAGR↑)` (기본 off)
- **NCAV는 기본 off** (Graham net-net, 카스넬슨 프레임워크와 불일치, 백테스트서 성과 저해)
- 재무제표가 없는 종목은 품질 필터에서 탈락 (DART 미수집 종목 = 자동 제외)
- merge_dart_financials: code 그룹별 searchsorted로 look-ahead bias 없는 매칭 (merge_asof 전역정렬 버그 주의)

## 설정 (.env)

- 세 전략은 블록 주석 전환: (1) PBR+멀티팩터 (2) 모멘텀+저변동성 (3) 카스넬슨 가치투자
- `DART_API_KEY` 필수 (카스넬슨 전용), `opendart.fss.go.kr` 발급
- `BACKTEST_END_DATE` 미설정 시 마지막 영업일 자동 계산
- `FUNDAMENTAL_LAG_MONTHS`로 look-ahead bias 실험 가능 (실제 사용 시 CAGR 붕괴)
- `EXCLUDE_NEGATIVE_PER=true`: 음수 PER(적자기업) 종목 제외. PBR은 true로 재동결 (CAGR +3.55→+5.58, OOS 폴드 4개 전부 양수). 모멘텀/카스넬슨은 false 유지(동결 보존)
- `USE_MARKET_REGIME`/`MA_WINDOW`: KOSPI 이동평균 레짐 필터 on-off / 기간(기본 200). M2는 MA200 의존(off 시 -8.12%)이나 PBR은 off에서 수익 상승(레짐 독립, MDD만 악화)
- `PER_PCTILE`/`PBR_PCTILE`/`ROE_PCTILE`은 `use_multi_factor=True`일 때만 적용
  - `use_multi_factor=True`: PBR 하위 n% 필터 + PER/ROE/배당 스코어링
  - `use_multi_factor=False`: fundmental 필터 없음 (모멘텀 전용)
- 카스넬슨 파라미터: `USE_KATSENELSON`, `MIN_ROIC`, `MAX_DEBT_EQUITY`, `MIN_INTEREST_COVERAGE`, `MAX_EV_EBITDA`, `KATSENELSON_USE_GROWTH`, `KATSENELSON_USE_NCAV`
- 카스넬슨 실전 결과(2016~2026): NCAV/성장 off 기본. K3(저변동성 결합) MDD -25%로 가장 견고

## 테스트

- 180개 테스트 통과 (합성 데이터, API 호출 없음)
- `_make_stock_rows` 헬퍼로 합성 데이터 생성
- `zero_cost_config` fixture: 비용 0% 시나리오, `use_multi_factor=False`
- 핵심 검증: cash 잔액 보존, 우선주 필터, 리밸런싱 동일가격 매도/매수
- `tests/test_dart.py`: 공시일 look-ahead bias 방지 검증 (가짜 캐시로 merge_asof 동작 확인)
- `tests/test_metrics.py`: 재무 지표 계산 + NaN/None 경계값
- `tests/test_report.py`: `align_to_dates` 벤치마크 정렬 (미래값 미사용)
- engine 무결성: 기간 필터(start/end), 레짐 NaN, 레짐 off, 음수 PER, 주기 검증, DART 전멸 경고

## 주의사항

- `.env`의 `KRX_ID`/`KRX_PW` 필수 (pykrx 로그인), `.gitignore`로 보호됨
- `DART_API_KEY`도 `.gitignore`로 보호됨 (`.env`에만 보관)
- `uv run` prefix 필수 (uv 가상환경)
- 우선주 필터: code 끝자리가 `0`이 아니면 `is_preferred=True`
- 거래대금 기반 동적 슬리피지: `주문금액/일거래대금 × 0.5`, cap = `SLIPPAGE`
- `engine.py` 최상단에 `pd.set_option('future.no_silent_downcasting', True)` 적용됨
- DART 데이터는 2015년 이후만 제공 (fnlttSinglAcntAll 한계)
- survivorship bias: 캐시된 market_data에 상장폐지 종목 포함 (pykrx가 과거 기준 상장 종목 반환), 다만 DART 재무제표 없는 상장폐지 종목은 카스넬슨 필터에서 탈락
- pykrx 1.2.8+: `get_index_ohlcv_by_date` 컬럼명이 한글로 변경됨 (날짜, 시가, 고가, 저가, 종가, 거래량, 거래대금, 상장시가총액). report.py/engine.py는 이미 한글 컬럼명 사용
