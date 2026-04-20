import sqlite3
import random
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

TOKEN = "8625949012:AAE8Z8KnLRIdqC9GjdvXcqdyUR5zh77SA3c"
BOT_USERNAME = "JomJudi_bot"
ADMIN_IDS = {"909399622"}
CHANNEL_ID = "@jomjudi88cuci"
GROUP_ID = "@jomjudi88official"

# ================= DB =================
def init_db():
    conn = sqlite3.connect("bot.db")
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        name TEXT,
        points INTEGER DEFAULT 0,
        invited_count INTEGER DEFAULT 0,
        spin_chances INTEGER DEFAULT 0,
        gift_claimed INTEGER DEFAULT 0,
        referrer_id TEXT
    )
    """)

    # 兼容旧表：如果没有 referrer_id 字段就自动补上
    cur.execute("PRAGMA table_info(users)")
    columns = [row[1] for row in cur.fetchall()]
    if "referrer_id" not in columns:
        cur.execute("ALTER TABLE users ADD COLUMN referrer_id TEXT")

    conn.commit()
    conn.close()


def get_user(user_id):
    conn = sqlite3.connect("bot.db")
    cur = conn.cursor()
    cur.execute("""
        SELECT user_id, name, points, invited_count, spin_chances, gift_claimed, referrer_id
        FROM users
        WHERE user_id=?
    """, (user_id,))
    user = cur.fetchone()
    conn.close()
    return user


def create_user(user_id, name, referrer_id=None):
    conn = sqlite3.connect("bot.db")
    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO users
        (user_id, name, points, invited_count, spin_chances, gift_claimed, referrer_id)
        VALUES (?, ?, 0, 0, 0, 0, ?)
    """, (user_id, name, referrer_id))
    conn.commit()
    conn.close()


def add_spin(user_id, amount):
    conn = sqlite3.connect("bot.db")
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET spin_chances = spin_chances + ? WHERE user_id=?",
        (amount, user_id)
    )
    conn.commit()
    conn.close()


def add_points(user_id, amount):
    conn = sqlite3.connect("bot.db")
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET points = points + ? WHERE user_id=?",
        (amount, user_id)
    )
    conn.commit()
    conn.close()


def add_invite(referrer_id):
    conn = sqlite3.connect("bot.db")
    cur = conn.cursor()
    cur.execute("""
        UPDATE users
        SET invited_count = invited_count + 1,
            points = points + 1
        WHERE user_id=?
    """, (referrer_id,))
    conn.commit()
    conn.close()


def get_all_users():
    conn = sqlite3.connect("bot.db")
    cur = conn.cursor()
    cur.execute("""
        SELECT user_id, name, points, invited_count, spin_chances, gift_claimed, referrer_id
        FROM users
        ORDER BY invited_count DESC, points DESC, name ASC
    """)
    rows = cur.fetchall()
    conn.close()
    return rows


