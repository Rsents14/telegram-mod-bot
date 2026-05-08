# modbot.py
"""
Telegram moderation bot + Photo Of The Week (python-telegram-bot v20+)

Fixes:
- Dealer detection no longer treats normal prices like "£50 hotel" as drug dealing.
- Friendlier Tina messages.
- /prompt and /tina commands added.
"""

import os
import re
import html
import json
import logging
import random
from collections import defaultdict, deque
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

from telegram import Update, ChatPermissions
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
)

# ---------- CONFIG ----------
TOKEN = os.environ.get("TOKEN") or os.environ.get("BOT_TOKEN")
ADMIN_LOG_CHAT_ID = os.environ.get("ADMIN_LOG_CHAT_ID")

FLOOD_LIMIT = 5
FLOOD_WINDOW = 8
WARN_BEFORE_MUTE = 2
MUTE_DURATION_SECONDS = 60 * 10
LINK_FILTER = True
BANNED_WORDS = {"nastyword1", "nastyword2"}
ADMIN_BYPASS = True

TRUSTED_USER_IDS = set()

RULES_COOLDOWN_SECONDS = 6 * 60 * 60
WELCOME_BATCH_SECONDS = 6 * 60 * 60

TZ = ZoneInfo("Europe/London")
POTW_HASHTAG_ONLY = os.environ.get("POTW_HASHTAG_ONLY", "").strip() in {"1", "true", "True", "yes", "YES"}
POTW_HASHTAG = "#potw"
POTW_FINALISTS = 5
POTW_SUNDAY_TIME = time(19, 0, tzinfo=TZ)
POTW_MONDAY_TIME = time(19, 0, tzinfo=TZ)
POTW_DATA_FILE = "potw_data.json"

CONVERSATION_PROMPTS = [
    "Right then, question of the day: what’s your craziest meet story? Keep it funny, not criminal 😏",
    "What’s your most embarrassing moment on a night out? Tina wants the gossip.",
    "Tell us about the worst date you’ve ever been on. Bonus points if it sounds like a Netflix documentary.",
    "What’s the funniest message someone has ever sent you on here?",
    "What’s one thing people always get wrong about you?",
    "Be honest: what’s your red flag that you pretend is a personality trait?",
    "What’s the weirdest message you’ve ever received? No names, babes.",
    "If your dating life had a title, what would it be?",
    "What’s your biggest green flag in someone?",
    "Photo of the week check-in 📸 Drop your best pic below.",
]

# ---------- LOGGING ----------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("modbot")

# ---------- STATE ----------
message_times = defaultdict(lambda: deque())
warnings = defaultdict(int)
user_offenses = defaultdict(int)
restriction_until = dict()

last_rules_sent_at = defaultdict(lambda: None)
pending_welcomes = defaultdict(list)
last_welcome_sent_at = defaultdict(lambda: None)

potw_state = {"chats": {}}

# ---------- REGEX ----------
PHONE_RE = re.compile(r"(?:(?:\+?\d{1,3}[\s\-\.]?)?(?:\(?\d{2,4}\)?[\s\-\.]?)?\d{3,4}[\s\-\.]?\d{3,4})")
WHATSAPP_LINK_RE = re.compile(r"(?:https?://)?(?:chat\.whatsapp\.com|wa\.me|whatsapp\.)[^\s]+", re.IGNORECASE)
TELEGRAM_INVITE_RE = re.compile(r"(?:https?://)?t\.me\/joinchat\/[A-Za-z0-9_-]+", re.IGNORECASE)

PRICE_WEIGHT_RE = re.compile(
    r"(\£|\$|€)\s?\d{1,4}(?:\.\d{1,2})?\s*(?:\/|per)?\s*(g|gram|gramme|grams|kg|kilo|oz|ozs)\b",
    re.IGNORECASE
)

DRUG_WORDS_RE = re.compile(
    r"\b(weed|bud|green|coke|cocaine|ket|ketamine|mdma|pills|xanax|m-cat|meow|gear|snow|hash|edibles)\b",
    re.IGNORECASE
)

SELL_KEYWORDS = re.compile(
    r"\b(sell(?:ing|s)?|vendor|supply|bulk|kilo|kg|g for|grams|plug|connect|dm for price|price dm|pm price|"
    r"we have stock|got (?:weed|mdma|cocaine|coke|pills|xanax|ketamine|m-cat))\b",
    re.IGNORECASE
)

PAYMENT_RE = re.compile(r"\b(paypal|venmo|cashapp|bank transfer|btc|bitcoin|zelle|revolut)\b", re.IGNORECASE)

