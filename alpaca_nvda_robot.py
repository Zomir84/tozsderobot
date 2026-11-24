import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest
from ta.momentum import RSIIndicator
from ta.trend import ADXIndicator, SMAIndicator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("nvda_robot")

SYMBOL = os.environ.get("NVDA_SYMBOL", "NVDA").strip().upper()
STATUS_FILE = Path(os.environ.get("NVDA_STATUS_FILE", "status.json"))

MA_FAST = int(os.environ.get("NVDA_MA_FAST", "10"))
MA_SLOW = int(os.environ.get("NVDA_MA_SLOW", "30"))
ADX_WINDOW = int(os.environ.get("NVDA_ADX_WINDOW", "14"))
RSI_WINDOW = int(os.environ.get("NVDA_RSI_WINDOW", "14"))

ADX_MIN_ENTRY = float(os.environ.get("NVDA_ADX_MIN_ENTRY", "25.0"))
RSI_MIN_ENTRY = float(os.environ.get("NVDA_RSI_MIN_ENTRY", "45.0"))
RSI_MAX_ENTRY = float(os.environ.get("NVDA_RSI_MAX_ENTRY", "68.0"))
RSI_EXIT = float(os.environ.get("NVDA_RSI_EXIT", "75.0"))
STOP_LOSS_PCT = float(os.environ.get("NVDA_STOP_LOSS_PCT", "0.04"))
ALLOCATION_PCT = float(os.environ.get("NVDA_ALLOCATION_PCT", "0.25"))


def load_status_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else {}
    except Exception as exc:
        logger.warning("A %s nem volt olvasható JSON (%s), új blokkal indul.", path, exc)
        return {}


def save_nvda_status(path: Path, payload: dict[str, Any]) -> None:
    data = load_status_json(path)
    data["nvda"] = payload
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    tmp_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(path)


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ma_fast"] = SMAIndicator(close=out["close"], window=MA_FAST).sma_indicator()
    out["ma_slow"] = SMAIndicator(close=out["close"], window=MA_SLOW).sma_indicator()
    out["rsi"] = RSIIndicator(close=out["close"], window=RSI_WINDOW).rsi()
    out["adx"] = ADXIndicator(
        high=out["high"],
        low=out["low"],
        close=out["close"],
        window=ADX_WINDOW,
    ).adx()
    return out


def fetch_hourly_bars(data_client: StockHistoricalDataClient, symbol: str) -> pd.DataFrame:
    start_dt = datetime.now(timezone.utc) - timedelta(days=45)
    request = StockBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=TimeFrame.Hour,
        start=start_dt,
        feed=DataFeed.IEX,
    )
    bars = data_client.get_stock_bars(request)
    df = bars.df

    if df.empty:
        raise RuntimeError(f"Nem érkezett {symbol} gyertyaadat az Alpaca Data API-tól.")

    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(symbol, level="symbol").copy()

    df.index = pd.to_datetime(df.index, utc=True)
    df = df.sort_index()
    return df


def get_position_snapshot(trading_client: TradingClient, symbol: str) -> tuple[bool, float, float | None]:
    try:
        pos = trading_client.get_open_position(symbol)
        qty = float(pos.qty)
        avg_entry = float(pos.avg_entry_price) if pos.avg_entry_price is not None else None
        if qty > 0:
            return True, qty, avg_entry
        return False, 0.0, None
    except Exception:
        return False, 0.0, None


