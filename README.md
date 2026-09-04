# Solo Metro Bot

Telegram trading bot UI matching the Maestro-style flow: captcha gate, command menu, chains, wallets, settings, copytrade, autosnipe, bridge, premium, cashback, referral, and Token Report on pasted CAs.

Live trading engine is wired:

- DexScreener token reports (price, MC, liquidity, chart) + GoPlus tax/honeypot flags
- Real wallet generate / import / auto-generate W1 (SOL, ETH, BSC, BASE)
- Live native balances
- EVM buys/sells via LiFi aggregator with Uniswap V2 / PancakeSwap fallback
- Solana buys/sells via Jupiter
- Send native / ERC-20, collect, disperse
- EVM→EVM bridge via LiFi (Relay/deBridge routes)
- Limit orders, autosnipe, copytrade worker (polls every ~8s)
- Signal auto-buy when you forward a CA from a tracked channel

Fund a generated wallet with native gas+size, paste a CA, tap Buy.

## Render (free web)

This repo is a **Web Service** (`render.yaml`). Free instances must bind `0.0.0.0:$PORT` and answer `GET /` — this app does both.

1. Create a bot with [@BotFather](https://t.me/BotFather). Copy the token.
2. Open [Render](https://dashboard.render.com) → **New → Web Service** → connect `https://github.com/daviddan-241/SOLO-METRO-BOT`.
3. Runtime: Python. **Build:** `pip install -r requirements.txt`. **Start:** `python run.py`. **Health check:** `/`. **Instance:** Free.
4. Environment (Blueprint from `render.yaml` also works):
   - `TELEGRAM_BOT_TOKEN` — required
   - `BOT_NAME` = `Solo Metro`
   - `FEE_EVM_ADDRESS` / `FEE_SOL_ADDRESS` — optional 1% fee + Premium payout address
   - `ENCRYPTION_KEY` — optional Fernet key so wallet encryption survives redeploys
5. Deploy. Open `https://YOUR-SERVICE.onrender.com/health` — you must see `Solo Metro is running` (HTTP 200).
6. Message the bot `/start`. Captcha → main menu.

### Keep it awake with UptimeRobot (free)

Render free web **sleeps after ~15 minutes** with no traffic. UptimeRobot pings keep it up.

1. [UptimeRobot](https://uptimerobot.com) → Add New Monitor
2. Monitor Type: **HTTP(s)**
3. URL: `https://YOUR-SERVICE.onrender.com/health`  
   Also works: `/` `/ping` `/uptime` `/status` `/health.json`
4. Interval: **5 minutes**
5. Optional keyword monitor: keyword **`running`** (the body is `Solo Metro is running`)
6. Alert When Down: your email

HEAD and GET both return 200. Health answers immediately (no Telegram/RPC) so Render’s own health check and UptimeRobot never time out on a live dyno.

Render sets `PORT` and `RENDER_EXTERNAL_URL`. With `FORCE_POLLING=0` (default) the bot registers a Telegram **webhook** on `/telegram`. Set `FORCE_POLLING=1` for long-polling plus the same health HTTP server.

SQLite on the free disk is **ephemeral** — wallets reset on redeploy unless you add a persistent disk.

## Local

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # put TELEGRAM_BOT_TOKEN in .env
python run.py
```

Local mode uses long polling unless `WEBHOOK_URL` is set.

## Command menu (BotFather list)

`/start` `/bridge` `/quick` `/monitor` `/summary` `/chains` `/autosnipe` `/referral` `/cleartrades` `/orders` `/pos` `/mvp` `/trending` `/pumpfun` `/private` `/relay` `/debridge` `/premium` `/wallets` `/collect` `/disperse` `/cashback` `/rewards` `/import` `/arc` `/support` `/help`

Per-chain shortcuts: `/wallets_ETH`, `/quick_SOL`, …

## Security

- Never commit `.env` or bot tokens.
- Generated private keys are Fernet-encrypted in SQLite.
- Render’s free disk is ephemeral — wallets stored in SQLite reset on redeploy. Add a persistent disk or Postgres before going live with funds.
- If a GitHub PAT was pasted in chat, revoke it in GitHub → Settings → Developer settings → Personal access tokens and create a new one.
