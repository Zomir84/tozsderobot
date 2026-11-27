import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

SYMBOL = os.getenv("SYMBOL", "SOLUSDT")
BASE_COIN = os.getenv("BASE_COIN", "SOL")
QUOTE_COIN = os.getenv("QUOTE_COIN", "USDT")
CYCLE_SECONDS = int(os.getenv("CYCLE_SECONDS", "45"))
GIT_SYNC_SECONDS = int(os.getenv("GIT_SYNC_SECONDS", "120"))
ADX_MIN = float(os.getenv("ADX_MIN", "20"))
RSI_MAX = float(os.getenv("RSI_MAX", "70"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "5.0"))

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
        return data if isinstance(data, type(default)) or isinstance(default, dict) else default
    except Exception:
        return default


def save_json_atomic(path, data):
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
    os.replace(tmp, p)


def fetch_pionex_snapshot(runtime_state, previous_status):
    # Ide illeszthető a tényleges Pionex API lekérés; hiba esetén biztonságos tartalékra esik vissza
    price = float(previous_status.get("price") or runtime_state.get("last_price") or 0.0)
    free_usdt = float(previous_status.get("free_usdt") or runtime_state.get("free_usdt") or 0.0)
    free_crypto = float(previous_status.get("free_crypto") or runtime_state.get("free_crypto") or 0.0)
    recent_trades = previous_status.get("recent_trades") if isinstance(previous_status.get("recent_trades"), list) else []
    next_bet_usdt = float(previous_status.get("next_bet_usdt") or runtime_state.get("next_bet_usdt") or 0.0)
    return {
        "price": price,
        "free_usdt": free_usdt,
        "free_crypto": free_crypto,
        "next_bet_usdt": next_bet_usdt,
        "recent_trades": recent_trades,
    }


def fetch_indicators_safe(runtime_state, previous_status, now_ts):
    # Megőrzi a korábbi érvényes indikátorokat, ha épp nincs friss külső mérés
    last_ind = runtime_state.get("last_indicators") or {}
    return {
        "yf_price": previous_status.get("yf_price", last_ind.get("yf_price")),
        "ma5": previous_status.get("ma5", last_ind.get("ma5")),
        "ma15": previous_status.get("ma15", last_ind.get("ma15")),
        "ma10": previous_status.get("ma10", last_ind.get("ma10")),
        "ma30": previous_status.get("ma30", last_ind.get("ma30")),
        "adx": previous_status.get("adx", last_ind.get("adx")),
        "rsi": previous_status.get("rsi", last_ind.get("rsi")),
    }


def sync_git_safe(runtime_state, now_ts):
    last_sync = float(runtime_state.get("last_git_sync_ts") or 0.0)
    if now_ts - last_sync < GIT_SYNC_SECONDS:
        return False

    try:
        subprocess.run(
            ["git", "pull", "--rebase", "origin", "main"],
            cwd=BASE_DIR,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
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
        if not status_proc.stdout.strip():
            runtime_state["last_git_sync_ts"] = now_ts
            return True

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
            ["git", "push", "origin", "main"],
            cwd=BASE_DIR,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        runtime_state["last_git_sync_ts"] = now_ts
        print(f"[GIT] Szinkronizálva az origin/main ággal ({msg}).")
        return True
    except Exception as exc:
        print(f"[GIT] Szinkron figyelmeztetés: {exc}")
        return False


def evaluate_and_execute_cycle(runtime_state, previous_status, now_dt, now_ts):
    pionex = fetch_pionex_snapshot(runtime_state, previous_status)
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

    entry_price = runtime_state.get("entry_price") if has_position else None
    entry_size = runtime_state.get("entry_size") if has_position else None
    raw_entry_cost = runtime_state.get("entry_cost_usdt")
    if has_position:
        entry_cost_usdt = round(float(raw_entry_cost), 2) if raw_entry_cost is not None else position_usd
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
        time.sleep(CYCLE_SECONDS)


if __name__ == "__main__":
    run_robot_forever()