import hashlib
import hmac
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# ==============================================================================
# 1. BEÁLLÍTÁSOK ÉS KONSTANSOK
# ==============================================================================
BASE_DIR = Path(__file__).resolve().parent
STATUS_FILE = BASE_DIR / "pionex_status.json"
HISTORY_FILE = BASE_DIR / "pionex_history.json"
STATE_FILE = BASE_DIR / "robot_runtime_state.json"

SYMBOL = "SOL_USDT"
YF_SYMBOL = "SOL-USD"
PIONEX_BASE_URL = "https://api.pionex.com"

CYCLE_SECONDS = 45               # Főciklus időköz (45 mp)
SELL_COOLDOWN_SEC = 300          # 5 perc várakozás SELL után új BUY előtt
STOP_LOSS_PCT = 6.0              # Fix Stop-Loss (%)
TRAILING_ACTIVATION_PCT = 1.5    # +1.5% nyereségnél aktiválódik a trailing stop
TRAILING_CALLBACK_PCT = 1.0      # 1.0% visszaesés a csúcsból zárja a pozíciót
ALERT_COOLDOWN_SEC = 1800      # Azonos hibából max. 30 percenként 1 Telegram-üzenet
GIT_SYNC_INTERVAL_SEC = 120    # Git pull --rebase + push legfeljebb 2 percenként
ENABLE_GIT_SYNC = True           # VPS-en vagy teszteléskor igény szerint False-ra állítható
LIVE_TRADING_ENABLED = False     # Csak akkor állítsd True-ra, ha éles Pionex megbízást is küldjön

ADX_MIN = 14.0
RSI_MAX = 72.0
MIN_BUY_USDT = 5.0

