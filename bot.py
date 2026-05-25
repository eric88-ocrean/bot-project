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
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, Tuple

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
AMOI_MANJA_URL = os.getenv("AMOI_MANJA_URL", "https://t.me/JomJManja_bot")
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/JomJudi88vip")
HOME_BANNER_FILE_ID = os.getenv("HOME_BANNER_FILE_ID", "").strip()

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
                    phone_number TEXT DEFAULT '',
                    phone_verified INTEGER DEFAULT 0,
                    phone_verified_at TEXT DEFAULT '',
                    invite_rewarded INTEGER DEFAULT 0,
                    last_seen_at TEXT DEFAULT '',
                    created_at TEXT DEFAULT ''
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
                "phone_number TEXT DEFAULT ''",
                "phone_verified INTEGER DEFAULT 0",
                "phone_verified_at TEXT DEFAULT ''",
                "invite_rewarded INTEGER DEFAULT 0",
                "last_seen_at TEXT DEFAULT ''",
                "created_at TEXT DEFAULT ''",
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
         checkin_streak, last_seen_at, created_at)
        VALUES (%s,%s,0,0,0,0,%s,0,'','','',0,%s,%s)
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
        SELECT user_id, name, points, invited_count, phone_number, phone_verified
        FROM users
        ORDER BY invited_count DESC, points DESC
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
                    return False, "❌ You already claimed today's reward.\n\n⏰ Please come back after 12AM Malaysia time."

                if safe_int(user.get("invited_count", 0)) < min_invites:
                    conn.rollback()
                    return False, f"🔒 {title_map[reward_type]} unlocks at {min_invites} invites."

            reward = random_reward(reward_pool)
            cur.execute(
                f"UPDATE users SET {column}=%s, points=GREATEST(points + %s, 0) WHERE user_id=%s",
                (today, int(reward), str(user_id)),
            )

            cur.execute(
                "INSERT INTO audit_logs (user_id, action, detail, created_at) VALUES (%s,%s,%s,%s)",
                (str(user_id), f"claim_{reward_type}", f"reward={reward}", now_iso()),
            )

        conn.commit()

        if reward > 0:
            return True, f"🎉 Reward Berjaya Dibuka!\n\n⭐ +{reward} Points masuk 🔥"
        return True, "😆 Belum kena reward kali ni\n\nCuba lagi esok 🔥"
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
                return False, "✅ You already claimed mission rewards."

            cur.execute("UPDATE users SET points=points+2, mission_claimed=1 WHERE user_id=%s", (str(user_id),))
            cur.execute(
                "INSERT INTO audit_logs (user_id, action, detail, created_at) VALUES (%s,%s,%s,%s)",
                (str(user_id), "mission_claim", "+2 points", now_iso()),
            )
        conn.commit()
        return True, "🎉 Mission Completed!\n\n⭐ +2 Points Added"
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
                return False, "❌ Not enough points for this reward.", None

            cur.execute("""
                SELECT id FROM redeem_requests
                WHERE user_id=%s AND reward_text=%s AND status='pending'
                LIMIT 1
            """, (str(user_id), reward_text))
            pending = cur.fetchone()
            if pending:
                conn.rollback()
                return False, "⏳ You already have a pending request for this reward.\n\nPlease wait for admin approval.", pending["id"]

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
        return True, "⏳ Redeem request submitted.\n\nAdmin will review your request.", request_id
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
                return False, "✅ You already claimed the new join gift.", None

            cur.execute("SELECT id FROM gift_requests WHERE user_id=%s AND status='pending' LIMIT 1", (str(user_id),))
            pending = cur.fetchone()
            if pending:
                conn.rollback()
                return False, "⏳ You already have a pending gift request.\n\nPlease wait for admin approval.", pending["id"]

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
        return True, "⏳ Gift request submitted.\n\nAdmin will review your request.", request_id
    except Exception as e:
        if conn:
            conn.rollback()
        logger.exception("create_gift_request_locked error: %s", e)
        return False, "⚠️ Gift system busy. Please try again.", None
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


# ================= UI =================

def get_main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔐 Register", url=REGISTER_URL),
            InlineKeyboardButton("💰 Earn Rewards", callback_data="menu"),
        ],
        [
            InlineKeyboardButton("🎁 Free RM38", callback_data="gift"),
            InlineKeyboardButton("🎁 Check In", callback_data="reward_center"),
        ],
        [
            InlineKeyboardButton("🌐 Community", callback_data="community"),
            InlineKeyboardButton("🎧 Support", callback_data="support"),
        ],
    ])


