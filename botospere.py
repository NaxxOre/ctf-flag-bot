import os
import logging
import random
import asyncio
from datetime import datetime
import html

from pymongo import MongoClient
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from telegram.error import TimedOut, BadRequest

# Optional dotenv support
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("[⚠️] python-dotenv not installed; ensure env vars are set externally.")

# Environment variables
TOKEN = os.getenv("TOKEN")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
MONGO_URI = os.getenv("MONGO_URI")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()

# MongoDB setup
client = MongoClient(MONGO_URI)
db = client.ctfbot
users = db.users
flags = db.flags
submissions = db.submissions
admins = db.admins

# Conversation states
SELECT_CHALLENGE, WAIT_FLAG, AF_NAME, AF_POINTS, AF_LINK, AF_FLAG = range(6)

# Pagination settings
ITEMS_PER_PAGE = 10
SUBMISSIONS_PER_PAGE = 20

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# GIF URLs
GIF_CORRECT = ["https://tenor.com/bCCX9.gif"]
GIF_WRONG = ["https://tenor.com/Agkx.gif"]


# ────────────────────────────── Helpers ─────────────────────────────────────────

def is_admin(username: str | None) -> bool:
    return bool(username) and (
        username == ADMIN_USERNAME or admins.find_one({"username": username})
    )

async def add_user_if_not_exists(user_id: int, username: str | None):
    users.update_one(
        {"_id": user_id},
        {
            "$set": {"username": username or "Unknown"},
            "$setOnInsert": {"points": 0},
        },
        upsert=True,
    )

async def get_unsolved_challenges(user_id: int) -> list[str]:
    all_chals = [c["_id"] for c in flags.find()]
    solved = [
        s["challenge"]
        for s in submissions.find({"user_id": user_id, "correct": True})
    ]
    return [ch for ch in all_chals if ch not in solved]

def build_menu(items, page, prefix, items_per_page=ITEMS_PER_PAGE):
    """Generic paginated inline‑keyboard builder."""
    start, end = page * items_per_page, (page + 1) * items_per_page
    page_items = items[start:end]
    kb = [[InlineKeyboardButton(it, callback_data=f"{prefix}:noop")] for it in page_items]

    nav = []
    if page:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"{prefix}:{page-1}:nav"))
    if end < len(items):
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"{prefix}:{page+1}:nav"))
    if nav:
        kb.append(nav)
    return kb

def build_submissions_message(submissions_list, page):
    start, end = page * SUBMISSIONS_PER_PAGE, (page + 1) * SUBMISSIONS_PER_PAGE
    page_subs = submissions_list[start:end]
    lines = []
    for r in page_subs:
        ts = r.get("timestamp", r["_id"].generation_time).strftime("%Y-%m-%d %H:%M:%S")
        user_doc = users.find_one({"_id": r["user_id"]}) or {}
        uname = user_doc.get("username") or "Unknown"
        status = "Correct" if r["correct"] else "Wrong"
        lines.append(f"{ts} - @{uname} - {r['challenge']} - {r['submitted_flag']} - {status}")

    text = "📝 Submissions:\n" + "\n".join(lines)

    kb, nav = [], []
    if page:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"submissions:{page-1}:nav"))
    if end < len(submissions_list):
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"submissions:{page+1}:nav"))
    if nav:
        kb.append(nav)

    return text, kb


# ─────────────────────────── Command handlers ──────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to Csec CTF Flag Bot! Use /help to see available commands."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/submit – Start flag submission\n"
        "/myviewpoints – View your points\n"
        "/viewchallenges – List all challenges\n"
        "/leaderboard – View top users\n"
        "/addflag – (Admin) Add/update a challenge\n"
        "/addnewadmins <username> – (Admin) Grant admin rights\n"
        "/delete <challenge> – (Admin) Delete a challenge\n"
        "/viewusers – (Admin) View registered users\n"
        "/viewsubmissions – (Admin) View submissions log\n"
        "/bloods – View all challenges & their solvers\n"
        "/cancel – Cancel current operation"
    )


# ───── View challenges

