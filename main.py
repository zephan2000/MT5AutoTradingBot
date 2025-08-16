import os, json, re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ConversationHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from uuid import UUID
from dotenv import load_dotenv
from lib.supa import service_client
# ========== State Constants ==========
load_dotenv()
print("DEBUG SUPABASE_URL:", os.getenv("SUPABASE_URL"))
BOT_TOKEN = os.getenv("BOT_TOKEN")  
sb = service_client()

ASKING_PATH, ASKING_ALERTS_PATH = range(2)
user_paths = {}
alerts_paths = {}

def _link_user(telegram_user) -> str:
    # returns user_id (uuid as string)
    res = sb.rpc("rpc_upsert_user_by_telegram", {
        "p_telegram_id": str(telegram_user.id),
        "p_username": telegram_user.username or ""
    }).execute()
    return res.data


# ========== Inline Keyboard ==========
def main_menu():
    keyboard = [
        [InlineKeyboardButton("Buy", callback_data="buy_prompt")],
        [InlineKeyboardButton("Sell", callback_data="sell_prompt")],
        [InlineKeyboardButton("Set Alert", callback_data="set_alert_prompt")],
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
# ========== Help Command Function ==========
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📘 *How to use this bot:*\n"
        "/buy SYMBOL VOLUME – Place a buy order (e.g., /buy EURUSD 0.1)\n"
        "/sell SYMBOL VOLUME – Place a sell order (e.g., /sell USDJPY 0.2)\n"
        "/alert SYMBOL PRICE above|below – Set a price alert\n"
        , parse_mode="Markdown"
    )


# ========== Setup ==========
app = ApplicationBuilder().token(BOT_TOKEN).build()

# Command handlers
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("buy", buy))
app.add_handler(CommandHandler("sell", sell))
app.add_handler(CommandHandler("setcopymode", set_copy_mode))
app.add_handler(CallbackQueryHandler(handle_approval, pattern=r"^appr:"))


app.add_handler(CallbackQueryHandler(lambda u,c: c.bot.send_message(
    chat_id=u.callback_query.from_user.id,
    text="Feature in Phase 2/3"
), pattern="^(buy_prompt|sell_prompt|setorderspath|set_alerts_path)$"))


# Inline button handler
app.add_handler(CallbackQueryHandler(handle_button))

print("🤖 Bot is running...")
app.run_polling()
