import os
import logging
import random
from datetime import datetime

from flask import Flask, request, abort
from pymongo import MongoClient

from telegram import Bot, Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)


# Optional dotenv support
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("[⚠️] python-dotenv not installed; ensure env vars are set externally.")

# Environment variables
TOKEN = os.getenv('TOKEN')
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME')
MONGO_URI = os.getenv('MONGO_URI')

# MongoDB setup
client = MongoClient(MONGO_URI)
db = client.ctfbot
users = db.users
flags = db.flags
submissions = db.submissions
admins = db.admins
application = ApplicationBuilder().token(TOKEN).build()


WEBHOOK_URL = os.getenv('WEBHOOK_URL')  # e.g. https://<your-render-service>.onrender.com/webhook

bot = Bot(token=TOKEN)
app = Flask(__name__)

# Conversation states
(
    SELECT_CHALLENGE,
    WAIT_FLAG,
    AF_NAME,
    AF_POINTS,
    AF_LINK,
    AF_FLAG
) = range(6)

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# GIF URLs (only correct/incorrect)
GIF_CORRECT = [
    "https://tenor.com/bCCX9.gif",
]
GIF_WRONG = [
    "https://tenor.com/Agkx.gif",
]

# Helper functions

def is_admin(username: str) -> bool:
    return (
        username == ADMIN_USERNAME
        or admins.find_one({"username": username}) is not None
    )

async def add_user_if_not_exists(user_id: int, username: str):
    users.update_one(
        {"_id": user_id},
        {"$setOnInsert": {"username": username, "points": 0}},
        upsert=True
    )

async def get_unsolved_challenges(user_id: int) -> list[str]:
    all_chals = [c['_id'] for c in flags.find()]
    solved = [s['challenge'] for s in submissions.find({"user_id": user_id, "correct": True})]
    return [ch for ch in all_chals if ch not in solved]

# Command handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 Welcome to Csec CTF flag Bot 👾\n"
        "🦾This bot is designed to Submit flags for CTF challenges from Csec Cyber_CTF learning Gp\n"
        "🎟Features\n"
        "🎗 Flag submission\n"
         "🎗View Challenges\n"
         "🎗Earn points\n"
        "🎗Leaderboard\n"
        "If you want to share CTF challenges or need help in solving one, you can create a challenge for everyone to think about and try to solve.\n"
        "Feel free to say something in the Csec Cyber_CTF Training Group to request if you really want to share challenges.\n"
        "https://t.me/+fFDeRvQtILU5MzZl\n"
        "Commands for managing challenges\n"
        "You can typically type just / for the bot to show you the commands.\n"
        "/help – View all the commands\n"
         "/submit – Start flag submission\n"
        "/myviewpoints – View your points\n"
        "/viewchallenges – List all challenges\n"
        "/leaderboard – View top users\n"
        "/cancel – Cancel current operation"
    )
    await update.message.reply_text(text)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/submit – Start flag submission\n"
        "/myviewpoints – View your points\n"
        "/viewchallenges – List all challenges\n"
        "/leaderboard – View top users\n"
        "/addflag – (Admin) Add/update a challenge\n"
        "/addnewadmins <username> – (Admin) Grant admin rights\n"
        "/delete <challenge> – (Admin) Delete a challenge\n"
        "/viewpoints – (Admin) View all users' points\n"
        "/viewusers – (Admin) View registered users\n"
        "/viewsubmissions – (Admin) View submissions log\n"
        "/cancel – Cancel current operation"
    )

