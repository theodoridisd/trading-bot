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
TRADE_SYMBOLS = ["ETHEUR", "BTCEUR", "XRPEUR", "TRXEUR"]
MAX_TRADE_PERCENT = 0.30
STOP_LOSS_PERCENT = 0.05
MIN_TRADE_EUR = 1
CONFIDENCE_THRESHOLD = 7
INTERVAL_SECONDS = 3600

trade_history = []

def get_portfolio(client):
    account = client.get_account()
    portfolio = {}
    relevant_coins = ["ETH", "BTC", "XRP", "TRX", "EUR"]
    for balance in account['balances']:
        asset = balance['asset']
        free = float(balance['free'])
        if asset in relevant_coins and free > 0:
            portfolio[asset] = free
    return portfolio

def get_prices(client):
    prices = {}
    for symbol in TRADE_SYMBOLS:
        ticker = client.get_ticker(symbol=symbol)
        klines = client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1HOUR, limit=24)
        closing_prices = [float(k[4]) for k in klines]
        prices[symbol] = {
            "current": float(ticker['lastPrice']),
            "change_24h": float(ticker['priceChangePercent']),
            "volume_24h": float(ticker['volume']),
            "high_24h": float(ticker['highPrice']),
            "low_24h": float(ticker['lowPrice']),
            "prices_24h": closing_prices
        }
    return prices

