import os, json, re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ConversationHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from uuid import UUID
from dotenv import load_dotenv
from lib.supa import service_client
from lib.parser import parse_trade_signal
from lib.llm_normalize import normalize_message
from datetime import datetime, timezone
# ========== State Constants ==========
load_dotenv()
print("DEBUG SUPABASE_URL:", os.getenv("SUPABASE_URL"))
BOT_TOKEN = os.getenv("BOT_TOKEN")  
sb = service_client()

ASKING_PATH, ASKING_ALERTS_PATH = range(2)
EDIT_STATE = 10
user_paths = {}
alerts_paths = {}

def _link_user(telegram_user) -> str:
    # returns user_id (uuid as string)
    res = sb.rpc("rpc_upsert_user_by_telegram", {
        "p_telegram_id": str(telegram_user.id),
        "p_username": telegram_user.username or ""
    }).execute()
    return res.data

def _get_payload_for_uim(uim_id: int):
    """Return merged payload: edited_json over parsed_json."""
    uim = (sb.table("user_inbound_messages")
             .select("edited_json,inbound_message_id")
             .eq("id", uim_id).single().execute().data)
    inbound = (sb.table("inbound_messages")
                 .select("parsed_json")
                 .eq("id", uim["inbound_message_id"]).single().execute().data)
    base = inbound.get("parsed_json") or {}
    edited = uim.get("edited_json") or {}
    merged = {**base, **{k:v for k,v in edited.items() if v not in (None, "", [])}}
    return merged

def _patch_edited_json(uim_id: int, patch: dict):
    uim = (sb.table("user_inbound_messages")
             .select("edited_json")
             .eq("id", uim_id).single().execute().data)
    edited = uim.get("edited_json") or {}
    edited.update(patch)
    sb.table("user_inbound_messages").update({"edited_json": edited}).eq("id", uim_id).execute()



# ========== Inline Keyboard ==========
def main_menu():
    keyboard = [
        [InlineKeyboardButton("Buy", callback_data="buy_prompt")],
        [InlineKeyboardButton("Sell", callback_data="sell_prompt")],
        [InlineKeyboardButton("Set Alert", callback_data="set_alert_prompt")],
         [InlineKeyboardButton("Sources (Subscribe)", callback_data="show_sources")],  # NEW
        [InlineKeyboardButton("Parse Signal", callback_data="parse_signal")]
    ]
    return InlineKeyboardMarkup(keyboard)

# ========== Start Command ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = _link_user(update.effective_user)
    sb.table("user_settings").upsert({"user_id": user_id, "copy_mode": "pending"}).execute()
    await context.bot.send_message(
        chat_id=update.effective_user.id,
        text="👋 Welcome! Choose a function or type a command:",
        reply_markup=main_menu()
    )