def main() -> None:
    api_key = os.environ.get("APCA_API_KEY_ID", "").strip()
    secret_key = os.environ.get("APCA_API_SECRET_KEY", "").strip()
    base_url = os.environ.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets").strip()
    force_run = os.environ.get("FORCE_RUN", "false").strip().lower() == "true"

    if not api_key or not secret_key:
        raise RuntimeError("Hiányzik az APCA_API_KEY_ID vagy az APCA_API_SECRET_KEY környezeti változó.")

    is_paper = "paper" in base_url.lower()
    trading_client = TradingClient(api_key=api_key, secret_key=secret_key, paper=is_paper)
    data_client = StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)

    now_utc = datetime.now(timezone.utc).isoformat()
    clock = trading_client.get_clock()
    logger.info("Piaci állapot | is_open=%s | timestamp=%s", clock.is_open, clock.timestamp)

    if not clock.is_open and not force_run:
        logger.info("A piac zárva van és FORCE_RUN hamis. Kilépés megbízás nélkül.")
        save_nvda_status(
            STATUS_FILE,
            {
                "updated_at": now_utc,
                "mode": "paper" if is_paper else "live",
                "market_open": bool(clock.is_open),
                "action": "market_closed",
                "message": "A piac jelenleg zárva van.",
            },
        )
        return

    raw_df = fetch_hourly_bars(data_client, SYMBOL)
    ind_df = compute_indicators(raw_df).dropna(subset=["ma_fast", "ma_slow", "adx", "rsi"])
    if ind_df.empty:
        raise RuntimeError("Nincs elegendő gyertya az indikátorok kiszámításához.")

    curr = ind_df.iloc[-1]
    price = float(curr["close"])
    ma10 = float(curr["ma_fast"])
    ma30 = float(curr["ma_slow"])
    adx = float(curr["adx"])
    rsi = float(curr["rsi"])

    logger.info(
        "%s gyertya (%s) | close=%.2f | MA%d=%.2f | MA%d=%.2f | ADX=%.2f | RSI=%.2f",
        SYMBOL,
        curr.name,
        price,
        MA_FAST,
        ma10,
        MA_SLOW,
        ma30,
        adx,
        rsi,
    )

    open_orders = trading_client.get_orders(
        filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[SYMBOL])
    )
    has_position, qty_held, avg_entry = get_position_snapshot(trading_client, SYMBOL)
    logger.info("Pozíció | has_position=%s | qty=%s | avg_entry=%s", has_position, qty_held, avg_entry)

    action = "no_signal"
    order_id: str | None = None
    order_qty = 0.0

    if open_orders:
        action = "open_order_exists"
        logger.info("Nyitott megbízás aktív (%d db) %s-ra; új megbízás nem megy ki.", len(open_orders), SYMBOL)
    elif has_position:
        stop_hit = (avg_entry is not None and avg_entry > 0) and (price <= avg_entry * (1.0 - STOP_LOSS_PCT))
        trend_down = ma10 < ma30
        rsi_overbought = rsi >= RSI_EXIT

        if stop_hit or trend_down or rsi_overbought:
            order = trading_client.submit_order(
                MarketOrderRequest(
                    symbol=SYMBOL,
                    qty=qty_held,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                )
            )
            action = "sell"
            order_id = str(order.id)
            order_qty = qty_held
            logger.info(
                "ELADÁS elküldve | id=%s | qty=%s | stop_hit=%s | trend_down=%s | rsi_exit=%s",
                order.id,
                qty_held,
                stop_hit,
                trend_down,
                rsi_overbought,
            )
        else:
            action = "hold"
            logger.info("Pozíció tartása: kilépési feltétel nem teljesült.")
    else:
        entry_ok = (ma10 > ma30) and (adx >= ADX_MIN_ENTRY) and (RSI_MIN_ENTRY <= rsi <= RSI_MAX_ENTRY)
        if entry_ok:
            account = trading_client.get_account()
            buying_power = float(account.buying_power)
            budget = buying_power * ALLOCATION_PCT
            qty = int(budget // price)

            if qty >= 1:
                order = trading_client.submit_order(
                    MarketOrderRequest(
                        symbol=SYMBOL,
                        qty=qty,
                        side=OrderSide.BUY,
                        time_in_force=TimeInForce.DAY,
                    )
                )
                action = "buy"
                order_id = str(order.id)
                order_qty = float(qty)
                logger.info("VÉTEL elküldve | id=%s | qty=%d | price~%.2f", order.id, qty, price)
            else:
                action = "insufficient_budget"
                logger.info("A keret (%.2f USD) kevesebb mint 1 db %s (%.2f USD).", budget, SYMBOL, price)
        else:
            action = "no_signal"
            logger.info("Nincs belépési jelzés.")

    # Megbízás után frissítjük a pozíció-pillanatképet a status.json-hoz
    has_position_after, qty_held_after, avg_entry_after = get_position_snapshot(trading_client, SYMBOL)

    save_nvda_status(
        STATUS_FILE,
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
            "has_position": has_position_after,
            "qty_held": qty_held_after,
            "avg_entry_price": round(avg_entry_after, 2) if avg_entry_after is not None else None,
            "action": action,
            "order_id": order_id,
            "order_qty": order_qty,
        },
    )


if __name__ == "__main__":
    main()
