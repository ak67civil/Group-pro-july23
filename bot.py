import os
import re
import time
import logging
import asyncio
import pymongo
import json
import random
import string
from datetime import datetime, timedelta
from pyrogram.enums import ParseMode
from pyrogram import Client, filters
from pyrogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from pyrogram.errors import FloodWait, MessageNotModified, QueryIdInvalid

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
API_ID     = int(os.environ["TELEGRAM_API_ID"])
API_HASH   = os.environ["TELEGRAM_API_HASH"]
OWNER_ID   = int(os.environ["OWNER_ID"])

PIC_CHANNEL = "@GrokBotsPics"
PIC_MSG_ID  = 133

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

app = Client("protection_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ---------------------------------------------------------------------------
# Database (MongoDB Atlas — persistent across Heroku redeploys/restarts)
# ---------------------------------------------------------------------------
MONGO_URI = os.environ["MONGO_URI"]
mongo_client = pymongo.MongoClient(MONGO_URI)
mdb = mongo_client["protection_bot"]

props_col = mdb["props"]           # {_id: key, value: value}
state_col = mdb["user_state"]      # {_id: user_id, state, temp}
last_msg_col = mdb["last_msg"]     # {_id: user_id, msg_id}

def db_init():
    # MongoDB creates collections/indexes lazily on first write — nothing to
    # create up front. This ping just confirms the connection works at boot,
    # so a bad MONGO_URI fails loudly on startup instead of silently later.
    mongo_client.admin.command("ping")
    logger.info("MongoDB connection OK")

def get_prop(key, default=None):
    doc = props_col.find_one({"_id": key})
    if doc is None:
        return default
    return doc.get("value", default)

def set_prop(key, value):
    props_col.update_one({"_id": key}, {"$set": {"value": value}}, upsert=True)

def get_state(user_id):
    doc = state_col.find_one({"_id": user_id})
    if doc:
        return doc.get("state") or "", doc.get("temp") or ""
    return "", ""

def set_state(user_id, state, temp=""):
    state_col.update_one({"_id": user_id}, {"$set": {"state": state, "temp": temp}}, upsert=True)

def get_last_msg(user_id):
    doc = last_msg_col.find_one({"_id": user_id})
    return doc.get("msg_id") if doc else None

def set_last_msg(user_id, msg_id):
    last_msg_col.update_one({"_id": user_id}, {"$set": {"msg_id": msg_id}}, upsert=True)

def get_all_user_ids():
    """All user_ids that have ever interacted with the bot (used for the
    owner's 'all users' admin view and broadcast)."""
    return state_col.distinct("_id")



# ---------------------------------------------------------------------------
# Small Caps Font
# ---------------------------------------------------------------------------
SMALL_CAPS = {
    'a':'ᴀ','b':'ʙ','c':'ᴄ','d':'ᴅ','e':'ᴇ','f':'ғ','g':'ɢ','h':'ʜ',
    'i':'ɪ','j':'J','k':'ᴋ','l':'ʟ','m':'ᴍ','n':'ɴ','o':'ᴏ','p':'ᴘ',
    'q':'ǫ','r':'ʀ','s':'s','t':'ᴛ','u':'ᴜ','v':'ᴠ','w':'ᴡ','x':'x',
    'y':'ʏ','z':'ᴢ'
}

def apply_font(text: str) -> str:
    if not text:
        return ""
    result = ""
    capitalize_next = True
    in_code = False
    in_tag = False
    tag_buf = ""
    for ch in text:
        if ch == '<' and not in_code:
            tag_buf = "<"; in_tag = True; continue
        if in_tag:
            tag_buf += ch
            if ch == '>':
                result += tag_buf
                lt = tag_buf.lower()
                if lt == "<code>": in_code = True
                if lt == "</code>": in_code = False
                in_tag = False; tag_buf = ""
            continue
        if in_code:
            result += ch; continue
        if ch.isalpha():
            if capitalize_next:
                result += ch.upper(); capitalize_next = False
            else:
                result += SMALL_CAPS.get(ch.lower(), ch.lower())
        else:
            result += ch
            if ch in " \n_.,:;!?-()[]":
                capitalize_next = True
    return result

def safe_name(user) -> str:
    fn = user.first_name or "User"
    ln = (" " + user.last_name) if user.last_name else ""
    return (fn + ln).strip()

def format_date(ms) -> str:
    if not ms:
        return "Unknown"
    d = datetime.fromtimestamp(ms / 1000)
    return f"{d.day}/{d.month}/{d.year}"

def gen_uid(admin_id: int) -> str:
    suffix = str(admin_id)[-3:]
    rand = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f"a{suffix}{rand}"

async def resolve_channel_info(client: Client, chat_id_raw: str):
    """Fetch a channel's title + a clickable link so admins can tell channels
    apart at a glance instead of just seeing raw IDs. Falls back gracefully
    (name/link = None) if the bot isn't a member yet or lacks permission —
    the pair is still saved either way, just without the extra info."""
    name, link = None, None
    try:
        chat = await client.get_chat(chat_id_raw.strip())
        name = chat.title or chat.first_name or chat_id_raw
        if chat.username:
            link = f"https://t.me/{chat.username}"
        else:
            try:
                link = await client.export_chat_invite_link(chat_id_raw.strip())
            except Exception as e:
                logger.warning(f"Could not export invite link for {chat_id_raw}: {e}")
    except Exception as e:
        logger.warning(f"Could not resolve channel info for {chat_id_raw}: {e}")
    return name, link

def format_channel_display(chat_id, name, link):
    label = name or str(chat_id)
    if link:
        return f'<a href="{link}">{label}</a> (<code>{chat_id}</code>)'
    return f"<code>{chat_id}</code>" + (f" — {label}" if name else "")

def extract_channel_id(message: Message, text: str) -> str:
    """Prefer a forwarded message's origin chat — forwarding carries the
    channel's full peer info, so Pyrogram can resolve/cache it immediately.
    A typed/pasted ID has no such info and will fail with 'Peer id invalid'
    until the bot happens to receive some other update from that channel."""
    if message.forward_from_chat:
        return str(message.forward_from_chat.id)
    return text.strip()

def extract_topic(caption: str):
    """Pull the topic out of a caption for the index feature. Looks for a
    line like 'Topic : X' (case-insensitive). Supports a 'Group → Item'
    hierarchy on that line (returns (group, item)). Falls back to the
    caption's first line, or 'Untitled' if there's no caption at all."""
    if not caption or not caption.strip():
        return None, "Untitled"
    for line in caption.strip().split("\n"):
        line = line.strip()
        m = re.match(r"(?i)^topic\s*:\s*(.+)$", line)
        if m:
            val = m.group(1).strip()
            if "→" in val:
                parent, child = [p.strip() for p in val.split("→", 1)]
                return parent, child
            return None, val
    first_line = caption.strip().split("\n")[0].strip()
    return None, first_line or "Untitled"

def extract_topic_for_routing(caption: str) -> str:
    """Strict topic extraction used ONLY for group-topic (forum) routing.

    Unlike extract_topic() above (which falls back to the caption's first
    line — fine for the index feature, but dangerous here), this function
    trusts ONLY an explicit 'Topic : X' line. If that line is missing, or
    if the admin accidentally appended extra text after the topic name
    (e.g. 'Topic : Hindi - Lecture 5'), we still isolate just the topic
    label by cutting at the first '-', '|', or '(' that follows it — this
    is the permanent guard against the bot confusing the topic name with
    trailing lecture/video-specific text. No explicit Topic line at all =
    the video goes into a single shared 'Uncategorized' topic instead of
    spawning a new (wrong) topic from random first-line text.
    """
    if not caption or not caption.strip():
        return "Uncategorized"
    for line in caption.strip().split("\n"):
        line = line.strip()
        m = re.match(r"(?i)^topic\s*:\s*(.+)$", line)
        if m:
            val = m.group(1).strip()
            if "→" in val:
                val = val.split("→", 1)[1].strip()
            # Cut off anything after a separator that usually introduces
            # lecture/video-specific text on the same line, so a caption
            # like "Topic : Hindi - Lecture 5" still resolves to "Hindi".
            val = re.split(r"\s*[-|(]\s*", val, maxsplit=1)[0].strip()
            return val if val else "Uncategorized"
    return "Uncategorized"

def normalize_topic(name: str) -> str:
    """Canonical form used to compare topic names — collapses whitespace
    and case so 'Hindi', ' hindi ', 'HINDI' are always treated as the
    SAME topic. This is the core of the permanent-fix: topic identity is
    decided by this normalized key, never by message order or exact
    caption text, so the bot can never 'lose track' of which topic a
    video belongs to."""
    if not name:
        return "uncategorized"
    return re.sub(r"\s+", " ", name.strip()).casefold()

# ---------------------------------------------------------------------------
# Admin / Ban helpers
# ---------------------------------------------------------------------------
def get_admins():
    lst = get_prop("admins_list", [])
    if OWNER_ID not in lst:
        lst.append(OWNER_ID)
    return lst

def is_admin(uid: int) -> bool:
    if uid == OWNER_ID:
        return True
    lst = get_admins()
    if uid not in lst:
        return False
    plan = get_prop(f"admin_plan_{uid}")
    if plan and plan.get("end_time", 0) < int(time.time() * 1000):
        return False
    return True

def is_banned(uid: int) -> bool:
    return bool(get_prop(f"banned_{uid}", False))

def set_banned(uid: int, val: bool):
    set_prop(f"banned_{uid}", val)

def check_daily_limit(uid: int) -> bool:
    if is_admin(uid):
        return True
    limit = get_prop(f"daily_limit_{uid}")
    if limit is None or limit == "unlimited" or limit == 0:
        return True
    today = datetime.now().strftime("%Y-%m-%d")
    if get_prop(f"daily_date_{uid}") != today:
        set_prop(f"daily_count_{uid}", 0)
        set_prop(f"daily_date_{uid}", today)
    count = get_prop(f"daily_count_{uid}", 0)
    if count >= limit:
        return False
    set_prop(f"daily_count_{uid}", count + 1)
    return True

# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------
def btn(text, cb_or_url, use_url=False):
    t = apply_font(text)
    if use_url or (isinstance(cb_or_url, str) and (cb_or_url.startswith("http") or cb_or_url.startswith("tg://"))):
        return InlineKeyboardButton(t, url=cb_or_url)
    return InlineKeyboardButton(t, callback_data=cb_or_url)

def main_kb(uid):
    rows = []
    if uid == OWNER_ID:
        rows.append([btn("👑 Owner Panel", "owner_main")])
    elif is_admin(uid):
        rows.append([btn("🛡️ Admin Panel", "admin_main")])
    return InlineKeyboardMarkup(rows) if rows else None

def owner_kb():
    maint = get_prop("maintenance_mode", False)
    return InlineKeyboardMarkup([
        [btn("👨‍💻 Manage Admins", "own_admins_menu"), btn("📊 Global Statistics", "own_stats")],
        [btn("👥 Manage Users", "adm_manage_user_0"), btn("📣 Broadcast", "own_bc")],
        [btn(f"{'🔴 Maintenance: ON' if maint else '🟢 Maintenance: OFF'}", "own_toggle_maint")],
        [btn("📝 Set Log Channel", "own_set_log"), btn("📚 Help", "help_menu")],
        [btn("🏠 Back To Dashboard", "go_home")],
    ])

def admin_kb(uid):
    notify = get_prop(f"user_notify_{uid}", False)
    return InlineKeyboardMarkup([
        [btn("⚙️ Channel Configurations", "config_menu")],
        [btn("📊 My Statistics", "adm_my_stats"), btn("👥 Manage Users", "adm_manage_user_0")],
        [btn(f"{'🔔 Alerts: ON' if notify else '🔕 Alerts: OFF'}", "adm_toggle_notify")],
        [btn("📚 Help", "help_menu")],
        [btn("🏠 Back To Dashboard", "go_home")],
    ])

def user_manage_kb(uid):
    return InlineKeyboardMarkup([
        [btn(f"🚫 Ban {uid}", f"act_ban_{uid}"), btn(f"✅ Unban {uid}", f"act_unban_{uid}")],
        [btn("📈 Set Daily Limit", f"act_limit_{uid}"), btn("💬 Private Message", f"act_pm_{uid}")],
        [btn("🔙 Back", "go_home")],
    ])

def back_kb(uid):
    return InlineKeyboardMarkup([[btn("🔙 Return Back", "owner_main" if uid == OWNER_ID else "admin_main")]])

# ---------------------------------------------------------------------------
# send_msg helper (edit if callback, else send with pic)
# ---------------------------------------------------------------------------
async def send_msg(client, chat_id, text, markup=None, is_cb=False, cb_msg=None):
    final = apply_font(text)
    last = get_last_msg(chat_id)

    if is_cb and cb_msg:
        mid = cb_msg.id
        try:
            if cb_msg.photo or cb_msg.video or cb_msg.document or cb_msg.animation:
                await cb_msg.edit_caption(final, parse_mode=ParseMode.HTML, reply_markup=markup)
            else:
                await cb_msg.edit_text(final, parse_mode=ParseMode.HTML, reply_markup=markup,
                                       disable_web_page_preview=True)
            return mid
        except MessageNotModified:
            return mid
        except Exception:
            pass

    if last:
        try:
            await client.delete_messages(chat_id, last)
        except Exception:
            pass

    try:
        res = await client.copy_message(
    chat_id=chat_id, from_chat_id=PIC_CHANNEL, message_id=PIC_MSG_ID,
    caption=final, parse_mode=ParseMode.HTML, reply_markup=markup
        )
    
        set_last_msg(chat_id, res.id)
        return res.id
    except Exception:
        res = await client.send_message(chat_id, final, parse_mode=ParseMode.HTML,
        
                                        reply_markup=markup,
                                        disable_web_page_preview=True)
        set_last_msg(chat_id, res.id)
        return res.id

# ---------------------------------------------------------------------------
# Config Menu
# ---------------------------------------------------------------------------
async def show_config_menu(client, chat_id, uid, is_cb=False, cb_msg=None):
    pairs = get_prop(f"channels_{uid}", [])
    limit = 999 if uid == OWNER_ID else get_prop(f"chan_limit_{uid}", 0)

    txt = ("<b>⚙️ Channel Configurations</b>\n\n"
           "<blockquote><b>Link your source and target channels below to automate "
           "forwarding.</b></blockquote>\n\n"
           f"<b>📈 Pairs Limit:</b> <code>{len(pairs)} / "
           f"{'Unlimited' if limit == 999 else limit}</code>\n\n")

    rows = []
    if not pairs:
        txt += "<i>No channels connected yet.</i>\n"
    else:
        for i, p in enumerate(pairs):
            target_disp = format_channel_display(p['target'], p.get('target_name'), p.get('target_link'))
            source_disp = format_channel_display(p['source'], p.get('source_name'), p.get('source_link'))
            txt += f"<b>{i+1}. Config:</b>\n🎯 Target: {target_disp}\n📣 Source: {source_disp}\n\n"
            rows.append([btn(f"❌ Remove Config {i+1}", f"rem_pair_{i}")])
            rows.append([btn(f"📑 Generate Index {i+1}", f"idx_gen_{i}"),
                         btn(f"🗑 Clear Index {i+1}", f"idx_clear_{i}")])

    if len(pairs) < limit:
        rows.append([btn("➕ Link New Channels", "add_pair_start")])
    elif uid != OWNER_ID:
        txt += "\n⚠️ <i>Channel limit reached. Contact Owner for upgrade.</i>"

    rows.append([btn("🔙 Back To Panel", "owner_main" if uid == OWNER_ID else "admin_main")])
    await send_msg(client, chat_id, txt, InlineKeyboardMarkup(rows), is_cb, cb_msg)

# ---------------------------------------------------------------------------
# /start handler
# ---------------------------------------------------------------------------
@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    uid = message.from_user.id
    text = message.text or ""

    # Track user join
    if not get_prop(f"join_date_{uid}"):
        set_prop(f"join_date_{uid}", int(time.time() * 1000))
    if not get_prop(f"uname_{uid}"):
        set_prop(f"uname_{uid}", safe_name(message.from_user))

    # Deep link media: /start vid_XXXX
    if "vid_" in text:
        unique_id = text.split("vid_")[1].strip()
        media_data = get_prop(f"m_{unique_id}")

        if not media_data:
            await send_msg(client, uid,
                "<b>❌ Deprecated Data Link</b>\n\n"
                "<blockquote><b>This content is no longer available.</b></blockquote>")
            return

        if not check_daily_limit(uid):
            await send_msg(client, uid,
                "<b>❌ Daily Limit Reached</b>\n\n"
                "<blockquote><b>You have exhausted your daily media quota. "
                "Contact admin.</b></blockquote>")
            return

        admin_uid = media_data.get("admin_uid")
        if admin_uid:
            if not get_prop(f"joined_via_{uid}"):
                set_prop(f"joined_via_{uid}", admin_uid)
            views = get_prop(f"views_own_{admin_uid}", 0) + 1
            set_prop(f"views_own_{admin_uid}", views)
            viewers = get_prop(f"viewers_{admin_uid}", [])
            if uid not in viewers:
                viewers.append(uid)
                set_prop(f"viewers_{admin_uid}", viewers)

            # Notify admin first time
            if not get_prop(f"notified_first_{uid}"):
                set_prop(f"notified_first_{uid}", True)
                if get_prop(f"user_notify_{admin_uid}"):
                    kb = InlineKeyboardMarkup([[btn("⚙️ Manage User", f"adm_uid_{uid}")]])
                    await client.send_message(
                        admin_uid,
                        apply_font(f"<b>🔔 New User Alert</b>\n\n"
                                   f"<b>👤 Name:</b> <code>{safe_name(message.from_user)}</code>\n"
                                   f"<b>🆔 ID:</b> <code>{uid}</code>"),
                        parse_mode=ParseMode.HTML, reply_markup=kb
                    )

        set_prop(f"views_by_{uid}", get_prop(f"views_by_{uid}", 0) + 1)

        # ASCII loading animation
        frames = [
            "____ \n| __ \n|__] ",
            "____ ____ \n| __ |  | \n|__] |__| ",
            "____ ____ ___  \n| __ |  | |  \\ \n|__] |__| |__/ ",
            "____ ____ ___  ____ \n| __ |  | |  \\ [__  \n|__] |__| |__/ ___] ",
            "____ ____ ___  ____ ____ \n| __ |  | |  \\ [__  |  | \n|__] |__| |__/ ___] |__| ",
            "____ ____ ___  ____ ____ _  _ \n| __ |  | |  \\ [__  |  | |\\ | \n|__] |__| |__/ ___] |__| | \\| ",
        ]
        mid = await send_msg(client, uid, f"<code>{frames[0]}</code>")
        for frame in frames[1:]:
            await asyncio.sleep(1.8)
            try:
                await client.edit_message_caption(uid, mid, caption=f"<code>{frame}</code>", parse_mode=ParseMode.HTML)
            except Exception:
                try:
                    await client.edit_message_text(uid, mid, text=f"<code>{frame}</code>", parse_mode=ParseMode.HTML)
                except Exception:
                    pass
        try:
            await client.delete_messages(uid, mid)
        except Exception:
            pass

        # Send protected media
        mtype = media_data.get("type", "video")
        fid = media_data.get("file_id")
        cap = apply_font(media_data.get("caption", "")) or None
        opts = dict(chat_id=uid, parse_mode=ParseMode.HTML, protect_content=True)
        if cap:
            opts["caption"] = cap
        try:
            if mtype == "photo":
                await client.send_photo(**opts, photo=fid)
            elif mtype == "document":
                await client.send_document(**opts, document=fid)
            else:
                await client.send_video(**opts, video=fid)
        except Exception as e:
            logger.exception("Media send failed")
        return

    # Maintenance check
    if get_prop("maintenance_mode", False) and not is_admin(uid):
        await send_msg(client, uid,
            "<b>🛠️ System Under Maintenance</b>\n\n"
            "<blockquote><b>Please check back shortly.</b></blockquote>")
        return

    # Ban check
    if is_banned(uid):
        await send_msg(client, uid,
            "<b>🚫 Security Alert</b>\n\n"
            "<blockquote><b>Your access has been permanently restricted.</b></blockquote>")
        return

    # Unauthorized user
    if not is_admin(uid):
        kb = InlineKeyboardMarkup([[btn(f"📞 Contact Owner To Buy", f"tg://user?id={OWNER_ID}", True)]])
        await send_msg(client, uid,
            f"<b>✨ Premium Bot Network</b>\n\n"
            f"<blockquote><b>👋 Hello {safe_name(message.from_user)}!\n\n"
            f"This is an elite private auto-forwarding and content protection system.\n\n"
            f"You are currently not authorized. Purchase an Administrator Subscription "
            f"to get access.</b></blockquote>", kb)
        return

    set_state(uid, "")
    role = "Master Owner" if uid == OWNER_ID else "Administrator"
    await send_msg(client, uid,
        f"<b>✨ Welcome To The Premium Dashboard</b>\n\n"
        f"<b>👤 Name:</b> <code>{safe_name(message.from_user)}</code>\n"
        f"<b>🆔 System ID:</b> <code>{uid}</code>\n"
        f"<b>🛡️ Role:</b> <code>{role}</code>\n\n"
        f"<blockquote><b>Use the panel below to access system controls.</b></blockquote>",
        main_kb(uid))

# ---------------------------------------------------------------------------
# Callback Query Handler
# ---------------------------------------------------------------------------
async def safe_answer(query: CallbackQuery, *args, **kwargs):
    """query.answer() wrapper — Telegram invalidates a callback query if it
    isn't answered fast enough (or if it's answered twice). Swallow that
    specific error instead of letting it blow up the handler."""
    try:
        await query.answer(*args, **kwargs)
    except QueryIdInvalid:
        logger.warning(f"Callback query expired before it could be answered (uid={query.from_user.id})")

@app.on_callback_query()
async def cb_handler(client: Client, query: CallbackQuery):
    uid = query.from_user.id
    d = query.data
    msg = query.message

    # Run all the gate checks off the event loop in one go. pymongo blocks
    # the whole loop per call; doing these sequentially on the main thread
    # can delay query.answer() past Telegram's window and cause
    # QUERY_ID_INVALID, especially under load. Batching them into a single
    # to_thread call keeps the answer near-instant.
    def _gate():
        return is_banned(uid), get_prop("maintenance_mode", False), is_admin(uid)

    banned, maintenance, admin = await asyncio.to_thread(_gate)

    # Ban gate applies to everyone
    if banned:
        await safe_answer(query, "Access Denied. Account restricted.", show_alert=True)
        return

    # vid_ callback → redirect to deep link.
    # IMPORTANT: this must run for ALL users (viewers in the source channel),
    # not just admins, and must be the ONLY query.answer() call for this click.
    if d.startswith("vid_"):
        unique_id = d.split("_", 1)[1]
        me = await client.get_me()
        url = f"https://t.me/{me.username}?start=vid_{unique_id}"
        await safe_answer(query, url=url)
        return

    # Maintenance / admin gate — only applies to admin-panel actions below
    if maintenance and not admin:
        await safe_answer(query, "System under maintenance. Back shortly.", show_alert=True)
        return
    if not admin:
        await safe_answer(query, "Unauthorized. Purchase a subscription.", show_alert=True)
        return

    await safe_answer(query)

    if d == "go_home":
        set_state(uid, "")
        role = "Master Owner" if uid == OWNER_ID else "Administrator"
        await send_msg(client, uid,
            f"<b>✨ Welcome To The Premium Dashboard</b>\n\n"
            f"<b>👤 Name:</b> <code>{safe_name(query.from_user)}</code>\n"
            f"<b>🆔 System ID:</b> <code>{uid}</code>\n"
            f"<b>🛡️ Role:</b> <code>{role}</code>\n\n"
            f"<blockquote><b>Use the panel below to access system controls.</b></blockquote>",
            main_kb(uid), True, msg)
        return

    if d == "help_menu":
        help_txt = ("<blockquote><b>📚 System Operations Tutorial</b>\n\n"
                    "<b>1. Channel Configurations:</b>\n"
                    "• Link New Channels → send Target ID (-100...) → send Source ID.\n"
                    "• Target = where you upload raw media.\n"
                    "• Source = where protected links get posted.\n\n"
                    "<b>2. Managing Users:</b>\n"
                    "• Ban/Unban users, set daily view limits, send private messages.\n"
                    "• Send 0 for Unlimited daily limit.\n\n"
                    "<b>3. Owner Exclusives:</b>\n"
                    "• Add/Remove admins with expiry duration (e.g. 30d).\n"
                    "• Set Log Channel for backup archives.\n"
                    "• Broadcast to all users.</blockquote>")
        await send_msg(client, uid, help_txt, back_kb(uid), True, msg)
        return

    if d == "config_menu":
        await show_config_menu(client, uid, uid, True, msg)
        return

    if d == "add_pair_start":
        set_state(uid, "wait_target")
        await send_msg(client, uid,
            "<b>🎯 Linking New Target Channel</b>\n\n"
            "<blockquote><b>Forward any post from the Target channel here "
            "(recommended), or send its Channel ID (starting with -100).</b>\n\n"
            "Forwarding lets the bot resolve the channel's name/link "
            "immediately — a typed ID may fail until the bot has otherwise "
            "seen that channel.</blockquote>",
            InlineKeyboardMarkup([[btn("❌ Cancel", "config_menu")]]), True, msg)
        return

    if d.startswith("rem_pair_"):
        idx = int(d.split("_")[2])
        pairs = get_prop(f"channels_{uid}", [])
        if 0 <= idx < len(pairs):
            pairs.pop(idx)
            set_prop(f"channels_{uid}", pairs)
        await show_config_menu(client, uid, uid, True, msg)
        return

    if d.startswith("idx_gen_"):
        idx = int(d.split("_")[2])
        pairs = get_prop(f"channels_{uid}", [])
        if not (0 <= idx < len(pairs)):
            await safe_answer(query, "Invalid config.", show_alert=True)
            return
        pair = pairs[idx]
        target_id, source_id = pair["target"], pair["source"]
        entries = get_prop(f"index_{target_id}", [])
        if not entries:
            await safe_answer(query, "No videos indexed yet for this channel.", show_alert=True)
            return

        def channel_link(e):
            msg_id = e.get("msg_id")
            if not msg_id:
                # Older entries indexed before this fix have no channel
                # message reference — fall back to the bot deep-link so
                # they still work instead of breaking.
                return f"https://t.me/{me.username}?start=vid_{e['uid']}"
            username = e.get("chat_username")
            if username:
                return f"https://t.me/{username}/{msg_id}"
            chat_id = str(e.get("chat_id", ""))
            if chat_id.startswith("-100"):
                chat_id = chat_id[4:]
            elif chat_id.startswith("-"):
                chat_id = chat_id[1:]
            return f"https://t.me/c/{chat_id}/{msg_id}"

        me = await client.get_me()
        lines = ["📂 <b>TOPICS COVERED IN THIS BATCH:</b>"]
        counter = 0
        last_group = None
        last_topic = None
        for e in entries:
            group, topic = e.get("group"), e.get("topic", "Untitled")
            # Same topic as the immediately previous video → part of the
            # same block (e.g. 5 "History" videos in a row), so only the
            # first video of that run gets a line in the index.
            if topic == last_topic and group == last_group:
                continue
            last_topic = topic
            url = channel_link(e)
            counter += 1
            if group:
                if group != last_group:
                    lines.append(f"\n<blockquote>{group}</blockquote>")
                lines.append(f"    {counter:02d}. <a href=\"{url}\">{topic}</a>")
            else:
                lines.append(f"\n{counter:02d}. <a href=\"{url}\">{topic}</a>")
            last_group = group
        full_text = "\n".join(lines)

        # Telegram caps a single message at 4096 chars — split on line
        # boundaries if the index has grown too long for one message.
        chunks, remaining = [], full_text
        while len(remaining) > 4000:
            split_at = remaining.rfind("\n", 0, 4000)
            if split_at == -1:
                split_at = 4000
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:]
        chunks.append(remaining)

        for chunk in chunks:
            for attempt in range(3):
                try:
                    await client.send_message(int(source_id), chunk,
                        parse_mode=ParseMode.HTML, disable_web_page_preview=True)
                    break
                except FloodWait as e:
                    await asyncio.sleep(e.value)

        await safe_answer(query, "✅ Index posted to the source channel.", show_alert=True)
        return

    if d.startswith("idx_clear_"):
        idx = int(d.split("_")[2])
        pairs = get_prop(f"channels_{uid}", [])
        if 0 <= idx < len(pairs):
            set_prop(f"index_{pairs[idx]['target']}", [])
        await safe_answer(query, "🗑 Index cleared for this channel.", show_alert=True)
        return

    if d == "owner_main" and uid == OWNER_ID:
        set_state(uid, "")
        await send_msg(client, uid, "<b>👑 Master Owner Dashboard</b>", owner_kb(), True, msg)
        return

    if d == "admin_main" and is_admin(uid):
        set_state(uid, "")
        await send_msg(client, uid, "<b>🛡️ Administration Dashboard</b>", admin_kb(uid), True, msg)
        return

    if d == "own_toggle_maint" and uid == OWNER_ID:
        set_prop("maintenance_mode", not get_prop("maintenance_mode", False))
        await send_msg(client, uid, "<b>👑 Master Owner Dashboard</b>", owner_kb(), True, msg)
        return

    if d == "adm_toggle_notify":
        set_prop(f"user_notify_{uid}", not get_prop(f"user_notify_{uid}", False))
        await send_msg(client, uid, "<b>🛡️ Administration Dashboard</b>", admin_kb(uid), True, msg)
        return

    if d == "own_admins_menu" and uid == OWNER_ID:
        kb = InlineKeyboardMarkup([
            [btn("➕ Create Admin", "own_admin_add"), btn("🗑️ Revoke Admin", "own_admin_del")],
            [btn("📋 View Admins", "own_admin_list")],
            [btn("🔙 Return", "owner_main")],
        ])
        await send_msg(client, uid,
            "<b>👨‍💻 Administrator Control Protocol</b>\n\n"
            "<blockquote><b>Select an operation below.</b></blockquote>", kb, True, msg)
        return

    if d == "own_admin_add" and uid == OWNER_ID:
        set_state(uid, "wait_admin_add")
        await send_msg(client, uid,
            "<b>➕ Add Admin (Step 1/2)</b>\n\n"
            "<blockquote><b>Enter the User ID to elevate to Administrator:</b></blockquote>",
            InlineKeyboardMarkup([[btn("❌ Cancel", "own_admins_menu")]]), True, msg)
        return

    if d == "own_admin_del" and uid == OWNER_ID:
        set_state(uid, "wait_admin_del")
        await send_msg(client, uid,
            "<b>🗑️ Revoke Admin</b>\n\n"
            "<blockquote><b>Enter the User ID to remove from Administrators:</b></blockquote>",
            InlineKeyboardMarkup([[btn("❌ Cancel", "own_admins_menu")]]), True, msg)
        return

    if d == "own_admin_list" and uid == OWNER_ID:
        admins = get_admins()
        txt = f"<b>📋 Active Administrators</b>\n\n<b>👑 Owner:</b> <code>{OWNER_ID}</code>\n"
        for a in admins:
            if a == OWNER_ID:
                continue
            plan = get_prop(f"admin_plan_{a}")
            if plan and plan.get("end_time", 0) > int(time.time() * 1000):
                left = int((plan["end_time"] - time.time() * 1000) / 86400000) + 1
                status = f"{left} Days Remaining"
            else:
                status = "Expired"
            txt += f"<b>🛡️ ID:</b> <code>{a}</code> ({status})\n"
        txt += "\n<blockquote><b>Send an Admin ID below to view their analytics.</b></blockquote>"
        set_state(uid, "wait_manage_admin")
        await send_msg(client, uid, txt,
            InlineKeyboardMarkup([[btn("🔙 Return", "own_admins_menu")]]), True, msg)
        return

    if d.startswith("adm_manage_user_"):
        page = int(d.split("_")[3]) or 0
        if uid == OWNER_ID:
            # All users in DB
            users_list = get_all_user_ids()
        else:
            users_list = get_prop(f"viewers_{uid}", [])

        total = len(users_list)
        start = page * 25
        sliced = users_list[start:start+25]

        txt = f"<b>👥 User Directory (Page {page+1})</b>\n\n<blockquote><b>Copy an ID and send it below.</b></blockquote>\n\n"
        if not sliced:
            txt += "<i>No users found.</i>\n"
        for u in sliced:
            name = get_prop(f"uname_{u}", "User")
            txt += f"👤 {name} - <code>{u}</code>\n"

        nav = []
        if page > 0:
            nav.append(btn("◀️ Prev", f"adm_manage_user_{page-1}"))
        if start + 25 < total:
            nav.append(btn("Next ▶️", f"adm_manage_user_{page+1}"))

        rows_kb = []
        if nav:
            rows_kb.append(nav)
        rows_kb.append([btn("🔙 Return", "owner_main" if uid == OWNER_ID else "admin_main")])
        set_state(uid, "wait_manage_uid")
        await send_msg(client, uid, txt, InlineKeyboardMarkup(rows_kb), True, msg)
        return

    if d.startswith("adm_uid_"):
        target = int(d.split("_")[2])
        await _send_user_stats(client, uid, target, True, msg)
        return

    if d.startswith("act_ban_"):
        target = int(d.split("_")[2])
        set_banned(target, True)
        await send_msg(client, uid, "<b>✅ User has been banned.</b>", user_manage_kb(target), True, msg)
        return

    if d.startswith("act_unban_"):
        target = int(d.split("_")[2])
        set_banned(target, False)
        await send_msg(client, uid, "<b>✅ Ban has been lifted.</b>", user_manage_kb(target), True, msg)
        return

    if d.startswith("act_limit_"):
        target = d.split("_")[2]
        set_state(uid, f"wait_limit_{target}")
        await send_msg(client, uid,
            f"<b>⚙️ Set Daily Limit for <code>{target}</code></b>\n\n"
            "<blockquote><b>Enter daily quota (0 = Unlimited):</b></blockquote>",
            InlineKeyboardMarkup([[btn("❌ Cancel", f"adm_uid_{target}")]]), True, msg)
        return

    if d.startswith("act_pm_"):
        target = d.split("_")[2]
        set_state(uid, f"wait_pm_{target}")
        await send_msg(client, uid,
            f"<b>💬 Private Message to <code>{target}</code></b>\n\n"
            "<blockquote><b>Type your message now:</b></blockquote>",
            user_manage_kb(int(target)), True, msg)
        return

    if d == "adm_my_stats":
        pairs = get_prop(f"channels_{uid}", [])
        viewers = get_prop(f"viewers_{uid}", [])
        joined = format_date(get_prop(f"join_date_{uid}"))
        txt = (f"<b>📊 My Statistics</b>\n\n"
               f"<b>📅 Since:</b> <code>{joined}</code>\n"
               f"<b>📡 Active Pairs:</b> <code>{len(pairs)}</code>\n"
               f"<b>🔄 Forwarded:</b> <code>{get_prop(f'fwd_{uid}', 0)} Files</code>\n"
               f"<b>▶️ Views:</b> <code>{get_prop(f'views_own_{uid}', 0)}</code>\n"
               f"<b>👥 Total Users:</b> <code>{len(viewers)}</code>")
        await send_msg(client, uid, txt, back_kb(uid), True, msg)
        return

    if d == "own_stats" and uid == OWNER_ID:
        log_ch = get_prop("admin_log_chan", "Unassigned")
        txt = f"<b>📊 Global Analytics</b>\n\n<b>📝 Log Channel:</b> <code>{log_ch}</code>\n\n"
        for i, aid in enumerate(get_admins()):
            pairs = get_prop(f"channels_{aid}", [])
            if pairs and is_admin(aid):
                viewers = get_prop(f"viewers_{aid}", [])
                txt += (f"<b>{i+1}. ID:</b> <code>{aid}</code>\n"
                        f"<b>📡 Pairs:</b> <code>{len(pairs)}</code> | "
                        f"<b>🔄 Fwd:</b> <code>{get_prop(f'fwd_{aid}', 0)}</code> | "
                        f"<b>👥 Users:</b> <code>{len(viewers)}</code>\n\n")
        await send_msg(client, uid, txt,
            InlineKeyboardMarkup([[btn("🔙 Return", "owner_main")]]), True, msg)
        return

    if d == "own_bc" and uid == OWNER_ID:
        set_state(uid, "wait_bc")
        await send_msg(client, uid,
            "<b>📣 Broadcast</b>\n\n<blockquote><b>Send the message to broadcast to all users:</b></blockquote>",
            InlineKeyboardMarkup([[btn("❌ Cancel", "owner_main")]]), True, msg)
        return

    if d == "own_set_log" and uid == OWNER_ID:
        set_state(uid, "wait_log_chan")
        await send_msg(client, uid,
            "<b>📝 Set Log Channel</b>\n\n"
            "<blockquote><b>Forward any post from the Log channel here "
            "(recommended), or send its Channel ID (starting with -100).</b></blockquote>",
            InlineKeyboardMarkup([[btn("❌ Cancel", "owner_main")]]), True, msg)
        return