async def save_orders_path(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = _link_user(update.effective_user)
    path = update.message.text.strip()
    # store regardless of local existence check (VM/remote)
    sb.table("user_settings").upsert({"user_id": user_id, "orders_path": path}).execute()
    await update.message.reply_text("✅ Path saved! Use /buy or /sell.")
    return ConversationHandler.END

async def show_sources_btn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = _link_user(q.from_user)
    text, markup = _render_sources_markup(user_id)
    await q.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")


async def set_copy_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = _link_user(update.effective_user)
    mode = (context.args[0].lower() if context.args else "")
    if mode not in ("auto","pending"):
      await update.message.reply_text("Usage: /setcopymode auto|pending")
      return
    sb.table("user_settings").upsert({"user_id": user_id, "copy_mode": mode}).execute()
    await update.message.reply_text(f"✅ Copy mode set to *{mode}*.", parse_mode="Markdown")

def _load_user_settings(user_id: str):
    q = sb.table("user_settings").select("*").eq("user_id", user_id).limit(1).execute()
    return (q.data or [{}])[0]

def _pick_account_id(user_id: str) -> str:
    s = _load_user_settings(user_id)
    if s.get("default_account_id"):
        return s["default_account_id"]
    q = sb.table("accounts").select("id").eq("user_id", user_id).eq("status","active").limit(1).execute()
    if not q.data:
        raise RuntimeError("No active account configured.")
    return q.data[0]["id"]
# ------- BUY / SELL → signal + (auto/pending) order via RPC -------
def _mk_meta(symbol: str, volume: float, sl=None, tp=None):
    meta = {"symbol": symbol, "side": None, "size": volume}
    if sl is not None: meta["sl"] = sl
    if tp is not None: meta["tp"] = tp
    return meta

async def _place_order(update: Update, ctx: ContextTypes.DEFAULT_TYPE, side: str):
    try:
        user_id = _link_user(update.effective_user)
        args = ctx.args
        symbol = args[0].upper()
        volume = float(args[1])
        sl = None; tp = None  # (optional: read from args)
        account_id = _pick_account_id(user_id)

        # 1) create signal
        sig = sb.rpc("rpc_create_signal", {
            "p_master_id": user_id,
            "p_symbol": symbol,
            "p_side": side,
            "p_size": volume,
            "p_sl": sl,
            "p_tp": json.dumps(tp) if tp else json.dumps([])
        }).execute().data

        # 2) queue order (auto vs pending)
        res = sb.rpc("rpc_queue_order_with_approval", {
            "p_user_id": user_id,
            "p_account_id": account_id,
            "p_signal_id": sig["id"],
            "p_client_order_id": f"tg-{update.message.id}",
            "p_meta": _mk_meta(symbol, volume, sl, tp)
        }).execute().data

        order = res["order"]
        appr = res.get("approval")

        if appr:
            token = appr["callback_token"]
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Yes", callback_data=f"appr:{token}:yes"),
                InlineKeyboardButton("❌ No",  callback_data=f"appr:{token}:no")
            ]])
            await update.message.reply_text(
                f"Approve order?\n{side.upper()} {symbol} x {volume}",
                reply_markup=kb
            )
        else:
            await update.message.reply_text(
                f"Queued (auto): {side.upper()} {symbol} x {volume}\nOrder: {order['id']}"
            )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _place_order(update, ctx, "buy")

async def sell(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _place_order(update, ctx, "sell")

# ------- Inline approval callback -------
async def handle_approval(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        _, token, decision = q.data.split(":", 2)  # "appr:<token>:yes|no"
        res = sb.rpc("rpc_record_approval", {
            "p_callback_token": token,
            "p_decision": decision
        }).execute().data
        new_status = res["order"]["status"]
        await q.edit_message_text(f"Decision recorded: {decision.upper()} ➜ order {new_status}")
    except Exception as e:
        await q.edit_message_text(f"❌ Approval error: {e}")

# ========== Inline Button Logic ==========
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    chat_id = query.from_user.id

    if data == "set_orders_path":
        await context.bot.send_message(chat_id=chat_id, text="Click or type /setorderspath")
    elif data == "set_alerts_path":
        await context.bot.send_message(chat_id=chat_id, text="Click or type /setalertspath")
    elif data == "buy_prompt":
        await context.bot.send_message(chat_id=chat_id, text="✏️ Use /buy SYMBOL VOLUME (e.g., /buy EURUSD 0.1)")
    elif data == "sell_prompt":
        await context.bot.send_message(chat_id=chat_id, text="✏️ Use /sell SYMBOL VOLUME (e.g., /sell GBPUSD 0.2)")
    elif data == "set_alert_prompt":
        await context.bot.send_message(chat_id=chat_id,text="✏️ Use /alert SYMBOL PRICE above|below\nExample: `/alert EURUSD 1.1050 above`")


# ========== Alert Function Logic ==========
async def alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = str(update.effective_user.id)
        if user_id not in alerts_paths:
            await context.bot.send_message(chat_id=update.effective_user.id, text="⚠️ Set your alerts path first using /setalertspath.")
            return

        symbol = context.args[0].upper()
        price = float(context.args[1])
        above = context.args[2].lower() == "above"

        alert = {
            "symbol": symbol,
            "price": price,
            "above": above,
            "triggered": False
        }

        filepath = os.path.join(alerts_paths[user_id], 'alerts.json')

        alerts = []
        if os.path.exists(filepath):
            with open(filepath, 'r') as f:
                try:
                    loaded = json.load(f)
                    alerts = loaded if isinstance(loaded, list) else [loaded]
                except json.JSONDecodeError:
                    alerts = []

        alerts.append(alert)

        with open(filepath, 'w') as f:
            json.dump(alerts, f, indent=2)

        await context.bot.send_message(
            chat_id=update.effective_user.id,
            text=f"✅ Alert set!\n\nSymbol: {symbol}\nPrice: {price}\nDirection: {'Above' if above else 'Below'}",
            reply_markup=main_menu()
        )

    except Exception as e:
        await context.bot.send_message(chat_id=update.effective_user.id, text=f"❌ Error: {e}")

# ===== Subscriptions UX (inline buttons) =====

def _fetch_sources():
    res = sb.table("group_sources").select("id,title,chat_id").execute()
    return res.data or []

def _subscribed_source_ids(user_id: str):
    q = (
        sb.table("copy_routes")
          .select("source_id")
          .eq("follower_user_id", user_id)
          .eq("active", True)
          .execute()
    )
    return {row["source_id"] for row in (q.data or [])}

def _render_sources_markup(user_id: str):
    """Return (text, InlineKeyboardMarkup) showing all sources with per-row Subscribe/Unsubscribe buttons."""
    sources = _fetch_sources()
    subs = _subscribed_source_ids(user_id)

    lines = ["📡 *Monitoring Sources*"]
    keyboard = []
    for s in sources:
        sid = s["id"]
        title = s.get("title") or s["chat_id"]
        is_sub = sid in subs
        state = "Subscribed ✅" if is_sub else "Not subscribed"
        lines.append(f"• *{title}*\n  `{sid}`\n  _{state}_")

        if is_sub:
            keyboard.append([InlineKeyboardButton("🛑 Unsubscribe", callback_data=f"src:unsub:{sid}")])
        else:
            keyboard.append([InlineKeyboardButton("➕ Subscribe", callback_data=f"src:sub:{sid}")])

    # Add a refresh button
    keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="src:refresh")])

    text = "\n\n".join(lines)
    return text, InlineKeyboardMarkup(keyboard)

