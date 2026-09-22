# =========================================================
# 🚀 TradingView Bot (FINAL - MODE BASED TELEGRAM CONTROL)
# ✔ Manual → NO Telegram
# ✔ Chartink → Send stock list
# ✔ Send a formatted stock card for every ticker
# =========================================================

import os
import re
import cv2
import pickle
import importlib
import numpy as np
from collections import defaultdict
from datetime import datetime
try:
    yf = importlib.import_module("yfinance")
except ModuleNotFoundError:
    raise SystemExit("Please install yfinance first: pip install yfinance")
try:
    # Import dynamically so static analyzers do not require Requests' package
    # metadata/source to be available in the selected Python environment.
    requests = __import__("requests")
except ImportError as exc:
    raise ImportError(
        "Requests is required. Install it with: "
        "python -m pip install requests"
    ) from exc
try:
    # Import dynamically so static analyzers do not require Playwright's type
    # stubs/source package to be available in the selected Python environment.
    sync_playwright = __import__("playwright.sync_api", fromlist=["sync_playwright"]).sync_playwright
except ImportError as exc:
    raise ImportError(
        "Playwright is required. Install it with: "
        "python -m pip install playwright && python -m playwright install"
    ) from exc

# ================== CONFIG ==================
CHARTINK_URL = "https://chartink.com/screener/copy-copy-copy-nh-mix-rules-30-hma-530"
NSE_LARGE_DEALS_URL = "https://www.nseindia.com/market-data/large-deals"
NSE_INDEX_PERFORMANCES_URL = "https://www.nseindia.com/market-data/index-performances"
TRADINGVIEW_CHART = "https://www.tradingview.com/chart/?symbol=NSE:{}"

TEMPLATE_PATH = "buy.jpg"

# The screenshot also contains the watchlist/details panel on the right. Keep
# it out of the crop before looking for the most recent Buy marker.
CHART_LEFT = 0.035
CHART_RIGHT = 0.80
CHART_BOTTOM = 0.95
RECENT_CHART_PORTION = 0.30
BUY_MATCH_THRESHOLD = 0.80

COOKIES_FILE = "tradingview_cookies.pkl"
SCREENSHOTS_DIR = "screenshots"
CROPPED_DIR = "cropped"
CARDS_DIR = "cards"

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")

HEADLESS = os.environ.get("HEADLESS", "true").lower() == "true"
DATE_STR = datetime.now().strftime("%Y-%m-%d")

os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
os.makedirs(CROPPED_DIR, exist_ok=True)
os.makedirs(CARDS_DIR, exist_ok=True)

manual_tickers = ["HDFCLIFE", "TCS", "GUJALKALI", "TECHM"]
QUOTE_CACHE = {}

def format_volume(quantity, signed=False):
    prefix = "+" if signed and quantity > 0 else "-" if signed and quantity < 0 else ""
    absolute = abs(quantity)
    if absolute >= 10_000_000:
        value = f"{absolute / 10_000_000:.2f} Cr"
    elif absolute >= 100_000:
        value = f"{absolute / 100_000:.2f} L"
    elif absolute >= 1_000:
        value = f"{absolute / 1_000:.2f} K"
    else:
        value = f"{absolute:,}"
    return f"{prefix}{value}"

def classify_client(client_name):
    name = client_name.upper()
    if any(term in name for term in ["MF", "MUTUAL FUND", "SBI", "HDFC", "ICICI PRU", "NIPPON", "AXIS"]):
        return "Mutual Fund"
    if any(term in name for term in ["FPI", "FII", "SCHRODER", "ACCEL", "TRUDY"]):
        return "FII / FPI"
    if any(term in name for term in ["BARING", "MADISON", "ACTIS", "AVP FUND", "CAPITAL FUND", "VENTURE"]):
        return "PE / VC Fund"
    if any(term in name for term in ["JUMP", "ALPHAGREP", "MICROCURVES", "JUNOMONETA", "QE SECURITIES", "HRTI", "NK SECURITIES", "IRAGE"]):
        return "Algo / Prop Desk"
    if any(term in name for term in ["ARIHANT", "MANSUKH", "NEO APEX", "SECURITIES LIMITED"]):
        return "Broker Facilitator"
    if any(term in name for term in ["LTD", "LIMITED", "HOLDINGS", "TRUST"]):
        return "Promoter / Company"
    return "HNI / Individual"

