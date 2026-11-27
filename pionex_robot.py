import hashlib
import hmac
import json
import math
import os
import subprocess
import time
import urllib.parse
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

LIVE_TRADING_ENABLED = os.getenv("LIVE_TRADING_ENABLED", "0").strip() in ("1", "true", "yes", "on")
ORDER_AMOUNT_USDT = float(os.getenv("ORDER_AMOUNT_USDT", "15.0"))
MIN_ORDER_USDT = float(os.getenv("MIN_ORDER_USDT", "5.0"))
MIN_POSITION_CRYPTO = float(os.getenv("MIN_POSITION_CRYPTO", "0.01"))
DEFAULT_ADOPT_ENTRY_PRICE = float(os.getenv("DEFAULT_ADOPT_ENTRY_PRICE", "70.03"))

CYCLE_SECONDS = int(os.getenv("CYCLE_SECONDS", "45"))
GIT_SYNC_SECONDS = int(os.getenv("GIT_SYNC_SECONDS", "120"))
ALERT_COOLDOWN_SECONDS = int(os.getenv("ALERT_COOLDOWN_SECONDS", "600"))
BUY_COOLDOWN_SECONDS = int(os.getenv("BUY_COOLDOWN_SECONDS", "900"))
SELL_COOLDOWN_SECONDS = int(os.getenv("SELL_COOLDOWN_SECONDS", "900"))

ADX_MIN = float(os.getenv("ADX_MIN", "20"))
RSI_MAX = float(os.getenv("RSI_MAX", "70"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "5.0"))
TRAILING_START_PCT = float(os.getenv("TRAILING_START_PCT", "1.5"))
TRAILING_CALLBACK_PCT = float(os.getenv("TRAILING_CALLBACK_PCT", "0.8"))

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


def append_history_event(event):
    history = load_json_safe(HISTORY_FILE, [])
    history.append(event)
    save_json_atomic(HISTORY_FILE, history[-200:])


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
    q = (((res.get("indicators") or {}).get("quote") or [{}])[0])
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


def _pionex_signed_request(method, path, params=None, body_obj=None):
    if not PIONEX_API_KEY or not PIONEX_API_SECRET:
        raise RuntimeError("Hiányzik a PIONEX_API_KEY vagy a PIONEX_API_SECRET.")

    ts_ms = str(int(time.time() * 1000))
    q_dict = dict(params or {})
    q_dict["timestamp"] = ts_ms
    sorted_items = sorted((k, str(v)) for k, v in q_dict.items())
    query_str = urllib.parse.urlencode(sorted_items)

    body_str = ""
    if body_obj is not None:
        body_str = json.dumps(body_obj, separators=(",", ":"))

    sign_payload = f"{method.upper()}{path}?{query_str}{body_str}"
    signature = hmac.new(
        PIONEX_API_SECRET.encode("utf-8"),
        sign_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    url = f"{PIONEX_BASE_URL}{path}?{query_str}"
    data_bytes = body_str.encode("utf-8") if body_obj is not None else None
    headers = {
        "PIONEX-KEY": PIONEX_API_KEY,
        "PIONEX-SIGNATURE": signature,
        "User-Agent": "Mozilla/5.0",
    }
    if body_obj is not None:
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method.upper())
    with urllib.request.urlopen(req, timeout=12) as resp:
        return json.loads(resp.read().decode("utf-8"))


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
    payload = _pionex_signed_request("GET", "/api/v1/account/balances")
    balances = ((payload.get("data") or {}).get("balances")) or []
    free_map = {}
    for row in balances:
        coin = str(row.get("coin") or "").upper()
        if coin:
            free_map[coin] = float(row.get("free") or 0.0)
    return free_map


def _collect_order_fills(order_id, expected_size=None, ref_price=None):
    if not order_id or not PIONEX_API_KEY or not PIONEX_API_SECRET:
        return None
    try:
        payload = _pionex_signed_request(
            "GET",
            "/api/v1/trade/fills",
            params={"symbol": PIONEX_SYMBOL, "orderId": str(order_id)},
        )
        fills = ((payload.get("data") or {}).get("fills")) or []
        if not fills:
            return None

        total_qty = 0.0
        total_quote = 0.0
        for f in fills:
            raw_qty = float(f.get("size") or f.get("qty") or 0.0)
            px = float(f.get("price") or ref_price or 0.0)
            raw_quote = float(f.get("quoteAmount") or f.get("amount") or 0.0)

            # Ha a size értéke hibásan quoteAmount nagyságrendű, korrigáljuk
            if expected_size and expected_size > 0 and raw_qty > expected_size * 10 and px > 0:
                qty = raw_qty / px
                quote_amt = raw_qty
            else:
                qty = raw_qty
                quote_amt = raw_quote if raw_quote > 0 else (qty * px)

            if qty > 0:
                total_qty += qty
                total_quote += quote_amt if quote_amt > 0 else (qty * px)

        if total_qty <= 0:
            return None
        avg_price = (total_quote / total_qty) if total_quote > 0 else float(ref_price or 0.0)
        return {
            "order_id": str(order_id),
            "filled_qty": round(total_qty, 6),
            "filled_usdt": round(total_quote, 2),
            "avg_price": round(avg_price, 4),
            "fill_count": len(fills),
        }
    except Exception as exc:
        print(f"[PIONEX] Részleges teljesülések lekérési hiba ({order_id}): {exc}")
        return None


