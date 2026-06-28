import sys
import argparse
from dataclasses import replace
from config import Config
from engine import fetch_rebalancing_data, run_backtest, clear_cache
from report import benchmark_strategy, print_report


def run_single(config: Config, label: str, cache_dir=None):
    print("\n" + "=" * 70)
    print(f"  실험: {label}")
    print("=" * 70)
    print(f"  시총≥{config.min_market_cap//1e8:.0f}억, 거래대금≥{config.min_trading_val//1e8:.0f}억, PER {config.per_min}~{config.per_max}, {config.rebalance_freq}, {config.n_stocks}종목")
    if config.use_multi_factor:
        print(f"  멀티팩터: PBR≤{config.pbr_max}, ROE≥{config.roe_min}, 배당가점 | max_turnover={config.max_turnover}")
    print()

    data = fetch_rebalancing_data(
        config.start_date, config.end_date,
        cache_dir=cache_dir, force_refresh=False
    )

    history, metrics = run_backtest(data, config)

    history = benchmark_strategy(
        history, config,
        cache_dir=cache_dir, force_refresh=False
    )

    print_report(history, metrics, config)
    return history, metrics


def parse_args():
    parser = argparse.ArgumentParser(description="백테스트 실험 배치 실행")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--no-cache", action="store_true",
                        help="캐시 사용하지 않고 전체 데이터 다시 다운로드")
    parser.add_argument("--clear-cache", action="store_true",
                        help="캐시 전체 삭제")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.clear_cache:
        clear_cache(args.cache_dir)
        sys.exit(0)

    base = Config.from_env()

    # ── H: slippage 0.2% → 0.1% (멀티팩터, 월별, 30종목) ──
    # ── I: max_turnover=0.5 (50%만 교체, 멀티팩터, 월별, 30종목) ──
    # ── J: sell_cost 0.23% → 0.015% (멀티팩터, 월별, 30종목) ──
    #
    # ★ 최종 최적: PER 0~4 + 멀티팩터 + max_turnover=0.5 (CAGR 9.83%, KOSPI Alpha +112%)
    #   .env 에 MAX_TURNOVER=0.5 설정 후 uv run python backtest.py 로 실행

    mf_kwargs = dict(
        per_min=0.01, per_max=12.0,
        use_multi_factor=True,
        pbr_max=1.5, roe_min=0.05,
        start_date="2008-01-01", end_date="2026-06-26",
    )

    experiments = [
        {
            "label": "H: slippage=0.1% (멀티팩터, 월별, 30종목)",
            "config": replace(base, slippage=0.001, **mf_kwargs),
        },
        {
            "label": "I: max_turnover=0.5 (멀티팩터, 월별, 30종목)",
            "config": replace(base, max_turnover=0.5, **mf_kwargs),
        },
        {
            "label": "J: sell_cost=0.015% (멀티팩터, 월별, 30종목)",
            "config": replace(base, sell_cost=0.00015, **mf_kwargs),
        },
    ]

    for exp in experiments:
        run_single(exp["config"], exp["label"], cache_dir=args.cache_dir)

    print("\n\n✅ 모든 실험 완료!")
