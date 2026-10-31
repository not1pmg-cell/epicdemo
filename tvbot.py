"""
Uzs TV Bot — PRO versiya
- Admin panel: kontent (qo'shish/tahrir/qidiruv/nusxa/yashirish/tanlangan), M3U/JSON import,
  obunalar (linklar, bonus kunlar, tez tugaydiganlar), foydalanuvchilar (ro'yxat/filtr/CSV/xabar),
  xabar yuborish (tugma, test, auditoriya), majburiy kanallar, sozlamalar (sinov, referal, texnik rejim,
  e'lon, xush kelibsiz matni...), adminlar boshqaruvi, zaxira (backup), tozalash, statistika
- Oylik obuna: admin link yaratadi (necha oy / necha kishi), foydalanuvchi link orqali obuna oladi
- Mini App uchun xavfsiz API (Telegram initData tekshiruvi bilan)
"""
import asyncio
import calendar
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import parse_qsl, urlparse

from aiohttp import ClientSession, ClientTimeout, web
from dotenv import load_dotenv

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("tvbot")

load_dotenv()

# ───────────────────────── SOZLAMALAR ─────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
# .env dagi ADMIN_IDS — "egalar" (owner). Ular qo'shimcha adminlar qo'sha/o'chira oladi.
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()]
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://not1pmg-cell.github.io/epicdemo/").strip()
API_PORT = os.getenv("PORT") or os.getenv("API_PORT")  # Render PORT beradi
PUBLIC_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
CORS_ORIGIN_ENV = os.getenv("CORS_ORIGIN", "").strip()  # bo'sh bo'lsa MINI_APP_URL dan olinadi

if not BOT_TOKEN:
    raise SystemExit("❌ Xatolik: BOT_TOKEN .env faylida topilmadi!")
if not ADMIN_IDS:
    logger.warning("ADMIN_IDS bo'sh — admin panel hech kimga ochilmaydi!")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "data.json")
USERS_FILE = os.path.join(BASE_DIR, "users.json")
SUBS_FILE = os.path.join(BASE_DIR, "subscriptions.json")
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")
REQ_CHANNELS_FILE = os.path.join(BASE_DIR, "required_channels.json")
REQUESTS_FILE = os.path.join(BASE_DIR, "channel_requests.json")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
admin_r = Router(name="admin")
user_r = Router(name="user")
# is_admin() pastda aniqlanadi (qo'shimcha adminlar settings.json da saqlanadi)
admin_r.message.filter(lambda m: bool(m.from_user) and is_admin(m.from_user.id))
admin_r.callback_query.filter(lambda c: is_admin(c.from_user.id))

BOT_USERNAME = ""
_bg_tasks: set = set()

TYPES = {
    "tv": "📺 TV kanal",
    "football": "⚽ Futbol",
    "movies": "🎬 Film",
    "series": "🍿 Serial",
    "cartoons": "🧸 Multfilm",
}
BADGES = {"tv": "TV", "football": "FUTBOL", "movies": "KINO", "series": "SERIAL", "cartoons": "MULTFILM"}
TEXT = F.text & ~F.text.startswith("/")


# ───────────────────────── JSON SAQLASH ─────────────────────────
def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("O'qishda xatolik %s: %s", path, e)
        return default


