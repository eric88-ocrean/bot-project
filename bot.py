# FULL UPGRADED BOT V3 - STABLE FIXED VERSION
# Fixes:
# - Prevent buttons from "no response" by global callback error handling
# - Auto-create missing users on any button click
# - Remove unnecessary sleeps that caused callback lag
# - Safer Telegram message edit/reply fallback
# - Safer PostgreSQL connection handling
# - Prevent duplicate gift claim requests
# - Prevent duplicate pending redeem requests for same user/reward
# - Prevent redeem approval from being processed twice
# - Prevent points from going negative
# - Daily reward protection with Malaysia timezone
# - Better admin permission checks
# - Better environment/startup validation

import os
import random
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg2
from psycopg2.extras import RealDictCursor

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.error import BadRequest, TimedOut, NetworkError, RetryAfter
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)


# ================= CONFIG =================

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

BOT_USERNAME = os.getenv("BOT_USERNAME", "JomJudi_bot")

# Put admin IDs as strings.
ADMIN_IDS = {"909399622"}

CHANNEL_ID = "@jomjudi88cuci"
GROUP_ID = "@jomjudi88official"

CHANNEL_URL = "https://t.me/jomjudi88cuci"
GROUP_URL = "https://t.me/jomjudi88official"

REGISTER_URL = "https://jomjudi88.live/my/register/?referral=JJ27817922"
AMOI_MANJA_URL = "https://t.me/JomJManja_bot"
SUPPORT_URL = "https://t.me/JomJudi88vip"

TZ = ZoneInfo("Asia/Kuala_Lumpur")


# ================= LOGGING =================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("jomjudi88-bot")


# ================= HELPERS =================

def today_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def is_admin(user_id) -> bool:
    return str(user_id) in ADMIN_IDS


def safe_int(value, default=0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def random_reward(pool):
    """
    pool example: [(0, 40), (1, 60)]
    """
    rewards = [item[0] for item in pool]
    weights = [item[1] for item in pool]
    return random.choices(rewards, weights=weights, k=1)[0]


# ================= DB =================

def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing.")
    return psycopg2.connect(DATABASE_URL, connect_timeout=10)


def db_fetchone(query, params=None):
    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params or ())
            return cur.fetchone()
    finally:
        if conn:
            conn.close()


def db_fetchall(query, params=None):
    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params or ())
            return cur.fetchall()
    finally:
        if conn:
            conn.close()


def db_execute(query, params=None):
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(query, params or ())
        conn.commit()
    finally:
        if conn:
            conn.close()


def db_execute_returning_id(query, params=None):
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(query, params or ())
            result = cur.fetchone()
        conn.commit()
        return result[0] if result else None
    finally:
        if conn:
            conn.close()


def init_db():
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    name TEXT,
                    points INTEGER DEFAULT 0,
                    invited_count INTEGER DEFAULT 0,
                    spin_chances INTEGER DEFAULT 0,
                    gift_claimed INTEGER DEFAULT 0,
                    referrer_id TEXT,
                    mission_claimed INTEGER DEFAULT 0,
                    last_lucky_claim TEXT DEFAULT '',
                    last_vip_claim TEXT DEFAULT '',
                    last_elite_claim TEXT DEFAULT ''
                )
            """)

            # Auto-upgrade old database safely.
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS points INTEGER DEFAULT 0")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS invited_count INTEGER DEFAULT 0")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS spin_chances INTEGER DEFAULT 0")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS gift_claimed INTEGER DEFAULT 0")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referrer_id TEXT")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS mission_claimed INTEGER DEFAULT 0")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_lucky_claim TEXT DEFAULT ''")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_vip_claim TEXT DEFAULT ''")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_elite_claim TEXT DEFAULT ''")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS redeem_requests (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT,
                    username TEXT,
                    reward_text TEXT,
                    points_needed INTEGER,
                    status TEXT DEFAULT 'pending',
                    created_at TEXT DEFAULT ''
                )
            """)

            cur.execute("ALTER TABLE redeem_requests ADD COLUMN IF NOT EXISTS created_at TEXT DEFAULT ''")

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_redeem_user_status
                ON redeem_requests(user_id, status)
            """)

        conn.commit()
        logger.info("Database initialized.")
    finally:
        if conn:
            conn.close()


def get_user(user_id):
    return db_fetchone("SELECT * FROM users WHERE user_id=%s", (str(user_id),))


def create_user(user_id, name, referrer_id=None):
    db_execute("""
        INSERT INTO users
        (
            user_id, name, points, invited_count,
            spin_chances, gift_claimed, referrer_id, mission_claimed,
            last_lucky_claim, last_vip_claim, last_elite_claim
        )
        VALUES (%s,%s,0,0,0,0,%s,0,'','','')
        ON CONFLICT (user_id) DO UPDATE SET
            name = EXCLUDED.name
    """, (str(user_id), name or "User", referrer_id))


def ensure_user(user_id, name="User"):
    user = get_user(user_id)
    if user:
        return user

    create_user(str(user_id), name or "User")
    user = get_user(user_id)

    if not user:
        raise RuntimeError(f"Could not create/fetch user {user_id}")

    return user


def add_points(user_id, amount):
    amount = safe_int(amount)
    db_execute("""
        UPDATE users
        SET points = GREATEST(points + %s, 0)
        WHERE user_id=%s
    """, (amount, str(user_id)))


def deduct_points(user_id, amount) -> bool:
    amount = safe_int(amount)
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE users
                SET points = points - %s
                WHERE user_id=%s AND points >= %s
                RETURNING user_id
            """, (amount, str(user_id), amount))
            row = cur.fetchone()
        conn.commit()
        return row is not None
    finally:
        if conn:
            conn.close()


