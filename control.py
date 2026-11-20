import sqlite3
import pandas as pd


connection = sqlite3.connect(
    "portfolio.db"
)

trade_check = pd.read_sql_query(
    """
    SELECT
        trade_id,
        date,
        ticker,
        side,
        raw_shares,
        raw_price_usd,
        split_factor,
        is_gift,
        shares,
        price_usd,
        net_flow_usd,
        cumulative_shares
    FROM trades
    ORDER BY
        date,
        COALESCE(
            time,
            '00:00:00'
        ),
        trade_id
    """,
    connection,
)

connection.close()

print(
    trade_check[
        trade_check["split_factor"] != 1
    ].to_string(index=False)
)

print()
print("Gift trades")

print(
    trade_check[
        trade_check["is_gift"] == 1
    ].to_string(index=False)
)
