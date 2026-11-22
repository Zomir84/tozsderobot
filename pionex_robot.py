import os
import time
import hmac
import json
import math
import hashlib
from datetime import datetime, timezone
import requests
import yfinance as yf
import ta

# --- BEÁLLÍTÁSOK ---
TESZT_MOD = False             # Éles mód (a robot kereskedik)
YF_SZIMBOLUM = "SOL-USD"      # Yahoo Finance jelölés
PIONEX_SZIMBOLUM = "SOL_USDT" # Pionex pár
ALAP_COIN = "SOL"             # Megvásárolt kriptó
JEGYZESI_COIN = "USDT"        # Amivel fizetsz
TET_SZAZALEK = 0.99           # A szabad USDT 99%-át költi egy vételre
MINIMUM_TET_USDT = 11.0       # Pionex minimum megbízás feletti biztonsági határ
STOP_LOSS_SZAZALEK = 0.06     # Alap 6%-os veszteségvágó
TRAILING_AKTIVALAS = 0.04     # +4% nyereség felett kapcsol be a követő Stop-Loss
TRAILING_TAVOLSAG = 0.03      # A csúcsártól 3%-ra követi az árfolyamot
NULLSZALDO_PUFFER = 0.005     # Aktiválás után min. +0.5%-on véd (díjak felett)
TIZEDES_PONTOSSAG = 3         # BTC: 6, ETH: 5, SOL: 3, XRP: 1

# --- SZŰRŐK ---
ADX_MINIMUM = 20              # 20 alatt oldalazás, nem vesz
RSI_MAXIMUM = 70              # 70 felett túlvásárolt, nem vesz
# --------------

API_KEY = os.environ["PIONEX_API_KEY"].strip()
SECRET_KEY = os.environ["PIONEX_SECRET_KEY"].strip()
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
BASE_URL = "https://api.pionex.com"


def telegram_uzenet(szoveg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"TELEGRAM HIBA: Hiányzó adat! Token megvan: {bool(TELEGRAM_TOKEN)} | Chat ID megvan: {bool(TELEGRAM_CHAT_ID)}")
        return
    try:
        valasz = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": szoveg},
            timeout=10
        )
        print(f"Telegram válasz ({valasz.status_code}): {valasz.text}")
    except Exception as hiba:
        print(f"Telegram értesítési hiba: {hiba}")


def pionex_keres(method, path, params=None, body=None):
    params = dict(params or {})
    params["timestamp"] = str(int(time.time() * 1000))
    rendezett = "&".join(f"{k}={params[k]}" for k in sorted(params.keys()))
    path_url = f"{path}?{rendezett}"

    body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""
    uzenet = f"{method.upper()}{path_url}{body_str}"
    alairas = hmac.new(
        SECRET_KEY.encode("utf-8"),
        uzenet.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    headers = {
        "PIONEX-KEY": API_KEY,
        "PIONEX-SIGNATURE": alairas,
        "Content-Type": "application/json"
    }

    url = f"{BASE_URL}{path_url}"
    if method.upper() == "GET":
        valasz = requests.get(url, headers=headers, timeout=15)
    else:
        valasz = requests.post(url, headers=headers, data=body_str, timeout=15)

    adat = valasz.json()
    if not adat.get("result"):
        raise RuntimeError(f"Pionex API hiba: {adat}")
    return adat["data"]


def szabad_egyenleg(coin):
    adat = pionex_keres("GET", "/api/v1/account/balances")
    for b in adat.get("balances", []):
        if b["coin"] == coin:
            return float(b["free"])
    return 0.0


def utolso_kotesek(szimbolum, limit=15):
    adat = pionex_keres("GET", "/api/v1/trade/fills", params={"symbol": szimbolum})
    fills = adat.get("fills", [])
    kotesek = []
    for f in fills[:limit]:
        ar_f = float(f["price"])
        meret_f = float(f["size"])
        ts = f.get("timestamp")
        ido_iso = None
        if ts:
            ts_num = float(ts)
            ts_sec = ts_num / 1000.0 if ts_num > 1e12 else ts_num
            ido_iso = datetime.fromtimestamp(ts_sec, tz=timezone.utc).isoformat()
        kotesek.append({
            "order_id": f.get("orderId"),
            "side": f.get("side"),
            "price": round(ar_f, 2),
            "size": round(meret_f, 6),
            "cost_usdt": round(ar_f * meret_f, 2),
            "time": ido_iso
        })
    return kotesek


def utolso_vetel_adatok(kotesek):
    elso_buy = next((k for k in kotesek if k.get("side") == "BUY"), None)
    if not elso_buy:
        return None
    oid = elso_buy.get("order_id")
    if oid is not None:
        azonos = [k for k in kotesek if k.get("side") == "BUY" and k.get("order_id") == oid]
    else:
        azonos = [elso_buy]
    ossz_meret = sum(k["size"] for k in azonos)
    ossz_usdt = sum(k["price"] * k["size"] for k in azonos)
    atlag_ar = ossz_usdt / ossz_meret if ossz_meret > 0 else elso_buy["price"]
    return {
        "price": round(atlag_ar, 2),
        "size": round(ossz_meret, 6),
        "cost_usdt": round(ossz_usdt, 2),
        "time": elso_buy["time"]
    }


def elozo_allapot_betoltese():
    try:
        with open("status.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def lefele_kerekit(ertek, tizedes):
    szorzo = 10 ** tizedes
    return math.floor(ertek * szorzo) / szorzo


def profit_gorbe_mentese(teljes_usd, aktualis_ar, allapot):
    fajl = "history.json"
    most = datetime.now(timezone.utc)
    try:
        with open(fajl, "r", encoding="utf-8") as f:
            elozmenyek = json.load(f)
            if not isinstance(elozmenyek, list):
                elozmenyek = []
    except Exception:
        elozmenyek = []

    mentes_kell = False
    if not elozmenyek or allapot in ("VÉTEL", "ELADÁS"):
        mentes_kell = True
    else:
        try:
            utolso_ido = datetime.fromisoformat(elozmenyek[-1]["time"])
            if (most - utolso_ido).total_seconds() >= 3300:  # kb. óránként 1 pont
                mentes_kell = True
        except Exception:
            mentes_kell = True

    if mentes_kell:
        elozmenyek.append({
            "time": most.isoformat(),
            "total_usd": round(teljes_usd, 2),
            "price": round(aktualis_ar, 2),
            "status": allapot
        })
        elozmenyek = elozmenyek[-720:]  # utolsó ~30 nap órás adatai
        with open(fajl, "w", encoding="utf-8") as f:
            json.dump(elozmenyek, f, ensure_ascii=False, indent=2)
        print(f"Profit-görbe frissítve ({len(elozmenyek)} pont a history.json-ban).")


# 1. API kapcsolat, szabad egyenlegek és előző állapot betöltése
elozo = elozo_allapot_betoltese()
szabad_usdt = szabad_egyenleg(J