PIONEX_API_KEY = os.getenv("PIONEX_API_KEY", "").strip()
PIONEX_API_SECRET = os.getenv("PIONEX_API_SECRET", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso_utc(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def load_json_safe(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json_atomic(path: Path, payload) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)


def http_get_json(url: str, headers: dict | None = None, timeout: int = 12):
    req_headers = {"User-Agent": "Mozilla/5.0 PionexRobot/1.0"}
    if headers:
        req_headers.update(headers)
    req = Request(url, headers=req_headers, method="GET")
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw)


# ==============================================================================
# 2. KERESKEDÉSI LOGIKA: PARTIAL FILL, COOLDOWN, TRAILING STOP
# ==============================================================================
def consolidate_trades(raw_trades: list[dict]) -> list[dict]:
    """Egyesíti az azonos order_id alatti részleges kitöltéseket (partial fills)."""
    grouped: dict[str, dict] = {}

    for idx, trade in enumerate(raw_trades or []):
        raw_oid = trade.get("order_id")
        order_key = str(raw_oid) if raw_oid not in (None, "") else f"noid_{trade.get('time', '')}_{idx}"
        size = float(trade.get("size", 0.0) or 0.0)
        cost_usdt = float(trade.get("cost_usdt", 0.0) or 0.0)
        price = float(trade.get("price", 0.0) or 0.0)

        if cost_usdt <= 0.0 and size > 0.0 and price > 0.0:
            cost_usdt = size * price

        if order_key not in grouped:
            grouped[order_key] = {
                "order_id": raw_oid,
                "side": trade.get("side"),
                "source": trade.get("source", "BOT"),
                "total_size": 0.0,
                "total_cost_usdt": 0.0,
                "fallback_price": price,
                "time": trade.get("time"),
                "fills_count": 0,
            }

        entry = grouped[order_key]
        entry["total_size"] += size
        entry["total_cost_usdt"] += cost_usdt
        entry["fills_count"] += 1

        if trade.get("time") and (not entry["time"] or str(trade["time"]) > str(entry["time"])):
            entry["time"] = trade["time"]

    result: list[dict] = []
    for item in grouped.values():
        avg_price = (
            round(item["total_cost_usdt"] / item["total_size"], 4)
            if item["total_size"] > 0
            else round(item["fallback_price"], 4)
        )
        result.append({
            "order_id": item["order_id"],
            "side": item["side"],
            "source": item["source"],
            "price": avg_price,
            "size": round(item["total_size"], 6),
            "cost_usdt": round(item["total_cost_usdt"], 2),
            "fills_count": item["fills_count"],
            "time": item["time"],
        })

    result.sort(key=lambda x: str(x.get("time", "")), reverse=True)
    return result


def is_in_sell_cooldown(last_sell_iso: str | None, now_dt: datetime) -> tuple[bool, int]:
    last_sell = parse_iso_utc(last_sell_iso)
    if not last_sell:
        return False, 0
    elapsed = int((now_dt - last_sell).total_seconds())
    if elapsed < SELL_COOLDOWN_SEC:
        return True, SELL_COOLDOWN_SEC - elapsed
    return False, 0


def update_position_stops(runtime_state: dict, current_price: float) -> tuple[bool, str]:
    """Frissíti a highest_price és trailing_active állapotot; kilépési jelzést ad."""
    if not runtime_state.get("has_position"):
        runtime_state["trailing_active"] = False
        runtime_state["highest_price"] = None
        return False, ""

    entry_price = float(runtime_state.get("entry_price") or current_price)
    highest_price = float(runtime_state.get("highest_price") or entry_price)
    if current_price > highest_price:
        highest_price = current_price
    runtime_state["highest_price"] = round(highest_price, 4)

    stop_loss_price = float(
        runtime_state.get("stop_loss_price") or round(entry_price * (1.0 - STOP_LOSS_PCT / 100.0), 4)
    )
    runtime_state["stop_loss_price"] = stop_loss_price

    gain_from_entry_pct = ((highest_price - entry_price) / entry_price) * 100.0 if entry_price > 0 else 0.0
    if gain_from_entry_pct >= TRAILING_ACTIVATION_PCT:
        runtime_state["trailing_active"] = True

    trailing_price = None
    if runtime_state.get("trailing_active"):
        trailing_price = round(highest_price * (1.0 - TRAILING_CALLBACK_PCT / 100.0), 4)

    if current_price <= stop_loss_price:
        return True, f"STOP_LOSS ({current_price:.4f} <= {stop_loss_price:.4f})"

    if trailing_price is not None and current_price <= trailing_price:
        return True, f"TRAILING_STOP ({current_price:.4f} <= {trailing_price:.4f}, csúcs: {highest_price:.4f})"

    return False, ""


# ==============================================================================
# 3. HIBAKEZELÉS: TELEGRAM RITKÍTÁS, YAHOO TARTALÉK, GIT PULL --REBASE + PUSH
# ==============================================================================
def send_telegram_now(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    body = urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": message}).encode("utf-8")
    req = Request(url, data=body, method="POST")
    with urlopen(req, timeout=10):
        pass


def send_throttled_telegram(runtime_state: dict, alert_key: str, message: str, now_ts: float) -> None:
    alerts = runtime_state.setdefault("alert_timestamps", {})
    last_sent = float(alerts.get(alert_key, 0.0) or 0.0)
    if now_ts - last_sent >= ALERT_COOLDOWN_SEC:
        alerts[alert_key] = now_ts
        print(f"[TELEGRAM] {message}")
        try:
            send_telegram_now(message)
        except Exception:
            pass


def compute_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 1)


def compute_adx(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float | None:
    n = min(len(highs), len(lows), len(closes))
    if n <= period + 1:
        return None
    trs = []
    plus_dms = []
    minus_dms = []
    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
        plus_dms.append(plus_dm)
        minus_dms.append(minus_dm)

    atr = sum(trs[-period:]) / period
    if atr <= 0:
        return None
    plus_di = 100.0 * (sum(plus_dms[-period:]) / period) / atr
    minus_di = 100.0 * (sum(minus_dms[-period:]) / period) / atr
    di_sum = plus_di + minus_di
    if di_sum <= 0:
        return 0.0
    dx = 100.0 * abs(plus_di - minus_di) / di_sum
    return round(dx, 1)


def fetch_yf_indicators_direct(symbol: str = YF_SYMBOL) -> dict:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=15m&range=5d"
    data = http_get_json(url, timeout=12)
    chart = (data or {}).get("chart", {})
    if chart.get("error"):
        raise RuntimeError(str(chart["error"]))
    results = chart.get("result") or []
    if not results:
        raise ValueError("Üres result lista a Yahoo Finance válaszban")

    res0 = results[0]
    meta = res0.get("meta") or {}
    quote = ((res0.get("indicators") or {}).get("quote") or [{}])[0]
    raw_closes = quote.get("close") or []
    raw_highs = quote.get("high") or []
    raw_lows = quote.get("low") or []

    closes = [float(x) for x in raw_closes if x is not None]
    highs = [float(x) for x in raw_highs if x is not None]
    lows = [float(x) for x in raw_lows if x is not None]

    if len(closes) < 30:
        raise ValueError("Nincs elegendő (legalább 30) záróár a Yahoo Finance adatsorban")

    yf_price = float(meta.get("regularMarketPrice") or closes[-1])
    ma5 = round(sum(closes[-5:]) / 5.0, 2)
    ma15 = round(sum(closes[-15:]) / 15.0, 2)
    ma10 = round(sum(closes[-10:]) / 10.0, 2)
    ma30 = round(sum(closes[-30:]) / 30.0, 2)
    rsi = compute_rsi(closes, 14)
    adx = compute_adx(highs, lows, closes, 14)

    return {
        "yf_price": round(yf_price, 2),
        "ma5": ma5,
        "ma15": ma15,
        "ma10": ma10,
        "ma30": ma30,
        "adx": adx,
        "rsi": rsi,
    }


def fetch_indicators_safe(runtime_state: dict, now_ts: float) -> dict:
    """Yahoo Finance (SOL-USD) indikátorok lekérése tartalékállapottal."""
    try:
        indicators = fetch_yf_indicators_direct(YF_SYMBOL)
        if not indicators or indicators.get("ma10") is None or indicators.get("ma30") is None:
            raise ValueError("Hiányos Yahoo Finance indikátor adatsor")
        runtime_state["last_valid_indicators"] = indicators
        return indicators
    except Exception as exc:
        send_throttled_telegram(
            runtime_state,
            alert_key="yf_sol_usd_error",
            message=f"⚠️ Yahoo Finance ({YF_SYMBOL}) hiba, tartalék indikátorok aktívak: {exc}",
            now_ts=now_ts,
        )
        fallback = runtime_state.get("last_valid_indicators")
        if fallback:
            return fallback
        return {
            "yf_price": None,
            "ma5": None,
            "ma15": None,
            "ma10": None,
            "ma30": None,
            "adx": None,
            "rsi": None,
        }


def sync_git_safe(runtime_state: dict, now_ts: float) -> None:
    """
    Nem blokkoló Git szinkronizálás: minden push előtt futtatja a
    `git pull --rebase origin main` parancsot, ütközés esetén tiszta
    visszaállítással és újrapróbálkozással.
    """
    if not ENABLE_GIT_SYNC:
        return

    last_sync = float(runtime_state.get("last_git_sync_ts", 0.0) or 0.0)
    if now_ts - last_sync < GIT_SYNC_INTERVAL_SEC:
        return

    runtime_state["last_git_sync_ts"] = now_ts
    target_files = [STATUS_FILE.name, HISTORY_FILE.name]

    def run_git(args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=25,
        )

    def pull_rebase_with_conflict_recovery() -> None:
        pull_res = run_git(["pull", "--rebase", "origin", "main"])
        if pull_res.returncode == 0:
            return

        # Ha félbehagyott rebase maradt fenn, megszakítjuk, majd tiszta munkakönyvtárral húzunk be
        run_git(["rebase", "--abort"])
        run_git(["checkout", "--", *target_files])

        pull_retry = run_git(["pull", "--rebase", "origin", "main"])
        if pull_retry.returncode == 0:
            return

        # Ha továbbra is konfliktus van a JSON fájlokon, a legfrissebb állapotot megtartva továbblépünk
        run_git(["checkout", "--theirs", *target_files])
        run_git(["add", *target_files])
        rebase_cont = run_git(["rebase", "--continue"])
        if rebase_cont.returncode != 0:
            run_git(["rebase", "--abort"])
            run_git(["fetch", "origin", "main"])
            reset_res = run_git(["reset", "--hard", "origin/main"])
            if reset_res.returncode != 0:
                raise RuntimeError(f"Git ütközéskezelés sikertelen: {pull_retry.stderr.strip()}")

    try:
        # 1. Meglévő (akár korábban sikertelen push miatt beragadt) helyi commitok rebase-elése
        pull_rebase_with_conflict_recovery()

        # 2. Friss JSON állapotok stage-elése és commitolása, ha van változás
        run_git(["add", *target_files])
        commit_msg = f"Pionex robot frissítés - {utc_now().strftime('%H:%M')} UTC [skip ci]"
        commit_res = run_git(["commit", "-m", commit_msg])
        combined_commit_out = (commit_res.stdout + commit_res.stderr).lower()
        has_new_commit = not (
            commit_res.returncode != 0 and "nothing to commit" in combined_commit_out
        )

        # 3. Push előtt ismét frissítjük a távoli origin/main változásait
        pull_rebase_with_conflict_recovery()

        # 4. Push (ha van új helyi commit vagy előrehaladott helyi ág)
        push_res = run_git(["push", "origin", "main"])
        if push_res.returncode != 0:
            # Végső mentőöv: még egy pull --rebase + push próba ütközéskezeléssel
            pull_rebase_with_conflict_recovery()
            push_retry = run_git(["push", "origin", "main"])
            if push_retry.returncode != 0:
                raise RuntimeError(f"git push origin main sikertelen: {push_retry.stderr.strip()}")

        if has_new_commit:
            print(f"[GIT] Szinkronizálva az origin/main ággal ({commit_msg}).")

    except Exception as exc:
        send_throttled_telegram(
            runtime_state,
            alert_key="git_sync_error",
            message=f"⚠️ Git szinkron hiba (a robot tovább fut): {exc}",
            now_ts=now_ts,
        )


# ==============================================================================
# 4. PIONEX ADATLEKÉRÉS ÉS FŐCIKLUSSZERVEZÉS
# ==============================================================================
def pionex_signed_get(path: str, params: dict | None = None) -> dict:
    query = urlencode(sorted((params or {}).items()))
    full_path = f"{path}?{query}" if query else path
    sign_target = f"GET{full_path}"
    sig = hmac.new(
        PIONEX_API_SECRET.encode("utf-8"),
        sign_target.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    headers = {
        "PIONEX-KEY": PIONEX_API_KEY,
        "PIONEX-SIGNATURE": sig,
    }
    return http_get_json(f"{PIONEX_BASE_URL}{full_path}", headers=headers, timeout=12)


def fetch_pionex_ticker_price(symbol: str = SYMBOL) -> float | None:
    try:
        data = http_get_json(f"{PIONEX_BASE_URL}/api/v1/market/tickers?symbol={symbol}", timeout=10)
        tickers = ((data or {}).get("data") or {}).get("tickers") or []
        if tickers and tickers[0].get("close") is not None:
            return float(tickers[0]["close"])
    except Exception:
        pass
    return None


def fetch_pionex_snapshot(runtime_state: dict, previous_status: dict) -> dict:
    live_price = fetch_pionex_ticker_price(SYMBOL)
    price = float(
        live_price
        or previous_status.get("pionex_price")
        or previous_status.get("price")
        or runtime_state.get("last_price")
        or 70.52
    )

    free_usdt = float(previous_status.get("free_usdt") if previous_status.get("free_usdt") is not None else 0.5193)
    free_crypto = float(
        previous_status.get("free_crypto") if previous_status.get("free_crypto") is not None else 0.734455
    )
    raw_trades = previous_status.get("recent_trades") or runtime_state.get("raw_trades_cache") or []

    if PIONEX_API_KEY and PIONEX_API_SECRET:
        try:
            bal_resp = pionex_signed_get("/api/v1/account/balances")
            balances = ((bal_resp or {}).get("data") or {}).get("balances") or []
            for item in balances:
                coin = str(item.get("coin", "")).upper()
                free_val = float(item.get("free", 0.0) or 0.0)
                if coin == "USDT":
                    free_usdt = free_val
                elif coin == "SOL":
                    free_crypto = free_val

            fills_resp = pionex_signed_get("/api/v1/trade/fills", {"symbol": SYMBOL, "limit": 50})
            fills = ((fills_resp or {}).get("data") or {}).get("fills") or []
            if fills:
                normalized = []
                for f in fills:
                    ts_ms = f.get("timestamp")
                    iso_time = (
                        datetime.fromtimestamp(float(ts_ms) / 1000.0, tz=timezone.utc).isoformat()
                        if ts_ms is not None
                        else utc_now().isoformat()
                    )
                    normalized.append({
                        "order_id": f.get("orderId"),
                        "side": f.get("side"),
                        "source": "BOT",
                        "price": float(f.get("price", 0.0) or 0.0),
                        "size": float(f.get("size", 0.0) or 0.0),
                        "cost_usdt": float(f.get("quoteQty", 0.0) or 0.0),
                        "time": iso_time,
                    })
                raw_trades = normalized
                runtime_state["raw_trades_cache"] = normalized
        except Exception:
            pass

    consolidated = consolidate_trades(raw_trades)
    next_bet_usdt = round(max(0.0, free_usdt - 0.0093), 2) if free_usdt > 0.5 else round(free_usdt, 2)

    return {
        "price": price,
        "free_usdt": round(free_usdt, 4),
        "free_crypto": round(free_crypto, 6),
        "next_bet_usdt": next_bet_usdt,
        "recent_trades": consolidated[:15],
    }


def evaluate_and_execute_cycle(runtime_state: dict, previous_status: dict, now_dt: datetime, now_ts: float) -> dict:
    pionex = fetch_pionex_snapshot(runtime_state, previous_status)
    indicators = fetch_indicators_safe(runtime_state, now_ts)

    price = float(pionex["price"])
    runtime_state["last_price"] = price

    free_usdt = float(pionex["free_usdt"])
    free_crypto = float(pionex["free_crypto"])
    position_usd = round(free_crypto * price, 2)
    total_usd = round(free_usdt + position_usd, 2)

    has_position = bool(runtime_state.get("has_position", free_crypto > 0.01))
runtime_state["has_position"] = has_position
    ma10, ma30, adx, rsi = indicators.get("ma10"), indicators.get("ma30"), indicators.get("adx"), indicators.get("rsi")
    status, status_detail = "VÁRAKOZÁS", ""

    if has_position:
        entry_price = float(runtime_state.get("entry_price") or price)
        should_sell, reason = update_position_stops(runtime_state, price)
        trend_exit = bool(ma10 is not None and ma30 is not None and float(ma10) < float(ma30) and rsi is not None and float(rsi) > RSI_MAX)
        if should_sell or trend_exit:
            runtime_state.update({"has_position": False, "trailing_active": False, "highest_price": None, "last_sell_time": now_dt.isoformat()})
            has_position = False
            status, status_detail = "ELADÁS", f"Eladva @ ${price:.2f} | Ok: {reason or 'TREND_EXIT'}"
        else:
            profit_pct = round(((price - entry_price) / entry_price) * 100.0, 2) if entry_price > 0 else 0.0
            status, status_detail = "TARTÁS", f"{free_crypto:.2f} SOL (~{position_usd:.2f} USD) | Eredmény: {profit_pct:+.2f}% | SL: ${runtime_state.get('stop_loss_price')}"
    else:
        in_cooldown, rem_sec = is_in_sell_cooldown(runtime_state.get("last_sell_time"), now_dt)
        buy_signal = bool(not in_cooldown and ma10 is not None and ma30 is not None and adx is not None and rsi is not None and float(ma10) > float(ma30) and float(adx) >= ADX_MIN and float(rsi) <= RSI_MAX and free_usdt >= MIN_BUY_USDT)
        if buy_signal:
            entry_size = round(free_usdt / price, 6) if price > 0 else 0.0
            sl_val = round(price * (1.0 - STOP_LOSS_PCT / 100.0), 4)
            runtime_state.update({"has_position": True, "entry_price": price, "entry_time": now_dt.isoformat(), "entry_size": entry_size, "entry_cost_usdt": round(free_usdt, 2), "highest_price": price, "trailing_active": False, "stop_loss_price": sl_val})
            has_position = True
            status, status_detail = "TARTÁS", f"Belépés @ ${price:.2f} | SL: ${sl_val}"
        elif in_cooldown:
            status, status_detail = "VÁRAKOZÁS", f"SELL utáni cooldown aktív ({rem_sec} mp hátra)"
        else:
            status, status_detail = "VÁRAKOZÁS", f"Ár: ${price:.2f} | MA10: {ma10} / MA30: {ma30} | ADX: {adx}"

    entry_price_val = runtime_state.get("entry_price") if has_position else None
    entry_size_val = runtime_state.get("entry_size") if has_position else None
    entry_cost_val = runtime_state.get("entry_cost_usdt") if has_position else None
    profit_pct_val = round(((price - float(entry_price_val)) / float(entry_price_val)) * 100.0, 2) if has_position and entry_price_val else 0.0
    profit_usdt_val = round((price - float(entry_price_val)) * float(entry_size_val), 2) if has_position and entry_price_val and entry_size_val else 0.0

    status_doc = {
        "updated_at": now_dt.isoformat(), "symbol": SYMBOL, "base_coin": "SOL", "quote_coin": "USDT",
        "price": price, "pionex_price": price, "yf_price": indicators.get("yf_price"),
        "ma5": indicators.get("ma5"), "ma15": indicators.get("ma15"), "ma10": ma10, "ma30": ma30,
        "adx": adx, "rsi": rsi, "free_usdt": free_usdt, "free_crypto": free_crypto,
        "next_bet_usdt": pionex["next_bet_usdt"], "position_usd": position_usd, "total_usd": total_usd,
        "has_position": has_position, "entry_price": entry_price_val,
        "entry_time": runtime_state.get("entry_time") if has_position else None,
        "entry_size": entry_size_val, "entry_cost_usdt": entry_cost_val,
        "highest_price": runtime_state.get("highest_price") if has_position else None,
        "trailing_active": bool(runtime_state.get("trailing_active", False)),
        "stop_loss_price": runtime_state.get("stop_loss_price") if has_position else None,
        "stop_loss_pct": STOP_LOSS_PCT, "profit_pct": profit_pct_val, "profit_usdt": profit_usdt_val,
        "status": status, "status_detail": status_detail, "adx_min": ADX_MIN, "rsi_max": RSI_MAX,
        "manual_buys": [], "recent_trades": pionex["recent_trades"],
    }
    save_json_atomic(STATUS_FILE, status_doc)

    history = load_json_safe(HISTORY_FILE, [])
    if not isinstance(history, list):
        history = []
    last_entry = history[-1] if history else None
    last_dt = parse_iso_utc(last_entry.get("time")) if last_entry else None
    if not (last_entry and last_entry.get("status") == status and last_dt and (now_dt - last_dt).total_seconds() < 3600):
        history.append({"time": now_dt.isoformat(), "total_usd": total_usd, "price": price, "status": status})
        save_json_atomic(HISTORY_FILE, history[-2000:])

    save_json_atomic(STATE_FILE, runtime_state)
    return status_doc


def run_robot_forever() -> None:
    initial_status = load_json_safe(STATUS_FILE, {})
    runtime_state = load_json_safe(STATE_FILE, {
        "has_position": bool(initial_status.get("has_position", True)),
        "entry_price": initial_status.get("entry_price", 70.03),
        "entry_time": initial_status.get("entry_time", "2026-11-26T15:09:31.117000+00:00"),
        "entry_size": initial_status.get("entry_size", 0.727974),
        "entry_cost_usdt": initial_status.get("entry_cost_usdt", 50.98),
        "highest_price": initial_status.get("highest_price", 70.52),
        "trailing_active": bool(initial_status.get("trailing_active", False)),
        "stop_loss_price": initial_status.get("stop_loss_price", 65.83),
        "last_sell_time": "2026-11-26T15:09:12.670000+00:00",
        "alert_timestamps": {}, "last_git_sync_ts": 0.0,
    })
    while True:
        now_dt, now_ts = utc_now(), time.time()
        previous_status = load_json_safe(STATUS_FILE, {})
        try:
            evaluate_and_execute_cycle(runtime_state, previous_status, now_dt, now_ts)
            sync_git_safe(runtime_state, now_ts)
        except Exception as exc:
            send_throttled_telegram(runtime_state, "main_loop_error", f"⚠️ Robot ciklus hiba: {exc}", now_ts)
        time.sleep(CYCLE_SECONDS)

if __name__ == "__main__":
    run_robot_forever()
