import sys
import argparse
from config import Config
from engine import fetch_rebalancing_data, run_backtest, clear_cache
from report import benchmark_strategy, print_report


def parse_args():
    parser = argparse.ArgumentParser(description="KOSPI/KOSDAQ 퀀트 백테스트 엔진")
    parser.add_argument(
        "--cache-dir", default=None, help="캐시 디렉토리 경로 (기본: .cache/backtest)"
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="캐시 사용하지 않고 전체 데이터 다시 다운로드",
    )
    parser.add_argument("--clear-cache", action="store_true", help="캐시 전체 삭제")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    config = Config.from_env()

    if args.clear_cache:
        clear_cache(args.cache_dir)
        sys.exit(0)

    print("=" * 60)
    if config.use_katsenelson:
        print("  KOSPI/KOSDAQ 카스넬슨 가치투자 백테스트 (DART 재무제표)")
    else:
        print("  KOSPI/KOSDAQ 실거래 데이터 기반 가치투자 백테스트 엔진")
    print("=" * 60)

    data = fetch_rebalancing_data(
        config.start_date,
        config.end_date,
        cache_dir=args.cache_dir,
        force_refresh=args.no_cache,
        lag_months=config.fundamental_lag_months,
        use_market_data_v2=config.execution_mode == "next_close",
        execution_mode=config.execution_mode,
    )
    print("\n데이터 수집 완료. 백테스트를 시작합니다...\n")

    history, metrics = run_backtest(data, config, cache_dir=args.cache_dir)

    history = benchmark_strategy(
        history, config, cache_dir=args.cache_dir, force_refresh=args.no_cache
    )

    print_report(history, metrics, config)
