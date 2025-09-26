# modbot.py
"""
Moderation bot for Telegram groups.
Features:
- Admin commands: /ban, /kick, /mute, /unmute, /pin, /warn, /rules
- Welcome new members
- Anti-flood
- Link & badword filter
- Dealer-ad detection (regex score -> delete + log -> possible ban)
- Sends a log message to ADMIN_LOG_CHAT_ID (if set)
Notes:
- This uses python-telegram-bot v20+ (async)
- In-memory storage (warnings/history) is NOT persistent across restarts. Use a DB if you need persistence.
"""

import os
import re
import html
import logging
from collections import defaultdict, deque
from datetime import datetime, timedelta

from telegram import Update, ChatPermissions
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes
)

# -------------- CONFIG --------------
TOKEN = os.environ.get("TOKEN")  # set this in Replit secrets or env
ADMIN_LOG_CHAT_ID = os.environ.get("ADMIN_LOG_CHAT_ID")  # set to your private group/user id (string) or leave empty

# Behavior tuning
FLOOD_LIMIT = 5             # messages
FLOOD_WINDOW = 8            # seconds
WARN_BEFORE_MUTE = 2        # warns before auto-mute
MUTE_DURATION_SECONDS = 60 * 10  # default 10 minutes

BANNED_WORDS = {"nastyword1", "nastyword2"}  # customise
LINK_FILTER = True

# -------------- logging --------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# -------------- in-memory stores --------------
message_times = defaultdict(lambda: deque())  # user_id -> deque of datetimes
warnings = defaultdict(int)

# -------------- regexes for ad detection --------------
PHONE_RE = re.compile(
    r"(?:(?:\+?\d{1,3}[\s\-\.]?)?(?:\(?\d{2,4}\)?[\s\-\.]?)?\d{3,4}[\s\-\.]?\d{3,4})"
)
WHATSAPP_LINK_RE = re.compile(r"(?:https?://)?(?:chat\.whatsapp\.com|wa\.me|whatsapp\.)[^\s]+", re.IGNORECASE)
TELEGRAM_INVITE_RE = re.compile(r"(?:https?://)?t\.me\/joinchat\/[A-Za-z0-9_-]+", re.IGNORECASE)
PRICE_WEIGHT_RE = re.compile(r"(\£|\$|€)\s?\d{1,4}(\.\d{1,2})?\s*(?:\/|per)?\s*(g|gram|gramme|kg|kilo|oz|ozs)?", re.IGNORECASE)
SELL_KEYWORDS = re.compile(
    r"\b(sell(?:ing|s)?|vendor|supply|sells|bulk|kilo|kg|g for|grams|plug|connect|dm for price|price dm|pm price|we have stock|got (?:weed|mdma|cocaine|coke|pills|xanax|ketamine|m-cat))\b",
    re.IGNORECASE
)
PAYMENT_RE = re.compile(r"\b(paypal|venmo|cashapp|bank transfer|btc|bitcoin|zelle|revolut)\b", re.IGNORECASE)

def ad_score(text: str) -> int:
    """Simple scoring — higher = more likely dealer ad"""
    s = 0
    t = text.lower()
    if PHONE_RE.search(t): s += 3
    if WHATSAPP_LINK_RE.search(t): s += 4
    if TELEGRAM_INVITE_RE.search(t): s += 3
    if PRICE_WEIGHT_RE.search(t): s += 3
    if SELL_KEYWORDS.search(t): s += 3
    if PAYMENT_RE.search(t): s += 2
    # single-line phone numbers are very suspicious
    if re.fullmatch(r"\s*" + PHONE_RE.pattern + r"\s*", t):
        s += 2
    return s

