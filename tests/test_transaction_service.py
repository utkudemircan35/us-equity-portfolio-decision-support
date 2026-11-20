import pandas as pd
import pytest

import transaction_service


def test_add_normal_buy(
    isolated_database,
    no_split_history,
):
    """
    A normal Buy must preserve raw values and
    calculate adjusted and cash-flow values.
    """

    result = (
        transaction_service.add_trade(
            date="2024-01-10",
            time_value="14:30:00",
            ticker="AAPL",
            side="Buy",
            raw_shares=2.5,
            raw_price_usd=190.25,
            commission_usd=1.50,
            refresh_calculated_data=False,
        )
    )

    trade = (
        transaction_service.get_trade(
            result.record_id
        )
    )

    assert trade["ticker"] == "AAPL"
    assert trade["side"] == "Buy"

    assert trade["raw_shares"] == pytest.approx(
        2.5
    )

    assert trade["raw_price_usd"] == pytest.approx(
        190.25
    )

    assert trade["split_factor"] == pytest.approx(
        1.0
    )

    assert trade["shares"] == pytest.approx(
        2.5
    )

    assert trade["price_usd"] == pytest.approx(
        190.25
    )

    expected_net_flow = -(
        2.5
        * 190.25
        + 1.50
    )

    assert trade["net_flow_usd"] == pytest.approx(
        expected_net_flow
    )

    assert trade[
        "cumulative_shares"
    ] == pytest.approx(
        2.5
    )


def test_add_old_trade_with_multiple_splits(
    isolated_database,
    monkeypatch,
):
    """
    An old trade must include every split that
    occurred after its transaction date.
    """

    split_history = [
        (
            pd.Timestamp(
                "2022-07-15"
            ),
            4.0,
        ),
        (
            pd.Timestamp(
                "2024-06-10"
            ),
            10.0,
        ),
    ]

    monkeypatch.setattr(
        transaction_service,
        "download_split_history_strict",
        lambda ticker: split_history,
    )

    result = (
        transaction_service.add_trade(
            date="2021-01-05",
            ticker="TEST",
            side="Buy",
            raw_shares=1.0,
            raw_price_usd=400.0,
            commission_usd=0.0,
            refresh_calculated_data=False,
        )
    )

    trade = (
        transaction_service.get_trade(
            result.record_id
        )
    )

    assert trade["split_factor"] == pytest.approx(
        40.0
    )

    assert trade["shares"] == pytest.approx(
        40.0
    )

    assert trade["price_usd"] == pytest.approx(
        10.0
    )

    assert trade["net_flow_usd"] == pytest.approx(
        -400.0
    )


def test_trade_on_split_date_is_post_split(
    isolated_database,
    monkeypatch,
):
    """
    A trade on the effective split date must not
    receive that split again.
    """

    split_history = [
        (
            pd.Timestamp(
                "2024-06-10"
            ),
            10.0,
        ),
    ]

    monkeypatch.setattr(
        transaction_service,
        "download_split_history_strict",
        lambda ticker: split_history,
    )

    result = (
        transaction_service.add_trade(
            date="2024-06-10",
            ticker="TEST",
            side="Buy",
            raw_shares=10.0,
            raw_price_usd=100.0,
            refresh_calculated_data=False,
        )
    )

    trade = (
        transaction_service.get_trade(
            result.record_id
        )
    )

    assert trade["split_factor"] == pytest.approx(
        1.0
    )

    assert trade["shares"] == pytest.approx(
        10.0
    )

    assert trade["price_usd"] == pytest.approx(
        100.0
    )