def add_invite(referrer_id):
    db_execute("""
        UPDATE users
        SET invited_count = invited_count + 1,
            points = points + 1
        WHERE user_id=%s
    """, (str(referrer_id),))


def update_claim(user_id, claim_type):
    today = today_str()

    column_map = {
        "lucky": "last_lucky_claim",
        "vip": "last_vip_claim",
        "elite": "last_elite_claim",
    }

    column = column_map.get(claim_type)
    if not column:
        raise ValueError("Invalid claim type.")

    db_execute(
        f"UPDATE users SET {column}=%s WHERE user_id=%s",
        (today, str(user_id))
    )


def has_claimed_any_reward_today(user):
    today = today_str()
    return (
        user.get("last_lucky_claim") == today or
        user.get("last_vip_claim") == today or
        user.get("last_elite_claim") == today
    )


def mark_gift_claimed(user_id):
    db_execute("UPDATE users SET gift_claimed=1 WHERE user_id=%s", (str(user_id),))


def mark_mission_claimed(user_id):
    db_execute("UPDATE users SET mission_claimed=1 WHERE user_id=%s", (str(user_id),))


def create_redeem_request(user_id, username, reward_text, points_needed):
    return db_execute_returning_id("""
        INSERT INTO redeem_requests
        (user_id, username, reward_text, points_needed, status, created_at)
        VALUES (%s,%s,%s,%s,'pending',%s)
        RETURNING id
    """, (str(user_id), username, reward_text, int(points_needed), datetime.now(TZ).isoformat()))


def get_redeem_request(request_id):
    return db_fetchone("""
        SELECT *
        FROM redeem_requests
        WHERE id=%s
    """, (int(request_id),))


def has_pending_redeem(user_id, reward_text=None):
    if reward_text:
        row = db_fetchone("""
            SELECT id FROM redeem_requests
            WHERE user_id=%s AND reward_text=%s AND status='pending'
            LIMIT 1
        """, (str(user_id), reward_text))
    else:
        row = db_fetchone("""
            SELECT id FROM redeem_requests
            WHERE user_id=%s AND status='pending'
            LIMIT 1
        """, (str(user_id),))
    return row


def update_redeem_status(request_id, status):
    db_execute("""
        UPDATE redeem_requests
        SET status=%s
        WHERE id=%s
    """, (status, int(request_id)))


