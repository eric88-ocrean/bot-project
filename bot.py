# JOMJUDI88 TELEGRAM BOT - PRODUCTION READY V4
# Based on the uploaded V3 bot.py, upgraded for stable customer operation.
# Main upgrades:
# - Optional webhook mode for faster production response
# - PostgreSQL connection pool instead of opening a new DB connection every action
# - Anti-spam / anti-double-click protection for callback buttons
# - Full callback error protection so customers do not press buttons with no response
# - Safer redeem flow with locked approval, no double approval, no negative points
# - Gift request table to stop duplicate gift requests and repeated approvals
# - Mission claim locked transaction to avoid double +2 points
# - Daily reward locked transaction to avoid double claim from fast repeated clicks
# - Audit logs table for important user/admin actions
# - Admin commands: stats, pending redeem, pending gift, setpoints, reset reward
# - Malaysia timezone daily reset

import os
import random
import logging
import time
import asyncio
from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo
from typing import Optional, Tuple
from urllib.parse import quote_plus

import psycopg2
from psycopg2.pool import SimpleConnectionPool
from psycopg2.extras import RealDictCursor

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InputMediaPhoto,
)
from telegram.error import BadRequest, TimedOut, NetworkError, RetryAfter, Forbidden
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# ================= CONFIG =================

TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

BOT_USERNAME = os.getenv("BOT_USERNAME", "JomJudi_bot").strip().replace("@", "")
ADMIN_IDS = set(x.strip() for x in os.getenv("ADMIN_IDS", "1929969589,7889168080,909399622").split(",") if x.strip())

CHANNEL_ID = os.getenv("CHANNEL_ID", "@jomjudi88cuci")
GROUP_ID = os.getenv("GROUP_ID", "@jomjudi88official")

CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/jomjudi88cuci")
GROUP_URL = os.getenv("GROUP_URL", "https://t.me/jomjudi88official")
REGISTER_URL = os.getenv("REGISTER_URL", "https://jomjudi88.live/my/register/?referral=JJ27817922")
JOM_REWARDS_URL = os.getenv("JOM_REWARDS_URL", "https://jom-rewards.atoms.world/")
AMOI_MANJA_URL = os.getenv("AMOI_MANJA_URL", "https://t.me/JomJManja_bot")
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/JomJudi88vip")
HOME_BANNER_FILE_ID = os.getenv("HOME_BANNER_FILE_ID", "").strip()
SHARE_BANNER_FILE_ID = os.getenv("SHARE_BANNER_FILE_ID", "").strip()
SHARE_BANNER_PATH = os.getenv("SHARE_BANNER_PATH", "share_earn.jpg").strip()

TZ = ZoneInfo("Asia/Kuala_Lumpur")

# Optional webhook production mode.
# Set USE_WEBHOOK=true only when you already have a real HTTPS URL.
USE_WEBHOOK = os.getenv("USE_WEBHOOK", "false").lower() == "true"
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
PORT = int(os.getenv("PORT", "8080"))

# DB pool settings. For small Railway/Render DB, keep this modest.
DB_POOL_MIN = int(os.getenv("DB_POOL_MIN", "1"))
DB_POOL_MAX = int(os.getenv("DB_POOL_MAX", "8"))

# Anti-spam settings.
CALLBACK_COOLDOWN_SECONDS = float(os.getenv("CALLBACK_COOLDOWN_SECONDS", "0.8"))
ADMIN_COOLDOWN_SECONDS = float(os.getenv("ADMIN_COOLDOWN_SECONDS", "0.2"))

# Gentle automatic push reminder settings.
# Designed to avoid spam:
# - Check-in reminder: max once every 3 days per user.
# - Share & Earn reminder: max once every 7 days per user.
# - Each run has a send limit to avoid Telegram rate limits.
CHECKIN_PUSH_ENABLED = os.getenv("CHECKIN_PUSH_ENABLED", "true").lower() == "true"
SHARE_PUSH_ENABLED = os.getenv("SHARE_PUSH_ENABLED", "true").lower() == "true"
CHECKIN_PUSH_COOLDOWN_DAYS = int(os.getenv("CHECKIN_PUSH_COOLDOWN_DAYS", "3"))
SHARE_PUSH_COOLDOWN_DAYS = int(os.getenv("SHARE_PUSH_COOLDOWN_DAYS", "7"))
PUSH_MAX_PER_RUN = int(os.getenv("PUSH_MAX_PER_RUN", "120"))
PUSH_SLEEP_SECONDS = float(os.getenv("PUSH_SLEEP_SECONDS", "0.15"))


# ================= LOGGING =================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("jomjudi88-bot-v4")


# ================= GLOBALS =================

DB_POOL: Optional[SimpleConnectionPool] = None
USER_CALLBACK_LAST_SEEN = {}
START_LOCK = {}


# ================= HELPERS =================

def now_my() -> datetime:
    return datetime.now(TZ)


def today_str() -> str:
    return now_my().strftime("%Y-%m-%d")


def now_iso() -> str:
    return now_my().isoformat(timespec="seconds")


def is_admin(user_id) -> bool:
    return str(user_id) in ADMIN_IDS


def safe_int(value, default=0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def random_reward(pool):
    rewards = [item[0] for item in pool]
    weights = [item[1] for item in pool]
    return random.choices(rewards, weights=weights, k=1)[0]


def callback_allowed(user_id: str) -> bool:
    if is_admin(user_id):
        cooldown = ADMIN_COOLDOWN_SECONDS
    else:
        cooldown = CALLBACK_COOLDOWN_SECONDS

    current = time.monotonic()
    last = USER_CALLBACK_LAST_SEEN.get(user_id, 0)
    if current - last < cooldown:
        return False

    USER_CALLBACK_LAST_SEEN[user_id] = current
    return True


def clean_callback_cache():
    # Prevent unlimited memory growth.
    if len(USER_CALLBACK_LAST_SEEN) < 5000:
        return
    current = time.monotonic()
    for uid, ts in list(USER_CALLBACK_LAST_SEEN.items()):
        if current - ts > 300:
            USER_CALLBACK_LAST_SEEN.pop(uid, None)


# ================= DB =================

def init_pool():
    global DB_POOL
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing.")
    if DB_POOL is None:
        DB_POOL = SimpleConnectionPool(
            minconn=DB_POOL_MIN,
            maxconn=DB_POOL_MAX,
            dsn=DATABASE_URL,
            connect_timeout=10,
        )
        logger.info("DB pool initialized. min=%s max=%s", DB_POOL_MIN, DB_POOL_MAX)


def get_conn():
    if DB_POOL is None:
        init_pool()
    return DB_POOL.getconn()


def put_conn(conn):
    if conn and DB_POOL:
        DB_POOL.putconn(conn)


def db_fetchone(query, params=None):
    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params or ())
            return cur.fetchone()
    finally:
        put_conn(conn)


def db_fetchall(query, params=None):
    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params or ())
            return cur.fetchall()
    finally:
        put_conn(conn)


def db_execute(query, params=None):
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(query, params or ())
        conn.commit()
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        put_conn(conn)


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
                    last_elite_claim TEXT DEFAULT '',
                    checkin_streak INTEGER DEFAULT 0,
                    last_checkin_at TEXT DEFAULT '',
                    last_checkin_reminder_at TEXT DEFAULT '',
                    last_checkin_push_at TEXT DEFAULT '',
                    last_share_push_at TEXT DEFAULT '',
                    phone_number TEXT DEFAULT '',
                    phone_verified INTEGER DEFAULT 0,
                    phone_verified_at TEXT DEFAULT '',
                    invite_rewarded INTEGER DEFAULT 0,
                    last_seen_at TEXT DEFAULT '',
                    created_at TEXT DEFAULT '',
                    language TEXT DEFAULT ''
                )
            """)

            user_columns = [
                "points INTEGER DEFAULT 0",
                "invited_count INTEGER DEFAULT 0",
                "spin_chances INTEGER DEFAULT 0",
                "gift_claimed INTEGER DEFAULT 0",
                "referrer_id TEXT",
                "mission_claimed INTEGER DEFAULT 0",
                "last_lucky_claim TEXT DEFAULT ''",
                "last_vip_claim TEXT DEFAULT ''",
                "last_elite_claim TEXT DEFAULT ''",
                "checkin_streak INTEGER DEFAULT 0",
                "last_checkin_at TEXT DEFAULT ''",
                "last_checkin_reminder_at TEXT DEFAULT ''",
                "last_checkin_push_at TEXT DEFAULT ''",
                "last_share_push_at TEXT DEFAULT ''",
                "phone_number TEXT DEFAULT ''",
                "phone_verified INTEGER DEFAULT 0",
                "phone_verified_at TEXT DEFAULT ''",
                "invite_rewarded INTEGER DEFAULT 0",
                "last_seen_at TEXT DEFAULT ''",
                "created_at TEXT DEFAULT ''",
                "language TEXT DEFAULT ''",
            ]
            for col in user_columns:
                cur.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col}")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS redeem_requests (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT,
                    username TEXT,
                    reward_text TEXT,
                    points_needed INTEGER,
                    status TEXT DEFAULT 'pending',
                    created_at TEXT DEFAULT '',
                    processed_at TEXT DEFAULT '',
                    processed_by TEXT DEFAULT ''
                )
            """)
            redeem_columns = [
                "created_at TEXT DEFAULT ''",
                "processed_at TEXT DEFAULT ''",
                "processed_by TEXT DEFAULT ''",
            ]
            for col in redeem_columns:
                cur.execute(f"ALTER TABLE redeem_requests ADD COLUMN IF NOT EXISTS {col}")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS gift_requests (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT,
                    username TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TEXT DEFAULT '',
                    processed_at TEXT DEFAULT '',
                    processed_by TEXT DEFAULT ''
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS deposit_mission_requests (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT,
                    username TEXT,
                    deposit_amount INTEGER,
                    reward_points INTEGER,
                    status TEXT DEFAULT 'pending',
                    created_at TEXT DEFAULT '',
                    processed_at TEXT DEFAULT '',
                    processed_by TEXT DEFAULT ''
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT,
                    action TEXT,
                    detail TEXT,
                    created_at TEXT DEFAULT ''
                )
            """)

            cur.execute("CREATE INDEX IF NOT EXISTS idx_users_points ON users(points DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_users_invites ON users(invited_count DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_users_phone_number ON users(phone_number)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_users_phone_verified ON users(phone_verified)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_redeem_user_status ON redeem_requests(user_id, status)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_redeem_status ON redeem_requests(status)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_gift_user_status ON gift_requests(user_id, status)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_gift_status ON gift_requests(status)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_deposit_mission_user_status ON deposit_mission_requests(user_id, status)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_deposit_mission_status ON deposit_mission_requests(status)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_logs(user_id)")

        conn.commit()
        logger.info("Database initialized.")
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        put_conn(conn)


def audit_log(user_id, action, detail=""):
    try:
        db_execute(
            "INSERT INTO audit_logs (user_id, action, detail, created_at) VALUES (%s,%s,%s,%s)",
            (str(user_id), str(action), str(detail)[:1000], now_iso()),
        )
    except Exception as e:
        logger.warning("audit_log failed: %s", e)


def get_user(user_id):
    return db_fetchone("SELECT * FROM users WHERE user_id=%s", (str(user_id),))


def get_user_phone(user_id) -> str:
    try:
        user = get_user(user_id) or {}
        return user.get("phone_number") or "Not verified"
    except Exception:
        return "Not verified"


def set_user_language(user_id, language):
    lang = str(language or "ms").lower()
    if lang not in ["ms", "en", "zh"]:
        lang = "ms"
    db_execute("UPDATE users SET language=%s WHERE user_id=%s", (lang, str(user_id)))


def get_user_language(user_id) -> str:
    try:
        user = get_user(user_id)
        lang = (user or {}).get("language") or "ms"
        return lang if lang in ["ms", "en", "zh"] else "ms"
    except Exception:
        return "ms"


def normalize_phone(phone: str) -> str:
    phone = (phone or "").strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if phone.startswith("00"):
        phone = "+" + phone[2:]
    elif phone.startswith("60"):
        phone = "+" + phone
    elif phone.startswith("0"):
        phone = "+6" + phone
    return phone


def is_phone_verified(user_id) -> bool:
    user = get_user(user_id)
    return bool(user and safe_int(user.get("phone_verified", 0)) == 1)


def get_verify_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Verify Malaysia Number", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Tap to verify your number",
    )


def get_verified_phones(limit=300):
    return db_fetchall("""
        SELECT user_id, name, phone_number, phone_verified_at, created_at
        FROM users
        WHERE phone_verified=1 AND phone_number <> ''
        ORDER BY phone_verified_at DESC, created_at DESC
        LIMIT %s
    """, (int(limit),))


def create_user(user_id, name, referrer_id=None):
    db_execute("""
        INSERT INTO users
        (user_id, name, points, invited_count, spin_chances, gift_claimed, referrer_id,
         mission_claimed, last_lucky_claim, last_vip_claim, last_elite_claim,
         checkin_streak, last_checkin_at, last_checkin_reminder_at, last_seen_at, created_at)
        VALUES (%s,%s,0,0,0,0,%s,0,'','','',0,'','',%s,%s)
        ON CONFLICT (user_id) DO UPDATE SET
            name = EXCLUDED.name,
            last_seen_at = EXCLUDED.last_seen_at
    """, (str(user_id), name or "User", referrer_id, now_iso(), now_iso()))


def ensure_user(user_id, name="User"):
    user = get_user(user_id)
    if user:
        try:
            db_execute("UPDATE users SET name=%s, last_seen_at=%s WHERE user_id=%s", (name or "User", now_iso(), str(user_id)))
        except Exception:
            pass
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


def set_points(user_id, points):
    db_execute("UPDATE users SET points=%s WHERE user_id=%s", (max(safe_int(points), 0), str(user_id)))


def add_invite(referrer_id):
    db_execute("""
        UPDATE users
        SET invited_count = invited_count + 1,
            points = points + 1
        WHERE user_id=%s
    """, (str(referrer_id),))


def reward_referrer_if_needed(new_user_id: str):
    """Credit referral only after Malaysia phone verification, once per user."""
    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE user_id=%s FOR UPDATE", (str(new_user_id),))
            user = cur.fetchone()
            if not user:
                conn.rollback()
                return

            referrer_id = user.get("referrer_id")
            if not referrer_id or referrer_id == str(new_user_id):
                conn.rollback()
                return

            if safe_int(user.get("invite_rewarded", 0)) == 1:
                conn.rollback()
                return

            cur.execute("""
                UPDATE users
                SET invited_count = invited_count + 1,
                    points = points + 1
                WHERE user_id=%s
            """, (str(referrer_id),))

            cur.execute("UPDATE users SET invite_rewarded=1 WHERE user_id=%s", (str(new_user_id),))

            cur.execute(
                "INSERT INTO audit_logs (user_id, action, detail, created_at) VALUES (%s,%s,%s,%s)",
                (str(referrer_id), "invite_rewarded_after_phone_verify", f"new_user={new_user_id}", now_iso()),
            )

        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        logger.warning("reward_referrer_if_needed failed: %s", e)
    finally:
        put_conn(conn)


def get_top_invites():
    return db_fetchall("""
        SELECT name, points, invited_count
        FROM users
        ORDER BY invited_count DESC, points DESC
        LIMIT 10
    """)


def get_all_users():
    return db_fetchall("""
        SELECT
            u.user_id,
            u.name,
            u.points,
            u.invited_count,
            u.phone_number,
            u.phone_verified,
            COALESCE(u.checkin_streak, 0) AS checkin_streak,
            COALESCE(u.last_checkin_at, '') AS last_checkin_at,
            COALESCE(dm.rm100_count, 0) AS rm100_count,
            COALESCE(dm.rm300_count, 0) AS rm300_count
        FROM users u
        LEFT JOIN (
            SELECT
                user_id,
                SUM(CASE WHEN deposit_amount=100 AND status='approved' THEN 1 ELSE 0 END) AS rm100_count,
                SUM(CASE WHEN deposit_amount=300 AND status='approved' THEN 1 ELSE 0 END) AS rm300_count
            FROM deposit_mission_requests
            GROUP BY user_id
        ) dm ON dm.user_id = u.user_id
        ORDER BY u.invited_count DESC, u.points DESC
    """)


def get_stats():
    return db_fetchone("""
        SELECT
            COUNT(*) AS total_users,
            COALESCE(SUM(points),0) AS total_points,
            COALESCE(SUM(invited_count),0) AS total_invites,
            COALESCE(SUM(CASE WHEN gift_claimed=1 THEN 1 ELSE 0 END),0) AS gifts_claimed
        FROM users
    """)


def get_pending_redeems(limit=20):
    return db_fetchall("""
        SELECT * FROM redeem_requests
        WHERE status='pending'
        ORDER BY id DESC
        LIMIT %s
    """, (int(limit),))


def get_pending_gifts(limit=20):
    return db_fetchall("""
        SELECT * FROM gift_requests
        WHERE status='pending'
        ORDER BY id DESC
        LIMIT %s
    """, (int(limit),))


def get_pending_deposit_missions(limit=20):
    return db_fetchall("""
        SELECT * FROM deposit_mission_requests
        WHERE status='pending'
        ORDER BY id DESC
        LIMIT %s
    """, (int(limit),))


def update_user_checkin_after_claim(cur, user):
    """Update check-in streak when a daily reward is successfully claimed."""
    today = today_str()
    yesterday = (now_my() - timedelta(days=1)).strftime("%Y-%m-%d")
    last_checkin = (user.get("last_checkin_at") or "")[:10]

    if last_checkin == today:
        new_streak = safe_int(user.get("checkin_streak", 0))
    elif last_checkin == yesterday:
        new_streak = safe_int(user.get("checkin_streak", 0)) + 1
    else:
        new_streak = 1

    cur.execute(
        """
        UPDATE users
        SET checkin_streak=%s,
            last_checkin_at=%s
        WHERE user_id=%s
        """,
        (new_streak, now_iso(), str(user.get("user_id"))),
    )
    return new_streak


def get_users_for_checkin_reminder():
    """Users who have not checked in recently. Gentle push: max once every few days."""
    today = today_str()
    inactive_cutoff = (now_my() - timedelta(days=2)).strftime("%Y-%m-%d")
    push_cutoff = (now_my() - timedelta(days=CHECKIN_PUSH_COOLDOWN_DAYS)).isoformat(timespec="seconds")

    return db_fetchall("""
        SELECT user_id, name, language, last_checkin_at, last_checkin_push_at
        FROM users
        WHERE phone_verified=1
          AND (
                COALESCE(last_checkin_push_at, '') = ''
             OR last_checkin_push_at <= %s
          )
          AND (
                (COALESCE(last_checkin_at, '') = '' AND LEFT(COALESCE(created_at, ''), 10) <= %s)
             OR (COALESCE(last_checkin_at, '') <> '' AND LEFT(last_checkin_at, 10) <= %s)
          )
        ORDER BY COALESCE(last_checkin_push_at, '') ASC, last_checkin_at ASC
        LIMIT %s
    """, (push_cutoff, inactive_cutoff, inactive_cutoff, PUSH_MAX_PER_RUN))


def get_users_for_share_reminder():
    """Users who can benefit from Share & Earn reminder. Gentle push: max once a week."""
    push_cutoff = (now_my() - timedelta(days=SHARE_PUSH_COOLDOWN_DAYS)).isoformat(timespec="seconds")
    created_cutoff = (now_my() - timedelta(days=2)).strftime("%Y-%m-%d")

    return db_fetchall("""
        SELECT user_id, name, language, invited_count, last_share_push_at
        FROM users
        WHERE phone_verified=1
          AND LEFT(COALESCE(created_at, ''), 10) <= %s
          AND COALESCE(invited_count, 0) < 20
          AND (
                COALESCE(last_share_push_at, '') = ''
             OR last_share_push_at <= %s
          )
        ORDER BY COALESCE(last_share_push_at, '') ASC, invited_count ASC
        LIMIT %s
    """, (created_cutoff, push_cutoff, PUSH_MAX_PER_RUN))


def mark_checkin_reminder_sent(user_id):
    # Keep old column updated for compatibility, and use the new dedicated push column.
    db_execute(
        "UPDATE users SET last_checkin_reminder_at=%s, last_checkin_push_at=%s WHERE user_id=%s",
        (now_iso(), now_iso(), str(user_id)),
    )


def mark_share_reminder_sent(user_id):
    db_execute("UPDATE users SET last_share_push_at=%s WHERE user_id=%s", (now_iso(), str(user_id)))


def checkin_reminder_text(lang):
    lang = lang if lang in ["ms", "en", "zh"] else "ms"
    if lang == "zh":
        return "🎁 Boss，今天的 Check In 还没领取哦。\n\n回来按一下，继续收集 Reward Points 🔥"
    if lang == "en":
        return "🎁 Boss, your Check In reward is waiting.\n\nCome back and continue collecting Reward Points 🔥"
    return "🎁 Boss, Check In reward anda sedang tunggu.\n\nJom masuk balik dan terus kumpul Reward Points 🔥"


def share_reminder_text(lang, user_id):
    link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
    lang = lang if lang in ["ms", "en", "zh"] else "ms"
    if lang == "zh":
        return (
            "💰 Share & Earn 提醒\n\n"
            "Boss，分享你的专属 link 给朋友，朋友验证后你就可以继续累积 Reward Points 🔥\n\n"
            f"🔗 你的邀请链接:\n{link}"
        )
    if lang == "en":
        return (
            "💰 Share & Earn reminder\n\n"
            "Boss, share your personal link with friends. When they verify, you can collect more Reward Points 🔥\n\n"
            f"🔗 Your invite link:\n{link}"
        )
    return (
        "💰 Share & Earn reminder\n\n"
        "Boss, share link anda kepada kawan. Bila mereka verify, anda boleh kumpul lebih banyak Reward Points 🔥\n\n"
        f"🔗 Link Boss:\n{link}"
    )


def checkin_push_keyboard(user_id):
    # Push reminder button should match the main menu wording: 🎁 Check In.
    # Customer taps this and goes directly to the Check In / Daily Reward page.
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(tr(user_id, "checkin"), callback_data="reward_center")],
    ])


def share_push_keyboard(user_id):
    # Push reminder button should match the rewards menu wording: 💰 Share & Earn.
    # Customer taps this and goes directly to their referral/share page inside the bot.
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(tr(user_id, "share_earn_btn"), callback_data="link")],
    ])


def broadcast_share_earn_keyboard():
    """Inline buttons for promotional broadcasts.

    Button 1 opens the bot Share & Earn page through callback_data="link".
    Button 2 opens Customer Service for screenshot claim.
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Share Kepada Kawan", callback_data="link")],
        [InlineKeyboardButton("🎧 Screenshot Kepada Customer Service", url=SUPPORT_URL)],
    ])


