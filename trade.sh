#!/bin/bash
set -euo pipefail

cd /Users/sonic/.openclaw/workspace/trading-bot
exec python3 /Users/sonic/.openclaw/workspace/trading-bot/trade.py run "$@"
