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
TESZT_MOD = False                 # Éles mód (a robot kereskedik)
YF_SZIMBOLUM = "SOL-USD"          # Yahoo Finance jelölés
PIONEX_SZIMBOLUM = "SOL_USDT"     # Pionex pár
ALAP_COIN = "SOL"                 # Megvásárolt kriptó
JEGYZESI_COIN = "USDT"            # Amivel fizetsz
TET_SZAZALEK = 0.99               # A szabad USDT 99%-át költi egy vételre
MINIMUM_TET_USDT = 11.0           # Pionex minimum megbízás feletti biztonsági határ
STOP_LOSS_SZAZALEK = 0.06         # Alap 6%-os veszteségvágó
TRAILING_AKTIVALAS = 0.04         # +4% nyereség felett kapcsol be a követő Stop-Loss
TRAILING_TAVOLSAG = 0.025         # Alap követési távolság aktiválás után (-2.5%)
TRAILING_SZOROS_TAVOLSAG = 0.012  # +8% csúcsnyereség felett szorosabb követés (-1.2%)
TRAILING_SZOROS_KUSZOB = 0.08     # +8% nyereségtől szűkül a követési sáv
NULLSZALDO_PUFFER = 0.005         # Aktiválás után min. +0.5%-on véd (díjak felett)
ALAP_TIZEDES_PONTOSSAG = 2        # Tartalék tizedespontosság, ha az API-lekérés nem elérhető

# --- SZŰRŐK ---
ADX_MINIMUM = 20                  # 20 alatt oldalazás, nem vesz
RSI_MAXIMUM = 70                  # 70 felett túlvásárolt, nem vesz
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


