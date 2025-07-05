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
SUBMISSIONS_PER_PAGE = 20  # Added for submissions pagination

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# GIF URLs
GIF_CORRECT = ["https://tenor.com/bCCX9.gif"]
GIF_WRONG = ["https://tenor.com/Agkx.gif"]

# Helper functions
def is_admin(username: str) -> bool:
    return username == ADMIN_USERNAME or bool(admins.find_one({"username": username}))

async def add_user_if_not_exists(user_id: int, username: str):
    if username is None:
        username = "Unknown"
    users.update_one(
        {"_id": user_id},
        {"$set": {"username": username}, "$setOnInsert": {"points": 0}},
        upsert=True,
    )

async def get_unsolved_challenges(user_id: int) -> list[str]:
    all_chals = [c["_id"] for c in flags.find()]
    solved = [s["challenge"] for s in submissions.find({"user_id": user_id, "correct": True})]
    return [ch for ch in all_chals if ch not in solved]

# Build paginated keyboard (used for leaderboard, viewusers)
def build_menu(items, page, prefix, items_per_page=ITEMS_PER_PAGE):
    start = page * items_per_page
    end = start + items_per_page
    page_items = items[start:end]
    keyboard = []
    for item in page_items:
        keyboard.append([InlineKeyboardButton(item, callback_data=f"{prefix}:noop")])
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"{prefix}:{page-1}:nav"))
    if end < len(items):
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"{prefix}:{page+1}:nav"))
    if nav_buttons:
        keyboard.append(nav_buttons)
    logger.info(f"Generated keyboard for {prefix}, page {page}, callback data: {nav_buttons}")
    return keyboard

# Build submissions message with pagination
def build_submissions_message(submissions_list, page):
    start = page * SUBMISSIONS_PER_PAGE
    end = start + SUBMISSIONS_PER_PAGE
    page_submissions = submissions_list[start:end]
    lines = []
    for r in page_submissions:
        ts = r.get("timestamp", r["_id"].generation_time).strftime("%Y-%m-%d %H:%M:%S")
        user_doc = users.find_one({"_id": r["user_id"]})
        uname = user_doc.get("username") or "Unknown"
        status = "Correct" if r["correct"] else "Wrong"
        lines.append(f"{ts} - @{uname} - {r['challenge']} - {r['submitted_flag']} - {status}")
    text = "📝 Submissions:\n" + "\n".join(lines)
    
    keyboard = []
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"submissions:{page-1}:nav"))
    if end < len(submissions_list):
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"submissions:{page+1}:nav"))
    if nav_buttons:
        keyboard.append(nav_buttons)
    
    return text, keyboard

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
        "/viewusers – (Admin) View registered users\n"
        "/viewsubmissions – (Admin) View submissions log\n"
        "/bloods – View all challenges & their solvers\n"
        "/cancel – Cancel current operation"
    )

