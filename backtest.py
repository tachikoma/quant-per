from config import Config
from engine import fetch_rebalancing_data, run_backtest
from report import benchmark_strategy, print_report


if __name__ == "__main__":
    config = Config.from_env()

    print("=" * 60)
    print("  KOSPI/KOSDAQ 실거래 데이터 기반 가치투자 백테스트 엔진")
    print("=" * 60)

    data = fetch_rebalancing_data(config.start_date, config.end_date)
    print("\n데이터 수집 완료. 백테스트를 시작합니다...\n")

    history, metrics = run_backtest(data, config)

    history = benchmark_strategy(history, config)

    print_report(history, metrics, config)
