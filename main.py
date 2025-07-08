import os
import json
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

# ========== State Constants ==========
ASKING_PATH, ASKING_ALERTS_PATH = range(2)
user_paths = {}
alerts_paths = {}

# ========== Inline Keyboard ==========
def main_menu():
    keyboard = [
        [InlineKeyboardButton("Buy", callback_data="buy_prompt")],
        [InlineKeyboardButton("Sell", callback_data="sell_prompt")],
        [InlineKeyboardButton("Set Orders Path", callback_data="set_path")],
        [InlineKeyboardButton("Set Alerts Path", callback_data="set_alerts_path")],
        [InlineKeyboardButton("Set Alert", callback_data="set_alert_prompt")]
    ]
    return InlineKeyboardMarkup(keyboard)

# ========== Start Command ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=update.effective_user.id,
        text="👋 Welcome! Choose a function or type a command:",
        reply_markup=main_menu()
    )

# ========== Set MT5 Path ==========
async def ask_for_path(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=update.effective_user.id,
        text="📁 Please send your MT5 Files folder path."
    )
    return ASKING_PATH

async def save_path(update: Update, context: ContextTypes.DEFAULT_TYPE):
    path = update.message.text.strip()
    if not os.path.exists(path):
        await update.message.reply_text("❌ That path doesn't exist. Try again.")
        return ASKING_PATH

    user_id = str(update.effective_user.id)
    user_paths[user_id] = path
    await context.bot.send_message(
        chat_id=update.effective_user.id,
        text="✅ Path saved! You can now use /buy or /sell.",
        reply_markup=main_menu()
    )
    return ConversationHandler.END

# ========== Set Alerts Path ==========
async def ask_for_alerts_path(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=update.effective_user.id,
        text="📂 Please send the folder path where MT5 will read alerts.json"
    )
    return ASKING_ALERTS_PATH

async def save_alerts_path(update: Update, context: ContextTypes.DEFAULT_TYPE):
    path = update.message.text.strip()
    if not os.path.exists(path):
        await update.message.reply_text("❌ That path doesn't exist. Try again.")
        return ASKING_ALERTS_PATH

    user_id = str(update.effective_user.id)
    alerts_paths[user_id] = path
    await context.bot.send_message(
        chat_id=update.effective_user.id,
        text="✅ Alerts path saved!",
        reply_markup=main_menu()
    )
    return ConversationHandler.END

# ========== Buy & Sell ==========
async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = str(update.effective_user.id)
        if user_id not in user_paths:
            await context.bot.send_message(chat_id=update.effective_user.id, text="⚠️ Set your MT5 path first using /setpath.")
            return

        symbol = context.args[0]
        volume = float(context.args[1])
        order = {"action": "buy", "symbol": symbol, "volume": volume}
        filepath = os.path.join(user_paths[user_id], 'order.json')

        orders = []
        if os.path.exists(filepath):
            with open(filepath, 'r') as f:
                try:
                    loaded = json.load(f)
                    orders = loaded if isinstance(loaded, list) else [loaded]
                except json.JSONDecodeError:
                    orders = []

        orders.append(order)

        with open(filepath, 'w') as f:
            json.dump(orders, f, indent=2)

        await context.bot.send_message(chat_id=update.effective_user.id, text=f"🟢 Buy order saved: {symbol}, {volume} lots")
    except Exception as e:
        await context.bot.send_message(chat_id=update.effective_user.id, text=f"❌ Error: {e}")

async def sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = str(update.effective_user.id)
        if user_id not in user_paths:
            await context.bot.send_message(chat_id=update.effective_user.id, text="⚠️ Set your MT5 path first using /setpath.")
            return

        symbol = context.args[0]
        volume = float(context.args[1])
        order = {"action": "sell", "symbol": symbol, "volume": volume}
        filepath = os.path.join(user_paths[user_id], 'order.json')

        orders = []
        if os.path.exists(filepath):
            with open(filepath, 'r') as f:
                try:
                    loaded = json.load(f)
                    orders = loaded if isinstance(loaded, list) else [loaded]
                except json.JSONDecodeError:
                    orders = []

        orders.append(order)

        with open(filepath, 'w') as f:
            json.dump(orders, f, indent=2)

        await context.bot.send_message(chat_id=update.effective_user.id, text=f"🔴 Sell order saved: {symbol}, {volume} lots")
    except Exception as e:
        await context.bot.send_message(chat_id=update.effective_user.id, text=f"❌ Error: {e}")

# ========== Inline Button Logic ==========
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    chat_id = query.from_user.id

    if data == "set_path":
        await context.bot.send_message(chat_id=chat_id, text="Click or type /setpath")
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
        "/setpath – Set your MT5 orders file path\n"
        "/setalertspath – Set the alerts file folder path\n"
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
app.add_handler(CommandHandler("alert", alert))
app.add_handler(CommandHandler("help", help_cmd))

# Conversation handlers
path_conv = ConversationHandler(
    entry_points=[CommandHandler("setpath", ask_for_path)],
    states={ASKING_PATH: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_path)]},
    fallbacks=[],
)
alerts_conv = ConversationHandler(
    entry_points=[CommandHandler("setalertspath", ask_for_alerts_path)],
    states={ASKING_ALERTS_PATH: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_alerts_path)]},
    fallbacks=[],
)


app.add_handler(path_conv)
app.add_handler(alerts_conv)

# Inline button handler
app.add_handler(CallbackQueryHandler(handle_button))

print("🤖 Bot is running...")
app.run_polling()
