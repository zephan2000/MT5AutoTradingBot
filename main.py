import json
import os
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

BOT_TOKEN = 'YOUR_BOT_TOKEN'  # <-- Replace this
OUTPUT_DIR = 'C:/Program Files/MetaTrader 5/MQL5/Files/'  # Adjust to match your MT5 Files path

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        symbol = context.args[0]
        volume = float(context.args[1])
        order = {
            "action": "buy",
            "symbol": symbol,
            "volume": volume
        }
        filepath = os.path.join(OUTPUT_DIR, 'order.json')
        with open(filepath, 'w') as f:
            json.dump(order, f)
        await update.message.reply_text(f"🟢 Buy order saved: {symbol}, {volume} lots")
    except Exception as e:
        await update.message.reply_text("❌ Error: " + str(e))

async def sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        symbol = context.args[0]
        volume = float(context.args[1])
        order = {
            "action": "sell",
            "symbol": symbol,
            "volume": volume
        }
        filepath = os.path.join(OUTPUT_DIR, 'order.json')
        with open(filepath, 'w') as f:
            json.dump(order, f)
        await update.message.reply_text(f"🔴 Sell order saved: {symbol}, {volume} lots")
    except Exception as e:
        await update.message.reply_text("❌ Error: " + str(e))

app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("buy", buy))
app.add_handler(CommandHandler("sell", sell))

print("Bot is running...")
app.run_polling()
