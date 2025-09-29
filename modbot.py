# modbot.py
"""
Telegram moderation bot (python-telegram-bot v20.6)

Features:
- /ban /kick /mute /unmute /pin /warn /rules
- /trust /untrust (whitelist users who bypass moderation)
- Welcome new members
- Anti-flood + link & bad-word filters
- Dealer-ad detection (regex scoring):
    * 1st offense (score >= 3): delete + restrict for 3 days + warn message
    * 2nd offense AFTER restriction period: kick (ban+unban)
- Admin logs to ADMIN_LOG_CHAT_ID

ENV required before running:
  export TOKEN="123456:ABCdef..."            # from @BotFather
  export ADMIN_LOG_CHAT_ID=123456789         # your user id or a private group id (numeric)
"""

import os
import re
import html
import logging
from collections import defaultdict, deque
from datetime import datetime, timedelta

from telegram import Update, ChatPermissions
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
)

# ---------- CONFIG ----------
TOKEN = os.environ.get("TOKEN")
ADMIN_LOG_CHAT_ID = os.environ.get("ADMIN_LOG_CHAT_ID")  # numeric id as str or None

FLOOD_LIMIT = 5          # messages in window
FLOOD_WINDOW = 8         # seconds
WARN_BEFORE_MUTE = 2     # /warn threshold (separate from dealer logic)
MUTE_DURATION_SECONDS = 60 * 10
LINK_FILTER = True
BANNED_WORDS = {"nastyword1", "nastyword2"}  # customise
ADMIN_BYPASS = True  # if True, admins are not moderated by filters

# Whitelist: users completely skipped by moderation
TRUSTED_USER_IDS = set()  # e.g. {2133156282}

# ---------- LOGGING ----------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("modbot")

# ---------- STATE ----------
message_times = defaultdict(lambda: deque())     # user_id -> deque[timestamps] (anti-flood)
warnings = defaultdict(int)                      # user_id -> warn count (manual /warn)
user_offenses = defaultdict(int)                 # user_id -> number of dealer offenses detected
restriction_until = dict()                       # user_id -> datetime of restriction end (for dealer flow)

# ---------- REGEX (dealer ads) ----------
PHONE_RE = re.compile(r"(?:(?:\+?\d{1,3}[\s\-\.]?)?(?:\(?\d{2,4}\)?[\s\-\.]?)?\d{3,4}[\s\-\.]?\d{3,4})")
WHATSAPP_LINK_RE = re.compile(r"(?:https?://)?(?:chat\.whatsapp\.com|wa\.me|whatsapp\.)[^\s]+", re.IGNORECASE)
TELEGRAM_INVITE_RE = re.compile(r"(?:https?://)?t\.me\/joinchat\/[A-Za-z0-9_-]+", re.IGNORECASE)
PRICE_WEIGHT_RE = re.compile(r"(\£|\$|€)\s?\d{1,4}(?:\.\d{1,2})?\s*(?:\/|per)?\s*(g|gram|gramme|kg|kilo|oz|ozs)?", re.IGNORECASE)
SELL_KEYWORDS = re.compile(
    r"\b(sell(?:ing|s)?|vendor|supply|bulk|kilo|kg|g for|grams|plug|connect|dm for price|price dm|pm price|"
    r"we have stock|got (?:weed|mdma|cocaine|coke|pills|xanax|ketamine|m-cat))\b",
    re.IGNORECASE
)
PAYMENT_RE = re.compile(r"\b(paypal|venmo|cashapp|bank transfer|btc|bitcoin|zelle|revolut)\b", re.IGNORECASE)

def ad_score(text: str) -> int:
    """Score likelihood of a dealer advert."""
    s = 0
    t = (text or "").lower()
    if PHONE_RE.search(t): s += 3
    if WHATSAPP_LINK_RE.search(t): s += 4
    if TELEGRAM_INVITE_RE.search(t): s += 3
    if PRICE_WEIGHT_RE.search(t): s += 3
    if SELL_KEYWORDS.search(t): s += 3
    if PAYMENT_RE.search(t): s += 2
    if re.fullmatch(r"\s*" + PHONE_RE.pattern + r"\s*", t): s += 2
    return s