def test_edit_trade_recalculates_split(
    isolated_database,
    monkeypatch,
):
    """
    Moving a trade across a split date must
    recalculate its split factor.
    """

    split_history = [
        (
            pd.Timestamp(
                "2024-01-01"
            ),
            4.0,
        ),
    ]

    monkeypatch.setattr(
        transaction_service,
        "download_split_history_strict",
        lambda ticker: split_history,
    )

    added = (
        transaction_service.add_trade(
            date="2023-01-01",
            ticker="TEST",
            side="Buy",
            raw_shares=1.0,
            raw_price_usd=400.0,
            refresh_calculated_data=False,
        )
    )

    before_edit = (
        transaction_service.get_trade(
            added.record_id
        )
    )

    assert before_edit[
        "split_factor"
    ] == pytest.approx(
        4.0
    )

    transaction_service.edit_trade(
        trade_id=added.record_id,
        date="2024-01-01",
        ticker="TEST",
        side="Buy",
        raw_shares=4.0,
        raw_price_usd=100.0,
        refresh_calculated_data=False,
    )

    after_edit = (
        transaction_service.get_trade(
            added.record_id
        )
    )

    assert after_edit[
        "split_factor"
    ] == pytest.approx(
        1.0
    )

    assert after_edit[
        "shares"
    ] == pytest.approx(
        4.0
    )

    assert after_edit[
        "price_usd"
    ] == pytest.approx(
        100.0
    )


def test_gift_trade_has_zero_cash_flow(
    isolated_database,
    no_split_history,
):
    """
    Gift shares must increase shares without
    changing cash.
    """

    result = (
        transaction_service.add_trade(
            date="2024-01-10",
            ticker="MSFT",
            side="Buy",
            raw_shares=0.5,
            raw_price_usd=380.0,
            commission_usd=0.0,
            is_gift=True,
            refresh_calculated_data=False,
        )
    )

    trade = (
        transaction_service.get_trade(
            result.record_id
        )
    )

    assert trade["is_gift"] == 1

    assert trade["net_flow_usd"] == pytest.approx(
        0.0
    )

    assert trade[
        "cumulative_shares"
    ] == pytest.approx(
        0.5
    )


def test_gift_trade_cannot_be_sell(
    isolated_database,
    no_split_history,
):
    """
    A gift transaction cannot use Sell.
    """

    with pytest.raises(
        ValueError,
        match="gift transaction",
    ):
        transaction_service.add_trade(
            date="2024-01-10",
            ticker="MSFT",
            side="Sell",
            raw_shares=0.5,
            raw_price_usd=380.0,
            is_gift=True,
            refresh_calculated_data=False,
        )


def test_gift_trade_cannot_have_commission(
    isolated_database,
    no_split_history,
):
    """
    Gift shares cannot silently ignore a commission.
    """

    with pytest.raises(
        ValueError,
        match="cannot have a commission",
    ):
        transaction_service.add_trade(
            date="2024-01-10",
            ticker="MSFT",
            side="Buy",
            raw_shares=0.5,
            raw_price_usd=380.0,
            commission_usd=1.0,
            is_gift=True,
            refresh_calculated_data=False,
        )


def test_same_datetime_uses_trade_id_order(
    isolated_database,
    no_split_history,
):
    """
    Trades with identical dates and times must retain
    a deterministic trade-ID order.
    """

    first = (
        transaction_service.add_trade(
            date="2024-01-10",
            time_value="15:00:00",
            ticker="AAPL",
            side="Buy",
            raw_shares=1.0,
            raw_price_usd=100.0,
            refresh_calculated_data=False,
        )
    )

    second = (
        transaction_service.add_trade(
            date="2024-01-10",
            time_value="15:00:00",
            ticker="AAPL",
            side="Buy",
            raw_shares=2.0,
            raw_price_usd=110.0,
            refresh_calculated_data=False,
        )
    )

    first_trade = (
        transaction_service.get_trade(
            first.record_id
        )
    )

    second_trade = (
        transaction_service.get_trade(
            second.record_id
        )
    )

    assert first_trade[
        "cumulative_shares"
    ] == pytest.approx(
        1.0
    )

    assert second_trade[
        "cumulative_shares"
    ] == pytest.approx(
        3.0
    )


