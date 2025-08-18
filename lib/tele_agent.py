# tele_agent.py
import asyncio
import os
import sys
import json
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple
from parser import parse_trade_signal
from llm_normalize import normalize_message

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
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
BOT_TOKEN = os.getenv("BOT_TOKEN")  # your existing bot token
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))     # from my.telegram.org
API_HASH = os.getenv("TELEGRAM_API_HASH", "")

if not (SUPABASE_URL and SUPABASE_KEY and BOT_TOKEN and API_ID and API_HASH):
    print("Missing env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY/ANON_KEY, BOT_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH")
    sys.exit(1)

sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
bot = Bot(token=BOT_TOKEN)



@dataclass
class SessionCtx:
    owner_user_id: str
    telegram_user_id: int
    client: TelegramClient
    # simple cache of allowed chats (group_sources) for this owner
    allowed_chat_ids: Set[int]
    last_refresh: float = 0.0


_refresh_secs = 15.0  # refresh whitelist every 60s


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
      # NEW: log after refresh so it's the latest list
    print(f"[{ctx.telegram_user_id}] allowed_chat_ids (refreshed) -> {sorted(ctx.allowed_chat_ids)}")


async def send_to_followers(
    source_row: dict,
    text: str,
    parsed: dict,
    follower_user_id: str,
    target_chat: str,
    message_id: str,
    uim_id: int
):
    # (A) Forward original for transparency via Bot
    title = source_row.get("title") or "Signal"
    await bot.send_message(
        chat_id=target_chat,
        text=f"📨 [{title}] (original)\n{text}"
    )
    # (B) Summary card before approvals
    entry_text = (
        f"{parsed['entry_min']}-{parsed['entry_max']}"
        if parsed.get('entry_max') and parsed['entry_max'] != parsed['entry_min']
        else f"{parsed['entry_min']}"
    )
    sl_text = parsed.get('sl') if parsed.get('sl') is not None else "—"
    tp_text = ", ".join(str(x) for x in (parsed.get('tp') or [])) or "—"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("▶️ Execute", callback_data=f"exec:yes:{uim_id}"),
        InlineKeyboardButton("🚫 Ignore",  callback_data=f"exec:no:{uim_id}")    ]])

    await bot.send_message(
        chat_id=target_chat,
        text=(f"Proposed Order\n"
            f"Symbol: {parsed['symbol']} | Side: {parsed['action'].upper()}\n"
            f"Entry: {entry_text} | Stop: {sl_text}\n"
            f"Targets: {tp_text}\n"
            f"Source msg: {message_id}"
            f"Choose: Execute or Ignore"),
    reply_markup=kb
    )
  



def make_handler(ctx: SessionCtx):
    @events.register(events.NewMessage(incoming=True, outgoing=True))
    async def on_new_message(event):
        # at the top of on_new_message
        print(f"[{ctx.telegram_user_id}] got msg chat_id={event.chat_id}")
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
            src = (sb.table("group_sources")
                     .select("*")
                     .eq("platform", "telegram")
                     .eq("chat_id", str(chat_id))
                     .eq("owner_user_id", ctx.owner_user_id)
                     .limit(1)
                     .execute().data)
            if not src:
                return
            source = src[0]

            # --- 1. Normalize with LLM ---
            # BUGFIX: use the same message_id object you already extracted
            hints = normalize_message(text, group_id=chat_id, message_id=message_id)
            if not hints or not isinstance(hints, dict):
                print("[WARN] Normalizer failed, skipping.\nRaw text:", text[:200])
                return

            print("Raw normalized payload:", hints)

            # --- 2. Parse trade signal ---
            parsed = parse_trade_signal(text, hints)
            if not parsed:
                print("[WARN] Parser failed, skipping.\nRaw text:", text[:200])
                return

            print("✅ Parsed payload:", parsed)

            # 3) Persist inbound message (idempotent on source_id,message_id)
            source_id = source["id"]  # from group_sources
            inbound_payload = {
                "source_id": source_id,
                "message_id": str(message_id),
                "message_ts": message_ts,
                "raw_text": text,
                "normalized_json": hints,
                "parsed_json": parsed,
            }

            try:
                sb.table("inbound_messages").upsert(
                    inbound_payload,
                    on_conflict="source_id,message_id"
                ).execute()

                inbound = (sb.table("inbound_messages")
                             .select("id")
                             .eq("source_id", source_id)
                             .eq("message_id", str(message_id))
                             .maybe_single()
                             .execute()
                             .data)
                if not inbound or "id" not in inbound:
                    print("[ERROR] inbound_messages row not found after upsert")
                    return
                inbound_id = inbound["id"]
            except Exception as e:
                print("[ERROR] Upsert inbound_messages failed:", repr(e))
                return

            # 4) Find subscribers (routes)
            routes = (sb.table("copy_routes")
                        .select("*")
                        .eq("source_id", source_id)
                        .eq("active", True)
                        .execute().data) or []
            if not routes:
                print("[INFO] No active routes for this source; nothing to fan out.")
                return

            # 5) Fan out PER ROUTE
            for r in routes:
                follower_id = r["follower_user_id"]
                target_chat = r["target_chat_id"]

                try:
                    # 1) upsert and ask PostgREST to return the row
                    res = (sb.table("user_inbound_messages")
                            .upsert(
                                {
                                    "user_id": follower_id,
                                    "inbound_message_id": inbound_id,
                                    "status": "pending",
                                },
                                on_conflict="user_id,inbound_message_id",
                                returning="representation",   # <-- key fix
                            )
                            .execute())

                    # 2) extract id (fallback to a select if representation wasn’t returned)
                    if res.data and len(res.data) > 0 and "id" in res.data[0]:
                        uim_id = res.data[0]["id"]
                    else:
                        uim_row = (sb.table("user_inbound_messages")
                                    .select("id")
                                    .eq("user_id", follower_id)
                                    .eq("inbound_message_id", inbound_id)
                                    .maybe_single()
                                    .execute()
                                    .data)
                        if not uim_row or "id" not in uim_row:
                            print(f"[ERROR] No uim row for user={follower_id}")
                            continue
                        uim_id = uim_row["id"]

                    await send_to_followers(
                        source, text, parsed, follower_id, target_chat, str(message_id), uim_id
                    )

                except Exception as e:
                    print(f"[ERROR] Route fanout failed for user={follower_id}:", repr(e))
                    continue


        except Exception as e:
            # Top-level safety net for the handler
            print(f"[{ctx.telegram_user_id}] Handler error:", e)

    # IMPORTANT: return the handler from make_handler (NOT inside on_new_message)
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
