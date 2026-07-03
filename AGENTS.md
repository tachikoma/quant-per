# quant-per

KOSPI/KOSDAQ 퀀트 백테스트 엔진 (pykrx 기반).

## 명령어

```bash
uv sync                      # 의존성 설치
cp .env.sample .env          # 설정 생성 후 KRX_ID/PW 입력
uv run python backtest.py    # 백테스트 실행
uv run python experiments.py # 배치 실험 실행
uv run pytest                # 전체 테스트
uv run pytest tests/test_engine.py::TestBasicBacktest -v  # 특정 테스트
uv run ruff check            # 린트
uv run ruff format           # 포매팅
```

### CLI 플래그

```bash
uv run python backtest.py --clear-cache   # 캐시 전체 삭제
uv run python backtest.py --no-cache      # 재다운로드
```

## 아키텍처

```
backtest.py → Config.from_env() → fetch_rebalancing_data() → run_backtest() → benchmark_strategy() → print_report()
```

- flat layout, 모든 .py 파일은 루트
- 설정은 `.env`에서 python-dotenv 로딩
- pykrx로 KRX 실거래 데이터 수집, `.cache/backtest/market_data/{YYYY-MM}.parquet`에 캐싱
- KOSPI 벤치마크는 `.cache/backtest/kospi.parquet` 증분 캐싱
- KOSPI 200일 이동평균 시장 레짐 필터: 종가 < MA200이면 전량 현금화, ≥ MA200이면 정상 리밸런싱 (`_fetch_kospi_for_ma()` → `.cache/backtest/kospi_ma.parquet`)

## 설정 (.env)

- 두 전략은 블록 주석 전환: (1) PBR+멀티팩터 (2) 모멘텀+저변동성
- `BACKTEST_END_DATE` 미설정 시 마지막 영업일 자동 계산
- `FUNDAMENTAL_LAG_MONTHS`로 look-ahead bias 실험 가능 (실제 사용 시 CAGR 붕괴)
- `PER_PCTILE`/`PBR_PCTILE`/`ROE_PCTILE`은 `use_multi_factor=True`일 때만 적용
  - `use_multi_factor=True`: PBR 하위 n% 필터 + PER/ROE/배당 스코어링
  - `use_multi_factor=False`: fundmental 필터 없음 (모멘텀 전용)

## 테스트

- 실제 API 호출 없음, 합성 데이터로 테스트 (`_make_stock_rows` 헬퍼)
- `zero_cost_config` fixture: 비용 0% 시나리오, `use_multi_factor=False`
- 핵심 검증: cash 잔액 보존, 우선주 필터, 리밸런싱 동일가격 매도/매수

## 주의사항

- `.env`의 `KRX_ID`/`KRX_PW` 필수 (pykrx 로그인), `.gitignore`로 보호됨
- `uv run` prefix 필수 (uv 가상환경)
- 우선주 필터: code 끝자리가 `0`이 아니면 `is_preferred=True`
- 거래대금 기반 동적 슬리피지: `주문금액/일거래대금 × 0.5`, cap = `SLIPPAGE`
- `engine.py` 최상단에 `pd.set_option('future.no_silent_downcasting', True)` 적용됨
