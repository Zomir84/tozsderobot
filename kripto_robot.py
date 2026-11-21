import os
import yfinance as yf
import ta
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# 1. Kulcsok beolvasása
API_KEY = os.environ["ALPACA_API_KEY"].strip()
SECRET_KEY = os.environ["ALPACA_SECRET_KEY"].strip()

YF_SZIMBOLUM = "BTC-USD"      # Yahoo Finance jelölés (pl. ETH-USD is lehet)
ALPACA_SZIMBOLUM = "BTC/USD"  # Alpaca jelölés
OSSZEG_USD = 1000             # Ennyi dollárért vesz egy körben
STOP_LOSS_SZAZALEK = 0.05     # 5%-os veszteségvágó

client = TradingClient(API_KEY, SECRET_KEY, paper=True)

# 2. Órás gyertyák letöltése és MA10/30 számítása
adatok = yf.Ticker(YF_SZIMBOLUM).history(period="5d", interval="1h")
adatok["MA10"] = ta.trend.SMAIndicator(close=adatok["Close"], window=10).sma_indicator()
adatok["MA30"] = ta.trend.SMAIndicator(close=adatok["Close"], window=30).sma_indicator()

ar = float(adatok["Close"].iloc[-1])
ma10 = float(adatok["MA10"].iloc[-1])
ma30 = float(adatok["MA30"].iloc[-1])
print(f"{ALPACA_SZIMBOLUM} ár: {ar:.2f} USD | MA10 (1h): {ma10:.2f} | MA30 (1h): {ma30:.2f}")

# 3. Nyitott pozíció keresése (az Alpaca kriptónál sokszor perjel nélkül, BTCUSD-ként tárolja)
poziciok = {p.symbol: p for p in client.get_all_positions()}
pozicio = poziciok.get(ALPACA_SZIMBOLUM) or poziciok.get(ALPACA_SZIMBOLUM.replace("/", ""))

# 4. VÉTEL
if ma10 > ma30 and pozicio is None:
    print(f"VÉTELI JELZÉS -> {OSSZEG_USD} USD értékű {ALPACA_SZIMBOLUM} vásárlása...")
    megbizas = MarketOrderRequest(
        symbol=ALPACA_SZIMBOLUM,
        notional=OSSZEG_USD,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.GTC
    )
    client.submit_order(megbizas)
    print("Vételi megbízás elküldve!")

# 5. ELADÁS (5%-os Stop-Loss vagy trend vége)
elif pozicio is not None:
    veteli_ar = float(pozicio.avg_entry_price)
    aktualis_ar = float(pozicio.current_price)
    profit_pct = ((aktualis_ar - veteli_ar) / veteli_ar) * 100

    if aktualis_ar <= veteli_ar * (1 - STOP_LOSS_SZAZALEK):
        print(f"STOP-LOSS (-5%) -> Pozíció zárása ({profit_pct:+.2f}%)...")
        client.close_position(pozicio.symbol)
        print("Pozíció lezárva!")
    elif ma10 < ma30:
        print(f"ELADÁSI JELZÉS (MA10 < MA30) -> Pozíció zárása ({profit_pct:+.2f}%)...")
        client.close_position(pozicio.symbol)
        print("Pozíció lezárva!")
    else:
        print(f"TARTÁS -> Vételi ár: {veteli_ar:.2f} USD | Jelenlegi: {profit_pct:+.2f}%")

else:
    print("VÁRAKOZÁS -> Nincs nyitott pozíció és nincs vételi jelzés.")