def get_crypto_news():
    try:
        response = requests.get(
            "https://www.coindesk.com/arc/outboundfeeds/rss/",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        titles = re.findall(r'<title><!\[CDATA\[(.*?)\]\]></title>', response.text)
        news = titles[1:6] if len(titles) > 1 else []
        return news
    except Exception as e:
        print(f"⚠️ Could not fetch news: {e}")
        return []

def calculate_portfolio_value(portfolio, prices):
    total = portfolio.get("EUR", 0)
    symbol_map = {
        "ETH": "ETHEUR",
        "BTC": "BTCEUR",
        "XRP": "XRPEUR",
        "TRX": "TRXEUR"
    }
    for coin, amount in portfolio.items():
        if coin != "EUR" and coin in symbol_map:
            symbol = symbol_map[coin]
            if symbol in prices:
                total += amount * prices[symbol]["current"]
    return total

def check_stop_loss(client, portfolio, prices, entry_prices):
    symbol_map = {
        "ETH": "ETHEUR",
        "BTC": "BTCEUR",
        "XRP": "XRPEUR",
        "TRX": "TRXEUR"
    }
    for coin in list(entry_prices.keys()):
        if coin in portfolio and coin in symbol_map:
            symbol = symbol_map[coin]
            current_price = prices[symbol]["current"]
            entry_price = entry_prices[coin]
            loss_percent = (entry_price - current_price) / entry_price
            if loss_percent >= STOP_LOSS_PERCENT:
                print(f"🚨 STOP-LOSS triggered for {coin}! Loss: {loss_percent*100:.1f}%")
                try:
                    client.order_market_sell(symbol=symbol, quantity=round(portfolio[coin], 6))
                    print(f"✅ Stop-loss sell executed for {coin}")
                    trade_history.append({
                        "time": str(datetime.now()),
                        "action": "SELL (STOP-LOSS)",
                        "symbol": symbol,
                        "reason": f"Stop-loss triggered at {loss_percent*100:.1f}% loss"
                    })
                    del entry_prices[coin]
                except Exception as e:
                    print(f"❌ ERROR stop-loss for {coin}: {e}")

def ask_claude(portfolio, prices, portfolio_value, news):
    ai_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    symbol_map = {
        "ETH": "ETHEUR",
        "BTC": "BTCEUR",
        "XRP": "XRPEUR",
        "TRX": "TRXEUR"
    }

    portfolio_summary = {}
    tradeable_positions = []

    for coin, amount in portfolio.items():
        if coin == "EUR":
            portfolio_summary[coin] = {"amount": amount, "value_eur": amount}
        elif coin in symbol_map:
            symbol = symbol_map[coin]
            value = amount * prices[symbol]["current"]
            portfolio_summary[coin] = {
                "amount": amount,
                "value_eur": round(value, 2),
                "percent_of_portfolio": round((value/portfolio_value)*100, 1)
            }
            if value >= MIN_TRADE_EUR:
                tradeable_positions.append(f"{coin} (€{value:.2f})")

    recent_history = trade_history[-5:] if trade_history else []
    news_text = "\n".join([f"- {n}" for n in news]) if news else "No news available"
    eur_available = portfolio.get("EUR", 0)

    prompt = f"""You are an expert crypto trader managing a portfolio. Analyze all information and return a list of trading decisions.

PORTFOLIO (Total value: €{portfolio_value:.2f} EUR, Available EUR: €{eur_available:.2f}):
{json.dumps(portfolio_summary, indent=2)}

TRADEABLE POSITIONS (value >= €{MIN_TRADE_EUR}):
{', '.join(tradeable_positions) if tradeable_positions else 'None'}

MARKET DATA (last 24 hours):
{json.dumps({s: {k: v for k, v in d.items() if k != 'prices_24h'} for s, d in prices.items()}, indent=2)}

LATEST CRYPTO NEWS:
{news_text}

LAST 5 TRADES HISTORY:
{json.dumps(recent_history, indent=2) if recent_history else "No trade history yet"}

STRICT RULES:
- You can ONLY trade positions listed under TRADEABLE POSITIONS — ignore positions worth less than €{MIN_TRADE_EUR}
- Maximum 30% of total portfolio value per trade
- Minimum trade amount: €{MIN_TRADE_EUR} EUR
- Available EUR for buying: €{eur_available:.2f}
- If you want to BUY, you MUST ensure sufficient EUR exists. If not, you MUST first include a SELL of one of the TRADEABLE POSITIONS to generate the necessary EUR. There are no other funding sources — only what is in the portfolio.
- The total EUR spent on BUY orders must never exceed the sum of available EUR plus EUR generated by SELL orders in the same decision list
- SELL orders must always appear before BUY orders in the list
- Use trade history to avoid overtrading
- Use news for sentiment analysis
- If no good opportunity exists, return a single HOLD

Respond ONLY with a JSON array, no explanation, no markdown:
[
  {{"action": "SELL", "symbol": "ETHEUR", "amount_eur": 50.00, "reason": "...", "confidence": 8}},
  {{"action": "BUY", "symbol": "BTCEUR", "amount_eur": 50.00, "reason": "...", "confidence": 8}}
]"""

    message = ai_client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )

    response_text = message.content[0].text
    match = re.search(r'\[.*\]', response_text, re.DOTALL)
    if match:
        return json.loads(match.group())
    else:
        raise ValueError("No JSON array found in response")