async def _send_user_stats(client, from_uid, target_uid, is_cb=False, cb_msg=None):
    joined = format_date(get_prop(f"join_date_{target_uid}"))
    via = get_prop(f"joined_via_{target_uid}", "Direct")
    views = get_prop(f"views_by_{target_uid}", 0)
    limit = get_prop(f"daily_limit_{target_uid}")
    limit_txt = "Unlimited" if (limit is None or limit == "unlimited" or limit == 0) else str(limit)
    count = get_prop(f"daily_count_{target_uid}", 0)
    today = datetime.now().strftime("%Y-%m-%d")
    if get_prop(f"daily_date_{target_uid}") != today:
        count = 0

    txt = (f"<b>👤 User Analytics</b>\n\n"
           f"<b>🆔 ID:</b> <code>{target_uid}</code>\n"
           f"<b>📅 Joined:</b> <code>{joined}</code>\n"
           f"<b>🔗 Via Admin:</b> <code>{via}</code>\n"
           f"<b>▶️ Views:</b> <code>{views}</code>\n"
           f"<b>📈 Daily Quota:</b> <code>{count} / {limit_txt}</code>\n"
           f"<b>🚫 Status:</b> <code>{'Banned' if is_banned(target_uid) else 'Active'}</code>\n\n"
           f"<blockquote><b>Choose an action below:</b></blockquote>")
    await send_msg(client, from_uid, txt, user_manage_kb(target_uid), is_cb, cb_msg)

