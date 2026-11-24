from datetime import datetime, timedelta, timezone
import pandas as pd
import yfinance as yf
from ta.momentum import RSIIndicator
from ta.trend import ADXIndicator, SMAIndicator


SYMBOL = "NVDA"
YEARS = 2
WARMUP_DAYS = 45
INITIAL_CAPITAL = 100_000.0
ALLOCATION_PCT = 0.25

MA_FAST = 10
MA_SLOW = 30
ADX_WINDOW = 14
RSI_WINDOW = 14

ADX_MIN_ENTRY = 25.0
RSI_MIN_ENTRY = 45.0
RSI_MAX_ENTRY = 68.0
RSI_EXIT = 80.0
STOP_LOSS_PCT = 0.08  # -8.0%


def load_2y_hourly_data(symbol: str, years: int, warmup_days: int) -> pd.DataFrame:
    end_dt = datetime.now(timezone.utc)
    total_days = min(years * 365 + warmup_days, 725)
    start_dt = end_dt - timedelta(days=total_days)

    df = yf.download(
        symbol,
        start=start_dt.strftime("%Y-%m-%d"),
        end=end_dt.strftime("%Y-%m-%d"),
        interval="1h",
        auto_adjust=True,
        progress=False,
    )
    if df.empty:
        raise RuntimeError("Nem sikerült 2 éves órás (1h) adatot letölteni.")

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df.rename(
        columns={
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        }
    )
    idx = pd.to_datetime(df.index)
    if idx.tz is None:
        idx = idx.tz_localize(timezone.utc)
    else:
        idx = idx.tz_convert(timezone.utc)
    df.index = idx
    return df.sort_index()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
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


def run_backtest(df: pd.DataFrame) -> dict:
    cash = INITIAL_CAPITAL
    shares = 0
    entry_price = 0.0
    trades = []
    equity_curve = []

    cutoff_date = datetime.now(timezone.utc) - timedelta(days=min(YEARS * 365, 680))
    window_df = df[df.index >= cutoff_date]
    bh_start_price = float(window_df.iloc[0]["close"]) if not window_df.empty else float(df.iloc[0]["close"])

    for dt, row in df.iterrows():
        price = float(row["close"])
        ma_fast = row["ma_fast"]
        ma_slow = row["ma_slow"]
        adx = row["adx"]
        rsi = row["rsi"]

        if pd.isna(ma_fast) or pd.isna(ma_slow) or pd.isna(adx) or pd.isna(rsi):
            continue

        if dt >= cutoff_date:
            if shares > 0:
                stop_hit = price <= entry_price * (1.0 - STOP_LOSS_PCT)
                exit_signal = (ma_fast < ma_slow) or (rsi >= RSI_EXIT) or stop_hit
                if exit_signal:
                    cash += shares * price
                    pnl = (price - entry_price) * shares
                    pnl_pct = ((price / entry_price) - 1.0) * 100.0
                    trades.append({"pnl": pnl, "pnl_pct": pnl_pct})
                    shares = 0
                    entry_price = 0.0
            else:
                entry_ok = (
                    (ma_fast > ma_slow)
                    and (adx >= ADX_MIN_ENTRY)
                    and (RSI_MIN_ENTRY <= rsi <= RSI_MAX_ENTRY)
                )
                if entry_ok:
                    budget = cash * ALLOCATION_PCT
                    qty = int(budget // price)
                    if qty >= 1:
                        cash -= qty * price
                        shares = qty
                        entry_price = price

            equity_curve.append({"date": dt, "equity": cash + shares * price})

    final_price = float(df.iloc[-1]["close"])
    final_equity = cash + shares * final_price

    eq_df = pd.DataFrame(equity_curve).set_index("date")
    rolling_max = eq_df["equity"].cummax()
    drawdown_pct = ((eq_df["equity"] - rolling_max) / rolling_max) * 100.0
    max_drawdown = float(drawdown_pct.min()) if not drawdown_pct.empty else 0.0

    closed_count = len(trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    win_rate = (wins / closed_count * 100.0) if closed_count > 0 else 0.0
    total_return_pct = ((final_equity / INITIAL_CAPITAL) - 1.0) * 100.0

    bh_shares = int(INITIAL_CAPITAL // bh_start_price)
    bh_final = (INITIAL_CAPITAL - bh_shares * bh_start_price) + bh_shares * final_price
    bh_return_pct = ((bh_final / INITIAL_CAPITAL) - 1.0) * 100.0

    return {
        "initial_capital": INITIAL_CAPITAL,
        "final_equity": final_equity,
        "total_return_pct": total_return_pct,
        "bh_return_pct": bh_return_pct,
        "closed_trades": closed_count,
        "win_rate_pct": win_rate,
        "max_drawdown_pct": max_drawdown,
        "open_shares_at_end": shares,
    }


def main() -> None:
    raw_df = load_2y_hourly_data(SYMBOL, YEARS, WARMUP_DAYS)
    ind_df = add_indicators(raw_df)
    res = run_backtest(ind_df)

    print(f"=== {SYMBOL} ~2 ÉVES VISSZATESZT (1H - STOP -8%, RSI EXIT 80) ===")
    print(f"Kezdő tőke:         {res['initial_capital']:,.2f} USD")
    print(f"Végső tőke:         {res['final_equity']:,.2f} USD")
    print(f"Stratégia hozam:   {res['total_return_pct']:+.2f}%")
    print(f"Buy & Hold hozam:   {res['bh_return_pct']:+.2f}%")
    print(f"Lezárt kötések:     {res['closed_trades']} db")
    print(f"Találati arány:     {res['win_rate_pct']:.1f}%")
    print(f"Max visszaesés:     {res['max_drawdown_pct']:.2f}%")
    print(f"Záráskor nyitott:    {res['open_shares_at_end']} db")


if __name__ == "__main__":
    main()