# ---------- HELPERS ----------
async def is_admin(update: Update, user_id: int) -> bool:
    try:
        member = await update.effective_chat.get_member(user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False

def record_message(user_id: int) -> int:
    now = datetime.utcnow()
    dq = message_times[user_id]
    dq.append(now)
    while dq and (now - dq[0]).total_seconds() > FLOOD_WINDOW:
        dq.popleft()
    return len(dq)

async def mute_user(chat, user_id: int, secs: int, ctx: ContextTypes.DEFAULT_TYPE):
    until = datetime.utcnow() + timedelta(seconds=secs)
    perms = ChatPermissions(can_send_messages=False)
    try:
        await ctx.bot.restrict_chat_member(chat.id, user_id, permissions=perms, until_date=until)
    except Exception as e:
        log.warning("mute_user failed: %s", e)

async def restrict_until(chat, user_id: int, until_dt: datetime, ctx: ContextTypes.DEFAULT_TYPE):
    """Restrict (mute) user until a specific datetime."""
    perms = ChatPermissions(can_send_messages=False)
    try:
        await ctx.bot.restrict_chat_member(chat.id, user_id, permissions=perms, until_date=until_dt)
    except Exception as e:
        log.warning("restrict_until failed: %s", e)

async def send_admin_log(ctx: ContextTypes.DEFAULT_TYPE, text: str):
    if not ADMIN_LOG_CHAT_ID:
        log.info("[ADMIN LOG]\n%s", text)
        return
    try:
        await ctx.bot.send_message(int(ADMIN_LOG_CHAT_ID), text, parse_mode="HTML")
    except Exception as e:
        log.warning("Failed to send admin log: %s\nLog:\n%s", e, text)

# ---------- COMMANDS ----------
async def start_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("I'm the mod bot. Admins: /help")

async def help_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "/ban (reply)  | /kick (reply)\n"
        "/mute (reply) [minutes] | /unmute (reply)\n"
        "/pin (reply)  | /warn (reply)\n"
        "/rules | /trust (reply/id) | /untrust (reply/id)"
    )

async def ban_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not u.message.reply_to_message:
        return await u.message.reply_text("Reply to the user's message to ban.")
    if not await is_admin(u, u.effective_user.id):
        return await u.message.reply_text("Admin only.")
    target = u.message.reply_to_message.from_user
    try:
        await u.effective_chat.ban_member(target.id)
    except Exception as e:
        log.warning("ban failed: %s", e)
    await u.message.reply_text(f"Banned {target.full_name}.")

async def kick_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not u.message.reply_to_message:
        return await u.message.reply_text("Reply to the user's message to kick.")
    if not await is_admin(u, u.effective_user.id):
        return await u.message.reply_text("Admin only.")
    target = u.message.reply_to_message.from_user
    try:
        await u.effective_chat.kick_member(target.id)
        await u.effective_chat.unban_member(target.id)
    except Exception as e:
        log.warning("kick failed: %s", e)
    await u.message.reply_text(f"Kicked {target.full_name}.")

async def mute_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not u.message.reply_to_message:
        return await u.message.reply_text("Reply to the user's message to mute.")
    if not await is_admin(u, u.effective_user.id):
        return await u.message.reply_text("Admin only.")
    minutes = 10
    if c.args:
        try: minutes = int(c.args[0])
        except: pass
    target = u.message.reply_to_message.from_user
    await mute_user(u.effective_chat, target.id, minutes*60, c)
    await u.message.reply_text(f"Muted {target.full_name} for {minutes} minutes.")

async def unmute_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not u.message.reply_to_message:
        return await u.message.reply_text("Reply to the user's message to unmute.")
    if not await is_admin(u, u.effective_user.id):
        return await u.message.reply_text("Admin only.")
    target = u.message.reply_to_message.from_user
    perms = ChatPermissions(
        can_send_messages=True, can_send_media_messages=True, can_send_polls=True,
        can_send_other_messages=True, can_add_web_page_previews=True
    )
    try:
        await c.bot.restrict_chat_member(u.effective_chat.id, target.id, permissions=perms)
    except Exception as e:
        log.warning("unmute failed: %s", e)
    await u.message.reply_text(f"Unmuted {target.full_name}.")

async def pin_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not u.message.reply_to_message:
        return await u.message.reply_text("Reply to the message to pin.")
    if not await is_admin(u, u.effective_user.id):
        return await u.message.reply_text("Admin only.")
    try:
        await u.effective_chat.pin_message(u.message.reply_to_message.message_id)
    except Exception as e:
        log.warning("pin failed: %s", e)
    await u.message.reply_text("Pinned.")