# Submission handlers
async def submit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return ConversationHandler.END
    await add_user_if_not_exists(user.id, user.username)
    unsolved = await get_unsolved_challenges(user.id)
    if not unsolved:
        await update.message.reply_text("🎉 All challenges solved!")
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(ch, callback_data=ch)] for ch in unsolved]
    await update.message.reply_text(
        "📋 Select a challenge to submit:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SELECT_CHALLENGE

async def select_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chal = query.data
    context.user_data['challenge'] = chal
    await query.edit_message_text(
        f"🚩 Submit flag for *{chal}*:\n_Please send only the flag._",
        parse_mode='Markdown'
    )
    return WAIT_FLAG

async def receive_flag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chal = context.user_data.get('challenge')
    flag_text = update.message.text.strip()
    doc = flags.find_one({"_id": chal})
    if not doc:
        await update.message.reply_text("❗ Challenge not found.")
        return ConversationHandler.END
    correct = (flag_text == doc['flag'])
    pts = doc.get('points', 0)
    submissions.insert_one({
        "user_id": user.id,
        "challenge": chal,
        "submitted_flag": flag_text,
        "correct": correct,
        "timestamp": datetime.utcnow()
    })
    if correct:
        users.update_one({"_id": user.id}, {"$inc": {"points": pts}})
        await update.message.reply_text(f"✅ Correct! You earned {pts} points for {chal}!")
        await update.message.reply_animation(random.choice(GIF_CORRECT))
    else:
        await update.message.reply_text(f"❌ Incorrect for {chal}. Try again! /submit")
        await update.message.reply_animation(random.choice(GIF_WRONG))
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❎ Operation cancelled.")
    return ConversationHandler.END

# View commands
async def my_viewpoints(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    doc = users.find_one({"_id": user.id})
    pts = doc.get('points', 0) if doc else 0
    await update.message.reply_text(f"👤 @{user.username}, you have {pts} points.")

async def view_challenges(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = [c['_id'] for c in flags.find()]
    if not rows:
        await update.message.reply_text("No challenges available.")
        return
    keyboard = [[InlineKeyboardButton(ch, callback_data=ch)] for ch in rows]
    await update.message.reply_text("📋 Select a challenge:", reply_markup=InlineKeyboardMarkup(keyboard))

async def details_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    name = query.data
    doc = flags.find_one({"_id": name})
    pts = doc.get('points', 0)
    link = doc.get('post_link', '')
    await query.edit_message_text(f"*{name}*\nPoints: {pts}\n[Post Link]({link})", parse_mode='Markdown')

# Leaderboard command
async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    top_users = list(users.find().sort('points', -1).limit(10))
    if not top_users:
        await update.message.reply_text("No users on the leaderboard yet.")
        return

    lines = ["🏅 *Leaderboard* 🏅\n"]
    for rank, u in enumerate(top_users, start=1):
        username = u.get('username', f"<{u['_id']}>")
        pts = u.get('points', 0)
        lines.append(f"{rank}. @{username} — {pts} pts")

    await update.message.reply_text("\n".join(lines), parse_mode='Markdown')

# Admin commands
async def addnewadmins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # Log who is calling and with what args
    logger.info(f"[addnewadmins] @{user.username} args={context.args}")

    # 1) Authorization check
    if not is_admin(user.username):
        await update.message.reply_text(
            f"❗ @{user.username}, you are not authorized to add admins."
        )
        return

    # 2) Argument check
    if len(context.args) != 1:
        await update.message.reply_text("❗ Usage: /addnewadmins <username>")
        return

    # 3) Perform the upgrade
    new_admin = context.args[0].lstrip('@')
    admins.update_one(
        {"username": new_admin},
        {"$set": {"username": new_admin}},
        upsert=True
    )

    await update.message.reply_text(f"✅ @{new_admin} is now an admin.")


async def addflag_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.username):
        await update.message.reply_text("❗ Unauthorized.")
        return ConversationHandler.END
    await update.message.reply_text("📝 Enter challenge name:")
    return AF_NAME

async def af_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['af_name'] = update.message.text.strip()
    await update.message.reply_text("🎯 Enter points value:")
    return AF_POINTS

async def af_points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['af_points'] = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("⚠️ Please enter a valid integer for points.")
        return AF_POINTS
    await update.message.reply_text("🔗 Enter Telegram post link:")
    return AF_LINK

async def af_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['af_link'] = update.message.text.strip()
    await update.message.reply_text("🚩 Enter the correct flag string:")
    return AF_FLAG

async def af_flag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = context.user_data['af_name']
    pts = context.user_data['af_points']
    link = context.user_data['af_link']
    flag_str = update.message.text.strip()
    flags.update_one({"_id": name}, {"$set": {"flag": flag_str, "points": pts, "post_link": link}}, upsert=True)
    await update.message.reply_text(f"✅ Challenge '{name}' added/updated with {pts} points.")
    return ConversationHandler.END

async def delete_challenge(update:	Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.username):
        await update.message.reply_text("❗ You are not authorized.")
        return
    if not context.args or len(context.args) != 1:
        await update.message.reply_text("Usage: /delete <challenge>")
        return
    name = context.args[0]
    doc = flags.find_one({"_id": name})
    if not doc:
        await update.message.reply_text(f"❗ Challenge '{name}' does not exist.")
        return
    pts = doc.get('points', 0)
    for s in submissions.find({"challenge": name, "correct": True}):
        users.update_one({"_id": s['user_id']}, {"$inc": {"points": -pts}})
    submissions.delete_many({"challenge": name})
    flags.delete_one({"_id": name})
    await update.message.reply_text(f"✅ Challenge '{name}' and all related data deleted.")

async def viewpoints(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.username):
        await update.message.reply_text("❗ Unauthorized.")
        return
    rows = users.find().sort('points', -1)
    text = "🏆 Users Points:\n" + "\n".join(f"{u['username']}: {u['points']}" for u in rows)
    await update.message.reply_text(text)

async def viewusers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.username):
        await update.message.reply_text("❗ Unauthorized.")
        return
    rows = users.find()
    text = "👥 Registered Users:\n" + "\n".join(f"{u['_id']}: {u['username']}" for u in rows)
    await update.message.reply_text(text)

async def viewsubmissions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.username):
        await update.message.reply_text("❗ Unauthorized.")
        return
    rows = submissions.find().sort('timestamp', -1)
    lines = []
    for r in rows:
        ts = r.get('timestamp', r['_id'].generation_time)
        uname = users.find_one({'_id': r['user_id']})['username']
        status = 'Correct' if r['correct'] else 'Wrong'
        lines.append(f"{ts} - @{uname} - {r['challenge']} - {r['submitted_flag']} - {status}")
    await update.message.reply_text("📝 Submissions:\n" + "\n".join(lines))

