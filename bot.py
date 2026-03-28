import anthropic
import time
import os
import json
import re
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from binance.client import Client
from datetime import datetime, timedelta

# API Keys
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY")
EMAIL_SENDER = os.environ.get("EMAIL_SENDER")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.environ.get("EMAIL_RECEIVER")

# Settings
MIN_TRADE_EUR = 1
MAX_TRADE_PERCENT = 0.30
STOP_LOSS_PERCENT = 0.05
TAKE_PROFIT_PERCENT = 0.08
CONFIDENCE_THRESHOLD = 7
INTERVAL_SECONDS = 3600 # 1 hour
DRAWDOWN_LIMIT = 0.30
TARGET_GROWTH = 0.20
MAX_CONSECUTIVE_HOLDS = 6  # 6 hours before relaxing criteria
TRADE_HISTORY_FILE = "trade_history.json"
PORTFOLIO_BASELINE_FILE = "portfolio_baseline.json"
DAILY_STATS_FILE = "daily_stats.json"

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

def load_from_github(filename):
    """Load JSON file from GitHub repository"""
    try:
        token = os.environ.get("GITHUB_TOKEN")
        repo = os.environ.get("GITHUB_REPO")
        url = f"https://api.github.com/repos/{repo}/contents/{filename}"
        headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            import base64
            content = base64.b64decode(response.json()["content"]).decode("utf-8")
            return json.loads(content)
        return None
    except Exception as e:
        print(f"⚠️ Could not load {filename} from GitHub: {e}")
        return None

def save_to_github(filename, data):
    """Save JSON file to GitHub repository"""
    try:
        token = os.environ.get("GITHUB_TOKEN")
        repo = os.environ.get("GITHUB_REPO")
        url = f"https://api.github.com/repos/{repo}/contents/{filename}"
        headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}

        content = json.dumps(data, indent=2)
        import base64
        encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")

        # Get current SHA if file exists
        sha = None
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            sha = response.json()["sha"]

        payload = {
            "message": f"Update {filename}",
            "content": encoded
        }
        if sha:
            payload["sha"] = sha

        requests.put(url, headers=headers, json=payload)
    except Exception as e:
        print(f"⚠️ Could not save {filename} to GitHub: {e}")

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
    if len(prices) < slow + signal:
        return 0, 0, 0

    def ema_series(data, period):
        k = 2 / (period + 1)
        result = [data[0]]
        for price in data[1:]:
            result.append(price * k + result[-1] * (1 - k))
        return result

    fast_ema = ema_series(prices, fast)
    slow_ema = ema_series(prices, slow)
    macd_line_series = [f - s for f, s in zip(fast_ema, slow_ema)]
    signal_line = ema_series(macd_line_series, signal)[-1]
    macd_val = macd_line_series[-1]
    histogram = macd_val - signal_line

    # Normalize as percentage of current price to avoid scale issues
    current_price = prices[-1]
    if current_price > 0:
        macd_val = round((macd_val / current_price) * 100, 4)
        signal_line = round((signal_line / current_price) * 100, 4)
        histogram = round((histogram / current_price) * 100, 4)

    return macd_val, signal_line, histogram

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

    if win_rate is not None:
        return "AGGRESSIVE", f"Win rate: {win_rate*100:.1f}% ({total_trades} trades) — target is 60%+"

    return "AGGRESSIVE", "Not enough trade history yet — using aggressive strategy"

def count_consecutive_holds(trade_history):
    count = 0
    for trade in reversed(trade_history):
        if trade.get("action") == "HOLD":
            count += 1
        else:
            break
    return count

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