def test_sell_reduces_cumulative_shares(
    isolated_database,
    no_split_history,
):
    """
    Sell transactions must reduce the running balance.
    """

    transaction_service.add_trade(
        date="2024-01-01",
        ticker="AAPL",
        side="Buy",
        raw_shares=5.0,
        raw_price_usd=100.0,
        refresh_calculated_data=False,
    )

    sale = (
        transaction_service.add_trade(
            date="2024-02-01",
            ticker="AAPL",
            side="Sell",
            raw_shares=2.0,
            raw_price_usd=120.0,
            commission_usd=1.0,
            refresh_calculated_data=False,
        )
    )

    trade = (
        transaction_service.get_trade(
            sale.record_id
        )
    )

    assert trade[
        "net_flow_usd"
    ] == pytest.approx(
        239.0
    )

    assert trade[
        "cumulative_shares"
    ] == pytest.approx(
        3.0
    )


def test_overselling_is_rejected_and_rolled_back(
    isolated_database,
    no_split_history,
):
    """
    An invalid sale must not remain in the database.
    """

    purchase = (
        transaction_service.add_trade(
            date="2024-01-01",
            ticker="AAPL",
            side="Buy",
            raw_shares=1.0,
            raw_price_usd=100.0,
            refresh_calculated_data=False,
        )
    )

    with pytest.raises(
        RuntimeError,
        match="restored",
    ):
        transaction_service.add_trade(
            date="2024-02-01",
            ticker="AAPL",
            side="Sell",
            raw_shares=2.0,
            raw_price_usd=120.0,
            refresh_calculated_data=False,
        )

    trades = (
        transaction_service.list_trades()
    )

    assert len(trades) == 1

    assert int(
        trades.iloc[0]["trade_id"]
    ) == purchase.record_id

    assert trades.iloc[0][
        "cumulative_shares"
    ] == pytest.approx(
        1.0
    )


def test_delete_trade_recalculates_balances(
    isolated_database,
    no_split_history,
):
    """
    Deleting a trade must recalculate later balances.
    """

    first = (
        transaction_service.add_trade(
            date="2024-01-01",
            ticker="AAPL",
            side="Buy",
            raw_shares=1.0,
            raw_price_usd=100.0,
            refresh_calculated_data=False,
        )
    )

    second = (
        transaction_service.add_trade(
            date="2024-02-01",
            ticker="AAPL",
            side="Buy",
            raw_shares=2.0,
            raw_price_usd=110.0,
            refresh_calculated_data=False,
        )
    )

    transaction_service.delete_trade(
        trade_id=first.record_id,
        refresh_calculated_data=False,
    )

    remaining = (
        transaction_service.get_trade(
            second.record_id
        )
    )

    assert remaining[
        "cumulative_shares"
    ] == pytest.approx(
        2.0
    )


def test_delete_required_buy_is_rejected(
    isolated_database,
    no_split_history,
):
    """
    A Buy cannot be deleted when a later Sell would
    create a negative historical balance.
    """

    purchase = (
        transaction_service.add_trade(
            date="2024-01-01",
            ticker="AAPL",
            side="Buy",
            raw_shares=2.0,
            raw_price_usd=100.0,
            refresh_calculated_data=False,
        )
    )

    transaction_service.add_trade(
        date="2024-02-01",
        ticker="AAPL",
        side="Sell",
        raw_shares=1.0,
        raw_price_usd=120.0,
        refresh_calculated_data=False,
    )

    with pytest.raises(
        RuntimeError,
        match="restored",
    ):
        transaction_service.delete_trade(
            trade_id=purchase.record_id,
            refresh_calculated_data=False,
        )

    trades = (
        transaction_service.list_trades()
    )

    assert len(trades) == 2