async def sources(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Step 1–2: List all sources with inline buttons (Subscribe / Unsubscribe)."""
    user_id = _link_user(update.effective_user)  # returns UUID:contentReference[oaicite:1]{index=1}
    text, markup = _render_sources_markup(user_id)
    await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")

async def toggle_source(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Step 3–4: Handle Subscribe/Unsubscribe clicks, update DB, then re-render list."""
    q = update.callback_query
    await q.answer()
    user_id = _link_user(q.from_user)  # returns UUID:contentReference[oaicite:2]{index=2}

    data = q.data  # e.g., "src:sub:<source_id>" or "src:unsub:<source_id>" or "src:refresh"
    parts = data.split(":")
    if len(parts) == 2 and parts[1] == "refresh":
        text, markup = _render_sources_markup(user_id)
        await q.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
        return

    if len(parts) != 3:
        await q.edit_message_text("❌ Invalid action.")
        return

    _, action, source_id = parts

    try:
        if action == "sub":
            # Default target = current chat with the bot; can be changed later via a command/UI
            target_chat_id = str(q.message.chat_id)
            sb.table("copy_routes").upsert(
                {
                    "source_id": source_id,
                    "follower_user_id": user_id,
                    "target_chat_id": target_chat_id,
                    "active": True
                },
                on_conflict="source_id,follower_user_id"
            ).execute()

        elif action == "unsub":
            sb.table("copy_routes").upsert({
                "source_id": source_id,
                "follower_user_id": user_id,
                "target_chat_id": str(q.message.chat.id),
                "active": False
            }, on_conflict="source_id,follower_user_id").execute()
        else:
            await q.edit_message_text("❌ Unknown action.")
            return
    except Exception as e:
        await q.edit_message_text(f"❌ Error: {e}")
        return

    # Re-render updated list
    text, markup = _render_sources_markup(user_id)
    await q.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
# ========== Help Command Function ==========
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📘 *How to use this bot:*\n"
        "/buy SYMBOL VOLUME – Place a buy order (e.g., /buy EURUSD 0.1)\n"
        "/sell SYMBOL VOLUME – Place a sell order (e.g., /sell USDJPY 0.2)\n"
        "/alert SYMBOL PRICE above|below – Set a price alert\n"
        , parse_mode="Markdown"
    )
# ===== Review/Adjust coming from tele_agent buttons =====
async def handle_exec_choice(update, ctx):
    q = update.callback_query
    await q.answer()
    decision, uim_id = q.data.split(":")[1:]
    uim = (sb.table("user_inbound_messages")
             .select("id, user_id, inbound_message_id, status")
             .eq("id", int(uim_id)).single().execute().data)
    if not uim:
        return await q.edit_message_text("❌ Not found/expired.")

    if decision == "no":
        sb.table("user_inbound_messages").update({
            "status": "ignored", "decided_at": datetime.now(timezone.utc).isoformat()
        }).eq("id", uim["id"]).execute()
        return await q.edit_message_text("🚫 Ignored.")

