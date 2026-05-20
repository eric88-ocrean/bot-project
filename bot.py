
# FULL UPGRADED BOT V2
# Added:
# - Lucky Reward
# - VIP Reward
# - Elite Reward
# - Daily claim protection
# - Progression system

import os
import psycopg2
import random
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes
)

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

BOT_USERNAME = "JomJudi_bot"

ADMIN_IDS = {"909399622"}

CHANNEL_ID = "@jomjudi88cuci"
GROUP_ID = "@jomjudi88official"

CHANNEL_URL = "https://t.me/jomjudi88cuci"
GROUP_URL = "https://t.me/jomjudi88official"


# ================= DB =================
def get_conn():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    conn = get_conn()
    cur = conn.cursor()

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


    # ===== AUTO UPGRADE OLD DATABASE =====
    cur.execute("""
    ALTER TABLE users
    ADD COLUMN IF NOT EXISTS last_lucky_claim TEXT DEFAULT ''
    """)

    cur.execute("""
    ALTER TABLE users
    ADD COLUMN IF NOT EXISTS last_vip_claim TEXT DEFAULT ''
    """)

    cur.execute("""
    ALTER TABLE users
    ADD COLUMN IF NOT EXISTS last_elite_claim TEXT DEFAULT ''
    """)


    cur.execute("""
    CREATE TABLE IF NOT EXISTS redeem_requests (
        id SERIAL PRIMARY KEY,
        user_id TEXT,
        username TEXT,
        reward_text TEXT,
        points_needed INTEGER,
        status TEXT DEFAULT 'pending'
    )
    """)

    conn.commit()
    cur.close()
    conn.close()


def get_user(user_id):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT * FROM users WHERE user_id=%s", (user_id,))
    row = cur.fetchone()

    cur.close()
    conn.close()

    return row


def create_user(user_id, name, referrer_id=None):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO users
        (
            user_id,name,points,invited_count,
            spin_chances,gift_claimed,
            referrer_id,mission_claimed,
            last_lucky_claim,last_vip_claim,last_elite_claim
        )
        VALUES (%s,%s,0,0,0,0,%s,0,'','','')
        ON CONFLICT (user_id) DO NOTHING
    """, (user_id, name, referrer_id))

    conn.commit()
    cur.close()
    conn.close()


def add_points(user_id, amount):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        UPDATE users
        SET points = points + %s
        WHERE user_id=%s
    """, (amount, user_id))

    conn.commit()
    cur.close()
    conn.close()


def deduct_points(user_id, amount):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        UPDATE users
        SET points = points - %s
        WHERE user_id=%s
    """, (amount, user_id))

    conn.commit()
    cur.close()
    conn.close()


def add_invite(referrer_id):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        UPDATE users
        SET invited_count = invited_count + 1,
            points = points + 1
        WHERE user_id=%s
    """, (referrer_id,))

    conn.commit()
    cur.close()
    conn.close()


def update_claim(user_id, claim_type):
    today = datetime.now().strftime("%Y-%m-%d")

    conn = get_conn()
    cur = conn.cursor()

    if claim_type == "lucky":
        cur.execute("""
            UPDATE users
            SET last_lucky_claim=%s
            WHERE user_id=%s
        """, (today, user_id))

    elif claim_type == "vip":
        cur.execute("""
            UPDATE users
            SET last_vip_claim=%s
            WHERE user_id=%s
        """, (today, user_id))

    elif claim_type == "elite":
        cur.execute("""
            UPDATE users
            SET last_elite_claim=%s
            WHERE user_id=%s
        """, (today, user_id))

    conn.commit()
    cur.close()
    conn.close()


def create_redeem_request(user_id, username, reward_text, points_needed):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO redeem_requests
        (user_id, username, reward_text, points_needed, status)
        VALUES (%s,%s,%s,%s,'pending')
        RETURNING id
    """, (user_id, username, reward_text, points_needed))

    request_id = cur.fetchone()[0]

    conn.commit()
    cur.close()
    conn.close()

    return request_id


def get_redeem_request(request_id):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT *
        FROM redeem_requests
        WHERE id=%s
    """, (request_id,))

    row = cur.fetchone()

    cur.close()
    conn.close()

    return row


def update_redeem_status(request_id, status):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        UPDATE redeem_requests
        SET status=%s
        WHERE id=%s
    """, (status, request_id))

    conn.commit()
    cur.close()
    conn.close()


def mark_gift_claimed(user_id):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        UPDATE users
        SET gift_claimed=1
        WHERE user_id=%s
    """, (user_id,))

    conn.commit()
    cur.close()
    conn.close()


