#!/bin/bash
# Restart-safe launcher. Requires HYPEBOT_DISCORD_TOKEN in env.
cd /workspace/nft-hype-bot
[ -f bot.pid ] && kill "$(cat bot.pid)" 2>/dev/null && sleep 2
DISCORD_TOKEN="$HYPEBOT_DISCORD_TOKEN" SYNC_COMMANDS="${SYNC_COMMANDS:-false}" DATABASE_URL=sqlite:////workspace/nft-hype-bot/hypebot.db \
  setsid nohup .venv/bin/python -m hypebot >> /workspace/nft-hype-bot/bot.log 2>&1 < /dev/null &
echo $! > bot.pid