def analyse_deal_pattern(item):
    buyer_types = {classify_client(name) for name in item["buyers"]}
    seller_types = {classify_client(name) for name in item["sellers"]}
    quality_types = {"Mutual Fund", "FII / FPI", "PE / VC Fund", "Promoter / Company"}
    same_entities = set(item["buyers"]) & set(item["sellers"])
    near_zero = abs(item["net_qty"]) < max(5000, int(item["total_qty"] * 0.05))

    if same_entities or (near_zero and "Algo / Prop Desk" in buyer_types | seller_types):
        return "ROUND-TRIP / SETTLEMENT", "WATCH"
    if item["net_qty"] < 0 and seller_types & quality_types and not buyer_types & quality_types:
        return "INSTITUTIONAL DISTRIBUTION", "AVOID"
    if item["net_qty"] > 0 and len(buyer_types & quality_types) >= 2:
        return "GENUINE ACCUMULATION", "STRONG BUY"
    if item["net_qty"] > 0 and buyer_types & quality_types:
        return "QUALITY BUYING", "BUY"
    if item["net_qty"] > 0:
        return "NET BUYING", "BUY ON DIP"
    if near_zero:
        return "MIXED / NEUTRAL", "WATCH"
    return "NET SELLING", "AVOID"

# ================== TELEGRAM ==================
def send_telegram_message(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text},
            timeout=10
        )
    except Exception as e:
        print("Telegram message error:", e)

def send_telegram_photo(image_path, caption):
    try:
        if not os.path.exists(image_path):
            print("❌ Screenshot not found:", image_path)
            return

        with open(image_path, "rb") as img:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                data={"chat_id": CHAT_ID, "caption": caption},
                files={"photo": img},
                timeout=20
            )
    except Exception as e:
        print("Telegram photo error:", e)

def get_stock_snapshot(symbol):
    symbol = symbol.replace("$", "").replace(".NS", "").strip().upper()
    if symbol in QUOTE_CACHE:
        return QUOTE_CACHE[symbol]

    empty_snapshot = {
        "price": "-", "change": "-", "change_value": None,
        "sector": "Unknown", "previous_volume": None, "available": False,
    }
    try:
        stock = yf.Ticker(f"{symbol}.NS")
        history = stock.history(period="5d", auto_adjust=False)
        if history.empty:
            QUOTE_CACHE[symbol] = empty_snapshot
            return empty_snapshot

        latest = history.iloc[-1]
        previous = history.iloc[-2] if len(history) > 1 else latest
        price = float(latest["Close"])
        previous_close = float(previous["Close"])
        change_value = (price - previous_close) / previous_close * 100 if previous_close else None
        previous_volume = int(previous["Volume"]) if previous["Volume"] == previous["Volume"] else None
        try:
            info = stock.info
        except Exception:
            info = {}
        snapshot = {
            "price": f"{price:,.2f}",
            "change": f"{change_value:+.2f}%" if change_value is not None else "-",
            "change_value": change_value,
            "sector": info.get("sector") or info.get("industry") or "Unknown",
            "previous_volume": previous_volume,
            "available": True,
        }
        QUOTE_CACHE[symbol] = snapshot
        return snapshot
    except Exception as error:
        print(f"⚠️ Quote unavailable for {symbol}; sector performance will be omitted")
        QUOTE_CACHE[symbol] = empty_snapshot
        return empty_snapshot

def draw_card_text(image, text, origin, font_scale, color, thickness=2):
    cv2.putText(
        image, text, origin, cv2.FONT_HERSHEY_DUPLEX,
        font_scale, color, thickness, cv2.LINE_AA
    )

