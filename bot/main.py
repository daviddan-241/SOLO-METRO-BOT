import asyncio
import logging
import os
import sys

from aiohttp import web
from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeDefault,
    MenuButtonCommands,
    Update,
)
from telegram.ext import (
    Application,
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
from bot.extra_cmds import register_command_handlers


def _allowed_updates():
    # PTB v21 / Python 3.14: Update.ALL_TYPES. Older PTB used ALL_UPDATES.
    return getattr(Update, "ALL_TYPES", None) or getattr(Update, "ALL_UPDATES", None)


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("solo-metro")
_STARTED = __import__("time").time()
HEALTH_PATHS = ("/", "/health", "/ping", "/uptime", "/status")


def _health_headers() -> dict:
    return {
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
    }


async def health_plain(request: web.Request) -> web.Response:
    """UptimeRobot + Render: instant 200, no Telegram/RPC calls."""
    body = f"{BOT_NAME} is running"
    if request.method == "HEAD":
        return web.Response(
            status=200,
            headers={**_health_headers(), "Content-Type": "text/plain; charset=utf-8", "Content-Length": str(len(body))},
        )
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=_health_headers())
    return web.Response(
        text=body,
        status=200,
        content_type="text/plain",
        charset="utf-8",
        headers=_health_headers(),
    )


async def health_json(_request: web.Request) -> web.Response:
    import time

    payload = {
        "ok": True,
        "status": "running",
        "bot": BOT_NAME,
        "uptime_seconds": int(time.time() - _STARTED),
    }
    try:
        from bot.config import DB_PATH as _DBP
        from bot.persist import db_status

        payload["db"] = db_status(_DBP)
    except Exception:
        pass
    return web.json_response(payload, headers=_health_headers())


def mount_health(app: web.Application) -> None:
    # aiohttp add_get also registers HEAD. Adding HEAD again raises
    # RuntimeError: Added route will never be executed, method HEAD is already registered
    for path in HEALTH_PATHS:
        app.router.add_get(path, health_plain)
        try:
            app.router.add_options(path, health_plain)
        except RuntimeError:
            pass
    app.router.add_get("/health.json", health_json)
    app.router.add_get("/status.json", health_json)


async def serve_http(app: web.Application) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("HTTP health on 0.0.0.0:%s  paths=%s", PORT, ",".join(HEALTH_PATHS))
    return runner


async def sync_menu_commands(app: Application) -> int:
    """Register the Maestro-identical side Menu. Retried, multi-scope.

    Telegram clients (esp. iOS) only show the blue Menu button when commands
    are set for the chat's scope. We set Default + Private + Group so the
    menu shows everywhere, then force MenuButtonCommands globally.
    Returns number of scopes successfully set.
    """
    commands = [BotCommand(name, desc[:256]) for name, desc in BOT_COMMANDS]
    scopes = (BotCommandScopeDefault(), BotCommandScopeAllPrivateChats(), BotCommandScopeAllGroupChats())
    ok = 0
    for scope in scopes:
        for attempt in range(3):
            try:
                await app.bot.set_my_commands(commands, scope=scope)
                ok += 1
                break
            except Exception as exc:
                log.warning("set_my_commands %s try %d: %s", scope, attempt + 1, exc)
                await asyncio.sleep(1 + attempt * 2)
    # verify
    try:
        got = await app.bot.get_my_commands(scope=BotCommandScopeAllPrivateChats())
        log.info("menu verify: %d/%d commands live in private scope", len(got), len(commands))
    except Exception as exc:
        log.warning("menu verify: %s", exc)
    for attempt in range(3):
        try:
            await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
            break
        except Exception as exc:
            log.warning("set_chat_menu_button try %d: %s", attempt + 1, exc)
            await asyncio.sleep(1 + attempt * 2)
    return ok


async def post_init(app: Application) -> None:
    menu_ok = await sync_menu_commands(app)
    try:
        await app.bot.set_my_description(texts.bot_description()[:512])
        await app.bot.set_my_short_description(
            f"{BOT_NAME}, the one-stop solution for all your trading needs!"[:120]
        )
    except Exception as exc:
        log.warning("Could not set bot description: %s", exc)
    log.info("%s commands registered for Menu button", len(BOT_COMMANDS))
    from bot.admin import set_bot

    set_bot(app.bot)
    if menu_ok < 3:
        # Telegram hiccup during boot — one silent retry a minute later.
        async def _resync() -> None:
            await asyncio.sleep(60)
            try:
                await sync_menu_commands(app)
            except Exception:
                log.warning("delayed menu resync failed")

        asyncio.create_task(_resync())
    try:
        from bot.admin import fire
        from bot.config import DB_PATH as _DBP

        try:
            import os as _os

            _sz = _os.path.getsize(_DBP) if _os.path.exists(_DBP) else 0
        except OSError:
            _sz = 0
        fire(
            f"🟢 <b>{BOT_NAME} booted</b>\n"
            f"Menu: {len(BOT_COMMANDS)} cmds ({menu_ok}/3 scopes)\n"
            f"DB: <code>{_DBP}</code> ({_sz // 1024} KB)"
        )
    except Exception:
        pass
    from bot.worker import run as worker_run

    async def _worker() -> None:
        # self-healing: restart worker if it ever crashes
        backoff = 5
        while True:
            try:
                await worker_run(app.bot)
                backoff = 5
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("background worker crashed, restart in %ss", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)

    asyncio.create_task(_worker())
    log.info("background worker scheduled (self-healing)")

    # periodic DB backup so restarts/redeploys lose nothing
    try:
        from bot.config import DB_PATH as _DBP
        from bot.persist import backup_loop

        async def _backups() -> None:
            try:
                await backup_loop(_DBP, 300)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("backup loop crashed")

        asyncio.create_task(_backups())
        log.info("DB backup loop scheduled")
    except Exception as exc:
        log.warning("backup loop: %s", exc)


