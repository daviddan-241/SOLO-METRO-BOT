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

1. Create a bot with [@BotFather](https://t.me/BotFather). Copy the token.
2. In BotFather you can leave the description empty — the app sets it on boot:
   - *What can this bot do?* → `This is the official Solo Metro bot 🔫 deployed by @SoloMetroBot.`
3. New Web Service on Render → connect this GitHub repo.
4. **Build:** `pip install -r requirements.txt`
5. **Start:** `python run.py`
6. **Health check path:** `/`
7. Environment:
   - `TELEGRAM_BOT_TOKEN` = your BotFather token (required)
   - `BOT_NAME` = `Solo Metro` (optional)
   - `BOT_HANDLE` = `@YourBotUsername` (optional)
   - `HUB_URL` / `UPDATES_URL` / `TWITTER_URL` / `DOCS_URL` / `SUPPORT_URL` / `TOS_URL` (optional links)

Render sets `PORT` and `RENDER_EXTERNAL_URL`. The bot uses those to bind `0.0.0.0` and register a Telegram webhook automatically.

Free web dynos sleep after idle time. The first message after sleep can be slow while Render wakes the service.

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