def approve_redeem_request(request_id) -> tuple[bool, str]:
    """
    Returns: (success, message)
    """
    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT *
                FROM redeem_requests
                WHERE id=%s
                FOR UPDATE
            """, (int(request_id),))
            req = cur.fetchone()

            if not req:
                conn.rollback()
                return False, "❌ Redeem request not found."

            if req["status"] != "pending":
                conn.rollback()
                return False, f"⚠️ This request was already {req['status']}."

            cur.execute("""
                UPDATE users
                SET points = points - %s
                WHERE user_id=%s AND points >= %s
                RETURNING points
            """, (req["points_needed"], req["user_id"], req["points_needed"]))
            updated_user = cur.fetchone()

            if not updated_user:
                conn.rollback()
                return False, "❌ User does not have enough points now. Approval cancelled."

            cur.execute("""
                UPDATE redeem_requests
                SET status='approved'
                WHERE id=%s
            """, (int(request_id),))

        conn.commit()
        return True, "✅ Redeem Approved."
    except Exception as e:
        if conn:
            conn.rollback()
        logger.exception("approve_redeem_request error: %s", e)
        return False, "⚠️ Approval failed. Please check logs."
    finally:
        if conn:
            conn.close()


def reject_redeem_request(request_id) -> tuple[bool, str]:
    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT *
                FROM redeem_requests
                WHERE id=%s
                FOR UPDATE
            """, (int(request_id),))
            req = cur.fetchone()

            if not req:
                conn.rollback()
                return False, "❌ Redeem request not found."

            if req["status"] != "pending":
                conn.rollback()
                return False, f"⚠️ This request was already {req['status']}."

            cur.execute("""
                UPDATE redeem_requests
                SET status='rejected'
                WHERE id=%s
            """, (int(request_id),))

        conn.commit()
        return True, "❌ Redeem Rejected."
    except Exception as e:
        if conn:
            conn.rollback()
        logger.exception("reject_redeem_request error: %s", e)
        return False, "⚠️ Reject failed. Please check logs."
    finally:
        if conn:
            conn.close()


def get_top_invites():
    return db_fetchall("""
        SELECT name, points, invited_count
        FROM users
        ORDER BY invited_count DESC, points DESC
        LIMIT 10
    """)


def get_all_users():
    return db_fetchall("""
        SELECT user_id, name, points, invited_count
        FROM users
        ORDER BY invited_count DESC, points DESC
    """)


# ================= UI =================

def get_main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔐 Daftar Akaun", url=REGISTER_URL),
            InlineKeyboardButton("💰 Earn Rewards", callback_data="menu"),
        ],
        [
            InlineKeyboardButton("🎁 New Join Free RM38", callback_data="gift"),
            InlineKeyboardButton("🎁 Daily Check In", callback_data="reward_center"),
        ],
        [
            InlineKeyboardButton("📢 Join Channel", url=CHANNEL_URL),
            InlineKeyboardButton("👥 Join Group", url=GROUP_URL),
        ],
        [
            InlineKeyboardButton("🔞 Amoi Manja", url=AMOI_MANJA_URL),
            InlineKeyboardButton("🎧 Support", callback_data="support"),
        ],
    ])


def get_main_text():
    return (
        "🎁 Welcome to JomJudi88 Bot Rewards 🔥\n\n"
        "🚀 Sistem Reward & Bonus untuk player Malaysia 🇲🇾\n\n"
        "💸 Main sambil collect reward setiap hari!\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "✅ Invite & unlock VIP rewards\n"
        "✅ Claim points & redeem hadiah\n"
        "✅ Touch 'n Go RM100\n"
        "✅ Reward update setiap hari\n\n"
        "━━━━━━━━━━━━━━\n\n"
        "🧠 Sistem Auto Layan Diri\n"
        "✔️ Deposit & withdraw auto\n"
        "✔️ Support 24/7\n"
        "✔️ Privasi terjamin 🔐\n\n"
        "👇 Pilih menu di bawah untuk mula"
    )


def back_to_menu_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu")]])


def back_to_home_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back")]])


async def safe_reply(query, text, reply_markup=None):
    try:
        await query.message.reply_text(text=text, reply_markup=reply_markup)
    except Exception as e:
        logger.exception("safe_reply error: %s", e)


async def safe_edit(query, text, reply_markup=None):
    """
    Never lets a callback silently die.
    If editing fails, replies with a new message.
    """
    try:
        msg = query.message

        if msg and msg.photo:
            try:
                await query.edit_message_caption(
                    caption=text[:1024],
                    reply_markup=reply_markup
                )
                return
            except BadRequest as e:
                # If old message is photo but new text is long, reply as text instead.
                logger.warning("edit caption failed, fallback reply_text: %s", e)

        try:
            await query.edit_message_text(
                text=text,
                reply_markup=reply_markup
            )
            return
        except BadRequest as e:
            message = str(e).lower()
            if "message is not modified" in message:
                return
            logger.warning("edit text failed, fallback reply_text: %s", e)

        await safe_reply(query, text, reply_markup=reply_markup)

    except RetryAfter as e:
        logger.warning("Telegram rate limit: retry after %s", e.retry_after)
        await safe_reply(query, "⚠️ Too many requests. Please try again in a few seconds.", reply_markup)
    except (TimedOut, NetworkError) as e:
        logger.warning("Telegram network error: %s", e)
        try:
            await safe_reply(query, "⚠️ Network busy. Please press again.", reply_markup)
        except Exception:
            pass
    except Exception as e:
        logger.exception("SAFE_EDIT_ERROR: %s", e)
        try:
            await safe_reply(query, text, reply_markup=reply_markup)
        except Exception:
            pass