# -------------- helpers --------------
async def is_admin(update: Update, user_id: int) -> bool:
    try:
        chat = update.effective_chat
        member = await chat.get_member(user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False

def record_message(user_id:int):
    now = datetime.utcnow()
    dq = message_times[user_id]
    dq.append(now)
    while dq and (now - dq[0]).total_seconds() > FLOOD_WINDOW:
        dq.popleft()
    return len(dq)

async def mute_user(chat, user_id:int, until_seconds:int, app: ContextTypes.DEFAULT_TYPE):
    until = datetime.utcnow() + timedelta(seconds=until_seconds)
    perms = ChatPermissions(can_send_messages=False)
    try:
        await app.bot.restrict_chat_member(chat.id, user_id, permissions=perms, until_date=until)
    except Exception as e:
        log.warning("mute_user failed: %s", e)

async def send_admin_log(context: ContextTypes.DEFAULT_TYPE, text: str, parse_mode="HTML"):
    if not ADMIN_LOG_CHAT_ID:
        log.info("ADMIN_LOG_CHAT_ID not set; admin logs will appear in console.")
        log.info(text)
        return
    try:
        await context.bot.send_message(int(ADMIN_LOG_CHAT_ID), text, parse_mode=parse_mode)
    except Exception as e:
        log.warning("Failed to send admin log: %s", e)
        log.info("Log content:\n%s", text)

# -------------- command handlers --------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("I'm the mod bot. Admins: use /help to see commands.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "/ban (reply) - ban user\n"
        "/kick (reply) - kick user\n"
        "/mute (reply) [minutes] - mute user\n"
        "/unmute (reply) - unmute user\n"
        "/pin (reply) - pin message\n"
        "/warn (reply) - warn user\n"
        "/rules - show rules\n"
    )
    await update.message.reply_text(help_text)

async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        return await update.message.reply_text("Reply to the user's message you want to ban.")
    if not await is_admin(update, update.effective_user.id):
        return await update.message.reply_text("You must be an admin to use this.")
    target = update.message.reply_to_message.from_user
    try:
        await update.effective_chat.ban_member(target.id)
    except Exception as e:
        log.warning("ban failed: %s", e)
    await update.message.reply_text(f"Banned {target.full_name}.")

async def kick_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        return await update.message.reply_text("Reply to the user's message you want to kick.")
    if not await is_admin(update, update.effective_user.id):
        return await update.message.reply_text("You must be an admin to use this.")
    target = update.message.reply_to_message.from_user
    try:
        await update.effective_chat.kick_member(target.id)
        await update.effective_chat.unban_member(target.id)  # so they can rejoin later
    except Exception as e:
        log.warning("kick failed: %s", e)
    await update.message.reply_text(f"Kicked {target.full_name}.")

async def mute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        return await update.message.reply_text("Reply to the user's message you want to mute.")
    if not await is_admin(update, update.effective_user.id):
        return await update.message.reply_text("You must be an admin to use this.")
    minutes = 10
    if context.args:
        try:
            minutes = int(context.args[0])
        except:
            pass
    target = update.message.reply_to_message.from_user
    await mute_user(update.effective_chat, target.id, minutes*60, context)
    await update.message.reply_text(f"Muted {target.full_name} for {minutes} minutes.")

async def unmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        return await update.message.reply_text("Reply to the user's message you want to unmute.")
    if not await is_admin(update, update.effective_user.id):
        return await update.message.reply_text("You must be an admin to use this.")
    target = update.message.reply_to_message.from_user
    perms = ChatPermissions(
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True
    )
    try:
        await context.bot.restrict_chat_member(update.effective_chat.id, target.id, permissions=perms)
    except Exception as e:
        log.warning("unmute failed: %s", e)
    await update.message.reply_text(f"Unmuted {target.full_name}.")

async def pin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        return await update.message.reply_text("Reply to the message you want to pin.")
    if not await is_admin(update, update.effective_user.id):
        return await update.message.reply_text("You must be an admin to use this.")
    try:
        await update.effective_chat.pin_message(update.message.reply_to_message.message_id)
    except Exception as e:
        log.warning("pin failed: %s", e)
    await update.message.reply_text("Pinned message.")

async def warn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        return await update.message.reply_text("Reply to the user's message you want to warn.")
    target = update.message.reply_to_message.from_user
    warnings[target.id] += 1
    w = warnings[target.id]
    await update.message.reply_text(f"{target.full_name} warned ({w}).")
    if w >= WARN_BEFORE_MUTE:
        await mute_user(update.effective_chat, target.id, MUTE_DURATION_SECONDS, context)
        await update.message.reply_text(f"{target.full_name} has been auto-muted for repeat warnings.")
        warnings[target.id] = 0

async def rules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Group rules:\n"
        "1) 18+ only\n"
        "2) Consent always\n"
        "3) No illegal activity / no dealing\n"
        "4) No doxxing\n"
        "5) Keep explicit media to DMs if requested\n"
    )
    await update.message.reply_text(text)

# -------------- message handlers --------------
async def welcome_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.new_chat_members:
        return
    for member in update.message.new_chat_members:
        try:
            await update.message.reply_text(f"Welcome, {member.full_name}! Read the rules with /rules. This is an 18+ space — be kind & consensual.")
        except Exception:
            pass