def check_stop_loss_and_take_profit(client, portfolio, market_data, trade_history):
    for trade in trade_history:
        if trade.get("action") == "BUY" and trade.get("closed") != True:
            symbol = trade.get("symbol")
            entry_price = trade.get("entry_price", 0)
            if symbol and symbol in market_data and entry_price > 0:
                current_price = market_data[symbol]["current_price"]
                coin = symbol.replace("EUR", "")
                amount = portfolio.get(coin, 0)

                if amount <= 0:
                    continue

                loss_pct = (entry_price - current_price) / entry_price
                gain_pct = (current_price - entry_price) / entry_price

                # Stop-loss
                if loss_pct >= STOP_LOSS_PERCENT:
                    print(f"🚨 STOP-LOSS triggered for {coin}! Loss: {loss_pct*100:.1f}%")
                    try:
                        client.order_market_sell(symbol=symbol, quantity=round(amount, 6))
                        profit_eur = round(-trade.get("amount_eur", 0) * loss_pct, 2)
                        trade["closed"] = True
                        trade["profit_eur"] = profit_eur
                        trade["close_time"] = str(datetime.now())
                        trade["close_reason"] = "STOP-LOSS"
                        print(f"✅ Stop-loss executed for {coin} | Loss: €{abs(profit_eur):.2f}")
                    except Exception as e:
                        print(f"❌ ERROR stop-loss {coin}: {e}")

                # Take-profit
                elif gain_pct >= TAKE_PROFIT_PERCENT:
                    print(f"🎯 TAKE-PROFIT triggered for {coin}! Gain: {gain_pct*100:.1f}%")
                    try:
                        client.order_market_sell(symbol=symbol, quantity=round(amount, 6))
                        profit_eur = round(trade.get("amount_eur", 0) * gain_pct, 2)
                        trade["closed"] = True
                        trade["profit_eur"] = profit_eur
                        trade["close_time"] = str(datetime.now())
                        trade["close_reason"] = "TAKE-PROFIT"
                        print(f"✅ Take-profit executed for {coin} | Profit: €{profit_eur:.2f}")
                    except Exception as e:
                        print(f"❌ ERROR take-profit {coin}: {e}")

