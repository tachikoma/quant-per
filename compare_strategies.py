"""캐시 데이터 기반 전략 비교 스크립트 (pykrx 호출 없음).

market_data 캐시(2016~2026) + DART 재무제표 캐시로 여러 전략의
백테스트를 실행해 성과를 비교한다. pykrx 다운로드 없이 순수 캐시만 사용.
"""

from dataclasses import replace
from pathlib import Path

import pandas as pd

from config import Config
from engine import run_backtest
from report import benchmark_strategy


def load_market_data() -> pd.DataFrame:
    parts = []
    for f in sorted(Path(".cache/backtest/market_data").glob("*.parquet")):
        parts.append(pd.read_parquet(f))
    return pd.concat(parts, ignore_index=True)


def run(config: Config, label: str):
    print("=" * 70)
    print(f"  {label}")
    print("=" * 70)
    history, metrics = run_backtest(market_data, config)
    history = benchmark_strategy(history, config)
    return history, metrics


if __name__ == "__main__":
    market_data = load_market_data()
    print(
        f"market_data: {market_data['date'].min().date()} ~ {market_data['date'].max().date()}, "
        f"{market_data['code'].nunique()}종목\n"
    )

    base = Config.from_env()
    # 2016-2026 캐시 기간에 맞춤 (DART 2015년보고서는 2016-04-15부터 사용 가능)
    period = dict(start_date="2016-01-01", end_date="2026-12-31")

    # 동결 설정과 일치하도록 전략별로 명시 (또는 .env의 EXCLUDE_NEGATIVE_PER가
    # 모든 전략에 암묵 적용돼 M2/K1/K3 결과가 바뀌는 것을 방지)
    strategies = [
        (
            "K1: 카스넬슨 가치투자",
            replace(
                base,
                use_katsenelson=True,
                use_multi_factor=False,
                use_momentum=False,
                use_low_volatility=False,
                min_roic=0.05,
                max_ev_ebitda=15.0,
                max_turnover=0.5,
                exclude_negative_per=False,
                **period,
            ),
        ),
        (
            "K2: 카스넬슨 + 모멘텀",
            replace(
                base,
                use_katsenelson=True,
                use_multi_factor=False,
                use_momentum=True,
                momentum_window=12,
                use_low_volatility=False,
                min_roic=0.05,
                max_ev_ebitda=15.0,
                max_turnover=0.5,
                exclude_negative_per=False,
                **period,
            ),
        ),
        (
            "K3: 카스넬슨 + 저변동성",
            replace(
                base,
                use_katsenelson=True,
                use_multi_factor=False,
                use_momentum=False,
                use_low_volatility=True,
                min_roic=0.05,
                max_ev_ebitda=15.0,
                max_turnover=0.5,
                exclude_negative_per=False,
                **period,
            ),
        ),
        (
            "M2: 모멘텀 + 저변동성",
            replace(
                base,
                use_katsenelson=False,
                use_multi_factor=False,
                use_momentum=True,
                momentum_window=12,
                use_low_volatility=True,
                max_turnover=0.5,
                exclude_negative_per=False,
                **period,
            ),
        ),
        (
            "PBR: 멀티팩터",
            replace(
                base,
                use_katsenelson=False,
                use_multi_factor=True,
                use_momentum=False,
                use_low_volatility=False,
                max_turnover=0.5,
                exclude_negative_per=True,
                **period,
            ),
        ),
    ]

    results = []
    for label, config in strategies:
        try:
            history, metrics = run(config, label)
            last = history.iloc[-1]
            results.append(
                {
                    "전략": label,
                    "CAGR(%)": metrics["CAGR_PCT"],
                    "MDD(%)": metrics["MAX_DRAWDOWN_PCT"],
                    "누적수익(%)": metrics["TOTAL_RETURN_PCT"],
                    "KOSPI(%)": round(last.get("KOSPI_Return(%)", 0), 2),
                    "Alpha(%)": round(last.get("Alpha(%)", 0), 2),
                    "비용(만원)": round(metrics["TOTAL_COST_IMPACT_KRW"] / 1e4, 1),
                }
            )
        except Exception as e:
            print(f"  ❌ {label} 실패: {e}")

    print("\n" + "=" * 70)
    print("  전략 비교 요약")
    print("=" * 70)
    df = pd.DataFrame(results)
    print(df.to_string(index=False))
