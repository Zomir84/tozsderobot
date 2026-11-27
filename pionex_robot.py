import hashlib
import hmac
import json
import math
import os
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

SYMBOL = os.getenv("SYMBOL", "SOLUSDT")
PIONEX_SYMBOL = os.getenv("PIONEX_SYMBOL", "SOL_USDT")
BASE_COIN = os.getenv("BASE_COIN", "SOL")
QUOTE_COIN = os.getenv("QUOTE_COIN", "USDT")
YAHOO_SYMBOL = os.getenv("YAHOO_SYMBOL", "SOL-USD")

PIONEX_BASE_URL = os.getenv("PIONEX_BASE_URL", "https://api.pionex.com").rstrip("/")
PIONEX_API_KEY = os.getenv("PIONEX_API_KEY", "").strip()
PIONEX_API_SECRET = os.getenv("PIONEX_API_SECRET", "").strip()

CYCLE_SECONDS = int(os.getenv("CYCLE_SECONDS", "45"))
GIT_SYNC_SECONDS = int(os.getenv("GIT_SYNC_SECONDS", "120"))
ALERT_COOLDOWN_SECONDS = int(os.getenv("ALERT_COOLDOWN_SECONDS", "600"))

ADX_MIN = float(os.getenv("ADX_MIN", "20"))
RSI_MAX = float(os.getenv("RSI_MAX", "70"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "5.0"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

STATUS_FILE = BASE_DIR / os.getenv("STATUS_FILE", "pionex_status.json")
STATE_FILE = BASE_DIR / os.getenv("STATE_FILE", "robot_runtime_state.json")
HISTORY_FILE = BASE_DIR / os.getenv("HISTORY_FILE", "pionex_history.json")


def utc_now():
    return datetime.now(timezone.utc)


def load_json_safe(path, default):
    p = Path(path)
    if not p.exists():
        return default
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(default, dict):
            return data if isinstance(data, dict) else default
        if isinstance(default, list):
            return data if isinstance(data, list) else default
        return data
    except Exception:
        return default


def save_json_atomic(path, data):
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
    os.replace(tmp, p)


def send_throttled_telegram(runtime_state, alert_key, message, now_ts, cooldown_seconds=ALERT_COOLDOWN_SECONDS):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    alert_map = runtime_state.setdefault("alert_timestamps", {})
    last_sent = float(alert_map.get(alert_key) or 0.0)
    if now_ts - last_sent < cooldown_seconds:
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps(
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": f"[{SYMBOL}] {message}",
        }
    ).encode("utf-8")

    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            ok = resp.status == 200
        if ok:
            alert_map[alert_key] = now_ts
        return ok
    except Exception as exc:
        print(f"[TG] Küldési hiba ({alert_key}): {exc}")
        return False


def _sma(values, period):
    if len(values) < period or period <= 0:
        return None
    return round(sum(values[-period:]) / period, 4)


def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0 if avg_g > 0 else 50.0
    return round(100.0 - (100.0 / (1.0 + (avg_g / avg_l))), 2)


def _adx(highs, lows, closes, period=14):
    n = len(closes)
    if n < period * 2 + 1 or not (len(highs) == len(lows) == n):
        return None
    tr_l, pdm_l, mdm_l = [], [], []
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        pdm_l.append(up if (up > dn and up > 0) else 0.0)
        mdm_l.append(dn if (dn > up and dn > 0) else 0.0)
        tr_l.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )
    atr = sum(tr_l[:period])
    pdm = sum(pdm_l[:period])
    mdm = sum(mdm_l[:period])

    def dx(a, p_v, m_v):
        if a <= 0 or (p_v + m_v) <= 0:
            return 0.0
        pdi = 100.0 * p_v / a
        mdi = 100.0 * m_v / a
        return 100.0 * abs(pdi - mdi) / (pdi + mdi)

    dxs = [dx(atr, pdm, mdm)]
    for i in range(period, len(tr_l)):
        atr = atr - atr / period + tr_l[i]
        pdm = pdm - pdm / period + pdm_l[i]
        mdm = mdm - mdm / period + mdm_l[i]
        dxs.append(dx(atr, pdm, mdm))
    if len(dxs) < period:
        return None
    val = sum(dxs[:period]) / period
    for i in range(period, len(dxs)):
        val = (val * (period - 1) + dxs[i]) / period
    return None if (math.isnan(val) or math.isinf(val)) else round(val, 2)


def _fetch_yahoo_candles(symbol_yf=YAHOO_SYMBOL, range_str="5d", interval_str="15m"):
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol_yf}"
        f"?range={range_str}&interval={interval_str}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    res = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not res:
        return None
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    h_r, l_r, c_r = q.get("high") or [], q.get("low") or [], q.get("close") or []
    highs, lows, closes = [], [], []
    for h, l, c in zip(h_r, l_r, c_r):
        if h is not None and l is not None and c is not None:
            highs.append(float(h))
            lows.append(float(l))
            closes.append(float(c))
    return (highs, lows, closes) if len(closes) >= 35 else None


