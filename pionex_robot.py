import os
import time
import hmac
import json
import math
import hashlib
import requests
import yfinance as yf
import ta

# --- BEÁLLÍTÁSOK ---
TESZT_MOD = False              # True = csak ellenőriz, False = éles kereskedés
YF_SZIMBOLUM = "SOL-USD"      # Yahoo Finance jelölés
PIONEX_SZIMBOLUM = "SOL_USDT" # Pionex pár
ALAP_COIN = "SOL"             # Megvásárolt kriptó
JEGYZESI_COIN = "USDT"        # Amivel fizetsz
TET_SZAZALEK = 0.50           # A szabad USDT 50%-át költi egy vételre
MINIMUM_TET_USDT = 11.0       # Pionex minimum megbízás feletti biztonsági határ
STOP_LOSS_SZAZALEK = 0.06     # 6%-os veszteségvágó (SOL-hoz ideális)
TIZEDES_PONTOSSAG = 3         # BTC: 6, ETH: 5, SOL: 3, XRP: 1

# --- SZŰRŐK (oldalazás és csúcson vétel ellen) ---
ADX_MINIMUM = 20              # 20 alatt oldalazásnak tekintjük, nem vesz
RSI_MAXIMUM = 70              # 70 felett túlvásárolt, nem vesz
# -------------------------------------------------

API_KEY = os.environ["PIONEX_API_KEY"].strip()
SECRET_KEY = os.environ["PIONEX_SECRET_KEY"].strip()
BASE_URL = "https://api.pionex.com"


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


def utolso_veteli_ar(szimbolum):
    adat = pionex_keres("GET", "/api/v1/trade/fills", params={"symbol": szimbolum})
    for f in adat.get("fills", []):
        if f.get("side") == "BUY":
            return float(f["price"])
    return None


def lefele_kerekit(ertek, tizedes):
    szorzo = 10 ** tizedes
    return math.floor(ertek * szorzo) / szorzo


# 1. API kapcsolat és szabad egyenlegek ellenőrzése
szabad_usdt = szabad_egyenleg(JEGYZESI_COIN)
szabad_kripto = szabad_egyenleg(ALAP_COIN)
veteli_osszeg = round(szabad_usdt * TET_SZAZALEK, 2)

print(
    f"Pionex API kapcsolat OK! Szabad egyenleg: {szabad_usdt:.4f} {JEGYZESI_COIN} "
    f"(50% tét = {veteli_osszeg:.2f} {JEGYZESI_COIN}) | {szabad_kripto:.6f} {ALAP_COIN}"
)

# 2. Órás gyertyák és indikátorok
adatok = yf.Ticker(YF_SZIMBOLUM).history(period="10d", interval="1h")
adatok["MA10"] = ta.trend.SMAIndicator(close=adatok["Close"], window=10).sma_indicator()
adatok["MA30"] = ta.trend.SMAIndicator(close=adatok["Close"], window=30).sma_indicator()
adatok["ADX"] = ta.trend.ADXIndicator(
    high=adatok["High"], low=adatok["Low"], close=adatok["Close"], window=14
).adx()
adatok["RSI"] = ta.momentum.RSIIndicator(close=adatok["Close"], window=14).rsi()

ar = float(adatok["Close"].iloc[-1])
ma10 = float(adatok["MA10"].iloc[-1])
ma30 = float(adatok["MA30"].iloc[-1])
adx = float(adatok["ADX"].iloc[-1])
rsi = float(adatok["RSI"].iloc[-1])

print(f"{PIONEX_SZIMBOLUM} ár: {ar:.2f} | MA10: {ma10:.2f} | MA30: {ma30:.2f} | ADX: {adx:.1f} | RSI: {rsi:.1f}")

pozicio_ertek_usd = szabad_kripto * ar
van_pozicio = pozicio_ertek_usd >= 10.0

# 3. Döntés
if ma10 > ma30 and not van_pozicio:
    if adx < ADX_MINIMUM:
        print(f"SZŰRŐ BLOKKOLTA -> Gyenge trend / oldalazás (ADX: {adx:.1f}, min. {ADX_MINIMUM} kell).")
    elif rsi > RSI_MAXIMUM:
        print(f"SZŰRŐ BLOKKOLTA -> Túlvásárolt piac (RSI: {rsi:.1f}, max. {RSI_MAXIMUM} lehet).")
    elif veteli_osszeg < MINIMUM_TET_USDT:
        print(
            f"KEVÉS EGYENLEG -> A szabad {JEGYZESI_COIN} 50%-a ({veteli_osszeg:.2f}) "
            f"kevesebb, mint a {MINIMUM_TET_USDT:.0f} {JEGYZESI_COIN} minimum tét."
        )
    elif TESZT_MOD:
        print(f"[TESZT MÓD] Minden feltétel teljesült! Vételi jelzés lenne ({veteli_osszeg:.2f} {JEGYZESI_COIN}).")
    else:
        print(f"VÉTELI JELZÉS -> {veteli_osszeg:.2f} {JEGYZESI_COIN} értékű {PIONEX_SZIMBOLUM} vásárlása...")
        eredmeny = pionex_keres("POST", "/api/v1/trade/order", body={
            "symbol": PIONEX_SZIMBOLUM,
            "side": "BUY",
            "type": "MARKET",
            "amount": f"{veteli_osszeg:.2f}"
        })
        print(f"Éles vételi megbízás elküldve! ID: {eredmeny.get('orderId')}")

elif van_pozicio:
    veteli_ar = utolso_veteli_ar(PIONEX_SZIMBOLUM)
    eladhato = lefele_kerekit(szabad_kripto, TIZEDES_PONTOSSAG)
    profit_pct = ((ar - veteli_ar) / veteli_ar) * 100 if veteli_ar else 0.0
    stop_loss_aktiv = (ar <= veteli_ar * (1 - STOP_LOSS_SZAZALEK)) if veteli_ar else False

    if stop_loss_aktiv or ma10 < ma30:
        ok = f"STOP-LOSS (-{int(STOP_LOSS_SZAZALEK*100)}%)" if stop_loss_aktiv else "ELADÁSI JELZÉS (MA10 < MA30)"
        if TESZT_MOD:
            print(f"[TESZT MÓD] {ok} -> {eladhato} {ALAP_COIN} eladása lenne ({profit_pct:+.2f}%).")
        else:
            print(f"{ok} -> {eladhato} {ALAP_COIN} eladása ({profit_pct:+.2f}%)...")
            pionex_keres("POST", "/api/v1/trade/order", body={
                "symbol": PIONEX_SZIMBOLUM,
                "side": "SELL",
                "type": "MARKET",
                "size": f"{eladhato:.{TIZEDES_PONTOSSAG}f}"
            })
            print("Pozíció lezárva!")
    else:
        print(f"TARTÁS -> {eladhato} {ALAP_COIN} (~{pozicio_ertek_usd:.2f} USD) | Eredmény: {profit_pct:+.2f}%")
else:
    print("VÁRAKOZÁS -> Nincs nyitott pozíció és nincs vételi jelzés.")
