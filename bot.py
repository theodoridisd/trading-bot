import anthropic
import time
import os
from binance.client import Client
from datetime import datetime

# API Keys από environment variables (ασφαλής τρόπος)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY")

# Ρυθμίσεις
SYMBOL = "BTCUSDT"  # Ζεύγος που θέλουμε να κάνουμε trade
TRADE_AMOUNT = 10   # Ποσό σε USDT ανά trade

def get_market_data(client):
    """Παίρνει δεδομένα αγοράς από Binance"""
    klines = client.get_klines(symbol=SYMBOL, interval=Client.KLINE_INTERVAL_1HOUR, limit=24)
    prices = [float(k[4]) for k in klines]  # Closing prices
    current_price = prices[-1]
    price_24h_ago = prices[0]
    change_24h = ((current_price - price_24h_ago) / price_24h_ago) * 100
    
    volume = sum(float(k[5]) for k in klines)
    
    return {
        "symbol": SYMBOL,
        "current_price": current_price,
        "change_24h": round(change_24h, 2),
        "volume_24h": round(volume, 2),
        "prices_24h": prices
    }

def ask_claude(market_data):
    """Ρωτάει τον Claude τι να κάνει"""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    
    prompt = f"""Είσαι ένας έμπειρος crypto trader. Ανάλυσε τα παρακάτω δεδομένα και απόφασισε αν πρέπει να κάνω BUY, SELL ή HOLD.

Δεδομένα αγοράς για {market_data['symbol']}:
- Τρέχουσα τιμή: ${market_data['current_price']}
- Μεταβολή 24ω: {market_data['change_24h']}%
- Όγκος 24ω: {market_data['volume_24h']} BTC
- Τιμές τελευταίων 24 ωρών: {market_data['prices_24h']}

Απάντησε ΜΟΝΟ με ένα JSON στην εξής μορφή:
{{"action": "BUY" ή "SELL" ή "HOLD", "reason": "σύντομη εξήγηση", "confidence": 1-10}}"""

    message = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )
    
    import json
    response_text = message.content[0].text
    return json.loads(response_text)

def execute_trade(binance_client, action, amount):
    """Εκτελεί το trade στο Binance"""
    try:
        if action == "BUY":
            order = binance_client.order_market_buy(
                symbol=SYMBOL,
                quoteOrderQty=amount
            )
            print(f"✅ BUY order executed: {order}")
        elif action == "SELL":
            order = binance_client.order_market_sell(
                symbol=SYMBOL,
                quoteOrderQty=amount
            )
            print(f"✅ SELL order executed: {order}")
        else:
            print("⏸️ HOLD - Δεν εκτελείται trade")
    except Exception as e:
        print(f"❌ Σφάλμα στο trade: {e}")

def main():
    print(f"🤖 Trading Bot ξεκίνησε - {datetime.now()}")
    
    binance_client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)
    
    while True:
        try:
            print(f"\n📊 Ανάλυση αγοράς - {datetime.now()}")
            
            # Παίρνουμε δεδομένα
            market_data = get_market_data(binance_client)
            print(f"💰 Τιμή: ${market_data['current_price']} | Μεταβολή: {market_data['change_24h']}%")
            
            # Ρωτάμε τον Claude
            decision = ask_claude(market_data)
            print(f"🧠 Claude απόφαση: {decision['action']} (confidence: {decision['confidence']}/10)")
            print(f"📝 Λόγος: {decision['reason']}")
            
            # Εκτελούμε trade μόνο αν confidence >= 7
            if decision['confidence'] >= 7:
                execute_trade(binance_client, decision['action'], TRADE_AMOUNT)
            else:
                print(f"⚠️ Χαμηλό confidence ({decision['confidence']}/10) - Παράλειψη trade")
            
        except Exception as e:
            print(f"❌ Σφάλμα: {e}")
        
        # Περιμένουμε 1 ώρα
        print(f"⏰ Επόμενη ανάλυση σε 1 ώρα...")
        time.sleep(3600)

if __name__ == "__main__":
    main()