def fetch_indicators_safe(runtime_state, previous_status, now_ts):
    last_ind = runtime_state.get("last_indicators") or {}
    fb = {
        k: previous_status.get(k, last_ind.get(k))
        for k in ["yf_price", "ma5", "ma15", "ma10", "ma30", "adx", "rsi"]
    }
    try:
        candles = _fetch_yahoo_candles()
        if not candles:
            return fb
        highs, lows, closes = candles
        return {
            "yf_price": round(closes[-1], 4),
            "ma5": _sma(closes, 5) if _sma(closes, 5) is not None else fb["ma5"],
            "ma15": _sma(closes, 15) if _sma(closes, 15) is not None else fb["ma15"],
            "ma10": _sma(closes, 10) if _sma(closes, 10) is not None else fb["ma10"],
            "ma30": _sma(closes, 30) if _sma(closes, 30) is not None else fb["ma30"],
            "adx": _adx(highs, lows, closes, 14) if _adx(highs, lows, closes, 14) is not None else fb["adx"],
            "rsi": _rsi(closes, 14) if _rsi(closes, 14) is not None else fb["rsi"],
        }
    except Exception as exc:
        print(f"[IND] Figyelmeztetés (korábbi értékek megtartva): {exc}")
        send_throttled_telegram(runtime_state, "ind_error", f"Indikátor hiba: {exc}", now_ts)
        return fb


def _fetch_pionex_public_price(symbol_pair=PIONEX_SYMBOL):
    url = f"{PIONEX_BASE_URL}/api/v1/market/tickers?symbol={symbol_pair}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    tickers = ((payload.get("data") or {}).get("tickers")) or []
    for item in tickers:
        if item.get("symbol") == symbol_pair and item.get("close") is not None:
            return round(float(item["close"]), 4)
    return None