def futtatas():
    elozo = elozo_allapot_betoltese()
    tizedes_pontossag = lekerdezd_mennyiseg_pontossag(PIONEX_SZIMBOLUM)
    szabad_usdt = szabad_egyenleg(JEGYZESI_COIN)
    szabad_kripto = szabad_egyenleg(ALAP_COIN)
    veteli_osszeg = round(szabad_usdt * TET_SZAZALEK, 2)
    kotesek = utolso_kotesek(PIONEX_SZIMBOLUM)
    vetel_info = utolso_vetel_adatok(kotesek)

    print(
        f"Pionex API kapcsolat OK! ({PIONEX_SZIMBOLUM} tizedespontosság: {tizedes_pontossag}) | "
        f"Szabad egyenleg: {szabad_usdt:.4f} {JEGYZESI_COIN} "
        f"({int(TET_SZAZALEK*100)}% tét = {veteli_osszeg:.2f} {JEGYZESI_COIN}) | {szabad_kripto:.6f} {ALAP_COIN}"
    )

    adatok = yf.Ticker(YF_SZIMBOLUM).history(period="10d", interval="1h")
    if adatok is None or adatok.empty:
        raise RuntimeError(f"Yahoo Finance üres adatsort adott vissza ({YF_SZIMBOLUM}).")

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

    dontes = "VÁRAKOZÁS"
    dontes_reszlet = "Nincs nyitott pozíció és nincs vételi jelzés."
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
    eladhato_most = lefele_kerekit(szabad_kripto, tizedes_pontossag)

    if kenyszertett_eladas and eladhato_most > 0:
        profit_pct = ((ar - veteli_ar) / veteli_ar) * 100 if veteli_ar else 0.0
        profit_usdt = (ar - veteli_ar) * eladhato_most if veteli_ar else 0.0
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
            kotesek = utolso_kotesek(PIONEX_SZIMBOLUM)
            veteli_osszeg = round(szabad_usdt * TET_SZAZALEK, 2)
            pozicio_ertek_usd = szabad_kripto * ar
            van_pozicio = False
            legmagasabb_ar = None
            trailing_aktiv = False
            stop_loss_ar = None
            dontes = "ELADÁS"
            dontes_reszlet = f"{ok} | Eredmény: {profit_pct:+.2f}% ({profit_usdt:+.2f} {JEGYZESI_COIN})"

            cimke = "🟢 NYERESÉG" if profit_usdt >= 0 else "🔴 VESZTESÉG"
            telegram_uzenet(
                f"{cimke} | {PIONEX_SZIMBOLUM} lezárva\n"
                f"Ok: {ok}\n"
                f"Eredmény: {profit_pct:+.2f}% ({profit_usdt:+.2f} {JEGYZESI_COIN})\n"
                f"Eladási ár: {ar:.2f} {JEGYZESI_COIN}\n"
                f"Új szabad egyenleg: {uj_usdt:.2f} {JEGYZESI_COIN}"
            )

    elif ma10 > ma30 and not van_pozicio:
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
            dontes_reszlet = f"[TESZT MÓD] Vételi jelzés lenne ({veteli_osszeg:.2f} {JEGYZESI_COIN})."
            print(dontes_reszlet)
        else:
            print(f"VÉTELI JELZÉS -> {veteli_osszeg:.2f} {JEGYZESI_COIN} értékű {PIONEX_SZIMBOLUM} vásárlása...")
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
            kotesek = utolso_kotesek(PIONEX_SZIMBOLUM)
            vetel_info = utolso_vetel_adatok(kotesek)
            veteli_osszeg = round(szabad_usdt * TET_SZAZALEK, 2)
            pozicio_ertek_usd = szabad_kripto * ar
            van_pozicio = True
            veteli_ar = vetel_info["price"] if vetel_info else ar
            veteli_ido = vetel_info["time"] if vetel_info else datetime.now(timezone.utc).isoformat()
            veteli_meret = vetel_info["size"] if vetel_info else szabad_kripto
            veteli_koltseg = vetel_info["cost_usdt"] if vetel_info else round(szabad_kripto * ar, 2)
            legmagasabb_ar = max(ar, veteli_ar)
            stop_loss_ar = veteli_ar * (1 - STOP_LOSS_SZAZALEK)
            dontes = "VÉTEL"
            dontes_reszlet = f"Új pozíció nyitva ~{veteli_ar:.2f} {JEGYZESI_COIN} áron."

            telegram_uzenet(
                f"🟦 VÉTEL | {PIONEX_SZIMBOLUM} megnyitva\n"
                f"Belépési ár: ~{veteli_ar:.2f} {JEGYZESI_COIN}\n"
                f"Mennyiség: {veteli_meret:.6f} {ALAP_COIN} (~{pozicio_ertek_usd:.2f} {JEGYZESI_COIN})\n"
                f"Indikátorok: MA10={ma10:.2f} > MA30={ma30:.2f}, ADX={adx:.1f}, RSI={rsi:.1f}"
            )

    elif van_pozicio:
        eladhato = lefele_kerekit(szabad_kripto, tizedes_pontossag)
        profit_pct = ((ar - veteli_ar) / veteli_ar) * 100 if veteli_ar else 0.0
        profit_usdt = (ar - veteli_ar) * eladhato if veteli_ar else 0.0

        azonos_pozicio = (
            elozo.get("has_position")
            and elozo.get("entry_price") == veteli_ar
            and elozo.get("highest_price") is not None
        )
        if azonos_pozicio:
            legmagasabb_ar = max(float(elozo["highest_price"]), ar, veteli_ar or ar)
        else:
            legmagasabb_ar = max(ar, veteli_ar or ar)

        if veteli_ar:
            alap_sl = veteli_ar * (1 - STOP_LOSS_SZAZALEK)
            if legmagasabb_ar >= veteli_ar * (1 + TRAILING_AKTIVALAS):
                trailing_aktiv = True
                csucs_nyereseg_pct = (legmagasabb_ar - veteli_ar) / veteli_ar
                tavolsag = (
                    TRAILING_SZOROS_TAVOLSAG
                    if csucs_nyereseg_pct >= TRAILING_SZOROS_KUSZOB
                    else TRAILING_TAVOLSAG
                )
                koveto_sl = max(
                    veteli_ar * (1 + NULLSZALDO_PUFFER),
                    legmagasabb_ar * (1 - tavolsag)
                )
                stop_loss_ar = max(alap_sl, koveto_sl)
            else:
                trailing_aktiv = False
                stop_loss_ar = alap_sl

        stop_loss_aktiv = (ar <= stop_loss_ar) if stop_loss_ar else False

        if stop_loss_aktiv or ma10 < ma30:
            if stop_loss_aktiv:
                ok = (
                    f"KÖVETŐ STOP-LOSS (Csúcs: ${legmagasabb_ar:.2f})"
                    if trailing_aktiv
                    else f"STOP-LOSS (-{int(STOP_LOSS_SZAZALEK*100)}%)"
                )
            else:
                ok = "ELADÁSI JELZÉS (MA10 < MA30)"

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
                kotesek = utolso_kotesek(PIONEX_SZIMBOLUM)
                veteli_osszeg = round(szabad_usdt * TET_SZAZALEK, 2)
                pozicio_ertek_usd = szabad_kripto * ar
                van_pozicio = False
                legmagasabb_ar = None
                trailing_aktiv = False
                stop_loss_ar = None
                dontes = "ELADÁS"
                dontes_reszlet = f"{ok} | Eredmény: {profit_pct:+.2f}% ({profit_usdt:+.2f} {JEGYZESI_COIN})"

                cimke = "🟢 NYERESÉG" if profit_usdt >= 0 else "🔴 VESZTESÉG"
                telegram_uzenet(
                    f"{cimke} | {PIONEX_SZIMBOLUM} lezárva\n"
                    f"Ok: {ok}\n"
                    f"Eredmény: {profit_pct:+.2f}% ({profit_usdt:+.2f} {JEGYZESI_COIN})\n"
                    f"Eladási ár: {ar:.2f} {JEGYZESI_COIN}\n"
                    f"Új szabad egyenleg: {uj_usdt:.2f} {JEGYZESI_COIN}"
                )
        else:
            dontes = "TARTÁS"
            sl_info = f" | Követő SL: ${stop_loss_ar:.2f} (Csúcs: ${legmagasabb_ar:.2f})" if trailing_aktiv else f" | SL: ${stop_loss_ar:.2f}"
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
        "recent_trades": kotesek
    }

    with open("status.json", "w", encoding="utf-8") as f:
        json.dump(dashboard_adat, f, ensure_ascii=False, indent=2)
    print("Dashboard status.json sikeresen elmentve.")

    profit_gorbe_mentese(szabad_usdt + pozicio_ertek_usd, ar, dontes)

    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch" and not kenyszertett_eladas:
        allapot = f"Nyitott pozíció (~{pozicio_ertek_usd:.2f} USD)" if van_pozicio else "Nincs nyitott pozíció"
        print("Telegram tesztüzenet küldése...")
        telegram_uzenet(
            f"🤖 Telegram teszt OK!\n"
            f"{PIONEX_SZIMBOLUM} ár: {ar:.2f} {JEGYZESI_COIN}\n"
            f"Állapot: {allapot}\n"
            f"Szabad egyenleg: {szabad_usdt:.2f} {JEGYZESI_COIN}"
        )


if __name__ == "__main__":
    try:
        futtatas()
    except Exception as hiba:
        print(f"ROBOT HIBA: {hiba}")
        telegram_uzenet(
            f"⚠️ ROBOT HIBA | {PIONEX_SZIMBOLUM}\n"
            f"Hibaüzenet: {hiba}"
        )
        raise
