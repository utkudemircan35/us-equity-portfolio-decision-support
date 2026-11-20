from typing import Any

import pandas as pd
import yfinance as yf

from database import (
    database_transaction,
    fetch_all,
    validate_database,
)


BENCHMARK_TICKER = "SPY"
CASH_TICKER = "CASH"


def load_required_tickers() -> list[str]:
    """
    Load all portfolio tickers and include the benchmark.
    """

    rows = fetch_all(
        """
        SELECT DISTINCT ticker
        FROM trades
        WHERE ticker IS NOT NULL
          AND TRIM(ticker) <> ''
        ORDER BY ticker
        """
    )

    tickers = {
        str(row["ticker"]).strip().upper()
        for row in rows
    }

    tickers.add(BENCHMARK_TICKER)

    return sorted(tickers)


def load_existing_assets() -> dict[str, dict[str, Any]]:
    """
    Load existing metadata for use during temporary download failures.
    """

    rows = fetch_all(
        """
        SELECT
            ticker,
            company_name,
            asset_type,
            sector,
            industry
        FROM assets
        ORDER BY ticker
        """
    )

    return {
        str(row["ticker"]).strip().upper(): row
        for row in rows
    }


def clean_text(value: Any) -> str | None:
    """
    Convert a metadata value into clean text.
    """

    if value is None:
        return None

    cleaned_value = str(value).strip()

    if not cleaned_value:
        return None

    return cleaned_value


def download_yahoo_metadata(
    ticker: str,
) -> tuple[dict[str, Any], str | None]:
    """
    Download metadata for one ticker from Yahoo Finance.
    """

    try:
        information = yf.Ticker(ticker).get_info()

    except Exception as error:
        return {}, str(error)

    if not isinstance(information, dict):
        return {}, "Yahoo Finance returned an invalid response."

    return information, None


def normalize_asset_type(
    quote_type: Any,
) -> str:
    """
    Convert Yahoo Finance quote types into application asset types.
    """

    normalized_quote_type = (
        clean_text(quote_type) or ""
    ).upper()

    if normalized_quote_type == "EQUITY":
        return "EQUITY"

    if normalized_quote_type == "ETF":
        return "ETF"

    return "UNKNOWN"


def build_asset_record(
    ticker: str,
    information: dict[str, Any],
) -> dict[str, str]:
    """
    Build one normalized asset metadata record.
    """

    company_name = (
        clean_text(information.get("longName"))
        or clean_text(information.get("shortName"))
        or ticker
    )

    asset_type = normalize_asset_type(
        information.get("quoteType")
    )

    yahoo_sector = clean_text(
        information.get("sector")
    )

    yahoo_industry = clean_text(
        information.get("industry")
    )

    if asset_type == "ETF":
        sector = yahoo_sector or "ETF / Fund"
        industry = (
            yahoo_industry
            or "Exchange Traded Fund"
        )

    elif asset_type == "EQUITY":
        sector = yahoo_sector or "Unknown"
        industry = yahoo_industry or "Unknown"

    else:
        sector = yahoo_sector or "Unknown"
        industry = yahoo_industry or "Unknown"

    return {
        "ticker": ticker,
        "company_name": company_name,
        "asset_type": asset_type,
        "sector": sector,
        "industry": industry,
    }


def build_cash_record() -> dict[str, str]:
    """
    Build the application-defined USD cash record.
    """

    return {
        "ticker": CASH_TICKER,
        "company_name": "US Dollar Cash",
        "asset_type": "CASH",
        "sector": "Cash",
        "industry": "Cash",
    }


def build_asset_metadata() -> tuple[
    pd.DataFrame,
    list[str],
    dict[str, str],
]:
    """
    Download and normalize metadata for all required assets.
    """

    tickers = load_required_tickers()
    existing_assets = load_existing_assets()

    records: list[dict[str, str]] = [
        build_cash_record()
    ]

    unavailable_tickers: list[str] = []
    failed_downloads: dict[str, str] = {}

    for ticker in tickers:
        information, error = (
            download_yahoo_metadata(ticker)
        )

        if error is not None:
            failed_downloads[ticker] = error

            existing_record = existing_assets.get(
                ticker
            )

            if existing_record is not None:
                records.append(
                    {
                        "ticker": ticker,
                        "company_name": str(
                            existing_record[
                                "company_name"
                            ]
                        ),
                        "asset_type": str(
                            existing_record[
                                "asset_type"
                            ]
                        ),
                        "sector": str(
                            existing_record["sector"]
                        ),
                        "industry": str(
                            existing_record["industry"]
                        ),
                    }
                )

                continue

        record = build_asset_record(
            ticker=ticker,
            information=information,
        )

        if record["asset_type"] == "UNKNOWN":
            unavailable_tickers.append(ticker)

        records.append(record)

    assets = pd.DataFrame(records)

    assets = (
        assets
        .drop_duplicates(
            subset=["ticker"],
            keep="last",
        )
        .sort_values(
            "ticker",
            kind="stable",
        )
        .reset_index(drop=True)
    )

    return (
        assets,
        unavailable_tickers,
        failed_downloads,
    )


def save_assets(
    assets: pd.DataFrame,
) -> int:
    """
    Replace the assets table with refreshed metadata.
    """

    query = """
        INSERT INTO assets (
            ticker,
            company_name,
            asset_type,
            sector,
            industry
        )
        VALUES (?, ?, ?, ?, ?)
    """

    rows = [
        (
            row.ticker,
            row.company_name,
            row.asset_type,
            row.sector,
            row.industry,
        )
        for row in assets.itertuples(
            index=False
        )
    ]

    with database_transaction() as connection:
        connection.execute(
            "DELETE FROM assets"
        )

        connection.executemany(
            query,
            rows,
        )

    return len(rows)


def refresh_asset_metadata() -> dict[str, Any]:
    """
    Download, validate and save asset metadata.
    """

    validate_database()

    (
        assets,
        unavailable_tickers,
        failed_downloads,
    ) = build_asset_metadata()

    saved_rows = save_assets(assets)

    asset_type_counts = (
        assets["asset_type"]
        .value_counts()
        .to_dict()
    )

    return {
        "saved_rows": saved_rows,
        "equities": int(
            asset_type_counts.get("EQUITY", 0)
        ),
        "etfs": int(
            asset_type_counts.get("ETF", 0)
        ),
        "cash": int(
            asset_type_counts.get("CASH", 0)
        ),
        "unknown": int(
            asset_type_counts.get("UNKNOWN", 0)
        ),
        "unavailable_tickers":
            unavailable_tickers,
        "failed_downloads":
            failed_downloads,
    }


def main() -> None:
    """
    Refresh asset metadata and print summary information.
    """

    result = refresh_asset_metadata()

    print(
        "Asset metadata refreshed successfully."
    )

    print(
        f"Rows saved: {result['saved_rows']}"
    )

    print(
        f"Equities: {result['equities']}"
    )

    print(
        f"ETFs: {result['etfs']}"
    )

    print(
        f"Cash: {result['cash']}"
    )

    print(
        f"Unknown: {result['unknown']}"
    )

    unavailable_tickers = result[
        "unavailable_tickers"
    ]

    if unavailable_tickers:
        print()
        print(
            "Metadata unavailable for:"
        )

        for ticker in unavailable_tickers:
            print(f"- {ticker}")

    failed_downloads = result[
        "failed_downloads"
    ]

    if failed_downloads:
        print()
        print(
            "Metadata download errors:"
        )

        for ticker, error in (
            failed_downloads.items()
        ):
            print(f"- {ticker}: {error}")


if __name__ == "__main__":
    main()
