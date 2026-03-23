import anthropic
import time
import os
import json
import re
import requests
from binance.client import Client
from datetime import datetime

# API Keys
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY")

# Settings
MIN_TRADE_EUR = 1
MAX_TRADE_PERCENT = 0.30
STOP_LOSS_PERCENT = 0.05
CONFIDENCE_THRESHOLD = 7
INTERVAL_SECONDS = 900
WIN_RATE_THRESHOLD = 0.60
DRAWDOWN_LIMIT = 0.40
TARGET_GROWTH = 1.00
TRADE_HISTORY_FILE = "trade_history.json"
PORTFOLIO_BASELINE_FILE = "portfolio_baseline.json"

def load_json_file(filepath, default):
    try:
        with open(filepath, "r") as f:
            return json.load(f)
    except:
        return default

def save_json_file(filepath, data):
    try:
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"⚠️ Could not save {filepath}: {e}")

def get_portfolio(client):
    account = client.get_account()
    portfolio = {}
    for balance in account['balances']:
        free = float(balance['free'])
        if free > 0:
            portfolio[balance['asset']] = free
    return portfolio

def get_top_eur_symbols(client, limit=10):
    try:
        info = client.get_exchange_info()
        valid = {s['symbol'] for s in info['symbols'] if s['status'] == 'TRADING' and s['isSpotTradingAllowed']}
        tickers = client.get_ticker()
        eur_tickers = [
            t for t in tickers
            if t['symbol'] in valid
            and t['symbol'].endswith('EUR')
        ]
        sorted_tickers = sorted(eur_tickers, key=lambda x: float(x['quoteVolume']), reverse=True)
        return [t['symbol'] for t in sorted_tickers[:limit]]
    except Exception as e:
        print(f"⚠️ Could not fetch top symbols: {e}")
        return ["BTCEUR", "ETHEUR", "XRPEUR"]

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50
    deltas = [prices[i+1] - prices[i] for i in range(len(prices)-1)]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def calculate_macd(prices, fast=12, slow=26, signal=9):
    if len(prices) < slow:
        return 0, 0, 0
    def ema(data, period):
        k = 2 / (period + 1)
        ema_val = data[0]
        for price in data[1:]:
            ema_val = price * k + ema_val * (1 - k)
        return ema_val
    fast_ema = ema(prices, fast)
    slow_ema = ema(prices, slow)
    macd_line = fast_ema - slow_ema
    signal_line = ema([macd_line] * signal, signal)
    histogram = macd_line - signal_line
    return round(macd_line, 4), round(signal_line, 4), round(histogram, 4)

def calculate_bollinger_bands(prices, period=20, std_dev=2):
    if len(prices) < period:
        return prices[-1], prices[-1], prices[-1]
    recent = prices[-period:]
    mean = sum(recent) / period
    variance = sum((p - mean) ** 2 for p in recent) / period
    std = variance ** 0.5
    return round(mean + std_dev * std, 4), round(mean, 4), round(mean - std_dev * std, 4)

def calculate_moving_averages(prices):
    def ma(data, period):
        if len(data) < period:
            return None
        return round(sum(data[-period:]) / period, 4)
    return {
        "MA7": ma(prices, 7),
        "MA25": ma(prices, 25),
        "MA99": ma(prices, 99)
    }

def get_market_data(client, symbols):
    market_data = {}
    try:
        info = client.get_exchange_info()
        valid = {s['symbol'] for s in info['symbols'] if s['status'] == 'TRADING'}
        valid_symbols = [s for s in symbols if s in valid]
    except:
        valid_symbols = symbols

    for symbol in valid_symbols:
        try:
            ticker = client.get_ticker(symbol=symbol)
            klines_1h = client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1HOUR, limit=100)
            klines_15m = client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_15MINUTE, limit=100)

            closes_1h = [float(k[4]) for k in klines_1h]
            closes_15m = [float(k[4]) for k in klines_15m]
            volumes_1h = [float(k[5]) for k in klines_1h]

            bb_upper, bb_mid, bb_lower = calculate_bollinger_bands(closes_1h)
            macd, macd_signal, macd_hist = calculate_macd(closes_1h)
            mas = calculate_moving_averages(closes_1h)

            market_data[symbol] = {
                "current_price": float(ticker['lastPrice']),
                "change_24h": float(ticker['priceChangePercent']),
                "volume_24h": float(ticker['quoteVolume']),
                "high_24h": float(ticker['highPrice']),
                "low_24h": float(ticker['lowPrice']),
                "RSI_1h": calculate_rsi(closes_1h),
                "RSI_15m": calculate_rsi(closes_15m),
                "MACD": macd,
                "MACD_signal": macd_signal,
                "MACD_histogram": macd_hist,
                "BB_upper": bb_upper,
                "BB_middle": bb_mid,
                "BB_lower": bb_lower,
                "MA7": mas["MA7"],
                "MA25": mas["MA25"],
                "MA99": mas["MA99"],
                "volume_trend": round(sum(volumes_1h[-6:]) / max(sum(volumes_1h[-12:-6]), 0.001), 2)
            }
        except Exception as e:
            print(f"⚠️ Could not get data for {symbol}: {e}")

    return market_data