# ================= TRANSACTION FLOWS =================

def claim_daily_reward(user_id: str, reward_type: str, min_invites: int, reward_pool) -> Tuple[bool, str]:
    """Locked transaction to stop double claim from double-click/spam."""
    today = today_str()
    column_map = {
        "lucky": "last_lucky_claim",
        "vip": "last_vip_claim",
        "elite": "last_elite_claim",
    }
    title_map = {
        "lucky": "Lucky Reward",
        "vip": "VIP Reward",
        "elite": "Elite Reward",
    }
    column = column_map.get(reward_type)
    if not column:
        return False, "⚠️ Invalid reward type."

    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE user_id=%s FOR UPDATE", (str(user_id),))
            user = cur.fetchone()
            if not user:
                conn.rollback()
                return False, "⚠️ User not found. Please press /start again."

            if not is_admin(user_id):
                if (
                    user.get("last_lucky_claim") == today or
                    user.get("last_vip_claim") == today or
                    user.get("last_elite_claim") == today
                ):
                    conn.rollback()
                    return False, tr(user_id, "daily_claimed")

                if safe_int(user.get("invited_count", 0)) < min_invites:
                    conn.rollback()
                    return False, tr(user_id, "unlock_invites", title=title_map[reward_type], invites=min_invites)

            reward = random_reward(reward_pool)
            cur.execute(
                f"UPDATE users SET {column}=%s, points=GREATEST(points + %s, 0) WHERE user_id=%s",
                (today, int(reward), str(user_id)),
            )

            new_streak = update_user_checkin_after_claim(cur, user)

            cur.execute(
                "INSERT INTO audit_logs (user_id, action, detail, created_at) VALUES (%s,%s,%s,%s)",
                (str(user_id), f"claim_{reward_type}", f"reward={reward}; checkin_streak={new_streak}", now_iso()),
            )

        conn.commit()

        if reward > 0:
            return True, tr(user_id, "reward_win", reward=reward)
        return True, tr(user_id, "reward_zero")
    except Exception as e:
        if conn:
            conn.rollback()
        logger.exception("claim_daily_reward error: %s", e)
        return False, "⚠️ Reward system busy. Please try again."
    finally:
        put_conn(conn)


def claim_mission_reward(user_id: str) -> Tuple[bool, str]:
    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE user_id=%s FOR UPDATE", (str(user_id),))
            user = cur.fetchone()
            if not user:
                conn.rollback()
                return False, "⚠️ User not found. Please press /start again."

            if safe_int(user.get("mission_claimed", 0)) == 1 and not is_admin(user_id):
                conn.rollback()
                return False, tr(user_id, "mission_claimed")

            cur.execute("UPDATE users SET points=points+2, mission_claimed=1 WHERE user_id=%s", (str(user_id),))
            cur.execute(
                "INSERT INTO audit_logs (user_id, action, detail, created_at) VALUES (%s,%s,%s,%s)",
                (str(user_id), "mission_claim", "+2 points", now_iso()),
            )
        conn.commit()
        return True, tr(user_id, "mission_completed")
    except Exception as e:
        if conn:
            conn.rollback()
        logger.exception("claim_mission_reward error: %s", e)
        return False, "⚠️ Mission system busy. Please try again."
    finally:
        put_conn(conn)


def create_redeem_request_locked(user_id, username, reward_text, points_needed) -> Tuple[bool, str, Optional[int]]:
    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE user_id=%s FOR UPDATE", (str(user_id),))
            user = cur.fetchone()
            if not user:
                conn.rollback()
                return False, "⚠️ User not found. Please press /start again.", None

            if safe_int(user.get("points", 0)) < int(points_needed) and not is_admin(user_id):
                conn.rollback()
                return False, tr(user_id, "not_enough_text"), None

            cur.execute("""
                SELECT id FROM redeem_requests
                WHERE user_id=%s AND reward_text=%s AND status='pending'
                LIMIT 1
            """, (str(user_id), reward_text))
            pending = cur.fetchone()
            if pending:
                conn.rollback()
                return False, tr(user_id, "redeem_pending"), pending["id"]

            cur.execute("""
                INSERT INTO redeem_requests
                (user_id, username, reward_text, points_needed, status, created_at)
                VALUES (%s,%s,%s,%s,'pending',%s)
                RETURNING id
            """, (str(user_id), username, reward_text, int(points_needed), now_iso()))
            row = cur.fetchone()
            request_id = row["id"]

            cur.execute(
                "INSERT INTO audit_logs (user_id, action, detail, created_at) VALUES (%s,%s,%s,%s)",
                (str(user_id), "redeem_request", f"{reward_text} / {points_needed} points / id={request_id}", now_iso()),
            )

        conn.commit()
        return True, tr(user_id, "redeem_submitted"), request_id
    except Exception as e:
        if conn:
            conn.rollback()
        logger.exception("create_redeem_request_locked error: %s", e)
        return False, "⚠️ Redeem system busy. Please try again.", None
    finally:
        put_conn(conn)


def approve_redeem_request(request_id, admin_id) -> Tuple[bool, str, Optional[str], str]:
    conn = None
    target_user = None
    reward_text = ""
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM redeem_requests WHERE id=%s FOR UPDATE", (int(request_id),))
            req = cur.fetchone()
            if not req:
                conn.rollback()
                return False, "❌ Redeem request not found.", None, ""

            target_user = req["user_id"]
            reward_text = req["reward_text"]

            if req["status"] != "pending":
                conn.rollback()
                return False, f"⚠️ This request was already {req['status']}.", target_user, reward_text

            cur.execute("""
                UPDATE users
                SET points = points - %s
                WHERE user_id=%s AND points >= %s
                RETURNING points
            """, (req["points_needed"], req["user_id"], req["points_needed"]))
            updated = cur.fetchone()
            if not updated:
                conn.rollback()
                return False, "❌ User does not have enough points now. Approval cancelled.", target_user, reward_text

            cur.execute("""
                UPDATE redeem_requests
                SET status='approved', processed_at=%s, processed_by=%s
                WHERE id=%s
            """, (now_iso(), str(admin_id), int(request_id)))

            cur.execute(
                "INSERT INTO audit_logs (user_id, action, detail, created_at) VALUES (%s,%s,%s,%s)",
                (str(admin_id), "approve_redeem", f"request={request_id} target={target_user} reward={reward_text}", now_iso()),
            )
        conn.commit()
        return True, "✅ Redeem Approved.", target_user, reward_text
    except Exception as e:
        if conn:
            conn.rollback()
        logger.exception("approve_redeem_request error: %s", e)
        return False, "⚠️ Approval failed. Please check logs.", target_user, reward_text
    finally:
        put_conn(conn)