# ---------------------------------------------------------------------------
# Message Handler (state machine)
# ---------------------------------------------------------------------------
@app.on_message(filters.private & ~filters.command(["start"]))
async def msg_handler(client: Client, message: Message):
    uid = message.from_user.id
    text = (message.text or "").strip()
    st, temp = get_state(uid)

    if not is_admin(uid):
        return

    async def cleanup():
        try:
            await message.delete()
        except Exception:
            pass

    if st == "wait_target":
        await cleanup()
        target_id = extract_channel_id(message, text)
        t_name, t_link = await resolve_channel_info(client, target_id)
        if t_name is None:
            await send_msg(client, uid,
                f"<b>⚠️ Couldn't verify that channel (<code>{target_id}</code>).</b>\n\n"
                f"<blockquote><b>Make sure the bot is added as admin there, then "
                f"forward a post from that channel here instead of typing the ID.</b></blockquote>",
                InlineKeyboardMarkup([[btn("❌ Cancel", "config_menu")]]))
            return
        set_state(uid, "wait_source", json.dumps({"id": target_id, "name": t_name, "link": t_link}))
        target_display = format_channel_display(target_id, t_name, t_link)
        await send_msg(client, uid,
            f"<b>📣 Now Send Source Channel</b>\n\n"
            f"<blockquote><b>Binding to Target: {target_display}</b>\n\n"
            f"Forward a post from the Source channel (recommended), or send its ID.</blockquote>",
            InlineKeyboardMarkup([[btn("❌ Abort", "config_menu")]]))
        return

    if st == "wait_source":
        await cleanup()
        try:
            target_info = json.loads(temp)
        except Exception:
            target_info = {"id": temp.strip(), "name": None, "link": None}
        source_id = extract_channel_id(message, text)
        s_name, s_link = await resolve_channel_info(client, source_id)
        if s_name is None:
            await send_msg(client, uid,
                f"<b>⚠️ Couldn't verify that channel (<code>{source_id}</code>).</b>\n\n"
                f"<blockquote><b>Make sure the bot is added as admin there, then "
                f"forward a post from that channel here instead of typing the ID.</b></blockquote>",
                InlineKeyboardMarkup([[btn("❌ Cancel", "config_menu")]]))
            return
        pairs = get_prop(f"channels_{uid}", [])
        pairs.append({
            "target": target_info["id"], "target_name": target_info.get("name"), "target_link": target_info.get("link"),
            "source": source_id, "source_name": s_name, "source_link": s_link,
        })
        set_prop(f"channels_{uid}", pairs)
        set_state(uid, "")
        await send_msg(client, uid, "<b>✅ Channels Successfully Linked!</b>",
            InlineKeyboardMarkup([[btn("⚙️ Back to Config", "config_menu")]]))
        return

    if st == "wait_admin_add" and uid == OWNER_ID:
        await cleanup()
        try:
            new_ad = int(text)
        except ValueError:
            set_state(uid, "")
            await send_msg(client, uid, "<b>⚠️ Invalid ID.</b>",
                InlineKeyboardMarkup([[btn("❌ Cancel", "own_admins_menu")]]))
            return
        admins = get_admins()
        if new_ad in admins and is_admin(new_ad):
            set_state(uid, "")
            await send_msg(client, uid, "<b>⚠️ Already an Admin.</b>",
                InlineKeyboardMarkup([[btn("❌ Cancel", "own_admins_menu")]]))
            return
        set_state(uid, f"wait_admin_duration_{new_ad}", str(new_ad))
        await send_msg(client, uid,
            "<b>⏳ Set Duration (Step 2/2)</b>\n\n"
            "<blockquote><b>Enter duration e.g. 30d, 365d:</b></blockquote>",
            InlineKeyboardMarkup([[btn("❌ Abort", "own_admins_menu")]]))
        return

    if st.startswith("wait_admin_duration_") and uid == OWNER_ID:
        await cleanup()
        new_ad = int(st.split("_")[3])
        days_str = text.lower().replace("d", "").strip()
        try:
            days = int(days_str)
            assert days > 0
        except Exception:
            set_state(uid, "")
            await send_msg(client, uid, "<b>⚠️ Invalid format. Use e.g. 30d</b>",
                InlineKeyboardMarkup([[btn("🔙 Return", "own_admins_menu")]]))
            return
        end_time = int((time.time() + days * 86400) * 1000)
        set_prop(f"admin_plan_{new_ad}", {"end_time": end_time, "days": days})
        admins = get_admins()
        if new_ad not in admins:
            admins.append(new_ad)
            set_prop("admins_list", admins)
        set_state(uid, "")
        await send_msg(client, uid,
            f"<b>✅ Admin Added for {days} Days!</b>",
            InlineKeyboardMarkup([[btn("✔️ Done", "own_admins_menu")]]))
        return

    if st == "wait_admin_del" and uid == OWNER_ID:
        await cleanup()
        try:
            del_ad = int(text)
        except ValueError:
            set_state(uid, "")
            await send_msg(client, uid, "<b>⚠️ Invalid ID.</b>",
                InlineKeyboardMarkup([[btn("🔙 Return", "own_admins_menu")]]))
            return
        if del_ad == OWNER_ID:
            await send_msg(client, uid, "<b>⚠️ Cannot remove Owner.</b>",
                InlineKeyboardMarkup([[btn("🔙 Return", "own_admins_menu")]]))
            return
        admins = [a for a in get_admins() if a != del_ad]
        set_prop("admins_list", admins)
        set_prop(f"admin_plan_{del_ad}", {"end_time": 0})
        set_state(uid, "")
        await send_msg(client, uid, "<b>✅ Admin Revoked.</b>",
            InlineKeyboardMarkup([[btn("✔️ Done", "own_admins_menu")]]))
        return

    if st == "wait_manage_admin" and uid == OWNER_ID:
        await cleanup()
        try:
            aid = int(text)
        except ValueError:
            set_state(uid, "")
            await send_msg(client, uid, "<b>⚠️ Invalid ID.</b>",
                InlineKeyboardMarkup([[btn("🔙 Return", "own_admins_menu")]]))
            return
        set_state(uid, f"wait_admin_limit_{aid}")
        pairs = get_prop(f"channels_{aid}", [])
        viewers = get_prop(f"viewers_{aid}", [])
        ch_limit = get_prop(f"chan_limit_{aid}", 0)
        txt = (f"<b>📊 Admin Details</b>\n\n"
               f"<b>🆔 ID:</b> <code>{aid}</code>\n"
               f"<b>📡 Pairs:</b> <code>{len(pairs)}/{ch_limit}</code>\n"
               f"<b>🔄 Forwarded:</b> <code>{get_prop(f'fwd_{aid}', 0)}</code>\n"
               f"<b>👥 Users:</b> <code>{len(viewers)}</code>\n\n"
               f"<blockquote><b>Send a number to set their max channel pairs limit:</b></blockquote>")
        await send_msg(client, uid, txt,
            InlineKeyboardMarkup([[btn("❌ Abort", "own_admins_menu")]]))
        return

    if st.startswith("wait_admin_limit_") and uid == OWNER_ID:
        await cleanup()
        aid = st.split("_")[3]
        try:
            lim = int(text)
            assert lim >= 0
        except Exception:
            set_state(uid, "")
            await send_msg(client, uid, "<b>⚠️ Invalid number.</b>",
                InlineKeyboardMarkup([[btn("🔙 Return", "own_admins_menu")]]))
            return
        set_prop(f"chan_limit_{aid}", lim)
        set_state(uid, "")
        await send_msg(client, uid,
            f"<b>✅ Channel limit set to {lim} for admin <code>{aid}</code>.</b>",
            InlineKeyboardMarkup([[btn("✔️ Done", "own_admins_menu")]]))
        return

    if st == "wait_log_chan" and uid == OWNER_ID:
        await cleanup()
        log_id = extract_channel_id(message, text)
        name, _ = await resolve_channel_info(client, log_id)
        if name is None:
            await send_msg(client, uid,
                f"<b>⚠️ Couldn't verify that channel (<code>{log_id}</code>).</b>\n\n"
                f"<blockquote><b>Make sure the bot is added as admin there, then "
                f"forward a post from that channel here instead of typing the ID.</b></blockquote>",
                InlineKeyboardMarkup([[btn("❌ Cancel", "owner_main")]]))
            return
        set_prop("admin_log_chan", log_id)
        set_state(uid, "")
        await send_msg(client, uid,
            f"<b>✅ Log Channel Set: {name} (<code>{log_id}</code>)</b>",
            InlineKeyboardMarkup([[btn("🔙 Back", "owner_main")]]))
        return

    if st.startswith("wait_limit_"):
        await cleanup()
        target_id = st.split("_")[2]
        try:
            limit = int(text)
            assert limit >= 0
        except Exception:
            await send_msg(client, uid, "<b>⚠️ Enter a valid number.</b>", user_manage_kb(int(target_id)))
            return
        if limit == 0:
            set_prop(f"daily_limit_{target_id}", "unlimited")
            await send_msg(client, uid,
                f"<b>✅ Limit set to Unlimited for <code>{target_id}</code>.</b>",
                user_manage_kb(int(target_id)))
        else:
            set_prop(f"daily_limit_{target_id}", limit)
            await send_msg(client, uid,
                f"<b>✅ Daily limit set to {limit} for <code>{target_id}</code>.</b>",
                user_manage_kb(int(target_id)))
        set_state(uid, "")
        return

    if st == "wait_manage_uid":
        await cleanup()
        try:
            target_id = int(text)
        except ValueError:
            await send_msg(client, uid, "<b>⚠️ Invalid ID.</b>", back_kb(uid))
            return
        set_state(uid, "")
        await _send_user_stats(client, uid, target_id)
        return

    if st.startswith("wait_pm_"):
        target_id = st.split("_")[2]
        try:
            await client.copy_message(int(target_id), uid, message.id)
            await cleanup()
            await send_msg(client, uid, "<b>✅ Message Delivered.</b>", user_manage_kb(int(target_id)))
        except Exception:
            await send_msg(client, uid, "<b>❌ Could not deliver — user may have blocked the bot.</b>",
                user_manage_kb(int(target_id)))
        set_state(uid, "")
        return

    if st == "wait_bc" and uid == OWNER_ID:
        set_state(uid, "")
        bc_msg_id = message.id
        mid = await send_msg(client, uid, "<b>⏳ Broadcasting...</b>")
        users_all = get_all_user_ids()
        sent = failed = 0
        for u in users_all:
            try:
                await client.copy_message(u, uid, bc_msg_id)
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.035)
        await cleanup()
        try:
            await client.edit_message_text(uid, mid,
                apply_font(f"<b>📣 Broadcast Done</b>\n\n"
                           f"<b>✅ Sent:</b> <code>{sent}</code>\n"
                           f"<b>❌ Failed:</b> <code>{failed}</code>"),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[btn("✔️ Done", "owner_main")]]))
        except Exception:
            pass
        return

