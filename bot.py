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
TRADE_SYMBOLS = ["ETHEUR", "BTCEUR", "XRPEUR", "TRXEUR"]
MAX_TRADE_PERCENT = 0.30
STOP_LOSS_PERCENT = 0.05
MIN_TRADE_EUR = 10
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
        print(f"⚠️ Δεν ήταν δυνατή η λήψη news: {e}")
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
    ai_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    portfolio_summary = {}
    symbol_map = {
        "ETH": "ETHEUR",
        "BTC": "BTCEUR",
        "XRP": "XRPEUR",
        "TRX": "TRXEUR"
    }

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

    recent_history = trade_history[-5:] if trade_history else []
    news_text = "\n".join([f"- {n}" for n in news]) if news else "Δεν υπάρχουν διαθέσιμα νέα"

    prompt = f"""Είσαι ένας έμπειρος crypto trader. Ανάλυσε όλες τις πληροφορίες και δώσε λίστα εντολών.

PORTFOLIO (Συνολική αξία: €{portfolio_value:.2f} EUR):
{json.dumps(portfolio_summary, indent=2)}

ΔΕΔΟΜΕΝΑ ΑΓΟΡΑΣ (τελευταίες 24 ώρες):
{json.dumps({s: {k: v for k, v in d.items() if k != 'prices_24h'} for s, d in prices.items()}, indent=2)}

ΤΕΛΕΥΤΑΙΑ CRYPTO NEWS:
{news_text}

ΙΣΤΟΡΙΚΟ ΤΕΛΕΥΤΑΙΩΝ TRADES:
{json.dumps(recent_history, indent=2) if recent_history else "Δεν υπάρχει ιστορικό ακόμα"}

ΚΑΝΟΝΕΣ:
- Μέγιστο 30% του portfolio ανά trade
- Ελάχιστο trade: €10 EUR — αν η αξία ενός coin είναι κάτω από €10 μην το πουλάς
- ΠΑΝΤΑ να αναφέρεις amount_eur για κάθε εντολή
- Αν θέλεις να κάνεις BUY αλλά δεν υπάρχει αρκετό EUR, πρόσθεσε πρώτα SELL για να δημιουργηθεί EUR
- Βεβαιώσου ότι το συνολικό ποσό των BUY δεν ξεπερνά το συνολικό ποσό των SELL συν το διαθέσιμο EUR
- Λάβε υπόψη το ιστορικό για να αποφύγεις overtrading
- Λάβε υπόψη τα news για sentiment ανάλυση

Απάντησε ΜΟΝΟ με JSON array χωρίς καμία άλλη εξήγηση. Τα SELL να είναι πάντα πρώτα:
[
  {{"action": "SELL", "symbol": "XRPEUR", "amount_eur": 50.00, "reason": "...", "confidence": 8}},
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
    """Εκτελεί λίστα trades — πρώτα SELL μετά BUY με έλεγχο διαθέσιμου EUR"""

    sells = [d for d in decisions if d.get("action") == "SELL" and d.get("confidence", 0) >= CONFIDENCE_THRESHOLD]
    buys = [d for d in decisions if d.get("action") == "BUY" and d.get("confidence", 0) >= CONFIDENCE_THRESHOLD]
    
    # Παρακολούθηση πόσο EUR δημιουργήθηκε από SELL
    eur_from_sells = 0.0

    # Εκτέλεση SELL πρώτα
    for decision in sells:
        symbol = decision.get("symbol")
        amount_eur = decision.get("amount_eur")

        if not symbol or not amount_eur:
            continue

        max_allowed = portfolio_value * MAX_TRADE_PERCENT
        if amount_eur > max_allowed:
            amount_eur = max_allowed
            print(f"⚠️ SELL ποσό περιορίστηκε στο 30%: €{amount_eur:.2f}")

        if amount_eur < MIN_TRADE_EUR:
            print(f"⚠️ SELL {symbol}: ποσό €{amount_eur:.2f} κάτω από minimum — παράλειψη")
            continue

        coin = symbol.replace("EUR", "")
        coin_value = portfolio.get(coin, 0) * prices[symbol]["current"]
        if coin_value < MIN_TRADE_EUR:
            print(f"⚠️ SELL {symbol}: αξία θέσης €{coin_value:.2f} κάτω από minimum — παράλειψη")
            continue

        try:
            client.order_market_sell(symbol=symbol, quoteOrderQty=round(amount_eur, 2))
            print(f"✅ SELL {symbol}: €{amount_eur:.2f} EUR")
            eur_from_sells += amount_eur
            trade_history.append({
                "time": str(datetime.now()),
                "action": "SELL",
                "symbol": symbol,
                "amount_eur": amount_eur,
                "reason": decision.get("reason", "")
            })
        except Exception as e:
            print(f"❌ Σφάλμα SELL {symbol}: {e}")

    # Ανανέωση portfolio μετά τα SELL
    portfolio = get_portfolio(client)
    eur_available = portfolio.get("EUR", 0)

    # Εκτέλεση BUY με έλεγχο διαθέσιμου EUR
    for decision in buys:
        symbol = decision.get("symbol")
        amount_eur = decision.get("amount_eur")

        if not symbol or not amount_eur:
            continue

        max_allowed = portfolio_value * MAX_TRADE_PERCENT
        if amount_eur > max_allowed:
            amount_eur = max_allowed
            print(f"⚠️ BUY ποσό περιορίστηκε στο 30%: €{amount_eur:.2f}")

        if amount_eur < MIN_TRADE_EUR:
            print(f"⚠️ BUY {symbol}: ποσό €{amount_eur:.2f} κάτω από minimum — παράλειψη")
            continue

        if eur_available < amount_eur:
            print(f"⚠️ BUY {symbol}: ανεπαρκές EUR (€{eur_available:.2f} < €{amount_eur:.2f}) — παράλειψη")
            continue

        try:
            client.order_market_buy(symbol=symbol, quoteOrderQty=round(amount_eur, 2))
            print(f"✅ BUY {symbol}: €{amount_eur:.2f} EUR")
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
            print(f"❌ Σφάλμα BUY {symbol}: {e}")

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

            print(f"💼 Portfolio αξία: €{portfolio_value:.2f} EUR")
            for coin, amount in portfolio.items():
                print(f"   {coin}: {amount:.4f}")

            print("📰 Λήψη crypto news...")
            news = get_crypto_news()
            if news:
                print(f"   Βρέθηκαν {len(news)} νέα")

            check_stop_loss(binance_client, portfolio, prices, entry_prices)

            decisions = ask_claude(portfolio, prices, portfolio_value, news)
            print(f"🧠 Ο Claude πρότεινε {len(decisions)} εντολές:")

            for d in decisions:
                print(f"   → {d['action']} | {d.get('symbol', '-')} | €{d.get('amount_eur', 0):.2f} | Confidence: {d['confidence']}/10")
                print(f"     Λόγος: {d['reason']}")

            execute_trades(binance_client, decisions, portfolio, portfolio_value, entry_prices, prices)

        except Exception as e:
            print(f"❌ Σφάλμα: {e}")

        print(f"⏰ Επόμενη ανάλυση σε {INTERVAL_SECONDS//3600} ώρα...")
        time.sleep(INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