# ================= START =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.effective_user
        user_id = str(user.id)
        user_name = user.first_name or "User"

        referrer_id = None
        if context.args:
            referrer_id = str(context.args[0]).strip()

        existing = get_user(user_id)

        if not existing:
            valid_referrer = None

            if referrer_id and referrer_id != user_id:
                ref_user = get_user(referrer_id)
                if ref_user:
                    valid_referrer = referrer_id

            create_user(user_id, user_name, valid_referrer)

            # Only first-time user can add invite.
            if valid_referrer:
                add_invite(valid_referrer)
        else:
            # Keep display name updated.
            create_user(user_id, user_name, existing.get("referrer_id"))

        try:
            if os.path.exists("banner.jpg"):
                with open("banner.jpg", "rb") as photo:
                    await context.bot.send_photo(
                        chat_id=update.effective_chat.id,
                        photo=photo,
                        caption=get_main_text(),
                        reply_markup=get_main_keyboard()
                    )
            else:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=get_main_text(),
                    reply_markup=get_main_keyboard()
                )
        except Exception as e:
            logger.warning("Banner/send_photo error, fallback send_message: %s", e)
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=get_main_text(),
                reply_markup=get_main_keyboard()
            )

    except Exception as e:
        logger.exception("START ERROR: %s", e)
        try:
            await update.effective_message.reply_text(
                "⚠️ Bot temporarily busy. Please press /start again."
            )
        except Exception:
            pass


async def is_user_joined(chat_id, user_id, context) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, int(user_id))
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logger.warning("Join check failed for chat %s user %s: %s", chat_id, user_id, e)
        return False