def mark_mission_claimed(user_id):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        UPDATE users
        SET mission_claimed=1
        WHERE user_id=%s
    """, (user_id,))

    conn.commit()
    cur.close()
    conn.close()


def get_top_invites():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT name, points, invited_count
        FROM users
        ORDER BY invited_count DESC, points DESC
        LIMIT 10
    """)

    rows = cur.fetchall()

    cur.close()
    conn.close()

    return rows


def get_all_users():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT user_id,name,points,invited_count
        FROM users
        ORDER BY invited_count DESC
    """)

    rows = cur.fetchall()

    cur.close()
    conn.close()

    return rows


# ================= UI =================
def get_main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔐 Daftar Akaun",
                url="https://jomjudi88.live/my/register/?referral=JJ27817922"
            ),
            InlineKeyboardButton(
                "💰 Earn Rewards",
                callback_data="menu"
            )
        ],
        [
            InlineKeyboardButton(
                "🎁 Claim Credit",
                callback_data="gift"
            ),
            InlineKeyboardButton(
                "🎁 Lucky Reward",
                callback_data="reward_center"
            )
        ],
        [
            InlineKeyboardButton(
                "📢 Join Channel",
                url=CHANNEL_URL
            ),
            InlineKeyboardButton(
                "👥 Join Group",
                url=GROUP_URL
            )
        ],
        [
            InlineKeyboardButton(
                "🔞 Amoi Manja",
                url="https://t.me/JomJManja_bot"
            ),
            InlineKeyboardButton(
                "🎧 Support",
                callback_data="support"
            )
        ]
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

async def safe_edit(query, text, reply_markup=None):
    try:
        await query.edit_message_text(
            text=text,
            reply_markup=reply_markup
        )
    except Exception as e:
        print(e)


def random_reward(pool):
    rewards = []

    for reward, weight in pool:
        rewards.extend([reward] * weight)

    return random.choice(rewards)


# ================= START =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    user_id = str(user.id)
    user_name = user.first_name or ""

    referrer_id = context.args[0] if context.args else None

    existing = get_user(user_id)

    if not existing:

        valid_referrer = None

        if referrer_id and referrer_id != user_id:
            ref_user = get_user(referrer_id)

            if ref_user:
                valid_referrer = referrer_id

        create_user(user_id, user_name, valid_referrer)

        if valid_referrer:
            add_invite(valid_referrer)

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=get_main_text(),
        reply_markup=get_main_keyboard()
    )


async def is_user_joined(chat_id, user_id, context):
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False


# ================= BUTTON =================
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)

    # ================= MENU =================
    if query.data == "menu":

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 Your Rewards", callback_data="profile")],
            [InlineKeyboardButton("💰 Share & Earn", callback_data="link")],
            [InlineKeyboardButton("🎯 Missions", callback_data="missions")],
            [InlineKeyboardButton("🎁 Claim Reward", callback_data="redeem_menu")],
            [InlineKeyboardButton("🏆 Top Ranking", callback_data="ranking")],
            [InlineKeyboardButton("🔙 Back", callback_data="back")]
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

    elif query.data == "profile":

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back", callback_data="menu")]
        ])

        await safe_edit(
            query,
            f"💎 Your Rewards\n\n"
            f"⭐️ Reward Points: {user[2]}\n"
            f"👥 Friends Referred: {user[3]}\n\n"
            f"----------------------------------------",
            keyboard
        )

    elif query.data == "link":

        link = f"https://t.me/{BOT_USERNAME}?start={user_id}"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="menu")]
        ])

        await safe_edit(
            query,
            f"💰 Share & Earn Lagi!\n\n"
            f"Jom ajak kawan join & collect reward sama-sama 🔥\n\n"
            f"🔗 Link Boss:\n\n{link}",
            keyboard
        )

    # ================= REWARD CENTER =================
    elif query.data == "reward_center":

        invites = user[3]

        vip_status = "✅ Unlocked" if invites >= 5 else "🔒 Unlock at 5 invites"
        elite_status = "✅ Unlocked" if invites >= 20 else "🔒 Unlock at 20 invites"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎁 Lucky Reward", callback_data="lucky_reward")],
            [InlineKeyboardButton("🔥 VIP Reward", callback_data="vip_reward")],
            [InlineKeyboardButton("👑 Elite Reward", callback_data="elite_reward")],
            [InlineKeyboardButton("🔙 Back", callback_data="back")]
        ])

        await safe_edit(
            query,
            f"🎁 Reward Center\n\n"
            f"🎁 Lucky Reward\nDaily free reward\n\n"
            f"🔥 VIP Reward\n{vip_status}\n\n"
            f"👑 Elite Reward\n{elite_status}",
            keyboard
        )

    # ================= NORMAL REWARD =================
    elif query.data == "lucky_reward":

        today = datetime.now().strftime("%Y-%m-%d")

        if user[8] == today:

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="reward_center")]
            ])

            await safe_edit(
                query,
                "❌ You already claimed today's Lucky Reward.",
                keyboard
            )
            return

        reward = random_reward([
            (0, 40),
            (1, 35),
            (2, 15),
            (5, 8),
            (10, 2)
        ])

        update_claim(user_id, "lucky")

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="reward_center")]
        ])

        if reward > 0:
            add_points(user_id, reward)

            await safe_edit(
                query,
                f"🎉 Lucky Reward Opened!\n\n"
                f"⭐ +{reward} Points Added",
                keyboard
            )
        else:
            await safe_edit(
                query,
                "❌ Better luck tomorrow!",
                keyboard
            )

    # ================= VIP REWARD =================
    elif query.data == "vip_reward":

        invites = user[3]

        if invites < 5:

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="reward_center")]
            ])

            await safe_edit(
                query,
                "🔒 VIP Reward unlocks at 5 invites.",
                keyboard
            )
            return

        today = datetime.now().strftime("%Y-%m-%d")

        if user[9] == today:

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="reward_center")]
            ])

            await safe_edit(
                query,
                "❌ You already claimed today's VIP Reward.",
                keyboard
            )
            return

        reward = random_reward([
            (2, 40),
            (5, 35),
            (10, 20),
            (20, 5)
        ])

        update_claim(user_id, "vip")
        add_points(user_id, reward)

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="reward_center")]
        ])

        await safe_edit(
            query,
            f"🔥 VIP Reward Opened!\n\n"
            f"⭐ +{reward} Points Added",
            keyboard
        )

    # ================= ELITE REWARD =================
    elif query.data == "elite_reward":

        invites = user[3]

        if invites < 20:

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="reward_center")]
            ])

            await safe_edit(
                query,
                "🔒 Elite Reward unlocks at 20 invites.",
                keyboard
            )
            return

        today = datetime.now().strftime("%Y-%m-%d")

        if user[10] == today:

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="reward_center")]
            ])

            await safe_edit(
                query,
                "❌ You already claimed today's Elite Reward.",
                keyboard
            )
            return

        reward = random_reward([
            (5, 35),
            (10, 35),
            (20, 20),
            (50, 10)
        ])

        update_claim(user_id, "elite")
        add_points(user_id, reward)

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="reward_center")]
        ])

        await safe_edit(
            query,
            f"👑 Elite Reward Opened!\n\n"
            f"🔥 +{reward} Points Added",
            keyboard
        )

    # ================= MISSIONS =================
    elif query.data == "missions":

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Join Channel", url=CHANNEL_URL)],
            [InlineKeyboardButton("👥 Join Group", url=GROUP_URL)],
            [InlineKeyboardButton("✅ Done Join", callback_data="check_missions")],
            [InlineKeyboardButton("🔙 Back", callback_data="menu")]
        ])

        await safe_edit(
            query,
            "🎯 Missions\n\n"
            "Jom complete mission & collect reward 🔥",
            keyboard
        )

    elif query.data == "check_missions":

        if user[7] == 1:

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="menu")]
            ])

            await safe_edit(
                query,
                "✅ You already claimed mission rewards.",
                keyboard
            )
            return

        joined_channel = await is_user_joined(
            CHANNEL_ID,
            int(user_id),
            context
        )

        joined_group = await is_user_joined(
            GROUP_ID,
            int(user_id),
            context
        )

        if joined_channel and joined_group:

            add_points(user_id, 2)
            mark_mission_claimed(user_id)

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="menu")]
            ])

            await safe_edit(
                query,
                "🎉 Mission Completed!\n\n⭐ +2 Points Added",
                keyboard
            )

        else:

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="menu")]
            ])

            await safe_edit(
                query,
                "❌ Please join Channel & Group first.",
                keyboard
            )

    # ================= REDEEM =================
    elif query.data == "redeem_menu":

        points = user[2]

        keyboard_rows = []

        rewards = [
            ("RM1 Credit", 3),
            ("RM5 Credit", 10),
            ("RM10 Credit", 20),
            ("Touch 'n Go RM100", 200)
        ]

        for reward_text, pts in rewards:
            if points >= pts:
                keyboard_rows.append([
                    InlineKeyboardButton(
                        f"{reward_text} ({pts} Points)",
                        callback_data=f"redeem:{pts}:{reward_text}"
                    )
                ])

        keyboard_rows.append([
            InlineKeyboardButton("🔙 Back", callback_data="menu")
        ])

        await safe_edit(
            query,
            f"🎁 Claim Reward\n\n⭐ Your Points: {points}",
            InlineKeyboardMarkup(keyboard_rows)
        )

    elif query.data.startswith("redeem:"):

        _, pts, reward_text = query.data.split(":", 2)

        pts = int(pts)

        username = query.from_user.username
        username_text = f"@{username}" if username else "No Username"

        request_id = create_redeem_request(
            user_id,
            username_text,
            reward_text,
            pts
        )

        for admin in ADMIN_IDS:

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✅ Approve",
                        callback_data=f"approve_redeem:{request_id}"
                    ),
                    InlineKeyboardButton(
                        "❌ Reject",
                        callback_data=f"reject_redeem:{request_id}"
                    )
                ]
            ])

            try:
                await context.bot.send_message(
                    chat_id=int(admin),
                    text=(
                        f"🎁 New Redeem Request\n\n"
                        f"User ID: {user_id}\n"
                        f"Reward: {reward_text}\n"
                        f"Points: {pts}"
                    ),
                    reply_markup=keyboard
                )
            except:
                pass

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="menu")]
        ])

        await safe_edit(
            query,
            "⏳ Redeem request submitted.",
            keyboard
        )

    elif query.data.startswith("approve_redeem:"):

        if str(query.from_user.id) not in ADMIN_IDS:
            return

        request_id = int(query.data.split(":")[1])

        req = get_redeem_request(request_id)

        if not req:
            return

        _, target_user, _, reward_text, pts, status = req

        deduct_points(target_user, pts)
        update_redeem_status(request_id, "approved")

        await safe_edit(query, "✅ Redeem Approved")

    elif query.data.startswith("reject_redeem:"):

        update_redeem_status(
            int(query.data.split(":")[1]),
            "rejected"
        )

        await safe_edit(query, "❌ Redeem Rejected")

    # ================= GIFT =================
    elif query.data == "gift":

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎁 Claim Gift", callback_data="claim_gift")],
            [InlineKeyboardButton("🔙 Back", callback_data="back")]
        ])

        await safe_edit(
            query,
            "🎁 New Customer Gift\n\n"
            "✅ Register Account\n"
            "✅ First Deposit RM20+\n"
            "✅ Join Channel & Group\n\n"
            "🎁 Reward:\nRM38 Free Credit",
            keyboard
        )

    elif query.data == "claim_gift":

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="menu")]
        ])

        for admin in ADMIN_IDS:

            admin_keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✅ Approve",
                        callback_data=f"approve_gift:{user_id}"
                    ),
                    InlineKeyboardButton(
                        "❌ Reject",
                        callback_data=f"reject_gift:{user_id}"
                    )
                ]
            ])

            try:
                await context.bot.send_message(
                    chat_id=int(admin),
                    text=f"🎁 New Gift Request\nUser ID: {user_id}",
                    reply_markup=admin_keyboard
                )
            except:
                pass

        await safe_edit(
            query,
            "⏳ Gift request submitted.",
            keyboard
        )

    elif query.data.startswith("approve_gift:"):

        target = query.data.split(":")[1]

        add_points(target, 38)
        mark_gift_claimed(target)

        await safe_edit(query, "✅ Gift Approved")

    elif query.data.startswith("reject_gift:"):

        await safe_edit(query, "❌ Gift Rejected")

    # ================= RANKING =================
    elif query.data == "ranking":

        rows = get_top_invites()

        lines = ["🏆 Top Ranking\n"]

        for i, row in enumerate(rows, start=1):

            name, points, invites = row

            lines.append(
                f"{i}. {name}\n"
                f"👥 {invites} invites\n"
                f"⭐ {points} points\n"
            )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="menu")]
        ])

        await safe_edit(
            query,
            "\n".join(lines),
            keyboard
        )

    elif query.data == "support":

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="back")]
        ])

        await safe_edit(
            query,
            "🎧 Need Help?\n\n"
            "📲 Telegram:\n"
            "https://t.me/JomJudi88vip",
            keyboard
        )

    elif query.data == "back":

        await safe_edit(
            query,
            get_main_text(),
            get_main_keyboard()
        )


# ================= ADMIN =================
async def all_users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if str(update.effective_user.id) not in ADMIN_IDS:
        return

    rows = get_all_users()

    lines = ["📊 All Users\n"]

    for i, row in enumerate(rows, start=1):

        uid, name, points, invites = row

        lines.append(
            f"{i}. {name}\n"
            f"ID: {uid}\n"
            f"⭐ {points} points\n"
            f"👥 {invites} invites\n"
        )

    text = "\n".join(lines)

    for i in range(0, len(text), 3500):
        await update.message.reply_text(
            text[i:i+3500]
        )


# ================= RUN =================
init_db()

app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("all_users", all_users_cmd))
app.add_handler(CallbackQueryHandler(button))

print("Bot Running...")
app.run_polling()