# View challenges → details
async def view_challenges(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = [c["_id"] for c in flags.find()]
    if not rows:
        await update.message.reply_text("No challenges available.")
        return
    keyboard = [[InlineKeyboardButton(ch, callback_data=f"detail:{ch}")] for ch in rows]
    await update.message.reply_text(
        "📋 Select a challenge:", reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def details_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    name = query.data.split(":", 1)[1]
    doc = flags.find_one({"_id": name})
    pts = doc.get("points", 0)
    link = doc.get("post_link", "")
    await query.edit_message_text(
        f"<b>{html.escape(name)}</b>\nPoints: {pts}\n<a href=\"{link}\">Post Link</a>",
        parse_mode="HTML"
    )

# Submission flow
async def submit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await add_user_if_not_exists(user.id, user.username)
    unsolved = await get_unsolved_challenges(user.id)
    if not unsolved:
        await update.message.reply_text("🎉 All challenges solved!")
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(ch, callback_data=f"submit:{ch}")] for ch in unsolved]
    await update.message.reply_text(
        "📋 Select a challenge to submit:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return SELECT_CHALLENGE

async def select_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chal = query.data.split(":", 1)[1]
    context.user_data["challenge"] = chal
    await query.edit_message_text(
        f"🚩 Submit flag for <b>{html.escape(chal)}</b>:\n<i>Please send only the flag.</i>",
        parse_mode="HTML",
    )
    return WAIT_FLAG

async def receive_flag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chal = context.user_data.get("challenge")
    flag_text = update.message.text.strip()
    doc = flags.find_one({"_id": chal})
    if not doc:
        await update.message.reply_text("❗ Challenge not found.")
        return ConversationHandler.END
    correct = flag_text == doc["flag"]
    pts = doc.get("points", 0)
    submissions.insert_one({
        "user_id": user.id,
        "challenge": chal,
        "submitted_flag": flag_text,
        "correct": correct,
        "timestamp": datetime.utcnow(),
    })
    if correct:
        users.update_one(
            {"_id": user.id},
            {
                "$inc": {"points": pts},
                "$set": {"last_correct_submission": datetime.utcnow()}
            }
        )
        await update.message.reply_text(
            f"✅ Correct! You earned {pts} points for {chal}!"
        )
        await update.message.reply_animation(random.choice(GIF_CORRECT))
    else:
        await update.message.reply_text(
            f"❌ Incorrect for {chal}. Try again with /submit"
        )
        await update.message.reply_animation(random.choice(GIF_WRONG))
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❎ Operation cancelled.")
    return ConversationHandler.END

# Other view commands
async def my_viewpoints(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    doc = users.find_one({"_id": user.id}) or {}
    pts = doc.get("points", 0)
    if user.username:
        name = f"@{user.username}"
    else:
        name = user.first_name or "User"
    await update.message.reply_text(f"👤 {name}, you have {pts} points.")

# Leaderboard with pagination (fixed username escaping)
async def leaderboard_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    all_users = list(users.find().sort([("points", -1), ("last_correct_submission", 1)]))
    if not all_users:
        await update.message.reply_text("No users on the leaderboard yet.")
        return
    context.user_data['leaderboard_list'] = all_users
    items = [
        f"{i+1}. @{html.escape(u.get('username') or 'Unknown')} — {u['points']} pts"
        for i, u in enumerate(all_users)
    ]
    keyboard = build_menu(items, 0, 'lead')
    await update.message.reply_text(
        "<b>🏅 Leaderboard 🏅</b>\n\n" + "\n".join(items[0:ITEMS_PER_PAGE]),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def leaderboard_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split(":", 2)
    if len(data) == 3 and data[2] == 'nav':
        page = int(data[1])
        all_users = context.user_data.get('leaderboard_list', [])
        if not all_users:
            await query.edit_message_text("Error: Leaderboard data not found. Please run /leaderboard again.")
            return
        items = [
            f"{i+1}. @{html.escape(u.get('username') or 'Unknown')} — {u['points']} pts"
            for i, u in enumerate(all_users)
        ]
        start = page * ITEMS_PER_PAGE
        end = start + ITEMS_PER_PAGE
        page_items = items[start:end]
        keyboard = build_menu(items, page, 'lead')
        for attempt in range(3):
            try:
                await query.edit_message_text(
                    "<b>🏅 Leaderboard 🏅</b>\n\n" + "\n".join(page_items),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return
            except TimedOut:
                await asyncio.sleep(2)
            except BadRequest as e:
                if "MESSAGE_ID_INVALID" in str(e):
                    await query.message.reply_text("Leaderboard expired, please run /leaderboard again.")
                    return
        await query.edit_message_text("Error: Could not update leaderboard. Please try again.")

# Registered users with pagination
async def viewusers_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.username):
        await update.message.reply_text("❗ Unauthorized.")
        return
    all_users = list(users.find())
    context.user_data['users_list'] = all_users
    items = [f"{u['_id']}: {u.get('username') or 'No username'}" for u in all_users]
    keyboard = build_menu(items, 0, 'users')
    await update.message.reply_text("👥 Registered Users:", reply_markup=InlineKeyboardMarkup(keyboard))

async def viewusers_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, page_str, kind = query.data.split(":", 2)
    if kind != 'nav':
        return
    page = int(page_str)
    items = [f"{u['_id']}: {u.get('username') or 'No username'}" for u in context.user_data.get('users_list', [])]
    keyboard = build_menu(items, page, 'users')
    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))

# Admin submission log
async def viewsubmissions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.username):
        await update.message.reply_text("❗ Unauthorized.")
        return
    all_submissions = list(submissions.find().sort("timestamp", -1))
    context.user_data['submissions_list'] = all_submissions
    if not all_submissions:
        await update.message.reply_text("No submissions yet.")
        return
    text, keyboard = build_submissions_message(all_submissions, 0)
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def submissions_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, page_str, kind = query.data.split(":", 2)
    if kind != 'nav':
        return
    page = int(page_str)
    text, keyboard = build_submissions_message(context.user_data.get('submissions_list', []), page)
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