def get_main_text():
    return (
        "🎁 𝗝𝗢𝗠𝗝𝗨𝗗𝗜𝟴𝟴 𝗥𝗘𝗪𝗔𝗥𝗗𝗦 🔥\n\n"
        "💸 Main & collect reward setiap hari\n"
        "🎯 Claim points & redeem hadiah\n"
        "👑 Unlock VIP rewards\n"
        "💰 Touch ’n Go RM100\n\n"
        "⚡ Auto Deposit & Withdraw 24/7\n"
        "🔐 Support & Privasi Terjamin\n\n"
        "👇 Pilih menu di bawah 🚀"
    )


def kb_back_menu():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu")]])


def kb_back_home():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back")]])


def kb_back_reward_center():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="reward_center")]])


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
                caption=get_main_text(),
                reply_markup=get_main_keyboard(),
            )
            return

        # FIRST TIME MODE: upload local image once
        if os.path.exists("banner.jpg"):
            with open("banner.jpg", "rb") as photo:
                msg = await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=photo,
                    caption=get_main_text(),
                    reply_markup=get_main_keyboard(),
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
            text=get_main_text(),
            reply_markup=get_main_keyboard(),
        )

    except Exception as e:
        logger.warning("Banner/send_photo error, fallback send_message: %s", e)

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=get_main_text(),
            reply_markup=get_main_keyboard(),
        )
    except Exception as e:
        logger.warning("Banner/send_photo error, fallback send_message: %s", e)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=get_main_text(),
            reply_markup=get_main_keyboard(),
        )


async def request_phone_verification(update: Update):
    await update.effective_message.reply_text(
        "🇲🇾 Malaysia Users Only\n\n"
        "Please verify your phone number to continue.\n\n"
        "Tap the button below and share your Telegram contact.",
        reply_markup=get_verify_keyboard(),
    )