def create_stock_card(symbol, chart_signal):
    snapshot = get_stock_snapshot(symbol)
    width, height = 1200, 650
    image = np.full((height, width, 3), (248, 247, 250), dtype=np.uint8)
    navy = (57, 25, 76)
    muted = (112, 103, 119)
    magenta = (130, 20, 190)
    teal = (0, 129, 128)
    green = (31, 139, 83)
    red = (42, 55, 190)

    cv2.rectangle(image, (0, 0), (width, 16), magenta, -1)
    draw_card_text(image, symbol, (70, 105), 1.8, navy, 3)
    draw_card_text(image, snapshot["price"], (70, 175), 1.5, navy, 2)
    change_color = green if snapshot["change_value"] is not None and snapshot["change_value"] >= 0 else red
    draw_card_text(image, snapshot["change"], (390, 175), 1.05, change_color, 2)
    draw_card_text(image, snapshot["sector"], (70, 225), 0.85, muted, 1)
    draw_card_text(image, "STRONG", (390, 225), 0.85, teal, 2)

    cv2.line(image, (70, 265), (1130, 265), (215, 208, 218), 2)
    draw_card_text(image, "gap", (70, 315), 0.8, muted, 1)
    draw_card_text(image, "today", (260, 315), 0.8, muted, 1)
    draw_card_text(image, "Strong candle", (470, 315), 0.8, muted, 1)
    draw_card_text(image, "volume", (820, 315), 0.8, muted, 1)
    draw_card_text(image, "-", (70, 360), 1.0, navy, 2)
    draw_card_text(image, "-", (260, 360), 1.0, navy, 2)
    draw_card_text(image, "Yes", (470, 360), 1.0, teal, 2)
    draw_card_text(image, "-", (820, 360), 1.0, navy, 2)

    cv2.rectangle(image, (55, 420), (790, 500), magenta, -1)
    draw_card_text(image, "*** STRONG SELL WATCH", (88, 475), 1.25, (255, 255, 255), 2)
    draw_card_text(image, "2140 PE @ ...", (75, 575), 0.95, navy, 2)
    draw_card_text(image, f"Chart: {'BUY CONFIRMED' if chart_signal else 'NO BUY'}", (820, 475), 0.75, muted, 1)

    card_path = os.path.join(CARDS_DIR, f"{symbol}.png")
    cv2.imwrite(card_path, image)
    return card_path, snapshot

# ================== CHARTINK ==================
def get_chartink_stocks(page):
    print("🌐 Fetching Chartink stocks...")
    page.goto(CHARTINK_URL)
    page.wait_for_selector("table tbody tr")

    rows = page.locator("table tbody tr td:nth-child(3) a")
    tickers = [rows.nth(i).inner_text().strip() for i in range(rows.count())]

    print(f"✅ Found {len(tickers)} stocks")
    return tickers

def get_nse_sector(page, symbol):
    return get_stock_snapshot(symbol)["sector"]

