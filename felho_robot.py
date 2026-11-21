name: Tozsderobot Futtatas

on:
  schedule:
    - cron: '40 14 * * 1-5'
  workflow_dispatch:

jobs:
  futtatas:
    runs-on: ubuntu-latest
    steps:
      - name: Kód letöltése
        uses: actions/checkout@v4

      - name: Python beállítása
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Csomagok telepítése
        run: pip install -r requirements.txt

      - name: Robot futtatása
        env:
          ALPACA_API_KEY: ${{ secrets.ALPACA_API_KEY }}
          ALPACA_SECRET_KEY: ${{ secrets.ALPACA_SECRET_KEY }}
        run: python felho_robot.py
