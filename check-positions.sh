#!/bin/bash
"""
自动交易监控 - 每分钟检查持仓，止盈止损
"""

API_KEY="PJ43OsTuORuYxZGypVHDTQRW71rtDM85gi8oNJsr0slQtTNVrgI7djgkbUfqAIST"
SECRET_KEY="hviHVBZVlOCouEYOzjilSjEhCkS9VLAFmcDUFI4nK7KzlygTKtJ81fGaanr6clzy"
TRADE_LOG="/Users/sonic/.openclaw/workspace/trading-bot/trades.json"
MIN_PROFIT_PCT=3
MAX_LOSS_PCT=2

sign() {
    echo -n "$1" | openssl dgst -sha256 -hmac "$SECRET_KEY" | cut -d' ' -f2
}

get_balance() {
    local result=$(curl -s "https://fapi.binance.com/fapi/v2/balance?timestamp=$(date +%s000)&recvWindow=5000")
    echo "$result" | grep -o '"asset":"USDT","balance":"[^"]*"' | grep -o '[0-9.]*'
}

log_trade() {
    local trade_type=$1
    local symbol=$2
    local amount=$3
    local price=$4
    local pnl=$5
    local reason=$6
    
    local balance=$(get_balance)
    local timestamp=$(date "+%Y-%m-%d %H:%M:%S")
    
    python3 << PYEOF
import json
trades = json.load(open('$TRADE_LOG'))
trades.append({
    'time': '$timestamp',
    'type': '$trade_type',
    'symbol': '$symbol',
    'amount': float('$amount'),
    'price': float('$price'),
    'pnl': float('$pnl') if '$pnl' else 0,
    'reason': '$reason',
    'balance': float('$balance')
})
json.dump(trades, open('$TRADE_LOG', 'w'), indent=2)
PYEOF
}

echo "🔍 检查持仓..."

timestamp=$(date +%s000)
query="timestamp=${timestamp}&recvWindow=5000"
signature=$(sign "$query")

positions=$(curl -s "https://fapi.binance.com/fapi/v2/account?${query}&signature=$signature" \
    -H "X-MBX-APIKEY: $API_KEY" \
    -H "User-Agent: binance-auto-trade/1.0")

# Check for open positions
echo "$positions" | python3 -c "
import json, sys, subprocess, os

data = json.load(sys.stdin)
positions = data.get('positions', [])

for p in positions:
    amt = float(p.get('positionAmt', 0))
    if amt != 0:
        symbol = p['symbol']
        entry = float(p['entryPrice'])
        unrealized = float(p['unrealProfit'])
        
        # Get current price
        result = subprocess.run(
            ['curl', '-s', f'https://api.binance.com/api/v3/ticker/price?symbol={symbol}'],
            capture_output=True, text=True
        )
        try:
            current = float(json.loads(result.stdout).get('price', entry))
        except:
            current = entry
        
        pnl_pct = ((current - entry) / entry) * 100
        
        print(f'持仓: {symbol} 数量:{amt} 均价:{entry} 当前:{current} 盈亏:{pnl_pct:.2f}%')
        
        # Check if should close
        balance_result = subprocess.run(
            ['curl', '-s', 'https://fapi.binance.com/fapi/v2/balance?timestamp=' + str(int(__import__('time').time()*1000)) + '&recvWindow=5000'],
            capture_output=True, text=True
        )
        
        balance = 10.0
        try:
            for b in json.loads(balance_result.stdout):
                if b['asset'] == 'USDT':
                    balance = float(b['balance'])
                    break
        except:
            pass
        
        should_close = False
        reason = ''
        
        if pnl_pct >= 3:  # 3% take profit
            should_close = True
            reason = '止盈3%'
        elif pnl_pct <= -2:  # 2% stop loss
            should_close = True
            reason = '止损2%'
        
        if should_close:
            print(f'  -> 平仓: {reason}')
            side = 'SELL' if amt > 0 else 'BUY'
            positionSide = 'LONG' if amt > 0 else 'SHORT'
            
            # Place close order
            ts = int(__import__('time').time()*1000)
            q = f'symbol={symbol}&side={side}&type=MARKET&quantity={abs(amt)}&positionSide={positionSide}&timestamp={ts}&recvWindow=5000'
            sig = subprocess.run(
                ['bash', '-c', f'echo -n \"{q}\" | openssl dgst -sha256 -hmac \"{os.environ.get(\"SECRET_KEY\", \"\")}\" | cut -d\" \" -f2'],
                capture_output=True, text=True
            ).stdout.strip()
            
            subprocess.run([
                'curl', '-s', '-X', 'POST',
                f'https://fapi.binance.com/fapi/v1/order?{q}&signature={sig}',
                '-H', 'X-MBX-APIKEY: ' + os.environ.get('API_KEY', ''),
                '-H', 'User-Agent: binance-auto-trade/1.0'
            ])
            
            # Log the trade
            unrealized_usd = unrealized
            subprocess.run([
                'bash', '-c',
                f'python3 << PYEOT\\nimport json\\ntrades = json.load(open(\"{os.environ.get(\"TRADE_LOG\", \"/tmp/trades.json\")}\"));'
                f'trades.append({{\"time\": \"{__import__(\"datetime\").datetime.now().strftime(\"%Y-%m-%d %H:%M:%S\")}', 
                f'\"type\": \"SELL\", \"symbol\": \"{symbol}\", \"amount\": {abs(amt)}, \"price\": {current}, '
                f'\"pnl\": {unrealized_usd}, \"reason\": \"{reason}\", \"balance\": {balance}}});'
                f'json.dump(trades, open(\"{os.environ.get(\"TRADE_LOG\", \"/tmp/trades.json\")}\", \"w\"), indent=2)\\nPYEOT'
            ])
            print(f'  -> 已平仓!')
        else:
            print(f'  -> 继续持有')
"