def get_nse_deal_analysis(page):
    print("🌐 Fetching NSE large-deal stocks...")
    page.goto(NSE_LARGE_DEALS_URL, wait_until="domcontentloaded")
    try:
        page.wait_for_function(
            """
            () => [...document.querySelectorAll('table')].some(table => {
                const headers = [...table.querySelectorAll('thead th')]
                    .map(header => header.innerText.toLowerCase());
                const hasSymbol = headers.some(header => header.includes('symbol'));
                const hasBuySell = headers.some(header =>
                    header.includes('buy') && header.includes('sell'));
                    const hasQuantity = headers.some(header =>
                        header.includes('quantity') || header.includes('volume'));
                const hasRows = [...table.querySelectorAll('tbody tr')].some(row =>
                    row.innerText.trim().length > 0);
                return hasSymbol && hasBuySell && hasQuantity && hasRows;
            })
            """,
            timeout=30000,
        )
    except Exception as error:
        print(f"⚠️ NSE large-deal table was not ready: {error}")
    page.wait_for_timeout(2000)

    tables = page.locator("table")
    totals = defaultdict(lambda: {
        "security_name": "", "buy_qty": 0, "sell_qty": 0,
        "buyers": set(), "sellers": set(), "buy_prices": [], "sell_prices": [],
    })
    for table_index in range(tables.count()):
        table = tables.nth(table_index)
        headers = [
            header.inner_text().strip().lower()
            for header in table.locator("thead th").all()
        ]
        normalized_headers = [re.sub(r"[^a-z]", "", header) for header in headers]
        symbol_index = next(
            (index for index, header in enumerate(normalized_headers) if header == "symbol"),
            None,
        )
        security_index = next(
            (index for index, header in enumerate(normalized_headers) if "securityname" in header),
            None,
        )
        client_index = next(
            (index for index, header in enumerate(normalized_headers) if "clientname" in header),
            None,
        )
        side_index = next(
            (
                index for index, header in enumerate(normalized_headers)
                if "buy" in header and "sell" in header
            ),
            None,
        )
        quantity_index = next(
            (
                index for index, header in enumerate(normalized_headers)
                if "quantity" in header or "volume" in header
            ),
            None,
        )
        price_index = next(
            (index for index, header in enumerate(normalized_headers) if "price" in header),
            None,
        )
        if any(index is None for index in [symbol_index, client_index, side_index, quantity_index]):
            continue

        rows = table.locator("tbody tr")
        for row_index in range(rows.count()):
            cells = rows.nth(row_index).locator("td")
            required_indices = [symbol_index, client_index, side_index, quantity_index]
            if cells.count() <= max(index for index in required_indices if index is not None):
                continue

            symbol = cells.nth(symbol_index).inner_text().strip().upper()
            security_name = cells.nth(security_index).inner_text().strip() if security_index is not None else symbol
            client_name = cells.nth(client_index).inner_text().strip()
            side = cells.nth(side_index).inner_text().strip().upper()
            quantity_text = cells.nth(quantity_index).inner_text().strip()
            quantity_text = re.sub(r"[^0-9.]", "", quantity_text)
            if not re.fullmatch(r"[A-Z][A-Z0-9&.-]*", symbol) or not quantity_text:
                continue

            quantity = int(float(quantity_text))
            price_text = ""
            if price_index is not None and price_index < cells.count():
                price_text = re.sub(r"[^0-9.]", "", cells.nth(price_index).inner_text())
            price = float(price_text) if price_text else None
            record = totals[symbol]
            record["security_name"] = security_name or record["security_name"]
            if "BUY" in side:
                record["buy_qty"] += quantity
                record["buyers"].add(client_name)
                if price is not None:
                    record["buy_prices"].append(price)
            elif "SELL" in side:
                record["sell_qty"] += quantity
                record["sellers"].add(client_name)
                if price is not None:
                    record["sell_prices"].append(price)

        if totals:
            break

    analysis = []
    for symbol, quantities in totals.items():
        net_qty = quantities["buy_qty"] - quantities["sell_qty"]
        item = {
            "symbol": symbol,
            "security_name": quantities["security_name"] or symbol,
            "buy_qty": quantities["buy_qty"],
            "sell_qty": quantities["sell_qty"],
            "net_qty": net_qty,
            "buyers": quantities["buyers"],
            "sellers": quantities["sellers"],
            "buy_prices": quantities["buy_prices"],
            "sell_prices": quantities["sell_prices"],
        }
        item["total_qty"] = item["buy_qty"] + item["sell_qty"]
        item["pattern"], item["rating"] = analyse_deal_pattern(item)
        item["recommendation"] = item["rating"]
        buyer_names = ", ".join(sorted(item["buyers"])[:2]) or "no named buyer"
        seller_names = ", ".join(sorted(item["sellers"])[:2]) or "no named seller"
        item["price_range"] = format_price_range(item["buy_prices"], item["sell_prices"])
        item["rationale"] = (
            f"Buyers: {buyer_names}; sellers: {seller_names}. "
            f"{item['pattern']} with net {format_volume(net_qty, signed=True)}."
        )
        analysis.append(item)

    for item in analysis:
        item["sector"] = get_nse_sector(page, item["symbol"])
        snapshot = get_stock_snapshot(item["symbol"])
        item["previous_volume"] = snapshot["previous_volume"]
        item["stock_change"] = snapshot["change_value"]
        item["rationale"] += f" Sector: {item['sector']}."

    analysis.sort(key=lambda row: abs(row["net_qty"]), reverse=True)
    sector_changes = defaultdict(list)
    for item in analysis:
        if item["stock_change"] is not None:
            sector_changes[item["sector"]].append(item["stock_change"])
    for item in analysis:
        changes = sector_changes[item["sector"]]
        item["sector_performance"] = sum(changes) / len(changes) if changes else None
    print(f"✅ Found {len(analysis)} unique NSE stocks")
    return analysis

def format_price_range(buy_prices, sell_prices):
    prices = buy_prices + sell_prices
    if not prices:
        return "-"
    return f"{min(prices):.2f}-{max(prices):.2f}"