def execute_trades(client, decisions, portfolio, portfolio_value, entry_prices, prices):
    sells = [d for d in decisions if d.get("action") == "SELL" and d.get("confidence", 0) >= CONFIDENCE_THRESHOLD]
    buys = [d for d in decisions if d.get("action") == "BUY" and d.get("confidence", 0) >= CONFIDENCE_THRESHOLD]

    # Execute SELLs first
    for decision in sells:
        symbol = decision.get("symbol")
        amount_eur = decision.get("amount_eur")

        if not symbol or not amount_eur:
            continue

        max_allowed = portfolio_value * MAX_TRADE_PERCENT
        if amount_eur > max_allowed:
            amount_eur = max_allowed
            print(f"⚠️ SELL amount capped at 30%: €{amount_eur:.2f}")

        if amount_eur < MIN_TRADE_EUR:
            print(f"⚠️ SELL {symbol}: amount €{amount_eur:.2f} below minimum — skipping")
            continue

        coin = symbol.replace("EUR", "")
        coin_value = portfolio.get(coin, 0) * prices[symbol]["current"]
        if coin_value < MIN_TRADE_EUR:
            print(f"⚠️ SELL {symbol}: position value €{coin_value:.2f} below minimum — skipping")
            continue

        try:
            client.order_market_sell(symbol=symbol, quoteOrderQty=round(amount_eur, 2))
            print(f"✅ SELL executed {symbol}: €{amount_eur:.2f} EUR")
            trade_history.append({
                "time": str(datetime.now()),
                "action": "SELL",
                "symbol": symbol,
                "amount_eur": amount_eur,
                "reason": decision.get("reason", "")
            })
        except Exception as e:
            print(f"❌ ERROR SELL {symbol}: {e}")

    # Refresh portfolio after SELLs
    portfolio = get_portfolio(client)
    eur_available = portfolio.get("EUR", 0)

    # Execute BUYs
    for decision in buys:
        symbol = decision.get("symbol")
        amount_eur = decision.get("amount_eur")

        if not symbol or not amount_eur:
            continue

        max_allowed = portfolio_value * MAX_TRADE_PERCENT
        if amount_eur > max_allowed:
            amount_eur = max_allowed
            print(f"⚠️ BUY amount capped at 30%: €{amount_eur:.2f}")

        if amount_eur < MIN_TRADE_EUR:
            print(f"⚠️ BUY {symbol}: amount €{amount_eur:.2f} below minimum — skipping")
            continue

        if eur_available < amount_eur:
            if eur_available >= MIN_TRADE_EUR:
                print(f"⚠️ BUY {symbol}: insufficient EUR — buying with available €{eur_available:.2f}")
                amount_eur = eur_available
            else:
                print(f"⚠️ BUY {symbol}: insufficient EUR (€{eur_available:.2f}) — skipping")
                continue

        try:
            client.order_market_buy(symbol=symbol, quoteOrderQty=round(amount_eur, 2))
            print(f"✅ BUY executed {symbol}: €{amount_eur:.2f} EUR")
            eur_available -= amount_eur
            coin = symbol.replace("EUR", "")
            entry_prices[coin] = prices[symbol]["current"]
            trade_history.append({
                "time": str(datetime.now()),
                "action": "BUY",
                "symbol": symbol,
                "amount_eur": amount_eur,
                "reason": decision.get("reason", "")
            })
        except Exception as e:
            print(f"❌ ERROR BUY {symbol}: {e}")

def main():
    print(f"🤖 Trading Bot started - {datetime.now()}")
    print(f"📋 Coins: {TRADE_SYMBOLS}")
    print(f"🛡️ Stop-loss: {STOP_LOSS_PERCENT*100}% | Max trade: {MAX_TRADE_PERCENT*100}%")

    binance_client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)
    entry_prices = {}

    while True:
        try:
            print(f"\n{'='*50}")
            print(f"📊 Analysis - {datetime.now()}")

            portfolio = get_portfolio(binance_client)
            prices = get_prices(binance_client)
            portfolio_value = calculate_portfolio_value(portfolio, prices)

            print(f"💼 Portfolio value: €{portfolio_value:.2f} EUR")
            for coin, amount in portfolio.items():
                print(f"   {coin}: {amount:.4f}")

            print("📰 Fetching crypto news...")
            news = get_crypto_news()
            if news:
                print(f"   Found {len(news)} news items")

            check_stop_loss(binance_client, portfolio, prices, entry_prices)

            decisions = ask_claude(portfolio, prices, portfolio_value, news)
            print(f"🧠 Claude suggested {len(decisions)} decision(s):")

            for d in decisions:
                print(f"   → {d['action']} | {d.get('symbol', '-')} | €{d.get('amount_eur', 0):.2f} | Confidence: {d['confidence']}/10")
                print(f"     Reason: {d['reason']}")

            execute_trades(binance_client, decisions, portfolio, portfolio_value, entry_prices, prices)

        except Exception as e:
            print(f"❌ ERROR: {e}")

        print(f"⏰ Next analysis in {INTERVAL_SECONDS//3600} hour(s)...")
        time.sleep(INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