def save_json(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.error("Saqlashda xatolik %s: %s", path, e)


def utcnow():
    return datetime.now(timezone.utc)


def parse_dt(s):
    return datetime.fromisoformat(s)


def fmt_dt(dt):
    return (dt + timedelta(hours=5)).strftime("%d.%m.%Y %H:%M")  # Toshkent vaqti


TZ_UZ = timezone(timedelta(hours=5))


def parse_match_time(s):
    """'21:00' (bugun), '31.10 21:00', '2026-10-31 21:00' yoki ISO -> ISO (+05:00). Noto'g'ri bo'lsa None."""
    s = (s or "").strip()
    if not s:
        return None
    now = datetime.now(TZ_UZ)
    try:
        m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", s)
        if m:
            h, mi = int(m.group(1)), int(m.group(2))
            return now.replace(hour=h, minute=mi, second=0, microsecond=0).isoformat()
        m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\s+(\d{1,2})[:.](\d{2})", s)
        if m:
            d, mo, y, h, mi = m.groups()
            return datetime(int(y or now.year), int(mo), int(d), int(h), int(mi), tzinfo=TZ_UZ).isoformat()
        dt = datetime.fromisoformat(s.replace(" ", "T", 1) if "T" not in s else s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ_UZ)
        return dt.isoformat()
    except ValueError:
        return None


def add_months(dt, months):
    total = dt.month - 1 + months
    y, m = dt.year + total // 12, total % 12 + 1
    d = min(dt.day, calendar.monthrange(y, m)[1])
    return dt.replace(year=y, month=m, day=d)


# ── Kontent
def load_items():
    return load_json(DATA_FILE, {"items": []}).get("items", [])


def save_items(items):
    save_json(DATA_FILE, {"items": items})


# ── Foydalanuvchilar (eski ro'yxat formatidan avtomatik o'tadi)
def load_users():
    raw = load_json(USERS_FILE, {})
    if isinstance(raw, list):
        raw = {
            str(uid): {"name": "", "username": "", "joined": utcnow().isoformat(),
                       "banned": False, "blocked": False}
            for uid in raw
        }
        save_json(USERS_FILE, raw)
    return raw


def touch_user(tg_user):
    """Foydalanuvchini bazaga yozadi. Yangi bo'lsa True qaytaradi."""
    users = load_users()
    key = str(tg_user.id)
    rec = users.get(key)
    changed = False
    is_new = rec is None
    if rec is None:
        rec = {"joined": utcnow().isoformat(), "banned": False, "blocked": False}
        changed = True
    name = tg_user.full_name or ""
    uname = tg_user.username or ""
    if rec.get("name") != name or rec.get("username") != uname or rec.get("blocked"):
        rec["name"], rec["username"], rec["blocked"] = name, uname, False
        changed = True
    users[key] = rec
    if changed:
        save_json(USERS_FILE, users)
    return is_new


def is_banned(uid):
    return bool(load_users().get(str(uid), {}).get("banned"))


# ── Sozlamalar (xotirada keshlanadi)
DEFAULT_SETTINGS = {
    "paywall": False,
    "support": "",
    "price_text": "💳 Obuna narxlari va to'lov uchun admin bilan bog'laning.",
    "welcome": "👋 <b>Uzs TV botiga</b> xush kelibsiz, {name}!",
    "maintenance": False,
    "maintenance_text": "🛠 Hozir texnik ishlar olib borilmoqda. Birozdan so'ng qayta urinib ko'ring.",
    "trial_days": 0,          # yangi foydalanuvchiga bepul sinov (kun), 0 = o'chiq
    "referral_days": 0,       # taklif qilgan foydalanuvchiga bonus (kun), 0 = o'chiq
    "announcement": "",       # Mini App'da ko'rinadigan e'lon
    "notify_admins": True,    # yangi foydalanuvchi / obuna haqida adminlarga xabar
    "auto_backup": True,      # har 24 soatda zaxira nusxani egaga yuborish
    "auto_status": True,      # TV kanal havolalarini avtomatik tekshirish (status ok/down)
    "match_reminders": True,  # kuzatilgan jamoa o'yini oldidan botdan eslatma yuborish
    "last_backup": "",
    "admins": [],             # qo'shimcha adminlar (ID lar)
    "mini_app_url": "",       # bo'sh bo'lsa .env dagi MINI_APP_URL ishlatiladi
}
_settings_cache = None


def load_settings():
    global _settings_cache
    if _settings_cache is None:
        s = dict(DEFAULT_SETTINGS)
        s.update(load_json(SETTINGS_FILE, {}))
        _settings_cache = s
    return dict(_settings_cache)


def save_settings(s):
    global _settings_cache
    _settings_cache = dict(s)
    save_json(SETTINGS_FILE, s)


def reset_settings_cache():
    global _settings_cache
    _settings_cache = None


def get_mini_app_url():
    return load_settings().get("mini_app_url", "").strip() or MINI_APP_URL


def support_url():
    s = load_settings().get("support", "").strip()
    if not s:
        return None
    if s.startswith("http"):
        return s
    return f"https://t.me/{s.lstrip('@')}"


def all_admin_ids():
    extra = []
    for x in load_settings().get("admins", []):
        try:
            extra.append(int(x))
        except (TypeError, ValueError):
            pass
    return list(dict.fromkeys(ADMIN_IDS + extra))


def is_admin(uid):
    return uid in ADMIN_IDS or uid in all_admin_ids()


def is_owner(uid):
    return uid in ADMIN_IDS


# ── Majburiy kanallar
def load_req_channels():
    return load_json(REQ_CHANNELS_FILE, [])


# ── Obunalar
def load_subs():
    db = load_json(SUBS_FILE, {})
    db.setdefault("subs", {})
    db.setdefault("links", {})
    return db


def save_subs(db):
    save_json(SUBS_FILE, db)


def sub_expiry(uid):
    s = load_subs()["subs"].get(str(uid))
    return parse_dt(s["expires"]) if s else None


def is_subscribed(uid):
    if is_admin(uid):
        return True
    exp = sub_expiry(uid)
    return bool(exp and exp > utcnow())

def _grant(db, uid, months, source, days=0):
    rec = db["subs"].get(str(uid))
    base = utcnow()
    if rec:
        cur = parse_dt(rec["expires"])
        if cur > base:
            base = cur
    new_exp = add_months(base, months) + timedelta(days=days)
    if not rec:
        rec = {"history": []}
    rec["expires"] = new_exp.isoformat()
    rec["warned"] = False
    rec["expired_notified"] = False
    rec.setdefault("history", []).append(
        {"at": utcnow().isoformat(), "months": months, "days": days, "source": source}
    )
    db["subs"][str(uid)] = rec
    return new_exp


def grant_days(uid, days, source):
    db = load_subs()
    exp = _grant(db, uid, 0, source, days=days)
    save_subs(db)
    return exp


def grant(uid, months, source):
    db = load_subs()
    exp = _grant(db, uid, months, source)
    save_subs(db)
    return exp


def revoke(uid):
    db = load_subs()
    ok = db["subs"].pop(str(uid), None) is not None
    save_subs(db)
    return ok


def create_link(months, max_uses, admin_id):
    db = load_subs()
    code = secrets.token_urlsafe(6)
    while code in db["links"]:
        code = secrets.token_urlsafe(6)
    db["links"][code] = {
        "months": months, "max_uses": max_uses, "used_by": [],
        "active": True, "created": utcnow().isoformat(), "created_by": admin_id,
    }
    save_subs(db)
    return code


def redeem_code(uid, code):
    db = load_subs()
    link = db["links"].get(code)
    if not link:
        return False, "❌ Bunday link yoki kod topilmadi.", None
    if not link.get("active", True):
        return False, "❌ Bu link o'chirilgan.", None
    if uid in link["used_by"]:
        return False, "ℹ️ Siz bu linkdan allaqachon foydalangansiz.", None
    if len(link["used_by"]) >= link["max_uses"]:
        return False, "❌ Bu linkning limiti tugagan.", None
    link["used_by"].append(uid)
    exp = _grant(db, uid, link["months"], f"link:{code}")
    save_subs(db)
    return True, f"🎉 <b>Obuna faollashtirildi!</b>\n📅 {link['months']} oy qo'shildi.\n⏳ Amal qilish muddati: <b>{fmt_dt(exp)}</b>", exp


def make_link_url(code):
    return f"https://t.me/{BOT_USERNAME}?start=sub_{code}"

# ───────────────────────── YORDAMCHI FUNKSIYALAR ─────────────────────────
def b(text, cb):
    return InlineKeyboardButton(text=text, callback_data=cb)


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


def paginate(seq, page, per=8):
    pages = max(1, (len(seq) + per - 1) // per)
    page = max(0, min(page, pages - 1))
    return seq[page * per:(page + 1) * per], page, pages


def nav_row(prefix, page, pages):
    row = []
    if page > 0:
        row.append(b("⬅️", f"{prefix}:{page - 1}"))
    row.append(b(f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        row.append(b("➡️", f"{prefix}:{page + 1}"))
    return row


async def show(event, text, markup=None):
    """Callback bo'lsa xabarni tahrirlaydi, aks holda yangi xabar yuboradi."""
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                await event.message.answer(text, reply_markup=markup, disable_web_page_preview=True)
        await event.answer()
    else:
        await event.answer(text, reply_markup=markup, disable_web_page_preview=True)


def user_label(uid):
    rec = load_users().get(str(uid), {})
    name = escape(rec.get("name") or "—")
    un = f" @{escape(rec['username'])}" if rec.get("username") else ""
    return f"{name}{un} (<code>{uid}</code>)"


async def safe_send(uid, text, **kw):
    try:
        await bot.send_message(uid, text, **kw)
        return True
    except TelegramForbiddenError:
        users = load_users()
        if str(uid) in users:
            users[str(uid)]["blocked"] = True
            save_json(USERS_FILE, users)
    except Exception as e:
        logger.warning("Xabar yuborilmadi %s: %s", uid, e)
    return False
async def notify_admins(text):
    if not load_settings().get("notify_admins", True):
        return
    for aid in all_admin_ids():
        await safe_send(aid, text)


async def on_new_user(user):
    days = int(load_settings().get("trial_days") or 0)
    if days > 0 and not is_admin(user.id):
        grant_days(user.id, days, "trial")
    await notify_admins(f"🆕 Yangi foydalanuvchi: {user_label(user.id)}")


# ───────────────────────── MIDDLEWARE ─────────────────────────
class GuardMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        data["is_new"] = False
        if user and not user.is_bot:
            is_new = touch_user(user)
            data["is_new"] = is_new
            if is_new:
                await on_new_user(user)
            if not is_admin(user.id):
                text = None
                if is_banned(user.id):
                    text = "🚫 Siz bloklangansiz."
                elif load_settings().get("maintenance"):
                    text = load_settings().get("maintenance_text") or "🛠 Texnik ishlar."
                if text:
                    if isinstance(event, CallbackQuery):
                        await event.answer(re.sub(r"<[^>]+>", "", text)[:190], show_alert=True)
                    elif isinstance(event, Message):
                        await event.answer(text)
                    return
        return await handler(event, data)


dp.message.outer_middleware(GuardMiddleware())
dp.callback_query.outer_middleware(GuardMiddleware())


# ───────────────────────── FSM HOLATLARI ─────────────────────────
class AddState(StatesGroup):
    title = State()
    url = State()
    language = State()
    genre = State()
    match_time = State()
    premium = State()
    poster = State()


class EditState(StatesGroup):
    value = State()


class LinkState(StatesGroup):
    months = State()
    people = State()


class UserFindState(StatesGroup):
    query = State()


class UserGrantState(StatesGroup):
    days = State()


class UserMsgState(StatesGroup):
    content = State()


class BonusState(StatesGroup):
    days = State()


class BroadcastState(StatesGroup):
    content = State()
    button = State()


class ChannelState(StatesGroup):
    ref = State()


class SettingState(StatesGroup):
    value = State()


class RedeemState(StatesGroup):
    code = State()


class SearchState(StatesGroup):
    query = State()


class ImportState(StatesGroup):
    file = State()
    options = State()


class AdminAddState(StatesGroup):
    uid = State()


async def check_sub_channels(user_id):
    if is_admin(user_id):
        return []
    missing = []
    for ch in load_req_channels():
        try:
            m = await bot.get_chat_member(chat_id=ch["chat_id"], user_id=user_id)
            if m.status in ("left", "kicked") or (m.status == "restricted" and not m.is_member):
                missing.append(ch)
        except Exception as e:
            logger.warning("Kanal tekshirib bo'lmadi %s: %s (bot kanalda adminmi?)", ch.get("title"), e)
    return missing


def sub_keyboard(channels):
    rows = [[InlineKeyboardButton(text=f"📢 {ch['title']}", url=ch["url"])] for ch in channels if ch.get("url")]
    rows.append([b("✅ Obunani tekshirish", "check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_menu(uid):
    """Faqat Mini App tugmasi (+ adminlar uchun Admin panel)."""
    rows = [[InlineKeyboardButton(text="📺 Uzs TV ochish", web_app=WebAppInfo(url=get_mini_app_url()))]]
    if is_admin(uid):
        rows.append([b("🛠 Admin panel", "admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def welcome_text(user, uid=None):
    s = load_settings()
    name = escape(getattr(user, "full_name", "") or "")
    txt = (s.get("welcome") or DEFAULT_SETTINGS["welcome"]).replace("{name}", name)
    if uid and s.get("paywall") and not is_subscribed(uid):
        txt += f"\n\n{escape(s.get('price_text', ''))}"
    return txt


async def send_menu(event, uid, text=None):
    if text is None:
        text = welcome_text(event.from_user, uid)
    await show(event, text, main_menu(uid))


async def handle_referral(uid, arg):
    days = int(load_settings().get("referral_days") or 0)
    if days <= 0:
        return
    try:
        inviter = int(arg)
    except ValueError:
        return
    users = load_users()
    if inviter == uid or str(inviter) not in users or str(uid) not in users:
        return
    users[str(uid)]["ref"] = inviter
    save_json(USERS_FILE, users)
    exp = grant_days(inviter, days, f"ref:{uid}")
    await safe_send(inviter, f"🎉 Sizning havolangiz orqali yangi foydalanuvchi qo'shildi!\n"
                             f"🎁 Bonus: <b>+{days} kun</b>\n⏳ Obuna muddati: <b>{fmt_dt(exp)}</b>")


@user_r.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext, is_new: bool = False):
    await state.clear()
    uid = message.from_user.id
    arg = command.args or ""
    if arg.startswith("sub_"):
        ok, text, _ = redeem_code(uid, arg[4:])
        await message.answer(text)
        if ok:
            await notify_admins(f"🎟 {user_label(uid)} link orqali obuna oldi (<code>{escape(arg[4:])}</code>)")
    elif arg.startswith("ref_") and is_new:
        await handle_referral(uid, arg[4:])
    if is_new:
        days = int(load_settings().get("trial_days") or 0)
        if days > 0 and not is_admin(uid):
            await message.answer(f"🎁 Sizga <b>{days} kunlik</b> bepul sinov obunasi berildi!")
    missing = await check_sub_channels(uid)
    if missing:
        await message.answer("⚠️ Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling:",
                             reply_markup=sub_keyboard(missing))
        return
    await send_menu(message, uid)


@user_r.callback_query(F.data == "check_sub")
async def cb_check_sub(cb: CallbackQuery):
    missing = await check_sub_channels(cb.from_user.id)
    if missing:
        await cb.answer("❌ Hali hamma kanalga obuna bo'lmadingiz.", show_alert=True)
        return
    await send_menu(cb, cb.from_user.id)


@user_r.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await send_menu(cb, cb.from_user.id, "🏠 <b>Asosiy menyu</b>")


@user_r.callback_query(F.data == "noop")
@admin_r.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


@user_r.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(f"🆔 Sizning ID: <code>{message.from_user.id}</code>")


@user_r.message(Command("help"))
async def cmd_help(message: Message):
    text = ("ℹ️ <b>Yordam</b>\n\n/start — bosh menyu\n/profile — obuna holati\n/redeem — obuna kodini kiritish\n"
            "/id — Telegram ID\n/cancel — amalni bekor qilish")
    if is_admin(message.from_user.id):
        text += ("\n\n🛠 <b>Admin buyruqlari</b>\n/admin — admin panel\n/user &lt;id|@username&gt; — foydalanuvchi kartasi\n"
                 "/grant &lt;id|@username&gt; &lt;kun&gt; — obuna berish\n/ban &lt;id&gt; · /unban &lt;id&gt;\n"
                 "/backup — zaxira nusxa")
    await message.answer(text)


@user_r.message(Command("redeem"))
async def cmd_redeem(message: Message, state: FSMContext, command: CommandObject):
    if command.args:
        code = command.args.strip().split("sub_")[-1].strip()
        ok, text, _ = redeem_code(message.from_user.id, code)
        await message.answer(text, reply_markup=main_menu(message.from_user.id))
        if ok:
            await notify_admins(f"🎟 {user_label(message.from_user.id)} kod orqali obuna oldi (<code>{escape(code)}</code>)")
        return
    await state.set_state(RedeemState.code)
    await message.answer("🎟 Obuna kodini yuboring (yoki /cancel):")


async def profile_text(uid):
    exp = sub_expiry(uid)
    if is_admin(uid):
        st = "👑 Admin (cheksiz)"
    elif exp and exp > utcnow():
        left = exp - utcnow()
        st = f"✅ Faol\n⏳ Tugash: <b>{fmt_dt(exp)}</b>\n📆 Qoldi: {left.days} kun"
    elif exp:
        st = f"⛔️ Muddati tugagan ({fmt_dt(exp)})"
    else:
        st = "➖ Obuna yo'q"
    text = f"👤 <b>Profil</b>\n🆔 <code>{uid}</code>\n\n💎 Obuna: {st}"
    days = int(load_settings().get("referral_days") or 0)
    if days > 0 and BOT_USERNAME:
        refs = sum(1 for u in load_users().values() if u.get("ref") == uid)
        text += (f"\n\n👥 <b>Do'stlarni taklif qiling</b> — har biri uchun <b>+{days} kun</b>!\n"
                 f"🔗 <code>https://t.me/{BOT_USERNAME}?start=ref_{uid}</code>\n📈 Taklif qilinganlar: {refs}")
    return text


@user_r.message(Command("profile"))
async def cmd_profile(message: Message):
    await message.answer(await profile_text(message.from_user.id), reply_markup=kb([b("⬅️ Menyu", "menu")]))


@user_r.callback_query(F.data == "me")
async def cb_me(cb: CallbackQuery):
    await show(cb, await profile_text(cb.from_user.id), kb([b("⬅️ Menyu", "menu")]))


@user_r.callback_query(F.data == "buy")
async def cb_buy(cb: CallbackQuery):
    rows = []
    url = support_url()
    if url:
        rows.append([InlineKeyboardButton(text="📞 Admin bilan bog'lanish", url=url)])
    rows.append([b("🎟 Kod kiritish", "redeem")])
    rows.append([b("⬅️ Menyu", "menu")])
    await show(cb, f"💎 <b>Premium obuna</b>\n\n{escape(load_settings()['price_text'])}", InlineKeyboardMarkup(inline_keyboard=rows))


@user_r.callback_query(F.data == "redeem")
async def cb_redeem(cb: CallbackQuery, state: FSMContext):
    await state.set_state(RedeemState.code)
    await show(cb, "🎟 Obuna kodini yuboring (yoki /cancel):")


@user_r.message(RedeemState.code, TEXT)
async def msg_redeem(message: Message, state: FSMContext):
    code = message.text.strip().split("sub_")[-1].strip()
    ok, text, _ = redeem_code(message.from_user.id, code)
    await state.clear()
    await message.answer(text, reply_markup=main_menu(message.from_user.id))


# ───────────────────────── /cancel ─────────────────────────
@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❎ Bekor qilindi.", reply_markup=main_menu(message.from_user.id))

# ───────────────────────── ADMIN: ASOSIY ─────────────────────────
def admin_home_kb():
    s = load_settings()
    return kb(
        [b("⚽ Bugungi o'yin qo'shish", "mt:new")],
        [b("📣 E'lon" + (" ✅" if s.get("announcement") else ""), "se:announcement"),
         b("🛠 Texnik ish: " + ("YOQIQ" if s.get("maintenance") else "o'chiq"), "set:tg:maintenance")],
        [b("📩 Kanal so'rovlari", "rq:0")],
        [b("➕ Kontent qo'shish", "adm:add"), b("📋 Kontent ro'yxati", "lst:0")],
        [b("🔎 Kontent qidirish", "srch:start"), b("📥 Import (M3U/JSON)", "imp:menu")],
        [b("💎 Obunalar", "sub:menu"), b("👤 Foydalanuvchilar", "usr:menu")],
        [b("📢 Xabar yuborish", "bc:menu"), b("📡 Majburiy kanallar", "cr:menu")],
        [b("📊 Statistika", "stats"), b("⚙️ Sozlamalar", "set:menu")],
        [b("💾 Zaxira / Eksport", "bk:menu"), b("🧹 Tozalash", "cl:menu")],
        [b("👑 Adminlar", "own:menu")],
        [b("🏠 Asosiy menyu", "menu")],
    )


def admin_home_text():
    users = load_users()
    now = utcnow()
    active = sum(1 for s in load_subs()["subs"].values() if parse_dt(s["expires"]) > now)
    s = load_settings()
    flags = []
    if s["maintenance"]:
        flags.append("🛠 texnik rejim YOQIQ")
    if s["paywall"]:
        flags.append("💳 pullik rejim")
    return ("🛠 <b>Admin boshqaruv paneli</b>\n\n"
            f"👥 {len(users)} foydalanuvchi · 💎 {active} faol obuna · 📺 {len(load_items())} kontent"
            + (f"\n⚠️ {' · '.join(flags)}" if flags else ""))


@admin_r.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(admin_home_text(), reply_markup=admin_home_kb())


@admin_r.callback_query(F.data == "admin")
async def cb_admin(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, admin_home_text(), admin_home_kb())


# ───────────────────────── ADMIN: TEZ BUYRUQLAR ─────────────────────────
@admin_r.message(Command("user"))
async def cmd_user(message: Message, command: CommandObject):
    uid = find_user(command.args or "")
    if uid is None:
        await message.answer("Ishlatish: <code>/user 123456789</code> yoki <code>/user @username</code>")
        return
    text, markup = user_card(uid)
    await message.answer(text, reply_markup=markup)


@admin_r.message(Command("grant"))
async def cmd_grant(message: Message, command: CommandObject):
    parts = (command.args or "").split()
    uid = find_user(parts[0]) if parts else None
    if uid is None or len(parts) < 2 or not parts[1].isdigit() or not 1 <= int(parts[1]) <= 3650:
        await message.answer("Ishlatish: <code>/grant &lt;id|@username&gt; &lt;kun&gt;</code>\nMasalan: <code>/grant 123456789 30</code>")
        return
    days = int(parts[1])
    exp = grant_days(uid, days, f"admin:{message.from_user.id}")
    await safe_send(uid, f"🎁 Sizga <b>{days} kunlik</b> obuna berildi!\n⏳ Amal qilish muddati: <b>{fmt_dt(exp)}</b>")
    await message.answer(f"✅ {user_label(uid)} ga {days} kun berildi. Tugash: <b>{fmt_dt(exp)}</b>")


@admin_r.message(Command("ban"))
@admin_r.message(Command("unban"))
async def cmd_ban(message: Message, command: CommandObject):
    uid = find_user(command.args or "")
    if uid is None:
        await message.answer("Ishlatish: <code>/ban 123456789</code> yoki <code>/unban @username</code>")
        return
    if is_admin(uid):
        await message.answer("Adminni banlab bo'lmaydi.")
        return
    want = message.text.startswith("/ban")
    users = load_users()
    users[str(uid)]["banned"] = want
    save_json(USERS_FILE, users)
    await message.answer(f"{'🚫 Banlandi' if want else '✅ Unban qilindi'}: {user_label(uid)}")


@admin_r.message(Command("backup"))
async def cmd_backup(message: Message):
    await send_backup(message.chat.id)


# ───────────────────────── ADMIN: STATISTIKA ─────────────────────────
def spark(n, mx):
    return "▓" * max(1, round(10 * n / mx)) if mx and n else "·"


@admin_r.callback_query(F.data == "stats")
async def cb_stats(cb: CallbackQuery):
    users = load_users()
    db = load_subs()
    items = load_items()
    now = utcnow()
    today = now.date().isoformat()
    new_today = sum(1 for u in users.values() if u.get("joined", "")[:10] == today)
    active = sum(1 for s in db["subs"].values() if parse_dt(s["expires"]) > now)
    expired = len(db["subs"]) - active
    links_active = sum(1 for l in db["links"].values() if l.get("active", True))
    redeemed = sum(len(l["used_by"]) for l in db["links"].values())
    by_type = "\n".join(
        f"  {label}: {sum(1 for i in items if i.get('content_type') == t)}" for t, label in TYPES.items())
    views = sum(int(i.get("views", 0)) for i in items)

    days = [(now - timedelta(days=i)).date().isoformat() for i in range(6, -1, -1)]
    cnt = {d: 0 for d in days}
    for u in users.values():
        d = u.get("joined", "")[:10]
        if d in cnt:
            cnt[d] += 1
    mx = max(cnt.values())
    chart = "\n".join(f"<code>{d[5:]}</code> {spark(cnt[d], mx)} {cnt[d]}" for d in days)

    top = sorted(items, key=lambda i: int(i.get("views", 0)), reverse=True)[:5]
    top_txt = "\n".join(f"  {n}. {escape(i['title'][:30])} — 👁 {i.get('views', 0)}" for n, i in enumerate(top, 1)) or "  —"

    sources = {"trial": 0, "link": 0, "admin": 0, "ref": 0}
    for s in db["subs"].values():
        for h in s.get("history", []):
            k = str(h.get("source", "")).split(":")[0]
            if k in sources:
                sources[k] += 1
    text = (
        "📊 <b>Statistika</b>\n\n"
        f"👥 Foydalanuvchilar: <b>{len(users)}</b> (bugun +{new_today})\n"
        f"🚫 Banlangan: {sum(1 for u in users.values() if u.get('banned'))} | "
        f"🔕 Botni bloklagan: {sum(1 for u in users.values() if u.get('blocked'))}\n\n"
        f"📈 <b>Oxirgi 7 kun (yangi)</b>\n{chart}\n\n"
        f"💎 Faol obuna: <b>{active}</b> | Tugagan: {expired}\n"
        f"🧾 Manba: link {sources['link']} · admin {sources['admin']} · sinov {sources['trial']} · referal {sources['ref']}\n"
        f"🔗 Linklar: {len(db['links'])} (faol {links_active}) | Ishlatilgan: {redeemed}\n\n"
        f"📺 Kontent: <b>{len(items)}</b> (💎 {sum(1 for i in items if i.get('premium'))} premium, "
        f"🙈 {sum(1 for i in items if i.get('hidden'))} yashirin)\n{by_type}\n"
        f"👁 Jami ko'rishlar: {views}\n🔥 <b>Top 5</b>\n{top_txt}\n\n"
        f"📡 Majburiy kanallar: {len(load_req_channels())}\n"
        f"💳 Pullik rejim: {'yoqilgan ✅' if load_settings()['paywall'] else 'o`chirilgan ❌'}"
    )
    await show(cb, text, kb([b("🔄 Yangilash", "stats")], [b("⬅️ Admin panel", "admin")]))


# ───────────────────────── ADMIN: KONTENT QO'SHISH ─────────────────────────
@admin_r.callback_query(F.data == "adm:add")
async def cb_add_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    rows, row = [], []
    for t, label in TYPES.items():
        row.append(b(label, f"add:{t}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([b("⬅️ Admin panel", "admin")])
    await show(cb, "➕ Qaysi turdagi kontent qo'shamiz?", InlineKeyboardMarkup(inline_keyboard=rows))


@admin_r.callback_query(F.data.startswith("add:"))
async def cb_add_type(cb: CallbackQuery, state: FSMContext):
    t = cb.data.split(":")[1]
    if t not in TYPES:
        await cb.answer("Noma'lum tur", show_alert=True)
        return
    await state.clear()
    await state.set_state(AddState.title)
    await state.update_data(type=t)
    await show(cb, f"{TYPES[t]}\n\n📝 Nomini yozing (bekor qilish: /cancel):")


@admin_r.message(AddState.title, TEXT)
async def add_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await state.set_state(AddState.url)
    await message.answer("🔗 Translyatsiya havolasini yuboring (m3u8 / mp4 / YouTube):")


@admin_r.message(AddState.url, TEXT)
async def add_url(message: Message, state: FSMContext):
    url = message.text.strip()
    if not url.lower().startswith(("http://", "https://")):
        await message.answer("❌ Havola http:// yoki https:// bilan boshlanishi kerak. Qayta yuboring:")
        return
    await state.update_data(url=url)
    await state.set_state(AddState.language)
    await message.answer("🌐 Tilni tanlang:", reply_markup=kb(
        [b("🇺🇿 uz", "al:uz"), b("🇷🇺 ru", "al:ru"), b("🇬🇧 en", "al:en")]))


@admin_r.callback_query(AddState.language, F.data.startswith("al:"))
async def add_language(cb: CallbackQuery, state: FSMContext):
    await state.update_data(language=cb.data.split(":")[1])
    await state.set_state(AddState.genre)
    await show(cb, "🎭 Janrni yozing (masalan: Ko'ngilochar, Sport, Yangiliklar, Multfilm):")


@admin_r.message(AddState.genre, TEXT)
async def add_genre(message: Message, state: FSMContext):
    await state.update_data(genre=message.text.strip())
    data = await state.get_data()
    if data["type"] == "football":
        await state.set_state(AddState.match_time)
        await message.answer("⏰ O'yin vaqtini yozing (masalan: 22:00):")
    else:
        await ask_premium(message, state)


@admin_r.message(AddState.match_time, TEXT)
async def add_match_time(message: Message, state: FSMContext):
    iso = parse_match_time(message.text)
    if not iso:
        await message.answer("❌ Vaqt noto'g'ri. Masalan: <code>21:00</code> yoki <code>31.10 21:00</code>")
        return
    await state.update_data(match_time=iso)
    await ask_premium(message, state)


async def ask_premium(message, state):
    await state.set_state(AddState.premium)
    await message.answer("💎 Bu kontent faqat obunachilar uchunmi?", reply_markup=kb(
        [b("💎 Ha, premium", "ap:1"), b("🆓 Yo'q, bepul", "ap:0")]))


@admin_r.callback_query(AddState.premium, F.data.startswith("ap:"))
async def add_premium(cb: CallbackQuery, state: FSMContext):
    await state.update_data(premium=cb.data.endswith("1"))
    await state.set_state(AddState.poster)
    await show(cb, "🖼 Poster rasm havolasini yuboring:",
               kb([b("⏭ O'tkazib yuborish", "ps:skip")]))


@admin_r.callback_query(AddState.poster, F.data == "ps:skip")
async def add_poster_skip(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await finish_add(cb.message, state, "")


@admin_r.message(AddState.poster, F.photo | F.document)
async def add_poster_image(message: Message, state: FSMContext):
    url, err = image_url_from_message(message)
    if err:
        await message.answer(err)
        return
    await finish_add(message, state, url)


@admin_r.message(AddState.poster, TEXT)
async def add_poster(message: Message, state: FSMContext):
    poster = message.text.strip()
    if poster == "-":
        poster = ""
    await finish_add(message, state, poster)


async def finish_add(message, state, poster):
    data = await state.get_data()
    items = load_items()
    new_id = max([x.get("id", 0) for x in items], default=0) + 1
    item = {
        "id": new_id,
        "title": data["title"],
        "content_type": data["type"],
        "language": data.get("language", "uz"),
        "genre": data.get("genre", "TV"),
        "badge": BADGES[data["type"]],
        "url": data["url"],
        "poster": poster,
        "premium": bool(data.get("premium", False)),
        "views": 0,
        "added": utcnow().isoformat(),
    }
    if data["type"] == "football":
        item["match_time"] = data.get("match_time", "")
    items.append(item)
    save_items(items)
    await state.clear()
    await message.answer(
        f"✅ <b>Saqlandi!</b>\n{TYPES[data['type']]}: {escape(item['title'])} (ID {new_id})\n"
        f"{'💎 Premium' if item['premium'] else '🆓 Bepul'}\n\n"
        "ℹ️ Agar Mini App <code>data.json</code> ni GitHub'dan o'qisa, faylni yangilab yuklang. "
        "API orqali ishlasa — avtomatik yangilanadi.",
        reply_markup=kb([b("➕ Yana qo'shish", "adm:add")], [b("⬅️ Admin panel", "admin")]))


# ───────────────────────── ADMIN: BUGUNGI O'YIN (tez forma) ─────────────────────────
class MatchState(StatesGroup):
    step = State()
    premium = State()


# (maydon, so'rov matni, ixtiyoriymi)
MATCH_STEPS = [
    ("home", "🏠 <b>Uy jamoasi</b> nomini yozing (masalan: Real Madrid):", False),
    ("away", "✈️ <b>Mehmon jamoasi</b> nomini yozing (masalan: Barcelona):", False),
    ("home_logo", "🖼 Uy jamoasi <b>logotipi</b>: rasmni shu yerga yuboring yoki havola yozing:", True),
    ("away_logo", "🖼 Mehmon jamoasi <b>logotipi</b>: rasmni yuboring yoki havola yozing:", True),
    ("poster", "🏟 O'yin <b>fon rasmi</b>: rasmni yuboring yoki havola yozing:", True),
    ("league", "🏆 <b>Liga</b> nomi (masalan: La Liga):", False),
    ("match_time", "⏰ <b>O'yin vaqti</b> (Toshkent vaqti)\nMasalan: <code>21:00</code> (bugun) yoki <code>01.11 21:00</code>:", False),
    ("url", "🔗 <b>Translyatsiya havolasi</b> (m3u8 / mp4 / YouTube):", False),
]


async def ask_match_step(event, state, idx):
    await state.set_state(MatchState.step)
    await state.update_data(idx=idx)
    _, prompt, optional = MATCH_STEPS[idx]
    rows = []
    if optional:
        rows.append([b("⏭ O'tkazib yuborish", "mt:skip")])
    rows.append([b("❌ Bekor", "admin")])
    text = f"⚽ <b>Yangi o'yin</b> ({idx + 1}/{len(MATCH_STEPS)})\n\n{prompt}"
    if isinstance(event, CallbackQuery):
        await show(event, text, InlineKeyboardMarkup(inline_keyboard=rows))
    else:
        await event.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def match_next(event, state, idx):
    if idx + 1 < len(MATCH_STEPS):
        await ask_match_step(event, state, idx + 1)
        return
    await state.set_state(MatchState.premium)
    text = "💎 Bu o'yin faqat obunachilar uchunmi?"
    markup = kb([b("💎 Ha, premium", "mp:1"), b("🆓 Yo'q, bepul", "mp:0")])
    if isinstance(event, CallbackQuery):
        await show(event, text, markup)
    else:
        await event.answer(text, reply_markup=markup)


@admin_r.callback_query(F.data == "mt:new")
async def cb_match_new(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.update_data(m={})
    await ask_match_step(cb, state, 0)


@admin_r.callback_query(MatchState.step, F.data == "mt:skip")
async def cb_match_skip(cb: CallbackQuery, state: FSMContext):
    idx = (await state.get_data()).get("idx", 0)
    await match_next(cb, state, idx)


@admin_r.message(MatchState.step, TEXT)
async def msg_match_step(message: Message, state: FSMContext):
    data = await state.get_data()
    idx = data.get("idx", 0)
    field, _, optional = MATCH_STEPS[idx]
    val = message.text.strip()
    m = dict(data.get("m", {}))
    if val == "-" and optional:
        pass
    elif field in ("home_logo", "away_logo", "poster", "url"):
        if not val.lower().startswith(("http://", "https://")):
            await message.answer("❌ Havola http(s):// bilan boshlanishi kerak. Qayta yuboring:")
            return
        m[field] = val
    elif field == "match_time":
        iso = parse_match_time(val)
        if not iso:
            await message.answer("❌ Vaqt noto'g'ri. Masalan: <code>21:00</code> yoki <code>01.11 21:00</code>")
            return
        m[field] = iso
    else:
        if not val:
            return
        m[field] = val
    await state.update_data(m=m)
    await match_next(message, state, idx)


IMAGE_FIELDS = ("home_logo", "away_logo", "poster")
NO_PUBLIC_URL_MSG = ("❌ Rasm yuklash uchun serverning ochiq manzili kerak. <code>.env</code> / Render'da "
                     "<code>PUBLIC_URL=https://sizning-bot.onrender.com</code> ni qo'shing.\n"
                     "Hozircha rasm <b>havolasini</b> yuboring.")


def image_url_from_message(message):
    """Telegramga yuborilgan rasm (photo yoki rasm-fayl) uchun ochiq havola: PUBLIC_URL/img/<file_id>."""
    fid = None
    if message.photo:
        fid = message.photo[-1].file_id
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        fid = message.document.file_id
    if not fid:
        return None, "❌ Bu rasm emas. Rasm yuboring yoki havola yozing."
    if not PUBLIC_URL:
        return None, NO_PUBLIC_URL_MSG
    return f"{PUBLIC_URL}/img/{fid}", None


@admin_r.message(MatchState.step, F.photo | F.document)
async def msg_match_image(message: Message, state: FSMContext):
    data = await state.get_data()
    idx = data.get("idx", 0)
    field = MATCH_STEPS[idx][0]
    if field not in IMAGE_FIELDS:
        await message.answer("❌ Bu qadamda matn yuboring.")
        return
    url, err = image_url_from_message(message)
    if err:
        await message.answer(err)
        return
    m = dict(data.get("m", {}))
    m[field] = url
    await state.update_data(m=m)
    await match_next(message, state, idx)


@admin_r.callback_query(MatchState.premium, F.data.startswith("mp:"))
async def cb_match_save(cb: CallbackQuery, state: FSMContext):
    m = (await state.get_data()).get("m", {})
    if not m.get("home") or not m.get("away") or not m.get("url"):
        await cb.answer("Ma'lumot yetarli emas", show_alert=True)
        return
    items = load_items()
    new_id = max([x.get("id", 0) for x in items], default=0) + 1
    item = {
        "id": new_id, "title": f"{m['home']} vs {m['away']}", "content_type": "football",
        "language": "uz", "genre": m.get("league") or "Futbol", "badge": BADGES["football"],
        "url": m["url"], "poster": m.get("poster", ""), "premium": cb.data.endswith("1"),
        "views": 0, "added": utcnow().isoformat(),
        "home": m["home"], "away": m["away"], "home_logo": m.get("home_logo", ""),
        "away_logo": m.get("away_logo", ""), "league": m.get("league", ""),
        "match_time": m["match_time"], "score": "", "minute": "",
    }
    items.append(item)
    save_items(items)
    await state.clear()
    await show(cb, f"✅ <b>O'yin qo'shildi</b>\n⚽ {escape(item['title'])} (ID {new_id})\n"
                   f"⏰ {fmt_dt(parse_dt(item['match_time']))}\n{'💎 Premium' if item['premium'] else '🆓 Bepul'}",
               kb([b("📄 O'yin kartasi", f"itm:{new_id}")], [b("⚽ Yana o'yin", "mt:new")], [b("⬅️ Admin panel", "admin")]))


# ───────────────────────── ADMIN: KONTENT RO'YXATI / TAHRIR ─────────────────────────
def type_icon(t):
    return TYPES.get(t, "📺").split()[0]


def item_flags(i):
    return f"{'💎' if i.get('premium') else '🆓'}{'⭐' if i.get('featured') else ''}{'🙈' if i.get('hidden') else ''}"


async def render_list(cb, ftype, page):
    items = sorted(load_items(), key=lambda i: i.get("id", 0), reverse=True)
    if ftype:
        items = [i for i in items if i.get("content_type") == ftype]
    filt_row = [b("🌐 Hammasi", "lst:0")] + [b(type_icon(t), f"lt:{t}:0") for t in TYPES]
    if not items:
        await show(cb, "📭 Kontent yo'q.", kb(filt_row, [b("➕ Qo'shish", "adm:add")], [b("⬅️ Admin panel", "admin")]))
        return
    chunk, page, pages = paginate(items, page)
    rows = [[b(f"{item_flags(i)} {type_icon(i.get('content_type'))} {i['title']}"[:60], f"itm:{i['id']}")] for i in chunk]
    rows.append(nav_row(f"lt:{ftype}" if ftype else "lst", page, pages))
    rows.append(filt_row)
    rows.append([b("⬅️ Admin panel", "admin")])
    title = TYPES[ftype] if ftype else "Hammasi"
    await show(cb, f"📋 <b>Kontent ro'yxati</b> — {title} ({len(items)} ta)\n💎 premium · ⭐ tanlangan · 🙈 yashirin",
               InlineKeyboardMarkup(inline_keyboard=rows))


@admin_r.callback_query(F.data.startswith("lst:"))
async def cb_list(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await render_list(cb, None, int(cb.data.split(":")[1]))


@admin_r.callback_query(F.data.startswith("lt:"))
async def cb_list_type(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    _, t, p = cb.data.split(":")
    await render_list(cb, t if t in TYPES else None, int(p))


def find_item(item_id):
    for i in load_items():
        if i.get("id") == item_id:
            return i
    return None


async def show_item(cb, item_id):
    it = find_item(item_id)
    if not it:
        await cb.answer("Topilmadi", show_alert=True)
        return
    extra = ""
    if it.get("content_type") == "football":
        mt = it.get("match_time", "")
        try:
            mt = fmt_dt(parse_dt(mt)) if "T" in mt else mt
        except ValueError:
            pass
        extra = (f"⚽ {escape(str(it.get('home') or '—'))} — {escape(str(it.get('away') or '—'))}\n"
                 f"🏆 Liga: {escape(str(it.get('league') or '—'))}\n⏰ O'yin vaqti: {escape(str(mt or '-'))}\n"
                 f"📊 Hisob: {escape(str(it.get('score') or '—'))} · daqiqa: {escape(str(it.get('minute') or '—'))}\n")
    elif it.get("content_type") == "tv":
        extra = f"📡 Holat: {'🔴 MUAMMO' if it.get('status') == 'down' else '🟢 ishlayapti'}\n"
    added = fmt_dt(parse_dt(it["added"])) if it.get("added") else "—"
    text = (
        f"{TYPES.get(it.get('content_type'), '')}\n<b>{escape(it['title'])}</b> (ID {it['id']})\n\n"
        f"🌐 Til: {it.get('language', '-')}\n🎭 Janr: {escape(str(it.get('genre', '-')))}\n{extra}"
        f"💎 {'Premium' if it.get('premium') else 'Bepul'}"
        f"{' · ⭐ Tanlangan' if it.get('featured') else ''}{' · 🙈 Yashirin' if it.get('hidden') else ''}\n"
        f"👁 Ko'rishlar: {it.get('views', 0)}\n🕒 Qo'shilgan: {added}\n"
        f"🖼 Poster: {'bor' if it.get('poster') else 'yo`q'}\n"
        f"🔗 <code>{escape(it.get('url', ''))}</code>"
    )
    rows = [
        [b("✏️ Nomi", f"ed:{item_id}:title"), b("🔗 Havola", f"ed:{item_id}:url")],
        [b("🖼 Poster", f"ed:{item_id}:poster"), b("🎭 Janr", f"ed:{item_id}:genre")],
        [b("🌐 Til", f"ed:{item_id}:language")],
    ]
    if it.get("content_type") == "football":
        rows[-1].append(b("⏰ Vaqt", f"ed:{item_id}:match_time"))
        rows += [
            [b("🏠 Uy jamoa", f"ed:{item_id}:home"), b("✈️ Mehmon", f"ed:{item_id}:away"), b("🏆 Liga", f"ed:{item_id}:league")],
            [b("🖼 Uy logo", f"ed:{item_id}:home_logo"), b("🖼 Mehmon logo", f"ed:{item_id}:away_logo")],
            [b("📊 Hisob", f"ed:{item_id}:score"), b("⏱ Daqiqa", f"ed:{item_id}:minute")],
        ]
    if it.get("content_type") == "tv":
        rows.append([b("🟢 Ishlayapti" if it.get("status") == "down" else "🔴 Muammo deb belgilash", f"st:{item_id}")])
    rows += [
        [b("💎/🆓 Premium", f"pm:{item_id}"), b("⭐ Tanlangan", f"ft:{item_id}")],
        [b("👁 Ko'rsatish" if it.get("hidden") else "🙈 Yashirish", f"hd:{item_id}"), b("📑 Nusxa", f"dp:{item_id}")],
        [b("♻️ Ko'rishlarni nolga", f"vr:{item_id}"), b("🗑 O'chirish", f"dl:{item_id}")],
        [b("⬅️ Ro'yxat", "lst:0")],
    ]
    await show(cb, text, InlineKeyboardMarkup(inline_keyboard=rows))


@admin_r.callback_query(F.data.startswith("itm:"))
async def cb_item(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_item(cb, int(cb.data.split(":")[1]))


def _toggle_field(item_id, field):
    items = load_items()
    for i in items:
        if i.get("id") == item_id:
            i[field] = not i.get(field, False)
    save_items(items)


@admin_r.callback_query(F.data.startswith("pm:"))
async def cb_premium_toggle(cb: CallbackQuery):
    item_id = int(cb.data.split(":")[1])
    _toggle_field(item_id, "premium")
    await show_item(cb, item_id)


@admin_r.callback_query(F.data.startswith("st:"))
async def cb_status_toggle(cb: CallbackQuery):
    item_id = int(cb.data.split(":")[1])
    items = load_items()
    for i in items:
        if i.get("id") == item_id:
            i["status"] = "ok" if i.get("status") == "down" else "down"
    save_items(items)
    await show_item(cb, item_id)


@admin_r.callback_query(F.data.startswith("ft:"))
async def cb_featured_toggle(cb: CallbackQuery):
    item_id = int(cb.data.split(":")[1])
    _toggle_field(item_id, "featured")
    await show_item(cb, item_id)


@admin_r.callback_query(F.data.startswith("hd:"))
async def cb_hidden_toggle(cb: CallbackQuery):
    item_id = int(cb.data.split(":")[1])
    _toggle_field(item_id, "hidden")
    await show_item(cb, item_id)


@admin_r.callback_query(F.data.startswith("vr:"))
async def cb_views_reset(cb: CallbackQuery):
    item_id = int(cb.data.split(":")[1])
    items = load_items()
    for i in items:
        if i.get("id") == item_id:
            i["views"] = 0
    save_items(items)
    await show_item(cb, item_id)


@admin_r.callback_query(F.data.startswith("dp:"))
async def cb_duplicate(cb: CallbackQuery):
    item_id = int(cb.data.split(":")[1])
    items = load_items()
    src = next((i for i in items if i.get("id") == item_id), None)
    if not src:
        await cb.answer("Topilmadi", show_alert=True)
        return
    new = dict(src)
    new["id"] = max(i.get("id", 0) for i in items) + 1
    new["title"] = f"{src['title']} (nusxa)"
    new["views"] = 0
    new["added"] = utcnow().isoformat()
    items.append(new)
    save_items(items)
    await cb.answer("📑 Nusxa yaratildi")
    await show_item(cb, new["id"])


@admin_r.callback_query(F.data.startswith("ed:"))
async def cb_edit(cb: CallbackQuery, state: FSMContext):
    _, item_id, field = cb.data.split(":")
    hint = {
        "language": " (uz / ru / en ...)",
        "poster": " (o'chirish: <code>-</code>)",
        "home_logo": " (o'chirish: <code>-</code>)", "away_logo": " (o'chirish: <code>-</code>)",
        "match_time": " (masalan: <code>21:00</code> yoki <code>01.11 21:00</code>)",
        "score": " (masalan: <code>1:0</code>, tozalash: <code>-</code>)",
        "minute": " (masalan: <code>34</code>, tozalash: <code>-</code>)",
    }.get(field, "")
    await state.set_state(EditState.value)
    await state.update_data(item_id=int(item_id), field=field)
    await show(cb, f"✏️ Yangi qiymatni yuboring (<b>{field}</b>){hint} yoki /cancel:")


@admin_r.message(EditState.value, F.photo | F.document)
async def msg_edit_image(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("field") not in IMAGE_FIELDS:
        await message.answer("❌ Bu maydon uchun matn yuboring.")
        return
    url, err = image_url_from_message(message)
    if err:
        await message.answer(err)
        return
    items = load_items()
    for i in items:
        if i.get("id") == data["item_id"]:
            i[data["field"]] = url
    save_items(items)
    await state.clear()
    await message.answer("✅ Rasm yangilandi.", reply_markup=kb(
        [b("📄 Kontentga qaytish", f"itm:{data['item_id']}")], [b("⬅️ Admin panel", "admin")]))


@admin_r.message(EditState.value, TEXT)
async def msg_edit(message: Message, state: FSMContext):
    data = await state.get_data()
    field = data["field"]
    value = message.text.strip()
    if field in ("url", "poster", "home_logo", "away_logo") and value != "-" and not value.lower().startswith(("http://", "https://")):
        await message.answer("❌ Havola http(s):// bilan boshlanishi kerak. Qayta yuboring:")
        return
    if field == "match_time":
        iso = parse_match_time(value)
        if not iso:
            await message.answer("❌ Vaqt noto'g'ri. Masalan: <code>21:00</code> yoki <code>01.11 21:00</code>")
            return
        value = iso
        items = load_items()
        for i in items:
            if i.get("id") == data["item_id"]:
                i.pop("reminded", None)
        save_items(items)
    if field == "url" and value == "-":
        await message.answer("❌ Havolani o'chirib bo'lmaydi. Qayta yuboring:")
        return
    if field == "language":
        value = value.lower()
        if not re.fullmatch(r"[a-z]{2,3}", value):
            await message.answer("❌ Til kodi 2-3 harf bo'lishi kerak (uz, ru, en). Qayta yuboring:")
            return
    if field == "title" and not value:
        return
    if value == "-" and field in ("poster", "home_logo", "away_logo", "score", "minute"):
        value = ""
    items = load_items()
    for i in items:
        if i.get("id") == data["item_id"]:
            i[field] = value
    save_items(items)
    await state.clear()
    await message.answer("✅ Yangilandi.", reply_markup=kb(
        [b("📄 Kontentga qaytish", f"itm:{data['item_id']}")], [b("📋 Ro'yxat", "lst:0")], [b("⬅️ Admin panel", "admin")]))


# ───────────────────────── ADMIN: KONTENT QIDIRISH ─────────────────────────
@admin_r.callback_query(F.data == "srch:start")
async def cb_search_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SearchState.query)
    await show(cb, "🔎 Kontent nomidan (yoki janr/havoladan) qidirish uchun so'z yuboring (bekor: /cancel):")


@admin_r.message(SearchState.query, TEXT)
async def msg_search(message: Message, state: FSMContext):
    q = message.text.strip().lower()
    res = [i for i in load_items()
           if q in i.get("title", "").lower() or q in str(i.get("genre", "")).lower() or q in i.get("url", "").lower()]
    await state.clear()
    if not res:
        await message.answer("😕 Hech narsa topilmadi.", reply_markup=kb([b("🔎 Qayta qidirish", "srch:start")], [b("⬅️ Admin panel", "admin")]))
        return
    rows = [[b(f"{item_flags(i)} {type_icon(i.get('content_type'))} {i['title']}"[:60], f"itm:{i['id']}")] for i in res[:25]]
    rows += [[b("🔎 Qayta qidirish", "srch:start")], [b("⬅️ Admin panel", "admin")]]
    more = f"\n(dastlabki 25 tasi ko'rsatildi)" if len(res) > 25 else ""
    await message.answer(f"🔎 Topildi: <b>{len(res)}</b> ta{more}", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@admin_r.callback_query(F.data.startswith("dl:"))
async def cb_delete_ask(cb: CallbackQuery):
    item_id = cb.data.split(":")[1]
    await show(cb, "⚠️ Rostdan ham o'chirasizmi?", kb(
        [b("✅ Ha, o'chirish", f"dy:{item_id}"), b("❌ Yo'q", f"itm:{item_id}")]))


@admin_r.callback_query(F.data.startswith("dy:"))
async def cb_delete_yes(cb: CallbackQuery):
    item_id = int(cb.data.split(":")[1])
    save_items([i for i in load_items() if i.get("id") != item_id])
    await show(cb, "🗑 O'chirildi.", kb([b("📋 Ro'yxat", "lst:0")], [b("⬅️ Admin panel", "admin")]))


# ───────────────────────── ADMIN: OBUNALAR ─────────────────────────
@admin_r.callback_query(F.data == "sub:menu")
async def cb_sub_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    s = load_settings()
    now = utcnow()
    subs = load_subs()["subs"]
    active = sum(1 for x in subs.values() if parse_dt(x["expires"]) > now)
    soon = sum(1 for x in subs.values() if now < parse_dt(x["expires"]) <= now + timedelta(days=7))
    await show(cb, f"💎 <b>Obunalar boshqaruvi</b>\nPullik rejim: {'✅ yoqilgan' if s['paywall'] else '❌ o`chirilgan'}\n"
                   f"Faol: <b>{active}</b> · 7 kunda tugaydi: <b>{soon}</b>",
               kb([b("➕ Yangi obuna linki", "nl:start")],
                  [b("🔗 Linklar ro'yxati", "sub:links:0"), b("👥 Obunachilar", "sub:list:0")],
                  [b("⏳ Tez tugaydiganlar", "sub:soon:0"), b("🎁 Hammaga bonus kun", "sub:bonus")],
                  [b("🎁 Obuna berish / olib tashlash", "usr:find")],
                  [b("⬅️ Admin panel", "admin")]))


def months_kb(prefix):
    return kb(
        [b("1 oy", f"{prefix}:1"), b("2 oy", f"{prefix}:2"), b("3 oy", f"{prefix}:3")],
        [b("6 oy", f"{prefix}:6"), b("12 oy", f"{prefix}:12")],
        [b("✍️ Boshqa son", f"{prefix}c")],
        [b("⬅️ Orqaga", "sub:menu")])


@admin_r.callback_query(F.data == "nl:start")
async def cb_nl_start(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, "📅 <b>Necha oylik obuna?</b>", months_kb("nl:m"))


@admin_r.callback_query(F.data == "nl:mc")
async def cb_nl_months_custom(cb: CallbackQuery, state: FSMContext):
    await state.set_state(LinkState.months)
    await show(cb, "✍️ Oylar sonini yozing (1–60):")


async def ask_people(event, state, months):
    await state.update_data(months=months)
    rows = kb(
        [b("1 kishi", "nl:p:1"), b("5 kishi", "nl:p:5"), b("10 kishi", "nl:p:10")],
        [b("25 kishi", "nl:p:25"), b("50 kishi", "nl:p:50"), b("100 kishi", "nl:p:100")],
        [b("✍️ Boshqa son", "nl:pc")], [b("⬅️ Orqaga", "sub:menu")])
    await show(event, f"👥 <b>{months} oylik</b> link.\nNecha kishi foydalana oladi?", rows)


@admin_r.callback_query(F.data.startswith("nl:m:"))
async def cb_nl_months(cb: CallbackQuery, state: FSMContext):
    await ask_people(cb, state, int(cb.data.split(":")[2]))


@admin_r.message(LinkState.months, TEXT)
async def msg_nl_months(message: Message, state: FSMContext):
    if not message.text.strip().isdigit() or not 1 <= int(message.text) <= 60:
        await message.answer("❌ 1 dan 60 gacha son yozing:")
        return
    await state.set_state(None)
    await ask_people(message, state, int(message.text))


@admin_r.callback_query(F.data == "nl:pc")
async def cb_nl_people_custom(cb: CallbackQuery, state: FSMContext):
    await state.set_state(LinkState.people)
    await show(cb, "✍️ Kishilar sonini yozing (1–100000):")


async def create_link_and_show(event, state, admin_id, people):
    data = await state.get_data()
    months = data.get("months")
    if not months:
        await show(event, "⚠️ Ma'lumot topilmadi. Qaytadan boshlang.", kb([b("➕ Yangi link", "nl:start")]))
        return
    await state.clear()
    code = create_link(months, people, admin_id)
    await show(event,
               f"✅ <b>Link yaratildi!</b>\n\n📅 Muddat: <b>{months} oy</b>\n👥 Limit: <b>{people} kishi</b>\n\n"
               f"🔗 <code>{make_link_url(code)}</code>\n🎟 Kod: <code>{code}</code>\n\n"
               "Linkni bosganda foydalanuvchiga obuna avtomatik beriladi.",
               kb([b("➕ Yana link", "nl:start")], [b("🔗 Linklar", "sub:links:0")], [b("⬅️ Admin panel", "admin")]))


@admin_r.callback_query(F.data.startswith("nl:p:"))
async def cb_nl_people(cb: CallbackQuery, state: FSMContext):
    await create_link_and_show(cb, state, cb.from_user.id, int(cb.data.split(":")[2]))


@admin_r.message(LinkState.people, TEXT)
async def msg_nl_people(message: Message, state: FSMContext):
    if not message.text.strip().isdigit() or not 1 <= int(message.text) <= 100000:
        await message.answer("❌ 1 dan 100000 gacha son yozing:")
        return
    await create_link_and_show(message, state, message.from_user.id, int(message.text))


@admin_r.callback_query(F.data.startswith("sub:links:"))
async def cb_links(cb: CallbackQuery):
    db = load_subs()
    codes = sorted(db["links"], key=lambda c: db["links"][c]["created"], reverse=True)
    if not codes:
        await show(cb, "📭 Linklar yo'q.", kb([b("➕ Yangi link", "nl:start")], [b("⬅️ Orqaga", "sub:menu")]))
        return
    chunk, page, pages = paginate(codes, int(cb.data.split(":")[2]))
    rows = []
    for c in chunk:
        l = db["links"][c]
        mark = "🟢" if l.get("active", True) and len(l["used_by"]) < l["max_uses"] else "🔴"
        rows.append([b(f"{mark} {l['months']} oy • {len(l['used_by'])}/{l['max_uses']} • {c}", f"lk:{c}")])
    rows.append(nav_row("sub:links", page, pages))
    rows.append([b("⬅️ Orqaga", "sub:menu")])
    await show(cb, f"🔗 <b>Obuna linklari</b> ({len(codes)} ta)", InlineKeyboardMarkup(inline_keyboard=rows))


async def show_link(cb, code):
    l = load_subs()["links"].get(code)
    if not l:
        await cb.answer("Link topilmadi", show_alert=True)
        return
    users = "\n".join(f"• {user_label(u)}" for u in l["used_by"][:20]) or "—"
    more = f"\n… va yana {len(l['used_by']) - 20} ta" if len(l["used_by"]) > 20 else ""
    text = (
        f"🔗 <code>{make_link_url(code)}</code>\n\n📅 {l['months']} oy\n"
        f"👥 Ishlatilgan: {len(l['used_by'])}/{l['max_uses']}\n"
        f"📌 Holat: {'🟢 faol' if l.get('active', True) else '🔴 o`chirilgan'}\n"
        f"🕒 Yaratilgan: {fmt_dt(parse_dt(l['created']))}\n\n<b>Foydalanuvchilar:</b>\n{users}{more}")
    await show(cb, text, kb(
        [b("⏸/▶️ Faol/Nofaol", f"lkt:{code}"), b("🗑 O'chirish", f"lkd:{code}")],
        [b("⬅️ Linklar", "sub:links:0")]))


@admin_r.callback_query(F.data.startswith("lk:"))
async def cb_link(cb: CallbackQuery):
    await show_link(cb, cb.data.split(":", 1)[1])


@admin_r.callback_query(F.data.startswith("lkt:"))
async def cb_link_toggle(cb: CallbackQuery):
    code = cb.data.split(":", 1)[1]
    db = load_subs()
    if code in db["links"]:
        db["links"][code]["active"] = not db["links"][code].get("active", True)
        save_subs(db)
    await show_link(cb, code)


@admin_r.callback_query(F.data.startswith("lkd:"))
async def cb_link_delete(cb: CallbackQuery):
    code = cb.data.split(":", 1)[1]
    db = load_subs()
    db["links"].pop(code, None)
    save_subs(db)
    await show(cb, "🗑 Link o'chirildi. (Allaqachon berilgan obunalar saqlanadi.)", kb([b("⬅️ Linklar", "sub:links:0")]))


@admin_r.callback_query(F.data.startswith("sub:list:"))
async def cb_sub_list(cb: CallbackQuery):
    db = load_subs()
    now = utcnow()
    act = sorted(((u, parse_dt(s["expires"])) for u, s in db["subs"].items() if parse_dt(s["expires"]) > now),
                 key=lambda x: x[1])
    if not act:
        await show(cb, "📭 Faol obunachilar yo'q.", kb([b("⬅️ Orqaga", "sub:menu")]))
        return
    chunk, page, pages = paginate(act, int(cb.data.split(":")[2]), per=10)
    lines = [f"• {user_label(u)} — {fmt_dt(e)}" for u, e in chunk]
    rows = [nav_row("sub:list", page, pages), [b("⬅️ Orqaga", "sub:menu")]]
    await show(cb, f"👥 <b>Faol obunachilar</b> ({len(act)} ta)\n\n" + "\n".join(lines),
               InlineKeyboardMarkup(inline_keyboard=rows))


@admin_r.callback_query(F.data.startswith("sub:soon:"))
async def cb_sub_soon(cb: CallbackQuery):
    db = load_subs()
    now = utcnow()
    soon = sorted(((u, parse_dt(s["expires"])) for u, s in db["subs"].items()
                   if now < parse_dt(s["expires"]) <= now + timedelta(days=7)), key=lambda x: x[1])
    if not soon:
        await show(cb, "✅ 7 kun ichida tugaydigan obunalar yo'q.", kb([b("⬅️ Orqaga", "sub:menu")]))
        return
    chunk, page, pages = paginate(soon, int(cb.data.split(":")[2]), per=8)
    rows = []
    for u, e in chunk:
        rec = load_users().get(str(u), {})
        rows.append([b(f"{(rec.get('name') or u)[:22]} — {fmt_dt(e)}"[:60], f"ucard:{u}")])
    rows.append(nav_row("sub:soon", page, pages))
    rows.append([b("📨 Hammasiga eslatma yuborish", "sub:soonmsg")])
    rows.append([b("⬅️ Orqaga", "sub:menu")])
    await show(cb, f"⏳ <b>7 kun ichida tugaydiganlar</b> ({len(soon)} ta)", InlineKeyboardMarkup(inline_keyboard=rows))


@admin_r.callback_query(F.data == "sub:soonmsg")
async def cb_sub_soonmsg(cb: CallbackQuery):
    now = utcnow()
    ids = [int(u) for u, s in load_subs()["subs"].items() if now < parse_dt(s["expires"]) <= now + timedelta(days=7)]
    await show(cb, f"📨 {len(ids)} ta foydalanuvchiga eslatma yuborilmoqda…", kb([b("⬅️ Obunalar", "sub:menu")]))

    async def _go():
        for uid in ids:
            await safe_send(uid, "⏰ Obunangiz tez orada tugaydi. Uzaytirish uchun admin bilan bog'laning.")
            await asyncio.sleep(0.05)
    t = asyncio.create_task(_go())
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


@admin_r.callback_query(F.data == "sub:bonus")
async def cb_sub_bonus(cb: CallbackQuery, state: FSMContext):
    await state.set_state(BonusState.days)
    await show(cb, "🎁 <b>Barcha faol obunachilarga</b> necha kun bonus qo'shamiz? (1–365)\nBekor: /cancel")


@admin_r.message(BonusState.days, TEXT)
async def msg_sub_bonus(message: Message, state: FSMContext):
    if not message.text.strip().isdigit() or not 1 <= int(message.text) <= 365:
        await message.answer("❌ 1 dan 365 gacha son yozing:")
        return
    days = int(message.text)
    db = load_subs()
    now = utcnow()
    ids = []
    for uid, s in db["subs"].items():
        exp = parse_dt(s["expires"])
        if exp > now:
            s["expires"] = (exp + timedelta(days=days)).isoformat()
            s["warned"] = False
            s.setdefault("history", []).append({"at": now.isoformat(), "months": 0, "days": days, "source": "bonus"})
            ids.append(int(uid))
    save_subs(db)
    await state.clear()
    await message.answer(f"✅ {len(ids)} ta obunachiga <b>+{days} kun</b> qo'shildi.", reply_markup=kb([b("⬅️ Obunalar", "sub:menu")]))

    async def _go():
        for uid in ids:
            await safe_send(uid, f"🎁 Sizga <b>+{days} kun</b> bonus berildi! Rahmat, biz bilan qolganingiz uchun.")
            await asyncio.sleep(0.05)
    t = asyncio.create_task(_go())
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


# ───────────────────────── ADMIN: FOYDALANUVCHILAR ─────────────────────────
@admin_r.callback_query(F.data == "usr:menu")
async def cb_user_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    users = load_users()
    await show(cb, f"👤 <b>Foydalanuvchilar</b> — jami {len(users)} ta", kb(
        [b("🔎 Qidirish (ID/@username)", "usr:find")],
        [b("📃 Hammasi", "usr:list:all:0"), b("🆕 Oxirgi 7 kun", "usr:list:new:0")],
        [b("🚫 Banlanganlar", "usr:list:banned:0"), b("🔕 Bloklaganlar", "usr:list:blocked:0")],
        [b("🆓 Obunasizlar", "usr:list:free:0"), b("💎 Obunachilar", "sub:list:0")],
        [b("📤 CSV eksport", "bk:csv_users")],
        [b("⬅️ Admin panel", "admin")]))


@admin_r.callback_query(F.data.startswith("usr:list:"))
async def cb_user_list(cb: CallbackQuery):
    _, _, flt, page = cb.data.split(":")
    now = utcnow()
    week = (now - timedelta(days=7)).isoformat()
    rows_src = []
    for uid, u in load_users().items():
        if flt == "banned" and not u.get("banned"):
            continue
        if flt == "blocked" and not u.get("blocked"):
            continue
        if flt == "new" and u.get("joined", "") < week:
            continue
        if flt == "free" and is_subscribed(int(uid)):
            continue
        rows_src.append((uid, u))
    rows_src.sort(key=lambda x: x[1].get("joined", ""), reverse=True)
    labels = {"all": "Hammasi", "new": "Oxirgi 7 kun", "banned": "Banlanganlar", "blocked": "Bloklaganlar", "free": "Obunasizlar"}
    if not rows_src:
        await show(cb, f"📭 {labels.get(flt, '')}: hech kim yo'q.", kb([b("⬅️ Orqaga", "usr:menu")]))
        return
    chunk, page, pages = paginate(rows_src, int(page), per=10)
    rows = []
    for uid, u in chunk:
        un = f" @{u['username']}" if u.get("username") else ""
        rows.append([b(f"{'🚫' if u.get('banned') else '👤'} {(u.get('name') or uid)[:24]}{un}"[:60], f"ucard:{uid}")])
    rows.append(nav_row(f"usr:list:{flt}", page, pages))
    rows.append([b("⬅️ Orqaga", "usr:menu")])
    await show(cb, f"📃 <b>{labels.get(flt, '')}</b> ({len(rows_src)} ta)", InlineKeyboardMarkup(inline_keyboard=rows))


@admin_r.callback_query(F.data == "usr:find")
async def cb_user_find(cb: CallbackQuery, state: FSMContext):
    await state.set_state(UserFindState.query)
    await show(cb, "🔎 Foydalanuvchi <b>ID</b> yoki <b>@username</b> yuboring (bekor: /cancel):")


def find_user(q):
    users = load_users()
    q = (q or "").strip().lstrip("@")
    if not q:
        return None
    if q.isdigit() and q in users:
        return int(q)
    for uid, u in users.items():
        if u.get("username", "").lower() == q.lower():
            return int(uid)
    return None


def user_card(uid):
    users = load_users()
    u = users.get(str(uid), {})
    exp = sub_expiry(uid)
    if is_admin(uid):
        st = "👑 admin (cheksiz)"
    elif exp and exp > utcnow():
        st = f"✅ faol, {fmt_dt(exp)} gacha"
    elif exp:
        st = f"⛔️ tugagan ({fmt_dt(exp)})"
    else:
        st = "➖ yo'q"
    refs = sum(1 for x in users.values() if x.get("ref") == uid)
    hist = load_subs()["subs"].get(str(uid), {}).get("history", [])
    last = hist[-1] if hist else None
    text = (f"👤 {user_label(uid)}\n📅 Qo'shilgan: {fmt_dt(parse_dt(u['joined'])) if u.get('joined') else '—'}\n"
            f"💎 Obuna: {st}\n🚫 Ban: {'ha' if u.get('banned') else 'yo`q'}"
            f"{' · 🔕 botni bloklagan' if u.get('blocked') else ''}\n"
            f"👥 Taklif qilgan: {refs}" + (f" · taklif qilgan: <code>{u['ref']}</code>" if u.get("ref") else "") +
            (f"\n🧾 Oxirgi: {last.get('months', 0)} oy {last.get('days', 0)} kun ({escape(str(last.get('source', '')))})" if last else ""))
    markup = kb(
        [b("+7 kun", f"ugd:{uid}:7"), b("+1 oy", f"uga:{uid}:1"), b("+3 oy", f"uga:{uid}:3")],
        [b("+6 oy", f"uga:{uid}:6"), b("+12 oy", f"uga:{uid}:12"), b("✍️ Boshqa (kun)", f"ugc:{uid}")],
        [b("✉️ Xabar yuborish", f"umsg:{uid}"), b("❌ Obunani olib tashlash", f"urv:{uid}")],
        [b("✅ Unban" if u.get("banned") else "🚫 Ban", f"ubn:{uid}")],
        [b("⬅️ Foydalanuvchilar", "usr:menu"), b("🛠 Admin panel", "admin")])
    return text, markup


@admin_r.message(UserFindState.query, TEXT)
async def msg_user_find(message: Message, state: FSMContext):
    uid = find_user(message.text)
    if uid is None:
        await message.answer("❌ Foydalanuvchi topilmadi (u botga /start bosgan bo'lishi kerak). Qayta yuboring yoki /cancel:")
        return
    await state.clear()
    text, markup = user_card(uid)
    await message.answer(text, reply_markup=markup)


@admin_r.callback_query(F.data.startswith("ucard:"))
async def cb_user_card(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = user_card(int(cb.data.split(":")[1]))
    await show(cb, text, markup)


@admin_r.callback_query(F.data.startswith("uga:"))
async def cb_user_grant(cb: CallbackQuery):
    _, uid, months = cb.data.split(":")
    exp = grant(int(uid), int(months), f"admin:{cb.from_user.id}")
    await safe_send(int(uid), f"🎁 Sizga <b>{months} oylik</b> obuna berildi!\n⏳ Amal qilish muddati: <b>{fmt_dt(exp)}</b>")
    text, markup = user_card(int(uid))
    await show(cb, text, markup)


@admin_r.callback_query(F.data.startswith("ugd:"))
async def cb_user_grant_days(cb: CallbackQuery):
    _, uid, days = cb.data.split(":")
    exp = grant_days(int(uid), int(days), f"admin:{cb.from_user.id}")
    await safe_send(int(uid), f"🎁 Sizga <b>{days} kunlik</b> obuna berildi!\n⏳ Amal qilish muddati: <b>{fmt_dt(exp)}</b>")
    text, markup = user_card(int(uid))
    await show(cb, text, markup)


@admin_r.callback_query(F.data.startswith("ugc:"))
async def cb_user_grant_custom(cb: CallbackQuery, state: FSMContext):
    await state.set_state(UserGrantState.days)
    await state.update_data(uid=int(cb.data.split(":")[1]))
    await show(cb, "✍️ Necha kun obuna beramiz? (1–3650) Bekor: /cancel")


@admin_r.message(UserGrantState.days, TEXT)
async def msg_user_grant_custom(message: Message, state: FSMContext):
    if not message.text.strip().isdigit() or not 1 <= int(message.text) <= 3650:
        await message.answer("❌ 1 dan 3650 gacha son yozing:")
        return
    uid = (await state.get_data())["uid"]
    days = int(message.text)
    exp = grant_days(uid, days, f"admin:{message.from_user.id}")
    await state.clear()
    await safe_send(uid, f"🎁 Sizga <b>{days} kunlik</b> obuna berildi!\n⏳ Amal qilish muddati: <b>{fmt_dt(exp)}</b>")
    text, markup = user_card(uid)
    await message.answer(text, reply_markup=markup)


@admin_r.callback_query(F.data.startswith("umsg:"))
async def cb_user_msg(cb: CallbackQuery, state: FSMContext):
    await state.set_state(UserMsgState.content)
    await state.update_data(uid=int(cb.data.split(":")[1]))
    await show(cb, "✉️ Foydalanuvchiga yuboriladigan xabarni jo'nating (matn/rasm/video — istalgan). Bekor: /cancel")


@admin_r.message(UserMsgState.content)
async def msg_user_msg(message: Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        return
    uid = (await state.get_data())["uid"]
    await state.clear()
    try:
        await bot.copy_message(uid, message.chat.id, message.message_id)
        res = "✅ Yuborildi."
    except TelegramForbiddenError:
        res = "❌ Foydalanuvchi botni bloklagan."
    except Exception as e:
        res = f"❌ Xatolik: {escape(str(e))}"
    await message.answer(res, reply_markup=kb([b("👤 Kartaga qaytish", f"ucard:{uid}")], [b("⬅️ Admin panel", "admin")]))


@admin_r.callback_query(F.data.startswith("urv:"))
async def cb_user_revoke(cb: CallbackQuery):
    uid = int(cb.data.split(":")[1])
    revoke(uid)
    await safe_send(uid, "ℹ️ Obunangiz admin tomonidan bekor qilindi.")
    text, markup = user_card(uid)
    await show(cb, text, markup)


@admin_r.callback_query(F.data.startswith("ubn:"))
async def cb_user_ban(cb: CallbackQuery):
    uid = int(cb.data.split(":")[1])
    if is_admin(uid):
        await cb.answer("Adminni banlab bo'lmaydi", show_alert=True)
        return
    users = load_users()
    if str(uid) in users:
        users[str(uid)]["banned"] = not users[str(uid)].get("banned", False)
        save_json(USERS_FILE, users)
    text, markup = user_card(uid)
    await show(cb, text, markup)


# ───────────────────────── ADMIN: XABAR YUBORISH ─────────────────────────
_broadcast_running = False


@admin_r.callback_query(F.data == "bc:menu")
async def cb_bc_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, "📢 <b>Kimlarga xabar yuboramiz?</b>", kb(
        [b(f"👥 Hammaga ({len(audience_ids('all'))})", "bc:all")],
        [b(f"💎 Obunachilarga ({len(audience_ids('subs'))})", "bc:subs"),
         b(f"🆓 Obunasizlarga ({len(audience_ids('free'))})", "bc:free")],
        [b(f"⏳ Tez tugaydiganlarga ({len(audience_ids('soon'))})", "bc:soon")],
        [b("⬅️ Admin panel", "admin")]))


def audience_ids(kind):
    ids = []
    now = utcnow()
    subs = load_subs()["subs"]
    for uid, u in load_users().items():
        if u.get("banned") or u.get("blocked"):
            continue
        sub = is_subscribed(int(uid))
        if kind == "subs" and not sub:
            continue
        if kind == "free" and sub:
            continue
        if kind == "soon":
            s = subs.get(uid)
            if not s or not (now < parse_dt(s["expires"]) <= now + timedelta(days=3)):
                continue
        ids.append(int(uid))
    return ids


def bc_markup(data):
    if data.get("btn_text") and data.get("btn_url"):
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=data["btn_text"], url=data["btn_url"])]])
    return None


def bc_confirm_kb(data):
    btn = f"🔘 Tugma: {data['btn_text']}" if data.get("btn_text") else "🔘 Tugma qo'shish"
    return kb([b("✅ Yuborish", "bcgo"), b("🧪 Test (o'zimga)", "bctest")],
              [b(btn[:40], "bcbtn")],
              [b("❌ Bekor", "admin")])


@admin_r.callback_query(F.data.in_({"bc:all", "bc:subs", "bc:free", "bc:soon"}))
async def cb_bc_audience(cb: CallbackQuery, state: FSMContext):
    kind = cb.data.split(":")[1]
    await state.set_state(BroadcastState.content)
    await state.update_data(kind=kind)
    await show(cb, f"✉️ Yuboriladigan xabarni jo'nating (matn, rasm, video, fayl — istalgan).\n"
                   f"Qabul qiluvchilar: <b>{len(audience_ids(kind))}</b> ta. Bekor: /cancel")


@admin_r.message(BroadcastState.content)
async def msg_bc_content(message: Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        return
    await state.update_data(chat_id=message.chat.id, message_id=message.message_id)
    data = await state.get_data()
    await message.answer(f"❓ Xabar <b>{len(audience_ids(data['kind']))}</b> ta foydalanuvchiga yuboriladi.\n"
                         "Avval <b>Test</b> bilan o'zingizga yuborib ko'rishingiz mumkin.",
                         reply_markup=bc_confirm_kb(data))


@admin_r.callback_query(F.data == "bcbtn")
async def cb_bc_btn(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if "message_id" not in data:
        await cb.answer("Avval xabarni yuboring", show_alert=True)
        return
    await state.set_state(BroadcastState.button)
    await show(cb, "🔘 Tugmani shu formatda yuboring:\n<code>Matn | https://havola</code>\n"
                   "Tugmani olib tashlash: <code>-</code>")


@admin_r.message(BroadcastState.button, TEXT)
async def msg_bc_btn(message: Message, state: FSMContext):
    t = message.text.strip()
    if t == "-":
        await state.update_data(btn_text=None, btn_url=None)
    else:
        if "|" not in t:
            await message.answer("❌ Format: <code>Matn | https://havola</code>")
            return
        text, url = [x.strip() for x in t.split("|", 1)]
        if not text or not url.lower().startswith(("http://", "https://", "tg://")):
            await message.answer("❌ Havola http(s):// bilan boshlanishi kerak.")
            return
        await state.update_data(btn_text=text[:60], btn_url=url)
    await state.set_state(BroadcastState.content)
    data = await state.get_data()
    await message.answer("✅ Tugma yangilandi. Yuborishga tayyor:", reply_markup=bc_confirm_kb(data))


@admin_r.callback_query(F.data == "bctest")
async def cb_bc_test(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if "message_id" not in data:
        await cb.answer("Xabar topilmadi", show_alert=True)
        return
    try:
        await bot.copy_message(cb.from_user.id, data["chat_id"], data["message_id"], reply_markup=bc_markup(data))
        await cb.answer("🧪 Test yuborildi")
    except Exception as e:
        await cb.answer(f"Xatolik: {str(e)[:150]}", show_alert=True)


@admin_r.callback_query(F.data == "bcgo")
async def cb_bc_go(cb: CallbackQuery, state: FSMContext):
    global _broadcast_running
    data = await state.get_data()
    if "message_id" not in data:
        await cb.answer("Xabar topilmadi", show_alert=True)
        return
    if _broadcast_running:
        await cb.answer("⏳ Boshqa xabar yuborilmoqda, tugashini kuting.", show_alert=True)
        return
    await state.clear()
    ids = audience_ids(data["kind"])
    await show(cb, f"🚀 Yuborish boshlandi ({len(ids)} ta)…")
    task = asyncio.create_task(run_broadcast(cb.from_user.id, ids, data["chat_id"], data["message_id"], bc_markup(data)))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def run_broadcast(admin_id, ids, chat_id, message_id, markup=None):
    global _broadcast_running
    _broadcast_running = True
    ok = fail = blocked = 0
    started = time.time()
    status = None
    try:
        status = await bot.send_message(admin_id, f"📤 Yuborilmoqda… 0/{len(ids)}")
    except Exception:
        pass
    try:
        for n, uid in enumerate(ids, 1):
            for attempt in range(2):
                try:
                    await bot.copy_message(uid, chat_id, message_id, reply_markup=markup)
                    ok += 1
                    break
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                except TelegramForbiddenError:
                    users = load_users()
                    if str(uid) in users:
                        users[str(uid)]["blocked"] = True
                        save_json(USERS_FILE, users)
                    blocked += 1
                    break
                except Exception:
                    fail += 1
                    break
            else:
                fail += 1
            if status and n % 100 == 0:
                try:
                    await status.edit_text(f"📤 Yuborilmoqda… {n}/{len(ids)}")
                except Exception:
                    pass
            await asyncio.sleep(0.05)
    finally:
        _broadcast_running = False
    took = int(time.time() - started)
    await safe_send(admin_id, f"📢 <b>Xabar yuborish tugadi</b> ({took} soniya)\n✅ Yetkazildi: {ok}\n"
                              f"🔕 Bot bloklagan: {blocked}\n❌ Xato: {fail}",
                    reply_markup=kb([b("⬅️ Admin panel", "admin")]))


# ───────────────────────── ADMIN: MAJBURIY KANALLAR ─────────────────────────
@admin_r.callback_query(F.data == "cr:menu")
async def cb_cr_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    chans = load_req_channels()
    rows = [[b(f"🗑 {c['title']}", f"crd:{i}")] for i, c in enumerate(chans)]
    rows.append([b("➕ Kanal qo'shish", "cr:add")])
    rows.append([b("⬅️ Admin panel", "admin")])
    await show(cb, "📡 <b>Majburiy obuna kanallari</b>\n"
                   f"{'Hozircha yo`q.' if not chans else 'O`chirish uchun bosing.'}\n\n"
                   "⚠️ Bot kanalda <b>admin</b> bo'lishi shart.", InlineKeyboardMarkup(inline_keyboard=rows))


@admin_r.callback_query(F.data == "cr:add")
async def cb_cr_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ChannelState.ref)
    await show(cb, "📡 Kanal <b>@username</b> yoki ID (-100...) yuboring (bekor: /cancel):")


@admin_r.message(ChannelState.ref, TEXT)
async def msg_cr_ref(message: Message, state: FSMContext):
    ref = message.text.strip()
    if not ref.startswith("@") and not ref.lstrip("-").isdigit():
        ref = "@" + ref
    try:
        chat = await bot.get_chat(ref)
        me = await bot.get_chat_member(chat.id, bot.id)
        if me.status not in ("administrator", "creator"):
            await message.answer("❌ Bot bu kanalda admin emas. Avval botni admin qiling, so'ng qayta yuboring:")
            return
        url = f"https://t.me/{chat.username}" if chat.username else (chat.invite_link or "")
        if not url:
            try:
                url = await bot.export_chat_invite_link(chat.id)
            except Exception:
                url = ""
    except Exception as e:
        await message.answer(f"❌ Kanal topilmadi: {escape(str(e))}\nQayta yuboring:")
        return
    chans = load_req_channels()
    chans = [c for c in chans if c["chat_id"] != chat.id]
    chans.append({"chat_id": chat.id, "title": chat.title or ref, "url": url})
    save_json(REQ_CHANNELS_FILE, chans)
    await state.clear()
    await message.answer(f"✅ Qo'shildi: <b>{escape(chat.title or ref)}</b>", reply_markup=kb([b("📡 Kanallar", "cr:menu")]))


@admin_r.callback_query(F.data.startswith("crd:"))
async def cb_cr_del(cb: CallbackQuery):
    idx = int(cb.data.split(":")[1])
    chans = load_req_channels()
    if 0 <= idx < len(chans):
        chans.pop(idx)
        save_json(REQ_CHANNELS_FILE, chans)
    await show(cb, "🗑 O'chirildi.", kb([b("📡 Kanallar", "cr:menu")]))


# ───────────────────────── ADMIN: SOZLAMALAR ─────────────────────────
TOGGLES = {
    "paywall": "💳 Pullik rejim",
    "maintenance": "🛠 Texnik ish rejimi",
    "notify_admins": "🔔 Admin bildirishnomalari",
    "auto_backup": "💾 Avto-zaxira (24 soat)",
    "auto_status": "📡 Kanal holatini avto-tekshirish",
    "match_reminders": "⏰ O'yin eslatmalari (botdan)",
}
# kalit: (nom, tur, ko'rsatma, HTML tekshiriladimi)
SETTING_FIELDS = {
    "support": ("📞 Aloqa", "text", "@username yoki havola yuboring", False),
    "price_text": ("💬 Narx matni", "text", "Narxlar/to'lov haqidagi matn (HTML mumkin)", True),
    "welcome": ("👋 Xush kelibsiz matni", "text", "HTML mumkin. <code>{name}</code> — foydalanuvchi ismi", True),
    "maintenance_text": ("🛠 Texnik ish matni", "text", "Texnik rejimda foydalanuvchilarga ko'rinadigan matn", True),
    "announcement": ("📣 E'lon (Mini App'da)", "text", "Mini App yuqorisida ko'rinadigan qisqa e'lon", False),
    "mini_app_url": ("🌐 Mini App URL", "url", "https:// bilan boshlanadigan havola", False),
    "trial_days": ("🎁 Yangi foydalanuvchiga sinov (kun)", "int", "Son yozing (0 = o'chirish)", False),
    "referral_days": ("👥 Referal bonusi (kun)", "int", "Taklif qilgan foydalanuvchiga beriladigan kun (0 = o'chirish)", False),
}


def _short(v, n=60):
    v = str(v or "").replace("\n", " ")
    return escape(v[:n] + ("…" if len(v) > n else "")) or "—"


def settings_view():
    s = load_settings()
    on = lambda k: "✅" if s.get(k) else "❌"
    text = (
        "⚙️ <b>Sozlamalar</b>\n\n"
        f"💳 Pullik rejim: {on('paywall')}\n🛠 Texnik ish rejimi: {on('maintenance')}\n"
        f"🔔 Admin bildirishnomalari: {on('notify_admins')}\n💾 Avto-zaxira: {on('auto_backup')}\n\n"
        f"🎁 Bepul sinov: <b>{int(s['trial_days'] or 0)}</b> kun\n👥 Referal bonusi: <b>{int(s['referral_days'] or 0)}</b> kun\n"
        f"📞 Aloqa: {_short(s['support'])}\n🌐 Mini App: {_short(get_mini_app_url(), 45)}\n"
        f"📣 E'lon: {_short(s['announcement'])}\n👋 Xush kelibsiz: {_short(s['welcome'], 40)}\n\n"
        "ℹ️ Pullik rejim yoqilsa, premium kontent faqat obunachilarga ochiladi."
    )
    rows = [[b(f"{'🟢' if s.get(k) else '⚪️'} {label}", f"set:tg:{k}")] for k, label in TOGGLES.items()]
    keys = list(SETTING_FIELDS)
    for i in range(0, len(keys), 2):
        rows.append([b(SETTING_FIELDS[k][0][:28], f"se:{k}") for k in keys[i:i + 2]])
    rows.append([b("⬅️ Admin panel", "admin")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@admin_r.callback_query(F.data == "set:menu")
async def cb_set_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = settings_view()
    await show(cb, text, markup)


@admin_r.callback_query(F.data.startswith("set:tg:"))
async def cb_set_toggle(cb: CallbackQuery):
    key = cb.data.split(":")[2]
    if key not in TOGGLES:
        await cb.answer()
        return
    s = load_settings()
    s[key] = not s.get(key)
    save_settings(s)
    text, markup = settings_view()
    await show(cb, text, markup)


@admin_r.callback_query(F.data.startswith("se:"))
async def cb_set_edit(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    if key not in SETTING_FIELDS:
        await cb.answer()
        return
    label, kind, hint, _ = SETTING_FIELDS[key]
    await state.set_state(SettingState.value)
    await state.update_data(key=key)
    cur = load_settings().get(key, "")
    await show(cb, f"{label}\n\n{hint}\n\nHozirgi: <code>{escape(str(cur)) or '—'}</code>\n"
                   "O'chirish/asl holiga qaytarish: <code>-</code>   |   Bekor: /cancel")


@admin_r.message(SettingState.value, TEXT)
async def msg_set_value(message: Message, state: FSMContext):
    key = (await state.get_data()).get("key")
    if key not in SETTING_FIELDS:
        await state.clear()
        return
    label, kind, hint, check_html = SETTING_FIELDS[key]
    raw = message.text.strip()
    if raw == "-":
        value = 0 if kind == "int" else ""
    elif kind == "int":
        if not raw.isdigit() or not 0 <= int(raw) <= 3650:
            await message.answer("❌ 0 dan 3650 gacha son yozing:")
            return
        value = int(raw)
    elif kind == "url":
        if not raw.lower().startswith("https://"):
            await message.answer("❌ Mini App havolasi <b>https://</b> bilan boshlanishi shart. Qayta yuboring:")
            return
        value = raw
    else:
        value = message.html_text.strip() if check_html else raw
    if check_html and value:
        try:
            m = await message.answer("👁 Ko'rinishi:\n\n" + value.replace("{name}", "Ism"))
            await m.delete()
        except TelegramBadRequest as e:
            await message.answer(f"❌ HTML xato: {escape(str(e))}\nQayta yuboring:")
            return
    s = load_settings()
    s[key] = value
    save_settings(s)
    await state.clear()
    text, markup = settings_view()
    await message.answer("✅ Saqlandi.\n\n" + text, reply_markup=markup)


# ───────────────────────── ADMIN: KANAL SO'ROVLARI (Mini App'dan) ─────────────────────────
def load_requests():
    return load_json(REQUESTS_FILE, [])


@admin_r.callback_query(F.data.startswith("rq:"))
async def cb_requests(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await render_requests(cb, int(cb.data.split(":")[1]))


async def render_requests(cb, page_no):
    reqs = [r for r in load_requests() if not r.get("done")]
    reqs.sort(key=lambda r: r["at"], reverse=True)
    if not reqs:
        await show(cb, "📭 Yangi kanal so'rovlari yo'q.", kb([b("⬅️ Admin panel", "admin")]))
        return
    chunk, page, pages = paginate(reqs, page_no, per=6)
    lines = [f"<b>#{r['id']}</b> {escape(r['text'][:150])}\n    👤 {user_label(r['uid'])} · {fmt_dt(parse_dt(r['at']))}"
             for r in chunk]
    rows = [[b(f"✅ #{r['id']} bajarildi", f"rqd:{r['id']}")] for r in chunk]
    rows.append(nav_row("rq", page, pages))
    rows.append([b("⬅️ Admin panel", "admin")])
    await show(cb, f"📩 <b>Kanal so'rovlari</b> ({len(reqs)} ta)\n\n" + "\n\n".join(lines),
               InlineKeyboardMarkup(inline_keyboard=rows))


@admin_r.callback_query(F.data.startswith("rqd:"))
async def cb_request_done(cb: CallbackQuery):
    rid = int(cb.data.split(":")[1])
    reqs = load_requests()
    target = None
    for r in reqs:
        if r["id"] == rid:
            r["done"] = True
            target = r
    save_json(REQUESTS_FILE, reqs)
    if target:
        await safe_send(target["uid"], f"✅ Siz so'ragan kanal/kontent ko'rib chiqildi: <i>{escape(target['text'][:80])}</i>")
    await render_requests(cb, 0)


# ───────────────────────── ADMIN: ADMINLAR (faqat ega) ─────────────────────────
@admin_r.callback_query(F.data == "own:menu")
async def cb_owner_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    if not is_owner(cb.from_user.id):
        await cb.answer("🔒 Faqat bosh admin (ega) uchun.", show_alert=True)
        return
    extra = [int(x) for x in load_settings().get("admins", [])]
    lines = [f"👑 {user_label(a)}" for a in ADMIN_IDS] + [f"🛡 {user_label(a)}" for a in extra]
    rows = [[b(f"🗑 {(load_users().get(str(a), {}).get('name') or a)[:25]}", f"own:del:{a}")] for a in extra]
    rows += [[b("➕ Admin qo'shish", "own:add")], [b("⬅️ Admin panel", "admin")]]
    await show(cb, "👑 <b>Adminlar</b>\n\n" + "\n".join(lines) +
               "\n\n👑 — ega (.env), 🛡 — qo'shimcha admin. O'chirish uchun bosing.",
               InlineKeyboardMarkup(inline_keyboard=rows))


@admin_r.callback_query(F.data == "own:add")
async def cb_owner_add(cb: CallbackQuery, state: FSMContext):
    if not is_owner(cb.from_user.id):
        await cb.answer("🔒 Faqat ega.", show_alert=True)
        return
    await state.set_state(AdminAddState.uid)
    await show(cb, "➕ Yangi admin <b>ID</b> yoki <b>@username</b> ini yuboring (u botga /start bosgan bo'lishi kerak). Bekor: /cancel")


@admin_r.message(AdminAddState.uid, TEXT)
async def msg_owner_add(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        await state.clear()
        return
    uid = find_user(message.text)
    if uid is None:
        await message.answer("❌ Foydalanuvchi topilmadi. Qayta yuboring yoki /cancel:")
        return
    s = load_settings()
    admins = [int(x) for x in s.get("admins", [])]
    if uid not in admins and uid not in ADMIN_IDS:
        admins.append(uid)
    s["admins"] = admins
    save_settings(s)
    await state.clear()
    await safe_send(uid, "🛡 Siz admin etib tayinlandingiz. /admin buyrug'ini yuboring.")
    await message.answer(f"✅ Admin qo'shildi: {user_label(uid)}", reply_markup=kb([b("👑 Adminlar", "own:menu")]))


@admin_r.callback_query(F.data.startswith("own:del:"))
async def cb_owner_del(cb: CallbackQuery):
    if not is_owner(cb.from_user.id):
        await cb.answer("🔒 Faqat ega.", show_alert=True)
        return
    uid = int(cb.data.split(":")[2])
    s = load_settings()
    s["admins"] = [int(x) for x in s.get("admins", []) if int(x) != uid]
    save_settings(s)
    await safe_send(uid, "ℹ️ Sizning admin huquqingiz olib tashlandi.")
    await show(cb, "🗑 Admin o'chirildi.", kb([b("👑 Adminlar", "own:menu")]))


# ───────────────────────── ADMIN: ZAXIRA / EKSPORT ─────────────────────────
BACKUP_FILES = [DATA_FILE, USERS_FILE, SUBS_FILE, SETTINGS_FILE, REQ_CHANNELS_FILE, REQUESTS_FILE]


def users_csv_bytes():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["id", "name", "username", "joined", "banned", "blocked", "sub_expires", "referred_by"])
    subs = load_subs()["subs"]
    for uid, u in load_users().items():
        w.writerow([uid, u.get("name", ""), u.get("username", ""), u.get("joined", ""), u.get("banned", False),
                    u.get("blocked", False), subs.get(uid, {}).get("expires", ""), u.get("ref", "")])
    return out.getvalue().encode("utf-8-sig")


def subs_csv_bytes():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["id", "name", "username", "expires", "active", "grants"])
    users = load_users()
    now = utcnow()
    for uid, s in load_subs()["subs"].items():
        u = users.get(uid, {})
        w.writerow([uid, u.get("name", ""), u.get("username", ""), s["expires"],
                    parse_dt(s["expires"]) > now, len(s.get("history", []))])
    return out.getvalue().encode("utf-8-sig")


async def send_backup(chat_id, auto=False):
    stamp = (utcnow() + timedelta(hours=5)).strftime("%Y-%m-%d_%H-%M")
    sent = 0
    for path in BACKUP_FILES:
        if os.path.exists(path):
            with open(path, "rb") as f:
                data = f.read()
            await bot.send_document(chat_id, BufferedInputFile(data, filename=os.path.basename(path)),
                                    caption=f"💾 {'Avto-' if auto else ''}zaxira {stamp}" if sent == 0 else None)
            sent += 1
    if not sent:
        await bot.send_message(chat_id, "📭 Zaxiralanadigan fayl yo'q.")


@admin_r.callback_query(F.data == "bk:menu")
async def cb_backup_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    last = load_settings().get("last_backup")
    await show(cb, "💾 <b>Zaxira / Eksport</b>\n\nBarcha JSON fayllarni (kontent, foydalanuvchilar, obunalar, sozlamalar, kanallar) "
                   "sizga fayl qilib yuboradi. Qayta tiklash: <b>Import</b> bo'limida shu faylni yuboring.\n"
                   f"🕒 Oxirgi avto-zaxira: {fmt_dt(parse_dt(last)) if last else '—'}",
               kb([b("💾 Hozir zaxira olish", "bk:now")],
                  [b("📤 Foydalanuvchilar CSV", "bk:csv_users"), b("📤 Obunalar CSV", "bk:csv_subs")],
                  [b("📥 Import / tiklash", "imp:menu")],
                  [b("⬅️ Admin panel", "admin")]))


@admin_r.callback_query(F.data == "bk:now")
async def cb_backup_now(cb: CallbackQuery):
    await cb.answer("Yuborilmoqda…")
    await send_backup(cb.from_user.id)


@admin_r.callback_query(F.data == "bk:csv_users")
async def cb_csv_users(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer_document(BufferedInputFile(users_csv_bytes(), filename="users.csv"), caption="👥 Foydalanuvchilar")


@admin_r.callback_query(F.data == "bk:csv_subs")
async def cb_csv_subs(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer_document(BufferedInputFile(subs_csv_bytes(), filename="subscriptions.csv"), caption="💎 Obunalar")


# ───────────────────────── ADMIN: TOZALASH ─────────────────────────
CLEAN_TASKS = {
    "blocked": ("🔕 Botni bloklagan foydalanuvchilarni bazadan o'chirish", "Botni bloklagan foydalanuvchilar o'chiriladi (adminlar va obunachilar qoladi)."),
    "expired": ("⌛ Muddati tugagan obunalarni o'chirish", "Muddati tugagan obuna yozuvlari (tarixi bilan) o'chiriladi."),
    "links": ("🔗 Nofaol / limiti tugagan linklarni o'chirish", "O'chirilgan yoki to'lgan linklar o'chiriladi. Berilgan obunalar saqlanadi."),
    "views": ("♻️ Barcha ko'rishlarni nolga tushirish", "Hamma kontentning ko'rishlar soni 0 bo'ladi."),
}


@admin_r.callback_query(F.data == "cl:menu")
async def cb_clean_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = [[b(label[:60], f"cl:ask:{k}")] for k, (label, _) in CLEAN_TASKS.items()]
    rows.append([b("⬅️ Admin panel", "admin")])
    await show(cb, "🧹 <b>Tozalash</b>\nBazani tartibga keltirish. Avval zaxira olishni tavsiya qilamiz.", InlineKeyboardMarkup(inline_keyboard=rows))


@admin_r.callback_query(F.data.startswith("cl:ask:"))
async def cb_clean_ask(cb: CallbackQuery):
    k = cb.data.split(":")[2]
    if k not in CLEAN_TASKS:
        await cb.answer()
        return
    await show(cb, f"⚠️ <b>{CLEAN_TASKS[k][0]}</b>\n\n{CLEAN_TASKS[k][1]}\n\nDavom etamizmi?",
               kb([b("✅ Ha", f"cl:do:{k}"), b("❌ Yo'q", "cl:menu")]))


@admin_r.callback_query(F.data.startswith("cl:do:"))
async def cb_clean_do(cb: CallbackQuery):
    k = cb.data.split(":")[2]
    now = utcnow()
    n = 0
    if k == "blocked":
        users = load_users()
        subs = load_subs()["subs"]
        for uid in [u for u, r in users.items() if r.get("blocked") and not is_admin(int(u)) and u not in subs]:
            users.pop(uid)
            n += 1
        save_json(USERS_FILE, users)
    elif k == "expired":
        db = load_subs()
        for uid in [u for u, s in db["subs"].items() if parse_dt(s["expires"]) <= now]:
            db["subs"].pop(uid)
            n += 1
        save_subs(db)
    elif k == "links":
        db = load_subs()
        for c in [c for c, l in db["links"].items() if not l.get("active", True) or len(l["used_by"]) >= l["max_uses"]]:
            db["links"].pop(c)
            n += 1
        save_subs(db)
    elif k == "views":
        items = load_items()
        for i in items:
            i["views"] = 0
        n = len(items)
        save_items(items)
    else:
        await cb.answer()
        return
    await show(cb, f"✅ Bajarildi. Ta'sirlangan yozuvlar: <b>{n}</b>", kb([b("🧹 Tozalash", "cl:menu")], [b("⬅️ Admin panel", "admin")]))


# ───────────────────────── ADMIN: IMPORT (M3U / JSON) ─────────────────────────
RESTORE_MAP = {
    "users.json": (USERS_FILE, dict),
    "subscriptions.json": (SUBS_FILE, dict),
    "settings.json": (SETTINGS_FILE, dict),
    "required_channels.json": (REQ_CHANNELS_FILE, list),
}


def parse_m3u(text):
    out, cur = [], None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            name = line.rsplit(",", 1)[-1].strip() if "," in line else ""
            logo = re.search(r'tvg-logo="([^"]*)"', line)
            grp = re.search(r'group-title="([^"]*)"', line)
            cur = {"title": name or "Noma'lum kanal",
                   "poster": logo.group(1) if logo else "",
                   "genre": (grp.group(1) if grp and grp.group(1) else "TV")}
        elif line.startswith("#"):
            continue
        elif line.lower().startswith(("http://", "https://")):
            if cur is None:
                cur = {"title": line.rstrip("/").split("/")[-1][:40] or "Kanal", "poster": "", "genre": "TV"}
            cur["url"] = line
            out.append(cur)
            cur = None
    return out


def decode_bytes(raw):
    for enc in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def add_imported(entries, ctype, premium, language="uz", replace=False):
    items = [] if replace else load_items()
    known = {i.get("url") for i in items}
    next_id = max([x.get("id", 0) for x in items], default=0) + 1
    added = skipped = 0
    for e in entries:
        url = str(e.get("url", "")).strip()
        title = str(e.get("title", "")).strip()
        if not url.lower().startswith(("http://", "https://")) or not title or url in known:
            skipped += 1
            continue
        ct = e.get("content_type") if e.get("content_type") in TYPES else ctype
        item = {
            "id": next_id, "title": title, "content_type": ct,
            "language": e.get("language") or language, "genre": e.get("genre") or "TV",
            "badge": e.get("badge") or BADGES[ct], "url": url, "poster": e.get("poster", "") or "",
            "premium": bool(e.get("premium", premium)), "views": int(e.get("views", 0) or 0),
            "added": e.get("added") or utcnow().isoformat(),
        }
        if e.get("match_time"):
            item["match_time"] = e["match_time"]
        for k in ("featured", "hidden"):
            if e.get(k):
                item[k] = True
        for k in ("home", "away", "home_logo", "away_logo", "league", "score", "minute", "urls", "status",
                  "epg", "desc", "year", "cast", "trailer", "intro", "seasons", "duration", "backdrop"):
            if e.get(k) not in (None, "", [], {}):
                item[k] = e[k]
        items.append(item)
        known.add(url)
        next_id += 1
        added += 1
    save_items(items)
    return added, skipped


@admin_r.callback_query(F.data == "imp:menu")
async def cb_import_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(ImportState.file)
    await show(cb, "📥 <b>Import</b>\n\nFayl yuboring (hujjat sifatida):\n"
                   "• <b>.m3u / .m3u8</b> — pleylist (ko'plab kanalni birdan qo'shadi)\n"
                   "• <b>data.json</b> — kontentni qo'shish yoki to'liq almashtirish\n"
                   "• <b>users.json / subscriptions.json / settings.json / required_channels.json</b> — zaxiradan tiklash\n\n"
                   "Maksimal hajm: 10 MB. Bekor: /cancel", kb([b("⬅️ Admin panel", "admin")]))


@admin_r.message(ImportState.file, F.document)
async def msg_import_file(message: Message, state: FSMContext):
    doc = message.document
    name = (doc.file_name or "").lower()
    if doc.file_size and doc.file_size > 10 * 1024 * 1024:
        await message.answer("❌ Fayl juda katta (10 MB gacha).")
        return
    buf = await bot.download(doc)
    text = decode_bytes(buf.read())
    if name.endswith((".m3u", ".m3u8")) or text.lstrip().startswith("#EXTM3U"):
        entries = parse_m3u(text)
        if not entries:
            await message.answer("❌ Pleylistda havola topilmadi.")
            return
        if len(entries) > 3000:
            entries = entries[:3000]
        known = {i.get("url") for i in load_items()}
        new = [e for e in entries if e["url"] not in known]
        await state.update_data(entries=entries, mode="m3u")
        await message.answer(f"📄 Pleylist: <b>{len(entries)}</b> ta havola, shundan yangi: <b>{len(new)}</b> ta.\n"
                             "Qaysi turga qo'shamiz?",
                             reply_markup=kb([b(label, f"imt:{t}") for t, label in list(TYPES.items())[:3]],
                                             [b(label, f"imt:{t}") for t, label in list(TYPES.items())[3:]],
                                             [b("❌ Bekor", "admin")]))
        return
    try:
        obj = json.loads(text)
    except Exception as e:
        await message.answer(f"❌ JSON o'qib bo'lmadi: {escape(str(e))[:200]}")
        return
    if name in RESTORE_MAP:
        path, typ = RESTORE_MAP[name]
        if not isinstance(obj, typ):
            await message.answer(f"❌ {name} formati noto'g'ri.")
            return
        await state.update_data(mode="restore", name=name, obj=obj)
        await message.answer(f"⚠️ <b>{name}</b> to'liq almashtiriladi. Joriy ma'lumot o'chadi!\nTasdiqlaysizmi?",
                             reply_markup=kb([b("✅ Ha, tiklash", "imr:yes"), b("❌ Bekor", "admin")]))
        return
    entries = obj.get("items") if isinstance(obj, dict) else obj
    if not isinstance(entries, list) or not all(isinstance(x, dict) for x in entries):
        await message.answer("❌ Kutilgan format: <code>{\"items\": [...]}</code> yoki ob'ektlar ro'yxati.")
        return
    await state.update_data(entries=entries, mode="json")
    await message.answer(f"📄 data.json: <b>{len(entries)}</b> ta yozuv.\nQanday import qilamiz?",
                         reply_markup=kb([b("➕ Qo'shish (takrorlar o'tkaziladi)", "imj:merge")],
                                         [b("♻️ To'liq almashtirish", "imj:replace")],
                                         [b("❌ Bekor", "admin")]))


@admin_r.message(ImportState.file)
async def msg_import_wrong(message: Message):
    if message.text and message.text.startswith("/"):
        return
    await message.answer("📎 Iltimos, faylni <b>hujjat</b> sifatida yuboring (yoki /cancel).")


@admin_r.callback_query(F.data.startswith("imt:"))
async def cb_import_type(cb: CallbackQuery, state: FSMContext):
    t = cb.data.split(":")[1]
    data = await state.get_data()
    if t not in TYPES or data.get("mode") != "m3u":
        await cb.answer("Avval fayl yuboring", show_alert=True)
        return
    await state.update_data(ctype=t)
    await show(cb, f"{TYPES[t]}\n💎 Premium qilib qo'shamizmi?",
               kb([b("💎 Premium", "imp:go:1"), b("🆓 Bepul", "imp:go:0")], [b("❌ Bekor", "admin")]))


@admin_r.callback_query(F.data.startswith("imp:go:"))
async def cb_import_go(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("mode") != "m3u" or "ctype" not in data:
        await cb.answer("Ma'lumot topilmadi", show_alert=True)
        return
    added, skipped = add_imported(data["entries"], data["ctype"], cb.data.endswith("1"))
    await state.clear()
    await show(cb, f"✅ <b>Import tugadi</b>\n➕ Qo'shildi: {added}\n⏭ O'tkazildi (takror/xato): {skipped}",
               kb([b("📋 Ro'yxat", "lst:0")], [b("⬅️ Admin panel", "admin")]))


@admin_r.callback_query(F.data.startswith("imj:"))
async def cb_import_json(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("mode") != "json":
        await cb.answer("Ma'lumot topilmadi", show_alert=True)
        return
    replace = cb.data.endswith("replace")
    added, skipped = add_imported(data["entries"], "tv", False, replace=replace)
    await state.clear()
    await show(cb, f"✅ <b>Import tugadi</b>\n{'♻️ To`liq almashtirildi' if replace else '➕ Qo`shish rejimi'}\n"
                   f"Qo'shildi: {added} · O'tkazildi: {skipped}",
               kb([b("📋 Ro'yxat", "lst:0")], [b("⬅️ Admin panel", "admin")]))


@admin_r.callback_query(F.data == "imr:yes")
async def cb_import_restore(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("mode") != "restore" or data.get("name") not in RESTORE_MAP:
        await cb.answer("Ma'lumot topilmadi", show_alert=True)
        return
    path, _ = RESTORE_MAP[data["name"]]
    save_json(path, data["obj"])
    if path == SETTINGS_FILE:
        reset_settings_cache()
    await state.clear()
    await show(cb, f"✅ <b>{data['name']}</b> tiklandi.", kb([b("⬅️ Admin panel", "admin")]))


# ───────────────────────── MINI APP API ─────────────────────────
def verify_init_data(init_data):
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received = pairs.pop("hash", None)
        if not received:
            return None
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > 86400:
            return None
        return json.loads(pairs["user"])
    except Exception:
        return None


def _json_error(exc_cls, error):
    return exc_cls(text=json.dumps({"error": error}), content_type="application/json")


def maintenance_info():
    s = load_settings()
    return {"on": bool(s.get("maintenance")),
            "text": re.sub(r"<[^>]+>", "", s.get("maintenance_text", "") or "Tez orada qaytamiz")}


def _auth(request, allow_maintenance=False):
    """initData ni tekshiradi va Telegram foydalanuvchisini (dict) qaytaradi."""
    init = request.headers.get("X-Init-Data") or request.query.get("initData", "")
    user = verify_init_data(init)
    if not user:
        raise _json_error(web.HTTPUnauthorized, "unauthorized")
    uid = user["id"]
    if not is_admin(uid):
        if is_banned(uid):
            raise _json_error(web.HTTPForbidden, "banned")
        s = load_settings()
        if s.get("maintenance") and not allow_maintenance:
            raise web.HTTPServiceUnavailable(
                text=json.dumps({"error": "maintenance", "message": re.sub(r"<[^>]+>", "", s.get("maintenance_text", ""))}),
                content_type="application/json")
    return user


async def api_items(request):
    user = _auth(request, allow_maintenance=True)
    uid = user["id"]
    paywall = load_settings()["paywall"]
    subscribed = is_subscribed(uid)
    maint = maintenance_info()
    out = []
    for it in ([] if maint["on"] and not is_admin(uid) else load_items()):
        if it.get("hidden"):
            continue
        item = dict(it)
        locked = bool(paywall and it.get("premium") and not subscribed)
        item["locked"] = locked
        if locked:
            item["url"] = ""
        out.append(item)
    out.sort(key=lambda x: not x.get("featured"))  # tanlanganlar birinchi (barqaror saralash)
    exp = sub_expiry(uid)
    s = load_settings()
    return web.json_response({
        "paywall": paywall, "subscribed": subscribed,
        "expires": exp.isoformat() if exp else None,
        "bot": BOT_USERNAME, "items": out,
        "announcement": s.get("announcement", ""),
        "notice": s.get("announcement", ""),   # index.html "notice" maydoni
        "maintenance": maint,
        "server_time": utcnow().isoformat(),
    })


async def api_view(request):
    user = _auth(request)
    uid = user["id"]
    try:
        item_id = int(request.match_info["item_id"])
    except ValueError:
        raise web.HTTPBadRequest()
    items = load_items()
    for i in items:
        if i.get("id") == item_id:
            if load_settings()["paywall"] and i.get("premium") and not is_subscribed(uid):
                raise web.HTTPForbidden()
            i["views"] = int(i.get("views", 0)) + 1
            save_items(items)
            return web.json_response({"ok": True, "views": i["views"]})
    raise web.HTTPNotFound()


async def api_config(request):
    _auth(request, allow_maintenance=True)
    s = load_settings()
    return web.json_response({
        "paywall": s["paywall"], "announcement": s.get("announcement", ""),
        "support": support_url(), "price_text": re.sub(r"<[^>]+>", "", s.get("price_text", "")),
        "bot": BOT_USERNAME, "types": TYPES,
        "notice": s.get("announcement", ""), "maintenance": maintenance_info(),
    })


async def api_me(request):
    user = _auth(request)
    uid = user["id"]
    exp = sub_expiry(uid)
    days = int(load_settings().get("referral_days") or 0)
    return web.json_response({
        "id": uid, "name": user.get("first_name", ""), "admin": is_admin(uid),
        "subscribed": is_subscribed(uid), "expires": exp.isoformat() if exp else None,
        "referral_link": f"https://t.me/{BOT_USERNAME}?start=ref_{uid}" if days > 0 and BOT_USERNAME else None,
        "referral_days": days,
        "follow": load_users().get(str(uid), {}).get("follow", []),
    })


_redeem_attempts = {}


async def api_redeem(request):
    user = _auth(request)
    uid = user["id"]
    now = time.time()
    tries = [t for t in _redeem_attempts.get(uid, []) if now - t < 3600]
    if len(tries) >= 10:
        return web.json_response({"ok": False, "message": "Juda ko'p urinish. Keyinroq qayta urinib ko'ring."}, status=429)
    tries.append(now)
    _redeem_attempts[uid] = tries
    try:
        body = await request.json()
    except Exception:
        body = {}
    code = str(body.get("code", "")).strip().split("sub_")[-1].strip()
    if not code:
        return web.json_response({"ok": False, "message": "Kod kiritilmadi."}, status=400)
    ok, text, exp = redeem_code(uid, code)
    if ok:
        asyncio.create_task(notify_admins(f"🎟 {user_label(uid)} Mini App orqali obuna oldi (<code>{escape(code)}</code>)"))
    return web.json_response({"ok": ok, "message": re.sub(r"<[^>]+>", "", text),
                              "expires": exp.isoformat() if exp else None})


_request_attempts = {}


async def api_request(request):
    """Mini App: foydalanuvchi kanal/kontent so'raydi -> adminlarga yuboriladi."""
    user = _auth(request)
    uid = user["id"]
    now = time.time()
    tries = [t for t in _request_attempts.get(uid, []) if now - t < 3600]
    if len(tries) >= 5:
        return web.json_response({"ok": False, "message": "Juda ko'p so'rov. Keyinroq urinib ko'ring."}, status=429)
    try:
        body = await request.json()
    except Exception:
        body = {}
    text = " ".join(str(body.get("text") or body.get("title") or body.get("name") or "").split())[:300]
    note = " ".join(str(body.get("note") or "").split())[:300]
    if note:
        text = f"{text} — {note}" if text else note
    if len(text) < 2:
        return web.json_response({"ok": False, "message": "So'rov matnini yozing."}, status=400)
    tries.append(now)
    _request_attempts[uid] = tries
    reqs = load_requests()
    rid = max([r["id"] for r in reqs], default=0) + 1
    reqs.append({"id": rid, "uid": uid, "text": text, "at": utcnow().isoformat(), "done": False})
    save_json(REQUESTS_FILE, reqs[-500:])
    for aid in all_admin_ids():
        await safe_send(aid, f"📩 <b>Kanal so'rovi #{rid}</b>\n{escape(text)}\n👤 {user_label(uid)}",
                        reply_markup=kb([b("✅ Bajarildi", f"rqd:{rid}")]))
    return web.json_response({"ok": True, "message": "So'rovingiz adminga yuborildi."})


async def api_follow(request):
    """Mini App: jamoani kuzatish. Body: {"team": "Real Madrid", "on": true}."""
    user = _auth(request)
    uid = str(user["id"])
    try:
        body = await request.json()
    except Exception:
        body = {}
    team = " ".join(str(body.get("team", "")).split())[:60]
    if not team:
        return web.json_response({"ok": False}, status=400)
    users = load_users()
    rec = users.get(uid)
    if rec is None:
        return web.json_response({"ok": False}, status=404)
    follow = [t for t in rec.get("follow", []) if t.lower() != team.lower()]
    if body.get("on", True):
        follow.append(team)
    rec["follow"] = follow[-30:]
    save_json(USERS_FILE, users)
    return web.json_response({"ok": True, "follow": rec["follow"]})


_img_cache = {}


async def api_img(request):
    """Telegramga yuklangan rasmni file_id orqali beradi (bot tokeni ko'rinmaydi)."""
    fid = request.match_info["fid"]
    if not re.fullmatch(r"[A-Za-z0-9_\-]{10,200}", fid):
        raise web.HTTPNotFound()
    hit = _img_cache.get(fid)
    if not hit:
        try:
            f = await bot.get_file(fid)
            buf = io.BytesIO()
            await bot.download_file(f.file_path, destination=buf)
        except Exception:
            raise web.HTTPNotFound()
        ext = (f.file_path or "").rsplit(".", 1)[-1].lower()
        ctype = {"png": "image/png", "webp": "image/webp", "gif": "image/gif", "svg": "image/svg+xml"}.get(ext, "image/jpeg")
        hit = (buf.getvalue(), ctype)
        if len(_img_cache) > 200:
            _img_cache.pop(next(iter(_img_cache)))
        _img_cache[fid] = hit
    return web.Response(body=hit[0], content_type=hit[1], headers={"Cache-Control": "public, max-age=86400"})


async def api_report(request):
    """Mini App: 'kanal ishlamayapti' xabari. 3 ta turli foydalanuvchi xabar qilsa status='down'."""
    user = _auth(request)
    uid = user["id"]
    try:
        item_id = int(request.match_info["item_id"])
    except ValueError:
        raise web.HTTPBadRequest()
    items = load_items()
    for i in items:
        if i.get("id") == item_id:
            reps = i.setdefault("reports", [])
            if uid in reps:
                return web.json_response({"ok": True})
            reps.append(uid)
            went_down = len(reps) >= 3 and i.get("status") != "down"
            if went_down:
                i["status"] = "down"
            save_items(items)
            if len(reps) == 1 or went_down:
                await notify_admins(f"⚠️ <b>Shikoyat:</b> {escape(i.get('title', ''))} (ID {item_id}) — "
                                    f"{len(reps)} ta xabar{' · 🔴 MUAMMO belgilandi' if went_down else ''}\n"
                                    f"👤 {user_label(uid)}")
            return web.json_response({"ok": True})
    raise web.HTTPNotFound()


async def api_health(request):
    return web.json_response({"ok": True})


def cors_origin():
    if CORS_ORIGIN_ENV:
        return CORS_ORIGIN_ENV
    o = urlparse(get_mini_app_url())
    return f"{o.scheme}://{o.netloc}"


@web.middleware
async def cors_mw(request, handler):
    try:
        resp = web.Response() if request.method == "OPTIONS" else await handler(request)
    except web.HTTPException as e:
        resp = e
    resp.headers["Access-Control-Allow-Origin"] = cors_origin()
    resp.headers["Access-Control-Allow-Headers"] = "X-Init-Data, Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Max-Age"] = "600"
    return resp


def make_api_app():
    app = web.Application(middlewares=[cors_mw])
    app.router.add_get("/", api_health)
    app.router.add_get("/health", api_health)
    app.router.add_route("*", "/api/items", api_items)
    app.router.add_route("*", "/api/view/{item_id}", api_view)
    app.router.add_route("*", "/api/config", api_config)
    app.router.add_route("*", "/api/me", api_me)
    app.router.add_route("*", "/api/redeem", api_redeem)
    app.router.add_route("*", "/api/request", api_request)
    app.router.add_route("*", "/api/follow", api_follow)
    app.router.add_get("/img/{fid}", api_img)
    app.router.add_route("*", "/api/report/{item_id}", api_report)
    return app


async def start_api():
    if not API_PORT:
        logger.info("API o'chirilgan (PORT/API_PORT berilmagan).")
        return None
    runner = web.AppRunner(make_api_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(API_PORT)).start()
    logger.info("API ishga tushdi: port %s", API_PORT)
    return runner


# ───────────────────────── FON VAZIFALAR ─────────────────────────
async def reminder_loop():
    while True:
        try:
            db = load_subs()
            now = utcnow()
            changed = False
            for uid, s in db["subs"].items():
                left = parse_dt(s["expires"]) - now
                if timedelta(0) < left <= timedelta(days=3) and not s.get("warned"):
                    await safe_send(int(uid), f"⏰ Obunangiz tugashiga <b>{left.days + 1} kun</b> qoldi. Uzaytirish uchun admin bilan bog'laning.")
                    s["warned"] = True
                    changed = True
                elif left <= timedelta(0) and not s.get("expired_notified"):
                    await safe_send(int(uid), "⛔️ Obunangiz muddati tugadi. Qayta obuna olish uchun admin bilan bog'laning.")
                    s["expired_notified"] = True
                    changed = True
            if changed:
                save_subs(db)
        except Exception:
            logger.exception("reminder_loop xatolik")
        await asyncio.sleep(3600)


async def backup_loop():
    """Har 24 soatda barcha JSON fayllarni ega(lar)ga yuboradi."""
    while True:
        await asyncio.sleep(600)
        try:
            s = load_settings()
            if not s.get("auto_backup"):
                continue
            last = s.get("last_backup")
            if last and utcnow() - parse_dt(last) < timedelta(hours=24):
                continue
            for aid in ADMIN_IDS:
                try:
                    await send_backup(aid, auto=True)
                except Exception as e:
                    logger.warning("Avto-zaxira yuborilmadi %s: %s", aid, e)
            s2 = load_settings()
            s2["last_backup"] = utcnow().isoformat()
            save_settings(s2)
        except Exception:
            logger.exception("backup_loop xatolik")


async def _probe(session, url):
    try:
        async with session.get(url, headers={"Range": "bytes=0-1023", "User-Agent": "Mozilla/5.0"},
                               allow_redirects=True) as r:
            return r.status < 400
    except Exception:
        return False


async def channel_status_loop():
    """Har 15 daqiqada TV kanallarni tekshiradi. 2 marta ketma-ket xato bo'lsa status='down'."""
    fails = {}
    await asyncio.sleep(60)
    while True:
        try:
            if load_settings().get("auto_status"):
                tv = [(i["id"], i.get("url", "")) for i in load_items()
                      if i.get("content_type") == "tv" and str(i.get("url", "")).startswith(("http://", "https://"))]
                results = {}
                async with ClientSession(timeout=ClientTimeout(total=8)) as session:
                    sem = asyncio.Semaphore(10)

                    async def one(iid, url):
                        async with sem:
                            results[iid] = await _probe(session, url)
                    await asyncio.gather(*(one(iid, url) for iid, url in tv))
                items = load_items()
                changed, went_down, came_up = False, [], []
                for i in items:
                    iid = i.get("id")
                    if iid not in results:
                        continue
                    if results[iid]:
                        fails[iid] = 0
                        if i.get("status") == "down":
                            i["status"] = "ok"
                            i.pop("reports", None)
                            came_up.append(i.get("title", iid))
                            changed = True
                    else:
                        fails[iid] = fails.get(iid, 0) + 1
                        if fails[iid] >= 2 and i.get("status") != "down":
                            i["status"] = "down"
                            went_down.append(i.get("title", iid))
                            changed = True
                if changed:
                    save_items(items)
                if went_down:
                    await notify_admins("🔴 <b>Kanal ishlamayapti:</b>\n" + "\n".join(f"• {escape(str(t))}" for t in went_down[:20]))
                if came_up:
                    await notify_admins("🟢 <b>Qayta ishladi:</b>\n" + "\n".join(f"• {escape(str(t))}" for t in came_up[:20]))
        except Exception:
            logger.exception("channel_status_loop xatolik")
        await asyncio.sleep(900)


async def match_reminder_loop():
    """Kuzatilgan jamoa o'yiniga ~30 daqiqa qolganda botdan eslatma yuboradi."""
    while True:
        await asyncio.sleep(60)
        try:
            if not load_settings().get("match_reminders"):
                continue
            now = utcnow()
            due = []
            for i in load_items():
                if i.get("content_type") != "football" or i.get("reminded") or not i.get("match_time"):
                    continue
                try:
                    start = parse_dt(i["match_time"])
                except ValueError:
                    continue
                if start.tzinfo is None:
                    start = start.replace(tzinfo=TZ_UZ)
                if timedelta(0) < start - now <= timedelta(minutes=30):
                    due.append((i, start))
            if not due:
                continue
            users = load_users()
            for i, start in due:
                teams = {str(i.get("home", "")).lower(), str(i.get("away", "")).lower()} - {""}
                text = (f"🔔 ⚽ <b>{escape(i.get('title', ''))}</b>\n⏰ {fmt_dt(start)} da boshlanadi"
                        f"{' · ' + escape(str(i['league'])) if i.get('league') else ''}")
                markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                    text="📺 Ko'rish", web_app=WebAppInfo(url=get_mini_app_url()))]])
                for uid, u in users.items():
                    if u.get("banned") or u.get("blocked"):
                        continue
                    if teams & {t.lower() for t in u.get("follow", [])}:
                        await safe_send(int(uid), text, reply_markup=markup)
                        await asyncio.sleep(0.05)
            ids = {i["id"] for i, _ in due}
            items = load_items()
            for i in items:
                if i.get("id") in ids:
                    i["reminded"] = True
            save_items(items)
        except Exception:
            logger.exception("match_reminder_loop xatolik")


async def setup_commands():
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Bosh menyu"),
            BotCommand(command="profile", description="Obuna holati"),
            BotCommand(command="redeem", description="Obuna kodini kiritish"),
            BotCommand(command="help", description="Yordam"),
        ])
        for aid in all_admin_ids():
            await bot.set_my_commands([
                BotCommand(command="start", description="Bosh menyu"),
                BotCommand(command="admin", description="Admin panel"),
                BotCommand(command="user", description="Foydalanuvchi kartasi"),
                BotCommand(command="grant", description="Obuna berish"),
                BotCommand(command="backup", description="Zaxira nusxa"),
                BotCommand(command="profile", description="Obuna holati"),
                BotCommand(command="help", description="Yordam"),
            ], scope=BotCommandScopeChat(chat_id=aid))
    except Exception as e:
        logger.warning("Buyruqlarni o'rnatib bo'lmadi: %s", e)


# ───────────────────────── ISHGA TUSHIRISH ─────────────────────────
dp.include_router(admin_r)
dp.include_router(user_r)


async def main():
    global BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username
    logger.info("Bot ishga tushdi: @%s", BOT_USERNAME)
    await setup_commands()
    runner = await start_api()
    tasks = [asyncio.create_task(reminder_loop()), asyncio.create_task(backup_loop()),
             asyncio.create_task(channel_status_loop()), asyncio.create_task(match_reminder_loop())]
    _bg_tasks.update(tasks)
    try:
        await dp.start_polling(bot)
    finally:
        for t in tasks:
            t.cancel()
        if runner:
            await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())