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

# Ρυθμίσεις
TRADE_SYMBOLS = ["BETHUSDT", "BTCUSDT", "XRPUSDT", "TRXUSDT"]
MAX_TRADE_PERCENT = 0.30
STOP_LOSS_PERCENT = 0.05
MIN_TRADE_USDT = 10
CONFIDENCE_THRESHOLD = 7
INTERVAL_SECONDS = 3600  # 1 ώρα

# Ιστορικό trades (στη μνήμη)
trade_history = []

def get_portfolio(client):
    account = client.get_account()
    portfolio = {}
    relevant_coins = ["BETH", "BTC", "XRP", "TRX", "USDT"]
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
    """Παίρνει crypto news από CoinDesk RSS"""
    try:
        response = requests.get(
            "https://www.coindesk.com/arc/outboundfeeds/rss/",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        # Απλή εξαγωγή τίτλων από RSS
        titles = re.findall(r'<title><!\[CDATA\[(.*?)\]\]></title>', response.text)
        # Παίρνουμε τους πρώτους 5 τίτλους (εκτός του πρώτου που είναι το όνομα του site)
        news = titles[1:6] if len(titles) > 1 else []
        return news
    except Exception as e:
        print(f"⚠️ Δεν ήταν δυνατή η λήψη news: {e}")
        return []

def calculate_portfolio_value(portfolio, prices):
    total = portfolio.get("USDT", 0)
    symbol_map = {
        "BETH": "BETHUSDT",
        "BTC": "BTCUSDT",
        "XRP": "XRPUSDT",
        "TRX": "TRXUSDT"
    }
    for coin, amount in portfolio.items():
        if coin != "USDT" and coin in symbol_map:
            symbol = symbol_map[coin]
            if symbol in prices:
                total += amount * prices[symbol]["current"]
    return total

def check_stop_loss(client, portfolio, prices, entry_prices):
    symbol_map = {
        "BETH": "BETHUSDT",
        "BTC": "BTCUSDT",
        "XRP": "XRPUSDT",
        "TRX": "TRXUSDT"
    }
    for coin in list(entry_prices.keys()):
        if coin in portfolio and coin in symbol_map:
            symbol = symbol_map[coin]
            current_price = prices[symbol]["current"]
            entry_price = entry_prices[coin]
            loss_percent = (entry_price - current_price) / entry_price
            if loss_percent >= STOP_LOSS_PERCENT:
                amount = portfolio[coin]
                print(f"🚨 STOP-LOSS triggered για {coin}! Απώλεια: {loss_percent*100:.1f}%")
                try:
                    client.order_market_sell(symbol=symbol, quantity=round(amount, 6))
                    print(f"✅ Stop-loss sell εκτελέστηκε για {coin}")
                    trade_history.append({
                        "time": str(datetime.now()),
                        "action": "SELL (STOP-LOSS)",
                        "symbol": symbol,
                        "reason": f"Stop-loss triggered at {loss_percent*100:.1f}% loss"
                    })
                    del entry_prices[coin]
                except Exception as e:
                    print(f"❌ Σφάλμα stop-loss για {coin}: {e}")

def ask_claude(portfolio, prices, portfolio_value, news):
    """Ρωτάει τον Claude με ιστορικό trades και news"""
    ai_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    portfolio_summary = {}
    symbol_map = {
        "BETH": "BETHUSDT",
        "BTC": "BTCUSDT",
        "XRP": "XRPUSDT",
        "TRX": "TRXUSDT"
    }

    for coin, amount in portfolio.items():
        if coin == "USDT":
            portfolio_summary[coin] = {"amount": amount, "value_usdt": amount}
        elif coin in symbol_map:
            symbol = symbol_map[coin]
            value = amount * prices[symbol]["current"]
            portfolio_summary[coin] = {
                "amount": amount,
                "value_usdt": round(value, 2),
                "percent_of_portfolio": round((value/portfolio_value)*100, 1)
            }

    # Τελευταία 5 trades για context
    recent_history = trade_history[-5:] if trade_history else []

    news_text = "\n".join([f"- {n}" for n in news]) if news else "Δεν υπάρχουν διαθέσιμα νέα"

    prompt = f"""Είσαι ένας έμπειρος crypto trader. Ανάλυσε όλες τις πληροφορίες και δώσε μία συγκεκριμένη εντολή.

PORTFOLIO (Συνολική αξία: ${portfolio_value:.2f} USDT):
{json.dumps(portfolio_summary, indent=2)}

ΔΕΔΟΜΕΝΑ ΑΓΟΡΑΣ (τελευταίες 24 ώρες):
{json.dumps({s: {k: v for k, v in d.items() if k != 'prices_24h'} for s, d in prices.items()}, indent=2)}

ΤΕΛΕΥΤΑΙΑ CRYPTO NEWS:
{news_text}

ΙΣΤΟΡΙΚΟ ΤΕΛΕΥΤΑΙΩΝ TRADES:
{json.dumps(recent_history, indent=2) if recent_history else "Δεν υπάρχει ιστορικό ακόμα"}

ΚΑΝΟΝΕΣ:
- Μέγιστο 30% του portfolio ανά trade
- Ελάχιστο trade: $10 USDT
- Λάβε υπόψη το ιστορικό για να αποφύγεις overtrading
- Λάβε υπόψη τα news για sentiment ανάλυση

Απάντησε ΜΟΝΟ με JSON χωρίς καμία άλλη εξήγηση:
{{"action": "BUY" ή "SELL" ή "HOLD", "symbol": "BETHUSDT" ή "BTCUSDT" ή "XRPUSDT" ή "TRXUSDT" ή null, "amount_usdt": ποσό σε USDT ή null, "reason": "σύντομη εξήγηση", "confidence": 1-10}}"""

    message = ai_client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )

    response_text = message.content[0].text
    match = re.search(r'\{.*\}', response_text, re.DOTALL)
    if match:
        return json.loads(match.group())
    else:
        raise ValueError("No JSON found in response")