def format_balance(item):
    sell_qty = item["sell_qty"]
    if not sell_qty:
        return "-"
    return f"{item['net_qty'] / sell_qty * 100:+.1f}%"

def recommendation_label(item):
    labels = {
        "STRONG BUY": "🟢 Strong Buy",
        "BUY": "🟢 Buy",
        "BUY ON DIP": "🟢 Buy on Dip",
        "AVOID": "🟡 Avoid",
        "WATCH": "🟡 Neutral",
    }
    return labels.get(item["rating"], item["rating"].title())

def bulk_view_label(item):
    labels = {
        "STRONG BUY": "STRONG BUY",
        "BUY": "BUY",
        "BUY ON DIP": "BUY ON DIP",
        "AVOID": "AVOID",
        "WATCH": "WATCH",
    }
    return labels.get(item["rating"], item["rating"])

def format_sector_change(value):
    return f"{value:+.2f}%" if value is not None else "-"

def create_bulk_deal_image(ranked, confirmed_symbols):
    columns = ["Rank", "Stock", "Sector", "Buy", "Sell", "Net", "Balance", "View"]
    widths = [70, 165, 190, 140, 140, 140, 130, 270]
    row_height = 62
    image = np.full(
        (150 + row_height * (len(ranked) + 1), sum(widths), 3),
        (248, 247, 250),
        dtype=np.uint8,
    )
    navy = (57, 25, 76)
    muted = (112, 103, 119)
    magenta = (130, 20, 190)
    green = (45, 145, 75)
    yellow = (0, 170, 210)
    red = (42, 55, 190)
    orange = (0, 115, 220)

    cv2.rectangle(image, (0, 0), (image.shape[1], 18), magenta, -1)
    draw_card_text(image, "NSE BULK DEAL ANALYSIS", (40, 78), 1.15, navy, 2)
    draw_card_text(image, DATE_STR, (image.shape[1] - 220, 78), 0.7, muted, 1)
    x_positions = [0]
    for width in widths:
        x_positions.append(x_positions[-1] + width)
    header_y = 105
    cv2.rectangle(image, (0, header_y), (image.shape[1], header_y + row_height), navy, -1)
    for index, column in enumerate(columns):
        draw_card_text(image, column, (x_positions[index] + 14, header_y + 39), 0.6, (255, 255, 255), 1)

    for row_index, item in enumerate(ranked):
        y = header_y + row_height * (row_index + 1)
        if row_index % 2 == 0:
            cv2.rectangle(image, (0, y), (image.shape[1], y + row_height), (237, 233, 241), -1)
        has_buy_signal = item["symbol"] in confirmed_symbols
        view_label = bulk_view_label(item)
        if view_label in {"STRONG BUY", "BUY", "BUY ON DIP"} and not has_buy_signal:
            view_label = f"{view_label} / CHART PENDING"
        values = [
            str(row_index + 1), item["symbol"], item["sector"][:18],
            format_volume(item["buy_qty"]),
            format_volume(item["sell_qty"]) if item["sell_qty"] else "-",
            format_volume(item["net_qty"], signed=True),
            format_balance(item),
            view_label,
        ]
        if item["rating"] in {"STRONG BUY", "BUY", "BUY ON DIP"}:
            view_color = green if has_buy_signal else orange
        elif item["rating"] == "AVOID":
            view_color = red
        else:
            view_color = yellow
        for column_index, value in enumerate(values):
            color = view_color if column_index == 7 else navy
            draw_card_text(image, value, (x_positions[column_index] + 14, y + 39), 0.55, color, 1)

    output_path = os.path.join(CARDS_DIR, "nse_bulk_deal_analysis.png")
    cv2.imwrite(output_path, image)
    return output_path

