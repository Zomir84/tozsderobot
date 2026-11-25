import os
import sys
import time
import hmac
import json
import math
import hashlib
import subprocess
from datetime import datetime, timezone
import requests
import yfinance as yf
import ta

# --- FUTTATÁSI ÉS TRENDKÖVETŐ BEÁLLÍTÁSOK ---
TESZT_MOD = False                       # Éles mód (a robot kereskedik)
FOLYAMATOS_MOD = False                   # True: folyamatos hurok a laptopon | False: egyetlen futás
CIKLUSSZUNET_MP = 45                    # Hány másodpercenként ellenőrizzen a folyamatos hurokban
GITHUB_PUSH_MOD = False                 # True: frissíti a status.json/history.json fájlokat GitHubon
GITHUB_PUSH_INTERVAL_MP = 300           # Állapotváltozás nélkül hány mp-ként pusholjon (ha engedélyezve van)
HIBA_ERTESITES_SZUNET_MP = 900          # Hiba esetén legalább 15 percenként küld Telegram hibát (ne spammeljen)

YF_SZIMBOLUM = "SOL-USD"                # Yahoo Finance jelölés
PIONEX_SZIMBOLUM = "SOL_USDT"           # Pionex pár
ALAP_COIN = "SOL"                       # Megvásárolt kriptó
JEGYZESI_COIN = "USDT"                  # Amivel fizetsz
TET_SZAZALEK = 0.99                     # A szabad USDT 99%-át költi egy vételre
MINIMUM_TET_USDT = 11.0                 # Pionex minimum megbízás feletti biztonsági határ
STOP_LOSS_SZAZALEK = 0.06               # Alap 6%-os veszteségvágó
LEPCSO_SZAZALEK = 0.04                  # Minden további +4% új lépcső
VISSZAHUZAS_SZAZALEK = 0.015            # +4% elérése után a csúcsárból -1.5% visszahúzódásnál zár
KORAI_PROFIT_VEDELEM_KUSZOB_PCT = 2.0   # +2.0% nyereségnél aktiválja a korai profitvédő visszahúzást
KORAI_PROFIT_VISSZAHUZAS_PCT = 1.2     # +2.0%..+4.0% között a csúcsból -1.2% esésnél zár
MA_ELADAS_MIN_PROFIT_PCT = 0.5          # MA10 < MA30 legalább +0.5% nyereségnél zár
KORAI_BELEPES = True                    # Gyorsabb trendkövetés induló emelkedésnél
ALAP_TIZEDES_PONTOSSAG = 2              # Tartalék tizedespontosság, ha az API-lekérés nem elérhető

# --- SZŰRŐK ---
ADX_MINIMUM = 14                        # 14 alatt gyenge/oldalazó mozgás
RSI_MAXIMUM = 72                        # 72 felett túlvásárolt
KORAI_RSI_MA5_MIN = 38.0                # MA5 > MA15 korai belépéshez minimum RSI
KORAI_RSI_MA10_MIN = 44.0               # Emelkedő MA10 korai belépéshez minimum RSI
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


def pionex_aktualis_ar(szimbolum, tartalek_ar):
    try:
        valasz = requests.get(
            f"{BASE_URL}/api/v1/market/tickers",
            params={"symbol": szimbolum, "type": "SPOT"},
            timeout=10
        )
        adat = valasz.json()
        if adat.get("result"):
            tickers = adat.get("data", {}).get("tickers", [])
            for t in tickers:
                if t.get("symbol") == szimbolum:
                    for kulcs in ("close", "price", "lastPrice"):
                        if t.get(kulcs) is not None:
                            ar_val = float(t[kulcs])
                            if ar_val > 0:
                                return ar_val
    except Exception as hiba:
        print(f"Figyelmeztetés: nem sikerült lekérni a {szimbolum} élő Pionex árát ({hiba}), tartalék ár: {tartalek_ar:.2f}")
    return float(tartalek_ar)


def lekerdezd_mennyiseg_pontossag(szimbolum, alap_pontossag=ALAP_TIZEDES_PONTOSSAG):
    try:
        adat = pionex_keres("GET", "/api/v1/common/symbols", params={"symbols": szimbolum})
        for s in adat.get("symbols", []):
            if s.get("symbol") == szimbolum and s.get("basePrecision") is not None:
                pontossag = int(s["basePrecision"])
                if pontossag >= 0:
                    return pontossag
    except Exception as hiba:
        print(f"Figyelmeztetés: nem sikerült lekérni a {szimbolum} pontosságát ({hiba}), tartalék érték: {alap_pontossag}")
    return alap_pontossag