def reject_redeem_request(request_id, admin_id) -> Tuple[bool, str, Optional[str], str]:
    conn = None
    target_user = None
    reward_text = ""
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM redeem_requests WHERE id=%s FOR UPDATE", (int(request_id),))
            req = cur.fetchone()
            if not req:
                conn.rollback()
                return False, "❌ Redeem request not found.", None, ""

            target_user = req["user_id"]
            reward_text = req["reward_text"]

            if req["status"] != "pending":
                conn.rollback()
                return False, f"⚠️ This request was already {req['status']}.", target_user, reward_text

            cur.execute("""
                UPDATE redeem_requests
                SET status='rejected', processed_at=%s, processed_by=%s
                WHERE id=%s
            """, (now_iso(), str(admin_id), int(request_id)))
            cur.execute(
                "INSERT INTO audit_logs (user_id, action, detail, created_at) VALUES (%s,%s,%s,%s)",
                (str(admin_id), "reject_redeem", f"request={request_id} target={target_user} reward={reward_text}", now_iso()),
            )
        conn.commit()
        return True, "❌ Redeem Rejected.", target_user, reward_text
    except Exception as e:
        if conn:
            conn.rollback()
        logger.exception("reject_redeem_request error: %s", e)
        return False, "⚠️ Reject failed. Please check logs.", target_user, reward_text
    finally:
        put_conn(conn)


def create_gift_request_locked(user_id, username) -> Tuple[bool, str, Optional[int]]:
    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE user_id=%s FOR UPDATE", (str(user_id),))
            user = cur.fetchone()
            if not user:
                conn.rollback()
                return False, "⚠️ User not found. Please press /start again.", None

            if safe_int(user.get("gift_claimed", 0)) == 1 and not is_admin(user_id):
                conn.rollback()
                return False, tr(user_id, "gift_claimed"), None

            cur.execute("SELECT id FROM gift_requests WHERE user_id=%s AND status='pending' LIMIT 1", (str(user_id),))
            pending = cur.fetchone()
            if pending:
                conn.rollback()
                return False, tr(user_id, "gift_pending"), pending["id"]

            cur.execute("""
                INSERT INTO gift_requests (user_id, username, status, created_at)
                VALUES (%s,%s,'pending',%s)
                RETURNING id
            """, (str(user_id), username, now_iso()))
            row = cur.fetchone()
            request_id = row["id"]
            cur.execute(
                "INSERT INTO audit_logs (user_id, action, detail, created_at) VALUES (%s,%s,%s,%s)",
                (str(user_id), "gift_request", f"id={request_id}", now_iso()),
            )
        conn.commit()
        return True, tr(user_id, "gift_submitted"), request_id
    except Exception as e:
        if conn:
            conn.rollback()
        logger.exception("create_gift_request_locked error: %s", e)
        return False, "⚠️ Gift system busy. Please try again.", None
    finally:
        put_conn(conn)



def create_deposit_mission_request_locked(user_id, username, deposit_amount, reward_points) -> Tuple[bool, str, Optional[int]]:
    """User submits a daily deposit mission for admin review. Points are added only after admin approval."""
    deposit_amount = safe_int(deposit_amount)
    reward_points = safe_int(reward_points)
    if deposit_amount not in [100, 300] or reward_points not in [2, 5]:
        return False, tr(user_id, "deposit_invalid"), None

    today = today_str()
    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE user_id=%s FOR UPDATE", (str(user_id),))
            user = cur.fetchone()
            if not user:
                conn.rollback()
                return False, "⚠️ User not found. Please press /start again.", None

            # One pending request per user is enough. It prevents spam while admin is reviewing.
            cur.execute("""
                SELECT id FROM deposit_mission_requests
                WHERE user_id=%s AND status='pending'
                LIMIT 1
            """, (str(user_id),))
            pending = cur.fetchone()
            if pending:
                conn.rollback()
                return False, tr(user_id, "deposit_pending"), pending["id"]

            # Do not allow the same tier to be approved more than once per Malaysia day.
            cur.execute("""
                SELECT id FROM deposit_mission_requests
                WHERE user_id=%s AND deposit_amount=%s AND status='approved' AND created_at LIKE %s
                LIMIT 1
            """, (str(user_id), deposit_amount, today + "%"))
            approved_today = cur.fetchone()
            if approved_today and not is_admin(user_id):
                conn.rollback()
                return False, tr(user_id, "deposit_already_claimed"), approved_today["id"]

            cur.execute("""
                INSERT INTO deposit_mission_requests
                (user_id, username, deposit_amount, reward_points, status, created_at)
                VALUES (%s,%s,%s,%s,'pending',%s)
                RETURNING id
            """, (str(user_id), username, deposit_amount, reward_points, now_iso()))
            row = cur.fetchone()
            request_id = row["id"]

            cur.execute(
                "INSERT INTO audit_logs (user_id, action, detail, created_at) VALUES (%s,%s,%s,%s)",
                (str(user_id), "deposit_mission_request", f"deposit=RM{deposit_amount} reward=+{reward_points} id={request_id}", now_iso()),
            )

        conn.commit()
        return True, tr(user_id, "deposit_submitted"), request_id
    except Exception as e:
        if conn:
            conn.rollback()
        logger.exception("create_deposit_mission_request_locked error: %s", e)
        return False, "⚠️ Deposit mission system busy. Please try again.", None
    finally:
        put_conn(conn)


def approve_deposit_mission_request(request_id, admin_id) -> Tuple[bool, str, Optional[str], int, int]:
    conn = None
    target_user = None
    deposit_amount = 0
    reward_points = 0
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM deposit_mission_requests WHERE id=%s FOR UPDATE", (int(request_id),))
            req = cur.fetchone()
            if not req:
                conn.rollback()
                return False, "❌ Deposit mission request not found.", None, 0, 0

            target_user = req["user_id"]
            deposit_amount = safe_int(req.get("deposit_amount"))
            reward_points = safe_int(req.get("reward_points"))

            if req["status"] != "pending":
                conn.rollback()
                return False, f"⚠️ This request was already {req['status']}.", target_user, deposit_amount, reward_points

            cur.execute("UPDATE users SET points=points+%s WHERE user_id=%s", (reward_points, str(target_user)))
            cur.execute("""
                UPDATE deposit_mission_requests
                SET status='approved', processed_at=%s, processed_by=%s
                WHERE id=%s
            """, (now_iso(), str(admin_id), int(request_id)))
            cur.execute(
                "INSERT INTO audit_logs (user_id, action, detail, created_at) VALUES (%s,%s,%s,%s)",
                (str(admin_id), "approve_deposit_mission", f"request={request_id} target={target_user} RM{deposit_amount} +{reward_points}", now_iso()),
            )
        conn.commit()
        return True, f"✅ Deposit Mission Approved. +{reward_points} points added.", target_user, deposit_amount, reward_points
    except Exception as e:
        if conn:
            conn.rollback()
        logger.exception("approve_deposit_mission_request error: %s", e)
        return False, "⚠️ Deposit mission approval failed.", target_user, deposit_amount, reward_points
    finally:
        put_conn(conn)


def reject_deposit_mission_request(request_id, admin_id) -> Tuple[bool, str, Optional[str], int, int]:
    conn = None
    target_user = None
    deposit_amount = 0
    reward_points = 0
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM deposit_mission_requests WHERE id=%s FOR UPDATE", (int(request_id),))
            req = cur.fetchone()
            if not req:
                conn.rollback()
                return False, "❌ Deposit mission request not found.", None, 0, 0

            target_user = req["user_id"]
            deposit_amount = safe_int(req.get("deposit_amount"))
            reward_points = safe_int(req.get("reward_points"))

            if req["status"] != "pending":
                conn.rollback()
                return False, f"⚠️ This request was already {req['status']}.", target_user, deposit_amount, reward_points

            cur.execute("""
                UPDATE deposit_mission_requests
                SET status='rejected', processed_at=%s, processed_by=%s
                WHERE id=%s
            """, (now_iso(), str(admin_id), int(request_id)))
            cur.execute(
                "INSERT INTO audit_logs (user_id, action, detail, created_at) VALUES (%s,%s,%s,%s)",
                (str(admin_id), "reject_deposit_mission", f"request={request_id} target={target_user} RM{deposit_amount} +{reward_points}", now_iso()),
            )
        conn.commit()
        return True, "❌ Deposit Mission Rejected.", target_user, deposit_amount, reward_points
    except Exception as e:
        if conn:
            conn.rollback()
        logger.exception("reject_deposit_mission_request error: %s", e)
        return False, "⚠️ Deposit mission reject failed.", target_user, deposit_amount, reward_points
    finally:
        put_conn(conn)


def approve_gift_request(target_user_id, admin_id) -> Tuple[bool, str, Optional[str]]:
    """
    Approve RM38 game credit request
    WITHOUT adding Telegram reward points
    """
    conn = None

    try:
        conn = get_conn()

        with conn.cursor(cursor_factory=RealDictCursor) as cur:

            cur.execute(
                "SELECT * FROM users WHERE user_id=%s FOR UPDATE",
                (str(target_user_id),)
            )

            user = cur.fetchone()

            if not user:
                conn.rollback()
                return False, "❌ User not found.", None

            if safe_int(user.get("gift_claimed", 0)) == 1:
                conn.rollback()
                return False, "⚠️ Gift already approved before.", str(target_user_id)

            cur.execute("""
                SELECT * FROM gift_requests
                WHERE user_id=%s AND status='pending'
                ORDER BY id DESC
                LIMIT 1
                FOR UPDATE
            """, (str(target_user_id),))

            gift_req = cur.fetchone()

            # ONLY mark as claimed
            # DO NOT ADD REWARD POINTS
            cur.execute("""
                UPDATE users
                SET gift_claimed=1
                WHERE user_id=%s
            """, (str(target_user_id),))

            if gift_req:
                cur.execute("""
                    UPDATE gift_requests
                    SET status='approved', processed_at=%s, processed_by=%s
                    WHERE id=%s
                """, (now_iso(), str(admin_id), gift_req["id"]))

            cur.execute(
                "INSERT INTO audit_logs (user_id, action, detail, created_at) VALUES (%s,%s,%s,%s)",
                (
                    str(admin_id),
                    "approve_gift",
                    f"target={target_user_id} RM38_GAME_CREDIT",
                    now_iso(),
                ),
            )

        conn.commit()

        return True, "✅ Gift Approved. RM38 game credit confirmed.", str(target_user_id)

    except Exception as e:

        if conn:
            conn.rollback()

        logger.exception("approve_gift_request error: %s", e)

        return False, "⚠️ Gift approval failed.", str(target_user_id)

    finally:
        put_conn(conn)


