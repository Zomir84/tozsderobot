import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import ADXIndicator, SMAIndicator

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest


SYMBOL = "NVDA"
STATUS_FILE = Path("status.json")

MA_FAST = 10
MA_SLOW = 30
ADX_WINDOW = 14
RSI_WINDOW = 14

ADX_MIN_ENTRY = 25.0
RSI_MIN_ENTRY = 45.0
RSI_MAX_ENTRY = 68.0
RSI_EXIT = 75.0
STOP_LOSS_PCT = 0.04

ALLOCATION_PCT = 0.25


def write_nvda_status(payload: dict) -> None:
    data = {}
    if STATUS_FILE.exists():
        try:
            loaded = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}

    data["nvda"] = payload
    STATUS_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ma_fast"] = SMAIndicator(close=out["close"], window=MA_FAST).sma_indicator()
    out["ma_slow"] = SMAIndicator(close=out["close"], window=MA_SLOW).sma_indicator()
    out["rsi"] = RSIIndicator(close=out["close"], window=RSI_WINDOW).rsi()

    adx_ind = ADXIndicator(
        high=out["high"],
        low=out["low"],
        close=out["close"],
        window=ADX_WINDOW,
    )
    out["adx"] = adx_ind.adx()
    return out


def fetch_nvda_bars(data_client: StockHistoricalDataClient) -> pd.DataFrame:
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

    now_utc = datetime.now(timezone.utc).isoformat()
    clock = trading_client.get_clock()
    print(f"Piaci állapot | is_open={clock.is_open} | timestamp={clock.timestamp}")

    if not clock.is_open and not force_run:
        print("A piac jelenleg zárva van. Megbízás nem kerül kiküldésre.")
        write_nvda_status(
            {
                "updated_at": now_utc,
                "mode": "paper" if is_paper else "live",
                "market_open": bool(clock.is_open),
                "action": "market_closed",
                "message": "A piac jelenleg zárva van.",
            }
        )
        return

    raw_df = fetch_nvda_bars(data_client)
    ind_df = compute_indicators(raw_df).dropna(subset=["ma_fast", "ma_slow", "adx", "rsi"])

    if ind_df.empty:
        raise RuntimeError("Nincs elég feldolgozható gyertya az indikátorok kiértékeléséhez.")

    curr = ind_df.iloc[-1]
    price = float(curr["close"])
    ma10 = float(curr["ma_fast"])
    ma30 = float(curr["ma_slow"])
    adx = float(curr["adx"])
    rsi = float(curr["rsi"])

    trend_up = ma10 > ma30
    trend_down = ma10 < ma30

    print(
        f"{SYMBOL} utolsó gyertya ({curr.name}) | close={price:.2f} | "
        f"MA10={ma10:.2f} | MA30={ma30:.2f} | ADX={adx:.2f} | RSI={rsi:.2f}"
    )

    open_orders = trading_client.get_orders(
        filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[SYMBOL])
    )

    has_position = False
    qty_held = 0.0
    avg_entry_price = 0.0
    try:
        pos = trading_client.get_open_position(SYMBOL)
        qty_held = float(pos.qty)
        avg_entry_price = float(pos.avg_entry_price)
        has_position = qty_held > 0
    except Exception:
        has_position = False

    print(f"Jelenlegi pozíció | has_position={has_position} | qty={qty_held}")

    action = "no_signal"
    order_id = None
    order_qty = 0.0

    if open_orders:
        action = "open_order_exists"
        print(f"Van {len(open_orders)} nyitott megbízás {SYMBOL}-ra; új megbízást most nem küldök.")
    elif has_position:
        stop_hit = avg_entry_price > 0 and price <= avg_entry_price * (1.0 - STOP_LOSS_PCT)
        if trend_down or (rsi >= RSI_EXIT) or stop_hit:
            order_req = MarketOrderRequest(
                symbol=SYMBOL,
                qty=qty_held,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
            order = trading_client.submit_order(order_req)
            action = "sell"
            order_id = str(order.id)
            order_qty = qty_held
            print(f"ELADÁSI megbízás elküldve | id={order.id} | qty={qty_held} | stop_hit={stop_hit}")
        else:
            action = "hold"
            print("Pozíció tartása: kilépési és stop-loss feltétel sem teljesült.")
    else:
        entry_ok = (
            trend_up
            and (adx >= ADX_MIN_ENTRY)
            and (RSI_MIN_ENTRY <= rsi <= RSI_MAX_ENTRY)
        )
        if entry_ok:
            account = trading_client.get_account()
            buying_power = float(account.buying_power)
            budget = buying_power * ALLOCATION_PCT
            qty = int(budget // price)

            if qty >= 1:
                order_req = MarketOrderRequest(
                    symbol=SYMBOL,
                    qty=qty,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                )
                order = trading_client.submit_order(order_req)
                action = "buy"
                order_id = str(order.id)
                order_qty = float(qty)
                print(f"VÉTELI megbízás elküldve | id={order.id} | qty={qty} | price~{price:.2f}")
            else:
                action = "insufficient_budget"
                print(f"A keret ({budget:.2f} USD) kevesebb mint 1 db {SYMBOL} ára ({price:.2f} USD).")
        else:
            action = "no_signal"
            print("Nincs belépési jelzés.")

    write_nvda_status(
        {
            "updated_at": now_utc,
            "mode": "paper" if is_paper else "live",
            "market_open": bool(clock.is_open),
            "bar_timestamp": str(curr.name),
            "close": round(price, 2),
            "ma10": round(ma10, 2),
            "ma30": round(ma30, 2),
            "adx": round(adx, 2),
            "rsi": round(rsi, 2),
            "has_position": has_position,
            "qty_held": qty_held,
            "avg_entry_price": round(avg_entry_price, 2) if has_position else None,
            "action": action,
            "order_id": order_id,
            "order_qty": order_qty,
        }
    )


if __name__ == "__main__":
    main()
