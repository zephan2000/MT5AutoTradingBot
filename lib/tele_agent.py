# tele_agent.py
import asyncio
import os
import sys
import json
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple
from parser import parse_trade_signal

from dotenv import load_dotenv
load_dotenv()

# --- Supabase (v2) ---
from supabase import create_client, Client

# --- Telegram: Telethon (user) + Bot API (send approval, forward, etc.)
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton

# ---------- ENV ----------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_ANON_KEY")
BOT_TOKEN = os.getenv("BOT_TOKEN")  # your existing bot token
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))     # from my.telegram.org
API_HASH = os.getenv("TELEGRAM_API_HASH", "")

if not (SUPABASE_URL and SUPABASE_KEY and BOT_TOKEN and API_ID and API_HASH):
    print("Missing env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY/ANON_KEY, BOT_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH")
    sys.exit(1)

sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
bot = Bot(token=BOT_TOKEN)


# ---------- Utility: parse_trade_signal import (fallback no-op) ----------
def _fallback_parse_trade_signal(text: str) -> dict:
    return {}

try:
    # Adjust to your project path if needed:
    from main import parse_trade_signal  # reuse your existing parser
except Exception:
    parse_trade_signal = _fallback_parse_trade_signal


@dataclass
class SessionCtx:
    owner_user_id: str
    telegram_user_id: int
    client: TelegramClient
    # simple cache of allowed chats (group_sources) for this owner
    allowed_chat_ids: Set[int]
    last_refresh: float = 0.0


_refresh_secs = 60.0  # refresh whitelist every 60s


async def refresh_allowed_chats(ctx: SessionCtx):
    now = time.time()
    if now - ctx.last_refresh < _refresh_secs:
        return
    res = sb.table("group_sources") \
        .select("chat_id") \
        .eq("platform", "telegram") \
        .eq("owner_user_id", ctx.owner_user_id) \
        .execute()
    allowed = set()
    for row in res.data or []:
        try:
            allowed.add(int(row["chat_id"]))
        except Exception:
            pass
    ctx.allowed_chat_ids = allowed
    ctx.last_refresh = now


async def send_to_followers_and_queue(
    source_row: dict,
    text: str,
    parsed: dict,
    follower_user_id: str,
    target_chat: str,
    message_id: str
):
    # Forward/normalize message via Bot
    title = source_row.get("title") or "Signal"
    await bot.send_message(
        chat_id=target_chat,
        text=f"[{title}]\n{text}"
    )

    # Create signal + queue order (your existing RPCs)
    try:
        # sizing defaults
        symbol = parsed.get("symbol") or ""
        side = (parsed.get("action") or "buy").lower()
        size = float(parsed.get("entry_min") or 0.01)
        sl = parsed.get("sl")
        tp = parsed.get("tp") or []

        # Pick account id (you might move this into an RPC)
        # For now, assume you have a helper RPC or logic; example:
        acct = sb.rpc("rpc_pick_account_id", {"p_user_id": follower_user_id}).execute().data
        account_id = acct["account_id"] if acct else None

        # Fallback approach if you don't have rpc_pick_account_id:
        if not account_id:
            # Optionally query your accounts table here
            pass

        sig = sb.rpc("rpc_create_signal", {
            "p_master_id": follower_user_id,
            "p_symbol": symbol,
            "p_side": side,
            "p_size": size,
            "p_sl": sl,
            "p_tp": tp
        }).execute().data

        res = sb.rpc("rpc_queue_order_with_approval", {
            "p_user_id": follower_user_id,
            "p_account_id": account_id,
            "p_signal_id": sig["id"],
            "p_client_order_id": f"src-{message_id}",
            "p_meta": {"symbol": symbol, "side": side, "size": size}
        }).execute().data

        appr = (res or {}).get("approval")
        if appr:
            token = appr["callback_token"]
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes", callback_data=f"appr:{token}:yes"),
                InlineKeyboardButton("❌ No",  callback_data=f"appr:{token}:no")
            ]])
            # Send approval prompt to the same target chat
            await bot.send_message(chat_id=target_chat, text="Approve routed order?", reply_markup=kb)

    except Exception as e:
        await bot.send_message(chat_id=target_chat, text=f"Route error: {e}")