def reject_gift_request(target_user_id, admin_id) -> Tuple[bool, str, Optional[str]]:
    conn = None
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM gift_requests
                WHERE user_id=%s AND status='pending'
                ORDER BY id DESC
                LIMIT 1
                FOR UPDATE
            """, (str(target_user_id),))
            req = cur.fetchone()
            if req:
                cur.execute("""
                    UPDATE gift_requests
                    SET status='rejected', processed_at=%s, processed_by=%s
                    WHERE id=%s
                """, (now_iso(), str(admin_id), req["id"]))
            cur.execute(
                "INSERT INTO audit_logs (user_id, action, detail, created_at) VALUES (%s,%s,%s,%s)",
                (str(admin_id), "reject_gift", f"target={target_user_id}", now_iso()),
            )
        conn.commit()
        return True, "❌ Gift Rejected.", str(target_user_id)
    except Exception as e:
        if conn:
            conn.rollback()
        logger.exception("reject_gift_request error: %s", e)
        return False, "⚠️ Gift reject failed. Please check logs.", str(target_user_id)
    finally:
        put_conn(conn)


# ================= UI / LANGUAGE =================

TEXT = {
    "ms": {
        "language_title": "🌐 Sila pilih bahasa anda\n\n🇲🇾 Bahasa Melayu\n🇬🇧 English\n🇨🇳 中文",
        "language_saved": "✅ Bahasa berjaya ditetapkan.",
        "verify_button": "📱 Sahkan Nombor Malaysia",
        "verify_placeholder": "Tekan untuk sahkan nombor anda",
        "verify_text": "🇲🇾 Pengguna Malaysia Sahaja\n\nSila sahkan nombor telefon anda untuk teruskan.\n\nTekan butang di bawah dan kongsi contact Telegram anda.",
        "verify_own_phone": "❌ Sila kongsi nombor Telegram sendiri sahaja.",
        "verify_already": "✅ Nombor anda sudah disahkan.",
        "verify_malaysia_only": "❌ Nombor telefon Malaysia sahaja 🇲🇾",
        "verify_duplicate": "❌ Nombor telefon ini sudah didaftarkan.",
        "verify_success": "✅ Nombor Malaysia berjaya disahkan.\n\nSelamat datang ke JomJudi88 Rewards 🔥",
        "verify_busy": "⚠️ Sistem pengesahan sibuk. Sila cuba lagi.",
        "verify_manual_phone_reject": "❌ Jangan taip nombor telefon secara manual.\n\nSila tekan butang 📱 Sahkan Nombor Malaysia di bawah, kemudian pilih Share Contact.\n\nJika butang tidak nampak, tekan icon keyboard/menu di sebelah kiri bawah Telegram.",
        "must_verify": "🇲🇾 Pengguna Malaysia Sahaja\n\nSila tekan /start dan sahkan nombor telefon anda dahulu.",
        "register": "🔐 Daftar",
        "earn": "💰 Kumpul Rewards",
        "gift": "🎁 Free RM38",
        "checkin": "🎁 Check In",
        "community": "🌐 Komuniti",
        "support": "🎧 Support",
        "change_language": "🌐 Tukar Bahasa",
        "back": "🔙 Kembali",
        "home": "🎁 𝗝𝗢𝗠𝗝𝗨𝗗𝗜𝟴𝟴 𝗥𝗘𝗪𝗔𝗥𝗗𝗦 🔥\n\n💸 Main & collect reward setiap hari\n🎯 Claim points & redeem hadiah\n👑 Unlock VIP rewards\n💰 Touch ’n Go RM100\n\n⚡ Auto Deposit & Withdraw 24/7\n🔐 Support & Privasi Terjamin\n\n👇 Pilih menu di bawah 🚀",
        "your_rewards_btn": "💎 Rewards Anda",
        "share_earn_btn": "💰 Share & Earn",
        "missions_btn": "💰 Daily Deposit Mission",
        "jom_rewards_btn": "🌐 JOM Rewards",
        "claim_reward_btn": "🎁 Claim Reward",
        "menu_text": "💰 Rewards Center\n\n🎁 Complete missions\n🔥 Unlock VIP rewards\n💸 Collect points & claim hadiah setiap hari\n\n💰 Claim Touch'N Go FREE RM100\n\nSyarat untuk claim reward:\n\n• Mesti ada akaun berdaftar di JomJudi88\n• Share referral link ke Facebook / Telegram / kawan-kawan\n  (1 referral = 1 point)\n\n🎁 Ganjaran:\n\n• 3 Point = RM1 Kredit Game\n• 10 Point = RM5 Kredit Game\n• 20 Point = RM10 Kredit Game\n• 50 Point = RM25 Kredit Game\n• 100 Point = RM50 Kredit Game\n• 200 Point = Touch 'n Go RM100\n\n👇 Pilih option di bawah:",
        "profile_text": "💎 Rewards Anda\n\n⭐️ Reward Points: {points}\n👥 Kawan Dijemput: {invites}\n\n----------------------------------------",
        "share_caption": "💰 Share & Earn Lagi!\n\nJom ajak kawan join & collect reward sama-sama 🔥\n\n🔗 Link Boss:\n\n{link}",
        "share_button": "📤 Share Kepada Kawan",
        "share_text": "Jom join JomJudi88 & collect reward sama-sama 🔥",
        "reward_center_text": "⏰ Reset setiap hari 12AM Malaysia time\n\n━━━━━━━━━━━━━━\n\n🎁 Lucky Reward\n🔓 Semua boleh claim\n\n⭐️ Random Points:\n+0 • +1\n\n━━━━━━━━━━━━━━\n\n🔥 VIP Reward\n🔒 Unlock 5 invites\n\n⭐️ Better Rewards:\n+0 • +1 • +3\n\n━━━━━━━━━━━━━━\n\n👑 Elite Reward\n🔒 Unlock 20 invites\n\n💎 Big Rewards:\n+0 • +1 • +5",
        "lucky_reward": "🎁 Lucky Reward",
        "vip_reward": "🔥 VIP Reward",
        "elite_reward": "👑 Elite Reward",
        "missions_text": "💰 Daily Deposit Mission\n\n✅ Deposit RM100+ = +2 Reward Points\n✅ Deposit RM300+ = +5 Reward Points\n\nLepas deposit di website, tekan submit di bawah.\nAdmin akan semak dan approve dahulu sebelum Points masuk.",
        "submit_deposit_100": "📤 Submit RM100+ Deposit",
        "submit_deposit_300": "📤 Submit RM300+ Deposit",
        "deposit_submitted": "⏳ Deposit mission sudah dihantar.\n\nAdmin akan semak deposit anda.",
        "deposit_pending": "⏳ Anda sudah ada pending deposit mission.\n\nSila tunggu admin approval.",
        "deposit_already_claimed": "✅ Anda sudah claim mission ini hari ini. Sila cuba lagi esok.",
        "deposit_invalid": "⚠️ Deposit mission tidak sah.",
        "deposit_approved_user": "✅ Daily Deposit Mission approved!\n\n💰 Deposit: RM{amount}+\n⭐ +{points} Reward Points sudah masuk.",
        "deposit_rejected_user": "❌ Daily Deposit Mission anda ditolak.\n\n💰 Deposit: RM{amount}+",
        "join_channel": "📢 Join Channel",
        "join_group": "👥 Join Group",
        "done_join": "✅ Done Join",
        "mission_not_joined": "❌ Sila join Channel & Group dahulu.\n\n⚠️ Kalau sudah join tapi masih tidak boleh claim, pastikan bot sudah jadi admin dalam channel/group.",
        "claim_reward_text": "🎁 Claim Reward\n\n⭐ Points Anda: {points}\n\n• RM1 Credit → 3 Points\n• RM5 Credit → 10 Points\n• RM10 Credit → 20 Points\n• RM25 Credit → 50 Points\n• RM50 Credit → 100 Points\n• Touch 'n Go RM100 → 200 Points\n\nPilih hadiah di bawah untuk submit redemption.\nSemua hadiah tertakluk kepada admin approval.",
        "not_enough_btn": "🔒 Points belum cukup",
        "not_enough_text": "🔒 Points anda belum cukup untuk unlock hadiah ini.\n\nInvite kawan dan complete missions untuk kumpul lebih banyak Points.",
        "gift_claimed": "✅ Anda sudah claim hadiah member baru.",
        "gift_text": "🎁 Hadiah Member Baru\n\n✅ Daftar akaun baru\n✅ Deposit pertama RM20+\n✅ Join Channel & Group dulu 😎\n\n🎁 Reward Free:\nRM38 Kredit Game 💸",
        "claim_gift_btn": "🎁 Claim Gift",
        "gift_join_first": "❌ Sila join Channel & Group dahulu sebelum claim Free RM38.\n\n⚠️ Kalau sudah join tapi masih tidak boleh claim, pastikan bot sudah jadi admin dalam channel/group.",
        "community_text": "🌐 JOMJUDI88 COMMUNITY\n\n📢 Official Updates\n👥 VIP Member Group\n🔞 Exclusive Amoi Content\n\n👇 Pilih di bawah:",
        "official_channel": "📢 Official Channel",
        "vip_group": "👥 VIP Group",
        "amoi_manja": "🔞 Amoi Manja",
        "support_text": "🟢 JOMJUDI88 SUPPORT\n\n⚡ Fast Response\n🔐 Secure & Private\n🎧 24/7 Live Assistance\n\nTekan link di bawah untuk contact support.\n{url}",
        "unknown_button": "⚠️ Button tidak dikenali. Sila tekan /start semula.",
        "system_busy": "⚠️ Sistem sibuk, sila cuba lagi atau tekan /start.",
        "daily_claimed": "❌ Anda sudah claim reward hari ini.\n\n⏰ Sila datang semula selepas 12AM Malaysia time.",
        "unlock_invites": "🔒 {title} unlock pada {invites} invites.",
        "reward_win": "🎉 Reward Berjaya Dibuka!\n\n⭐ +{reward} Points masuk 🔥",
        "reward_zero": "😆 Belum kena reward kali ni\n\nCuba lagi esok 🔥",
        "mission_claimed": "✅ Anda sudah claim mission rewards.",
        "mission_completed": "🎉 Mission Completed!\n\n⭐ +2 Points Added",
        "redeem_submitted": "⏳ Redeem request sudah dihantar.\n\nAdmin akan review request anda.",
        "redeem_pending": "⏳ Anda sudah ada pending request untuk reward ini.\n\nSila tunggu admin approval.",
        "gift_submitted": "⏳ Gift request sudah dihantar.\n\nAdmin akan review request anda.",
        "gift_pending": "⏳ Anda sudah ada pending gift request.\n\nSila tunggu admin approval.",
        "approve_user_redeem": "✅ Redeem request anda sudah approved.\n\n🎁 Reward: {reward}",
        "reject_user_redeem": "❌ Redeem request anda ditolak.\n\n🎁 Reward: {reward}",
        "approve_user_gift": "✅ Request RM38 Kredit Game anda sudah approved.\n\n💸 RM38 game credit sudah dihantar ke gaming account anda.",
        "reject_user_gift": "❌ Request hadiah member baru anda ditolak.",
    },
    "en": {
        "language_title": "🌐 Please choose your language\n\n🇲🇾 Bahasa Melayu\n🇬🇧 English\n🇨🇳 中文",
        "language_saved": "✅ Language selected successfully.",
        "verify_button": "📱 Verify Malaysia Number",
        "verify_placeholder": "Tap to verify your number",
        "verify_text": "🇲🇾 Malaysia Users Only\n\nPlease verify your phone number to continue.\n\nTap the button below and share your Telegram contact.",
        "verify_own_phone": "❌ Please share your own Telegram phone number.",
        "verify_already": "✅ Your number is already verified.",
        "verify_malaysia_only": "❌ Malaysia phone number only 🇲🇾",
        "verify_duplicate": "❌ This phone number is already registered.",
        "verify_success": "✅ Malaysia number verified successfully.\n\nWelcome to JomJudi88 Rewards 🔥",
        "verify_busy": "⚠️ Verification system busy. Please try again.",
        "verify_manual_phone_reject": "❌ Please do not type your phone number manually.\n\nTap the 📱 Verify Malaysia Number button below, then choose Share Contact.\n\nIf you cannot see the button, tap the keyboard/menu icon at the bottom-left of Telegram.",
        "must_verify": "🇲🇾 Malaysia Users Only\n\nPlease press /start and verify your phone number first.",
        "register": "🔐 Register",
        "earn": "💰 Earn Rewards",
        "gift": "🎁 Free RM38",
        "checkin": "🎁 Check In",
        "community": "🌐 Community",
        "support": "🎧 Support",
        "change_language": "🌐 Change Language",
        "back": "🔙 Back",
        "home": "🎁 𝗝𝗢𝗠𝗝𝗨𝗗𝗜𝟴𝟴 𝗥𝗘𝗪𝗔𝗥𝗗𝗦 🔥\n\n💸 Play and collect rewards daily\n🎯 Claim points and redeem prizes\n👑 Unlock VIP rewards\n💰 Touch ’n Go RM100\n\n⚡ Auto Deposit & Withdraw 24/7\n🔐 Secure support and privacy\n\n👇 Choose a menu below 🚀",
        "your_rewards_btn": "💎 Your Rewards",
        "share_earn_btn": "💰 Share & Earn",
        "missions_btn": "💰 Daily Deposit Mission",
        "jom_rewards_btn": "🌐 JOM Rewards",
        "claim_reward_btn": "🎁 Claim Reward",
        "menu_text": "💰 Rewards Center\n\n🎁 Complete missions\n🔥 Unlock VIP rewards\n💸 Collect points and claim rewards daily\n\n💰 Claim Touch'N Go FREE RM100\n\nReward claim requirements:\n\n• Must have a registered JomJudi88 account\n• Share your referral link to Facebook / Telegram / friends\n  (1 referral = 1 point)\n\n🎁 Rewards:\n\n• 3 Points = RM1 Game Credit\n• 10 Points = RM5 Game Credit\n• 20 Points = RM10 Game Credit\n• 50 Points = RM25 Game Credit\n• 100 Points = RM50 Game Credit\n• 200 Points = Touch 'n Go RM100\n\n👇 Select an option below:",
        "profile_text": "💎 Your Rewards\n\n⭐️ Reward Points: {points}\n👥 Friends Referred: {invites}\n\n----------------------------------------",
        "share_caption": "💰 Share & Earn More!\n\nInvite your friends to join and collect rewards together 🔥\n\n🔗 Your Link:\n\n{link}",
        "share_button": "📤 Share with Friends",
        "share_text": "Join JomJudi88 and collect rewards together 🔥",
        "reward_center_text": "⏰ Resets daily at 12AM Malaysia time\n\n━━━━━━━━━━━━━━\n\n🎁 Lucky Reward\n🔓 Everyone can claim\n\n⭐️ Random Points:\n+0 • +1\n\n━━━━━━━━━━━━━━\n\n🔥 VIP Reward\n🔒 Unlocks at 5 invites\n\n⭐️ Better Rewards:\n+0 • +1 • +3\n\n━━━━━━━━━━━━━━\n\n👑 Elite Reward\n🔒 Unlocks at 20 invites\n\n💎 Big Rewards:\n+0 • +1 • +5",
        "lucky_reward": "🎁 Lucky Reward",
        "vip_reward": "🔥 VIP Reward",
        "elite_reward": "👑 Elite Reward",
        "missions_text": "💰 Daily Deposit Mission\n\n✅ Deposit RM100+ = +2 Reward Points\n✅ Deposit RM300+ = +5 Reward Points\n\nAfter depositing on the website, submit below.\nAdmin will review and approve before Points are added.",
        "submit_deposit_100": "📤 Submit RM100+ Deposit",
        "submit_deposit_300": "📤 Submit RM300+ Deposit",
        "deposit_submitted": "⏳ Deposit mission submitted.\n\nAdmin will review your deposit.",
        "deposit_pending": "⏳ You already have a pending deposit mission.\n\nPlease wait for admin approval.",
        "deposit_already_claimed": "✅ You already claimed this mission today. Please try again tomorrow.",
        "deposit_invalid": "⚠️ Invalid deposit mission.",
        "deposit_approved_user": "✅ Daily Deposit Mission approved!\n\n💰 Deposit: RM{amount}+\n⭐ +{points} Reward Points added.",
        "deposit_rejected_user": "❌ Your Daily Deposit Mission was rejected.\n\n💰 Deposit: RM{amount}+",
        "join_channel": "📢 Join Channel",
        "join_group": "👥 Join Group",
        "done_join": "✅ Done Join",
        "mission_not_joined": "❌ Please join the Channel & Group first.\n\n⚠️ If you already joined but still cannot claim, make sure the bot is admin in the channel/group.",
        "claim_reward_text": "🎁 Claim Reward\n\n⭐ Your Points: {points}\n\n• RM1 Credit → 3 Points\n• RM5 Credit → 10 Points\n• RM10 Credit → 20 Points\n• RM25 Credit → 50 Points\n• RM50 Credit → 100 Points\n• Touch 'n Go RM100 → 200 Points\n\nChoose a reward below to submit redemption.\nAll rewards are subject to admin approval.",
        "not_enough_btn": "🔒 Not enough Points",
        "not_enough_text": "🔒 You do not have enough Points to unlock this reward yet.\n\nInvite friends and complete missions to collect more Points.",
        "gift_claimed": "✅ You already claimed the new join gift.",
        "gift_text": "🎁 New Member Gift\n\n✅ Register a new account\n✅ First deposit RM20+\n✅ Join Channel & Group first 😎\n\n🎁 Free Reward:\nRM38 Game Credit 💸",
        "claim_gift_btn": "🎁 Claim Gift",
        "gift_join_first": "❌ Please join Channel & Group first before claiming Free RM38.\n\n⚠️ If you already joined but still cannot claim, make sure the bot is admin in the channel/group.",
        "community_text": "🌐 JOMJUDI88 COMMUNITY\n\n📢 Official Updates\n👥 VIP Member Group\n🔞 Exclusive Amoi Content\n\n👇 Choose below:",
        "official_channel": "📢 Official Channel",
        "vip_group": "👥 VIP Group",
        "amoi_manja": "🔞 Amoi Manja",
        "support_text": "🟢 JOMJUDI88 SUPPORT\n\n⚡ Fast Response\n🔐 Secure & Private\n🎧 24/7 Live Assistance\n\nTap the link below to contact support.\n{url}",
        "unknown_button": "⚠️ Unknown button. Please press /start again.",
        "system_busy": "⚠️ System busy, please try again or press /start.",
        "daily_claimed": "❌ You already claimed today's reward.\n\n⏰ Please come back after 12AM Malaysia time.",
        "unlock_invites": "🔒 {title} unlocks at {invites} invites.",
        "reward_win": "🎉 Reward Unlocked!\n\n⭐ +{reward} Points added 🔥",
        "reward_zero": "😆 No reward this time\n\nTry again tomorrow 🔥",
        "mission_claimed": "✅ You already claimed mission rewards.",
        "mission_completed": "🎉 Mission Completed!\n\n⭐ +2 Points Added",
        "redeem_submitted": "⏳ Redeem request submitted.\n\nAdmin will review your request.",
        "redeem_pending": "⏳ You already have a pending request for this reward.\n\nPlease wait for admin approval.",
        "gift_submitted": "⏳ Gift request submitted.\n\nAdmin will review your request.",
        "gift_pending": "⏳ You already have a pending gift request.\n\nPlease wait for admin approval.",
        "approve_user_redeem": "✅ Your redeem request has been approved.\n\n🎁 Reward: {reward}",
        "reject_user_redeem": "❌ Your redeem request was rejected.\n\n🎁 Reward: {reward}",
        "approve_user_gift": "✅ Your RM38 Game Credit request has been approved.\n\n💸 RM38 game credit has been sent to your gaming account.",
        "reject_user_gift": "❌ Your new join gift request was rejected.",
    },
    "zh": {
        "language_title": "🌐 请选择你的语言\n\n🇲🇾 Bahasa Melayu\n🇬🇧 English\n🇨🇳 中文",
        "language_saved": "✅ 语言已成功设置。",
        "verify_button": "📱 验证马来西亚号码",
        "verify_placeholder": "点击验证你的号码",
        "verify_text": "🇲🇾 仅限马来西亚用户\n\n请先验证你的电话号码才可继续。\n\n点击下面按钮并分享你的 Telegram 联系方式。",
        "verify_own_phone": "❌ 请分享你自己的 Telegram 电话号码。",
        "verify_already": "✅ 你的号码已经验证过了。",
        "verify_malaysia_only": "❌ 只接受马来西亚电话号码 🇲🇾",
        "verify_duplicate": "❌ 这个电话号码已经注册过了。",
        "verify_success": "✅ 马来西亚号码验证成功。\n\n欢迎来到 JomJudi88 Rewards 🔥",
        "verify_busy": "⚠️ 验证系统繁忙，请再试一次。",
        "verify_manual_phone_reject": "❌ 请不要手写电话号码。\n\n请点击下面的 📱 验证马来西亚号码按钮，然后选择 Share Contact。\n\n如果看不到按钮，请点击 Telegram 左下角的键盘/Menu 图标。",
        "must_verify": "🇲🇾 仅限马来西亚用户\n\n请先按 /start 并验证电话号码。",
        "register": "🔐 注册",
        "earn": "💰 赚奖励",
        "gift": "🎁 免费 RM38",
        "checkin": "🎁 每日签到",
        "community": "🌐 社群",
        "support": "🎧 客服",
        "change_language": "🌐 更换语言",
        "back": "🔙 返回",
        "home": "🎁 𝗝𝗢𝗠𝗝𝗨𝗗𝗜𝟴𝟴 奖励 🔥\n\n💸 每天游戏并领取奖励\n🎯 累积积分兑换礼品\n👑 解锁 VIP 奖励\n💰 Touch ’n Go RM100\n\n⚡ 24/7 自动存款与提款\n🔐 客服与隐私有保障\n\n👇 请选择下面菜单 🚀",
        "your_rewards_btn": "💎 我的奖励",
        "share_earn_btn": "💰 分享赚钱",
        "missions_btn": "💰 每日充值任务",
        "jom_rewards_btn": "🌐 JOM Rewards",
        "claim_reward_btn": "🎁 兑换奖励",
        "menu_text": "💰 奖励中心\n\n🎁 完成任务\n🔥 解锁 VIP 奖励\n💸 每天收集积分并领取奖励\n\n💰 免费兑换 Touch'N Go RM100\n\n兑换奖励条件：\n\n• 必须拥有 JomJudi88 注册账号\n• 分享你的邀请链接到 Facebook / Telegram / 朋友\n  （1 个邀请 = 1 分）\n\n🎁 奖励：\n\n• 3 分 = RM1 游戏信用\n• 10 分 = RM5 游戏信用\n• 20 分 = RM10 游戏信用\n• 50 分 = RM25 游戏信用\n• 100 分 = RM50 游戏信用\n• 200 分 = Touch 'n Go RM100\n\n👇 请选择下面选项：",
        "profile_text": "💎 我的奖励\n\n⭐️ 奖励积分：{points}\n👥 已邀请朋友：{invites}\n\n----------------------------------------",
        "share_caption": "💰 分享赚更多！\n\n邀请朋友一起加入并领取奖励 🔥\n\n🔗 你的链接：\n\n{link}",
        "share_button": "📤 分享给朋友",
        "share_text": "加入 JomJudi88，一起领取奖励 🔥",
        "reward_center_text": "⏰ 每天马来西亚时间 12AM 重置\n\n━━━━━━━━━━━━━━\n\n🎁 幸运奖励\n🔓 所有人都能领取\n\n⭐️ 随机积分：\n+0 • +1\n\n━━━━━━━━━━━━━━\n\n🔥 VIP 奖励\n🔒 邀请 5 人解锁\n\n⭐️ 更好奖励：\n+0 • +1 • +3\n\n━━━━━━━━━━━━━━\n\n👑 Elite 奖励\n🔒 邀请 20 人解锁\n\n💎 大奖励：\n+0 • +1 • +5",
        "lucky_reward": "🎁 幸运奖励",
        "vip_reward": "🔥 VIP 奖励",
        "elite_reward": "👑 Elite 奖励",
        "missions_text": "💰 每日充值任务\n\n✅ 今日充值 RM100+ = +2 奖励积分\n✅ 今日充值 RM300+ = +5 奖励积分\n\n在网站充值后，点击下面提交审核。\n管理员审核通过后才会加积分。",
        "submit_deposit_100": "📤 提交 RM100+ 充值",
        "submit_deposit_300": "📤 提交 RM300+ 充值",
        "deposit_submitted": "⏳ 充值任务已提交。\n\n管理员将会审核你的充值。",
        "deposit_pending": "⏳ 你已经有一个待审核的充值任务。\n\n请等待管理员审核。",
        "deposit_already_claimed": "✅ 你今天已经领取过这个任务，请明天再试。",
        "deposit_invalid": "⚠️ 无效的充值任务。",
        "deposit_approved_user": "✅ 每日充值任务审核通过！\n\n💰 充值：RM{amount}+\n⭐ +{points} 奖励积分已加入。",
        "deposit_rejected_user": "❌ 你的每日充值任务被拒绝。\n\n💰 充值：RM{amount}+",
        "join_channel": "📢 加入频道",
        "join_group": "👥 加入群组",
        "done_join": "✅ 已完成加入",
        "mission_not_joined": "❌ 请先加入 Channel 和 Group。\n\n⚠️ 如果你已经加入但还是不能领取，请确认 bot 已经是 channel/group 的 admin。",
        "claim_reward_text": "🎁 兑换奖励\n\n⭐ 你的积分：{points}\n\n• RM1 Credit → 3 Points\n• RM5 Credit → 10 Points\n• RM10 Credit → 20 Points\n• RM25 Credit → 50 Points\n• RM50 Credit → 100 Points\n• Touch 'n Go RM100 → 200 Points\n\n请选择下方奖励提交兑换申请。\n所有奖励需要管理员审核。",
        "not_enough_btn": "🔒 积分不足",
        "not_enough_text": "🔒 你的积分还不够，暂时不能解锁这个奖励。\n\n邀请朋友并完成任务来赚取更多积分。",
        "gift_claimed": "✅ 你已经领取过新会员礼物。",
        "gift_text": "🎁 新会员礼物\n\n✅ 注册新账号\n✅ 首次存款 RM20+\n✅ 先加入 Channel 和 Group 😎\n\n🎁 免费奖励：\nRM38 游戏信用 💸",
        "claim_gift_btn": "🎁 领取礼物",
        "gift_join_first": "❌ 请先加入 Channel 和 Group 才能领取免费 RM38。\n\n⚠️ 如果你已经加入但还是不能领取，请确认 bot 已经是 channel/group 的 admin。",
        "community_text": "🌐 JOMJUDI88 社群\n\n📢 官方更新\n👥 VIP 会员群\n🔞 独家 Amoi 内容\n\n👇 请选择：",
        "official_channel": "📢 官方频道",
        "vip_group": "👥 VIP 群组",
        "amoi_manja": "🔞 Amoi Manja",
        "support_text": "🟢 JOMJUDI88 客服\n\n⚡ 快速回复\n🔐 安全与隐私\n🎧 24/7 在线协助\n\n点击下面链接联系客服。\n{url}",
        "unknown_button": "⚠️ 未知按钮，请重新按 /start。",
        "system_busy": "⚠️ 系统繁忙，请再试一次或按 /start。",
        "daily_claimed": "❌ 你今天已经领取过奖励。\n\n⏰ 请在马来西亚时间 12AM 后再回来。",
        "unlock_invites": "🔒 {title} 需要邀请 {invites} 人解锁。",
        "reward_win": "🎉 奖励已开启！\n\n⭐ +{reward} 积分已加入 🔥",
        "reward_zero": "😆 这次没有中奖\n\n明天再试 🔥",
        "mission_claimed": "✅ 你已经领取过任务奖励。",
        "mission_completed": "🎉 任务完成！\n\n⭐ +2 积分已加入",
        "redeem_submitted": "⏳ 兑换申请已提交。\n\n管理员将会审核你的申请。",
        "redeem_pending": "⏳ 你已经有这个奖励的待审核申请。\n\n请等待管理员批准。",
        "gift_submitted": "⏳ 礼物申请已提交。\n\n管理员将会审核你的申请。",
        "gift_pending": "⏳ 你已经有待审核的礼物申请。\n\n请等待管理员批准。",
        "approve_user_redeem": "✅ 你的兑换申请已通过。\n\n🎁 奖励：{reward}",
        "reject_user_redeem": "❌ 你的兑换申请被拒绝。\n\n🎁 奖励：{reward}",
        "approve_user_gift": "✅ 你的 RM38 游戏信用申请已通过。\n\n💸 RM38 游戏信用已经发送到你的游戏账号。",
        "reject_user_gift": "❌ 你的新会员礼物申请被拒绝。",
    },
}


def lang_of(user_id) -> str:
    return get_user_language(user_id)


def tr(user_id, key, **kwargs):
    lang = lang_of(user_id)
    value = TEXT.get(lang, TEXT["ms"]).get(key, TEXT["ms"].get(key, key))
    try:
        return value.format(**kwargs)
    except Exception:
        return value


def tr_lang(lang, key, **kwargs):
    lang = lang if lang in ["ms", "en", "zh"] else "ms"
    value = TEXT.get(lang, TEXT["ms"]).get(key, TEXT["ms"].get(key, key))
    try:
        return value.format(**kwargs)
    except Exception:
        return value


def get_language_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇲🇾 Bahasa Melayu", callback_data="lang_ms")],
        [InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")],
        [InlineKeyboardButton("🇨🇳 中文", callback_data="lang_zh")],
    ])


def get_language_text():
    return TEXT["ms"]["language_title"]


def get_verify_keyboard(user_id=None):
    return ReplyKeyboardMarkup(
        [[KeyboardButton(tr(user_id, "verify_button") if user_id else "📱 Verify Malaysia Number", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder=tr(user_id, "verify_placeholder") if user_id else "Tap to verify your number",
    )


def get_main_keyboard(user_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(tr(user_id, "register"), url=REGISTER_URL),
            InlineKeyboardButton(tr(user_id, "earn"), callback_data="menu"),
        ],
        [
            InlineKeyboardButton(tr(user_id, "gift"), callback_data="gift"),
            InlineKeyboardButton(tr(user_id, "checkin"), callback_data="reward_center"),
        ],
        [
            InlineKeyboardButton(tr(user_id, "community"), callback_data="community"),
            InlineKeyboardButton(tr(user_id, "support"), callback_data="support"),
        ],
        [
            InlineKeyboardButton(tr(user_id, "change_language"), callback_data="change_language"),
        ],
    ])


def get_main_text(user_id):
    return tr(user_id, "home")


def kb_back_menu(user_id=None):
    return InlineKeyboardMarkup([[InlineKeyboardButton(tr(user_id, "back") if user_id else "🔙 Back", callback_data="menu")]])


def kb_back_home(user_id=None):
    return InlineKeyboardMarkup([[InlineKeyboardButton(tr(user_id, "back") if user_id else "🔙 Back", callback_data="back")]])


def kb_back_reward_center(user_id=None):
    return InlineKeyboardMarkup([[InlineKeyboardButton(tr(user_id, "back") if user_id else "🔙 Back", callback_data="reward_center")]])


def get_share_earn_caption(user_id: str) -> str:
    link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
    return tr(user_id, "share_caption", link=link)


def get_share_earn_keyboard(user_id: str):
    link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
    share_url = f"https://t.me/share/url?url={quote_plus(link)}&text={quote_plus(tr(user_id, 'share_text'))}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(tr(user_id, "share_button"), url=share_url)],
        [InlineKeyboardButton(tr(user_id, "back"), callback_data="menu")],
    ])


async def send_share_earn_page(query, user_id: str):
    caption = get_share_earn_caption(user_id)
    keyboard = get_share_earn_keyboard(user_id)

    photo_source = None
    opened_file = None
    try:
        if SHARE_BANNER_FILE_ID:
            photo_source = SHARE_BANNER_FILE_ID
        elif SHARE_BANNER_PATH and os.path.exists(SHARE_BANNER_PATH):
            opened_file = open(SHARE_BANNER_PATH, "rb")
            photo_source = opened_file

        if photo_source:
            try:
                await query.edit_message_media(
                    media=InputMediaPhoto(media=photo_source, caption=caption),
                    reply_markup=keyboard,
                )
                return
            except BadRequest as e:
                logger.warning("edit share earn media failed, fallback send_photo: %s", e)

            try:
                await query.message.reply_photo(
                    photo=photo_source,
                    caption=caption,
                    reply_markup=keyboard,
                )
                return
            except Exception as e:
                logger.warning("send share earn photo failed, fallback text: %s", e)

        await safe_edit(query, caption, keyboard)

    finally:
        if opened_file:
            try:
                opened_file.close()
            except Exception:
                pass


async def safe_send_user(context: ContextTypes.DEFAULT_TYPE, user_id: str, text: str):
    try:
        await context.bot.send_message(chat_id=int(user_id), text=text)
    except Forbidden:
        logger.warning("Cannot message user %s: bot blocked or user unavailable", user_id)
    except Exception as e:
        logger.warning("Failed to notify user %s: %s", user_id, e)


async def safe_reply(query, text, reply_markup=None):
    try:
        await query.message.reply_text(text=text, reply_markup=reply_markup)
    except Exception as e:
        logger.exception("safe_reply error: %s", e)


async def safe_edit(query, text, reply_markup=None):
    """Never let a callback silently die. If edit fails, reply with a new message."""
    try:
        msg = query.message
        if msg and msg.photo:
            try:
                await query.edit_message_caption(caption=text[:1024], reply_markup=reply_markup)
                return
            except BadRequest as e:
                logger.warning("edit caption failed, fallback text: %s", e)

        try:
            await query.edit_message_text(text=text, reply_markup=reply_markup)
            return
        except BadRequest as e:
            if "message is not modified" in str(e).lower():
                return
            logger.warning("edit text failed, fallback reply: %s", e)

        await safe_reply(query, text, reply_markup)
    except RetryAfter as e:
        logger.warning("Telegram rate limit, retry after %s", e.retry_after)
        await safe_reply(query, "⚠️ Too many requests. Please try again in a few seconds.", reply_markup)
    except (TimedOut, NetworkError) as e:
        logger.warning("Telegram network error: %s", e)
        await safe_reply(query, "⚠️ Network busy. Please press again.", reply_markup)
    except Exception as e:
        logger.exception("SAFE_EDIT_ERROR: %s", e)
        try:
            await safe_reply(query, text, reply_markup)
        except Exception:
            pass


async def send_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Optimized home sender using Telegram file_id cache.
    Much faster than re-uploading banner.jpg every /start.
    """

    try:
        # FAST MODE: use Telegram CDN file_id
        if HOME_BANNER_FILE_ID:
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=HOME_BANNER_FILE_ID,
                caption=get_main_text(update.effective_user.id),
                reply_markup=get_main_keyboard(update.effective_user.id),
            )
            return

        # FIRST TIME MODE: upload local image once
        if os.path.exists("banner.jpg"):
            with open("banner.jpg", "rb") as photo:
                msg = await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=photo,
                    caption=get_main_text(update.effective_user.id),
                    reply_markup=get_main_keyboard(update.effective_user.id),
                )

                # IMPORTANT:
                # Copy this file_id into HOME_BANNER_FILE_ID env
                try:
                    file_id = msg.photo[-1].file_id
                    logger.info("HOME_BANNER_FILE_ID=%s", file_id)
                    print("\n==============================")
                    print("COPY THIS FILE_ID:")
                    print(file_id)
                    print("==============================\n")
                except Exception:
                    pass

            return

        # fallback text mode
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=get_main_text(update.effective_user.id),
            reply_markup=get_main_keyboard(update.effective_user.id),
        )

    except Exception as e:
        logger.warning("Banner/send_photo error, fallback send_message: %s", e)

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=get_main_text(update.effective_user.id),
            reply_markup=get_main_keyboard(update.effective_user.id),
        )
    except Exception as e:
        logger.warning("Banner/send_photo error, fallback send_message: %s", e)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=get_main_text(update.effective_user.id),
            reply_markup=get_main_keyboard(update.effective_user.id),
        )