def send_daily_report(trade_history, portfolio_value, baseline_value, daily_stats):
    sendgrid_key = os.environ.get("SENDGRID_API_KEY")
    email_receiver = os.environ.get("EMAIL_RECEIVER")

    if not sendgrid_key or not email_receiver:
        print("⚠️ Email not configured — skipping report")
        return

    try:
        today = datetime.now().strftime("%Y-%m-%d")
        today_trades = [t for t in trade_history if t.get("time", "").startswith(today)]

        buys = [t for t in today_trades if t.get("action") == "BUY"]
        sells = [t for t in today_trades if t.get("action") == "SELL"]
        holds = [t for t in today_trades if t.get("action") == "HOLD"]
        shorts = [t for t in today_trades if t.get("action") == "SHORT"]
        margin_buys = [t for t in today_trades if t.get("action") == "MARGIN_BUY"]

        closed_today = [t for t in today_trades if t.get("closed") and t.get("profit_eur") is not None]
        total_pnl = sum(t.get("profit_eur", 0) for t in closed_today)
        wins_today = sum(1 for t in closed_today if t.get("profit_eur", 0) > 0)
        losses_today = sum(1 for t in closed_today if t.get("profit_eur", 0) <= 0)

        win_rate, total_trades = calculate_win_rate(trade_history)
        pnl_from_baseline = portfolio_value - baseline_value
        errors_today = daily_stats.get("errors_today", 0)

        subject = f"🤖 Trading Bot Daily Report — {today}"

        body = f"""
<html><body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
<h2>🤖 Trading Bot Daily Report</h2>
<p><strong>Date:</strong> {today}</p>

<h3>💼 Portfolio Summary</h3>
<table border="1" cellpadding="8" cellspacing="0" width="100%">
  <tr><td><strong>Current Value</strong></td><td>€{portfolio_value:.2f}</td></tr>
  <tr><td><strong>Baseline Value</strong></td><td>€{baseline_value:.2f}</td></tr>
  <tr><td><strong>PnL vs Baseline</strong></td><td style="color: {'green' if pnl_from_baseline >= 0 else 'red'}">€{pnl_from_baseline:.2f} ({(pnl_from_baseline/baseline_value)*100:.1f}%)</td></tr>
  <tr><td><strong>Overall Win Rate</strong></td><td>{f"{win_rate*100:.1f}%" if win_rate else "N/A"} ({total_trades} closed trades)</td></tr>
</table>

<h3>📊 Today's Activity</h3>
<table border="1" cellpadding="8" cellspacing="0" width="100%">
  <tr><td><strong>BUYs</strong></td><td>{len(buys)}</td></tr>
  <tr><td><strong>SELLs</strong></td><td>{len(sells)}</td></tr>
  <tr><td><strong>HOLDs</strong></td><td>{len(holds)}</td></tr>
  <tr><td><strong>SHORTs</strong></td><td>{len(shorts)}</td></tr>
  <tr><td><strong>MARGIN BUYs</strong></td><td>{len(margin_buys)}</td></tr>
  <tr><td><strong>Errors</strong></td><td style="color: {'red' if errors_today > 0 else 'green'}">{errors_today}</td></tr>
</table>

<h3>💰 Today's PnL</h3>
<table border="1" cellpadding="8" cellspacing="0" width="100%">
  <tr><td><strong>Closed Trades</strong></td><td>{len(closed_today)}</td></tr>
  <tr><td><strong>Wins</strong></td><td style="color: green">{wins_today}</td></tr>
  <tr><td><strong>Losses</strong></td><td style="color: red">{losses_today}</td></tr>
  <tr><td><strong>Total PnL Today</strong></td><td style="color: {'green' if total_pnl >= 0 else 'red'}">€{total_pnl:.2f}</td></tr>
</table>

{"<h3>📋 Closed Trades Today</h3><table border='1' cellpadding='8' cellspacing='0' width='100%'><tr><th>Time</th><th>Action</th><th>Symbol</th><th>Amount</th><th>PnL</th><th>Reason</th></tr>" + "".join([f"<tr><td>{t.get('time','')[:16]}</td><td>{t.get('action','')}</td><td>{t.get('symbol','')}</td><td>€{t.get('amount_eur',0):.2f}</td><td style='color: {'green' if t.get('profit_eur',0) >= 0 else 'red'}'>€{t.get('profit_eur',0):.2f}</td><td>{t.get('close_reason','')}</td></tr>" for t in closed_today]) + "</table>" if closed_today else "<p>No closed trades today.</p>"}

<br><p style="color: gray; font-size: 12px;">Trading Bot — Auto-generated report</p>
</body></html>
"""

        response = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": f"Bearer {sendgrid_key}",
                "Content-Type": "application/json"
            },
            json={
                "personalizations": [{"to": [{"email": email_receiver}]}],
                "from": {"email": email_receiver},
                "subject": subject,
                "content": [{"type": "text/html", "value": body}]
            }
        )

        if response.status_code == 202:
            print(f"📧 Daily report sent to {email_receiver}")
        else:
            print(f"❌ Email error: {response.status_code} — {response.text}")

    except Exception as e:
        print(f"❌ ERROR sending email: {e}")