def get_crypto_news():
    try:
        response = requests.get(
            "https://www.coindesk.com/arc/outboundfeeds/rss/",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        titles = re.findall(r'<title><!\[CDATA\[(.*?)\]\]></title>', response.text)
        return titles[1:6] if len(titles) > 1 else []
    except Exception as e:
        print(f"⚠️ Could not fetch news: {e}")
        return []

def calculate_portfolio_value(portfolio, market_data, client):
    total = portfolio.get("EUR", 0)
    for coin, amount in portfolio.items():
        if coin == "EUR":
            continue
        symbol = f"{coin}EUR"
        if symbol in market_data:
            total += amount * market_data[symbol]["current_price"]
        else:
            try:
                ticker = client.get_ticker(symbol=symbol)
                total += amount * float(ticker['lastPrice'])
            except:
                pass
    return total

def calculate_win_rate(trade_history):
    closed_trades = [t for t in trade_history if t.get("profit_eur") is not None]
    if len(closed_trades) < 5:
        return None, len(closed_trades)
    wins = sum(1 for t in closed_trades if t.get("profit_eur", 0) > 0)
    return round(wins / len(closed_trades), 2), len(closed_trades)

def determine_strategy(trade_history, portfolio_value, baseline_value):
    win_rate, total_trades = calculate_win_rate(trade_history)
    drawdown = (baseline_value - portfolio_value) / baseline_value if baseline_value > 0 else 0

    if drawdown >= DRAWDOWN_LIMIT:
        return "CONSERVATIVE", f"Portfolio down {drawdown*100:.1f}% from baseline — switching to conservative mode"

    if win_rate is not None and win_rate < WIN_RATE_THRESHOLD:
        return "CONSERVATIVE", f"Win rate {win_rate*100:.1f}% below {WIN_RATE_THRESHOLD*100:.0f}% threshold — switching to conservative mode"

    if win_rate is not None:
        return "AGGRESSIVE", f"Win rate: {win_rate*100:.1f}% ({total_trades} trades)"

    return "AGGRESSIVE", "Not enough trade history yet — using aggressive strategy"

def update_trade_profits(trade_history, market_data):
    for trade in trade_history:
        if trade.get("action") == "BUY" and trade.get("profit_eur") is None:
            symbol = trade.get("symbol")
            if symbol and symbol in market_data:
                entry_price = trade.get("entry_price", 0)
                current_price = market_data[symbol]["current_price"]
                amount_eur = trade.get("amount_eur", 0)
                if entry_price > 0:
                    price_change = (current_price - entry_price) / entry_price
                    trade["current_profit_eur"] = round(amount_eur * price_change, 2)
                    trade["current_profit_pct"] = round(price_change * 100, 2)
    return trade_history

def check_stop_loss(client, portfolio, market_data, trade_history):
    for trade in trade_history:
        if trade.get("action") == "BUY" and trade.get("closed") != True:
            symbol = trade.get("symbol")
            entry_price = trade.get("entry_price", 0)
            if symbol and symbol in market_data and entry_price > 0:
                current_price = market_data[symbol]["current_price"]
                loss_pct = (entry_price - current_price) / entry_price
                if loss_pct >= STOP_LOSS_PERCENT:
                    coin = symbol.replace("EUR", "")
                    amount = portfolio.get(coin, 0)
                    if amount > 0:
                        print(f"🚨 STOP-LOSS triggered for {coin}! Loss: {loss_pct*100:.1f}%")
                        try:
                            client.order_market_sell(symbol=symbol, quantity=round(amount, 6))
                            profit_eur = round(-trade.get("amount_eur", 0) * loss_pct, 2)
                            trade["closed"] = True
                            trade["profit_eur"] = profit_eur
                            trade["close_time"] = str(datetime.now())
                            print(f"✅ Stop-loss executed for {coin} | Loss: €{abs(profit_eur):.2f}")
                        except Exception as e:
                            print(f"❌ ERROR stop-loss {coin}: {e}")

def ask_claude(portfolio, market_data, portfolio_value, baseline_value, news, trade_history, strategy, strategy_reason):
    ai_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    eur_available = portfolio.get("EUR", 0)
    win_rate, total_trades = calculate_win_rate(trade_history)

    portfolio_summary = {}
    sellable_positions = []
    buyable_symbols = list(market_data.keys())

    for coin, amount in portfolio.items():
        if coin == "EUR":
            portfolio_summary[coin] = {"amount": amount, "value_eur": amount}
        else:
            symbol = f"{coin}EUR"
            if symbol in market_data:
                value = amount * market_data[symbol]["current_price"]
                portfolio_summary[coin] = {
                    "amount": amount,
                    "value_eur": round(value, 2),
                    "percent_of_portfolio": round((value/portfolio_value)*100, 1)
                }
                if value >= MIN_TRADE_EUR:
                    sellable_positions.append(f"{coin} (€{value:.2f}) → symbol: {symbol}")

    recent_history = trade_history[-10:] if trade_history else []
    news_text = "\n".join([f"- {n}" for n in news]) if news else "No news available"
    target_value = baseline_value * (1 + TARGET_GROWTH)
    growth_needed = ((target_value - portfolio_value) / portfolio_value) * 100

    if strategy == "CONSERVATIVE":
        strategy_instructions = """
STRATEGY: CONSERVATIVE MODE
- Only make high-confidence trades (confidence >= 8)
- Maximum 15% of portfolio per trade
- Prefer HOLD over uncertain trades
- Focus on capital preservation
- Only trade top 3 volume coins"""
    else:
        strategy_instructions = """
STRATEGY: AGGRESSIVE DAY TRADING MODE
- Target intraday price movements
- Use RSI oversold (<30) for BUY signals
- Use RSI overbought (>70) for SELL signals
- MACD crossovers are strong signals
- Bollinger Band breakouts indicate momentum
- Moving average crossovers (MA7 > MA25) = bullish
- Volume spikes (volume_trend > 1.5) confirm moves
- Maximum 30% of portfolio per trade"""

    prompt = f"""You are an expert crypto day trader. Your goal is to grow the portfolio by 100% every 2 weeks through active intraday trading.

PERFORMANCE:
- Current portfolio value: €{portfolio_value:.2f}
- Baseline value: €{baseline_value:.2f}
- Target value (2 weeks): €{target_value:.2f}
- Growth still needed: {growth_needed:.1f}%
- Win rate: {f"{win_rate*100:.1f}%" if win_rate else "N/A"} ({total_trades} closed trades)
- Current strategy: {strategy} — {strategy_reason}

PORTFOLIO (Available EUR: €{eur_available:.2f}):
{json.dumps(portfolio_summary, indent=2)}

SELLABLE POSITIONS (value >= €{MIN_TRADE_EUR}, can be sold):
{chr(10).join(sellable_positions) if sellable_positions else 'None'}

BUYABLE SYMBOLS (any of these can be bought if EUR is available):
{', '.join(buyable_symbols)}

MARKET DATA WITH TECHNICAL INDICATORS:
{json.dumps(market_data, indent=2)}

LATEST NEWS:
{news_text}

RECENT TRADE HISTORY (last 10):
{json.dumps(recent_history, indent=2) if recent_history else "No history yet"}

{strategy_instructions}

STRICT RULES:
- To SELL: position must be in SELLABLE POSITIONS list
- To BUY: use any symbol from BUYABLE SYMBOLS list
- ALWAYS use full symbol names ending in EUR (e.g. ETHEUR, BTCEUR, ADAEUR)
- Minimum trade: €{MIN_TRADE_EUR}
- If you want to BUY but EUR is insufficient, you MUST first include a SELL of a SELLABLE POSITION to generate EUR — there are no other funding sources
- Total EUR spent on BUYs must never exceed available EUR plus EUR generated by SELLs in this decision list
- SELL orders must always come before BUY orders
- ALWAYS include a confidence score (1-10) for every decision
- If no clear opportunity exists, return a single HOLD

Respond ONLY with a JSON array, no explanation, no markdown:
[
  {{"action": "SELL", "symbol": "ETHEUR", "amount_eur": 50.00, "reason": "...", "confidence": 8}},
  {{"action": "BUY", "symbol": "ADAEUR", "amount_eur": 50.00, "reason": "...", "confidence": 8}}
]"""

    message = ai_client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )

    response_text = message.content[0].text
    match = re.search(r'\[.*\]', response_text, re.DOTALL)
    if match:
        return json.loads(match.group())
    
    # Fallback: maybe Claude returned a single object instead of array
    match_obj = re.search(r'\{.*\}', response_text, re.DOTALL)
    if match_obj:
        return [json.loads(match_obj.group())]
    
    raise ValueError("No JSON array found in response")