# Admin commands (addnewadmins, addflag, delete)
async def addnewadmins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.username):
        await update.message.reply_text("❗ Unauthorized.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /addnewadmins <username>")
        return
    new_admin = context.args[0].lstrip("@")
    admins.update_one({"username": new_admin}, {"$set": {"username": new_admin}}, upsert=True)
    await update.message.reply_text(f"✅ @{new_admin} is now an admin.")

async def addflag_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.username):
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
    name = context.user_data["af_name"]
    pts = context.user_data["af_points"]
    link = context.user_data["af_link"]
    flag_str = update.message.text.strip()
    flags.update_one(
        {"_id": name},
        {"$set": {"flag": flag_str, "points": pts, "post_link": link}},
        upsert=True,
    )
    await update.message.reply_text(f"✅ Challenge '{name}' added/updated with {pts} points.")
    return ConversationHandler.END

async def delete_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.username):
        await update.message.reply_text("❗ Unauthorized.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /delete <challenge>")
        return
    name = " ".join(context.args).strip()
    doc = flags.find_one({"_id": name})
    if not doc:
        await update.message.reply_text(f"❗ Challenge '{name}' does not exist.")
        return
    pts = doc.get("points", 0)
    for s in submissions.find({"challenge": name, "correct": True}):
        users.update_one({"_id": s["user_id"]}, {"$inc": {"points": -pts}})
    submissions.delete_many({"challenge": name})
    flags.delete_one({"_id": name})
    await update.message.reply_text(f"✅ Challenge '{name}' and all related data deleted.")

# ----- /bloods command -----
async def bloods_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pipeline = [
        {"$lookup": {
            "from": "submissions",
            "let": {"chal": "$_id"},
            "pipeline": [
                {"$match": {"$expr": {
                    "$and": [
                        {"$eq": ["$challenge", "$$chal"]},
                        {"$eq": ["$correct", True]}
                    ]
                }}},
                {"$group": {"_id": None, "solvers": {"$addToSet": "$user_id"}}},
                {"$project": {"_id": 0, "count": {"$size": "$solvers"}}}
            ],
            "as": "info"
        }},
        {"$addFields": {"solver_count": {"$ifNull": [{"$arrayElemAt": ["$info.count", 0]}, 0]}}},
        {"$project": {"_id": 1, "solver_count": 1}}
    ]
    all_chals = list(flags.aggregate(pipeline))
    all_chals.sort(key=lambda x: x["_id"].lower())
    context.user_data["bloods_list"] = all_chals
    await _bloods_show_page(update, context, 0)

async def _bloods_show_page(update, context, page: int):
    all_chals = context.user_data.get("bloods_list", [])
    if not all_chals:
        return await update.message.reply_text("No challenges available.")
    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    kb = []
    for c in all_chals[start:end]:
        kb.append([
            InlineKeyboardButton(
                f"{c['_id']} ({c['solver_count']} solvers)",
                callback_data=f"bloods_show:{c['_id']}"
            )
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"bloods_page:{page-1}"))
    if end < len(all_chals):
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"bloods_page:{page+1}"))
    if nav:
        kb.append(nav)
    await update.message.reply_text("📋 All Challenges:", reply_markup=InlineKeyboardMarkup(kb))