def ask_claude(portfolio, market_data, portfolio_value, baseline_value, news, trade_history, strategy, strategy_reason, consecutive_holds):
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

    # Relaxed criteria after too many consecutive HOLDs
    hold_warning = ""
    if consecutive_holds >= MAX_CONSECUTIVE_HOLDS:
        hold_warning = f"""
⚠️ OVERRIDE: You have made {consecutive_holds} consecutive HOLDs ({consecutive_holds} hours of inactivity).
Relax entry criteria for NEW trades only — but ONLY if EUR is already available:
- Do NOT sell existing positions just to fund an override trade
- RSI_1h between 35-65 is acceptable if MACD histogram shows any directional signal
- volume_trend > 1.0 is sufficient
- Only use available EUR (€{portfolio.get('EUR', 0):.2f}) for any override trade
- Minimum confidence for override trade: 6/10
- If no good opportunity exists even with relaxed criteria, HOLD is still acceptable"""

    if strategy == "CONSERVATIVE":
        strategy_instructions = """
STRATEGY: CONSERVATIVE MODE (portfolio lost 30%+ from baseline)
- Only make high-confidence trades (confidence >= 9)
- Maximum 15% of portfolio per trade
- Prefer HOLD over uncertain trades
- Focus on capital preservation
- Only trade top 3 volume coins
- NO margin or short trades in conservative mode"""
    else:
        strategy_instructions = f"""
STRATEGY: AGGRESSIVE MODE
- Target 20% monthly growth through disciplined trading
- STRICT ENTRY CRITERIA (ALL must be met for standard trades):
  * RSI_1h must be <25 (oversold, BUY) or >75 (overbought, SELL)
  * MACD histogram must be >0.05 for BUY or <-0.05 for SELL
  * volume_trend must be >1.3 to confirm move
  * Price must be above MA7 for BUY, below MA7 for SELL
- If NO standard entry criteria are met → return HOLD, do NOT force trades
- Maximum 30% of portfolio per trade

MARGIN & SHORT TRADING (extra strict criteria):
- SHORT is allowed ONLY when ALL of these are met:
  * RSI_1h < 45 (bearish territory — market showing weakness)
  * MACD histogram < -0.05 (confirmed bearish momentum)
  * volume_trend > 1.0 (sufficient volume confirmation)
  * Price below MA7 AND MA25 (confirmed downtrend structure)
  * Stop-loss MANDATORY at +2% above entry (tight)
  * Maximum position size: 15% of portfolio
- SHORT is the PREFERRED action in bearish markets — use it to profit from downtrends instead of HOLDing
- MARGIN BUY is allowed ONLY when ALL of these are met:
  * RSI_1h < 22 (strongly oversold)
  * MACD histogram > 0.08 (confirmed bullish momentum)
  * volume_trend > 1.5 (high volume confirms reversal)
  * Price above MA7 AND MA25 (confirmed uptrend)
  * Stop-loss MANDATORY at -2% below entry (tight)
  * Maximum position size: 15% of portfolio
- For margin/short trades use action "SHORT" or "MARGIN_BUY" in JSON
{hold_warning}"""

    prompt = f"""You are an expert crypto trader. Your goal is to grow the portfolio by 20% per month through disciplined, high-conviction trading.

PERFORMANCE:
- Current portfolio value: €{portfolio_value:.2f}
- Baseline value: €{baseline_value:.2f}
- Monthly target: €{target_value:.2f}
- Growth still needed: {growth_needed:.1f}%
- Win rate: {f"{win_rate*100:.1f}%" if win_rate else "N/A"} ({total_trades} closed trades)
- Current strategy: {strategy} — {strategy_reason}
- Consecutive HOLDs: {consecutive_holds}

PORTFOLIO (Available EUR: €{eur_available:.2f}):
{json.dumps(portfolio_summary, indent=2)}

SELLABLE POSITIONS (value >= €{MIN_TRADE_EUR}, can be sold):
{chr(10).join(sellable_positions) if sellable_positions else 'None'}

BUYABLE SYMBOLS (any of these can be bought if EUR is available):
{', '.join(buyable_symbols)}

MARKET DATA WITH TECHNICAL INDICATORS:
{json.dumps({s: {k: v for k, v in d.items() if k != 'closes_1h_sample'} for s, d in market_data.items()}, indent=2)}

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
- If you want to BUY but EUR is insufficient, you MUST first include a SELL of a SELLABLE POSITION to generate EUR
- Total EUR spent on BUYs must never exceed available EUR plus EUR generated by SELLs in this decision list
- SELL orders must always come before BUY orders
- ALWAYS include confidence score (1-10) for every decision
- DO NOT force trades when criteria are not met — HOLD is always better than a bad trade
- If no clear opportunity exists, return a single HOLD

Respond ONLY with a JSON array, no explanation, no markdown:
[
  {{"action": "SELL", "symbol": "ETHEUR", "amount_eur": 50.00, "reason": "...", "confidence": 8}},
  {{"action": "BUY", "symbol": "BTCEUR", "amount_eur": 50.00, "reason": "...", "confidence": 8}},
  {{"action": "SHORT", "symbol": "LINKEUR", "amount_eur": 30.00, "stop_loss_pct": 2.0, "reason": "...", "confidence": 9}},
  {{"action": "MARGIN_BUY", "symbol": "SOLEUR", "amount_eur": 30.00, "stop_loss_pct": 2.0, "reason": "...", "confidence": 9}}
]"""

    message = ai_client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )

    response_text = message.content[0].text
    match = re.search(r'\[.*\]', response_text, re.DOTALL)
    if match:
        return json.loads(match.group())

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
    shorts = [d for d in decisions if d.get("action") == "SHORT" and d.get("confidence", 0) >= 9]
    margin_buys = [d for d in decisions if d.get("action") == "MARGIN_BUY" and d.get("confidence", 0) >= 9]

    for decision in sells:
        symbol = decision.get("symbol")
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
                        trade["close_reason"] = "MANUAL-SELL"

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

    for decision in shorts:
        symbol = decision.get("symbol")
        if symbol and not symbol.endswith("EUR"):
            symbol = f"{symbol}EUR"

        amount_eur = decision.get("amount_eur")
        stop_loss_pct = decision.get("stop_loss_pct", 2.0)

        if not symbol or not amount_eur:
            continue

        if amount_eur > portfolio_value * 0.15:
            amount_eur = portfolio_value * 0.15
            print(f"⚠️ SHORT capped at 15%: €{amount_eur:.2f}")

        try:
            client.create_margin_order(
                symbol=symbol,
                side="SELL",
                type="MARKET",
                quoteOrderQty=round(amount_eur, 2),
                sideEffectType="MARGIN_BUY"
            )
            print(f"✅ SHORT {symbol}: €{amount_eur:.2f} | Stop-loss: +{stop_loss_pct}%")
            trade_history.append({
                "time": str(datetime.now()),
                "action": "SHORT",
                "symbol": symbol,
                "amount_eur": amount_eur,
                "entry_price": market_data.get(symbol, {}).get("current_price", 0),
                "stop_loss_pct": stop_loss_pct,
                "reason": decision.get("reason", ""),
                "closed": False,
                "profit_eur": None
            })
        except Exception as e:
            print(f"❌ ERROR SHORT {symbol}: {e}")

    for decision in margin_buys:
        symbol = decision.get("symbol")
        if symbol and not symbol.endswith("EUR"):
            symbol = f"{symbol}EUR"

        amount_eur = decision.get("amount_eur")
        stop_loss_pct = decision.get("stop_loss_pct", 2.0)

        if not symbol or not amount_eur:
            continue

        if amount_eur > portfolio_value * 0.15:
            amount_eur = portfolio_value * 0.15
            print(f"⚠️ MARGIN_BUY capped at 15%: €{amount_eur:.2f}")

        try:
            client.create_margin_order(
                symbol=symbol,
                side="BUY",
                type="MARKET",
                quoteOrderQty=round(amount_eur, 2),
                sideEffectType="MARGIN_BUY"
            )
            print(f"✅ MARGIN_BUY {symbol}: €{amount_eur:.2f} | Stop-loss: -{stop_loss_pct}%")
            trade_history.append({
                "time": str(datetime.now()),
                "action": "MARGIN_BUY",
                "symbol": symbol,
                "amount_eur": amount_eur,
                "entry_price": market_data.get(symbol, {}).get("current_price", 0),
                "stop_loss_pct": stop_loss_pct,
                "reason": decision.get("reason", ""),
                "closed": False,
                "profit_eur": None
            })
        except Exception as e:
            print(f"❌ ERROR MARGIN_BUY {symbol}: {e}")

    return trade_history