# ================= CALLBACK BUTTONS =================

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    try:
        try:
            await query.answer(cache_time=0)
        except Exception as e:
            logger.warning("Callback answer error: %s", e)

        user_id = str(query.from_user.id)
        user_name = query.from_user.first_name or "User"
        data = query.data or ""

        # Important fix: user may press old inline button before /start finishes or DB row exists.
        user = ensure_user(user_id, user_name)

        # ================= MENU =================
        if data == "menu":
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("💎 Your Rewards", callback_data="profile")],
                [InlineKeyboardButton("💰 Share & Earn", callback_data="link")],
                [InlineKeyboardButton("🎯 Missions", callback_data="missions")],
                [InlineKeyboardButton("🎁 Claim Reward", callback_data="redeem_menu")],
                [InlineKeyboardButton("🔙 Back", callback_data="back")],
            ])

            await safe_edit(
                query,
                "💰 Rewards Center\n\n"
                "🎁 Complete missions\n"
                "🔥 Unlock VIP rewards\n"
                "💸 Collect points & claim hadiah setiap hari\n\n"
                "💰 Claim Touch'N Go FREE RM100\n\n"
                "Syarat untuk claim reward:\n\n"
                "• Mesti ada akaun berdaftar di JomJudi88\n"
                "• Share referral link ke Facebook / Telegram / kawan-kawan\n"
                "  (1 referral = 1 point)\n\n"
                "🎁 Ganjaran:\n\n"
                "• 3 Point = RM1 Kredit Game\n"
                "• 10 Point = RM5 Kredit Game\n"
                "• 20 Point = RM10 Kredit Game\n"
                "• 50 Point = RM25 Kredit Game\n"
                "• 100 Point = RM50 Kredit Game\n"
                "• 200 Point = Touch 'n Go RM100\n\n"
                "👇 Select an option below:",
                keyboard
            )

        elif data == "profile":
            user = get_user(user_id) or user
            keyboard = back_to_menu_keyboard()

            await safe_edit(
                query,
                f"💎 Your Rewards\n\n"
                f"⭐️ Reward Points: {user.get('points', 0)}\n"
                f"👥 Friends Referred: {user.get('invited_count', 0)}\n\n"
                f"----------------------------------------",
                keyboard
            )

        elif data == "link":
            link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
            keyboard = back_to_menu_keyboard()

            await safe_edit(
                query,
                f"💰 Share & Earn Lagi!\n\n"
                f"Jom ajak kawan join & collect reward sama-sama 🔥\n\n"
                f"🔗 Link Boss:\n\n{link}",
                keyboard
            )

        # ================= REWARD CENTER =================
        elif data == "reward_center":
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🎁 Lucky Reward", callback_data="lucky_reward")],
                [InlineKeyboardButton("🔥 VIP Reward", callback_data="vip_reward")],
                [InlineKeyboardButton("👑 Elite Reward", callback_data="elite_reward")],
                [InlineKeyboardButton("🔙 Back", callback_data="back")],
            ])

            reward_text = (
                "⏰ Reset setiap hari 12AM Malaysia time\n\n"
                "━━━━━━━━━━━━━━\n\n"
                "🎁 Lucky Reward\n"
                "🔓 Semua boleh claim\n\n"
                "⭐️ Random Points:\n"
                "+0 • +1\n\n"
                "━━━━━━━━━━━━━━\n\n"
                "🔥 VIP Reward\n"
                "🔒 Unlock 5 invites\n\n"
                "⭐️ Better Rewards:\n"
                "+0 • +1 • +3\n\n"
                "━━━━━━━━━━━━━━\n\n"
                "👑 Elite Reward\n"
                "🔒 Unlock 20 invites\n\n"
                "💎 Big Rewards:\n"
                "+0 • +1 • +5"
            )

            await safe_edit(query, reward_text, keyboard)

        # ================= DAILY REWARDS =================
        elif data == "lucky_reward":
            user = get_user(user_id) or user

            if not is_admin(user_id) and has_claimed_any_reward_today(user):
                await safe_edit(
                    query,
                    "❌ You already claimed today's reward.\n\n⏰ Please come back after 12AM Malaysia time.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="reward_center")]])
                )
                return

            reward = random_reward([(0, 40), (1, 60)])

            update_claim(user_id, "lucky")
            if reward > 0:
                add_points(user_id, reward)

            await safe_edit(
                query,
                (
                    f"🎉 Reward Berjaya Dibuka!\n\n⭐ +{reward} Points masuk 🔥"
                    if reward > 0 else
                    "😆 Belum kena reward kali ni\n\nCuba lagi esok 🔥"
                ),
                InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="reward_center")]])
            )

        elif data == "vip_reward":
            user = get_user(user_id) or user

            if not is_admin(user_id) and has_claimed_any_reward_today(user):
                await safe_edit(
                    query,
                    "❌ You already claimed today's reward.\n\n⏰ Please come back after 12AM Malaysia time.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="reward_center")]])
                )
                return

            invites = safe_int(user.get("invited_count", 0))

            if invites < 5 and not is_admin(user_id):
                await safe_edit(
                    query,
                    "🔒 VIP Reward unlocks at 5 invites.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="reward_center")]])
                )
                return

            reward = random_reward([(0, 30), (1, 65), (3, 5)])

            update_claim(user_id, "vip")
            if reward > 0:
                add_points(user_id, reward)

            await safe_edit(
                query,
                (
                    f"🎉 Reward Berjaya Dibuka!\n\n⭐ +{reward} Points masuk 🔥"
                    if reward > 0 else
                    "😆 Belum kena reward kali ni\n\nCuba lagi esok 🔥"
                ),
                InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="reward_center")]])
            )

        elif data == "elite_reward":
            user = get_user(user_id) or user

            if not is_admin(user_id) and has_claimed_any_reward_today(user):
                await safe_edit(
                    query,
                    "❌ You already claimed today's reward.\n\n⏰ Please come back after 12AM Malaysia time.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="reward_center")]])
                )
                return

            invites = safe_int(user.get("invited_count", 0))

            if invites < 20 and not is_admin(user_id):
                await safe_edit(
                    query,
                    "🔒 Elite Reward unlocks at 20 invites.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="reward_center")]])
                )
                return

            reward = random_reward([(0, 30), (1, 67), (5, 3)])

            update_claim(user_id, "elite")
            if reward > 0:
                add_points(user_id, reward)

            await safe_edit(
                query,
                (
                    f"🎉 Reward Berjaya Dibuka!\n\n⭐ +{reward} Points masuk 🔥"
                    if reward > 0 else
                    "😆 Belum kena reward kali ni\n\nCuba lagi esok 🔥"
                ),
                InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="reward_center")]])
            )

        # ================= MISSIONS =================
        elif data == "missions":
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📢 Join Channel", url=CHANNEL_URL)],
                [InlineKeyboardButton("👥 Join Group", url=GROUP_URL)],
                [InlineKeyboardButton("✅ Done Join", callback_data="check_missions")],
                [InlineKeyboardButton("🔙 Back", callback_data="menu")],
            ])

            await safe_edit(
                query,
                "🎯 Missions\n\n"
                "Jom complete mission & collect reward 🔥\n\n"
                "✅ Join Channel\n"
                "✅ Join Group\n"
                "🎁 Claim +2 Points",
                keyboard
            )

        elif data == "check_missions":
            user = get_user(user_id) or user

            if safe_int(user.get("mission_claimed", 0)) == 1 and not is_admin(user_id):
                await safe_edit(
                    query,
                    "✅ You already claimed mission rewards.",
                    back_to_menu_keyboard()
                )
                return

            joined_channel = await is_user_joined(CHANNEL_ID, user_id, context)
            joined_group = await is_user_joined(GROUP_ID, user_id, context)

            if joined_channel and joined_group:
                add_points(user_id, 2)
                mark_mission_claimed(user_id)

                await safe_edit(
                    query,
                    "🎉 Mission Completed!\n\n⭐ +2 Points Added",
                    back_to_menu_keyboard()
                )
            else:
                await safe_edit(
                    query,
                    "❌ Please join Channel & Group first.\n\n"
                    "⚠️ If you already joined but still cannot claim, make sure the bot is admin in the channel/group.",
                    back_to_menu_keyboard()
                )

        # ================= REDEEM =================
        elif data == "redeem_menu":
            user = get_user(user_id) or user
            points = safe_int(user.get("points", 0))

            keyboard_rows = []
            rewards = [
                ("RM1 Credit", 3),
                ("RM5 Credit", 10),
                ("RM10 Credit", 20),
                ("RM25 Credit", 50),
                ("RM50 Credit", 100),
                ("Touch 'n Go RM100", 200),
            ]

            for reward_text, pts in rewards:
                if points >= pts:
                    keyboard_rows.append([
                        InlineKeyboardButton(
                            f"{reward_text} ({pts} Points)",
                            callback_data=f"redeem:{pts}:{reward_text}"
                        )
                    ])

            if not keyboard_rows:
                keyboard_rows.append([
                    InlineKeyboardButton("❌ Not enough points yet", callback_data="not_enough_points")
                ])

            keyboard_rows.append([InlineKeyboardButton("🔙 Back", callback_data="menu")])

            await safe_edit(
                query,
                f"🎁 Claim Reward\n\n⭐ Your Points: {points}\n\n👇 Select available reward:",
                InlineKeyboardMarkup(keyboard_rows)
            )

        elif data == "not_enough_points":
            await safe_edit(
                query,
                "❌ Not enough points yet.\n\nInvite friends and complete missions to collect more points.",
                back_to_menu_keyboard()
            )

        elif data.startswith("redeem:"):
            user = get_user(user_id) or user

            try:
                _, pts, reward_text = data.split(":", 2)
                pts = int(pts)
            except Exception:
                await safe_edit(query, "⚠️ Invalid redeem request.", back_to_menu_keyboard())
                return

            current_points = safe_int(user.get("points", 0))
            if current_points < pts and not is_admin(user_id):
                await safe_edit(
                    query,
                    "❌ Not enough points for this reward.",
                    back_to_menu_keyboard()
                )
                return

            pending = has_pending_redeem(user_id, reward_text)
            if pending:
                await safe_edit(
                    query,
                    "⏳ You already have a pending request for this reward.\n\nPlease wait for admin approval.",
                    back_to_menu_keyboard()
                )
                return

            username = query.from_user.username
            username_text = f"@{username}" if username else "No Username"

            request_id = create_redeem_request(user_id, username_text, reward_text, pts)

            for admin in ADMIN_IDS:
                admin_keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Approve", callback_data=f"approve_redeem:{request_id}"),
                        InlineKeyboardButton("❌ Reject", callback_data=f"reject_redeem:{request_id}"),
                    ]
                ])

                try:
                    await context.bot.send_message(
                        chat_id=int(admin),
                        text=(
                            f"🎁 New Redeem Request\n\n"
                            f"👤 Username: {username_text}\n"
                            f"🆔 User ID: {user_id}\n"
                            f"🎁 Reward: {reward_text}\n"
                            f"⭐ Points Needed: {pts}\n"
                            f"⭐ User Current Points: {current_points}\n"
                            f"📝 Request ID: {request_id}"
                        ),
                        reply_markup=admin_keyboard
                    )
                except Exception as e:
                    logger.warning("Send redeem request to admin %s failed: %s", admin, e)

            await safe_edit(
                query,
                "⏳ Redeem request submitted.\n\nAdmin will review your request.",
                back_to_menu_keyboard()
            )

        elif data.startswith("approve_redeem:"):
            if not is_admin(user_id):
                await safe_edit(query, "❌ Admin only.")
                return

            request_id = int(data.split(":")[1])
            success, message = approve_redeem_request(request_id)
            await safe_edit(query, message)

        elif data.startswith("reject_redeem:"):
            if not is_admin(user_id):
                await safe_edit(query, "❌ Admin only.")
                return

            request_id = int(data.split(":")[1])
            success, message = reject_redeem_request(request_id)
            await safe_edit(query, message)

        # ================= GIFT =================
        elif data == "gift":
            user = get_user(user_id) or user

            if safe_int(user.get("gift_claimed", 0)) == 1 and not is_admin(user_id):
                await safe_edit(
                    query,
                    "✅ You already claimed the new join gift.",
                    back_to_home_keyboard()
                )
                return

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🎁 Claim Gift", callback_data="claim_gift")],
                [InlineKeyboardButton("🔙 Back", callback_data="back")],
            ])

            await safe_edit(
                query,
                "🎁 Hadiah Member Baru\n\n"
                "✅ Daftar akaun baru\n"
                "✅ Deposit pertama RM20+\n"
                "✅ Join Channel & Group dulu 😎\n\n"
                "🎁 Reward Free:\n"
                "RM38 Kredit Game 💸",
                keyboard
            )

        elif data == "claim_gift":
            user = get_user(user_id) or user

            if safe_int(user.get("gift_claimed", 0)) == 1 and not is_admin(user_id):
                await safe_edit(
                    query,
                    "✅ You already claimed the new join gift.",
                    back_to_home_keyboard()
                )
                return

            username = query.from_user.username
            username_text = f"@{username}" if username else "No Username"

            for admin in ADMIN_IDS:
                admin_keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Approve", callback_data=f"approve_gift:{user_id}"),
                        InlineKeyboardButton("❌ Reject", callback_data=f"reject_gift:{user_id}"),
                    ]
                ])

                try:
                    await context.bot.send_message(
                        chat_id=int(admin),
                        text=(
                            f"🎁 New Gift Request\n\n"
                            f"👤 Username: {username_text}\n"
                            f"🆔 User ID: {user_id}"
                        ),
                        reply_markup=admin_keyboard
                    )
                except Exception as e:
                    logger.warning("Send gift request to admin %s failed: %s", admin, e)

            await safe_edit(
                query,
                "⏳ Gift request submitted.\n\nAdmin will review your request.",
                back_to_home_keyboard()
            )

        elif data.startswith("approve_gift:"):
            if not is_admin(user_id):
                await safe_edit(query, "❌ Admin only.")
                return

            target = data.split(":")[1]
            target_user = get_user(target)

            if not target_user:
                await safe_edit(query, "❌ User not found.")
                return

            if safe_int(target_user.get("gift_claimed", 0)) == 1:
                await safe_edit(query, "⚠️ Gift was already approved before.")
                return

            add_points(target, 38)
            mark_gift_claimed(target)

            await safe_edit(query, "✅ Gift Approved. +38 Points added.")

        elif data.startswith("reject_gift:"):
            if not is_admin(user_id):
                await safe_edit(query, "❌ Admin only.")
                return

            await safe_edit(query, "❌ Gift Rejected.")

        elif data == "support":
            keyboard = back_to_home_keyboard()

            await safe_edit(
                query,
                "🎧 Need Help?\n\n"
                f"📲 Telegram:\n{SUPPORT_URL}",
                keyboard
            )

        elif data == "back":
            await safe_edit(query, get_main_text(), get_main_keyboard())

        else:
            await safe_edit(
                query,
                "⚠️ Unknown button. Please press /start again.",
                InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back")]])
            )

    except Exception as e:
        logger.exception("BUTTON ERROR: %s", e)
        try:
            await query.answer("System busy, please try again.", show_alert=False)
        except Exception:
            pass
        try:
            await query.message.reply_text(
                "⚠️ System busy, please try again or press /start."
            )
        except Exception:
            pass