# ---------------------------------------------------------------------------
# Group Topics (Forum) Support
# ---------------------------------------------------------------------------
# In-memory cache of "is this chat a forum/topics-enabled group?" — avoids
# an extra get_chat() call on every single post. A group's forum status
# essentially never flips mid-campaign, so caching for the process
# lifetime is safe; worst case a restart re-checks it once.
_forum_status_cache: dict[str, bool] = {}

async def is_forum_group(client: Client, chat_id_str: str) -> bool:
    if chat_id_str in _forum_status_cache:
        return _forum_status_cache[chat_id_str]
    is_forum = False
    try:
        chat = await client.get_chat(int(chat_id_str))
        is_forum = bool(getattr(chat, "is_forum", False))
    except Exception as e:
        logger.warning(f"Could not determine forum status for {chat_id_str}: {e}")
    _forum_status_cache[chat_id_str] = is_forum
    return is_forum

async def get_or_create_topic_thread(client: Client, group_id: int, topic_name: str):
    """Returns the message_thread_id to post into for a given topic name
    inside a forum group. Backed by a PERMANENT per-group list stored in
    Mongo: [{norm, thread_id, display_name}, ...].

    Why this is the permanent fix for topic confusion:
    - Lookup key is normalize_topic(topic_name) — not the raw caption
      text, not "was this the last topic sent" — so it doesn't matter if
      videos of the same topic are consecutive or interleaved with other
      topics, or if the caption has minor case/spacing differences.
    - Once a topic's thread_id is recorded, it is NEVER re-created or
      overwritten — the same normalized topic name will always resolve
      to the exact same Telegram forum topic, for as long as the bot runs.
    - A brand new normalized name (one never seen before for this group)
      is the ONLY thing that creates a new forum topic.
    """
    norm = normalize_topic(topic_name)
    display = (topic_name or "Uncategorized").strip() or "Uncategorized"
    key = f"group_topics_{group_id}"
    topics_list = get_prop(key, [])

    for t in topics_list:
        if t.get("norm") == norm:
            return t.get("thread_id")

    try:
        topic = await client.create_forum_topic(group_id, display)
    except FloodWait as e:
        await asyncio.sleep(e.value)
        topic = await client.create_forum_topic(group_id, display)
    except Exception as e:
        logger.error(f"Failed to create forum topic '{display}' in {group_id}: {e}")
        return None

    topics_list.append({"norm": norm, "thread_id": topic.id, "display_name": display})
    set_prop(key, topics_list)
    return topic.id