async def bloods_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    page = int(q.data.split(":", 1)[1])
    await _bloods_show_page(update, context, page)

async def bloods_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chal = q.data.split(":", 1)[1]
    subs = list(submissions.find({"challenge": chal, "correct": True}).sort("timestamp", 1))
    if not subs:
        return await q.edit_message_text(f"No solvers yet for {chal}.")
    seen, solvers = set(), []
    for s in subs:
        udoc = users.find_one({"_id": s["user_id"]}) or {}
        name = udoc.get("username") or udoc.get("first_name") or "Unknown"
        if name in seen:
            continue
        seen.add(name)
        solvers.append(name)
    lines = [f"Solvers for {chal}"]
    if solvers:
        lines.append(f"@{solvers[0]} — firstblood")
        for other in solvers[1:]:
            lines.append(f"@{other}")
    await q.edit_message_text("\n".join(lines))

# Startup: retry setting commands
def init_commands(app):
    async def on_startup(application):
        commands = [
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
        for attempt in range(3):
            try:
                await application.bot.set_my_commands(commands)
                return
            except TimedOut:
                logger.warning(f"set_my_commands timed out, retry {attempt+1}/3")
                await asyncio.sleep(2)
        logger.error("Failed to set bot commands after 3 attempts")
    return on_startup

def main():
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(init_commands(None))
        .build()
    )

    # Register handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("myviewpoints", my_viewpoints))
    app.add_handler(CommandHandler("viewchallenges", view_challenges))
    app.add_handler(CommandHandler("submit", submit_start))
    app.add_handler(CallbackQueryHandler(select_challenge, pattern=r"^submit:.+"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_flag))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("leaderboard", leaderboard_start))
    app.add_handler(CallbackQueryHandler(leaderboard_page, pattern=r"^lead:\d+:nav$"))
    app.add_handler(CommandHandler("viewusers", viewusers_start))
    app.add_handler(CallbackQueryHandler(viewusers_page, pattern=r"^users:\d+:(nav|.+)"))
    app.add_handler(CommandHandler("viewsubmissions", viewsubmissions))
    app.add_handler(CallbackQueryHandler(submissions_page, pattern=r"^submissions:\d+:nav$"))
    app.add_handler(CommandHandler("bloods", bloods_start))
    app.add_handler(CallbackQueryHandler(bloods_page,   pattern=r"^bloods_page:\d+$"))
    app.add_handler(CallbackQueryHandler(bloods_show,   pattern=r"^bloods_show:.+"))
    app.add_handler(CallbackQueryHandler(details_challenge, pattern=r"^detail:.+"))
    app.add_handler(CommandHandler("addnewadmins", addnewadmins))
    app.add_handler(CommandHandler("delete", delete_challenge))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("addflag", addflag_start)],
        states={
            AF_NAME:   [MessageHandler(filters.TEXT & ~filters.COMMAND, af_name)],
            AF_POINTS:[MessageHandler(filters.TEXT & ~filters.COMMAND, af_points)],
            AF_LINK:  [MessageHandler(filters.TEXT & ~filters.COMMAND, af_link)],
            AF_FLAG:  [MessageHandler(filters.TEXT & ~filters.COMMAND, af_flag)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
    ))

    # Error handler
    async def error_handler(update, context):
        logger.error("❌ Exception in handler:", exc_info=context.error)

    app.add_error_handler(error_handler)

    # Start webhook or polling
    if WEBHOOK_URL:
        app.run_webhook(
            listen="0.0.0.0",
            port=int(os.environ.get("PORT", 5000)),
            url_path="webhook",
            webhook_url=WEBHOOK_URL,
            drop_pending_updates=True,
        )
    else:
        app.run_polling()

if __name__ == "__main__":
    main()