# Register handlers and start bot
def init_commands(app):
    async def on_startup(application):
        commands = [
            BotCommand('start', 'Start the bot'),
            BotCommand('help', 'Show help'),
            BotCommand('submit', 'Submit a flag'),
            BotCommand('myviewpoints', 'View your points'),
            BotCommand('viewchallenges', 'List challenges'),
            BotCommand('leaderboard', 'View top users'),
            BotCommand('addflag', 'Add/update a challenge'),
            BotCommand('addnewadmins', 'Grant admin rights'),
            BotCommand('delete', 'Delete a challenge'),
            BotCommand('viewpoints', 'View all users points'),
            BotCommand('viewusers', 'View registered users'),
            BotCommand('viewsubmissions', 'View submissions log'),
            BotCommand('cancel', 'Cancel current operation')
        ]
        await application.bot.set_my_commands(commands)
    return on_startup

def main():
    on_startup = init_commands(None)
    app = (ApplicationBuilder()
           .token(TOKEN)
           .post_init(on_startup)
           .build())

    # Conversation handlers
    submit_conv = ConversationHandler(
        entry_points=[CommandHandler('submit', submit_start)],
        states={
            SELECT_CHALLENGE: [CallbackQueryHandler(select_challenge)],
            WAIT_FLAG: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_flag)]
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        per_user=True
    )
    addflag_conv = ConversationHandler(
        entry_points=[CommandHandler('addflag', addflag_start)],
        states={
            AF_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, af_name)],
            AF_POINTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, af_points)],
            AF_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, af_link)],
            AF_FLAG: [MessageHandler(filters.TEXT & ~filters.COMMAND, af_flag)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        per_user=True
    )


    # Add handlers
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('help', help_command))
    application.add_handler(CommandHandler('delete', delete_challenge))
    application.add_handler(CommandHandler('myviewpoints', my_viewpoints))
    application.add_handler(submit_conv)
    application.add_handler(addflag_conv)
    application.add_handler(CommandHandler('cancel', cancel))
    application.add_handler(CommandHandler('viewchallenges', view_challenges))
    application.add_handler(CallbackQueryHandler(details_challenge, pattern='^.+$'))
    application.add_handler(CommandHandler('viewpoints', viewpoints))
    application.add_handler(CommandHandler('viewusers', viewusers))
    application.add_handler(CommandHandler('addnewadmins', addnewadmins))

    application.add_handler(CommandHandler('viewsubmissions', viewsubmissions))
    application.add_handler(CommandHandler('leaderboard', leaderboard))

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json(force=True)
    update = Update.de_json(data, application.bot)
    asyncio.run_coroutine_threadsafe(application.process_update(update), application.loop)
    return 'OK'

import asyncio

if __name__ == '__main__':
    asyncio.run(application.bot.set_webhook(WEBHOOK_URL))
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