def create_sector_performance_image(ranked):
    sectors = []
    seen_sectors = set()
    for item in ranked:
        if item["sector"] not in seen_sectors:
            seen_sectors.add(item["sector"])
            sectors.append(item)

    columns = ["Sector / Index", "% Change"]
    widths = [500, 220]
    row_height = 62
    image = np.full(
        (150 + row_height * (len(sectors) + 1), sum(widths), 3),
        (248, 247, 250),
        dtype=np.uint8,
    )
    navy = (57, 25, 76)
    muted = (112, 103, 119)
    magenta = (130, 20, 190)
    green = (45, 145, 75)
    yellow = (0, 170, 210)

    cv2.rectangle(image, (0, 0), (image.shape[1], 18), magenta, -1)
    draw_card_text(image, "SECTOR-WISE PERFORMANCE", (40, 78), 0.9, navy, 2)
    draw_card_text(image, DATE_STR, (image.shape[1] - 180, 78), 0.65, muted, 1)

    x_positions = [0]
    for width in widths:
        x_positions.append(x_positions[-1] + width)
    header_y = 105
    cv2.rectangle(image, (0, header_y), (image.shape[1], header_y + row_height), navy, -1)
    for index, column in enumerate(columns):
        draw_card_text(image, column, (x_positions[index] + 14, header_y + 39), 0.6, (255, 255, 255), 1)

    for row_index, item in enumerate(sectors):
        y = header_y + row_height * (row_index + 1)
        if row_index % 2 == 0:
            cv2.rectangle(image, (0, y), (image.shape[1], y + row_height), (237, 233, 241), -1)
        values = [
            item["sector"],
            format_sector_change(item["sector_performance"]),
        ]
        for column_index, value in enumerate(values):
            color = green if column_index == 1 and item["sector_performance"] is not None and item["sector_performance"] >= 0 else yellow if column_index == 1 else navy
            draw_card_text(image, value, (x_positions[column_index] + 14, y + 39), 0.55, color, 1)

    output_path = os.path.join(CARDS_DIR, "nse_sector_performance.png")
    cv2.imwrite(output_path, image)
    return output_path

def send_nse_analysis(analysis, confirmed_symbols):
    if not analysis:
        send_telegram_message(
            "## Latest Bulk Deal Analysis\n\n"
            "No stock-wise bulk-deal data was found."
        )
        return

    ranked = sorted(analysis, key=lambda item: item["net_qty"], reverse=True)[:8]
    bulk_image = create_bulk_deal_image(ranked, confirmed_symbols)
    sector_image = create_sector_performance_image(ranked)
    send_telegram_photo(bulk_image, f"NSE Bulk Deal Analysis | {DATE_STR}")
    send_telegram_photo(sector_image, f"Sector-wise Performance | {DATE_STR}")

def send_top_nse_buy(analysis, confirmed_symbols):
    buy_stocks = [
        item for item in analysis
        if item["rating"] in {"STRONG BUY", "BUY", "BUY ON DIP"}
        and item["symbol"] in confirmed_symbols
    ]
    if not buy_stocks:
        send_telegram_message(
            "⭐ TOP NSE BUY RECOMMENDATION (FLOW + CHART CONFIRMED)\n"
            "No stock has both positive net buy quantity and a confirmed chart Buy signal."
        )
        return

    top_buy = max(buy_stocks, key=lambda item: item["net_qty"])
    send_telegram_message(
        "⭐ TOP NSE BUY RECOMMENDATION (FLOW + CHART CONFIRMED)\n"
        f"Symbol: {top_buy['symbol']}\n"
        f"Security: {top_buy['security_name']}\n"
        f"Sector: {top_buy['sector']}\n"
        f"Rating: {top_buy['rating']}\n"
        f"Buy: {format_volume(top_buy['buy_qty'])} | "
        f"Sell: {format_volume(top_buy['sell_qty'])} | "
        f"Net: {format_volume(top_buy['net_qty'], signed=True)}\n"
        f"Reason: {top_buy['rationale']}"
    )

