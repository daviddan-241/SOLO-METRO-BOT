import asyncio
import logging
import os
import sys

from aiohttp import web
from telegram import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeDefault, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot import db, texts
from bot.config import (
    BOT_COMMANDS,
    BOT_NAME,
    FORCE_POLLING,
    PORT,
    WEBHOOK_PATH,
    WEBHOOK_URL,
    require_token,
)
from bot.handlers import (
    cmd_arc,
    cmd_autosnipe,
    cmd_bridge,
    cmd_cashback,
    cmd_chains,
    cmd_cleartrades,
    cmd_collect,
    cmd_debridge,
    cmd_disperse,
    cmd_help,
    cmd_import,
    cmd_monitor,
    cmd_mvp,
    cmd_orders,
    cmd_pos,
    cmd_premium,
    cmd_private,
    cmd_pumpfun,
    cmd_quick,
    cmd_referral,
    cmd_relay,
    cmd_rewards,
    cmd_start,
    cmd_summary,
    cmd_support,
    cmd_trending,
    cmd_wallets,
    cmd_wallets_chain,
    on_callback,
    on_text,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("solo-metro")


async def post_init(app: Application) -> None:
    commands = [BotCommand(name, desc) for name, desc in BOT_COMMANDS]
    await app.bot.set_my_commands(commands, scope=BotCommandScopeDefault())
    try:
        await app.bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
    except Exception as exc:
        log.warning("Could not set private-chat commands: %s", exc)
    try:
        await app.bot.set_my_description(texts.bot_description()[:512])
        await app.bot.set_my_short_description(
            f"{BOT_NAME}, the one-stop solution for all your trading needs!"[:120]
        )
    except Exception as exc:
        log.warning("Could not set bot description: %s", exc)
    log.info("%s commands registered", len(commands))
    from bot.worker import run as worker_run

    async def _worker() -> None:
        try:
            await worker_run(app.bot)
        except Exception:
            log.exception("background worker crashed")

    asyncio.create_task(_worker())
    log.info("background worker scheduled")


def build_application() -> Application:
    token = require_token()
    application = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .concurrent_updates(True)
        .build()
    )
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("support", cmd_support))
    application.add_handler(CommandHandler("chains", cmd_chains))
    application.add_handler(CommandHandler("wallets", cmd_wallets))
    application.add_handler(CommandHandler("quick", cmd_quick))
    application.add_handler(CommandHandler("monitor", cmd_monitor))
    application.add_handler(CommandHandler("summary", cmd_summary))
    application.add_handler(CommandHandler("autosnipe", cmd_autosnipe))
    application.add_handler(CommandHandler("referral", cmd_referral))
    application.add_handler(CommandHandler("cleartrades", cmd_cleartrades))
    application.add_handler(CommandHandler("orders", cmd_orders))
    application.add_handler(CommandHandler("pos", cmd_pos))
    application.add_handler(CommandHandler("mvp", cmd_mvp))
    application.add_handler(CommandHandler("trending", cmd_trending))
    application.add_handler(CommandHandler("pumpfun", cmd_pumpfun))
    application.add_handler(CommandHandler("bridge", cmd_bridge))
    application.add_handler(CommandHandler("private", cmd_private))
    application.add_handler(CommandHandler("relay", cmd_relay))
    application.add_handler(CommandHandler("debridge", cmd_debridge))
    application.add_handler(CommandHandler("arc", cmd_arc))
    application.add_handler(CommandHandler("premium", cmd_premium))
    application.add_handler(CommandHandler("collect", cmd_collect))
    application.add_handler(CommandHandler("disperse", cmd_disperse))
    application.add_handler(CommandHandler("cashback", cmd_cashback))
    application.add_handler(CommandHandler("rewards", cmd_rewards))
    application.add_handler(CommandHandler("import", cmd_import))
    application.add_handler(MessageHandler(filters.Regex(r"^/wallets_"), cmd_wallets_chain))
    application.add_handler(MessageHandler(filters.Regex(r"^/quick_"), cmd_quick))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    application.add_error_handler(on_error)
    return application


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    await msg.reply_text(
        "Unknown command. Send /help for the full list, or /start for the main menu."
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("unhandled error: %s", context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "Something went wrong. Send /start to reopen the menu, or /support."
            )
    except Exception:
        pass


async def run_webhook(application: Application) -> None:
    path = WEBHOOK_PATH if WEBHOOK_PATH.startswith("/") else f"/{WEBHOOK_PATH}"
    hook_url = f"{WEBHOOK_URL}{path}"

    await application.initialize()
    await application.start()
    await application.bot.set_webhook(
        url=hook_url,
        allowed_updates=Update.ALL_UPDATES,
        drop_pending_updates=True,
    )
    log.info("Webhook set to %s", hook_url)

    async def health(_request: web.Request) -> web.Response:
        return web.Response(text=f"{BOT_NAME} is running")

    async def handle(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception:
            return web.Response(text="bad json", status=400)
        try:
            update = Update.de_json(payload, application.bot)
            if update:
                await application.process_update(update)
        except Exception:
            log.exception("webhook update")
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_head("/", health)
    app.router.add_post(path, handle)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("HTTP listening on 0.0.0.0:%s", PORT)

    stop = asyncio.Event()
    try:
        await stop.wait()
    finally:
        await application.stop()
        await application.shutdown()
        await runner.cleanup()


async def run_polling_with_health(application: Application) -> None:
    """Bind 0.0.0.0:PORT so Render health checks pass while using long polling."""
    await application.initialize()
    await application.start()
    await application.updater.start_polling(
        allowed_updates=Update.ALL_UPDATES, drop_pending_updates=True
    )
    log.info("Polling started")

    async def health(_request: web.Request) -> web.Response:
        return web.Response(text=f"{BOT_NAME} is running")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_head("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("Health HTTP listening on 0.0.0.0:%s", PORT)
    stop = asyncio.Event()
    try:
        await stop.wait()
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        await runner.cleanup()


def main() -> None:
    db.init_db()
    try:
        application = build_application()
    except RuntimeError as exc:
        log.error("%s", exc)
        sys.exit(1)

    use_webhook = bool(WEBHOOK_URL) and not FORCE_POLLING
    if use_webhook:
        log.info("Starting webhook mode on port %s", PORT)
        asyncio.run(run_webhook(application))
    elif os.getenv("RENDER"):
        log.info("Starting polling + health HTTP on port %s", PORT)
        asyncio.run(run_polling_with_health(application))
    else:
        log.info("Starting polling mode")
        application.run_polling(allowed_updates=Update.ALL_UPDATES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