def execute_trades(client, decisions, portfolio, portfolio_value, market_data, trade_history):
    for d in decisions:
        if "confidence" not in d:
            d["confidence"] = 0

    sells = [d for d in decisions if d.get("action") == "SELL" and d.get("confidence", 0) >= CONFIDENCE_THRESHOLD]
    buys = [d for d in decisions if d.get("action") == "BUY" and d.get("confidence", 0) >= CONFIDENCE_THRESHOLD]

    for decision in sells:
        symbol = decision.get("symbol")

        # Auto-fix symbol if Claude forgot EUR suffix
        if symbol and not symbol.endswith("EUR"):
            symbol = f"{symbol}EUR"
            decision["symbol"] = symbol

        amount_eur = decision.get("amount_eur")

        if not symbol or not amount_eur:
            continue

        max_allowed = portfolio_value * MAX_TRADE_PERCENT
        if amount_eur > max_allowed:
            amount_eur = max_allowed
            print(f"⚠️ SELL capped at 30%: €{amount_eur:.2f}")

        if amount_eur < MIN_TRADE_EUR:
            print(f"⚠️ SELL {symbol}: below minimum — skipping")
            continue

        coin = symbol.replace("EUR", "")
        coin_value = portfolio.get(coin, 0) * market_data.get(symbol, {}).get("current_price", 0)
        if coin_value < MIN_TRADE_EUR:
            print(f"⚠️ SELL {symbol}: position too small — skipping")
            continue

        try:
            client.order_market_sell(symbol=symbol, quoteOrderQty=round(amount_eur, 2))
            print(f"✅ SELL {symbol}: €{amount_eur:.2f}")

            for trade in trade_history:
                if trade.get("symbol") == symbol and trade.get("action") == "BUY" and not trade.get("closed"):
                    entry_price = trade.get("entry_price", 0)
                    current_price = market_data.get(symbol, {}).get("current_price", 0)
                    if entry_price > 0:
                        profit_pct = (current_price - entry_price) / entry_price
                        profit_eur = round(trade.get("amount_eur", 0) * profit_pct, 2)
                        trade["closed"] = True
                        trade["profit_eur"] = profit_eur
                        trade["close_time"] = str(datetime.now())

            trade_history.append({
                "time": str(datetime.now()),
                "action": "SELL",
                "symbol": symbol,
                "amount_eur": amount_eur,
                "reason": decision.get("reason", ""),
                "closed": True
            })
        except Exception as e:
            print(f"❌ ERROR SELL {symbol}: {e}")

    portfolio = get_portfolio(client)
    eur_available = portfolio.get("EUR", 0)

    for decision in buys:
        symbol = decision.get("symbol")

        # Auto-fix symbol if Claude forgot EUR suffix
        if symbol and not symbol.endswith("EUR"):
            symbol = f"{symbol}EUR"
            decision["symbol"] = symbol

        amount_eur = decision.get("amount_eur")

        if not symbol or not amount_eur:
            continue

        max_allowed = portfolio_value * MAX_TRADE_PERCENT
        if amount_eur > max_allowed:
            amount_eur = max_allowed
            print(f"⚠️ BUY capped at 30%: €{amount_eur:.2f}")

        if amount_eur < MIN_TRADE_EUR:
            print(f"⚠️ BUY {symbol}: below minimum — skipping")
            continue

        if eur_available < amount_eur:
            if eur_available >= MIN_TRADE_EUR:
                print(f"⚠️ BUY {symbol}: using available €{eur_available:.2f}")
                amount_eur = eur_available
            else:
                print(f"⚠️ BUY {symbol}: insufficient EUR — skipping")
                continue

        try:
            client.order_market_buy(symbol=symbol, quoteOrderQty=round(amount_eur, 2))
            current_price = market_data.get(symbol, {}).get("current_price", 0)
            print(f"✅ BUY {symbol}: €{amount_eur:.2f} @ €{current_price:.4f}")
            eur_available -= amount_eur

            trade_history.append({
                "time": str(datetime.now()),
                "action": "BUY",
                "symbol": symbol,
                "amount_eur": amount_eur,
                "entry_price": current_price,
                "reason": decision.get("reason", ""),
                "closed": False,
                "profit_eur": None
            })
        except Exception as e:
            print(f"❌ ERROR BUY {symbol}: {e}")

    return trade_history