# ================= ADMIN COMMANDS =================

async def all_users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    try:
        rows = get_all_users()
        lines = ["📊 All Users\n"]

        for i, row in enumerate(rows, start=1):
            lines.append(
                f"{i}. {row.get('name') or 'User'}\n"
                f"ID: {row.get('user_id')}\n"
                f"⭐ {row.get('points', 0)} points\n"
                f"👥 {row.get('invited_count', 0)} invites\n"
            )

        text = "\n".join(lines)

        for i in range(0, len(text), 3500):
            await update.message.reply_text(text[i:i + 3500])

    except Exception as e:
        logger.exception("all_users_cmd error: %s", e)
        await update.message.reply_text("⚠️ Failed to load users.")


async def admin_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    target_id = str(update.effective_user.id)
    if context.args:
        target_id = str(context.args[0])

    try:
        db_execute("""
            UPDATE users
            SET
                last_lucky_claim='',
                last_vip_claim='',
                last_elite_claim=''
            WHERE user_id=%s
        """, (target_id,))

        await update.message.reply_text(
            f"✅ Reward reset successful for {target_id}."
        )
    except Exception as e:
        logger.exception("admin_reset error: %s", e)
        await update.message.reply_text("⚠️ Reset failed.")


async def top_users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    try:
        rows = get_top_invites()
        lines = ["🏆 Top Invite Ranking\n"]

        for i, row in enumerate(rows, start=1):
            lines.append(
                f"{i}. {row.get('name') or 'User'}\n"
                f"⭐ {row.get('points', 0)} points\n"
                f"👥 {row.get('invited_count', 0)} invites\n"
            )

        await update.message.reply_text("\n".join(lines))

    except Exception as e:
        logger.exception("top_users_cmd error: %s", e)
        await update.message.reply_text("⚠️ Failed to load ranking.")


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("Usage: /broadcast your message")
        return

    message = " ".join(context.args)

    try:
        users = get_all_users()
        success = 0
        failed = 0

        for row in users:
            target_user_id = row.get("user_id")
            try:
                await context.bot.send_message(
                    chat_id=int(target_user_id),
                    text=message
                )
                success += 1
            except Exception:
                failed += 1

        await update.message.reply_text(
            f"✅ Broadcast sent to {success} users.\n❌ Failed: {failed}"
        )

    except Exception as e:
        logger.exception("broadcast_cmd error: %s", e)
        await update.message.reply_text("⚠️ Broadcast failed.")