async def request_phone_verification(update: Update):
    user_id = str(update.effective_user.id) if update.effective_user else None
    await update.effective_message.reply_text(
        tr(user_id, "verify_text"),
        reply_markup=get_verify_keyboard(user_id),
    )


async def contact_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        contact = update.effective_message.contact
        if not contact:
            return

        user = update.effective_user
        user_id = str(user.id)
        user_name = user.first_name or "User"

        # Contact verification should not use START_LOCK.
        # Users often tap /start and then immediately share contact.
        ensure_user(user_id, user_name)

        if contact.user_id and str(contact.user_id) != user_id:
            await update.effective_message.reply_text(
                tr(user_id, "verify_own_phone"),
                reply_markup=get_verify_keyboard(user_id),
            )
            return

        current_user = get_user(user_id)
        if current_user and safe_int(current_user.get("phone_verified", 0)) == 1:
            await update.effective_message.reply_text(
                tr(user_id, "verify_already"),
                reply_markup=ReplyKeyboardRemove(),
            )
            await send_home(update, context)
            return

        phone = normalize_phone(contact.phone_number)
        if not phone.startswith("+60"):
            audit_log(user_id, "phone_verify_rejected", f"phone={phone}")
            await update.effective_message.reply_text(
                tr(user_id, "verify_malaysia_only"),
                reply_markup=get_verify_keyboard(user_id),
            )
            return

        existing_phone = db_fetchone(
            "SELECT user_id FROM users WHERE phone_number=%s AND phone_verified=1 LIMIT 1",
            (phone,),
        )
        if existing_phone and str(existing_phone.get("user_id")) != user_id:
            audit_log(user_id, "phone_duplicate_rejected", f"phone={phone}")
            await update.effective_message.reply_text(
                tr(user_id, "verify_duplicate"),
                reply_markup=get_verify_keyboard(user_id),
            )
            return

        db_execute(
            """
            UPDATE users
            SET name=%s,
                phone_number=%s,
                phone_verified=1,
                phone_verified_at=%s,
                last_seen_at=%s
            WHERE user_id=%s
            """,
            (user_name, phone, now_iso(), now_iso(), user_id),
        )
        audit_log(user_id, "phone_verified", f"phone={phone}")
        reward_referrer_if_needed(user_id)

        for admin in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=int(admin),
                    text=(
                        "✅ New Malaysia Verify\n\n"
                        f"👤 User: {user_name}\n"
                        f"🆔 User ID: {user_id}\n"
                        f"📞 Phone: {phone}"
                    ),
                )
            except Exception as e:
                logger.warning("Send phone verify notice to admin %s failed: %s", admin, e)

        await update.effective_message.reply_text(
            tr(user_id, "verify_success"),
            reply_markup=ReplyKeyboardRemove(),
        )
        await send_home(update, context)

    except Exception as e:
        logger.exception("CONTACT_HANDLER_ERROR: %s", e)
        try:
            await update.effective_message.reply_text(tr(str(update.effective_user.id), "verify_busy") if update.effective_user else "⚠️ Verification system busy. Please try again.")
        except Exception:
            pass






