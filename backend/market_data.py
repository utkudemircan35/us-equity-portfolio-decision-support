from argparse import ArgumentParser
from datetime import date, timedelta
from typing import Any

import pandas as pd
import yfinance as yf

from database import (
    database_transaction,
    fetch_all,
    validate_database,
)


DEFAULT_BENCHMARKS = ("SPY",)
KNOWN_DELISTED_TICKERS = {"TWTR"}

SHARE_ZERO_TOLERANCE = 1e-10
START_DATE_BUFFER_DAYS = 7
OPEN_POSITION_LOOKBACK_DAYS = 3 * 365


def get_position_date_ranges() -> list[dict[str, Any]]:
    """
    Return the holding date range and current status of every ticker.
    """

    rows = fetch_all(
        """
        WITH ranked_trades AS (
            SELECT
                ticker,
                cumulative_shares,
                ROW_NUMBER() OVER (
                    PARTITION BY ticker
                    ORDER BY
                        date DESC,
                        COALESCE(time, '00:00:00') DESC,
                        trade_id DESC
                ) AS row_number
            FROM trades
        ),
        trade_ranges AS (
            SELECT
                ticker,
                MIN(date) AS first_trade_date,
                MAX(date) AS last_trade_date
            FROM trades
            GROUP BY ticker
        )
        SELECT
            trade_ranges.ticker,
            trade_ranges.first_trade_date,
            trade_ranges.last_trade_date,
            ranked_trades.cumulative_shares
        FROM trade_ranges
        JOIN ranked_trades
            ON ranked_trades.ticker =
               trade_ranges.ticker
        WHERE ranked_trades.row_number = 1
        ORDER BY trade_ranges.ticker
        """
    )

    positions: list[dict[str, Any]] = []

    for row in rows:
        cumulative_shares = float(
            row["cumulative_shares"]
        )

        if (
            abs(cumulative_shares)
            > SHARE_ZERO_TOLERANCE
        ):
            status = "OPEN"

        else:
            status = "CLOSED"

        first_trade_date = date.fromisoformat(
            row["first_trade_date"]
        )

        last_trade_date = date.fromisoformat(
            row["last_trade_date"]
        )

        positions.append(
            {
                "ticker": str(
                    row["ticker"]
                ).strip().upper(),
                "status": status,
                "cumulative_shares": (
                    cumulative_shares
                ),
                "first_trade_date": (
                    first_trade_date
                ),
                "last_trade_date": (
                    last_trade_date
                ),
            }
        )

    return positions


def build_download_requests(
    include_benchmarks: bool = True,
) -> list[dict[str, Any]]:
    """
    Build download ranges for open positions, closed positions
    and benchmarks.
    """

    positions = get_position_date_ranges()

    if not positions:
        raise RuntimeError(
            "No portfolio positions were found."
        )

    tomorrow = date.today() + timedelta(days=1)

    requests: list[dict[str, Any]] = []

    for position in positions:
        trade_start_date = (
            position["first_trade_date"]
            - timedelta(
                days=START_DATE_BUFFER_DAYS
            )
        )

        if position["status"] == "OPEN":
            risk_lookback_date = (
                date.today()
                - timedelta(
                    days=OPEN_POSITION_LOOKBACK_DAYS
                )
            )

            start_date = min(
                trade_start_date,
                risk_lookback_date,
            )

            # The end date is exclusive.
            end_date = tomorrow

        else:
            start_date = trade_start_date

            # Closed positions only need prices through
            # the final transaction date.
            end_date = (
                position["last_trade_date"]
                + timedelta(days=1)
            )

        requests.append(
            {
                "ticker": position["ticker"],
                "instrument_role": (
                    position["status"]
                ),
                "start_date": start_date,
                "end_date": end_date,
            }
        )

    if include_benchmarks:
        earliest_trade_date = min(
            position["first_trade_date"]
            for position in positions
        )

        benchmark_start_date = (
            earliest_trade_date
            - timedelta(
                days=START_DATE_BUFFER_DAYS
            )
        )

        for ticker in DEFAULT_BENCHMARKS:
            requests.append(
                {
                    "ticker": ticker,
                    "instrument_role": (
                        "BENCHMARK"
                    ),
                    "start_date": (
                        benchmark_start_date
                    ),
                    "end_date": tomorrow,
                }
            )

    return requests