# 2.a) Start review/adjust screen
async def handle_review(update, ctx):
    q = update.callback_query
    await q.answer()
    uim_id = int(q.data.split(":")[1])

    parsed = _get_payload_for_uim(uim_id)  # merged payload

    # Show current values & ask for optional edit
    summary = (
        f"Review/Adjust your order:\n"
        f"Symbol: {parsed.get('symbol')}\n"
        f"Side: {parsed.get('action')}\n"
        f"Entry: {parsed.get('entry_min')}–{parsed.get('entry_max')}\n"
        f"SL: {parsed.get('sl')}\n"
        f"TP: {', '.join([str(x) for x in (parsed.get('tp') or [])]) or '—'}\n\n"
        f"• Use the field buttons to tweak *one* value\n"
        f"• Or reply in one line to replace *all* values:\n"
        f"`SYMBOL SIDE ENTRY_MIN [ENTRY_MAX] SL TP1,TP2,...`"
        f"Example: `WLDUSDT buy 1.0413 1.1045 1.0132 1.1343,1.1641,1.1940,1.2834`\n"
        f"Or tap **Select Broker** to use current values."
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔤 Symbol",    callback_data=f"edit:symbol:{uim_id}"),
         InlineKeyboardButton("↕️ Side",      callback_data=f"edit:action:{uim_id}")],
        [InlineKeyboardButton("🎯 Entry Min", callback_data=f"edit:entry_min:{uim_id}"),
         InlineKeyboardButton("🎯 Entry Max", callback_data=f"edit:entry_max:{uim_id}")],
        [InlineKeyboardButton("🛑 Stop Loss", callback_data=f"edit:sl:{uim_id}"),
         InlineKeyboardButton("🎯 Targets",   callback_data=f"edit:tp:{uim_id}")],
        [InlineKeyboardButton("💼 Select Broker", callback_data=f"brokerlist:{uim_id}")],
        [InlineKeyboardButton("🚫 Cancel",        callback_data=f"exec:no:{uim_id}")]
    ])
    ctx.user_data[f"await_edit_{uim_id}"] = True
    await q.edit_message_text(summary, parse_mode="Markdown", reply_markup=kb)


# 2.b) Handle user’s text edit (optional)
async def handle_adjust_message(update, ctx):
    text = (update.message.text or "").strip()
    # find any uim awaiting edit for this user
    awaiting = [k for k in list(ctx.user_data.keys()) if k.startswith("await_edit_") and ctx.user_data.get(k)]
    if not awaiting:
        return  # not in edit mode

    uim_id = int(awaiting[0].split("_")[-1])

    try:
        # Parse simple line: SYMBOL SIDE ENTRY_MIN [ENTRY_MAX] SL TP1,TP2,...
        parts = text.replace(",", " ").split()
        if len(parts) < 5:
            return await update.message.reply_text("Format error. Need: SYMBOL SIDE ENTRY_MIN [ENTRY_MAX] SL TP1,TP2,...")

        symbol = parts[0]
        side   = parts[1].lower()  # buy/sell
        entry_min = float(parts[2])
        idx = 3
        entry_max = None
        # If the “next” token looks like a price and we still have enough tokens for SL + at least one TP
        if len(parts) >= 6:
            try:
                maybe_max = float(parts[3])
                entry_max = maybe_max
                idx = 4
            except:
                pass
        sl = float(parts[idx]); idx += 1
        tps = [float(p) for p in parts[idx:]] if idx < len(parts) else []

        edited = {
            "symbol": symbol,
            "action": side,
            "entry_min": entry_min,
            "entry_max": entry_max,
            "sl": sl,
            "tp": tps
        }

        # Save edits (DB preferred)
        sb.table("user_inbound_messages").update({"edited_json": edited}).eq("id", uim_id).execute()
        # clear flag
        ctx.user_data.pop(f"await_edit_{uim_id}", None)

        # Prompt broker selection
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("💼 Select Broker", callback_data=f"brokerlist:{uim_id}")]])
        await update.message.reply_text("✅ Edits saved. Now select a broker:", reply_markup=kb)

    except Exception as e:
        await update.message.reply_text(f"Parse error: {e}")

