"""DART 재무제표 배치 수집 스크립트.

market_data 캐시에 있는 티커(유니버스)의 연간 사업보고서 재무제표를
DART API로 수집해 .cache/backtest/dart_data/ 에 캐싱한다.

사용법:
    uv run python collect_dart_data.py [--years 2015 2016 ...] [--limit N]

주의:
    - 일 20,000건 / 분당 1,000회 API 한도 준수 (자동 sleep)
    - 이미 캐시된 종목은 건너뜀 (--force-refresh로 재수집)
"""

import argparse
import sys

import pandas as pd

from dart_data import (
    DEFAULT_CACHE_DIR,
    fetch_annual_financials,
    fetch_corp_codes,
    load_dart_financials_cache,
)
from config import Config


def parse_args():
    p = argparse.ArgumentParser(description="DART 재무제표 배치 수집")
    p.add_argument(
        "--years",
        nargs="+",
        type=int,
        default=None,
        help="수집할 사업연도 (기본: 2015~2025)",
    )
    p.add_argument(
        "--limit", type=int, default=0, help="수집할 종목 수 제한 (테스트용, 0=전체)"
    )
    p.add_argument("--force-refresh", action="store_true", help="캐시 무시하고 재수집")
    p.add_argument(
        "--reparse-incomplete",
        action="store_true",
        help="운영이익 등 핵심 지표가 누락된 행만 재조회 (IS 매핑 수정 후 사용)",
    )
    p.add_argument(
        "--sleep-per-call",
        type=float,
        default=0.07,
        help="호출 간 대기시간 (분당 1000회 한도 준수, 기본 0.07초)",
    )
    return p.parse_args()


def main():
    args = parse_args()
    config = Config.from_env()
    if not config.dart_api_key:
        print("ERROR: DART_API_KEY가 .env에 없습니다.")
        sys.exit(1)

    cache_base = DEFAULT_CACHE_DIR
    years = args.years or list(range(2015, 2026))

    # 1) market_data 유니버스 티커
    market_dir = cache_base / "market_data"
    if not market_dir.exists():
        print("ERROR: market_data 캐시가 없습니다. 먼저 백테스트를 1회 실행하세요.")
        sys.exit(1)
    all_codes = set()
    for f in sorted(market_dir.glob("*.parquet")):
        df = pd.read_parquet(f, columns=["code"])
        all_codes.update(df["code"].unique())
    print(f"market_data 유니버스 티커: {len(all_codes)}개")

    # 2) DART corp_code 매핑
    codes_df = fetch_corp_codes(cache_dir=cache_base)
    t2c = dict(zip(codes_df["ticker"], codes_df["corp_code"]))
    mapped = {t for t in all_codes if t in t2c}
    print(
        f"DART 매핑 가능 티커: {len(mapped)}개 (미매핑 {len(all_codes) - len(mapped)}개 스킵)"
    )

    # 3) 이미 캐시된 종목 확인
    cached = load_dart_financials_cache(cache_dir=cache_base)
    cached_corps = set(cached["corp_code"]) if not cached.empty else set()
    print(f"이미 재무제표 캐시된 corp: {len(cached_corps)}개")

    # 4) 수집 대상 corp_code (연도별로 누락분만)
    targets = []
    for t in sorted(mapped):
        cc = t2c[t]
        missing_years = []
        if cc in cached_corps and not args.force_refresh:
            cc_df = cached[cached["corp_code"] == cc]
            have = set(cc_df["year"])
            missing_years = [y for y in years if y not in have]
            if args.reparse_incomplete:
                # 핵심 지표(운영이익) 누락 행도 재조회 대상에 포함
                incomplete = pd.DataFrame(cc_df)[
                    pd.DataFrame(cc_df)["operating_income"].isna()
                ]["year"]
                missing_years = sorted(set(missing_years) | set(incomplete))
        else:
            missing_years = years
        if missing_years:
            targets.append((cc, missing_years))
    if args.limit > 0:
        targets = targets[: args.limit]
    total_requests = sum(len(my) for _, my in targets)
    print(f"수집 대상 corp: {len(targets)}개, API 호출 예정: {total_requests}건")
    if total_requests == 0:
        print("수집할 데이터가 없습니다.")
        return

    # 5) 배치 수집 (일 20,000건 / 분당 1,000회 한도 준수)
    import time
    from tqdm import tqdm

    daily_budget = 19_500  # 일 20,000건 한도 대비 안전 마진
    calls_per_day = 0
    calls = 0
    t0 = time.time()
    ok = 0
    with tqdm(total=total_requests, desc="DART 재무제표 수집") as pbar:
        for cc, missing_years in targets:
            for y in missing_years:
                # 일일 한도 도달 → 다음 자정(KST)까지 대기
                if calls_per_day >= daily_budget:
                    now = time.localtime()
                    next_midnight = (
                        time.mktime(
                            (
                                now.tm_year,
                                now.tm_mon,
                                now.tm_mday + 1,
                                0,
                                0,
                                0,
                                0,
                                0,
                                -1,
                            )
                        )
                        - 9 * 3600
                    )  # KST 자정
                    wait = max(0, next_midnight - time.time())
                    print(
                        f"\n[일일 한도 도달] {calls_per_day}건 완료. 다음 자정까지 {wait / 3600:.1f}시간 대기..."
                    )
                    time.sleep(wait)
                    calls_per_day = 0

                # 분당 한도 준수
                if calls >= 900:
                    elapsed = time.time() - t0
                    if elapsed < 60:
                        time.sleep(60 - elapsed)
                    calls, t0 = 0, time.time()
                try:
                    row = fetch_annual_financials(
                        cc,
                        y,
                        cache_dir=cache_base,
                        force_refresh=args.force_refresh or args.reparse_incomplete,
                    )
                    if row:
                        ok += 1
                except Exception as e:
                    tqdm.write(f"  {cc}/{y} 오류: {e}")
                calls += 1
                calls_per_day += 1
                pbar.update(1)
                time.sleep(args.sleep_per_call)

    print(f"\n완료! 성공: {ok}건")


if __name__ == "__main__":
    main()
