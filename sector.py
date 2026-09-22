import importlib

try:
    yf = importlib.import_module("yfinance")
except ModuleNotFoundError:
    raise SystemExit("Please install yfinance first: pip install yfinance")

# For Indian stocks, add ".NS" for NSE or ".BO" for BSE
ticker = "RELIANCE.NS"
stock = yf.Ticker(ticker)

# Fetch and print the sector
print(stock.info.get("sector"))