def execute_trade(client, decision, portfolio, portfolio_value):
    action = decision.get("action")
    symbol = decision.get("symbol")
    amount_usdt = decision.get("amount_usdt")

    if action == "HOLD" or not symbol:
        print("⏸️ HOLD - Δεν εκτελείται trade")
        trade_history.append({
            "time": str(datetime.now()),
            "action": "HOLD",
            "symbol": None,
            "reason": decision.get("reason", "")
        })
        return None

    max_allowed = portfolio_value * MAX_TRADE_PERCENT
    if amount_usdt and amount_usdt > max_allowed:
        amount_usdt = max_allowed
        print(f"⚠️ Ποσό περιορίστηκε στο 30%: ${amount_usdt:.2f}")

    if not amount_usdt or amount_usdt < MIN_TRADE_USDT:
        print(f"⚠️ Ποσό κάτω από minimum (${MIN_TRADE_USDT}) - Παράλειψη")
        return None

    try:
        if action == "BUY":
            usdt_available = portfolio.get("USDT", 0)
            if usdt_available < amount_usdt:
                print(f"⚠️ Ανεπαρκές USDT ({usdt_available:.2f}) - Παράλειψη")
                return None
            order = client.order_market_buy(symbol=symbol, quoteOrderQty=round(amount_usdt, 2))
            print(f"✅ BUY {symbol}: ${amount_usdt:.2f} USDT")
            trade_history.append({
                "time": str(datetime.now()),
                "action": "BUY",
                "symbol": symbol,
                "amount_usdt": amount_usdt,
                "reason": decision.get("reason", "")
            })
            return order

        elif action == "SELL":
            order = client.order_market_sell(symbol=symbol, quoteOrderQty=round(amount_usdt, 2))
            print(f"✅ SELL {symbol}: ${amount_usdt:.2f} USDT")
            trade_history.append({
                "time": str(datetime.now()),
                "action": "SELL",
                "symbol": symbol,
                "amount_usdt": amount_usdt,
                "reason": decision.get("reason", "")
            })
            return order

    except Exception as e:
        print(f"❌ Σφάλμα trade: {e}")
        return None

def main():
    print(f"🤖 Trading Bot ξεκίνησε - {datetime.now()}")
    print(f"📋 Coins: {TRADE_SYMBOLS}")
    print(f"🛡️ Stop-loss: {STOP_LOSS_PERCENT*100}% | Max trade: {MAX_TRADE_PERCENT*100}%")

    binance_client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)
    entry_prices = {}

    while True:
        try:
            print(f"\n{'='*50}")
            print(f"📊 Ανάλυση - {datetime.now()}")

            portfolio = get_portfolio(binance_client)
            prices = get_prices(binance_client)
            portfolio_value = calculate_portfolio_value(portfolio, prices)

            print(f"💼 Portfolio αξία: ${portfolio_value:.2f} USDT")
            for coin, amount in portfolio.items():
                print(f"   {coin}: {amount:.4f}")

            # News
            print("📰 Λήψη crypto news...")
            news = get_crypto_news()
            if news:
                print(f"   Βρέθηκαν {len(news)} νέα")

            # Stop-loss
            check_stop_loss(binance_client, portfolio, prices, entry_prices)

            # Απόφαση
            decision = ask_claude(portfolio, prices, portfolio_value, news)
            print(f"🧠 Απόφαση: {decision['action']} | {decision.get('symbol', '-')} | Confidence: {decision['confidence']}/10")
            print(f"📝 Λόγος: {decision['reason']}")

            # Εκτέλεση
            if decision['confidence'] >= CONFIDENCE_THRESHOLD:
                order = execute_trade(binance_client, decision, portfolio, portfolio_value)
                if order and decision['action'] == 'BUY':
                    coin = decision['symbol'].replace("USDT", "")
                    entry_prices[coin] = prices[decision['symbol']]["current"]
            else:
                print(f"⚠️ Confidence {decision['confidence']}/10 < {CONFIDENCE_THRESHOLD} - Παράλειψη")

        except Exception as e:
            print(f"❌ Σφάλμα: {e}")

        print(f"⏰ Επόμενη ανάλυση σε {INTERVAL_SECONDS//3600} ώρα...")
        time.sleep(INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