async def text_phone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles users who type their phone number manually instead of pressing the
    Telegram contact-share button. Manual text cannot prove ownership, so we
    guide them back to the proper Verify Malaysia Number button.
    """
    try:
        if not update.effective_user or not update.effective_message:
            return

        user_id = str(update.effective_user.id)
        user_name = update.effective_user.first_name or "User"
        ensure_user(user_id, user_name)

        if is_phone_verified(user_id):
            return

        text = (update.effective_message.text or "").strip()
        if not text:
            return

        # Any typed message before verification should guide the user to share
        # Telegram contact. This covers typed numbers like 013xxxxxxx.
        await update.effective_message.reply_text(
            tr(user_id, "verify_manual_phone_reject"),
            reply_markup=get_verify_keyboard(user_id),
        )

    except Exception as e:
        logger.exception("TEXT_PHONE_HANDLER_ERROR: %s", e)
        try:
            await update.effective_message.reply_text(
                tr(str(update.effective_user.id), "verify_busy") if update.effective_user else "⚠️ Verification system busy. Please try again."
            )
        except Exception:
            pass


# ================= ADMIN PANEL UI =================

def get_admin_panel_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Stats", callback_data="admin_stats"),
            InlineKeyboardButton("👥 All Users", callback_data="admin_users"),
        ],
        [
            InlineKeyboardButton("🏆 Top Users", callback_data="admin_top"),
            InlineKeyboardButton("📱 Phones", callback_data="admin_phones"),
        ],
        [
            InlineKeyboardButton("🎁 Pending Redeem", callback_data="admin_redeems"),
            InlineKeyboardButton("🎁 Pending Gift", callback_data="admin_gifts"),
        ],
        [
            InlineKeyboardButton("💰 Pending Deposit", callback_data="admin_deposits"),
        ],
        [
            InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast_help"),
            InlineKeyboardButton("💰 Add Points", callback_data="admin_addpoints_help"),
        ],
        [
            InlineKeyboardButton("🔄 Reset Reward", callback_data="admin_reset_help"),
        ],
    ])


def kb_admin_back():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]])


def get_admin_panel_text():
    stats = get_stats() or {}
    redeem_count = db_fetchone("SELECT COUNT(*) AS c FROM redeem_requests WHERE status='pending'") or {}
    gift_count = db_fetchone("SELECT COUNT(*) AS c FROM gift_requests WHERE status='pending'") or {}
    deposit_count = db_fetchone("SELECT COUNT(*) AS c FROM deposit_mission_requests WHERE status='pending'") or {}
    verified_count = db_fetchone("SELECT COUNT(*) AS c FROM users WHERE phone_verified=1") or {}

    return (
        "🛠 JomJudi88 Admin Panel\n\n"
        f"👥 Total Users: {stats.get('total_users', 0)}\n"
        f"📱 Verified Users: {verified_count.get('c', 0)}\n"
        f"⭐ Total Points: {stats.get('total_points', 0)}\n"
        f"👥 Total Invites: {stats.get('total_invites', 0)}\n"
        f"⏳ Pending Redeem: {redeem_count.get('c', 0)}\n"
        f"⏳ Pending Gift: {gift_count.get('c', 0)}\n"
        f"⏳ Pending Deposit: {deposit_count.get('c', 0)}\n\n"
        "👇 Choose an admin action below:"
    )


def format_admin_users(limit=30):
    rows = get_all_users()[:int(limit)]
    if not rows:
        return "👥 All Users\n\nNo users found."

    lines = [f"👥 All Users - Latest Top {len(rows)}\n"]
    for i, row in enumerate(rows, start=1):
        verified = "✅" if safe_int(row.get("phone_verified", 0)) == 1 else "❌"
        lines.append(
            f"{i}. {row.get('name') or 'User'}\n"
            f"ID: {row.get('user_id')}\n"
            f"⭐ {row.get('points', 0)} points | 👥 {row.get('invited_count', 0)} invites\n"
            f"💰 RM100: {row.get('rm100_count', 0)}x | RM300: {row.get('rm300_count', 0)}x\n"
            f"🎁 Check-in: {row.get('checkin_streak', 0)} days | Last: {(row.get('last_checkin_at') or '-')[:10]}\n"
            f"📱 {row.get('phone_number') or 'Not verified'} {verified}\n"
        )
    return "\n".join(lines)


def format_admin_top_users():
    rows = get_top_invites()
    if not rows:
        return "🏆 Top Invite Ranking\n\nNo ranking yet."

    lines = ["🏆 Top Invite Ranking\n"]
    for i, row in enumerate(rows, start=1):
        lines.append(
            f"{i}. {row.get('name') or 'User'}\n"
            f"⭐ {row.get('points', 0)} points\n"
            f"👥 {row.get('invited_count', 0)} invites\n"
        )
    return "\n".join(lines)


def format_admin_phones(limit=30):
    rows = get_verified_phones(int(limit))
    if not rows:
        return "📱 Verified Malaysia Numbers\n\nNo verified Malaysia numbers yet."

    lines = [f"📱 Verified Malaysia Numbers - Latest {len(rows)}\n"]
    for i, row in enumerate(rows, start=1):
        lines.append(
            f"{i}. {row.get('name') or 'User'}\n"
            f"ID: {row.get('user_id')}\n"
            f"📞 {row.get('phone_number')}\n"
            f"✅ {row.get('phone_verified_at') or '-'}\n"
        )
    return "\n".join(lines)


def get_admin_pending_redeem_markup(rows):
    buttons = []
    for r in rows[:10]:
        rid = r.get("id")
        reward = str(r.get("reward_text") or "Reward")[:20]
        buttons.append([
            InlineKeyboardButton(f"✅ #{rid} {reward}", callback_data=f"approve_redeem:{rid}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_redeem:{rid}"),
        ])
    buttons.append([InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(buttons)


def get_admin_pending_gift_markup(rows):
    buttons = []
    for r in rows[:10]:
        uid = r.get("user_id")
        name = str(r.get("username") or uid)[:20]
        buttons.append([
            InlineKeyboardButton(f"✅ {name}", callback_data=f"approve_gift:{uid}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_gift:{uid}"),
        ])
    buttons.append([InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(buttons)


def format_pending_redeems_for_panel(rows):
    if not rows:
        return "🎁 Pending Redeem\n\n✅ No pending redeem requests."

    lines = ["🎁 Pending Redeem Requests\n"]
    for r in rows[:10]:
        lines.append(
            f"📝 ID: {r.get('id')}\n"
            f"👤 User: {r.get('user_id')} | {r.get('username')}\n"
            f"📱 Phone: {get_user_phone(r.get('user_id'))}\n"
            f"🎁 Reward: {r.get('reward_text')}\n"
            f"⭐ Points: {r.get('points_needed')}\n"
        )
    return "\n".join(lines)


def format_pending_gifts_for_panel(rows):
    if not rows:
        return "🎁 Pending Gift\n\n✅ No pending gift requests."

    lines = ["🎁 Pending Gift Requests\n"]
    for r in rows[:10]:
        lines.append(
            f"📝 ID: {r.get('id')}\n"
            f"👤 User: {r.get('user_id')} | {r.get('username')}\n"
            f"📱 Phone: {get_user_phone(r.get('user_id'))}\n"
            f"🕒 {r.get('created_at')}\n"
        )
    return "\n".join(lines)


def get_admin_pending_deposit_markup(rows):
    buttons = []
    for r in rows[:10]:
        rid = r.get("id")
        amount = r.get("deposit_amount")
        points = r.get("reward_points")
        buttons.append([
            InlineKeyboardButton(f"✅ #{rid} RM{amount}+", callback_data=f"approve_deposit:{rid}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_deposit:{rid}"),
        ])
    buttons.append([InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(buttons)


def format_pending_deposits_for_panel(rows):
    if not rows:
        return "💰 Pending Deposit Mission\n\n✅ No pending deposit mission requests."

    lines = ["💰 Pending Deposit Mission Requests\n"]
    for r in rows[:10]:
        lines.append(
            f"📝 ID: {r.get('id')}\n"
            f"👤 User: {r.get('user_id')} | {r.get('username')}\n"
            f"📱 Phone: {get_user_phone(r.get('user_id'))}\n"
            f"💰 Deposit: RM{r.get('deposit_amount')}+\n"
            f"⭐ Reward: +{r.get('reward_points')} Points\n"
            f"🕒 {r.get('created_at')}\n"
        )
    return "\n".join(lines)

# ================= START / JOIN CHECK =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.effective_user
        user_id = str(user.id)
        user_name = user.first_name or "User"

        # Prevent spam multiple /start clicks
        current_time = time.time()
        last_start = START_LOCK.get(user_id, 0)

        if current_time - last_start < 2:
            return

        START_LOCK[user_id] = current_time

        referrer_id = str(context.args[0]).strip() if context.args else None
        existing = get_user(user_id)

        if not existing:
            valid_referrer = None
            if referrer_id and referrer_id != user_id:
                ref_user = get_user(referrer_id)
                if ref_user:
                    valid_referrer = referrer_id

            create_user(user_id, user_name, valid_referrer)
            audit_log(user_id, "start_new_user", f"referrer={valid_referrer or ''}")
            # Referral point is credited only after the new user verifies a Malaysia phone number.
        else:
            create_user(user_id, user_name, existing.get("referrer_id"))
            audit_log(user_id, "start_existing_user", "")

        current_user = get_user(user_id)
        if not current_user or not current_user.get("language"):
            await update.effective_message.reply_text(
                get_language_text(),
                reply_markup=get_language_keyboard(),
            )
            return

        if not is_phone_verified(user_id):
            await request_phone_verification(update)
            return

        await send_home(update, context)
    except Exception as e:
        logger.exception("START ERROR: %s", e)
        try:
            await update.effective_message.reply_text("⚠️ Bot temporarily busy. Please press /start again.")
        except Exception:
            pass


async def is_user_joined(chat_id, user_id, context) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, int(user_id))
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logger.warning("Join check failed chat=%s user=%s error=%s", chat_id, user_id, e)
        return False


# ================= CALLBACK BUTTONS =================

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = "unknown"

    try:
        try:
            await query.answer(cache_time=0)
        except Exception as e:
            logger.warning("Callback answer error: %s", e)

        user_id = str(query.from_user.id)
        user_name = query.from_user.first_name or "User"
        data = query.data or ""

        clean_callback_cache()
        if not callback_allowed(user_id):
            try:
                await query.answer("Please wait a moment...", show_alert=False)
            except Exception:
                pass
            return

        user = ensure_user(user_id, user_name)

        if data.startswith("lang_"):
            selected_lang = data.replace("lang_", "", 1)
            if selected_lang not in ["ms", "en", "zh"]:
                selected_lang = "ms"
            set_user_language(user_id, selected_lang)
            await safe_edit(query, tr_lang(selected_lang, "language_saved"))
            if not is_phone_verified(user_id):
                await request_phone_verification(update)
                return
            await send_home(update, context)
            return

        if data == "change_language":
            await safe_edit(query, get_language_text(), get_language_keyboard())
            return

        if not is_admin(user_id) and not (user.get("language") or ""):
            await safe_edit(query, get_language_text(), get_language_keyboard())
            return

        if not is_admin(user_id) and safe_int(user.get("phone_verified", 0)) != 1:
            await safe_reply(
                query,
tr(user_id, "must_verify")
            )
            return

        if data.startswith("admin_"):
            if not is_admin(user_id):
                await safe_edit(query, "❌ Admin only.")
                return

            if data == "admin_panel":
                await safe_edit(query, get_admin_panel_text(), get_admin_panel_keyboard())

            elif data == "admin_stats":
                await safe_edit(query, get_admin_panel_text(), kb_admin_back())

            elif data == "admin_users":
                await safe_edit(query, format_admin_users(30), kb_admin_back())

            elif data == "admin_top":
                await safe_edit(query, format_admin_top_users(), kb_admin_back())

            elif data == "admin_phones":
                await safe_edit(query, format_admin_phones(30), kb_admin_back())

            elif data == "admin_redeems":
                rows = get_pending_redeems(10)
                await safe_edit(query, format_pending_redeems_for_panel(rows), get_admin_pending_redeem_markup(rows) if rows else kb_admin_back())

            elif data == "admin_gifts":
                rows = get_pending_gifts(10)
                await safe_edit(query, format_pending_gifts_for_panel(rows), get_admin_pending_gift_markup(rows) if rows else kb_admin_back())

            elif data == "admin_deposits":
                rows = get_pending_deposit_missions(10)
                await safe_edit(query, format_pending_deposits_for_panel(rows), get_admin_pending_deposit_markup(rows) if rows else kb_admin_back())

            elif data == "admin_broadcast_help":
                await safe_edit(
                    query,
                    "📢 Broadcast\n\nUse command:\n/broadcast your message\n\nExample:\n/broadcast 🎁 Daily Reward Reset! Claim now 🔥",
                    kb_admin_back(),
                )

            elif data == "admin_addpoints_help":
                await safe_edit(
                    query,
                    "💰 Add Points\n\nUse command:\n/addpoints USER_ID POINTS\n\nExample:\n/addpoints 123456789 10",
                    kb_admin_back(),
                )

            elif data == "admin_reset_help":
                await safe_edit(
                    query,
                    "🔄 Reset Reward Cooldown\n\nUse command:\n/resetreward USER_ID\n\nExample:\n/resetreward 123456789",
                    kb_admin_back(),
                )

            else:
                await safe_edit(query, "⚠️ Unknown admin action.", kb_admin_back())
            return

        if data == "menu":
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(tr(user_id, "your_rewards_btn"), callback_data="profile")],
                [InlineKeyboardButton(tr(user_id, "share_earn_btn"), callback_data="link")],
                [InlineKeyboardButton(tr(user_id, "missions_btn"), callback_data="missions")],
                [InlineKeyboardButton(tr(user_id, "jom_rewards_btn"), url=JOM_REWARDS_URL)],
                [InlineKeyboardButton(tr(user_id, "claim_reward_btn"), callback_data="redeem_menu")],
                [InlineKeyboardButton(tr(user_id, "back"), callback_data="back")],
            ])
            await safe_edit(query, tr(user_id, "menu_text"), keyboard)

        elif data == "profile":
            user = get_user(user_id) or user
            await safe_edit(query, tr(user_id, "profile_text", points=user.get("points", 0), invites=user.get("invited_count", 0)), kb_back_menu(user_id))

        elif data == "link":
            await send_share_earn_page(query, user_id)

        elif data == "reward_center":
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(tr(user_id, "lucky_reward"), callback_data="lucky_reward")],
                [InlineKeyboardButton(tr(user_id, "vip_reward"), callback_data="vip_reward")],
                [InlineKeyboardButton(tr(user_id, "elite_reward"), callback_data="elite_reward")],
                [InlineKeyboardButton(tr(user_id, "back"), callback_data="back")],
            ])
            await safe_edit(query, tr(user_id, "reward_center_text"), keyboard)

        elif data == "lucky_reward":
            ok, msg = claim_daily_reward(user_id, "lucky", 0, [(0, 40), (1, 60)])
            await safe_edit(query, msg, kb_back_reward_center(user_id))

        elif data == "vip_reward":
            ok, msg = claim_daily_reward(user_id, "vip", 5, [(0, 30), (1, 65), (3, 5)])
            await safe_edit(query, msg, kb_back_reward_center(user_id))

        elif data == "elite_reward":
            ok, msg = claim_daily_reward(user_id, "elite", 20, [(0, 30), (1, 67), (5, 3)])
            await safe_edit(query, msg, kb_back_reward_center(user_id))

        elif data == "missions":
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(tr(user_id, "submit_deposit_100"), callback_data="submit_deposit:100:2")],
                [InlineKeyboardButton(tr(user_id, "submit_deposit_300"), callback_data="submit_deposit:300:5")],
                [InlineKeyboardButton(tr(user_id, "back"), callback_data="menu")],
            ])
            await safe_edit(query, tr(user_id, "missions_text"), keyboard)

        elif data.startswith("submit_deposit:"):
            try:
                _, amount, points_reward = data.split(":", 2)
                amount = int(amount)
                points_reward = int(points_reward)
            except Exception:
                await safe_edit(query, tr(user_id, "deposit_invalid"), kb_back_menu(user_id))
                return

            username = query.from_user.username
            username_text = f"@{username}" if username else "No Username"
            ok, msg, request_id = create_deposit_mission_request_locked(user_id, username_text, amount, points_reward)

            if ok and request_id:
                phone_text = get_user_phone(user_id)
                for admin in ADMIN_IDS:
                    admin_keyboard = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("✅ Approve", callback_data=f"approve_deposit:{request_id}"),
                            InlineKeyboardButton("❌ Reject", callback_data=f"reject_deposit:{request_id}"),
                        ]
                    ])
                    try:
                        await context.bot.send_message(
                            chat_id=int(admin),
                            text=(
                                "💰 New Daily Deposit Mission Request\n\n"
                                f"👤 Username: {username_text}\n"
                                f"🆔 User ID: {user_id}\n"
                                f"📱 Phone: {phone_text}\n"
                                f"💰 Deposit Mission: RM{amount}+\n"
                                f"⭐ Reward Points: +{points_reward}\n"
                                f"📝 Request ID: {request_id}\n\n"
                                "Please verify the customer's website deposit before approving."
                            ),
                            reply_markup=admin_keyboard,
                        )
                    except Exception as e:
                        logger.warning("Send deposit mission request to admin %s failed: %s", admin, e)
            await safe_edit(query, msg, kb_back_menu(user_id))

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
                if points >= pts or is_admin(user_id):
                    keyboard_rows.append([
                        InlineKeyboardButton(
                            f"🔓 💰 {reward_text} — {pts} Points",
                            callback_data=f"redeem:{pts}:{reward_text}",
                        )
                    ])
                else:
                    keyboard_rows.append([
                        InlineKeyboardButton(
                            f"🔒 💰 {reward_text} — {pts} Points",
                            callback_data="not_enough_points",
                        )
                    ])
            keyboard_rows.append([InlineKeyboardButton(tr(user_id, "back"), callback_data="menu")])
            await safe_edit(query, tr(user_id, "claim_reward_text", points=points), InlineKeyboardMarkup(keyboard_rows))

        elif data == "not_enough_points":
            await safe_edit(query, tr(user_id, "not_enough_text"), kb_back_menu(user_id))

        elif data.startswith("redeem:"):
            try:
                _, pts, reward_text = data.split(":", 2)
                pts = int(pts)
            except Exception:
                await safe_edit(query, "⚠️ Invalid redeem request.", kb_back_menu(user_id))
                return

            username = query.from_user.username
            username_text = f"@{username}" if username else "No Username"
            ok, msg, request_id = create_redeem_request_locked(user_id, username_text, reward_text, pts)

            if ok and request_id:
                user_latest = get_user(user_id) or user
                phone_text = user_latest.get("phone_number") or "Not verified"
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
                                f"📱 Phone: {phone_text}\n"
                                f"🎁 Reward: {reward_text}\n"
                                f"⭐ Points Needed: {pts}\n"
                                f"⭐ User Current Points: {user_latest.get('points', 0)}\n"
                                f"📝 Request ID: {request_id}"
                            ),
                            reply_markup=admin_keyboard,
                        )
                    except Exception as e:
                        logger.warning("Send redeem request to admin %s failed: %s", admin, e)
            await safe_edit(query, msg, kb_back_menu(user_id))

        elif data.startswith("approve_redeem:"):
            if not is_admin(user_id):
                await safe_edit(query, "❌ Admin only.")
                return
            request_id = int(data.split(":")[1])
            success, message, target_user, reward_text = approve_redeem_request(request_id, user_id)
            await safe_edit(query, message)
            if success and target_user:
                await safe_send_user(context, target_user, tr(target_user, "approve_user_redeem", reward=reward_text))

        elif data.startswith("reject_redeem:"):
            if not is_admin(user_id):
                await safe_edit(query, "❌ Admin only.")
                return
            request_id = int(data.split(":")[1])
            success, message, target_user, reward_text = reject_redeem_request(request_id, user_id)
            await safe_edit(query, message)
            if success and target_user:
                await safe_send_user(context, target_user, tr(target_user, "reject_user_redeem", reward=reward_text))

        elif data.startswith("approve_deposit:"):
            if not is_admin(user_id):
                await safe_edit(query, "❌ Admin only.")
                return
            request_id = int(data.split(":")[1])
            success, message, target_user, amount, points_reward = approve_deposit_mission_request(request_id, user_id)
            await safe_edit(query, message)
            if success and target_user:
                await safe_send_user(context, target_user, tr(target_user, "deposit_approved_user", amount=amount, points=points_reward))

        elif data.startswith("reject_deposit:"):
            if not is_admin(user_id):
                await safe_edit(query, "❌ Admin only.")
                return
            request_id = int(data.split(":")[1])
            success, message, target_user, amount, points_reward = reject_deposit_mission_request(request_id, user_id)
            await safe_edit(query, message)
            if success and target_user:
                await safe_send_user(context, target_user, tr(target_user, "deposit_rejected_user", amount=amount, points=points_reward))

        elif data == "gift":
            user = get_user(user_id) or user
            if safe_int(user.get("gift_claimed", 0)) == 1 and not is_admin(user_id):
                await safe_edit(query, tr(user_id, "gift_claimed"), kb_back_home(user_id))
                return
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(tr(user_id, "claim_gift_btn"), callback_data="claim_gift")],
                [InlineKeyboardButton(tr(user_id, "back"), callback_data="back")],
            ])
            await safe_edit(query, tr(user_id, "gift_text"), keyboard)

        elif data == "claim_gift":
            joined_channel = await is_user_joined(CHANNEL_ID, user_id, context)
            joined_group = await is_user_joined(GROUP_ID, user_id, context)

            if not joined_channel or not joined_group:
                await safe_edit(query, tr(user_id, "gift_join_first"), kb_back_home(user_id))
                return

            username = query.from_user.username
            username_text = f"@{username}" if username else "No Username"
            ok, msg, request_id = create_gift_request_locked(user_id, username_text)
            if ok:
                phone_text = get_user_phone(user_id)
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
                                f"🆔 User ID: {user_id}\n"
                                f"📱 Phone: {phone_text}\n"
                                f"📝 Request ID: {request_id}"
                            ),
                            reply_markup=admin_keyboard,
                        )
                    except Exception as e:
                        logger.warning("Send gift request to admin %s failed: %s", admin, e)
            await safe_edit(query, msg, kb_back_home(user_id))

        elif data.startswith("approve_gift:"):
            if not is_admin(user_id):
                await safe_edit(query, "❌ Admin only.")
                return
            target = data.split(":")[1]
            success, message, target_user = approve_gift_request(target, user_id)
            await safe_edit(query, message)
            if success and target_user:
                await safe_send_user(
                    context,
                    target_user,
                    tr(target_user, "approve_user_gift")
                )

        elif data.startswith("reject_gift:"):
            if not is_admin(user_id):
                await safe_edit(query, "❌ Admin only.")
                return
            target = data.split(":")[1]
            success, message, target_user = reject_gift_request(target, user_id)
            await safe_edit(query, message)
            if success and target_user:
                await safe_send_user(context, target_user, tr(target_user, "reject_user_gift"))

        elif data == "community":
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(tr(user_id, "official_channel"), url=CHANNEL_URL)],
                [InlineKeyboardButton(tr(user_id, "vip_group"), url=GROUP_URL)],
                [InlineKeyboardButton(tr(user_id, "amoi_manja"), url=AMOI_MANJA_URL)],
                [InlineKeyboardButton(tr(user_id, "back"), callback_data="back")],
            ])

            await safe_edit(query, tr(user_id, "community_text"), keyboard)

        elif data == "support":
            await safe_edit(query, tr(user_id, "support_text", url=SUPPORT_URL), kb_back_home(user_id))

        elif data == "back":
            await safe_edit(query, get_main_text(user_id), get_main_keyboard(user_id))

        else:
            await safe_edit(query, tr(user_id, "unknown_button"), kb_back_home(user_id))

    except Exception as e:
        logger.exception("BUTTON ERROR user=%s error=%s", user_id, e)
        try:
            await query.answer("System busy, please try again.", show_alert=False)
        except Exception:
            pass
        try:
            await query.message.reply_text(tr(user_id, "system_busy"))
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
                f"💰 RM100: {row.get('rm100_count', 0)}x | RM300: {row.get('rm300_count', 0)}x\n"
                f"🎁 Check-in: {row.get('checkin_streak', 0)} days | Last: {(row.get('last_checkin_at') or '-')[:10]}\n"
                f"📱 {row.get('phone_number') or 'Not verified'}\n"
            )
        text = "\n".join(lines)
        for i in range(0, len(text), 3500):
            await update.message.reply_text(text[i:i + 3500])
    except Exception as e:
        logger.exception("all_users_cmd error: %s", e)
        await update.message.reply_text("⚠️ Failed to load users.")


async def top_users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        rows = get_top_invites()
        lines = ["🏆 Top Invite Ranking\n"]
        for i, row in enumerate(rows, start=1):
            lines.append(f"{i}. {row.get('name') or 'User'}\n⭐ {row.get('points', 0)} points\n👥 {row.get('invited_count', 0)} invites\n")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        logger.exception("top_users_cmd error: %s", e)
        await update.message.reply_text("⚠️ Failed to load ranking.")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        s = get_stats()
        redeem_count = db_fetchone("SELECT COUNT(*) AS c FROM redeem_requests WHERE status='pending'")
        gift_count = db_fetchone("SELECT COUNT(*) AS c FROM gift_requests WHERE status='pending'")
        await update.message.reply_text(
            "📊 Bot Stats\n\n"
            f"👥 Total Users: {s.get('total_users', 0)}\n"
            f"⭐ Total Points: {s.get('total_points', 0)}\n"
            f"👥 Total Invites: {s.get('total_invites', 0)}\n"
            f"🎁 Gifts Claimed: {s.get('gifts_claimed', 0)}\n"
            f"⏳ Pending Redeem: {redeem_count.get('c', 0)}\n"
            f"⏳ Pending Gift: {gift_count.get('c', 0)}"
        )
    except Exception as e:
        logger.exception("stats_cmd error: %s", e)
        await update.message.reply_text("⚠️ Failed to load stats.")


async def pending_redeem_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    rows = get_pending_redeems(20)
    if not rows:
        await update.message.reply_text("✅ No pending redeem requests.")
        return
    lines = ["⏳ Pending Redeem Requests\n"]
    for r in rows:
        lines.append(f"ID: {r.get('id')} | User: {r.get('user_id')} | Phone: {get_user_phone(r.get('user_id'))} | {r.get('reward_text')} | {r.get('points_needed')} pts | {r.get('username')}")
    await update.message.reply_text("\n".join(lines))



async def pending_deposit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    rows = get_pending_deposit_missions(20)
    if not rows:
        await update.message.reply_text("✅ No pending deposit mission requests.")
        return
    lines = ["⏳ Pending Deposit Mission Requests\n"]
    for r in rows:
        lines.append(f"ID: {r.get('id')} | User: {r.get('user_id')} | Phone: {get_user_phone(r.get('user_id'))} | RM{r.get('deposit_amount')}+ | +{r.get('reward_points')} pts | {r.get('username')} | {r.get('created_at')}")
    await update.message.reply_text("\n".join(lines))


async def pending_gift_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    rows = get_pending_gifts(20)
    if not rows:
        await update.message.reply_text("✅ No pending gift requests.")
        return
    lines = ["⏳ Pending Gift Requests\n"]
    for r in rows:
        lines.append(f"ID: {r.get('id')} | User: {r.get('user_id')} | Phone: {get_user_phone(r.get('user_id'))} | {r.get('username')} | {r.get('created_at')}")
    await update.message.reply_text("\n".join(lines))


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    users = get_all_users()
    success = 0
    failed = 0

    # MEDIA BROADCAST
    if update.message.reply_to_message:
        replied = update.message.reply_to_message
        caption = " ".join(context.args) if context.args else (replied.caption or "")

        for row in users:
            target_user_id = row.get("user_id")

            try:
                if replied.photo:
                    await context.bot.send_photo(
                        chat_id=int(target_user_id),
                        photo=replied.photo[-1].file_id,
                        caption=caption,
                    )

                elif replied.video:
                    await context.bot.send_video(
                        chat_id=int(target_user_id),
                        video=replied.video.file_id,
                        caption=caption,
                    )

                elif replied.animation:
                    await context.bot.send_animation(
                        chat_id=int(target_user_id),
                        animation=replied.animation.file_id,
                        caption=caption,
                    )

                else:
                    await context.bot.send_message(
                        chat_id=int(target_user_id),
                        text=caption or "📢 Broadcast"
                    )

                success += 1

            except Exception:
                failed += 1

        audit_log(
            update.effective_user.id,
            "media_broadcast",
            f"success={success} failed={failed}"
        )

        await update.message.reply_text(
            f"✅ Media broadcast sent to {success} users.\n❌ Failed: {failed}"
        )
        return

    # TEXT BROADCAST
    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "/broadcast your message\n\n"
            "OR\n"
            "Reply photo/video/gif with /broadcast"
        )
        return

    message = " ".join(context.args)

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

    audit_log(
        update.effective_user.id,
        "broadcast",
        f"success={success} failed={failed}"
    )

    await update.message.reply_text(
        f"✅ Broadcast sent to {success} users.\n❌ Failed: {failed}"
    )



async def broadcast_buttons_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Broadcast with fixed Share & Earn + Customer Service buttons.

    How to use:
    1) Send/upload a banner photo/video/gif to the bot with caption.
    2) Reply to that media with /broadcast_buttons
       - Or reply with: /broadcast_buttons new caption here
    3) The bot sends the media + caption + 2 inline buttons to all users.
    """
    if not is_admin(update.effective_user.id):
        return

    users = get_all_users()
    success = 0
    failed = 0
    reply_markup = broadcast_share_earn_keyboard()

    # MEDIA BROADCAST WITH BUTTONS
    if update.message.reply_to_message:
        replied = update.message.reply_to_message
        caption = " ".join(context.args) if context.args else (replied.caption or "")

        for row in users:
            target_user_id = row.get("user_id")

            try:
                if replied.photo:
                    await context.bot.send_photo(
                        chat_id=int(target_user_id),
                        photo=replied.photo[-1].file_id,
                        caption=caption,
                        reply_markup=reply_markup,
                    )

                elif replied.video:
                    await context.bot.send_video(
                        chat_id=int(target_user_id),
                        video=replied.video.file_id,
                        caption=caption,
                        reply_markup=reply_markup,
                    )

                elif replied.animation:
                    await context.bot.send_animation(
                        chat_id=int(target_user_id),
                        animation=replied.animation.file_id,
                        caption=caption,
                        reply_markup=reply_markup,
                    )

                else:
                    await context.bot.send_message(
                        chat_id=int(target_user_id),
                        text=caption or "📢 Broadcast",
                        reply_markup=reply_markup,
                    )

                success += 1
                if PUSH_SLEEP_SECONDS > 0:
                    await asyncio.sleep(PUSH_SLEEP_SECONDS)

            except Forbidden:
                failed += 1
            except RetryAfter as e:
                await asyncio.sleep(float(e.retry_after) + 1)
                failed += 1
            except Exception:
                failed += 1

        audit_log(
            update.effective_user.id,
            "media_broadcast_buttons",
            f"success={success} failed={failed}"
        )

        await update.message.reply_text(
            f"✅ Media broadcast with buttons sent to {success} users.\n❌ Failed: {failed}"
        )
        return

    # TEXT BROADCAST WITH BUTTONS
    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "/broadcast_buttons your message\n\n"
            "OR\n"
            "Reply photo/video/gif with /broadcast_buttons\n\n"
            "Buttons added automatically:\n"
            "📤 Share Kepada Kawan\n"
            "🎧 Screenshot Kepada Customer Service"
        )
        return

    message = " ".join(context.args)

    for row in users:
        target_user_id = row.get("user_id")

        try:
            await context.bot.send_message(
                chat_id=int(target_user_id),
                text=message,
                reply_markup=reply_markup,
            )
            success += 1
            if PUSH_SLEEP_SECONDS > 0:
                await asyncio.sleep(PUSH_SLEEP_SECONDS)

        except Forbidden:
            failed += 1
        except RetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 1)
            failed += 1
        except Exception:
            failed += 1

    audit_log(
        update.effective_user.id,
        "broadcast_buttons",
        f"success={success} failed={failed}"
    )

    await update.message.reply_text(
        f"✅ Broadcast with buttons sent to {success} users.\n❌ Failed: {failed}"
    )