def _fetch_pionex_balances():
    if not PIONEX_API_KEY or not PIONEX_API_SECRET:
        return None
    path = "/api/v1/account/balances"
    ts_ms = str(int(time.time() * 1000))
    query = f"timestamp={ts_ms}"
    sign_msg = f"GET{path}?{query}"
    signature = hmac.new(
        PIONEX_API_SECRET.encode("utf-8"),
        sign_msg.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    url = f"{PIONEX_BASE_URL}{path}?{query}"
    req = urllib.request.Request(
        url,
        headers={
            "PIONEX-KEY": PIONEX_API_KEY,
            "PIONEX-SIGNATURE": signature,
            "User-Agent": "Mozilla/5.0",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    balances = ((payload.get("data") or {}).get("balances")) or []
    free_map = {}
    for row in balances:
        coin = str(row.get("coin") or "").upper()
        if coin:
            free_map[coin] = float(row.get("free") or 0.0)
    return free_map


def fetch_pionex_snapshot(runtime_state, previous_status, now_ts):
    price = float(previous_status.get("price") or runtime_state.get("last_price") or 0.0)
    free_usdt = float(previous_status.get("free_usdt") or runtime_state.get("free_usdt") or 0.0)
    free_crypto = float(previous_status.get("free_crypto") or runtime_state.get("free_crypto") or 0.0)

    try:
        live_price = _fetch_pionex_public_price()
        if live_price and live_price > 0:
            price = live_price
    except Exception as exc:
        print(f"[PIONEX] Árfolyam lekérési figyelmeztetés: {exc}")
        send_throttled_telegram(runtime_state, "pionex_price_err", f"Pionex ár hiba: {exc}", now_ts)

    try:
        bal_map = _fetch_pionex_balances()
        if bal_map is not None:
            if QUOTE_COIN.upper() in bal_map:
                free_usdt = round(bal_map[QUOTE_COIN.upper()], 4)
            if BASE_COIN.upper() in bal_map:
                free_crypto = round(bal_map[BASE_COIN.upper()], 6)
    except Exception as exc:
        print(f"[PIONEX] Egyenleg lekérési figyelmeztetés: {exc}")
        send_throttled_telegram(runtime_state, "pionex_bal_err", f"Pionex egyenleg hiba: {exc}", now_ts)

    recent_trades = (
        previous_status.get("recent_trades")
        if isinstance(previous_status.get("recent_trades"), list)
        else []
    )
    next_bet_usdt = float(
        previous_status.get("next_bet_usdt") or runtime_state.get("next_bet_usdt") or 0.0
    )
    return {
        "price": price,
        "free_usdt": free_usdt,
        "free_crypto": free_crypto,
        "next_bet_usdt": next_bet_usdt,
        "recent_trades": recent_trades,
    }


def sync_git_safe(runtime_state, now_ts):
    last_sync = float(runtime_state.get("last_git_sync_ts") or 0.0)
    if now_ts - last_sync < GIT_SYNC_SECONDS:
        return False

    try:
        files_to_add = [str(STATUS_FILE.name)]
        if HISTORY_FILE.exists():
            files_to_add.append(str(HISTORY_FILE.name))

        subprocess.run(
            ["git", "add"] + files_to_add,
            cwd=BASE_DIR,
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )

        status_proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=BASE_DIR,
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if status_proc.stdout.strip():
            msg = f"Pionex robot frissítés - {utc_now().strftime('%H:%M')} UTC [skip ci]"
            subprocess.run(
                ["git", "commit", "-m", msg],
                cwd=BASE_DIR,
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            )

        subprocess.run(
            ["git", "pull", "--rebase", "origin", "main"],
            cwd=BASE_DIR,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=BASE_DIR,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        runtime_state["last_git_sync_ts"] = now_ts
        print("[GIT] Szinkronizálva az origin/main ággal.")
        return True
    except Exception as exc:
        print(f"[GIT] Szinkron figyelmeztetés: {exc}")
        send_throttled_telegram(runtime_state, "git_sync_error", f"Git hiba: {exc}", now_ts)
        return False


def evaluate_and_execute_cycle(runtime_state, previous_status, now_dt, now_ts):
    pionex = fetch_pionex_snapshot(runtime_state, previous_status, now_ts)
    indicators = fetch_indicators_safe(runtime_state, previous_status, now_ts)
    runtime_state["last_indicators"] = indicators

    price = float(pionex["price"])
    free_usdt = float(pionex["free_usdt"])
    free_crypto = float(pionex["free_crypto"])
    position_usd = round(free_crypto * price, 2)
    total_usd = round(free_usdt + position_usd, 2)

    has_position = bool(runtime_state.get("has_position", free_crypto > 0.01))
    runtime_state["has_position"] = has_position
    runtime_state["last_price"] = price
    runtime_state["free_usdt"] = free_usdt
    runtime_state["free_crypto"] = free_crypto

    entry_price = runtime_state.get("entry_price") if has_position else None
    entry_size = runtime_state.get("entry_size") if has_position else None
    raw_entry_cost = runtime_state.get("entry_cost_usdt")
    if has_position:
        entry_cost_usdt = (
            round(float(raw_entry_cost), 2) if raw_entry_cost is not None else position_usd
        )
    else:
        entry_cost_usdt = 0.0

    profit_pct = (
        round(((price - float(entry_price)) / float(entry_price)) * 100.0, 2)
        if has_position and entry_price
        else 0.0
    )
    profit_usdt = (
        round((price - float(entry_price)) * float(entry_size), 2)
        if has_position and entry_price and entry_size
        else 0.0
    )

    status = "TARTAS" if has_position else "VARAKOZAS"
    status_detail = f"{free_crypto:.4f} {BASE_COIN} ({position_usd:.2f} {QUOTE_COIN})"

    status_doc = {
        "updated_at": now_dt.isoformat(),
        "symbol": SYMBOL,
        "base_coin": BASE_COIN,
        "quote_coin": QUOTE_COIN,
        "price": price,
        "pionex_price": price,
        "yf_price": indicators.get("yf_price"),
        "ma5": indicators.get("ma5"),
        "ma15": indicators.get("ma15"),
        "ma10": indicators.get("ma10"),
        "ma30": indicators.get("ma30"),
        "adx": indicators.get("adx"),
        "rsi": indicators.get("rsi"),
        "adx_min": ADX_MIN,
        "rsi_max": RSI_MAX,
        "free_usdt": free_usdt,
        "free_crypto": free_crypto,
        "next_bet_usdt": pionex.get("next_bet_usdt", 0.0),
        "position_usd": position_usd,
        "total_usd": total_usd,
        "has_position": has_position,
        "entry_price": entry_price,
        "entry_time": runtime_state.get("entry_time") if has_position else None,
        "entry_size": entry_size,
        "entry_cost_usdt": entry_cost_usdt,
        "highest_price": runtime_state.get("highest_price") if has_position else None,
        "trailing_active": bool(runtime_state.get("trailing_active", False)),
        "stop_loss_price": runtime_state.get("stop_loss_price") if has_position else None,
        "stop_loss_pct": STOP_LOSS_PCT,
        "profit_pct": profit_pct,
        "profit_usdt": profit_usdt,
        "status": status,
        "status_detail": status_detail,
        "recent_trades": pionex.get("recent_trades", []),
    }

    save_json_atomic(STATUS_FILE, status_doc)
    save_json_atomic(STATE_FILE, runtime_state)
    return status_doc


def run_robot_forever():
    runtime_state = load_json_safe(
        STATE_FILE,
        {"alert_timestamps": {}, "last_git_sync_ts": 0.0},
    )
    print(f"[{utc_now().strftime('%Y-%m-%d %H:%M:%S')} UTC] Pionex SOL Robot elindult.")
    while True:
        now_dt = utc_now()
        now_ts = time.time()
        try:
            previous_status = load_json_safe(STATUS_FILE, {})
            evaluate_and_execute_cycle(runtime_state, previous_status, now_dt, now_ts)
            sync_git_safe(runtime_state, now_ts)
        except Exception as exc:
            print(f"[ERROR] Ciklus hiba: {exc}")
            send_throttled_telegram(runtime_state, "loop_error", f"Ciklus hiba: {exc}", now_ts)
        time.sleep(CYCLE_SECONDS)


if __name__ == "__main__":
    run_robot_forever()