# ---------------------------------------------------------------------------
# Channel Post Handler (the core protection engine)
# ---------------------------------------------------------------------------
# All incoming channel posts are pushed onto this queue the instant they
# arrive (no awaiting before the push), so enqueue order == arrival order.
# A single dedicated worker (post_worker) drains this queue one item at a
# time, so outgoing "Watch Video" buttons are always sent in the exact same
# order the media was uploaded — regardless of how many other handlers
# (start, callbacks, admin panel) are running concurrently.
post_queue: asyncio.Queue = asyncio.Queue()

@app.on_message(filters.incoming & ~filters.private & ~filters.group)
async def channel_post_handler(client: Client, message: Message):
    media_type = file_id = ""
    if message.video:
        media_type, file_id = "video", message.video.file_id
    elif message.document:
        media_type, file_id = "document", message.document.file_id
    elif message.photo:
        media_type, file_id = "photo", message.photo.file_id

    if not media_type:
        return

    post_queue.put_nowait((message, media_type, file_id))


async def post_worker(client: Client):
    while True:
        message, media_type, file_id = await post_queue.get()
        try:
            await process_channel_post(client, message, media_type, file_id)
        except Exception:
            logger.exception("Error while processing queued channel post")
        finally:
            post_queue.task_done()


async def process_channel_post(client: Client, message: Message, media_type: str, file_id: str):
    post_chat_id = str(message.chat.id).strip()
    raw_caption = message.caption or ""

    logger.info(f"Channel post received from chat_id={post_chat_id} type={media_type}")

    # pymongo is a BLOCKING driver — every get_prop/set_prop call does a real
    # network round-trip to Atlas and freezes the entire asyncio event loop
    # while it waits. During a 100-video upload that means every other
    # handler (button clicks, /start) is frozen too. Running all the DB
    # work for this post in a worker thread keeps the event loop free so
    # buttons/commands keep responding while uploads are being processed.
    def _prepare_targets():
        targets = []
        sent_to_sources = []
        admins = get_admins()
        log_chan = get_prop("admin_log_chan")
        for aid in admins:
            if not is_admin(aid):
                continue
            pairs = get_prop(f"channels_{aid}", [])
            for pair in pairs:
                mapped_target = str(pair["target"]).strip()
                mapped_source = str(pair["source"]).strip()
                if mapped_target != post_chat_id or mapped_source in sent_to_sources:
                    continue
                sent_to_sources.append(mapped_source)

                unique_id = gen_uid(aid)
                set_prop(f"m_{unique_id}", {
                    "file_id": file_id,
                    "caption": raw_caption,
                    "type": media_type,
                    "admin_uid": aid,
                })
                set_prop(f"fwd_{aid}", get_prop(f"fwd_{aid}", 0) + 1)

                # Topic index: remember this video's topic (parsed from a
                # 'Topic : X' or 'Topic : X → Y' line in the caption) under
                # its target channel, so admins can later generate a
                # clickable table-of-contents for a lecture set.
                group, topic = extract_topic(raw_caption)
                idx_list = get_prop(f"index_{mapped_target}", [])
                idx_list.append({
                    "uid": unique_id,
                    "group": group,
                    "topic": topic,
                    "msg_id": message.id,
                    "chat_id": message.chat.id,
                    "chat_username": message.chat.username,
                })
                set_prop(f"index_{mapped_target}", idx_list)

                btn_text = "▶️ Watch Video" if media_type == "video" else (
                    "🖼️ View Photo" if media_type == "photo" else "📄 Download Document")
                routing_topic = extract_topic_for_routing(raw_caption)
                targets.append((mapped_target, mapped_source, unique_id, btn_text, routing_topic))
        return targets, log_chan

    targets, log_chan = await asyncio.to_thread(_prepare_targets)
    matched_any = bool(targets)
    send_text = apply_font(raw_caption) if raw_caption else "‎"

    for mapped_target, mapped_source, unique_id, btn_text, routing_topic in targets:
        inline_btn = InlineKeyboardMarkup([[btn(btn_text, f"vid_{unique_id}")]])

        thread_id = None
        if await is_forum_group(client, mapped_source):
            thread_id = await get_or_create_topic_thread(client, int(mapped_source), routing_topic)

        for attempt in range(3):
            try:
                await client.send_message(
                    int(mapped_source), send_text,
                    parse_mode=ParseMode.HTML, reply_markup=inline_btn,
                    message_thread_id=thread_id
                )
                break
            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception as e:
                logger.error(f"Failed to send button to source={mapped_source} "
                             f"(target={mapped_target}, thread={thread_id}): {e}")
                if attempt < 2:
                    await asyncio.sleep(2)

    if not matched_any:
        logger.warning(f"No pair matched target={post_chat_id}. "
                        f"Check your linked Target channel ID matches this exactly.")

    if log_chan and log_chan != "Unassigned":
        for attempt in range(5):
            try:
                await client.copy_message(int(log_chan), message.chat.id, message.id)
                break
            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception as e:
                logger.error(f"Log channel copy failed for log_chan={log_chan}: {e}")
                break

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
from pyrogram import idle

async def warm_peer_cache(client: Client):
    """Pyrogram/Telegram can't send to a channel it hasn't received any
    update from yet in this session ('Peer id invalid'). Channels that
    only ever RECEIVE posts from the bot (source channels, the log
    channel) never trigger that caching on their own. Touch every
    configured channel once at startup with get_chat() so they're all
    resolved before any real post comes in."""
    chat_ids = set()
    for aid in get_admins():
        for pair in get_prop(f"channels_{aid}", []):
            chat_ids.add(str(pair.get("target", "")).strip())
            chat_ids.add(str(pair.get("source", "")).strip())
    log_chan = get_prop("admin_log_chan")
    if log_chan and log_chan != "Unassigned":
        chat_ids.add(str(log_chan).strip())

    for cid in chat_ids:
        if not cid:
            continue
        try:
            await client.get_chat(int(cid))
        except Exception as e:
            logger.warning(f"Could not pre-warm peer cache for {cid}: {e}")

async def main():
    await app.start()
    await warm_peer_cache(app)
    asyncio.create_task(post_worker(app))
    logger.info("Protection Bot starting...")
    await idle()
    await app.stop()

if __name__ == "__main__":
    db_init()
    app.run(main())