def get_nse_sector_performance(page):
    print("🌐 Fetching NSE sector performance...")
    page.goto(NSE_INDEX_PERFORMANCES_URL, wait_until="domcontentloaded")
    page.wait_for_selector("table tbody tr", timeout=30000)

    performance = []
    tables = page.locator("table")
    for table_index in range(tables.count()):
        table = tables.nth(table_index)
        headers = [
            re.sub(r"[^a-z%]", "", header.inner_text().strip().lower())
            for header in table.locator("thead th").all()
        ]
        index_name = next(
            (index for index, header in enumerate(headers) if header == "indexname"),
            None,
        )
        last_index = next(
            (index for index, header in enumerate(headers) if header == "last"),
            None,
        )
        change_index = next(
            (index for index, header in enumerate(headers) if header in {"chng", "change"}),
            None,
        )
        percent_index = next(
            (index for index, header in enumerate(headers) if header in {"%chng", "%change"}),
            None,
        )
        pe_index = next(
            (index for index, header in enumerate(headers) if header == "pe"),
            None,
        )
        pb_index = next(
            (index for index, header in enumerate(headers) if header == "pb"),
            None,
        )
        dividend_index = next(
            (index for index, header in enumerate(headers) if "divyield" in header),
            None,
        )
        required = [index_name, last_index, change_index, percent_index]
        if any(index is None for index in required):
            continue

        rows = table.locator("tbody tr")
        for row_index in range(rows.count()):
            cells = rows.nth(row_index).locator("td")
            if cells.count() <= max(index for index in required if index is not None):
                continue

            values = [cells.nth(index).inner_text().strip() for index in range(cells.count())]
            percent_text = re.sub(r"[^0-9.-]", "", values[percent_index])
            if not percent_text:
                continue
            performance.append({
                "index": values[index_name].replace("\n", " "),
                "last": values[last_index],
                "change": values[change_index],
                "percent": values[percent_index],
                "percent_value": float(percent_text),
                "pe": values[pe_index] if pe_index is not None and pe_index < len(values) else "-",
                "pb": values[pb_index] if pb_index is not None and pb_index < len(values) else "-",
                "dividend": values[dividend_index] if dividend_index is not None and dividend_index < len(values) else "-",
            })

        if performance:
            break

    performance.sort(key=lambda row: row["percent_value"], reverse=True)
    print(f"✅ Found {len(performance)} NSE sector indices")
    return performance

def send_nse_sector_performance(performance):
    if not performance:
        return

    header = "# | Sector / Index       | % Change"
    rows = [
        f"{index:>2} | {item['index'][:21]:<21} | "
        f"{item['percent_value']:+.2f}%"
        for index, item in enumerate(performance, start=1)
    ]
    send_telegram_message(
        "📈 NSE SECTOR PERFORMANCE — ALL INDICES\n"
        f"{header}\n"
        f"{'-' * len(header)}\n"
        + "\n".join(rows)
    )

# ================== CLEAN UI ==================
def clean_chart_ui(page):
    try:
        page.hover('[data-qa-id="legend-titles"]')
        page.wait_for_timeout(500)
        page.click('[data-qa-id="legend-more-action"]')
        page.wait_for_timeout(500)
        page.click('text=Hide')
    except:
        pass

def add_previous_volume_to_chart(image_path, symbol):
    image = cv2.imread(image_path)
    if image is None:
        print(f"⚠️ Could not annotate chart volume for {symbol}")
        return False

    previous_volume = get_stock_snapshot(symbol)["previous_volume"]
    volume_text = format_volume(previous_volume) if previous_volume is not None else "-"
    banner_height = 58
    banner_top = max(0, image.shape[0] - banner_height)
    cv2.rectangle(image, (0, banner_top), (image.shape[1], image.shape[0]), (57, 25, 76), -1)
    draw_card_text(
        image,
        f"Previous day volume: {volume_text}",
        (32, image.shape[0] - 18),
        0.8,
        (255, 255, 255),
        2,
    )
    return cv2.imwrite(image_path, image)

