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
TESZT_MOD = True              # True = csak egyenleget és jelzést ellenőriz, NEM kereskedik
YF_SZIMBOLUM = "BTC-USD"      # Yahoo Finance jelölés
PIONEX_SZIMBOLUM = "BTC_USDT" # Pionex pár
ALAP_COIN = "BTC"             # Megvásárolt kriptó
JEGYZESI_COIN = "USDT"        # Amivel fizetsz (USDT vagy USDC)
OSSZEG_USDT = 20              # Éles tét (min. 10-15 USDT)
STOP_LOSS_SZAZALEK = 0.05     # 5%-os veszteségvágó
TIZEDES_PONTOSSAG = 6         # BTC: 6, ETH: 5, SOL: 3
# -------------------

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
print(f"Pionex API kapcsolat OK! Szabad egyenleg: {szabad_usdt:.4f} {JEGYZESI_COIN} | {szabad_kripto:.6f} {ALAP_COIN}")

# 2. Órás gyertyák és MA10/30 számítása
adatok = yf.Ticker(YF_SZIMBOLUM).history(period="5d", interval="1h")
adatok["MA10"] = ta.trend.SMAIndicator(close=adatok["Close"], window=10).sma_indicator()
adatok["MA30"] = ta.trend.SMAIndicator(close=adatok["Close"], window=30).sma_indicator()

ar = float(adatok["Close"].iloc[-1])
ma10 = float(adatok["MA10"].iloc[-1])
ma30 = float(adatok["MA30"].iloc[-1])
print(f"{PIONEX_SZIMBOLUM} ár: {ar:.2f} | MA10 (1h): {ma10:.2f} | MA30 (1h): {ma30:.2f}")

pozicio_ertek_usd = szabad_kripto * ar
van_pozicio = pozicio_ertek_usd >= 10.0

# 3. Döntés
if ma10 > ma30 and not van_pozicio:
    if TESZT_MOD:
        print(f"[TESZT MÓD] Vételi jelzés lenne ({OSSZEG_USDT} {JEGYZESI_COIN}), de nem küldtem megbízást.")
    else:
        print(f"VÉTELI JELZÉS -> {OSSZEG_USDT} {JEGYZESI_COIN} értékű {PIONEX_SZIMBOLUM} vásárlása...")
        eredmeny = pionex_keres("POST", "/api/v1/trade/order", body={
            "symbol": PIONEX_SZIMBOLUM,
            "side": "BUY",
            "type": "MARKET",
            "amount": str(OSSZEG_USDT)
        })
        print(f"Éles vételi megbízás elküldve! ID: {eredmeny.get('orderId')}")

elif van_pozicio:
    veteli_ar = utolso_veteli_ar(PIONEX_SZIMBOLUM)
    eladhato = lefele_kerekit(szabad_kripto, TIZEDES_PONTOSSAG)
    profit_pct = ((ar - veteli_ar) / veteli_ar) * 100 if veteli_ar else 0.0
    stop_loss_aktiv = (ar <= veteli_ar * (1 - STOP_LOSS_SZAZALEK)) if veteli_ar else False

    if stop_loss_aktiv or ma10 < ma30:
        ok = "STOP-LOSS (-5%)" if stop_loss_aktiv else "ELADÁSI JELZÉS (MA10 < MA30)"
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