async def addpoints_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /addpoints USER_ID POINTS")
        return
    target_user = str(context.args[0])
    points = safe_int(context.args[1])
    ensure_user(target_user, "User")
    add_points(target_user, points)
    audit_log(update.effective_user.id, "addpoints", f"target={target_user} points={points}")
    await update.message.reply_text(f"✅ Added {points} points to {target_user}")


async def setpoints_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /setpoints USER_ID POINTS")
        return
    target_user = str(context.args[0])
    points = max(safe_int(context.args[1]), 0)
    ensure_user(target_user, "User")
    set_points(target_user, points)
    audit_log(update.effective_user.id, "setpoints", f"target={target_user} points={points}")
    await update.message.reply_text(f"✅ Set {target_user} points to {points}")


async def resetreward_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    target_id = str(context.args[0]) if context.args else str(update.effective_user.id)
    db_execute("""
        UPDATE users
        SET last_lucky_claim='', last_vip_claim='', last_elite_claim=''
        WHERE user_id=%s
    """, (target_id,))
    audit_log(update.effective_user.id, "resetreward", f"target={target_id}")
    await update.message.reply_text(f"✅ Reward reset successful for {target_id}.")


async def phones_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        rows = get_verified_phones(500)
        if not rows:
            await update.message.reply_text("No verified Malaysia numbers yet.")
            return

        lines = [f"📱 Verified Malaysia Numbers ({len(rows)})\n"]
        for i, row in enumerate(rows, start=1):
            lines.append(
                f"{i}. {row.get('name') or 'User'}\n"
                f"ID: {row.get('user_id')}\n"
                f"📞 {row.get('phone_number')}\n"
                f"✅ {row.get('phone_verified_at') or '-'}\n"
            )

        text = "\n".join(lines)
        for i in range(0, len(text), 3500):
            await update.message.reply_text(text[i:i + 3500])
    except Exception as e:
        logger.exception("phones_cmd error: %s", e)
        await update.message.reply_text("⚠️ Failed to load verified numbers.")