# ================== BUY DETECTION ==================
def detect_buy(image_path):
    img = cv2.imread(image_path)
    template = cv2.imread(TEMPLATE_PATH)

    if img is None or template is None:
        print("❌ Image/template missing")
        return False

    h, w, _ = img.shape

    chart_left = int(w * CHART_LEFT)
    chart_right = int(w * CHART_RIGHT)
    chart_bottom = int(h * CHART_BOTTOM)
    chart_width = chart_right - chart_left

    roi = img[
        int(h * 0.55):chart_bottom,
        chart_left + int(chart_width * (1 - RECENT_CHART_PORTION)):chart_right
    ]

    # Save the recent-area crop, then perform matching on that crop only.
    crop_path = os.path.join(CROPPED_DIR, os.path.basename(image_path))
    if not cv2.imwrite(crop_path, roi):
        print("❌ Could not save cropped image")
        return False

    cropped = cv2.imread(crop_path)
    if cropped is None:
        print("❌ Could not read cropped image")
        return False

    hsv = cv2.cvtColor(cropped, cv2.COLOR_BGR2HSV)
    template_hsv = cv2.cvtColor(template, cv2.COLOR_BGR2HSV)
    template_green = cv2.inRange(
        template_hsv, (35, 60, 40), (90, 255, 255)
    )
    template_green_ratio = cv2.countNonZero(template_green) / template_green.size

    for scale in [0.7, 0.8, 0.9, 1.0, 1.1]:
        resized = cv2.resize(template, None, fx=scale, fy=scale)

        if resized.shape[0] > cropped.shape[0] or resized.shape[1] > cropped.shape[1]:
            continue

        resized_hsv = cv2.cvtColor(resized, cv2.COLOR_BGR2HSV)
        result = cv2.matchTemplate(hsv, resized_hsv, cv2.TM_CCOEFF_NORMED)

        _, confidence, _, location = cv2.minMaxLoc(result)
        height, width = resized.shape[:2]
        candidate = hsv[location[1]:location[1] + height, location[0]:location[0] + width]
        candidate_green = cv2.inRange(candidate, (35, 60, 40), (90, 255, 255))
        green_ratio = cv2.countNonZero(candidate_green) / candidate_green.size
        if confidence >= BUY_MATCH_THRESHOLD and green_ratio >= template_green_ratio * 0.65:
            print(f"✅ RECENT BUY found (match: {confidence:.2f})")
            return True

    return False

# ================== COOKIES ==================
def save_cookies(context):
    with open(COOKIES_FILE, "wb") as f:
        pickle.dump(context.cookies(), f)

def load_cookies(context):
    with open(COOKIES_FILE, "rb") as f:
        context.add_cookies(pickle.load(f))

# ================== MAIN ==================
def main(mode="chartink"):
    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=HEADLESS,
            args=["--start-maximized"]
        )

        context = browser.new_context(no_viewport=True)
        page = context.new_page()

        # LOGIN
        if not os.path.exists(COOKIES_FILE):
            page.goto("https://www.tradingview.com/")
            input("🔐 Login → press ENTER")
            save_cookies(context)
            return

        load_cookies(context)

        # MODE
        if mode == "chartink":
            tickers = get_chartink_stocks(page)
        elif mode == "nse":
            nse_analysis = get_nse_deal_analysis(page)
            tickers = [item["symbol"] for item in nse_analysis]
        else:
            tickers = manual_tickers

        if not tickers:
            print("❌ No stocks")
            return

        # 🔥 ONLY FOR CHARTINK → SEND STOCK LIST
        if mode == "chartink":
            stock_text = "\n".join(tickers[:50])
            send_telegram_message(
                f"📊 Stocks from Chartink ({len(tickers)}):\n\n{stock_text}"
            )

        # LOOP
        confirmed_symbols = set()
        if mode == "nse" and nse_analysis:
            send_nse_analysis(nse_analysis, confirmed_symbols)

        for ticker in tickers:
            print(f"\n📊 Checking {ticker}")

            try:
                page.goto(
                    TRADINGVIEW_CHART.format(ticker),
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                page.wait_for_function(
                    """
                    () => [...document.querySelectorAll('canvas')].some(canvas => {
                        const bounds = canvas.getBoundingClientRect();
                        return bounds.width > 500 && bounds.height > 300;
                    })
                    """,
                    timeout=60000,
                )
                page.wait_for_timeout(8000)

                clean_chart_ui(page)
                try:
                    page.locator('xpath=//*[@id="header-toolbar-fullscreen"]').click()
                    page.wait_for_timeout(1500)
                except Exception as error:
                    print(f"⚠️ Could not enable chart fullscreen: {error}")

                screenshot_path = os.path.join(SCREENSHOTS_DIR, f"{ticker}.png")
                page.screenshot(path=screenshot_path)

                chart_signal = detect_buy(screenshot_path)
                add_previous_volume_to_chart(screenshot_path, ticker)

                if chart_signal:
                    confirmed_symbols.add(ticker)
                    send_telegram_photo(screenshot_path, f"{ticker} | {DATE_STR}")
                else:
                    print("❌ No BUY signal; chart not sent")

            except Exception as e:
                print(f"⚠️ Error {ticker}: {e}")

        browser.close()
        print("\n🎯 DONE")

# ================== RUN ==================
if __name__ == "__main__":
    main(mode="nse")   # or "manual", "chartink", "nse"
