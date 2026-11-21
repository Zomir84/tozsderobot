import os
import time
import yfinance as yf
import ta
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, TrailingStopOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus, OrderStatus

# 1. Kulcsok biztonságos beolvasása a GitHub Secrets-ből
API_KEY = os.environ["ALPACA_API_KEY"].strip()
SECRET_KEY = os.environ["ALPACA_SECRET_KEY"].strip()

SZIMBOLUM = "NVDA"
DARABSZAM = 5
TRAILING_STOP_SZAZALEK = 5.0  # 5%-os követő Stop-Loss (százalékpontban!)

# FONTOS: Maradjon paper=True a tesztidőszak alatt!
client = TradingClient(API_KEY, SECRET_KEY, paper=True)

# 2. Ellenőrzés: nyitva van-e az amerikai tőzsde?
if not client.get_clock().is_open:
    print("Az amerikai tőzsde jelenleg zárva van. A robot most nem kereskedik.")
    raise SystemExit(0)

# 3. Friss árfolyam és MA10/30 számítása
adatok = yf.Ticker(SZIMBOLUM).history(period="3mo")
adatok["MA10"] = ta.trend.SMAIndicator(close=adatok["Close"], window=10).sma_indicator()
adatok["MA30"] = ta.trend.SMAIndicator(close=adatok["Close"], window=30).sma_indicator()

ar = float(adatok["Close"].iloc[-1])
ma10 = float(adatok["MA10"].iloc[-1])
ma30 = float(adatok["MA30"].iloc[-1])
print(f"{SZIMBOLUM} ár: {ar:.2f} USD | MA10: {ma10:.2f} | MA30: {ma30:.2f}")

# 4. Nyitott pozíció és nyitott megbízások ellenőrzése
poziciok = {p.symbol: p for p in client.get_all_positions()}
pozicio = poziciok.get(SZIMBOLUM)
van_pozicio = pozicio is not None

nyitott_megbizasok = client.get_orders(
    GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[SZIMBOLUM])
)

# 5. VÉTEL + szerveroldali 5%-os Trailing Stop
if ma10 > ma30 and not van_pozicio:
    print(f"VÉTELI JELZÉS -> {DARABSZAM} db {SZIMBOLUM} vásárlása...")
    veteli_megbizas = MarketOrderRequest(
        symbol=SZIMBOLUM,
        qty=DARABSZAM,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY
    )
    bekuldott = client.submit_order(veteli_megbizas)

    # Megvárjuk a teljesülést (max. 20 másodperc), hogy rátehessük a Trailing Stopot
    teljesult = False
    for _ in range(10):
        time.sleep(2)
        allapot = client.get_order_by_id(bekuldott.id)
        if allapot.status == OrderStatus.FILLED:
            teljesult = True
            break

    if teljesult:
        trailing_megbizas = TrailingStopOrderRequest(
            symbol=SZIMBOLUM,
            qty=DARABSZAM,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            trail_percent=TRAILING_STOP_SZAZALEK
        )
        client.submit_order(trailing_megbizas)
        print(f"Vétel teljesült! Szerveroldali {TRAILING_STOP_SZAZALEK}%-os Trailing Stop élesítve.")
    else:
        print("A vételi megbízás még nem teljesült 20 mp alatt; a következő futásnál pótoljuk a Trailing Stopot.")

# 6. ELADÁS, ha megtörik a trend (előbb töröljük a nyitott Trailing Stopot)
elif ma10 < ma30 and van_pozicio:
    print("ELADÁSI JELZÉS (MA10 < MA30) -> Nyitott Trailing Stop törlése és pozíció zárása...")
    for m in nyitott_megbizasok:
        client.cancel_order_by_id(m.id)
    time.sleep(2)
    client.close_position(SZIMBOLUM)
    print("Pozíció sikeresen lezárva!")

# 7. TARTÁS + biztonsági háló (ha van pozíció, de hiányzik róla a Trailing Stop)
elif van_pozicio:
    if len(nyitott_megbizasok) == 0:
        trailing_megbizas = TrailingStopOrderRequest(
            symbol=SZIMBOLUM,
            qty=float(pozicio.qty),
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            trail_percent=TRAILING_STOP_SZAZALEK
        )
        client.submit_order(trailing_megbizas)
        print(f"Hiányzó védelem pótolva: {TRAILING_STOP_SZAZALEK}%-os Trailing Stop élesítve.")
    else:
        print("TARTÁS -> A pozíció és a szerveroldali Trailing Stop aktív.")

else:
    print("VÁRAKOZÁS -> Nincs nyitott pozíció és nincs vételi jelzés.")