def test_edit_ticker_recalculates_both_tickers(
    isolated_database,
    no_split_history,
):
    """
    Changing a ticker must rebuild balances for both
    the old and new ticker groups.
    """

    first = (
        transaction_service.add_trade(
            date="2024-01-01",
            ticker="AAPL",
            side="Buy",
            raw_shares=1.0,
            raw_price_usd=100.0,
            refresh_calculated_data=False,
        )
    )

    second = (
        transaction_service.add_trade(
            date="2024-02-01",
            ticker="AAPL",
            side="Buy",
            raw_shares=2.0,
            raw_price_usd=110.0,
            refresh_calculated_data=False,
        )
    )

    transaction_service.edit_trade(
        trade_id=first.record_id,
        date="2024-01-01",
        ticker="MSFT",
        side="Buy",
        raw_shares=1.0,
        raw_price_usd=100.0,
        refresh_calculated_data=False,
    )

    changed = (
        transaction_service.get_trade(
            first.record_id
        )
    )

    remaining = (
        transaction_service.get_trade(
            second.record_id
        )
    )

    assert changed["ticker"] == "MSFT"

    assert changed[
        "cumulative_shares"
    ] == pytest.approx(
        1.0
    )

    assert remaining["ticker"] == "AAPL"

    assert remaining[
        "cumulative_shares"
    ] == pytest.approx(
        2.0
    )


def test_refresh_failure_restores_database(
    isolated_database,
    no_split_history,
    monkeypatch,
):
    """
    A downstream refresh failure must restore the
    database snapshot.
    """

    def failing_refresh(
        progress_callback=None,
    ):
        raise RuntimeError(
            "Simulated refresh failure"
        )

    monkeypatch.setattr(
        transaction_service,
        "run_portfolio_refresh",
        failing_refresh,
    )

    with pytest.raises(
        RuntimeError,
        match="restored",
    ):
        transaction_service.add_trade(
            date="2024-01-01",
            ticker="AAPL",
            side="Buy",
            raw_shares=1.0,
            raw_price_usd=100.0,
            refresh_calculated_data=True,
        )

    trades = (
        transaction_service.list_trades()
    )

    assert trades.empty


def test_cash_movement_crud(
    isolated_database,
):
    """
    Cash movements must support add, edit and delete.
    """

    added = (
        transaction_service.add_cash_movement(
            date="2024-01-01",
            movement_type="deposit",
            amount_usd=1000.0,
            refresh_calculated_data=False,
        )
    )

    record = (
        transaction_service.get_cash_movement(
            added.record_id
        )
    )

    assert record["type"] == "DEPOSIT"

    assert record[
        "amount_usd"
    ] == pytest.approx(
        1000.0
    )

    transaction_service.edit_cash_movement(
        cash_id=added.record_id,
        date="2024-01-02",
        movement_type="withdrawal",
        amount_usd=250.0,
        time_value="12:30:00",
        refresh_calculated_data=False,
    )

    edited = (
        transaction_service.get_cash_movement(
            added.record_id
        )
    )

    assert edited["type"] == "WITHDRAWAL"

    assert edited[
        "amount_usd"
    ] == pytest.approx(
        250.0
    )

    transaction_service.delete_cash_movement(
        cash_id=added.record_id,
        refresh_calculated_data=False,
    )

    assert (
        transaction_service
        .list_cash_movements()
        .empty
    )


def test_dividend_crud(
    isolated_database,
):
    """
    Dividends must support add, edit and delete.
    """

    added = (
        transaction_service.add_dividend(
            date="2024-01-01",
            ticker="AAPL",
            net_dividend_usd=5.25,
            refresh_calculated_data=False,
        )
    )

    record = (
        transaction_service.get_dividend(
            added.record_id
        )
    )

    assert record["ticker"] == "AAPL"

    assert record[
        "net_dividend_usd"
    ] == pytest.approx(
        5.25
    )

    transaction_service.edit_dividend(
        dividend_id=added.record_id,
        date="2024-01-02",
        ticker="MSFT",
        net_dividend_usd=6.50,
        refresh_calculated_data=False,
    )

    edited = (
        transaction_service.get_dividend(
            added.record_id
        )
    )

    assert edited["ticker"] == "MSFT"

    assert edited[
        "net_dividend_usd"
    ] == pytest.approx(
        6.50
    )

    transaction_service.delete_dividend(
        dividend_id=added.record_id,
        refresh_calculated_data=False,
    )

    assert (
        transaction_service
        .list_dividends()
        .empty
    )