async def addpoints_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    if len(context.args) < 2:
        await update.message.reply_text("Usage: /addpoints USER_ID POINTS")
        return

    target_user = str(context.args[0])
    points = safe_int(context.args[1])

    try:
        ensure_user(target_user, "User")
        add_points(target_user, points)

        await update.message.reply_text(
            f"✅ Added {points} points to {target_user}"
        )
    except Exception as e:
        logger.exception("addpoints_cmd error: %s", e)
        await update.message.reply_text("⚠️ Add points failed.")


async def setpoints_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    if len(context.args) < 2:
        await update.message.reply_text("Usage: /setpoints USER_ID POINTS")
        return

    target_user = str(context.args[0])
    points = max(safe_int(context.args[1]), 0)

    try:
        ensure_user(target_user, "User")
        db_execute("UPDATE users SET points=%s WHERE user_id=%s", (points, target_user))

        await update.message.reply_text(
            f"✅ Set {target_user} points to {points}"
        )
    except Exception as e:
        logger.exception("setpoints_cmd error: %s", e)
        await update.message.reply_text("⚠️ Set points failed.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled Telegram error: %s", context.error)


# ================= RUN =================

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is missing.")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing.")

    init_db()

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .connect_timeout(20)
        .read_timeout(20)
        .write_timeout(20)
        .pool_timeout(20)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("all_users", all_users_cmd))
    app.add_handler(CommandHandler("top_users", top_users_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("addpoints", addpoints_cmd))
    app.add_handler(CommandHandler("setpoints", setpoints_cmd))
    app.add_handler(CommandHandler("resetreward", admin_reset))
    app.add_handler(CallbackQueryHandler(button))
    app.add_error_handler(error_handler)

    logger.info("Bot Running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