def _place_pionex_order(side, amount_usdt=None, size_crypto=None, price=None):
    client_order_id = f"solbot_{int(time.time() * 1000)}"
    body = {
        "symbol": PIONEX_SYMBOL,
        "side": side.upper(),
        "type": "MARKET",
        "clientOrderId": client_order_id,
    }
    if side.upper() == "BUY":
        body["amount"] = f"{float(amount_usdt):.2f}"
    else:
        body["size"] = f"{float(size_crypto):.6f}"

    if not LIVE_TRADING_ENABLED:
        est_price = float(price or 0.0)
        est_size = (
            round(float(amount_usdt) / est_price, 6)
            if side.upper() == "BUY" and est_price > 0
            else round(float(size_crypto or 0.0), 6)
        )
        est_usdt = (
            round(float(amount_usdt or 0.0), 2)
            if side.upper() == "BUY"
            else round(est_size * est_price, 2)
        )
        return {
            "simulated": True,
            "order_id": f"sim_{client_order_id}",
            "avg_price": est_price,
            "filled_qty": est_size,
            "filled_usdt": est_usdt,
            "fill_count": 1,
        }

    resp = _pionex_signed_request("POST", "/api/v1/trade/order", body_obj=body)
    data = resp.get("data") or {}
    order_id = str(data.get("orderId") or data.get("order_id") or client_order_id)

    time.sleep(0.6)
    merged = _collect_order_fills(order_id, expected_size=size_crypto, ref_price=price)
    if merged:
        return {"simulated": False, **merged}

    fallback_px = float(price or data.get("price") or 0.0)
    fallback_qty = float(size_crypto or 0.0)
    if side.upper() == "BUY" and amount_usdt and fallback_px > 0:
        fallback_qty = round(float(amount_usdt) / fallback_px, 6)
    fallback_usdt = round(fallback_qty * fallback_px, 2)
    return {
        "simulated": False,
        "order_id": order_id,
        "avg_price": round(fallback_px, 4),
        "filled_qty": round(fallback_qty, 6),
        "filled_usdt": fallback_usdt,
        "fill_count": 1,
    }


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
        previous_status.get("next_bet_usdt") or runtime_state.get("next_bet_usdt") or ORDER_AMOUNT_USDT
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
    recent_trades = list(pionex.get("recent_trades") or [])

    has_position = bool(runtime_state.get("has_position", False))
    entry_price = runtime_state.get("entry_price")
    entry_size = runtime_state.get("entry_size")
    entry_cost_usdt = round(float(runtime_state.get("entry_cost_usdt") or 0.0), 2)
    highest_price = runtime_state.get("highest_price")
    trailing_active = bool(runtime_state.get("trailing_active", False))
    stop_loss_price = runtime_state.get("stop_loss_price")

    last_sell_ts = float(runtime_state.get("last_sell_ts") or 0.0)
    last_sold_crypto = float(runtime_state.get("last_sold_crypto") or 0.0)
    sell_cooldown_ok = (now_ts - last_sell_ts) >= SELL_COOLDOWN_SECONDS

    # Megakadályozzuk, hogy eladás után azonnal ugyanazt a maradék egyenleget újra átvegye és eladja
    unchanged_after_sell = (
        last_sold_crypto > 0
        and abs(free_crypto - last_sold_crypto) <= 0.001
        and not sell_cooldown_ok
    )

    if not has_position and free_crypto >= MIN_POSITION_CRYPTO and price > 0 and not unchanged_after_sell:
        adopted_entry = round(float(entry_price or DEFAULT_ADOPT_ENTRY_PRICE), 4)
        adopted_size = round(float(free_crypto), 6)
        has_position = True
        entry_price = adopted_entry
        entry_size = adopted_size
        entry_cost_usdt = round(adopted_size * adopted_entry, 2)
        highest_price = round(max(price, adopted_entry), 4)
        trailing_active = False
        stop_loss_price = round(adopted_entry * (1.0 - STOP_LOSS_PCT / 100.0), 4)
        runtime_state["entry_time"] = runtime_state.get("entry_time") or now_dt.isoformat()
        print(f"[POS] Meglévő {BASE_COIN} átvéve: {adopted_size} @ ${adopted_entry}")
    elif has_position and free_crypto < MIN_POSITION_CRYPTO:
        has_position = False
        entry_price = None
        entry_size = None
        entry_cost_usdt = 0.0
        highest_price = None
        trailing_active = False
        stop_loss_price = None
        runtime_state["entry_time"] = None

    ma10 = indicators.get("ma10")
    ma30 = indicators.get("ma30")
    adx = indicators.get("adx")
    rsi = indicators.get("rsi")

    buy_cooldown_ok = (now_ts - last_sell_ts) >= BUY_COOLDOWN_SECONDS

    # 1. VÉTELI LOGIKA
    if (
        not has_position
        and price > 0
        and buy_cooldown_ok
        and free_usdt >= MIN_ORDER_USDT
        and ma10 is not None
        and ma30 is not None
        and adx is not None
        and rsi is not None
        and float(ma10) > float(ma30)
        and float(adx) >= ADX_MIN
        and float(rsi) <= RSI_MAX
    ):
        spend_usdt = min(ORDER_AMOUNT_USDT, free_usdt) if LIVE_TRADING_ENABLED else ORDER_AMOUNT_USDT
        if spend_usdt >= MIN_ORDER_USDT:
            try:
                res = _place_pionex_order("BUY", amount_usdt=spend_usdt, price=price)
                fill_px = float(res["avg_price"] or price)
                fill_qty = float(res["filled_qty"] or 0.0)
                fill_usdt = float(res["filled_usdt"] or spend_usdt)
                if fill_qty > 0 and fill_px > 0:
                    has_position = True
                    entry_price = round(fill_px, 4)
                    entry_size = round(fill_qty, 6)
                    entry_cost_usdt = round(fill_usdt, 2)
                    highest_price = round(max(price, fill_px), 4)
                    trailing_active = False
                    stop_loss_price = round(entry_price * (1.0 - STOP_LOSS_PCT / 100.0), 4)
                    runtime_state["entry_time"] = now_dt.isoformat()
                    runtime_state["last_sold_crypto"] = 0.0
                    if not LIVE_TRADING_ENABLED:
                        free_crypto = round(free_crypto + fill_qty, 6)
                        free_usdt = round(max(0.0, free_usdt - fill_usdt), 4)
                    trade_rec = {
                        "time": now_dt.isoformat(),
                        "side": "BUY",
                        "price": entry_price,
                        "size": entry_size,
                        "usdt": entry_cost_usdt,
                        "order_id": res["order_id"],
                        "simulated": res["simulated"],
                    }
                    recent_trades = ([trade_rec] + recent_trades)[:30]
                    append_history_event(trade_rec)
                    mode_tag = "[SIM] " if res["simulated"] else ""
                    msg = f"{mode_tag}BUY @ ${entry_price} | {entry_size} {BASE_COIN} (${entry_cost_usdt})"
                    print(f"[TRADE] {msg}")
                    send_throttled_telegram(runtime_state, "trade_buy", msg, now_ts, cooldown_seconds=60)
            except Exception as exc:
                print(f"[TRADE] BUY hiba: {exc}")
                send_throttled_telegram(runtime_state, "trade_buy_err", f"BUY hiba: {exc}", now_ts)

    # 2. ELADÁSI LOGIKA (Stop-Loss / Trailing Stop)
    elif has_position and price > 0 and entry_price and sell_cooldown_ok:
        ep = float(entry_price)
        cur_high = max(float(highest_price or ep), price)
        highest_price = round(cur_high, 4)
        cur_profit_pct = ((price - ep) / ep) * 100.0

        if cur_profit_pct >= TRAILING_START_PCT:
            trailing_active = True

        trailing_stop_px = (
            cur_high * (1.0 - TRAILING_CALLBACK_PCT / 100.0) if trailing_active else None
        )
        sl_px = float(stop_loss_price or (ep * (1.0 - STOP_LOSS_PCT / 100.0)))
        stop_loss_price = round(sl_px, 4)

        sell_reason = None
        if price <= sl_px:
            sell_reason = f"STOP_LOSS ({cur_profit_pct:.2f}%)"
        elif trailing_active and trailing_stop_px is not None and price <= trailing_stop_px:
            sell_reason = f"TRAILING_STOP ({cur_profit_pct:.2f}%)"

        if sell_reason:
            sell_qty = round(min(float(entry_size or free_crypto), free_crypto), 6) if free_crypto > 0 else round(float(entry_size or 0.0), 6)
            if sell_qty >= MIN_POSITION_CRYPTO:
                try:
                    res = _place_pionex_order("SELL", size_crypto=sell_qty, price=price)
                    sell_px = float(res["avg_price"] or price)
                    sold_qty = float(res["filled_qty"] or sell_qty)
                    if sold_qty > free_crypto and free_crypto > 0:
                        sold_qty = free_crypto
                    sold_usdt = round(sold_qty * sell_px, 2)
                    realized_pnl = round((sell_px - ep) * sold_qty, 2)

                    if not LIVE_TRADING_ENABLED:
                        free_crypto = round(max(0.0, free_crypto - sold_qty), 6)
                        free_usdt = round(free_usdt + sold_usdt, 4)

                    has_position = False
                    entry_price = None
                    entry_size = None
                    entry_cost_usdt = 0.0
                    highest_price = None
                    trailing_active = False
                    stop_loss_price = None
                    runtime_state["entry_time"] = None
                    runtime_state["last_sell_ts"] = now_ts
                    runtime_state["last_sold_crypto"] = round(free_crypto, 6)

                    trade_rec = {
                        "time": now_dt.isoformat(),
                        "side": "SELL",
                        "price": round(sell_px, 4),
                        "size": round(sold_qty, 6),
                        "usdt": sold_usdt,
                        "pnl_usdt": realized_pnl,
                        "reason": sell_reason,
                        "order_id": res["order_id"],
                        "simulated": res["simulated"],
                    }
                    recent_trades = ([trade_rec] + recent_trades)[:30]
                    append_history_event(trade_rec)
                    mode_tag = "[SIM] " if res["simulated"] else ""
                    msg = f"{mode_tag}SELL ({sell_reason}) @ ${round(sell_px, 4)} | PnL: ${realized_pnl}"
                    print(f"[TRADE] {msg}")
                    send_throttled_telegram(runtime_state, "trade_sell", msg, now_ts, cooldown_seconds=60)
                except Exception as exc:
                    print(f"[TRADE] SELL hiba: {exc}")
                    send_throttled_telegram(runtime_state, "trade_sell_err", f"SELL hiba: {exc}", now_ts)

    position_usd = round(free_crypto * price, 2)
    total_usd = round(free_usdt + position_usd, 2)

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

    runtime_state["has_position"] = has_position
    runtime_state["entry_price"] = entry_price
    runtime_state["entry_size"] = entry_size
    runtime_state["entry_cost_usdt"] = entry_cost_usdt
    runtime_state["highest_price"] = highest_price
    runtime_state["trailing_active"] = trailing_active
    runtime_state["stop_loss_price"] = stop_loss_price
    runtime_state["last_price"] = price
    runtime_state["free_usdt"] = free_usdt
    runtime_state["free_crypto"] = free_crypto

    status = "TARTAS" if has_position else "VARAKOZAS"
    status_detail = f"{free_crypto:.4f} {BASE_COIN} ({position_usd:.2f} {QUOTE_COIN})"

    status_doc = {
        "updated_at": now_dt.isoformat(),
        "symbol": SYMBOL,
        "base_coin": BASE_COIN,
        "quote_coin": QUOTE_COIN,
        "live_trading_enabled": LIVE_TRADING_ENABLED,
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
        "next_bet_usdt": pionex.get("next_bet_usdt", ORDER_AMOUNT_USDT),
        "position_usd": position_usd,
        "total_usd": total_usd,
        "has_position": has_position,
        "entry_price": entry_price,
        "entry_time": runtime_state.get("entry_time") if has_position else None,
        "entry_size": entry_size,
        "entry_cost_usdt": entry_cost_usdt,
        "highest_price": highest_price,
        "trailing_active": trailing_active,
        "stop_loss_price": stop_loss_price,
        "stop_loss_pct": STOP_LOSS_PCT,
        "profit_pct": profit_pct,
        "profit_usdt": profit_usdt,
        "status": status,
        "status_detail": status_detail,
        "recent_trades": recent_trades,
    }

    save_json_atomic(STATUS_FILE, status_doc)
    save_json_atomic(STATE_FILE, runtime_state)
    return status_doc


def run_robot_forever():
    runtime_state = load_json_safe(
        STATE_FILE,
        {"alert_timestamps": {}, "last_git_sync_ts": 0.0, "last_sell_ts": 0.0, "last_sold_crypto": 0.0},
    )
    print(
        f"[{utc_now().strftime('%Y-%m-%d %H:%M:%S')} UTC] "
        f"Pionex SOL Robot elindult. (LIVE_TRADING_ENABLED={LIVE_TRADING_ENABLED})"
    )
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