async def contact_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        contact = update.effective_message.contact
        if not contact:
            return

        user = update.effective_user
        user_id = str(user.id)
        user_name = user.first_name or "User"

        # Prevent spam multiple /start clicks
        current_time = time.time()
        last_start = START_LOCK.get(user_id, 0)

        if current_time - last_start < 2:
            return

        START_LOCK[user_id] = current_time
        ensure_user(user_id, user_name)

        if contact.user_id and str(contact.user_id) != user_id:
            await update.effective_message.reply_text(
                "❌ Please share your own Telegram phone number.",
                reply_markup=get_verify_keyboard(),
            )
            return

        current_user = get_user(user_id)
        if current_user and safe_int(current_user.get("phone_verified", 0)) == 1:
            await update.effective_message.reply_text(
                "✅ Your number is already verified.",
                reply_markup=ReplyKeyboardRemove(),
            )
            await send_home(update, context)
            return

        phone = normalize_phone(contact.phone_number)
        if not phone.startswith("+60"):
            audit_log(user_id, "phone_verify_rejected", f"phone={phone}")
            await update.effective_message.reply_text(
                "❌ Malaysia phone number only 🇲🇾",
                reply_markup=get_verify_keyboard(),
            )
            return

        existing_phone = db_fetchone(
            "SELECT user_id FROM users WHERE phone_number=%s AND phone_verified=1 LIMIT 1",
            (phone,),
        )
        if existing_phone and str(existing_phone.get("user_id")) != user_id:
            audit_log(user_id, "phone_duplicate_rejected", f"phone={phone}")
            await update.effective_message.reply_text(
                "❌ This phone number is already registered.",
                reply_markup=get_verify_keyboard(),
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
            "✅ Malaysia number verified successfully.\n\n"
            "Welcome to JomJudi88 Rewards 🔥",
            reply_markup=ReplyKeyboardRemove(),
        )
        await send_home(update, context)

    except Exception as e:
        logger.exception("CONTACT_HANDLER_ERROR: %s", e)
        try:
            await update.effective_message.reply_text("⚠️ Verification system busy. Please try again.")
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
    verified_count = db_fetchone("SELECT COUNT(*) AS c FROM users WHERE phone_verified=1") or {}

    return (
        "🛠 JomJudi88 Admin Panel\n\n"
        f"👥 Total Users: {stats.get('total_users', 0)}\n"
        f"📱 Verified Users: {verified_count.get('c', 0)}\n"
        f"⭐ Total Points: {stats.get('total_points', 0)}\n"
        f"👥 Total Invites: {stats.get('total_invites', 0)}\n"
        f"⏳ Pending Redeem: {redeem_count.get('c', 0)}\n"
        f"⏳ Pending Gift: {gift_count.get('c', 0)}\n\n"
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

        if not is_admin(user_id) and safe_int(user.get("phone_verified", 0)) != 1:
            await safe_reply(
                query,
                "🇲🇾 Malaysia Users Only\n\nPlease press /start and verify your phone number first."
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
                keyboard,
            )

        elif data == "profile":
            user = get_user(user_id) or user
            await safe_edit(
                query,
                f"💎 Your Rewards\n\n"
                f"⭐️ Reward Points: {user.get('points', 0)}\n"
                f"👥 Friends Referred: {user.get('invited_count', 0)}\n\n"
                f"----------------------------------------",
                kb_back_menu(),
            )

        elif data == "link":
            link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
            await safe_edit(
                query,
                f"💰 Share & Earn Lagi!\n\n"
                f"Jom ajak kawan join & collect reward sama-sama 🔥\n\n"
                f"🔗 Link Boss:\n\n{link}",
                kb_back_menu(),
            )

        elif data == "reward_center":
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🎁 Lucky Reward", callback_data="lucky_reward")],
                [InlineKeyboardButton("🔥 VIP Reward", callback_data="vip_reward")],
                [InlineKeyboardButton("👑 Elite Reward", callback_data="elite_reward")],
                [InlineKeyboardButton("🔙 Back", callback_data="back")],
            ])
            await safe_edit(
                query,
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
                "+0 • +1 • +5",
                keyboard,
            )

        elif data == "lucky_reward":
            ok, msg = claim_daily_reward(user_id, "lucky", 0, [(0, 40), (1, 60)])
            await safe_edit(query, msg, kb_back_reward_center())

        elif data == "vip_reward":
            ok, msg = claim_daily_reward(user_id, "vip", 5, [(0, 30), (1, 65), (3, 5)])
            await safe_edit(query, msg, kb_back_reward_center())

        elif data == "elite_reward":
            ok, msg = claim_daily_reward(user_id, "elite", 20, [(0, 30), (1, 67), (5, 3)])
            await safe_edit(query, msg, kb_back_reward_center())

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
                keyboard,
            )

        elif data == "check_missions":
            joined_channel = await is_user_joined(CHANNEL_ID, user_id, context)
            joined_group = await is_user_joined(GROUP_ID, user_id, context)
            if joined_channel and joined_group:
                ok, msg = claim_mission_reward(user_id)
                await safe_edit(query, msg, kb_back_menu())
            else:
                await safe_edit(
                    query,
                    "❌ Please join Channel & Group first.\n\n"
                    "⚠️ If you already joined but still cannot claim, make sure the bot is admin in the channel/group.",
                    kb_back_menu(),
                )

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
                    keyboard_rows.append([InlineKeyboardButton(f"{reward_text} ({pts} Points)", callback_data=f"redeem:{pts}:{reward_text}")])
            if not keyboard_rows:
                keyboard_rows.append([InlineKeyboardButton("❌ Not enough points yet", callback_data="not_enough_points")])
            keyboard_rows.append([InlineKeyboardButton("🔙 Back", callback_data="menu")])
            await safe_edit(query, f"🎁 Claim Reward\n\n⭐ Your Points: {points}\n\n👇 Select available reward:", InlineKeyboardMarkup(keyboard_rows))

        elif data == "not_enough_points":
            await safe_edit(query, "❌ Not enough points yet.\n\nInvite friends and complete missions to collect more points.", kb_back_menu())

        elif data.startswith("redeem:"):
            try:
                _, pts, reward_text = data.split(":", 2)
                pts = int(pts)
            except Exception:
                await safe_edit(query, "⚠️ Invalid redeem request.", kb_back_menu())
                return

            username = query.from_user.username
            username_text = f"@{username}" if username else "No Username"
            ok, msg, request_id = create_redeem_request_locked(user_id, username_text, reward_text, pts)

            if ok and request_id:
                user_latest = get_user(user_id) or user
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
                                f"⭐ User Current Points: {user_latest.get('points', 0)}\n"
                                f"📝 Request ID: {request_id}"
                            ),
                            reply_markup=admin_keyboard,
                        )
                    except Exception as e:
                        logger.warning("Send redeem request to admin %s failed: %s", admin, e)
            await safe_edit(query, msg, kb_back_menu())

        elif data.startswith("approve_redeem:"):
            if not is_admin(user_id):
                await safe_edit(query, "❌ Admin only.")
                return
            request_id = int(data.split(":")[1])
            success, message, target_user, reward_text = approve_redeem_request(request_id, user_id)
            await safe_edit(query, message)
            if success and target_user:
                await safe_send_user(context, target_user, f"✅ Your redeem request has been approved.\n\n🎁 Reward: {reward_text}")

        elif data.startswith("reject_redeem:"):
            if not is_admin(user_id):
                await safe_edit(query, "❌ Admin only.")
                return
            request_id = int(data.split(":")[1])
            success, message, target_user, reward_text = reject_redeem_request(request_id, user_id)
            await safe_edit(query, message)
            if success and target_user:
                await safe_send_user(context, target_user, f"❌ Your redeem request was rejected.\n\n🎁 Reward: {reward_text}")

        elif data == "gift":
            user = get_user(user_id) or user
            if safe_int(user.get("gift_claimed", 0)) == 1 and not is_admin(user_id):
                await safe_edit(query, "✅ You already claimed the new join gift.", kb_back_home())
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
                keyboard,
            )

        elif data == "claim_gift":
            joined_channel = await is_user_joined(CHANNEL_ID, user_id, context)
            joined_group = await is_user_joined(GROUP_ID, user_id, context)

            if not joined_channel or not joined_group:
                await safe_edit(
                    query,
                    "❌ Please join Channel & Group first before claiming Free RM38.\n\n"
                    "⚠️ If you already joined but still cannot claim, make sure the bot is admin in the channel/group.",
                    kb_back_home(),
                )
                return

            username = query.from_user.username
            username_text = f"@{username}" if username else "No Username"
            ok, msg, request_id = create_gift_request_locked(user_id, username_text)
            if ok:
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
                                f"📝 Request ID: {request_id}"
                            ),
                            reply_markup=admin_keyboard,
                        )
                    except Exception as e:
                        logger.warning("Send gift request to admin %s failed: %s", admin, e)
            await safe_edit(query, msg, kb_back_home())

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
                    "✅ Your RM38 Game Credit request has been approved.\n\n💸 RM38 game credit has been sent to your gaming account."
                )

        elif data.startswith("reject_gift:"):
            if not is_admin(user_id):
                await safe_edit(query, "❌ Admin only.")
                return
            target = data.split(":")[1]
            success, message, target_user = reject_gift_request(target, user_id)
            await safe_edit(query, message)
            if success and target_user:
                await safe_send_user(context, target_user, "❌ Your new join gift request was rejected.")

        elif data == "community":
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📢 Official Channel", url=CHANNEL_URL)],
                [InlineKeyboardButton("👥 VIP Group", url=GROUP_URL)],
                [InlineKeyboardButton("🔞 Amoi Manja", url=AMOI_MANJA_URL)],
                [InlineKeyboardButton("🔙 Back", callback_data="back")],
            ])

            await safe_edit(
                query,
                "🌐 JOMJUDI88 COMMUNITY\n\n"
                "📢 Official Updates\n"
                "👥 VIP Member Group\n"
                "🔞 Exclusive Amoi Content\n\n"
                "👇 Choose below:",
                keyboard,
            )

        elif data == "support":
            await safe_edit(
                query,
                f"🟢 JOMJUDI88 SUPPORT\n\n"
                f"⚡ Fast Response\n"
                f"🔐 Secure & Private\n"
                f"🎧 24/7 Live Assistance\n\n"
                f"Tap the link below to contact support.\n"
                f"{SUPPORT_URL}",
                kb_back_home(),
            )

        elif data == "back":
            await safe_edit(query, get_main_text(), get_main_keyboard())

        else:
            await safe_edit(query, "⚠️ Unknown button. Please press /start again.", kb_back_home())

    except Exception as e:
        logger.exception("BUTTON ERROR user=%s error=%s", user_id, e)
        try:
            await query.answer("System busy, please try again.", show_alert=False)
        except Exception:
            pass
        try:
            await query.message.reply_text("⚠️ System busy, please try again or press /start.")
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
        lines.append(f"ID: {r.get('id')} | User: {r.get('user_id')} | {r.get('reward_text')} | {r.get('points_needed')} pts | {r.get('username')}")
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
        lines.append(f"ID: {r.get('id')} | User: {r.get('user_id')} | {r.get('username')} | {r.get('created_at')}")
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
        "/phones - verified Malaysia phone numbers\n"
        "/broadcast MESSAGE - send message to all users\n"
        "/addpoints USER_ID POINTS - add points\n"
        "/setpoints USER_ID POINTS - set exact points\n"
        "/resetreward USER_ID - reset daily reward for user"
    )


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
    app.add_handler(CommandHandler("help_admin", help_admin_cmd))
    app.add_handler(CommandHandler("all_users", all_users_cmd))
    app.add_handler(CommandHandler("top_users", top_users_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("pending_redeem", pending_redeem_cmd))
    app.add_handler(CommandHandler("pending_gift", pending_gift_cmd))
    app.add_handler(CommandHandler("phones", phones_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("addpoints", addpoints_cmd))
    app.add_handler(CommandHandler("setpoints", setpoints_cmd))
    app.add_handler(CommandHandler("resetreward", resetreward_cmd))
    app.add_handler(CallbackQueryHandler(button))
    app.add_error_handler(error_handler)
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