def main():
    print(f"🤖 Trading Bot started - {datetime.now()}")
    print(f"🛡️ Stop-loss: {STOP_LOSS_PERCENT*100}% | Max trade: {MAX_TRADE_PERCENT*100}%")
    print(f"⏰ Interval: {INTERVAL_SECONDS//60} minutes")
    print(f"🎯 Target: +{TARGET_GROWTH*100:.0f}% per 2 weeks")

    binance_client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)

    trade_history = load_json_file(TRADE_HISTORY_FILE, [])
    portfolio_baseline = load_json_file(PORTFOLIO_BASELINE_FILE, {})

    while True:
        try:
            print(f"\n{'='*50}")
            print(f"📊 Analysis - {datetime.now()}")

            portfolio = get_portfolio(binance_client)
            top_symbols = get_top_eur_symbols(binance_client, limit=10)

            for coin in portfolio:
                if coin != "EUR":
                    sym = f"{coin}EUR"
                    if sym not in top_symbols:
                        top_symbols.append(sym)

            print(f"📋 Watching {len(top_symbols)} symbols")

            market_data = get_market_data(binance_client, top_symbols)
            portfolio_value = calculate_portfolio_value(portfolio, market_data, binance_client)

            if not portfolio_baseline or "value" not in portfolio_baseline:
                portfolio_baseline = {
                    "value": portfolio_value,
                    "time": str(datetime.now())
                }
                save_json_file(PORTFOLIO_BASELINE_FILE, portfolio_baseline)
                print(f"📌 Baseline set: €{portfolio_value:.2f}")

            baseline_value = portfolio_baseline["value"]

            print(f"💼 Portfolio: €{portfolio_value:.2f} | Baseline: €{baseline_value:.2f}")
            for coin, amount in portfolio.items():
                if coin == "EUR":
                    print(f"   EUR: €{amount:.2f}")
                else:
                    symbol = f"{coin}EUR"
                    if symbol in market_data:
                        value = amount * market_data[symbol]["current_price"]
                        print(f"   {coin}: {amount:.6f} (€{value:.2f})")

            print("📰 Fetching news...")
            news = get_crypto_news()
            print(f"   Found {len(news)} items")

            trade_history = update_trade_profits(trade_history, market_data)
            check_stop_loss(binance_client, portfolio, market_data, trade_history)

            strategy, strategy_reason = determine_strategy(trade_history, portfolio_value, baseline_value)
            print(f"🎯 Strategy: {strategy} — {strategy_reason}")

            win_rate, total_trades = calculate_win_rate(trade_history)
            if win_rate is not None:
                print(f"📈 Win rate: {win_rate*100:.1f}% ({total_trades} trades)")

            decisions = ask_claude(portfolio, market_data, portfolio_value, baseline_value, news, trade_history, strategy, strategy_reason)
            print(f"🧠 Claude suggested {len(decisions)} decision(s):")
            for d in decisions:
                print(f"   → {d['action']} | {d.get('symbol', '-')} | €{d.get('amount_eur', 0):.2f} | Confidence: {d['confidence']}/10")
                print(f"     Reason: {d['reason']}")

            trade_history = execute_trades(binance_client, decisions, portfolio, portfolio_value, market_data, trade_history)
            save_json_file(TRADE_HISTORY_FILE, trade_history)

        except Exception as e:
            print(f"❌ ERROR: {e}")

        print(f"⏰ Next analysis in {INTERVAL_SECONDS//60} minutes...")
        time.sleep(INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