async def warn_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not u.message.reply_to_message:
        return await u.message.reply_text("Reply to the user's message to warn.")
    target = u.message.reply_to_message.from_user
    warnings[target.id] += 1
    w = warnings[target.id]
    await u.message.reply_text(f"{target.full_name} warned ({w}).")
    if w >= WARN_BEFORE_MUTE:
        await mute_user(u.effective_chat, target.id, MUTE_DURATION_SECONDS, c)
        await u.message.reply_text(f"{target.full_name} auto-muted for repeat warnings.")
        warnings[target.id] = 0

async def rules_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "Group rules:\n"
        "1) 18+ only\n2) Consent always\n3) No dealing / illegal activity\n"
        "4) No doxxing\n5) Move explicit media to DMs if asked"
    )

async def trust_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(u, u.effective_user.id):
        return await u.message.reply_text("Admin only.")
    if u.message.reply_to_message:
        uid = u.message.reply_to_message.from_user.id
    elif c.args:
        try: uid = int(c.args[0])
        except: return await u.message.reply_text("Reply to a user or provide their numeric id.")
    else:
        return await u.message.reply_text("Reply to a user or provide their numeric id.")
    TRUSTED_USER_IDS.add(uid)
    await u.message.reply_text(f"User {uid} added to trusted list.")

async def untrust_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(u, u.effective_user.id):
        return await u.message.reply_text("Admin only.")
    if u.message.reply_to_message:
        uid = u.message.reply_to_message.from_user.id
    elif c.args:
        try: uid = int(c.args[0])
        except: return await u.message.reply_text("Reply to a user or provide their numeric id.")
    else:
        return await u.message.reply_text("Reply to a user or provide their numeric id.")
    TRUSTED_USER_IDS.discard(uid)
    await u.message.reply_text(f"User {uid} removed from trusted list.")