def szabad_egyenleg(coin):
    adat = pionex_keres("GET", "/api/v1/account/balances")
    for b in adat.get("balances", []):
        if b["coin"] == coin:
            return float(b["free"])
    return 0.0


def utolso_kotesek(szimbolum, manual_order_ids=None, limit=15):
    manual_ids = set(str(x) for x in (manual_order_ids or []))
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
        oid = f.get("orderId")
        kotesek.append({
            "order_id": oid,
            "side": f.get("side"),
            "source": "MANUAL" if oid is not None and str(oid) in manual_ids else "BOT",
            "price": round(ar_f, 2),
            "size": round(meret_f, 6),
            "cost_usdt": round(ar_f * meret_f, 2),
            "time": ido_iso
        })
    return kotesek


def utolso_vetel_adatok(kotesek, manual_buys=None):
    elso_buy = next((k for k in kotesek if k.get("side") == "BUY"), None)
    if not elso_buy and not manual_buys:
        return None

    if elso_buy:
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

    utolso_man = manual_buys[-1]
    return {
        "price": round(float(utolso_man["price"]), 2),
        "size": round(float(utolso_man["size"]), 6),
        "cost_usdt": round(float(utolso_man["cost_usdt"]), 2),
        "time": utolso_man.get("time")
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
    if not elozmenyek or allapot in ("VÉTEL", "KÉZI VÉTEL", "ELADÁS"):
        mentes_kell = True
    else:
        try:
            utolso_ido = datetime.fromisoformat(elozmenyek[-1]["time"])
            if (most - utolso_ido).total_seconds() >= 3300:
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
        elozmenyek = elozmenyek[-720:]
        with open(fajl, "w", encoding="utf-8") as f:
            json.dump(elozmenyek, f, ensure_ascii=False, indent=2)
        print(f"Profit-görbe frissítve ({len(elozmenyek)} pont a history.json-ban).")


def github_allapot_feltoltese(dontes, utolso_push_ido):
    if not GITHUB_PUSH_MOD:
        return utolso_push_ido
    most_ts = time.time()
    fontos_esemeny = dontes in ("VÉTEL", "KÉZI VÉTEL", "ELADÁS")
    if not fontos_esemeny and (most_ts - utolso_push_ido) < GITHUB_PUSH_INTERVAL_MP:
        return utolso_push_ido

    try:
        subprocess.run(["git", "add", "status.json", "history.json"], check=True)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if diff.returncode != 0:
            uzenet = f"Laptop robot frissítés ({dontes}) - {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
            subprocess.run(["git", "commit", "-m", uzenet], check=True)
            subprocess.run(["git", "push"], check=True)
            print("GitHub dashboard fájlok (status.json, history.json) feltöltve.")
        return most_ts
    except Exception as hiba:
        print(f"Figyelmeztetés: GitHub push sikertelen ({hiba})")
        return utolso_push_ido


def eladasi_telegram_ertesites(ok, bazis_ar, eladasi_ar, profit_pct, profit_usdt, uj_usdt):
    veteli_szoveg = f"{bazis_ar:.2f} {JEGYZESI_COIN}" if bazis_ar else "ismeretlen"
    if profit_usdt >= 0:
        eredmeny_cimke = f"🟢 NYERESÉG: {profit_pct:+.2f}% ({profit_usdt:+.2f} {JEGYZESI_COIN})"
    else:
        eredmeny_cimke = f"🔴 VESZTESÉG: {profit_pct:+.2f}% ({profit_usdt:+.2f} {JEGYZESI_COIN})"

    telegram_uzenet(
        f"📤 ELADÁS | {PIONEX_SZIMBOLUM}\n"
        f"{eredmeny_cimke}\n"
        f"Vételi ár: {veteli_szoveg}\n"
        f"Eladási ár: {eladasi_ar:.2f} {JEGYZESI_COIN}\n"
        f"Ok: {ok}\n"
        f"Új szabad egyenleg: {uj_usdt:.2f} {JEGYZESI_COIN}"
    )


def veteli_telegram_ertesites(kezis, veteli_ar, mennyiseg, osszeg_usdt, indok):
    cim = f"🟩 KÉZI VÉTEL | {PIONEX_SZIMBOLUM}" if kezis else f"🟦 VÉTEL | {PIONEX_SZIMBOLUM}"
    telegram_uzenet(
        f"{cim}\n"
        f"Vételi ár: {veteli_ar:.2f} {JEGYZESI_COIN}\n"
        f"Mennyiség: {mennyiseg:.6f} {ALAP_COIN} ({osszeg_usdt:.2f} {JEGYZESI_COIN})\n"
        f"Nyereség / veszteség: új pozíció (még nincs realizált P/L)\n"
        f"Indok: {indok}"
    )


def futtatas():
    elozo = elozo_allapot_betoltese()
    manual_buys = list(elozo.get("manual_buys") or [])
    manual_order_ids = [str(m.get("order_id")) for m in manual_buys if m.get("order_id") is not None]

    tizedes_pontossag = lekerdezd_mennyiseg_pontossag(PIONEX_SZIMBOLUM)
    szabad_usdt = szabad_egyenleg(JEGYZESI_COIN)
    szabad_kripto = szabad_egyenleg(ALAP_COIN)
    veteli_osszeg = round(szabad_usdt * TET_SZAZALEK, 2)
    kotesek = utolso_kotesek(PIONEX_SZIMBOLUM, manual_order_ids=manual_order_ids)
    vetel_info = utolso_vetel_adatok(kotesek, manual_buys=manual_buys)

    print(
        f"Pionex API kapcsolat OK! ({PIONEX_SZIMBOLUM} tizedespontosság: {tizedes_pontossag}) | "
        f"Szabad egyenleg: {szabad_usdt:.4f} {JEGYZESI_COIN} "
        f"({int(TET_SZAZALEK*100)}% tét = {veteli_osszeg:.2f} {JEGYZESI_COIN}) | {szabad_kripto:.6f} {ALAP_COIN}"
    )

    adatok = yf.Ticker(YF_SZIMBOLUM).history(period="10d", interval="1h")
    if adatok is None or adatok.empty:
        raise RuntimeError(f"Yahoo Finance üres adatsort adott vissza ({YF_SZIMBOLUM}).")

    adatok["MA5"] = ta.trend.SMAIndicator(close=adatok["Close"], window=5).sma_indicator()
    adatok["MA10"] = ta.trend.SMAIndicator(close=adatok["Close"], window=10).sma_indicator()
    adatok["MA15"] = ta.trend.SMAIndicator(close=adatok["Close"], window=15).sma_indicator()
    adatok["MA30"] = ta.trend.SMAIndicator(close=adatok["Close"], window=30).sma_indicator()
    adatok["ADX"] = ta.trend.ADXIndicator(
        high=adatok["High"], low=adatok["Low"], close=adatok["Close"], window=14
    ).adx()
    adatok["RSI"] = ta.momentum.RSIIndicator(close=adatok["Close"], window=14).rsi()

    yf_ar = float(adatok["Close"].iloc[-1])
    ar = pionex_aktualis_ar(PIONEX_SZIMBOLUM, yf_ar)
    ma5 = float(adatok["MA5"].iloc[-1])
    ma10 = float(adatok["MA10"].iloc[-1])
    elozo_ma10 = float(adatok["MA10"].iloc[-2]) if len(adatok) >= 2 else ma10
    ma15 = float(adatok["MA15"].iloc[-1])
    ma30 = float(adatok["MA30"].iloc[-1])
    adx = float(adatok["ADX"].iloc[-1])
    rsi = float(adatok["RSI"].iloc[-1])

    print(
        f"{PIONEX_SZIMBOLUM} élő ár: {ar:.2f} (YF: {yf_ar:.2f}) | MA5/MA15: {ma5:.2f}/{ma15:.2f} | "
        f"MA10/MA30: {ma10:.2f}/{ma30:.2f} | ADX: {adx:.1f} | RSI: {rsi:.1f}"
    )

    pozicio_ertek_usd = szabad_kripto * ar
    van_pozicio = pozicio_ertek_usd >= 10.0

    korai_ma5_jel = (ma5 > ma15) and (rsi >= KORAI_RSI_MA5_MIN)
    korai_ma10_jel = (ar > ma10) and (ma10 > elozo_ma10) and (rsi >= KORAI_RSI_MA10_MIN)
    korai_emelked_jel = KORAI_BELEPES and (korai_ma5_jel or korai_ma10_jel)
    fo_ma_jel = (ma10 > ma30) and (ma10 >= elozo_ma10 * 0.998)
    veteli_jel = fo_ma_jel or korai_emelked_jel

    dontes = "VÁRAKOZÁS"
    dontes_reszlet = "Nincs nyitott pozíció és nincs szűrt vételi jelzés."
    veteli_ar = vetel_info["price"] if (van_pozicio and vetel_info) else None
    veteli_ido = vetel_info["time"] if (van_pozicio and vetel_info) else None
    veteli_meret = vetel_info["size"] if (van_pozicio and vetel_info) else None
    veteli_koltseg = vetel_info["cost_usdt"] if (van_pozicio and vetel_info) else None
    profit_pct = 0.0
    profit_usdt = 0.0
    stop_loss_ar = None
    legmagasabb_ar = None
    trailing_aktiv = False

    kenyszertett_eladas = os.environ.get("FORCE_SELL", "").strip().lower() == "true"
    kenyszertett_vetel = os.environ.get("FORCE_BUY", "").strip().lower() == "true"
    eladhato_most = lefele_kerekit(szabad_kripto, tizedes_pontossag)

    if kenyszertett_eladas and eladhato_most > 0:
        bazis_ar = veteli_ar or (float(elozo["entry_price"]) if elozo.get("entry_price") else ar)
        profit_pct = ((ar - bazis_ar) / bazis_ar) * 100 if bazis_ar else 0.0
        profit_usdt = (ar - bazis_ar) * eladhato_most if bazis_ar else 0.0
        ok = "MANUÁLIS AZONNALI ELADÁS (APP GOMB)"

        if TESZT_MOD:
            dontes = "TESZT ELADÁS"
            dontes_reszlet = f"[TESZT MÓD] {ok} -> {eladhato_most} {ALAP_COIN} eladása lenne ({profit_pct:+.2f}%)."
            print(dontes_reszlet)
        else:
            print(f"{ok} -> {eladhato_most} {ALAP_COIN} eladása ({profit_pct:+.2f}%)...")
            pionex_keres("POST", "/api/v1/trade/order", body={
                "symbol": PIONEX_SZIMBOLUM,
                "side": "SELL",
                "type": "MARKET",
                "size": f"{eladhato_most:.{tizedes_pontossag}f}"
            })
            print("Pozíció kézi gombbal lezárva!")

            time.sleep(2)
            uj_usdt = szabad_egyenleg(JEGYZESI_COIN)
            szabad_usdt = uj_usdt
            szabad_kripto = szabad_egyenleg(ALAP_COIN)
            manual_buys = []
            manual_order_ids = []
            kotesek = utolso_kotesek(PIONEX_SZIMBOLUM, manual_order_ids=manual_order_ids)
            veteli_osszeg = round(szabad_usdt * TET_SZAZALEK, 2)
            pozicio_ertek_usd = szabad_kripto * ar
            van_pozicio = False
            legmagasabb_ar = None
            trailing_aktiv = False
            stop_loss_ar = None
            dontes = "ELADÁS"
            dontes_reszlet = f"{ok} | Eredmény: {profit_pct:+.2f}% ({profit_usdt:+.2f} {JEGYZESI_COIN})"

            eladasi_telegram_ertesites(ok, bazis_ar, ar, profit_pct, profit_usdt, uj_usdt)

    elif kenyszertett_vetel:
        raw_manual_usdt = os.environ.get("MANUAL_BUY_USDT", "").strip()
        try:
            kezdi_osszeg = float(raw_manual_usdt) if raw_manual_usdt else veteli_osszeg
        except ValueError:
            kezdi_osszeg = veteli_osszeg
        kezdi_osszeg = round(min(szabad_usdt, max(0.0, kezdi_osszeg)), 2)

        if kezdi_osszeg < MINIMUM_TET_USDT:
            dontes = "KEVÉS EGYENLEG"
            dontes_reszlet = f"Kézi vételhez kevés a szabad {JEGYZESI_COIN} ({kezdi_osszeg:.2f} < {MINIMUM_TET_USDT:.0f})."
            print(f"KÉZI VÉTEL BLOKKOLVA -> {dontes_reszlet}")
        elif TESZT_MOD:
            dontes = "TESZT KÉZI VÉTEL"
            dontes_reszlet = f"[TESZT MÓD] Kézi vétel lenne ({kezdi_osszeg:.2f} {JEGYZESI_COIN}), a robot logikája érvényes lesz rá."
            print(dontes_reszlet)
        else:
            print(f"KÉZI VÉTEL -> {kezdi_osszeg:.2f} {JEGYZESI_COIN} értékű {PIONEX_SZIMBOLUM}...")
            eredmeny = pionex_keres("POST", "/api/v1/trade/order", body={
                "symbol": PIONEX_SZIMBOLUM,
                "side": "BUY",
                "type": "MARKET",
                "amount": f"{kezdi_osszeg:.2f}"
            })
            rendeles_id = eredmeny.get("orderId")
            print(f"Kézi vételi megbízás elküldve! ID: {rendeles_id}")
            time.sleep(2)

            uj_usdt = szabad_egyenleg(JEGYZESI_COIN)
            uj_kripto = szabad_egyenleg(ALAP_COIN)
            vasarolt_sol = max(0.0, uj_kripto - szabad_kripto)
            if vasarolt_sol == 0.0 and ar > 0:
                vasarolt_sol = kezdi_osszeg / ar

            manual_buys.append({
                "order_id": rendeles_id,
                "price": round(ar, 2),
                "size": round(vasarolt_sol, 6),
                "cost_usdt": round(kezdi_osszeg, 2),
                "time": datetime.now(timezone.utc).isoformat()
            })
            manual_order_ids = [str(m.get("order_id")) for m in manual_buys if m.get("order_id") is not None]

            szabad_usdt = uj_usdt
            szabad_kripto = uj_kripto
            veteli_osszeg = round(szabad_usdt * TET_SZAZALEK, 2)
            pozicio_ertek_usd = szabad_kripto * ar
            van_pozicio = pozicio_ertek_usd >= 10.0
            kotesek = utolso_kotesek(PIONEX_SZIMBOLUM, manual_order_ids=manual_order_ids)
            vetel_info = utolso_vetel_adatok(kotesek, manual_buys=manual_buys)

            veteli_ar = vetel_info["price"] if vetel_info else round(ar, 2)
            veteli_ido = vetel_info["time"] if vetel_info else datetime.now(timezone.utc).isoformat()
            veteli_meret = vetel_info["size"] if vetel_info else szabad_kripto
            veteli_koltseg = vetel_info["cost_usdt"] if vetel_info else round(szabad_kripto * veteli_ar, 2)
            elozo_csucs = float(elozo["highest_price"]) if elozo.get("highest_price") is not None else 0.0
            legmagasabb_ar = max(elozo_csucs, ar, veteli_ar)
            stop_loss_ar = veteli_ar * (1 - STOP_LOSS_SZAZALEK)
            profit_pct = ((ar - veteli_ar) / veteli_ar) * 100 if veteli_ar else 0.0
            profit_usdt = (ar - veteli_ar) * lefele_kerekit(szabad_kripto, tizedes_pontossag) if veteli_ar else 0.0

            dontes = "KÉZI VÉTEL"
            dontes_reszlet = (
                f"Kézi vétel rögzítve (~{vasarolt_sol:.4f} {ALAP_COIN} @ ~${ar:.2f}), "
                f"a robot automatikus logikája érvényes a pozícióra."
            )

            veteli_telegram_ertesites(
                True,
                veteli_ar,
                vasarolt_sol,
                kezdi_osszeg,
                "Kézi vételi gomb (APP)"
            )

    elif veteli_jel and not van_pozicio:
        belepes_ok = (
            "MA10 > MA30"
            if fo_ma_jel
            else ("Korai MA5 > MA15 + RSI" if korai_ma5_jel else "Korai emelkedő MA10 + RSI")
        )
        if adx < ADX_MINIMUM:
            dontes = "SZŰRŐ BLOKKOLTA"
            dontes_reszlet = f"Gyenge trend / oldalazás (ADX: {adx:.1f}, min. {ADX_MINIMUM} kell)."
            print(f"SZŰRŐ BLOKKOLTA -> {dontes_reszlet}")
        elif rsi > RSI_MAXIMUM:
            dontes = "SZŰRŐ BLOKKOLTA"
            dontes_reszlet = f"Túlvásárolt piac (RSI: {rsi:.1f}, max. {RSI_MAXIMUM} lehet)."
            print(f"SZŰRŐ BLOKKOLTA -> {dontes_reszlet}")
        elif veteli_osszeg < MINIMUM_TET_USDT:
            dontes = "KEVÉS EGYENLEG"
            dontes_reszlet = f"A {int(TET_SZAZALEK*100)}%-os tét ({veteli_osszeg:.2f} {JEGYZESI_COIN}) a {MINIMUM_TET_USDT:.0f} {JEGYZESI_COIN} minimum alatt van."
            print(f"KEVÉS EGYENLEG -> {dontes_reszlet}")
        elif TESZT_MOD:
            dontes = "TESZT VÉTEL"
            dontes_reszlet = f"[TESZT MÓD] Vételi jelzés lenne ({belepes_ok}, {veteli_osszeg:.2f} {JEGYZESI_COIN})."
            print(dontes_reszlet)
        else:
            print(f"VÉTELI JELZÉS ({belepes_ok}) -> {veteli_osszeg:.2f} {JEGYZESI_COIN} értékű {PIONEX_SZIMBOLUM} vásárlása...")
            eredmeny = pionex_keres("POST", "/api/v1/trade/order", body={
                "symbol": PIONEX_SZIMBOLUM,
                "side": "BUY",
                "type": "MARKET",
                "amount": f"{veteli_osszeg:.2f}"
            })
            print(f"Éles vételi megbízás elküldve! ID: {eredmeny.get('orderId')}")
            time.sleep(2)
            szabad_usdt = szabad_egyenleg(JEGYZESI_COIN)
            szabad_kripto = szabad_egyenleg(ALAP_COIN)
            kotesek = utolso_kotesek(PIONEX_SZIMBOLUM, manual_order_ids=manual_order_ids)
            vetel_info = utolso_vetel_adatok(kotesek, manual_buys=manual_buys)
            veteli_osszeg = round(szabad_usdt * TET_SZAZALEK, 2)
            pozicio_ertek_usd = szabad_kripto * ar
            van_pozicio = pozicio_ertek_usd >= 10.0
            veteli_ar = vetel_info["price"] if vetel_info else ar
            veteli_ido = vetel_info["time"] if vetel_info else datetime.now(timezone.utc).isoformat()
            veteli_meret = vetel_info["size"] if vetel_info else szabad_kripto
            veteli_koltseg = vetel_info["cost_usdt"] if vetel_info else round(szabad_kripto * ar, 2)
            legmagasabb_ar = max(ar, veteli_ar)
            stop_loss_ar = veteli_ar * (1 - STOP_LOSS_SZAZALEK)
            dontes = "VÉTEL"
            dontes_reszlet = f"Új pozíció nyitva ~{veteli_ar:.2f} {JEGYZESI_COIN} áron ({belepes_ok})."

            veteli_telegram_ertesites(
                False,
                veteli_ar,
                veteli_meret,
                pozicio_ertek_usd,
                f"{belepes_ok} | ADX={adx:.1f}, RSI={rsi:.1f}"
            )

    elif van_pozicio:
        eladhato = lefele_kerekit(szabad_kripto, tizedes_pontossag)
        bazis_ar = veteli_ar or (float(elozo["entry_price"]) if elozo.get("entry_price") else ar)
        profit_pct = ((ar - bazis_ar) / bazis_ar) * 100 if bazis_ar else 0.0
        profit_usdt = (ar - bazis_ar) * eladhato if bazis_ar else 0.0

        azonos_pozicio = (
            elozo.get("has_position")
            and elozo.get("entry_price") is not None
            and elozo.get("highest_price") is not None
            and float(elozo.get("entry_price")) <= (bazis_ar or ar) * 1.01
        )
        if azonos_pozicio:
            legmagasabb_ar = max(float(elozo["highest_price"]), ar, bazis_ar or ar)
        else:
            legmagasabb_ar = max(ar, bazis_ar or ar)

        lepes_szint = 0
        korai_vedelem_aktiv = False
        if bazis_ar and bazis_ar > 0:
            alap_sl = bazis_ar * (1 - STOP_LOSS_SZAZALEK)
            csucs_nyereseg_pct = ((legmagasabb_ar - bazis_ar) / bazis_ar) * 100.0
            lepes_szint = max(0, math.floor(csucs_nyereseg_pct / (LEPCSO_SZAZALEK * 100.0)))

            if lepes_szint >= 1:
                trailing_aktiv = True
                visszahuzasi_sl = legmagasabb_ar * (1 - VISSZAHUZAS_SZAZALEK)
                stop_loss_ar = max(alap_sl, visszahuzasi_sl)
            elif csucs_nyereseg_pct >= KORAI_PROFIT_VEDELEM_KUSZOB_PCT:
                trailing_aktiv = True
                korai_vedelem_aktiv = True
                korai_sl = legmagasabb_ar * (1 - (KORAI_PROFIT_VISSZAHUZAS_PCT / 100.0))
                stop_loss_ar = max(alap_sl, korai_sl)
            else:
                trailing_aktiv = False
                stop_loss_ar = alap_sl

        stop_loss_aktiv = (ar <= stop_loss_ar) if stop_loss_ar else False
        ma_eladas_jel = (
            (ma10 < ma30) and (profit_pct >= MA_ELADAS_MIN_PROFIT_PCT)
        ) or (
            (ar < ma30) and (adx >= 25.0) and (profit_pct <= -1.5)
        )

        if stop_loss_aktiv or ma_eladas_jel:
            if stop_loss_aktiv:
                if trailing_aktiv and korai_vedelem_aktiv:
                    ok = (
                        f"KORAI PROFITVÉDŐ KÖVETŐ SL (+{KORAI_PROFIT_VEDELEM_KUSZOB_PCT:.1f}% felett, "
                        f"Csúcs: ${legmagasabb_ar:.2f}, -{KORAI_PROFIT_VISSZAHUZAS_PCT:.1f}%)"
                    )
                elif trailing_aktiv:
                    ok = (
                        f"LÉPCSŐZETES KÖVETŐ SL (+{lepes_szint * int(LEPCSO_SZAZALEK * 100)}% lépcső, "
                        f"Csúcs: ${legmagasabb_ar:.2f}, -{VISSZAHUZAS_SZAZALEK * 100:.1f}%)"
                    )
                else:
                    ok = f"STOP-LOSS (-{int(STOP_LOSS_SZAZALEK*100)}%)"
            else:
                ok = f"TREND / ELADÁSI JELZÉS (MA10={ma10:.2f}, MA30={ma30:.2f}, {profit_pct:+.2f}%)"

            if TESZT_MOD:
                dontes = "TESZT ELADÁS"
                dontes_reszlet = f"[TESZT MÓD] {ok} -> {eladhato} {ALAP_COIN} eladása lenne ({profit_pct:+.2f}%)."
                print(dontes_reszlet)
            else:
                print(f"{ok} -> {eladhato} {ALAP_COIN} eladása ({profit_pct:+.2f}%)...")
                pionex_keres("POST", "/api/v1/trade/order", body={
                    "symbol": PIONEX_SZIMBOLUM,
                    "side": "SELL",
                    "type": "MARKET",
                    "size": f"{eladhato:.{tizedes_pontossag}f}"
                })
                print("Pozíció lezárva!")

                time.sleep(2)
                uj_usdt = szabad_egyenleg(JEGYZESI_COIN)
                szabad_usdt = uj_usdt
                szabad_kripto = szabad_egyenleg(ALAP_COIN)
                manual_buys = []
                manual_order_ids = []
                kotesek = utolso_kotesek(PIONEX_SZIMBOLUM, manual_order_ids=manual_order_ids)
                veteli_osszeg = round(szabad_usdt * TET_SZAZALEK, 2)
                pozicio_ertek_usd = szabad_kripto * ar
                van_pozicio = False
                legmagasabb_ar = None
                trailing_aktiv = False
                stop_loss_ar = None
                dontes = "ELADÁS"
                dontes_reszlet = f"{ok} | Eredmény: {profit_pct:+.2f}% ({profit_usdt:+.2f} {JEGYZESI_COIN})"

                eladasi_telegram_ertesites(ok, bazis_ar, ar, profit_pct, profit_usdt, uj_usdt)
        else:
            dontes = "TARTÁS"
            if trailing_aktiv and korai_vedelem_aktiv:
                sl_info = (
                    f" | Korai követő SL: ${stop_loss_ar:.2f} "
                    f"(Csúcs: ${legmagasabb_ar:.2f}, -{KORAI_PROFIT_VISSZAHUZAS_PCT:.1f}%)"
                )
            elif trailing_aktiv:
                sl_info = (
                    f" | Lépcső: +{lepes_szint * int(LEPCSO_SZAZALEK * 100)}%, Követő SL: ${stop_loss_ar:.2f} "
                    f"(Csúcs: ${legmagasabb_ar:.2f}, -{VISSZAHUZAS_SZAZALEK * 100:.1f}%)"
                )
            else:
                sl_info = f" | SL: ${stop_loss_ar:.2f}"
            dontes_reszlet = f"{eladhato} {ALAP_COIN} (~{pozicio_ertek_usd:.2f} USD) | Eredmény: {profit_pct:+.2f}%{sl_info}"
            print(f"TARTÁS -> {dontes_reszlet}")
    else:
        print(f"VÁRAKOZÁS -> {dontes_reszlet}")

    dashboard_adat = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "symbol": PIONEX_SZIMBOLUM,
        "base_coin": ALAP_COIN,
        "quote_coin": JEGYZESI_COIN,
        "price": round(ar, 2),
        "pionex_price": round(ar, 2),
        "yf_price": round(yf_ar, 2),
        "ma10": round(ma10, 2),
        "ma30": round(ma30, 2),
        "adx": round(adx, 1),
        "rsi": round(rsi, 1),
        "free_usdt": round(szabad_usdt, 4),
        "free_crypto": round(szabad_kripto, 6),
        "next_bet_usdt": round(veteli_osszeg, 2),
        "position_usd": round(pozicio_ertek_usd, 2),
        "total_usd": round(szabad_usdt + pozicio_ertek_usd, 2),
        "has_position": van_pozicio,
        "entry_price": round(veteli_ar, 2) if veteli_ar else None,
        "entry_time": veteli_ido,
        "entry_size": round(veteli_meret, 6) if veteli_meret else None,
        "entry_cost_usdt": round(veteli_koltseg, 2) if veteli_koltseg else None,
        "highest_price": round(legmagasabb_ar, 2) if legmagasabb_ar else None,
        "trailing_active": trailing_aktiv,
        "stop_loss_price": round(stop_loss_ar, 2) if stop_loss_ar else None,
        "stop_loss_pct": round(STOP_LOSS_SZAZALEK * 100, 1),
        "profit_pct": round(profit_pct, 2),
        "profit_usdt": round(profit_usdt, 2),
        "status": dontes,
        "status_detail": dontes_reszlet,
        "adx_min": ADX_MINIMUM,
        "rsi_max": RSI_MAXIMUM,
        "manual_buys": manual_buys,
        "recent_trades": kotesek
    }

    with open("status.json", "w", encoding="utf-8") as f:
        json.dump(dashboard_adat, f, ensure_ascii=False, indent=2)
    print("Dashboard status.json sikeresen elmentve.")

    profit_gorbe_mentese(szabad_usdt + pozicio_ertek_usd, ar, dontes)
    return dontes


