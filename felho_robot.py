import os
import time
import yfinance as yf
import ta
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, StopLossRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass, QueryOrderStatus

# 1. Kulcsok biztonságos beolvasása a GitHub Secrets-ből
API_KEY = os.environ["ALPACA_API_KEY"]
SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]

SZIMBOLUM = "NVDA"
DARABSZAM = 5
STOP_LOSS_SZAZALEK = 0.05  # 5% szerveroldali Stop-Loss

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

# 4. Nyitott pozíció ellenőrzése
poziciok = {p.symbol: p for p in client.get_all_positions()}
van_pozicio = SZIMBOLUM in poziciok

# 5. VÉTEL szerveroldali 5%-os Stop-Loss-szal (OTO megbízás)
if ma10 > ma30 and not van_pozicio:
    stop_ar = round(ar * (1 - STOP_LOSS_SZAZALEK), 2)
    print(f"VÉTELI JELZÉS -> {DARABSZAM} db {SZIMBOLUM}, szerveroldali Stop-Loss: {stop_ar} USD")
    megbizas = MarketOrderRequest(
        symbol=SZIMBOLUM,
        qty=DARABSZAM,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.GTC,
        order_class=OrderClass.OTO,
        stop_loss=StopLossRequest(stop_price=stop_ar)
    )
    client.submit_order(megbizas)
    print("Vételi megbízás és szerveroldali Stop-Loss sikeresen elküldve!")

# 6. ELADÁS, ha megtörik a trend (előbb töröljük a nyitott Stop-Losst, hogy felszabaduljanak a részvények)
elif ma10 < ma30 and van_pozicio:
    print("ELADÁSI JELZÉS (MA10 < MA30) -> Nyitott Stop-Loss törlése és pozíció zárása...")
    nyitott_megbizasok = client.get_orders(
        GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[SZIMBOLUM])
    )
    for m in nyitott_megbizasok:
        client.cancel_order_by_id(m.id)
    time.sleep(2)
    client.close_position(SZIMBOLUM)
    print("Pozíció sikeresen lezárva!")

else:
    print("VÁRAKOZÁS -> Nincs teendő, a robot (és az esetleges Stop-Loss) tartja az állapotot.")