# ---------- HANDLERS ----------
async def welcome_new(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not u.message or not u.message.new_chat_members: return
    for m in u.message.new_chat_members:
        try:
            await u.message.reply_text(
                f"Welcome, {m.full_name}! 18+ space. Read /rules. Consent > everything."
            )
        except Exception:
            pass

async def filter_message(u: Update, c: ContextTypes.DEFAULT_TYPE):
    msg = u.message
    if not msg: return
    if msg.from_user and msg.from_user.is_bot: return

    # Trusted bypass
    if msg.from_user and msg.from_user.id in TRUSTED_USER_IDS:
        return

    # Admins bypass (configurable)
    try:
        if ADMIN_BYPASS and await is_admin(u, msg.from_user.id):
            return
    except Exception:
        pass

async def chatid_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    chat = u.effective_chat
    await u.message.reply_text(f"Chat ID: {chat.id}")

   
    # Collect text + caption + URLs
    pieces = []
    if msg.text: pieces.append(msg.text)
    if msg.caption: pieces.append(msg.caption)
    try:
        entities = (msg.entities or []) + (msg.caption_entities or [])
        base = msg.text or msg.caption or ""
        for ent in entities:
            if ent.type in ("url", "text_link"):
                start = ent.offset; end = ent.offset + ent.length
                pieces.append(base[start:end])
    except Exception:
        pass

    full_text = " ".join(pieces).strip()
    down = full_text.lower()

    # ===== Dealer ad detection with "warn+restrict then kick" policy =====
    score = ad_score(full_text)

    if score >= 3:
        user_id = msg.from_user.id
        # delete the message always
        try: await msg.delete()
        except Exception: pass

        now = datetime.utcnow()
        user_offenses[user_id] += 1

        # First offense -> restrict 3 days + warn
        if user_offenses[user_id] == 1:
            until_dt = now + timedelta(days=3)
            restriction_until[user_id] = until_dt
            await restrict_until(u.effective_chat, user_id, until_dt, c)

            warn_text_group = (
                f"{msg.from_user.first_name}, your message was deleted because you've broken the group rules. "
                f"As a reminder, you cannot post drug-dealing adverts in this group. "
                f"As a result, your ability to send messages has been restricted for 3 days. "
                f"If you do this again after your restriction ends, you will be kicked out of the group."
            )
            try: await u.effective_chat.send_message(warn_text_group)
            except Exception: pass

            # admin log
            log_text = (
                f"<b>Dealer Ad — FIRST OFFENSE</b>\n"
                f"<b>User:</b> {html.escape(msg.from_user.full_name)} (id: <code>{user_id}</code>)\n"
                f"<b>Chat:</b> {html.escape(u.effective_chat.title or str(u.effective_chat.id))} "
                f"(id: <code>{u.effective_chat.id}</code>)\n"
                f"<b>Score:</b> {score}\n"
                f"<b>Restricted until:</b> {until_dt.isoformat()} UTC\n\n"
                f"<b>Content:</b>\n{html.escape(full_text)[:2000]}"
            )
            await send_admin_log(c, log_text)
            return

        # Second (or later) offense -> if restriction period has ended, kick
        else:
            until_dt = restriction_until.get(user_id)
            if until_dt and now >= until_dt:
                # kick (ban+unban so they can rejoin later if invited)
                try:
                    await u.effective_chat.ban_member(user_id)
                    await u.effective_chat.unban_member(user_id)
                except Exception as e:
                    log.warning("kick failed: %s", e)

                try:
                    await u.effective_chat.send_message(
                        f"{msg.from_user.first_name} has been kicked for repeated rule violations."
                    )
                except Exception:
                    pass

                log_text = (
                    f"<b>Dealer Ad — KICKED</b>\n"
                    f"<b>User:</b> {html.escape(msg.from_user.full_name)} (id: <code>{user_id}</code>)\n"
                    f"<b>Chat:</b> {html.escape(u.effective_chat.title or str(u.effective_chat.id))} "
                    f"(id: <code>{u.effective_chat.id}</code>)\n"
                    f"<b>Score:</b> {score}\n"
                    f"<b>Restriction expired:</b> {until_dt.isoformat()} UTC\n\n"
                    f"<b>Content:</b>\n{html.escape(full_text)[:2000]}"
                )
                await send_admin_log(c, log_text)

                # reset offense tracking after kick
                user_offenses.pop(user_id, None)
                restriction_until.pop(user_id, None)
                return
            else:
                # still in restriction period
                try:
                    await u.effective_chat.send_message(
                        f"{msg.from_user.first_name}, you're currently restricted. "
                        f"Posting dealer adverts will lead to a kick when your restriction ends."
                    )
                except Exception:
                    pass

                log_text = (
                    f"<b>Dealer Ad — MESSAGE DURING RESTRICTION</b>\n"
                    f"<b>User:</b> {html.escape(msg.from_user.full_name)} (id: <code>{user_id}</code>)\n"
                    f"<b>Chat:</b> {html.escape(u.effective_chat.title or str(u.effective_chat.id))} "
                    f"(id: <code>{u.effective_chat.id}</code>)\n"
                    f"<b>Score:</b> {score}\n"
                    f"<b>Restricted until:</b> {until_dt.isoformat() if until_dt else 'unknown'} UTC\n\n"
                    f"<b>Content:</b>\n{html.escape(full_text)[:2000]}"
                )
                await send_admin_log(c, log_text)
                return

    # ===== Other moderation (swear/bad words, links, flood) =====
    if any(b in down for b in BANNED_WORDS):
        try: await msg.delete()
        except Exception: pass
        try: await u.effective_chat.send_message(f"{msg.from_user.first_name}, that language is not allowed.")
        except Exception: pass
        return

    if LINK_FILTER and ("http://" in down or "https://" in down or "t.me/" in down):
        if not await is_admin(u, msg.from_user.id):
            try: await msg.delete()
            except Exception: pass
            try: await u.effective_chat.send_message(f"{msg.from_user.first_name}, links are not allowed.")
            except Exception: pass
            return

    cnt = record_message(msg.from_user.id)
    if cnt >= FLOOD_LIMIT:
        try:
            await u.effective_chat.send_message(
                f"{msg.from_user.first_name} — you're posting too much. Muted."
            )
            await mute_user(u.effective_chat, msg.from_user.id, MUTE_DURATION_SECONDS, c)
            message_times[msg.from_user.id].clear()
        except Exception:
            pass

# ---------- MAIN ----------
def main():
    if not TOKEN:
        raise RuntimeError("TOKEN env var not set.")
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
    app.add_handler(CommandHandler("trust", trust_cmd))
    app.add_handler(CommandHandler("untrust", untrust_cmd))
    app.add_handler(CommandHandler("chatid", chatid_cmd))


    # events & messages
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), filter_message))

    # captions on media (photos, videos, audio, voices, documents)
    media_filters = (
        filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE | filters.Document.ALL
    )
    app.add_handler(MessageHandler(media_filters, filter_message))

    print("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