def build_application() -> Application:
    token = require_token()
    application = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .concurrent_updates(True)
        .build()
    )
    register_command_handlers(application)
    # MUST stay in group 0 (default), added LAST: within one group only the
    # first matching handler runs, so unknown_command fires solely when no
    # real handler matched. NOTE: different groups ALL fire (groups don't
    # block each other), so putting this in group=1 spams "Unknown command"
    # after every valid command. Do not move it.
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    application.add_error_handler(on_error)
    return application


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not (msg.text or "").startswith("/"):
        return
    cmd = (msg.text or "").split()[0].lstrip("/").split("@")[0].lower()
    # chain aliases are handled by regex handlers; never call them unknown
    if cmd.startswith("wallets_") or cmd.startswith("quick_"):
        return
    await msg.reply_text(
        "Unknown command. Send /help for the full list, or /start for the main menu."
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("unhandled error: %s", context.error)
    try:
        from bot.admin import fire

        fire(f"⚠️ Bot error: {html_esc(str(context.error)[:400])}")
    except Exception:
        pass
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "Something went wrong. Send /start to reopen the menu, or /support."
            )
    except Exception:
        pass


def html_esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


async def run_webhook(application: Application) -> None:
    path = WEBHOOK_PATH if WEBHOOK_PATH.startswith("/") else f"/{WEBHOOK_PATH}"
    hook_url = f"{WEBHOOK_URL}{path}"
    ready = asyncio.Event()

    async def handle(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception:
            return web.Response(text="bad json", status=400)
        try:
            await asyncio.wait_for(ready.wait(), timeout=45)
        except asyncio.TimeoutError:
            return web.Response(text="starting", status=503)
        try:
            update = Update.de_json(payload, application.bot)
            if update:
                await application.process_update(update)
        except Exception:
            log.exception("webhook update")
        return web.Response(text="ok")

    # Bind health FIRST so Render + UptimeRobot get 200 while Telegram boots.
    http_app = web.Application()
    mount_health(http_app)
    http_app.router.add_post(path, handle)
    runner = await serve_http(http_app)

    try:
        await application.initialize()
        await application.start()
        await application.bot.set_webhook(
            url=hook_url,
            allowed_updates=_allowed_updates(),
            drop_pending_updates=True,
        )
        ready.set()
        log.info("Webhook set to %s", hook_url)
        stop = asyncio.Event()
        await stop.wait()
    finally:
        try:
            from bot.config import DB_PATH as _DBP
            from bot.persist import backup_now, checkpoint

            checkpoint(_DBP)
            backup_now(_DBP)
        except Exception:
            pass
        await application.stop()
        await application.shutdown()
        await runner.cleanup()


async def run_polling_with_health(application: Application) -> None:
    """Bind 0.0.0.0:PORT so Render + UptimeRobot stay green while polling."""
    http_app = web.Application()
    mount_health(http_app)
    runner = await serve_http(http_app)

    try:
        await application.initialize()
        await application.start()
        await application.updater.start_polling(
            allowed_updates=_allowed_updates(), drop_pending_updates=True
        )
        log.info("Polling started")
        stop = asyncio.Event()
        await stop.wait()
    finally:
        try:
            from bot.config import DB_PATH as _DBP
            from bot.persist import backup_now, checkpoint

            checkpoint(_DBP)
            backup_now(_DBP)
        except Exception:
            pass
        try:
            await application.updater.stop()
        except Exception:
            pass
        await application.stop()
        await application.shutdown()
        await runner.cleanup()


def main() -> None:
    from bot.config import DB_PATH as _DB

    try:
        from bot.persist import ensure_encryption_key, restore_if_needed

        ensure_encryption_key()
        restore_if_needed(_DB)
    except Exception as exc:
        log.warning("persist boot: %s", exc)
    log.info("SQLite (WAL) at %s", _DB)
    db.init_db()
    try:
        if not db.integrity_ok():
            log.warning("SQLite integrity check failed, continuing anyway")
    except Exception:
        pass
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
        application.run_polling(allowed_updates=_allowed_updates(), drop_pending_updates=True)


if __name__ == "__main__":
    main()