async def filter_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    if msg.from_user and msg.from_user.is_bot:
        return

    # Admins bypass filters
    try:
        if await is_admin(update, msg.from_user.id):
            return
    except Exception:
        pass

    # aggregate text
    text_parts = []
    if msg.text:
        text_parts.append(msg.text)
    if msg.caption:
        text_parts.append(msg.caption)
    # include url entities if present (best-effort)
    try:
        entities = (msg.entities or []) + (msg.caption_entities or [])
        base = msg.text or msg.caption or ""
        for ent in entities:
            if ent.type in ("url", "text_link"):
                start = ent.offset
                end = ent.offset + ent.length
                text_parts.append(base[start:end])
    except Exception:
        pass

    full_text = " ".join(text_parts).strip()
    down = full_text.lower()

    # AD DETECTION
    score = ad_score(full_text)
    # thresholds: >=5 = strong -> delete + ban; 3-4 = delete + warn/log; <3 = ignore
    if score >= 5:
        try:
            await msg.delete()
        except Exception:
            pass
        try:
            await update.effective_chat.ban_member(msg.from_user.id)
            action = "banned"
        except Exception:
            action = "deleted (ban failed)"
        log_text = (
            f"<b>Auto-Moderation — Dealer Ad</b>\n"
            f"<b>User:</b> {html.escape(msg.from_user.full_name)} (id: <code>{msg.from_user.id}</code>)\n"
            f"<b>Chat:</b> {html.escape(update.effective_chat.title or str(update.effective_chat.id))} (id: <code>{update.effective_chat.id}</code>)\n"
            f"<b>Action:</b> {action}\n"
            f"<b>Score:</b> {score}\n"
            f"<b>Time:</b> {datetime.utcnow().isoformat()} UTC\n\n"
            f"<b>Content:</b>\n{html.escape(full_text)[:2000]}"
        )
        await send_admin_log(context, log_text)
        try:
            await update.effective_chat.send_message("This post was removed because it looked like an illicit advert. Mods have been notified.")
        except Exception:
            pass
        return

    if 3 <= score < 5:
        try:
            await msg.delete()
        except Exception:
            pass
        warnings[msg.from_user.id] += 1
        try:
            await update.effective_chat.send_message(f"{msg.from_user.first_name}, your message was removed for suspected illicit content. Repeated posts may lead to a ban.")
        except Exception:
            pass
        log_text = (
            f"<b>Possible Dealer Ad</b>\n"
            f"User: {html.escape(msg.from_user.full_name)} (id: <code>{msg.from_user.id}</code>)\n"
            f"Chat: {html.escape(update.effective_chat.title or str(update.effective_chat.id))}\n"
            f"Score: {score}\n"
            f"Time: {datetime.utcnow().isoformat()} UTC\n\n"
            f"Content:\n{html.escape(full_text)[:2000]}"
        )
        await send_admin_log(context, log_text)
        return

    # banned words filter
    if any(b in down for b in BANNED_WORDS):
        try:
            await msg.delete()
        except Exception:
            pass
        await update.effective_chat.send_message(f"{msg.from_user.first_name}, that language is not allowed.")
        return

    # link filter
    if LINK_FILTER and ("http://" in down or "https://" in down or "t.me/" in down):
        if not await is_admin(update, msg.from_user.id):
            try:
                await msg.delete()
            except Exception:
                pass
            await update.effective_chat.send_message(f"{msg.from_user.first_name}, links are not allowed.")
            return

    # anti-flood
    count = record_message(msg.from_user.id)
    if count >= FLOOD_LIMIT:
        try:
            await update.effective_chat.send_message(f"{msg.from_user.first_name} — you're posting too much. Muted for flood.")
            await mute_user(update.effective_chat, msg.from_user.id, MUTE_DURATION_SECONDS, context)
            message_times[msg.from_user.id].clear()
        except Exception:
            pass

# -------------- main --------------
def main():
    if not TOKEN:
        raise RuntimeError("TOKEN env var not set. Get it from @BotFather and set it in your environment.")
    app = ApplicationBuilder().token(TOKEN).build()

    # commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("kick", kick_cmd))
    app.add_handler(CommandHandler("mute", mute_cmd))
    app.add_handler(CommandHandler("unmute", unmute_cmd))
    app.add_handler(CommandHandler("pin", pin_cmd))
    app.add_handler(CommandHandler("warn", warn_cmd))
    app.add_handler(CommandHandler("rules", rules_cmd))

    # status updates and messages
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), filter_message))
    app.add_handler(MessageHandler(filters.MEDIA, filter_message))  # handle captions

    print("Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