def make_handler(ctx: SessionCtx):
    @events.register(events.NewMessage)
    async def on_new_message(event):
        try:
            # Basic guards
            if not event.message or not event.message.message:
                return

            await refresh_allowed_chats(ctx)

            chat_id = event.chat_id  # int
            if chat_id is None:
                return
            if ctx.allowed_chat_ids and chat_id not in ctx.allowed_chat_ids:
                # Not whitelisted for this owner
                return

            text = event.message.message
            message_id = event.message.id
            message_ts = event.message.date.isoformat()

            # Find source row
            src = sb.table("group_sources") \
                .select("*") \
                .eq("platform", "telegram") \
                .eq("chat_id", str(chat_id)) \
                .eq("owner_user_id", ctx.owner_user_id) \
                .limit(1) \
                .execute().data
            if not src:
                return
            source = src[0]

            parsed = parse_trade_signal(text) or {}

            # Store inbound
            try:
                sb.table("inbound_messages").insert({
                    "source_id": source["id"],
                    "message_id": str(message_id),
                    "message_ts": message_ts,
                    "raw_text": text,
                    "parsed": parsed
                }).execute()
            except Exception:
                # non-fatal
                pass

            # Fan out to follower routes
            routes = sb.table("copy_routes").select("*") \
                .eq("source_id", source["id"]) \
                .eq("active", True) \
                .execute().data or []

            for r in routes:
                follower_id = r["follower_user_id"]
                target_chat = r["target_chat_id"]
                await send_to_followers_and_queue(
                    source, text, parsed, follower_id, target_chat, str(message_id)
                )

        except Exception as e:
            # Log to Supabase (optional) or print
            print(f"[{ctx.telegram_user_id}] Handler error:", e)

    return on_new_message


async def run_all_sessions():
    # Load all active sessions
    rows = sb.table("user_sessions").select("*").eq("is_active", True).execute().data or []
    if not rows:
        print("No active user sessions. Run `python tele_agent.py login --owner <uuid>` first.")
        return

    clients: List[TelegramClient] = []
    tasks = []

    for row in rows:
        session_str = row["session_string"]
        owner_id = row["owner_user_id"]
        tg_uid = int(row["telegram_user_id"])

        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.start()
        ctx = SessionCtx(owner_user_id=owner_id, telegram_user_id=tg_uid, client=client, allowed_chat_ids=set())
        handler = make_handler(ctx)
        client.add_event_handler(handler)

        clients.append(client)
        tasks.append(client.run_until_disconnected())

        print(f"Started watcher for owner={owner_id} tg_user={tg_uid}")

    # Run all clients concurrently
    await asyncio.gather(*tasks)


async def login_flow(owner_user_id: str):
    """
    CLI login to create a StringSession for a user account.
    Stores it in Supabase user_sessions.
    """
    phone = input("Enter phone number (international format, e.g. +14155552671): ").strip()
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        try:
            await client.send_code_request(phone)
            code = input("Enter the code you received: ").strip()
            try:
                await client.sign_in(phone=phone, code=code)
            except SessionPasswordNeededError:
                pwd = input("Two-step verification enabled. Enter password: ").strip()
                await client.sign_in(password=pwd)
        except Exception as e:
            print("Login failed:", e)
            return

    me = await client.get_me()
    session_string = client.session.save()
    tg_uid = me.id

    # Store in Supabase
    sb.table("user_sessions").upsert({
        "owner_user_id": owner_user_id,
        "telegram_user_id": tg_uid,
        "session_string": session_string,
        "is_active": True
    }).execute()

    print(f"Saved session for tg_user={tg_uid} owner={owner_user_id}")


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python tele_agent.py login --owner <owner_user_id>")
        print("  python tele_agent.py run")
        sys.exit(0)

    cmd = sys.argv[1]
    if cmd == "login":
        try:
            owner_idx = sys.argv.index("--owner")
            owner_id = sys.argv[owner_idx + 1]
        except ValueError:
            print("Missing --owner <uuid>")
            sys.exit(1)
        asyncio.run(login_flow(owner_id))
    elif cmd == "run":
        asyncio.run(run_all_sessions())
    else:
        print("Unknown command:", cmd)
        sys.exit(1)


if __name__ == "__main__":
    main()