def folytonos_futtatas():
    print(f"Folyamatos robot elindítva ({PIONEX_SZIMBOLUM}, ciklus: {CIKLUSSZUNET_MP} mp). Leállítás: Ctrl+C")
    utolso_push_ido = 0.0
    utolso_hiba_ertesites = 0.0

    while True:
        try:
            dontes = futtatas()
            utolso_push_ido = github_allapot_feltoltese(dontes, utolso_push_ido)
        except KeyboardInterrupt:
            print("\nRobot leállítva a felhasználó által (Ctrl+C).")
            break
        except Exception as hiba:
            print(f"ROBOT HIBA (folytatás {CIKLUSSZUNET_MP} mp múlva): {hiba}")
            most_ts = time.time()
            if (most_ts - utolso_hiba_ertesites) >= HIBA_ERTESITES_SZUNET_MP:
                utolso_hiba_ertesites = most_ts
                telegram_uzenet(
                    f"⚠️ ROBOT HIBA (folyamatos mód) | {PIONEX_SZIMBOLUM}\n"
                    f"Hibaüzenet: {hiba}"
                )
        time.sleep(CIKLUSSZUNET_MP)


if __name__ == "__main__":
    if FOLYAMATOS_MOD:
        folytonos_futtatas()
    else:
        try:
            futtatas()
        except Exception as hiba:
            print(f"ROBOT HIBA: {hiba}")
            telegram_uzenet(
                f"⚠️ ROBOT HIBA | {PIONEX_SZIMBOLUM}\n"
                f"Hibaüzenet: {hiba}"
            )
            sys.exit(1)