# 2.c) Start an edit for one field
async def handle_edit_field(update, ctx):
    q = update.callback_query
    await q.answer()
    _, field, uim_id = q.data.split(":")
    uim_id = int(uim_id)

    parsed = _get_payload_for_uim(uim_id)
    current = parsed.get(field)
    ctx.user_data["edit_field"] = (uim_id, field)
    ctx.user_data.pop(f"await_edit_{uim_id}", None)  # pause bulk while single-field edit is active
    await q.message.reply_text("Reply here with the new value:", reply_markup=ForceReply(selective=True))
    hint = "comma-separated (e.g. 1.1343,1.1641)" if field == "tp" else "a single value"
    await q.edit_message_text(
        f"Send *{field}* ({hint}). Current: `{current}`\n\n",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=f"review:{uim_id}")]])
    )

# 2.d) Handle In-line edit button values
async def handle_edit_value(update, ctx):
    if "edit_field" not in ctx.user_data:
        return
    uim_id, field = ctx.user_data.pop("edit_field")
    raw = (update.message.text or "").strip()

    try:
        if field in ("entry_min", "entry_max", "sl"):
            val = float(raw)
            _patch_edited_json(uim_id, {field: val})
        elif field == "tp":
            # allow comma/space separated list
            parts = [p for p in re.split(r"[,\s]+", raw) if p]
            tps = [float(p) for p in parts]
            _patch_edited_json(uim_id, {"tp": tps})
        elif field == "action":
            side = raw.lower().strip()
            if side not in ("buy", "sell", "long", "short"):
                return await update.message.reply_text("Use one of: buy/sell/long/short")
            # normalize to buy/sell
            side = "buy" if side in ("buy", "long") else "sell"
            _patch_edited_json(uim_id, {"action": side})
        elif field == "symbol":
            _patch_edited_json(uim_id, {"symbol": raw.upper()})
        else:
            return await update.message.reply_text("Unknown field.")
    except Exception as e:
        return await update.message.reply_text(f"Parse error: {e}")

    # show updated summary again
    parsed = _get_payload_for_uim(uim_id)
    summary = (
        f"Updated:\n"
        f"*Symbol*: {parsed.get('symbol')}\n"
        f"*Side*: {parsed.get('action')}\n"
        f"*Entry*: {parsed.get('entry_min')}–{parsed.get('entry_max')}\n"
        f"*SL*: {parsed.get('sl')}\n"
        f"*TP*: {', '.join([str(x) for x in (parsed.get('tp') or [])]) or '—'}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💼 Select Broker", callback_data=f"brokerlist:{uim_id}")],
        [InlineKeyboardButton("⬅️ More Edits",   callback_data=f"review:{uim_id}")],
    ])
    await update.message.reply_text(summary, parse_mode="Markdown", reply_markup=kb)



# 2.e) Show broker list and then finalize order
async def handle_brokerlist(update, ctx):
    q = update.callback_query
    await q.answer()
    uim_id = int(q.data.split(":")[1])

    uim = (sb.table("user_inbound_messages")
             .select("id,user_id,edited_json,inbound_message_id")
             .eq("id", uim_id).single().execute().data)

    # List accounts for this user
    accts = (sb.table("accounts")
               .select("id, broker")
               .eq("user_id", uim["user_id"])
               .eq("status", "active")
               .execute().data) or []
    if not accts:
        return await q.edit_message_text("⚠️ No active accounts found. Add one first.")

    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(a["broker"], callback_data=f"broker:{a['id']}:{uim_id}")]
         for a in accts]
    )
    await q.edit_message_text("Select broker:", reply_markup=kb)