def normalize_download_columns(
    dataframe: pd.DataFrame,
    ticker: str,
) -> pd.DataFrame:
    """
    Convert yfinance output to ordinary single-level columns.
    """

    data = dataframe.copy()

    if not isinstance(data.columns, pd.MultiIndex):
        return data

    level_zero = data.columns.get_level_values(0)
    level_one = data.columns.get_level_values(1)

    if ticker in level_one:
        return data.xs(
            ticker,
            axis=1,
            level=1,
            drop_level=True,
        )

    if ticker in level_zero:
        return data.xs(
            ticker,
            axis=1,
            level=0,
            drop_level=True,
        )

    raise ValueError(
        f"Unexpected yfinance column structure for {ticker}."
    )


def download_ticker_prices(
    ticker: str,
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """
    Download historical daily prices for one ticker.

    Yahoo Close is already adjusted for stock splits.
    Yahoo Adj Close also includes dividend distributions.
    """

    downloaded = yf.download(
        tickers=ticker,
        start=start_date.isoformat(),
        end=end_date.isoformat(),
        interval="1d",
        auto_adjust=False,
        actions=True,
        repair=False,
        keepna=False,
        progress=False,
        threads=False,
        rounding=False,
        timeout=20,
        multi_level_index=False,
    )

    if downloaded is None or downloaded.empty:
        raise ValueError(
            f"No market prices were returned for {ticker}."
        )

    downloaded = normalize_download_columns(
        downloaded,
        ticker,
    )

    if "Close" not in downloaded.columns:
        raise ValueError(
            f"Close prices were not returned for {ticker}."
        )

    prices = pd.DataFrame(
        index=downloaded.index
    )

    # Yahoo Close is already adjusted for stock splits.
    # It does not include dividend distributions.
    prices["close_usd"] = pd.to_numeric(
        downloaded["Close"],
        errors="coerce",
    )

    if "Adj Close" in downloaded.columns:
        # Adjusted Close includes stock splits and distributions.
        prices["adjusted_close_usd"] = (
            pd.to_numeric(
                downloaded["Adj Close"],
                errors="coerce",
            )
        )

    else:
        # Use Close only when Yahoo does not provide Adj Close.
        prices["adjusted_close_usd"] = prices[
            "close_usd"
        ]

    prices = prices.dropna(
        subset=[
            "close_usd",
            "adjusted_close_usd",
        ]
    )

    if prices.empty:
        raise ValueError(
            f"All downloaded prices were invalid for {ticker}."
        )

    prices = prices.reset_index()

    source_date_column = prices.columns[0]

    prices["date"] = pd.to_datetime(
        prices[source_date_column],
        errors="raise",
    ).dt.strftime("%Y-%m-%d")

    prices["ticker"] = ticker

    prices["key"] = (
        prices["ticker"]
        + "|"
        + prices["date"]
    )

    return prices[
        [
            "key",
            "ticker",
            "date",
            "close_usd",
            "adjusted_close_usd",
        ]
    ].copy()

def download_all_market_prices(
    requests: list[dict[str, Any]],
) -> tuple[
    pd.DataFrame,
    dict[str, str],
    dict[str, str],
]:
    """
    Download all available prices and identify fallback tickers.
    """

    price_frames: list[pd.DataFrame] = []

    trade_fallback_tickers: dict[str, str] = {}
    failed_required_tickers: dict[str, str] = {}

    for request in requests:
        ticker = request["ticker"]
        role = request["instrument_role"]
        start_date = request["start_date"]
        end_date = request["end_date"]

        if ticker in KNOWN_DELISTED_TICKERS:
            trade_fallback_tickers[ticker] = (
                "Known delisted ticker. "
                "Trade-price fallback will be used."
            )

            print(
                f"{ticker}: known delisted ticker, "
                "skipping yfinance."
            )

            continue

        print(
            f"Downloading {ticker} "
            f"({role}) from {start_date} "
            f"through {end_date - timedelta(days=1)}"
        )

        try:
            ticker_prices = download_ticker_prices(
                ticker=ticker,
                start_date=start_date,
                end_date=end_date,
            )

            price_frames.append(
                ticker_prices
            )

            print(
                f"{ticker}: "
                f"{len(ticker_prices)} prices downloaded"
            )

        except Exception as error:
            message = str(error)

            if role == "CLOSED":
                trade_fallback_tickers[ticker] = (
                    message
                )

                print(
                    f"{ticker}: historical prices unavailable. "
                    "Trade-price fallback will be used."
                )

            else:
                failed_required_tickers[ticker] = (
                    message
                )

                print(
                    f"{ticker}: required market prices "
                    f"could not be downloaded: {message}"
                )

    if price_frames:
        combined_prices = pd.concat(
            price_frames,
            ignore_index=True,
        )

        combined_prices = (
            combined_prices
            .drop_duplicates(
                subset=["key"],
                keep="last",
            )
            .sort_values(
                by=["ticker", "date"],
                kind="stable",
            )
            .reset_index(drop=True)
        )

    else:
        combined_prices = pd.DataFrame(
            columns=[
                "key",
                "ticker",
                "date",
                "close_usd",
                "adjusted_close_usd",
            ]
        )

    return (
        combined_prices,
        trade_fallback_tickers,
        failed_required_tickers,
    )


def save_market_prices(
    prices: pd.DataFrame,
    clear_existing: bool = False,
) -> int:
    """
    Save downloaded market prices to SQLite.
    """

    query = """
        INSERT INTO market_prices (
            key,
            ticker,
            date,
            close_usd,
            adjusted_close_usd
        )
        VALUES (?, ?, ?, ?, ?)

        ON CONFLICT(key) DO UPDATE SET
            ticker = excluded.ticker,
            date = excluded.date,
            close_usd = excluded.close_usd,
            adjusted_close_usd =
                excluded.adjusted_close_usd
    """

    rows = [
        (
            row.key,
            row.ticker,
            row.date,
            float(row.close_usd),
            float(row.adjusted_close_usd),
        )
        for row in prices.itertuples(index=False)
    ]

    with database_transaction() as connection:
        if clear_existing:
            connection.execute(
                "DELETE FROM market_prices"
            )

        if rows:
            connection.executemany(
                query,
                rows,
            )

    return len(rows)


def refresh_market_data(
    include_benchmarks: bool = True,
    clear_existing: bool = False,
) -> dict[str, Any]:
    """
    Refresh prices for open positions, closed positions
    and benchmarks.
    """

    validate_database()

    requests = build_download_requests(
        include_benchmarks=include_benchmarks
    )

    (
        prices,
        trade_fallback_tickers,
        failed_required_tickers,
    ) = download_all_market_prices(
        requests
    )

    saved_rows = save_market_prices(
        prices=prices,
        clear_existing=clear_existing,
    )

    open_tickers = sorted(
        request["ticker"]
        for request in requests
        if request["instrument_role"] == "OPEN"
    )

    closed_tickers = sorted(
        request["ticker"]
        for request in requests
        if request["instrument_role"] == "CLOSED"
    )

    benchmark_tickers = sorted(
        request["ticker"]
        for request in requests
        if request["instrument_role"] == "BENCHMARK"
    )

    return {
        "saved_rows": saved_rows,
        "open_tickers": open_tickers,
        "closed_tickers": closed_tickers,
        "benchmark_tickers": benchmark_tickers,
        "trade_fallback_tickers": (
            trade_fallback_tickers
        ),
        "failed_required_tickers": (
            failed_required_tickers
        ),
    }


def build_argument_parser() -> ArgumentParser:
    """
    Build command-line arguments.
    """

    parser = ArgumentParser(
        description=(
            "Download historical prices for open and "
            "closed portfolio positions."
        )
    )

    parser.add_argument(
        "--exclude-benchmark",
        action="store_true",
        help="Do not download the SPY benchmark.",
    )

    parser.add_argument(
        "--clear-existing",
        action="store_true",
        help=(
            "Delete existing market prices before "
            "saving the downloaded prices."
        ),
    )

    return parser


def main() -> None:
    """
    Run the market-data refresh.
    """

    parser = build_argument_parser()
    arguments = parser.parse_args()

    result = refresh_market_data(
        include_benchmarks=(
            not arguments.exclude_benchmark
        ),
        clear_existing=arguments.clear_existing,
    )

    print()
    print("Market-data refresh completed.")
    print(
        f"Open positions: "
        f"{len(result['open_tickers'])}"
    )
    print(
        f"Closed positions: "
        f"{len(result['closed_tickers'])}"
    )
    print(
        f"Benchmarks: "
        f"{len(result['benchmark_tickers'])}"
    )
    print(
        f"Price rows saved: "
        f"{result['saved_rows']}"
    )

    fallback_tickers = result[
        "trade_fallback_tickers"
    ]

    if fallback_tickers:
        print()
        print("Trade-price fallback tickers:")

        for ticker, reason in (
            fallback_tickers.items()
        ):
            print(f"- {ticker}: {reason}")

    failed_required = result[
        "failed_required_tickers"
    ]

    if failed_required:
        print()
        print("Required price downloads failed:")

        for ticker, reason in (
            failed_required.items()
        ):
            print(f"- {ticker}: {reason}")

        raise RuntimeError(
            "Market prices are missing for one or more "
            "open positions or benchmarks."
        )


if __name__ == "__main__":
    main()