async def view_challenges(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = [c["_id"] for c in flags.find()]
    if not rows:
        return await update.message.reply_text("No challenges available.")
    kb = [[InlineKeyboardButton(ch, callback_data=f"detail:{ch}")] for ch in rows]
    await update.message.reply_text("📋 Select a challenge:", reply_markup=InlineKeyboardMarkup(kb))

async def details_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    name = q.data.split(":", 1)[1]
    doc = flags.find_one({"_id": name}) or {}
    pts, link = doc.get("points", 0), doc.get("post_link", "")
    await q.edit_message_text(
        f"<b>{html.escape(name)}</b>\nPoints: {pts}\n<a href=\"{link}\">Post link</a>",
        parse_mode="HTML",
    )


# ───── Flag submission flow

async def submit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await add_user_if_not_exists(user.id, user.username)
    unsolved = await get_unsolved_challenges(user.id)
    if not unsolved:
        return await update.message.reply_text("🎉 All challenges solved!")

    kb = [[InlineKeyboardButton(ch, callback_data=f"submit:{ch}")] for ch in unsolved]
    await update.message.reply_text(
        "📋 Select a challenge to submit:", reply_markup=InlineKeyboardMarkup(kb)
    )
    return SELECT_CHALLENGE

async def select_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chal = q.data.split(":", 1)[1]
    context.user_data["challenge"] = chal
    await q.edit_message_text(
        f"🚩 Submit flag for <b>{html.escape(chal)}</b>:\n<i>Please send only the flag.</i>",
        parse_mode="HTML",
    )
    return WAIT_FLAG

async def receive_flag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles plain‑text messages during the WAIT_FLAG state.

    Quick guard so it *ignores* messages that are not part of a submission
    (e.g. admin replying in /addflag).
    """
    chal = context.user_data.get("challenge")
    if not chal:
        return  # Not in a flag‑submission conversation → ignore.

    user = update.effective_user
    flag_text = update.message.text.strip()
    doc = flags.find_one({"_id": chal})
    if not doc:
        await update.message.reply_text("❗ Challenge not found.")
        return ConversationHandler.END

    correct = flag_text == doc["flag"]
    pts = doc.get("points", 0)

    submissions.insert_one(
        {
            "user_id": user.id,
            "challenge": chal,
            "submitted_flag": flag_text,
            "correct": correct,
            "timestamp": datetime.utcnow(),
        }
    )

    if correct:
        users.update_one(
            {"_id": user.id},
            {"$inc": {"points": pts}, "$set": {"last_correct_submission": datetime.utcnow()}},
        )
        await update.message.reply_text(f"✅ Correct! You earned {pts} points.")
        await update.message.reply_animation(random.choice(GIF_CORRECT))
    else:
        await update.message.reply_text("❌ Incorrect. Try again with /submit")
        await update.message.reply_animation(random.choice(GIF_WRONG))

    context.user_data.pop("challenge", None)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❎ Operation cancelled.")
    return ConversationHandler.END


# ───── Misc user commands

async def my_viewpoints(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    pts = (users.find_one({"_id": user.id}) or {}).get("points", 0)
    name = f"@{user.username}" if user.username else user.first_name or "User"
    await update.message.reply_text(f"👤 {name}, you have {pts} points.")


# ───── Leaderboard (paginated) – unchanged

async def leaderboard_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    all_users = list(users.find().sort([("points", -1), ("last_correct_submission", 1)]))
    if not all_users:
        return await update.message.reply_text("No users on the leaderboard yet.")

    context.user_data["leaderboard_list"] = all_users
    items = [
        f"{i+1}. @{html.escape(u.get('username') or 'Unknown')} — {u['points']} pts"
        for i, u in enumerate(all_users)
    ]
    kb = build_menu(items, 0, "lead")
    await update.message.reply_text(
        "<b>🏅 Leaderboard 🏅</b>\n\n" + "\n".join(items[:ITEMS_PER_PAGE]),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb),
    )

async def leaderboard_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, page, nav = q.data.split(":", 2)
    if nav != "nav":
        return
    page = int(page)
    all_users = context.user_data.get("leaderboard_list", [])
    items = [
        f"{i+1}. @{html.escape(u.get('username') or 'Unknown')} — {u['points']} pts"
        for i, u in enumerate(all_users)
    ]
    start, end = page * ITEMS_PER_PAGE, (page + 1) * ITEMS_PER_PAGE
    kb = build_menu(items, page, "lead")
    await q.edit_message_text(
        "<b>🏅 Leaderboard 🏅</b>\n\n" + "\n".join(items[start:end]),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb),
    )


# ───── Admin: users, submissions, bloods (unchanged) ─────
# … (all functions viewusers_start, viewusers_page, viewsubmissions, submissions_page,
#    bloods_* remain as in your original code) …

# ───── Admin: addnewadmins / delete challenge (unchanged) ─────
async def addnewadmins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.username):
        return await update.message.reply_text("❗ Unauthorized.")
    if len(context.args) != 1:
        return await update.message.reply_text("Usage: /addnewadmins <username>")
    new_admin = context.args[0].lstrip("@")
    admins.update_one({"username": new_admin}, {"$set": {"username": new_admin}}, upsert=True)
    await update.message.reply_text(f"✅ @{new_admin} is now an admin.")

# ─── /addflag conversation (unchanged logic) ───
async def addflag_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.username):
        await update.message.reply_text("❗ Unauthorized.")
        return ConversationHandler.END
    await update.message.reply_text("📝 Enter challenge name:")
    return AF_NAME

async def af_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["af_name"] = update.message.text.strip()
    await update.message.reply_text("🎯 Enter points value:")
    return AF_POINTS

async def af_points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["af_points"] = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("⚠️ Please enter a valid integer for points.")
        return AF_POINTS
    await update.message.reply_text("🔗 Enter Telegram post link:")
    return AF_LINK

async def af_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["af_link"] = update.message.text.strip()
    await update.message.reply_text("🚩 Enter the correct flag string:")
    return AF_FLAG

async def af_flag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name, pts, link = (
        context.user_data["af_name"],
        context.user_data["af_points"],
        context.user_data["af_link"],
    )
    flag_str = update.message.text.strip()
    flags.update_one(
        {"_id": name},
        {"$set": {"flag": flag_str, "points": pts, "post_link": link}},
        upsert=True,
    )
    await update.message.reply_text(f"✅ Challenge “{name}” saved with {pts} points.")
    return ConversationHandler.END


# ─────────────────────────── Bot initialisation ────────────────────────────────

def init_commands(app):
    async def on_startup(application):
        cmds = [
            BotCommand("start", "Start the bot"),
            BotCommand("help", "Show help"),
            BotCommand("submit", "Submit a flag"),
            BotCommand("myviewpoints", "View your points"),
            BotCommand("viewchallenges", "List all challenges"),
            BotCommand("leaderboard", "View top users"),
            BotCommand("bloods", "View all challenges & their solvers"),
            BotCommand("addflag", "Add/update a challenge"),
            BotCommand("addnewadmins", "Grant admin rights"),
            BotCommand("delete", "Delete a challenge"),
            BotCommand("viewusers", "View registered users"),
            BotCommand("viewsubmissions", "View submissions log"),
            BotCommand("cancel", "Cancel current operation"),
        ]
        await application.bot.set_my_commands(cmds)

    return on_startup

def main():
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(init_commands(None))
        .build()
    )

    # ───── Conversations FIRST (group 0) ─────
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("addflag", addflag_start)],
            states={
                AF_NAME:   [MessageHandler(filters.TEXT & ~filters.COMMAND, af_name)],
                AF_POINTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, af_points)],
                AF_LINK:   [MessageHandler(filters.TEXT & ~filters.COMMAND, af_link)],
                AF_FLAG:   [MessageHandler(filters.TEXT & ~filters.COMMAND, af_flag)],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
            per_user=True,
        ),
        group=0,
    )

    # ───── Regular handlers (group 0) ─────
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("myviewpoints", my_viewpoints))
    app.add_handler(CommandHandler("viewchallenges", view_challenges))
    app.add_handler(CommandHandler("submit", submit_start))
    app.add_handler(CallbackQueryHandler(select_challenge, pattern=r"^submit:.+"))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("leaderboard", leaderboard_start))
    app.add_handler(CallbackQueryHandler(leaderboard_page, pattern=r"^lead:\d+:nav$"))
    app.add_handler(CommandHandler("addnewadmins", addnewadmins))
    # … add the rest of your unchanged handlers here …

    # ───── Catch‑all text handler LAST (group 1) ─────
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, receive_flag),
        group=1,
    )

    # Error handler
    async def error_handler(update, context):
        logger.error("❌ Exception in handler:", exc_info=context.error)

    app.add_error_handler(error_handler)

    # Start via webhook or polling
    if WEBHOOK_URL:
        app.run_webhook(
            listen="0.0.0.0",
            port=int(os.getenv("PORT", 5000)),
            url_path="webhook",
            webhook_url=WEBHOOK_URL,
            drop_pending_updates=True,
        )
    else:
        app.run_polling()

if __name__ == "__main__":
    main()
