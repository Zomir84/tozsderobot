import os
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest


SYMBOL = "NVDA"
MA_FAST = 10
MA_SLOW = 30
ADX_PERIOD = 14
RSI_PERIOD = 14

ADX_MIN_ENTRY = 20.0
RSI_MAX_ENTRY = 70.0
RSI_EXIT = 80.0

# Pozícióméret: a számla elérhető készpénzének legfeljebb hány százalékát használja egy belépésnél
ALLOCATION_PCT = 0.25


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ma_fast"] = out["close"].rolling(window=MA_FAST).mean()
    out["ma_slow"] = out["close"].rolling(window=MA_SLOW).mean()

    # RSI (Wilder simítás)
    delta = out["close"].diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / RSI_PERIOD, min_periods=RSI_PERIOD, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / RSI_PERIOD, min_periods=RSI_PERIOD, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out["rsi"] = 100.0 - (100.0 / (1.0 + rs))

    # ADX (Wilder simítás)
    high = out["high"]
    low = out["low"]
    close = out["close"]

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0.0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0.0), down_move, 0.0)

    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1.0 / ADX_PERIOD, min_periods=ADX_PERIOD, adjust=False).mean()
    plus_di = 100.0 * pd.Series(plus_dm, index=out.index).ewm(
        alpha=1.0 / ADX_PERIOD, min_periods=ADX_PERIOD, adjust=False
    ).mean() / atr.replace(0.0, np.nan)
    minus_di = 100.0 * pd.Series(minus_dm, index=out.index).ewm(
        alpha=1.0 / ADX_PERIOD, min_periods=ADX_PERIOD, adjust=False
    ).mean() / atr.replace(0.0, np.nan)

    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    out["adx"] = dx.ewm(alpha=1.0 / ADX_PERIOD, min_periods=ADX_PERIOD, adjust=False).mean()

    return out


def fetch_nvda_bars(data_client: StockHistoricalDataClient) -> pd.DataFrame:
    # Bőséges visszatekintés a hétvégék, ünnepek és MA30/ADX/RSI bemelegedése miatt
    start_dt = datetime.now(timezone.utc) - timedelta(days=45)
    request = StockBarsRequest(
        symbol_or_symbols=[SYMBOL],
        timeframe=TimeFrame.Hour,
        start=start_dt,
        feed=DataFeed.IEX,
    )
    bars = data_client.get_stock_bars(request)
    df = bars.df

    if df.empty:
        raise RuntimeError("Nem érkezett NVDA gyertyaadat az Alpaca Data API-tól.")

    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(SYMBOL, level="symbol").copy()

    df.index = pd.to_datetime(df.index, utc=True)
    df = df.sort_index()
    return df


def main() -> None:
    api_key = os.environ.get("APCA_API_KEY_ID", "").strip()
    secret_key = os.environ.get("APCA_API_SECRET_KEY", "").strip()
    base_url = os.environ.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets").strip()
    force_run = os.environ.get("FORCE_RUN", "false").lower() == "true"

    if not api_key or not secret_key:
        raise RuntimeError("Hiányzik az APCA_API_KEY_ID vagy az APCA_API_SECRET_KEY környezeti változó.")

    is_paper = "paper" in base_url.lower()
    trading_client = TradingClient(api_key=api_key, secret_key=secret_key, paper=is_paper)
    data_client = StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)

    clock = trading_client.get_clock()
    print(f"Piaci állapot | is_open={clock.is_open} | timestamp={clock.timestamp}")

    if not clock.is_open and not force_run:
        print("A piac jelenleg zárva van. Megbízás nem kerül kiküldésre.")
        return

    raw_df = fetch_nvda_bars(data_client)
    ind_df = compute_indicators(raw_df).dropna(subset=["ma_fast", "ma_slow", "adx", "rsi"])

    if len(ind_df) < 2:
        raise RuntimeError("Nincs elég feldolgozható gyertya az indikátorok kiértékeléséhez.")

    prev = ind_df.iloc[-2]
    curr = ind_df.iloc[-1]

    ma_cross_up = (prev["ma_fast"] <= prev["ma_slow"]) and (curr["ma_fast"] > curr["ma_slow"])
    ma_cross_down = (prev["ma_fast"] >= prev["ma_slow"]) and (curr["ma_fast"] < curr["ma_slow"])

    print(
        f"{SYMBOL} utolsó gyertya ({curr.name}) | close={curr['close']:.2f} | "
        f"MA10={curr['ma_fast']:.2f} | MA30={curr['ma_slow']:.2f} | "
        f"ADX={curr['adx']:.2f} | RSI={curr['rsi']:.2f}"
    )

    open_orders = trading_client.get_orders(
        filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[SYMBOL])
    )
    if open_orders:
        print(f"Van {len(open_orders)} nyitott megbízás {SYMBOL}-ra; új megbízást most nem küldök.")
        return

    has_position = False
    qty_held = 0.0
    try:
        pos = trading_client.get_open_position(SYMBOL)
        qty_held = float(pos.qty)
        has_position = qty_held > 0
    except Exception:
        has_position = False

    print(f"Jelenlegi pozíció | has_position={has_position} | qty={qty_held}")

    if has_position:
        if ma_cross_down or curr["rsi"] >= RSI_EXIT:
            order_req = MarketOrderRequest(
                symbol=SYMBOL,
                qty=qty_held,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
            order = trading_client.submit_order(order_req)
            print(f"ELADÁSI megbízás elküldve | id={order.id} | qty={qty_held}")
        else:
            print("Pozíció tartása: kilépési feltétel nem teljesült.")
    else:
        entry_ok = (
            ma_cross_up
            and (curr["adx"] >= ADX_MIN_ENTRY)
            and (curr["rsi"] < RSI_MAX_ENTRY)
        )
        if entry_ok:
            account = trading_client.get_account()
            buying_power = float(account.buying_power)
            budget = buying_power * ALLOCATION_PCT
            price = float(curr["close"])
            qty = int(budget // price)

            if qty >= 1:
                order_req = MarketOrderRequest(
                    symbol=SYMBOL,
                    qty=qty,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                )
                order = trading_client.submit_order(order_req)
                print(f"VÉTELI megbízás elküldve | id={order.id} | qty={qty} | price~{price:.2f}")
            else:
                print(f"A keret ({budget:.2f} USD) kevesebb mint 1 db {SYMBOL} ára ({price:.2f} USD).")
        else:
            print("Nincs belépési jelzés.")


if __name__ == "__main__":
    main()