SAFE_CONTEXT_RE = re.compile(
    r"\b(hotel|room|travelodge|premier inn|airbnb|booking|stay|accommodation|taxi|train|ticket|rent|bill|food|shopping|deposit)\b",
    re.IGNORECASE
)


def ad_score(text: str) -> int:
    s = 0
    t = (text or "").lower()

    has_safe_context = bool(SAFE_CONTEXT_RE.search(t))
    has_drug_word = bool(DRUG_WORDS_RE.search(t))
    has_selling_language = bool(SELL_KEYWORDS.search(t))
    has_price_weight = bool(PRICE_WEIGHT_RE.search(t))
    has_payment = bool(PAYMENT_RE.search(t))
    has_phone = bool(PHONE_RE.search(t))
    has_whatsapp = bool(WHATSAPP_LINK_RE.search(t))
    has_telegram_invite = bool(TELEGRAM_INVITE_RE.search(t))

    if has_safe_context and not has_drug_word and not has_selling_language and not has_whatsapp:
        return 0

    if has_drug_word:
        s += 4
    if has_selling_language:
        s += 3
    if has_price_weight:
        s += 2
    if has_whatsapp:
        s += 3
    if has_telegram_invite:
        s += 2
    if has_payment:
        s += 1
    if has_phone and (has_drug_word or has_selling_language or has_price_weight):
        s += 1

    return s


# ---------- POTW ----------
def _chat_key(chat_id: int) -> str:
    return str(chat_id)


def potw_load():
    global potw_state
    try:
        if os.path.exists(POTW_DATA_FILE):
            with open(POTW_DATA_FILE, "r", encoding="utf-8") as f:
                potw_state = json.load(f)
                if "chats" not in potw_state:
                    potw_state = {"chats": {}}
    except Exception as e:
        log.warning("POTW load failed: %s", e)
        potw_state = {"chats": {}}