def main():
    print(f"🤖 Trading Bot started - {datetime.now()}")
    print(f"🛡️ Stop-loss: {STOP_LOSS_PERCENT*100}% | Take-profit: {TAKE_PROFIT_PERCENT*100}%")
    print(f"⏰ Interval: {INTERVAL_SECONDS//60} minutes")
    print(f"🎯 Target: +{TARGET_GROWTH*100:.0f}% per month")

    binance_client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)

    trade_history = load_from_github(TRADE_HISTORY_FILE) or load_json_file(TRADE_HISTORY_FILE, [])
    portfolio_baseline = load_from_github(PORTFOLIO_BASELINE_FILE) or load_json_file(PORTFOLIO_BASELINE_FILE, {})
    daily_stats = load_from_github(DAILY_STATS_FILE) or load_json_file(DAILY_STATS_FILE, {})

    last_report_date = daily_stats.get("last_report_date", "")
    errors_today = daily_stats.get("errors_today", 0)

    while True:
        try:
            print(f"\n{'='*50}")
            print(f"📊 Analysis - {datetime.now()}")

            today = datetime.now().strftime("%Y-%m-%d")

            # Reset daily error counter on new day
            if today != last_report_date.split("T")[0] if "T" in last_report_date else last_report_date:
                errors_today = 0

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
            check_stop_loss_and_take_profit(binance_client, portfolio, market_data, trade_history)

            strategy, strategy_reason = determine_strategy(trade_history, portfolio_value, baseline_value)
            print(f"🎯 Strategy: {strategy} — {strategy_reason}")

            win_rate, total_trades = calculate_win_rate(trade_history)
            if win_rate is not None:
                print(f"📈 Win rate: {win_rate*100:.1f}% ({total_trades} trades)")

            consecutive_holds = count_consecutive_holds(trade_history)
            if consecutive_holds > 0:
                print(f"⏸️ Consecutive HOLDs: {consecutive_holds}/{MAX_CONSECUTIVE_HOLDS}")

            decisions = ask_claude(portfolio, market_data, portfolio_value, baseline_value, news, trade_history, strategy, strategy_reason, consecutive_holds)
            print(f"🧠 Claude suggested {len(decisions)} decision(s):")
            for d in decisions:
                print(f"   → {d['action']} | {d.get('symbol', '-')} | €{d.get('amount_eur', 0):.2f} | Confidence: {d.get('confidence', 0)}/10")
                print(f"     Reason: {d['reason']}")

            # Track HOLDs in history
            for d in decisions:
                if d.get("action") == "HOLD":
                    trade_history.append({
                        "time": str(datetime.now()),
                        "action": "HOLD",
                        "reason": d.get("reason", "")
                    })

            trade_history = execute_trades(binance_client, decisions, portfolio, portfolio_value, market_data, trade_history)
            save_json_file(TRADE_HISTORY_FILE, trade_history)
            save_to_github(TRADE_HISTORY_FILE, trade_history)
            save_to_github(PORTFOLIO_BASELINE_FILE, portfolio_baseline)

            # Send daily report at 23:00
            current_hour = datetime.now().hour
            if current_hour == 23 and today != last_report_date:
                send_daily_report(trade_history, portfolio_value, baseline_value, {"errors_today": errors_today})
                last_report_date = today
                daily_stats = {"last_report_date": today, "errors_today": 0}
                save_json_file(DAILY_STATS_FILE, daily_stats)
                errors_today = 0

        except Exception as e:
            print(f"❌ ERROR: {e}")
            errors_today += 1
            daily_stats["errors_today"] = errors_today
            save_json_file(DAILY_STATS_FILE, daily_stats)

        print(f"⏰ Next analysis in {INTERVAL_SECONDS//60} minutes...")
        time.sleep(INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
