import anthropic
import time
import os
import json
from binance.client import Client
from datetime import datetime

# API Keys από environment variables
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY")

# Ρυθμίσεις
TRADE_SYMBOLS = ["BETHUSDT", "BTCUSDT", "XRPUSDT", "TRXUSDT"]
MAX_TRADE_PERCENT = 0.30  # Μέγιστο 30% του portfolio ανά trade
STOP_LOSS_PERCENT = 0.05  # Stop-loss στο -5%
MIN_TRADE_USDT = 10       # Ελάχιστο ποσό trade σε USDT
CONFIDENCE_THRESHOLD = 7  # Ελάχιστο confidence για εκτέλεση

def get_portfolio(client):
    """Παίρνει το τρέχον portfolio από το Binance Spot"""
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
    """Παίρνει τρέχουσες τιμές και δεδομένα 24ω για όλα τα ζεύγη"""
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

def calculate_portfolio_value(portfolio, prices):
    """Υπολογίζει τη συνολική αξία του portfolio σε USDT"""
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
    """Ελέγχει αν κάποιο coin έχει φτάσει stop-loss"""
    symbol_map = {
        "BETH": "BETHUSDT",
        "BTC": "BTCUSDT",
        "XRP": "XRPUSDT", 
        "TRX": "TRXUSDT"
    }
    
    for coin, entry_price in entry_prices.items():
        if coin in portfolio and coin in symbol_map:
            symbol = symbol_map[coin]
            current_price = prices[symbol]["current"]
            loss_percent = (entry_price - current_price) / entry_price
            
            if loss_percent >= STOP_LOSS_PERCENT:
                amount = portfolio[coin]
                print(f"🚨 STOP-LOSS triggered για {coin}! Απώλεια: {loss_percent*100:.1f}%")
                try:
                    client.order_market_sell(symbol=symbol, quantity=round(amount, 6))
                    print(f"✅ Stop-loss sell εκτελέστηκε για {coin}")
                    del entry_prices[coin]
                except Exception as e:
                    print(f"❌ Σφάλμα stop-loss για {coin}: {e}")

def ask_claude(portfolio, prices, portfolio_value):
    """Ρωτάει τον Claude για απόφαση trading"""
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
    
    prompt = f"""Είσαι ένας έμπειρος crypto trader. Ανάλυσε το portfolio και τις αγορές και δώσε μία συγκεκριμένη εντολή.

PORTFOLIO (Συνολική αξία: ${portfolio_value:.2f} USDT):
{json.dumps(portfolio_summary, indent=2)}

ΔΕΔΟΜΕΝΑ ΑΓΟΡΑΣ (τελευταίες 24 ώρες):
{json.dumps({s: {k: v for k, v in d.items() if k != 'prices_24h'} for s, d in prices.items()}, indent=2)}

ΚΑΝΟΝΕΣ:
- Μέγιστο 30% του portfolio ανά trade
- Ελάχιστο trade: $10 USDT
- Αν δεν υπάρχει USDT για αγορά, μπορείς να προτείνεις πώληση ενός coin για αγορά άλλου

Απάντησε ΜΟΝΟ με JSON:
{{"action": "BUY" ή "SELL" ή "HOLD", "symbol": "BETHUSDT" ή "BTCUSDT" ή "XRPUSDT" ή "TRXUSDT" ή null, "amount_usdt": ποσό σε USDT ή null, "reason": "σύντομη εξήγηση", "confidence": 1-10}}"""

    message = ai_client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    
    response_text = message.content[0].text
    import re
match = re.search(r'\{.*\}', response_text, re.DOTALL)
if match:
    return json.loads(match.group())
else:
    raise ValueError("No JSON found in response")

def execute_trade(client, decision, portfolio, portfolio_value):
    """Εκτελεί το trade με όλους τους ελέγχους ασφαλείας"""
    action = decision.get("action")
    symbol = decision.get("symbol")
    amount_usdt = decision.get("amount_usdt")
    
    if action == "HOLD" or not symbol:
        print("⏸️ HOLD - Δεν εκτελείται trade")
        return None
    
    # Έλεγχος μέγιστου ποσού
    max_allowed = portfolio_value * MAX_TRADE_PERCENT
    if amount_usdt > max_allowed:
        amount_usdt = max_allowed
        print(f"⚠️ Ποσό περιορίστηκε στο 30%: ${amount_usdt:.2f}")
    
    # Έλεγχος ελάχιστου ποσού
    if amount_usdt < MIN_TRADE_USDT:
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
            return order
            
        elif action == "SELL":
            coin = symbol.replace("USDT", "")
            coin_amount = portfolio.get(coin, 0)
            
            if coin_amount <= 0:
                print(f"⚠️ Δεν υπάρχει {coin} για πώληση")
                return None
            
            order = client.order_market_sell(symbol=symbol, quoteOrderQty=round(amount_usdt, 2))
            print(f"✅ SELL {symbol}: ${amount_usdt:.2f} USDT")
            return order
            
    except Exception as e:
        print(f"❌ Σφάλμα trade: {e}")
        return None

def main():
    print(f"🤖 Trading Bot ξεκίνησε - {datetime.now()}")
    print(f"📋 Coins: {TRADE_SYMBOLS}")
    print(f"🛡️ Stop-loss: {STOP_LOSS_PERCENT*100}% | Max trade: {MAX_TRADE_PERCENT*100}%")
    
    binance_client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)
    entry_prices = {}  # Καταγραφή τιμών αγοράς για stop-loss
    
    while True:
        try:
            print(f"\n{'='*50}")
            print(f"📊 Ανάλυση - {datetime.now()}")
            
            # Παίρνουμε δεδομένα
            portfolio = get_portfolio(binance_client)
            prices = get_prices(binance_client)
            portfolio_value = calculate_portfolio_value(portfolio, prices)
            
            print(f"💼 Portfolio αξία: ${portfolio_value:.2f} USDT")
            for coin, amount in portfolio.items():
                print(f"   {coin}: {amount:.4f}")
            
            # Έλεγχος stop-loss
            check_stop_loss(binance_client, portfolio, prices, entry_prices)
            
            # Απόφαση από Claude
            decision = ask_claude(portfolio, prices, portfolio_value)
            print(f"🧠 Απόφαση: {decision['action']} | {decision.get('symbol', '-')} | Confidence: {decision['confidence']}/10")
            print(f"📝 Λόγος: {decision['reason']}")
            
            # Εκτέλεση αν confidence αρκετό
            if decision['confidence'] >= CONFIDENCE_THRESHOLD:
                order = execute_trade(binance_client, decision, portfolio, portfolio_value)
                if order and decision['action'] == 'BUY':
                    coin = decision['symbol'].replace("USDT", "")
                    entry_prices[coin] = prices[decision['symbol']]["current"]
            else:
                print(f"⚠️ Confidence {decision['confidence']}/10 < {CONFIDENCE_THRESHOLD} - Παράλειψη")
            
        except Exception as e:
            print(f"❌ Σφάλμα: {e}")
        
        print(f"⏰ Επόμενη ανάλυση σε 1 ώρα...")
        time.sleep(3600)

if __name__ == "__main__":
    main()
