import hashlib

import time

def evaluate_and_execute_cycle(runtime_state, previous_status, now_dt, now_ts):
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
    status_doc = {"updated_at": now_dt.isoformat(), "symbol": SYMBOL, "price": price, "pionex_price": price, "yf_price": indicators.get("yf_price"), "ma10": indicators.get("ma10"), "ma30": indicators.get("ma30"), "adx": indicators.get("adx"), "rsi": indicators.get("rsi"), "free_usdt": free_usdt, "free_crypto": free_crypto, "position_usd": position_usd, "total_usd": total_usd, "has_position": has_position, "status": "TARTAS", "status_detail": "OK", "recent_trades": pionex["recent_trades"]}
    save_json_atomic(STATUS_FILE, status_doc)
    save_json_atomic(STATE_FILE, runtime_state)
    return status_doc

def run_robot_forever():
    st = load_json_safe(STATE_FILE, {"alert_timestamps": {}, "last_git_sync_ts": 0.0})
    while True:
        now_dt, now_ts = utc_now(), time.time()
        try:
            evaluate_and_execute_cycle(st, load_json_safe(STATUS_FILE, {}), now_dt, now_ts)
            sync_git_safe(st, now_ts)
        except Exception as e:
            send_throttled_telegram(st, "loop_err", str(e), now_ts)
        time.sleep(CYCLE_SECONDS)

if __name__ == "__main__":
    run_robot_forever()