async def handle_broker_choice(update, ctx):
    q = update.callback_query
    await q.answer()
    _, account_id, uim_id = q.data.split(":")
    uim_id = int(uim_id)

    # Load edited or original parsed
    uim = (sb.table("user_inbound_messages")
             .select("id,user_id,edited_json,inbound_message_id")
             .eq("id", uim_id).single().execute().data)
    inbound = (sb.table("inbound_messages")
             .select("parsed_json, source_id")   # <-- source_id IS group_sources.id
             .eq("id", uim["inbound_message_id"]).single().execute().data)

    parsed = _get_payload_for_uim(uim_id)
    required = ("symbol", "action", "entry_min")
    # send a separate debug message (don’t edit the same one)
    await q.message.reply_text("Order Review:\n" + json.dumps(parsed, indent=2))
    # normalize keys before the required check
    side = parsed.get("side") or parsed.get("action")
    entry_min = parsed.get("entry_min")
    if entry_min is None and isinstance(parsed.get("entry"), (int, float)):
        entry_min = parsed["entry"]  # fallback if your parser used 'entry'

    if not (parsed.get("symbol") and side and entry_min):
        return await q.edit_message_text("❌ Invalid order payload. Try editing again.")

    group_source_id = inbound.get("source_id")  # this will be your group_sources.id
    # Create signal (now with group_source_id)
    sig_id = (sb.rpc("rpc_create_signal", {
        "p_master_id": uim["user_id"],
        "p_symbol": parsed["symbol"],
        "p_side": (parsed.get("side") or parsed.get("action")),   # normalize
        "p_size": 0.01,                                            # TODO: user setting
        "p_sl": parsed.get("sl"),
        "p_tp": parsed.get("tp"),
        "p_group_source_id": group_source_id
    }).execute().data)
    # Supabase py client may return a list; normalize:
    if isinstance(sig_id, list):
        sig_id = sig_id[0]

    # Create order (per-user)
    sb.rpc("rpc_create_order", {
        "p_user_id": uim["user_id"],
        "p_account_id": account_id,                # from the broker button
        "p_signal_id": sig_id,
        "p_client_order_id": f"tg:{uim_id}",       # per-user idempotency
        "p_meta": {"uim_id": uim_id}               # send dict -> jsonb
    }).execute()

    # Mark this user's decision as executed
    sb.table("user_inbound_messages").update({
        "status": "executed",
        "decided_at": datetime.now(timezone.utc).isoformat()
    }).eq("id", uim_id).execute()

    await q.edit_message_text(f"✅ Order saved for {parsed['symbol']} ({parsed['action'].upper()})")

async def log_chat_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    origin = getattr(update.message, "forward_origin", None)

    if origin and getattr(origin, "chat", None):
        src_id = origin.chat.id          # <-- this is the group/channel ID
        src_title = origin.chat.title
        print(f"[FORWARDED FROM] id={src_id} title={src_title}")
        await update.message.reply_text(f"Forwarded from:\nID: `{src_id}`\nTitle: {src_title or '(no title)' }",
                                        parse_mode="Markdown")
    else:
        # Forward origin hidden or not a forward. Fall back guidance:
        await update.message.reply_text(
            "Couldn’t read the source chat id from this forward.\n"
            "• Some groups/channels hide forward origin.\n"
            "• Add the bot to the group and send any message, or /id there, to capture its chat id."
        )

    # optional: bring user back to your menu
    await ctx.bot.send_message(chat_id=update.effective_user.id,
                               text="👋 Choose a function:",
                               reply_markup=main_menu())

# ========== Setup ==========
app = ApplicationBuilder().token(BOT_TOKEN).build()



# Specific callbacks first
app.add_handler(CallbackQueryHandler(handle_exec_choice,   pattern=r"^exec:(yes|no):"))
app.add_handler(CallbackQueryHandler(show_sources_btn,     pattern=r"^show_sources$"))
app.add_handler(CallbackQueryHandler(toggle_source,        pattern=r"^src:(sub|unsub|refresh)"))
app.add_handler(CallbackQueryHandler(handle_review,        pattern=r"^review:\d+$"))
app.add_handler(CallbackQueryHandler(handle_edit_field,    pattern=r"^edit:(symbol|action|entry_min|entry_max|sl|tp):\d+$"))
app.add_handler(CallbackQueryHandler(handle_brokerlist,    pattern=r"^brokerlist:\d+$"))
app.add_handler(CallbackQueryHandler(handle_broker_choice, pattern=r"^broker:[a-zA-Z0-9\-]+:\d+$"))

# Text replies for single-field edits (don’t force REPLY; we use ctx.user_data["edit_field"])
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_value))

# Bulk one-line edits (armed by ctx.user_data[f"await_edit_{uim_id}"])
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_adjust_message))

# Commands
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("buy", buy))
app.add_handler(CommandHandler("sell", sell))
app.add_handler(CommandHandler("setcopymode", set_copy_mode))
app.add_handler(CommandHandler("sources", sources))

# ONE catch-all, LAST
app.add_handler(MessageHandler(filters.ALL, log_chat_id))

# Inline button handler
app.add_handler(CallbackQueryHandler(handle_button))

print("🤖 Bot is running...")
app.run_polling()