def get_top_invites(limit=10):
    conn = sqlite3.connect("bot.db")
    cur = conn.cursor()
    cur.execute("""
        SELECT user_id, name, points, invited_count
        FROM users
        ORDER BY invited_count DESC, points DESC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows


# ================= MAIN MENU =================
def get_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔐 Daftar Akaun Baru", url="https://jomjudi88.live/my/register/?referral=JJ27817922")],
        [InlineKeyboardButton("💰 Touch 'n Go RM100", callback_data="menu")],
        [InlineKeyboardButton("🎁 New Customer Gift", callback_data="gift")],
        [InlineKeyboardButton("🔥 Claim ANGPOW FREE Hari-Hari", url="https://jomjudi88.live/my/")],
        [
            InlineKeyboardButton("📢 Sertai Channel", url="https://t.me/jomjudi88cuci"),
            InlineKeyboardButton("👥 Sertai Group", url="https://t.me/jomjudi88official")
        ],
        [InlineKeyboardButton("🎧 Hubungi Support", callback_data="support")]
    ])


def get_main_text():
    return (
        "Selamat Datang ke JomJudi88 — Sistem Layan Diri Terpantas! 🚀\n\n"
        "🙅‍♂️ Tanpa ejen | ⏳ Tanpa tunggu\n"
        "💸 Deposit → Main → Withdraw semua auto, terus masuk! ⚡️\n\n"
        "🚫 Website lain? Lambat & banyak masalah\n"
        "✅ JomJudi88: Pantas, telus & dipercayai ramai 🔥\n\n"
        "🎁 PROMOSI EKSKLUSIF:\n"
        "• 🎉 Kredit percuma setiap hari\n"
        "• 💰 Bonus 50% untuk ahli baru\n"
        "• 💳 Min deposit hanya RM5\n"
        "• 🧮 Min withdraw RM50\n\n"
        "📲 Claim sendiri bila-bila masa — sistem 24 jam nonstop!\n\n"
        "🧠 Sistem Auto Layan Diri:\n"
        "✔ Daftar & mula dalam beberapa klik\n"
        "✔ Transaksi real-time, 100% telus\n"
        "✔ Sokongan 24/7\n"
        "✔ Privasi dijaga sepenuhnya 🔐\n\n"
        "👉 Daftar sekarang & mula menang!"
    )


async def send_main_menu(chat_id, context: ContextTypes.DEFAULT_TYPE):
    try:
        with open("banner.jpg", "rb") as photo:
            await context.bot.send_photo(chat_id=chat_id, photo=photo)
    except Exception as e:
        print("Gagal hantar gambar:", e)

    await context.bot.send_message(
        chat_id=chat_id,
        text=get_main_text(),
        reply_markup=get_main_keyboard()
    )


async def send_menu_only(chat_id, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=chat_id,
        text="🏠 Main Menu\n\nSila pilih menu di bawah:",
        reply_markup=get_main_keyboard()
    )


# ================= START =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    user_id = str(user.id)
    user_name = user.first_name or ""

    referrer_id = None
    if context.args:
        referrer_id = context.args[0]

    existing_user = get_user(user_id)

    if not existing_user:
        valid_referrer = None

        # 条件：推荐人存在、不是自己
        if referrer_id and referrer_id != user_id:
            ref_user = get_user(referrer_id)
            if ref_user:
                valid_referrer = referrer_id

        create_user(user_id, user_name, valid_referrer)

        if valid_referrer:
            add_invite(valid_referrer)
            try:
                await context.bot.send_message(
                    chat_id=int(valid_referrer),
                    text="🎉 Anda dapat 1 referral baru!\n⭐ 1 point telah ditambah ke akaun anda."
                )
            except Exception as e:
                print("Gagal notify referrer:", e)

    await send_main_menu(update.effective_chat.id, context)


# ================= BUTTON =================
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = str(query.from_user.id)
    user = get_user(user_id)

    if not user:
        create_user(user_id, query.from_user.first_name or "")
        user = get_user(user_id)

    if query.data == "menu":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 My Profile", callback_data="profile")],
            [InlineKeyboardButton("🔗 My Referral Link", callback_data="link")],
            [InlineKeyboardButton("👥 Total Invites", callback_data="invite")],
            [InlineKeyboardButton("🔙 Back", callback_data="back")]
        ])
        await query.message.reply_text(
            "💰 Claim Touch'N Go FREE RM100\n\n"
            "Syarat untuk claim reward:\n\n"
            "• Minimum 10 referral join bot ❌\n"
            "• Mesti ada akaun berdaftar di JomJudi88\n"
            "• Share referral link ke Facebook / Telegram / kawan-kawan\n"
            "  (1 referral = 1 point)\n\n"
            "🎁 Ganjaran:\n"
            "• 10 Point = RM5 Kredit Game\n"
            "• 20 Point = RM10 Kredit Game\n"
            "• 50 Point = RM25 Kredit Game\n"
            "• 100 Point = RM50 Kredit Game\n"
            "• 200 Point = Touch 'n Go RM100\n\n"
            "👇 Select an option below:",
            reply_markup=keyboard
        )

    elif query.data == "profile":
        await query.message.reply_text(
            f"👤 My Profile\n\n"
            f"ID: {user[0]}\n"
            f"Points: {user[2]}\n"
            f"Invites: {user[3]}\n"
            f"Spin Chances: {user[4]}"
        )

    elif query.data == "link":
        link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
        await query.message.reply_text(
            f"🔗 Your Referral Link:\n\n{link}"
        )

    elif query.data == "invite":
        await query.message.reply_text(
            f"👥 Total Invites: {user[3]}"
        )

    elif query.data == "gift":
        if user[5] == 1:
            await query.message.reply_text(
                "❌ You have already claimed this gift."
            )
        else:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Claim Gift", callback_data="claim_gift")],
                [InlineKeyboardButton("🔙 Back", callback_data="back")]
            ])

            await query.message.reply_text(
                "🎁 New Customer Gift\n\n"
                "Sila lengkapkan semua mission berikut untuk claim reward anda ⚠️\n\n"
                "✅ Daftar Akaun Baru\n"
                "✅ First Deposit RM20.00 ke atas\n"
                "✅ Sertai Channel & Group\n\n"
                "🎁 Reward:\n"
                "• RM38 Free Credit",
                reply_markup=keyboard
            )

    elif query.data == "claim_gift":
        telegram_user_id = query.from_user.id

        joined_channel = await is_user_joined(CHANNEL_ID, telegram_user_id, context)
        joined_group = await is_user_joined(GROUP_ID, telegram_user_id, context)

        if not joined_channel:
            await query.message.reply_text("❌ Please join our Channel first.")
            return

        if not joined_group:
            await query.message.reply_text("❌ Please join our Group first.")
            return

        for admin in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=int(admin),
                    text=(
                        f"New Customer Gift Request\n\n"
                        f"User ID: {user_id}\n"
                        f"Name: {query.from_user.first_name or '-'}"
                    ),
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("Approve", callback_data=f"approve_gift:{user_id}"),
                            InlineKeyboardButton("Reject", callback_data=f"reject_gift:{user_id}")
                        ]
                    ])
                )
            except Exception as e:
                print("Error:", e)

        await query.message.reply_text(
            "⏳ Your request has been submitted for review."
        )

    elif query.data.startswith("approve_gift:"):
        if str(query.from_user.id) not in ADMIN_IDS:
            await query.message.reply_text("❌ Admin only.")
            return

        target = query.data.split(":", 1)[1]
        target_user = get_user(target)

        if not target_user:
            await query.message.reply_text("❌ User not found.")
            return

        if target_user[5] == 1:
            await query.message.reply_text("⚠️ This gift has already been claimed.")
            return

        add_points(target, 38)

        conn = sqlite3.connect("bot.db")
        cur = conn.cursor()
        cur.execute("UPDATE users SET gift_claimed=1 WHERE user_id=?", (target,))
        conn.commit()
        conn.close()

        await query.edit_message_text(
            f"✅ Gift approved for user {target}"
        )

        try:
            await context.bot.send_message(
                chat_id=int(target),
                text=(
                    "🎉 Your New Customer Gift has been approved!\n\n"
                    "Rewards added:\n"
                    "• RM38 Free Credit"
                )
            )
        except Exception as e:
            print("Gagal notify user approve gift:", e)

    elif query.data.startswith("reject_gift:"):
        if str(query.from_user.id) not in ADMIN_IDS:
            await query.message.reply_text("❌ Admin only.")
            return

        target = query.data.split(":", 1)[1]

        await query.edit_message_text(
            f"❌ Gift request rejected for user {target}"
        )

        try:
            await context.bot.send_message(
                chat_id=int(target),
                text="❌ Your New Customer Gift request was not approved."
            )
        except Exception:
            pass

    elif query.data == "support":
        await query.message.reply_text(
            "🎧 Need help? Please contact our support team.\n\n"
            "📱 Whatsapp:\nhttps://jomjudi88.wasap.my/\n\n"
            "📲 Telegram:\nhttps://t.me/JomJudi88vip\n\n"
            "🌐 Website:\nhttps://jomjudi88.live/my/"
        )

    elif query.data == "back":
        await send_menu_only(query.message.chat_id, context)


# ================= ADMIN =================
async def add_spin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) not in ADMIN_IDS:
        await update.message.reply_text("Admin only.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("Usage: /add_spin USER_ID AMOUNT")
        return

    uid = context.args[0]

    try:
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text("Amount must be a number.")
        return

    add_spin(uid, amount)
    await update.message.reply_text("Spin added successfully.")


async def all_users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) not in ADMIN_IDS:
        await update.message.reply_text("Admin only.")
        return

    rows = get_all_users()
    if not rows:
        await update.message.reply_text("No users found.")
        return

    lines = ["📊 All Users Invite Report\n"]
    for i, row in enumerate(rows, start=1):
        user_id, name, points, invited_count, spin_chances, gift_claimed, referrer_id = row
        lines.append(
            f"{i}. {name or '-'}\n"
            f"ID: {user_id}\n"
            f"Invites: {invited_count}\n"
            f"Points: {points}\n"
            f"Spin: {spin_chances}\n"
            f"Gift Claimed: {'Yes' if gift_claimed else 'No'}\n"
            f"Referrer: {referrer_id or '-'}\n"
        )

    text = "\n".join(lines)

    # 避免 Telegram 单条过长
    chunk_size = 3500
    for i in range(0, len(text), chunk_size):
        await update.message.reply_text(text[i:i + chunk_size])


async def top_invites_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) not in ADMIN_IDS:
        await update.message.reply_text("Admin only.")
        return

    rows = get_top_invites(10)
    if not rows:
        await update.message.reply_text("No ranking data yet.")
        return

    lines = ["🏆 Top Invite Ranking\n"]
    for i, row in enumerate(rows, start=1):
        user_id, name, points, invited_count = row
        lines.append(
            f"{i}. {name or '-'}\n"
            f"ID: {user_id}\n"
            f"Invites: {invited_count}\n"
            f"Points: {points}\n"
        )

    await update.message.reply_text("\n".join(lines))

async def is_user_joined(chat_id: str, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        print(f"CHECK {chat_id} -> status: {member.status}")
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        print(f"Gagal semak membership {chat_id}: {e}")
        return False
# ================= RUN =================
init_db()

app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("add_spin", add_spin_cmd))
app.add_handler(CommandHandler("all_users", all_users_cmd))
app.add_handler(CommandHandler("top_invites", top_invites_cmd))
app.add_handler(CallbackQueryHandler(button))

print("Bot running...")
app.run_polling()