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
    print(f"  시총≥{config.min_market_cap//1e8:.0f}억, 거래대금≥{config.min_trading_val//1e8:.0f}억, PER {config.per_min}~{config.per_max}, 리밸런싱={config.rebalance_freq}")
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

    # ── 실험 D: 분기별 리밸런싱 + 멀티팩터 ──
    #    비용 절감이 1순위. 복합팩터로 선별된 종목은 3개월 보유해도 문제 적음.
    # ── 실험 F: 20종목 집중 + 멀티팩터 (월별) ──
    #    더 적은 종목으로 우량주에 집중, 비용도 일부 절감.
    # ── 실험 G: 분기별 + 20종목 + 멀티팩터 ──
    #    가장 공격적인 비용 절감 조합.

    experiments = [
        {
            "label": "D: 분기별 + 멀티팩터 (시총500억, 거래대금10억, PER0~12, PBR≤1.5, ROE≥5%)",
            "config": replace(
                base,
                per_min=0.01,
                per_max=12.0,
                rebalance_freq="quarterly",
                use_multi_factor=True,
                pbr_max=1.5,
                roe_min=0.05,
                start_date="2008-01-01",
                end_date="2026-06-26",
            ),
        },
        {
            "label": "F: 20종목 + 멀티팩터 (월별, 시총500억, 거래대금10억, PER0~12, PBR≤1.5, ROE≥5%)",
            "config": replace(
                base,
                per_min=0.01,
                per_max=12.0,
                n_stocks=20,
                use_multi_factor=True,
                pbr_max=1.5,
                roe_min=0.05,
                start_date="2008-01-01",
                end_date="2026-06-26",
            ),
        },
        {
            "label": "G: 분기별 + 20종목 + 멀티팩터 (시총500억, 거래대금10억, PER0~12, PBR≤1.5, ROE≥5%)",
            "config": replace(
                base,
                per_min=0.01,
                per_max=12.0,
                n_stocks=20,
                rebalance_freq="quarterly",
                use_multi_factor=True,
                pbr_max=1.5,
                roe_min=0.05,
                start_date="2008-01-01",
                end_date="2026-06-26",
            ),
        },
    ]

    for exp in experiments:
        run_single(exp["config"], exp["label"], cache_dir=args.cache_dir)

    print("\n\n✅ 모든 실험 완료!")
