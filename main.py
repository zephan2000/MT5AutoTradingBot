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
ASKING_PATH = 1
user_paths = {}

# ========== Inline Keyboard ==========
def main_menu():
    keyboard = [
        [InlineKeyboardButton("Buy", callback_data="buy_prompt")],
        [InlineKeyboardButton("Sell", callback_data="sell_prompt")],
        [InlineKeyboardButton("Set MT5 Path", callback_data="set_path")]
    ]
    return InlineKeyboardMarkup(keyboard)

# ========== Start Command ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=update.effective_user.id,
        text="👋 Welcome! Choose a function or type a command:",
        reply_markup=main_menu()
    )

# ========== Set Path ==========
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
        with open(filepath, 'w') as f:
            json.dump(order, f)

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
        with open(filepath, 'w') as f:
            json.dump(order, f)

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
        # 👇 This triggers the /setpath command, which is linked to the ConversationHandler
        await context.bot.send_message(chat_id=chat_id, text="Click or type /setpath")
    elif data == "buy_prompt":
        await context.bot.send_message(chat_id=chat_id, text="✏️ Use /buy SYMBOL VOLUME (e.g., /buy EURUSD 0.1)")
    elif data == "sell_prompt":
        await context.bot.send_message(chat_id=chat_id, text="✏️ Use /sell SYMBOL VOLUME (e.g., /sell GBPUSD 0.2)")

# ========== Setup ==========
app = ApplicationBuilder().token(BOT_TOKEN).build()

# Command handlers
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("buy", buy))
app.add_handler(CommandHandler("sell", sell))

# Conversation for path input
conv_handler = ConversationHandler(
    entry_points=[CommandHandler("setpath", ask_for_path)],
    states={ASKING_PATH: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_path)]},
    fallbacks=[],
)
app.add_handler(conv_handler)

# Inline button handler
app.add_handler(CallbackQueryHandler(handle_button))

print("🤖 Bot is running...")
app.run_polling()