async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    try:
        await update.message.reply_text(
            get_admin_panel_text(),
            reply_markup=get_admin_panel_keyboard(),
        )
    except Exception as e:
        logger.exception("admin_cmd error: %s", e)
        await update.message.reply_text("⚠️ Failed to open admin panel.")


async def help_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        "🛠 Admin Commands\n\n"
        "/admin - open button admin panel\n"
        "/stats - bot statistics\n"
        "/all_users - list all users\n"
        "/top_users - invite ranking\n"
        "/pending_redeem - pending redeem list\n"
        "/pending_gift - pending gift list\n"
        "/pending_deposit - pending deposit mission list\n"
        "/phones - verified Malaysia phone numbers\n"
        "/broadcast MESSAGE - send message to all users\n"
        "/addpoints USER_ID POINTS - add points\n"
        "/setpoints USER_ID POINTS - set exact points\n"
        "/resetreward USER_ID - reset daily reward for user"
    )


async def checkin_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    if not CHECKIN_PUSH_ENABLED:
        return

    try:
        rows = get_users_for_checkin_reminder()
        if not rows:
            return

        sent = 0
        for row in rows:
            user_id = str(row.get("user_id"))
            try:
                await context.bot.send_message(
                    chat_id=int(user_id),
                    text=checkin_reminder_text(row.get("language") or "ms"),
                    reply_markup=checkin_push_keyboard(user_id),
                )
                mark_checkin_reminder_sent(user_id)
                audit_log(user_id, "checkin_push_sent", f"cooldown_days={CHECKIN_PUSH_COOLDOWN_DAYS}")
                sent += 1
                if PUSH_SLEEP_SECONDS > 0:
                    await asyncio.sleep(PUSH_SLEEP_SECONDS)
            except Forbidden:
                mark_checkin_reminder_sent(user_id)
                logger.warning("Check-in push skipped; bot blocked by user %s", user_id)
            except RetryAfter as e:
                logger.warning("Check-in push rate limited. Sleeping %s seconds.", e.retry_after)
                await asyncio.sleep(float(e.retry_after) + 1)
            except Exception as e:
                logger.warning("Check-in push failed user=%s error=%s", user_id, e)

        logger.info("Check-in push job completed. sent=%s", sent)
    except Exception as e:
        logger.exception("checkin_reminder_job error: %s", e)


async def share_earn_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    if not SHARE_PUSH_ENABLED:
        return

    try:
        rows = get_users_for_share_reminder()
        if not rows:
            return

        sent = 0
        for row in rows:
            user_id = str(row.get("user_id"))
            try:
                await context.bot.send_message(
                    chat_id=int(user_id),
                    text=share_reminder_text(row.get("language") or "ms", user_id),
                    reply_markup=share_push_keyboard(user_id),
                )
                mark_share_reminder_sent(user_id)
                audit_log(user_id, "share_earn_push_sent", f"cooldown_days={SHARE_PUSH_COOLDOWN_DAYS}")
                sent += 1
                if PUSH_SLEEP_SECONDS > 0:
                    await asyncio.sleep(PUSH_SLEEP_SECONDS)
            except Forbidden:
                mark_share_reminder_sent(user_id)
                logger.warning("Share & Earn push skipped; bot blocked by user %s", user_id)
            except RetryAfter as e:
                logger.warning("Share & Earn push rate limited. Sleeping %s seconds.", e.retry_after)
                await asyncio.sleep(float(e.retry_after) + 1)
            except Exception as e:
                logger.warning("Share & Earn push failed user=%s error=%s", user_id, e)

        logger.info("Share & Earn push job completed. sent=%s", sent)
    except Exception as e:
        logger.exception("share_earn_reminder_job error: %s", e)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled Telegram error: %s", context.error)


# ================= RUN =================

def build_app():
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
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(MessageHandler(filters.CONTACT, contact_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_phone_handler))
    app.add_handler(CommandHandler("help_admin", help_admin_cmd))
    app.add_handler(CommandHandler("all_users", all_users_cmd))
    app.add_handler(CommandHandler("top_users", top_users_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("pending_redeem", pending_redeem_cmd))
    app.add_handler(CommandHandler("pending_gift", pending_gift_cmd))
    app.add_handler(CommandHandler("pending_deposit", pending_deposit_cmd))
    app.add_handler(CommandHandler("phones", phones_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("broadcast_buttons", broadcast_buttons_cmd))
    app.add_handler(CommandHandler("broadcast_button", broadcast_buttons_cmd))
    app.add_handler(CommandHandler("addpoints", addpoints_cmd))
    app.add_handler(CommandHandler("setpoints", setpoints_cmd))
    app.add_handler(CommandHandler("resetreward", resetreward_cmd))
    app.add_handler(CallbackQueryHandler(button))
    app.add_error_handler(error_handler)

    # Gentle automatic push reminders.
    # Check-in: daily job at 12:00 PM MYT, but each user receives it max once every CHECKIN_PUSH_COOLDOWN_DAYS.
    # Share & Earn: daily job at 8:30 PM MYT, but each user receives it max once every SHARE_PUSH_COOLDOWN_DAYS.
    if app.job_queue:
        app.job_queue.run_daily(
            checkin_reminder_job,
            time=dt_time(hour=12, minute=0, second=0, tzinfo=TZ),
            name="gentle_checkin_push",
        )
        app.job_queue.run_daily(
            share_earn_reminder_job,
            time=dt_time(hour=20, minute=30, second=0, tzinfo=TZ),
            name="gentle_share_earn_push",
        )
    else:
        logger.warning("JobQueue is not available. Install python-telegram-bot[job-queue] to enable auto push reminders.")

    return app


def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is missing.")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing.")

    init_pool()
    init_db()

    app = build_app()
    logger.info("Bot Running... mode=%s", "webhook" if USE_WEBHOOK else "polling")

    if USE_WEBHOOK:
        if not WEBHOOK_URL:
            raise RuntimeError("WEBHOOK_URL is missing while USE_WEBHOOK=true.")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TOKEN,
            webhook_url=f"{WEBHOOK_URL.rstrip('/')}/{TOKEN}",
            drop_pending_updates=True,
        )
    else:
        app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