def potw_save():
    try:
        with open(POTW_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(potw_state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("POTW save failed: %s", e)


def potw_get_chat(chat_id: int) -> dict:
    ck = _chat_key(chat_id)
    if ck not in potw_state["chats"]:
        potw_state["chats"][ck] = {"submissions": [], "current_week_poll": None}
    return potw_state["chats"][ck]


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


def user_display_name(u) -> str:
    try:
        return u.full_name or u.first_name or "someone"
    except Exception:
        return "someone"


# ---------- COMMANDS ----------
async def start_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("I'm Tina, the mod bot. Admins: /help")


async def help_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "/ban (reply)  | /kick (reply)\n"
        "/mute (reply) [minutes] | /unmute (reply)\n"
        "/pin (reply)  | /warn (reply)\n"
        "/rules | /trust (reply/id) | /untrust (reply/id)\n"
        "/prompt | /tina | /chatid"
    )


async def prompt_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(random.choice(CONVERSATION_PROMPTS))


async def tina_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "Tina is awake, caffeinated, and trying very hard not to accuse hotel guests of drug dealing today 💛"
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
    await u.message.reply_text(f"Banned {user_display_name(target)}.")


async def kick_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not u.message.reply_to_message:
        return await u.message.reply_text("Reply to the user's message to kick.")
    if not await is_admin(u, u.effective_user.id):
        return await u.message.reply_text("Admin only.")
    target = u.message.reply_to_message.from_user
    try:
        await u.effective_chat.ban_member(target.id)
        await u.effective_chat.unban_member(target.id)
    except Exception as e:
        log.warning("kick failed: %s", e)
    await u.message.reply_text(f"Kicked {user_display_name(target)}.")


async def mute_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not u.message.reply_to_message:
        return await u.message.reply_text("Reply to the user's message to mute.")
    if not await is_admin(u, u.effective_user.id):
        return await u.message.reply_text("Admin only.")
    minutes = 10
    if c.args:
        try:
            minutes = int(c.args[0])
        except Exception:
            pass
    target = u.message.reply_to_message.from_user
    await mute_user(u.effective_chat, target.id, minutes * 60, c)
    await u.message.reply_text(f"Muted {user_display_name(target)} for {minutes} minutes.")


async def unmute_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not u.message.reply_to_message:
        return await u.message.reply_text("Reply to the user's message to unmute.")
    if not await is_admin(u, u.effective_user.id):
        return await u.message.reply_text("Admin only.")
    target = u.message.reply_to_message.from_user
    perms = ChatPermissions(
        can_send_messages=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True
    )
    try:
        await c.bot.restrict_chat_member(u.effective_chat.id, target.id, permissions=perms)
    except Exception as e:
        log.warning("unmute failed: %s", e)
    await u.message.reply_text(f"Unmuted {user_display_name(target)}.")


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
    await u.message.reply_text(f"{user_display_name(target)} warned ({w}).")
    if w >= WARN_BEFORE_MUTE:
        await mute_user(u.effective_chat, target.id, MUTE_DURATION_SECONDS, c)
        await u.message.reply_text(f"{user_display_name(target)} auto-muted for repeat warnings.")
        warnings[target.id] = 0


async def rules_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    chat_id = u.effective_chat.id
    now = datetime.utcnow()

    last = last_rules_sent_at[chat_id]
    if last and (now - last).total_seconds() < RULES_COOLDOWN_SECONDS:
        try:
            await u.message.delete()
        except Exception:
            pass
        return

    last_rules_sent_at[chat_id] = now

    await u.message.reply_text(
        "Group rules:\n"
        "1) 18+ only\n"
        "2) Consent always\n"
        "3) No dealing / illegal activity\n"
        "4) No doxxing\n"
        "5) Don't be a dick, just suck one"
    )


async def trust_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(u, u.effective_user.id):
        return await u.message.reply_text("Admin only.")
    if u.message.reply_to_message:
        uid = u.message.reply_to_message.from_user.id
    elif c.args:
        try:
            uid = int(c.args[0])
        except Exception:
            return await u.message.reply_text("Reply to a user or provide their numeric id.")
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
        try:
            uid = int(c.args[0])
        except Exception:
            return await u.message.reply_text("Reply to a user or provide their numeric id.")
    else:
        return await u.message.reply_text("Reply to a user or provide their numeric id.")
    TRUSTED_USER_IDS.discard(uid)
    await u.message.reply_text(f"User {uid} removed from trusted list.")


async def chatid_cmd(u: Update, c: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(f"Chat ID: {u.effective_chat.id}")


# ---------- WELCOME ----------
async def welcome_new(u: Update, c: ContextTypes.DEFAULT_TYPE):
    if not u.message or not u.message.new_chat_members:
        return

    chat_id = u.effective_chat.id
    for m in u.message.new_chat_members:
        name = (m.first_name or m.full_name or "someone").strip()
        pending_welcomes[chat_id].append(f"[{name}](tg://user?id={m.id})")


async def welcome_flush_job(ctx: ContextTypes.DEFAULT_TYPE):
    now = datetime.utcnow()
    for chat_id, mentions in list(pending_welcomes.items()):
        if not mentions:
            continue

        last = last_welcome_sent_at[chat_id]
        if last and (now - last).total_seconds() < WELCOME_BATCH_SECONDS:
            continue

        text = "Welcome " + ", ".join(mentions[:40]) + " 🥳\nPlease read /rules"
        try:
            await ctx.bot.send_message(chat_id, text, parse_mode="Markdown")
            last_welcome_sent_at[chat_id] = now
            pending_welcomes[chat_id].clear()
        except Exception as e:
            log.warning("welcome_flush_job failed for chat %s: %s", chat_id, e)


# ---------- PHOTO OF THE WEEK ----------
def _is_photo_submission(msg) -> bool:
    if not msg or not msg.photo:
        return False
    if not POTW_HASHTAG_ONLY:
        return True
    cap = (msg.caption or "").lower()
    txt = (msg.text or "").lower()
    return (POTW_HASHTAG in cap) or (POTW_HASHTAG in txt)


async def potw_collect(u: Update, c: ContextTypes.DEFAULT_TYPE):
    msg = u.message
    if not msg or not msg.photo or not msg.from_user:
        return
    if not _is_photo_submission(msg):
        return

    chat_id = u.effective_chat.id
    chat_state = potw_get_chat(chat_id)

    biggest = msg.photo[-1]
    submission = {
        "file_id": biggest.file_id,
        "user_id": msg.from_user.id,
        "user_name": user_display_name(msg.from_user),
        "msg_id": msg.message_id,
        "ts": datetime.now(TZ).isoformat()
    }
    chat_state["submissions"].append(submission)
    potw_save()


async def potw_sunday_post(ctx: ContextTypes.DEFAULT_TYPE):
    for chat_id_str, chat_state in list(potw_state.get("chats", {}).items()):
        try:
            chat_id = int(chat_id_str)
        except Exception:
            continue

        subs = chat_state.get("submissions", [])
        if not subs:
            continue

        if chat_state.get("current_week_poll"):
            continue

        chosen = random.sample(subs, k=min(POTW_FINALISTS, len(subs)))
        options_map = []

        try:
            await ctx.bot.send_message(chat_id, "📸 Photo of the Week time! Here are the finalists:")
        except Exception:
            pass

        for i, s in enumerate(chosen, start=1):
            caption = f"Photo {i}\nSubmitted by: {s.get('user_name', 'someone')}"
            try:
                await ctx.bot.send_photo(chat_id, s["file_id"], caption=caption)
            except Exception as e:
                log.warning("Failed to send finalist photo to %s: %s", chat_id, e)

            try:
                idx = subs.index(s)
            except ValueError:
                idx = None
            options_map.append(idx)

        poll_options = [str(i) for i in range(1, len(chosen) + 1)]
        try:
            poll_msg = await ctx.bot.send_poll(
                chat_id=chat_id,
                question="Vote for Photo of the Week:",
                options=poll_options,
                is_anonymous=False,
                allows_multiple_answers=False
            )
            chat_state["current_week_poll"] = {
                "poll_message_id": poll_msg.message_id,
                "options_map": options_map,
                "created_at": datetime.now(TZ).isoformat()
            }
            potw_save()
        except Exception as e:
            log.warning("Failed to create poll in chat %s: %s", chat_id, e)


async def potw_monday_announce(ctx: ContextTypes.DEFAULT_TYPE):
    for chat_id_str, chat_state in list(potw_state.get("chats", {}).items()):
        try:
            chat_id = int(chat_id_str)
        except Exception:
            continue

        poll_info = chat_state.get("current_week_poll")
        if not poll_info:
            continue

        poll_message_id = poll_info.get("poll_message_id")
        options_map = poll_info.get("options_map", [])
        subs = chat_state.get("submissions", [])

        if not poll_message_id:
            chat_state["current_week_poll"] = None
            potw_save()
            continue

        try:
            final_poll = await ctx.bot.stop_poll(chat_id=chat_id, message_id=poll_message_id)
        except Exception as e:
            log.warning("Failed to stop poll in chat %s: %s", chat_id, e)
            continue

        if not final_poll or not getattr(final_poll, "options", None):
            continue

        counts = [opt.voter_count for opt in final_poll.options]
        if not counts:
            continue

        max_votes = max(counts)
        top_indexes = [i for i, count in enumerate(counts) if count == max_votes]
        winner_choice_index = random.choice(top_indexes)

        winner_sub_idx = None
        if winner_choice_index < len(options_map):
            winner_sub_idx = options_map[winner_choice_index]

        winner = None
        if winner_sub_idx is not None and 0 <= winner_sub_idx < len(subs):
            winner = subs[winner_sub_idx]

        if winner:
            winner_name = winner.get("user_name", "someone")
            winner_user_id = winner.get("user_id")

            try:
                await ctx.bot.send_message(
                    chat_id,
                    f"🏆 Photo of the Week winner: {winner_name}!\n"
                    f"Winning photo: {winner_choice_index + 1} with {max_votes} vote(s)."
                )
            except Exception:
                pass

            if winner_user_id:
                try:
                    await ctx.bot.send_message(
                        winner_user_id,
                        "🏆 You won Photo of the Week! Your photo got the most votes. Congrats! 🎉"
                    )
                except Exception:
                    pass
        else:
            try:
                await ctx.bot.send_message(
                    chat_id,
                    f"🏆 Photo of the Week results are in! Winning option: {winner_choice_index + 1} with {max_votes} vote(s)."
                )
            except Exception:
                pass

        chat_state["submissions"] = []
        chat_state["current_week_poll"] = None
        potw_save()


# ---------- MESSAGE FILTER ----------
async def filter_message(u: Update, c: ContextTypes.DEFAULT_TYPE):
    msg = u.message
    if not msg:
        return
    if msg.from_user and msg.from_user.is_bot:
        return

    try:
        if msg.photo:
            await potw_collect(u, c)
    except Exception:
        pass

    if msg.from_user and msg.from_user.id in TRUSTED_USER_IDS:
        return

    try:
        if ADMIN_BYPASS and msg.from_user and await is_admin(u, msg.from_user.id):
            return
    except Exception:
        pass

    pieces = []
    if msg.text:
        pieces.append(msg.text)
    if msg.caption:
        pieces.append(msg.caption)

    try:
        entities = (msg.entities or []) + (msg.caption_entities or [])
        base = msg.text or msg.caption or ""
        for ent in entities:
            if ent.type == "url":
                pieces.append(base[ent.offset:ent.offset + ent.length])
            elif ent.type == "text_link" and getattr(ent, "url", None):
                pieces.append(ent.url)
    except Exception:
        pass

    full_text = " ".join(pieces).strip()
    down = full_text.lower()

    # ===== Dealer ad detection =====
    score = ad_score(full_text)

    if score >= 6 and msg.from_user:
        user_id = msg.from_user.id

        try:
            await msg.delete()
        except Exception:
            pass

        now = datetime.utcnow()
        user_offenses[user_id] += 1

        if user_offenses[user_id] == 1:
            until_dt = now + timedelta(days=3)
            restriction_until[user_id] = until_dt
            await restrict_until(u.effective_chat, user_id, until_dt, c)

            try:
                await u.effective_chat.send_message(
                    f"{msg.from_user.first_name}, Tina here 💛 I’ve removed that because it looks like it may involve buying/selling illegal substances. "
                    f"We can’t allow that here, so you’ve been restricted for 3 days. If this was wrong, an admin can review it."
                )
            except Exception:
                pass

            log_text = (
                f"<b>Dealer Ad — FIRST OFFENSE</b>\n"
                f"<b>User:</b> {html.escape(user_display_name(msg.from_user))} (id: <code>{user_id}</code>)\n"
                f"<b>Chat:</b> {html.escape(u.effective_chat.title or str(u.effective_chat.id))} "
                f"(id: <code>{u.effective_chat.id}</code>)\n"
                f"<b>Score:</b> {score}\n"
                f"<b>Restricted until:</b> {until_dt.isoformat()} UTC\n\n"
                f"<b>Content:</b>\n{html.escape(full_text)[:2000]}"
            )
            await send_admin_log(c, log_text)
            return

        until_dt = restriction_until.get(user_id)
        if until_dt and now >= until_dt:
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
                f"<b>User:</b> {html.escape(user_display_name(msg.from_user))} (id: <code>{user_id}</code>)\n"
                f"<b>Chat:</b> {html.escape(u.effective_chat.title or str(u.effective_chat.id))} "
                f"(id: <code>{u.effective_chat.id}</code>)\n"
                f"<b>Score:</b> {score}\n"
                f"<b>Restriction expired:</b> {until_dt.isoformat()} UTC\n\n"
                f"<b>Content:</b>\n{html.escape(full_text)[:2000]}"
            )
            await send_admin_log(c, log_text)

            user_offenses.pop(user_id, None)
            restriction_until.pop(user_id, None)
            return

        try:
            await u.effective_chat.send_message(
                f"{msg.from_user.first_name}, you're currently restricted."
            )
        except Exception:
            pass
        return

    # ===== Bad words =====
    if down and any(b in down for b in BANNED_WORDS):
        try:
            await msg.delete()
        except Exception:
            pass
        try:
            await u.effective_chat.send_message(f"{msg.from_user.first_name}, that language is not allowed.")
        except Exception:
            pass
        return

    # ===== Link filter =====
    if LINK_FILTER and down and ("http://" in down or "https://" in down or "t.me/" in down):
        if msg.from_user and not await is_admin(u, msg.from_user.id):
            try:
                await msg.delete()
            except Exception:
                pass
            try:
                await u.effective_chat.send_message(
                    f"{msg.from_user.first_name}, Tina here 👋 Links need admin approval, so I’ve removed that one for now."
                )
            except Exception:
                pass
            return

    # ===== Flood =====
    if msg.from_user:
        cnt = record_message(msg.from_user.id)
        if cnt >= FLOOD_LIMIT:
            try:
                await u.effective_chat.send_message(
                    f"{msg.from_user.first_name} — you're posting a bit too fast. Tina has muted you for a breather."
                )
                await mute_user(u.effective_chat, msg.from_user.id, MUTE_DURATION_SECONDS, c)
                message_times[msg.from_user.id].clear()
            except Exception:
                pass


# ---------- MAIN ----------
def main():
    if not TOKEN:
        raise RuntimeError("TOKEN env var not set (or BOT_TOKEN).")

    potw_load()

    app = ApplicationBuilder().token(TOKEN).build()

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
    app.add_handler(CommandHandler("prompt", prompt_cmd))
    app.add_handler(CommandHandler("tina", tina_cmd))

    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), filter_message))

    media_filters = filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE | filters.Document.ALL
    app.add_handler(MessageHandler(media_filters, filter_message))

    app.job_queue.run_repeating(welcome_flush_job, interval=60 * 10, first=60)

    app.job_queue.run_daily(potw_sunday_post, time=POTW_SUNDAY_TIME, days=(6,))
    app.job_queue.run_daily(potw_monday_announce, time=POTW_MONDAY_TIME, days=(0,))

    print("Tina starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
