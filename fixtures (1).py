"""
Kunlik o'yinlar moduli (football-data.org, BITTA API).
- Har kuni bugun/ertangi o'yinlar jadvalini avtomatik oladi (logo, liga, vaqt, jonli hisob)
- Admin panel: "📅 Kunlik o'yinlar" — ro'yxat, havola bilan qo'shish, hammasini qo'shish, top futbolchilar
- Mini App uchun /api/fixtures (jadval + top futbolchilar)
- Qo'shilgan o'yinlar hisobi (score/minute) avtomatik yangilanadi

Ulash: tvbot.py ga PATCH.md dagi 4 qatorni qo'shing.
.env: FOOTBALL_DATA_KEY=<football-data.org dan bepul token>
"""
import asyncio
import logging
import os
import time
from datetime import datetime, timedelta
from html import escape

from aiohttp import ClientSession, ClientTimeout, web
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

log = logging.getLogger("fixtures")

API_KEY = os.getenv("FOOTBALL_DATA_KEY", "").strip()
BASE = "https://api.football-data.org/v4"
COMPS = {
    "PL": "Premier League", "PD": "La Liga", "SA": "Serie A",
    "BL1": "Bundesliga", "FL1": "Ligue 1", "CL": "Champions League",
}
LIVE_ST = {"IN_PLAY", "PAUSED", "LIVE"}
SHOW_SCORE = LIVE_ST | {"FINISHED"}

m = None          # tvbot moduli (init() da beriladi)
FX_FILE = ""
_state = {"error": "", "scorers_at": 0.0}


class FxState(StatesGroup):
    url = State()


# ───────────── saqlash ─────────────
def load_fx():
    d = m.load_json(FX_FILE, {})
    d.setdefault("matches", [])
    d.setdefault("scorers", {})
    return d


def save_fx(d):
    m.save_json(FX_FILE, d)


def _dt(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def _label_time(x):
    t = _dt(x["time"]).astimezone(m.TZ_UZ)
    today = datetime.now(m.TZ_UZ).date()
    return t.strftime("%H:%M") if t.date() == today else t.strftime("%d.%m %H:%M")


# ───────────── API ─────────────
async def _get(session, path, params=None):
    async with session.get(BASE + path, params=params, headers={"X-Auth-Token": API_KEY}) as r:
        if r.status == 429:
            raise RuntimeError("API limiti (429), keyinroq urinib ko'ring")
        if r.status in (401, 403):
            raise RuntimeError(f"API kaliti noto'g'ri yoki ruxsat yo'q ({r.status})")
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}")
        return await r.json()


async def sync_matches():
    """Bugun va ertangi o'yinlarni oladi. Qaytaradi: o'yinlar soni."""
    if not API_KEY:
        raise RuntimeError("FOOTBALL_DATA_KEY berilmagan")
    today = datetime.now(m.TZ_UZ).date()
    params = {"dateFrom": (today - timedelta(days=1)).isoformat(),
              "dateTo": (today + timedelta(days=2)).isoformat()}
    async with ClientSession(timeout=ClientTimeout(total=20)) as s:
        data = await _get(s, "/matches", params)
    out = []
    for x in data.get("matches", []):
        try:
            local = _dt(x["utcDate"]).astimezone(m.TZ_UZ)
        except (KeyError, ValueError):
            continue
        if local.date() not in (today, today + timedelta(days=1)):
            continue
        st = x.get("status", "")
        ft = (x.get("score") or {}).get("fullTime") or {}
        h, a = ft.get("home"), ft.get("away")
        score = f"{h}:{a}" if st in SHOW_SCORE and h is not None and a is not None else ""
        minute = str(x.get("minute") or "") or ("HT" if st == "PAUSED" else "")
        comp = x.get("competition") or {}
        ht, at = x.get("homeTeam") or {}, x.get("awayTeam") or {}
        out.append({
            "ext_id": x["id"],
            "home": ht.get("shortName") or ht.get("name") or "?",
            "away": at.get("shortName") or at.get("name") or "?",
            "home_logo": ht.get("crest") or "", "away_logo": at.get("crest") or "",
            "league": comp.get("name") or "", "league_code": comp.get("code") or "",
            "time": local.isoformat(), "status": st, "score": score, "minute": minute,
        })
    out.sort(key=lambda z: z["time"])
    d = load_fx()
    d["matches"] = out
    d["updated"] = m.utcnow().isoformat()
    save_fx(d)
    _state["error"] = ""
    _apply_to_items(out)
    return len(out)


def _apply_to_items(matches):
    """Qo'shilgan o'yinlar (ext_id) hisobi, vaqti va holatini yangilaydi; avto-qo'shish."""
    items = m.load_items()
    by = {x["ext_id"]: x for x in matches}
    changed = False
    for it in items:
        fx = by.get(it.get("ext_id"))
        if not fx:
            continue
        for k_item, val in (("score", fx["score"]), ("minute", fx["minute"])):
            if it.get(k_item, "") != val:
                it[k_item] = val
                changed = True
        if it.get("match_time") != fx["time"]:
            it["match_time"] = fx["time"]
            it.pop("reminded", None)
            changed = True
    if changed:
        m.save_items(items)
    if m.load_settings().get("fixtures_auto"):
        for fx in matches:
            if fx["league_code"] in COMPS and fx["status"] not in ("FINISHED", "CANCELLED", "POSTPONED"):
                create_item(fx)


async def sync_scorers():
    if not API_KEY:
        return
    d = load_fx()
    async with ClientSession(timeout=ClientTimeout(total=20)) as s:
        for code in COMPS:
            try:
                data = await _get(s, f"/competitions/{code}/scorers", {"limit": 10})
                d["scorers"][code] = [{
                    "name": (r.get("player") or {}).get("name", "?"),
                    "team": (r.get("team") or {}).get("shortName") or (r.get("team") or {}).get("name", ""),
                    "crest": (r.get("team") or {}).get("crest", ""),
                    "goals": r.get("goals") or 0, "assists": r.get("assists") or 0,
                    "played": r.get("playedMatches") or 0,
                } for r in data.get("scorers", [])]
            except Exception as e:
                log.warning("scorers %s: %s", code, e)
            await asyncio.sleep(7)  # bepul limit: daqiqasiga 10 so'rov
    d["scorers_at"] = m.utcnow().isoformat()
    save_fx(d)
    _state["scorers_at"] = time.time()


def create_item(fx, url="", premium=False):
    """O'yinni kontentga qo'shadi (ext_id bo'yicha takrorlanmaydi). -> (id, yangi_mi)"""
    items = m.load_items()
    for i in items:
        if i.get("ext_id") == fx["ext_id"]:
            if url:
                i["url"] = url
                m.save_items(items)
            return i["id"], False
    nid = max([x.get("id", 0) for x in items], default=0) + 1
    items.append({
        "id": nid, "title": f"{fx['home']} vs {fx['away']}", "content_type": "football",
        "language": "uz", "genre": fx["league"] or "Futbol", "badge": m.BADGES["football"],
        "url": url, "poster": "", "premium": premium, "views": 0, "added": m.utcnow().isoformat(),
        "home": fx["home"], "away": fx["away"], "home_logo": fx["home_logo"], "away_logo": fx["away_logo"],
        "league": fx["league"], "match_time": fx["time"], "score": fx["score"], "minute": fx["minute"],
        "ext_id": fx["ext_id"],
    })
    m.save_items(items)
    return nid, True


def _find(ext_id):
    return next((x for x in load_fx()["matches"] if x["ext_id"] == ext_id), None)


def _linked(ext_id):
    return next((i for i in m.load_items() if i.get("ext_id") == ext_id), None)


# ───────────── fon vazifa ─────────────
async def loop():
    await asyncio.sleep(20)
    while True:
        wait = 900
        try:
            if API_KEY:
                await sync_matches()
                now = m.utcnow()
                for x in load_fx()["matches"]:
                    if timedelta(hours=-3) < _dt(x["time"]) - now < timedelta(minutes=15):
                        wait = 120  # o'yin vaqtida tez-tez yangilanadi
                        break
                if time.time() - _state["scorers_at"] > 6 * 3600:
                    await sync_scorers()
        except Exception as e:
            _state["error"] = str(e)[:150]
            log.warning("fixtures loop: %s", e)
        await asyncio.sleep(wait)


# ───────────── Mini App API ─────────────
async def api_fixtures(request):
    user = m._auth(request, allow_maintenance=True)
    if m.maintenance_info()["on"] and not m.is_admin(user["id"]):
        return web.json_response({"matches": [], "scorers": {}})
    d = load_fx()
    linked = {i.get("ext_id"): i["id"] for i in m.load_items() if i.get("ext_id") and not i.get("hidden")}
    ms = [dict(x, item_id=linked.get(x["ext_id"])) for x in d["matches"]]
    return web.json_response({"matches": ms, "scorers": d["scorers"], "comps": COMPS, "updated": d.get("updated")})


def add_routes(app):
    app.router.add_route("*", "/api/fixtures", api_fixtures)


# ───────────── Admin panel ─────────────
def init(module):
    global m, FX_FILE
    m = module
    FX_FILE = os.path.join(m.BASE_DIR, "fixtures.json")
    m.BACKUP_FILES.append(FX_FILE)
    m.TOGGLES["fixtures_auto"] = "📅 Kunlik o'yinlarni avto-qo'shish"

    # admin bosh menyusiga tugma qo'shish
    orig_kb = m.admin_home_kb

    def new_kb():
        mk = orig_kb()
        mk.inline_keyboard.insert(1, [m.b("📅 Kunlik o'yinlar / Top futbolchilar", "fx:menu")])
        return mk
    m.admin_home_kb = new_kb

    r, b, kb, show = m.admin_r, m.b, m.kb, m.show

    @r.callback_query(F.data == "fx:menu")
    async def fx_menu(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        d = load_fx()
        upd = m.fmt_dt(m.parse_dt(d["updated"])) if d.get("updated") else "—"
        linked = sum(1 for x in d["matches"] if _linked(x["ext_id"]))
        text = ("📅 <b>Kunlik o'yinlar</b>\n\n"
                f"🔑 API: {'✅ ulangan' if API_KEY else '❌ FOOTBALL_DATA_KEY yo`q'}\n"
                f"⚽ Jadvalda: <b>{len(d['matches'])}</b> o'yin · kontentda: <b>{linked}</b>\n"
                f"🕒 Yangilangan: {upd}\n"
                f"🤖 Avto-qo'shish: {'✅' if m.load_settings().get('fixtures_auto') else '❌'}"
                + (f"\n⚠️ Xato: {escape(_state['error'])}" if _state["error"] else ""))
        await show(cb, text, kb(
            [b("📋 Bugungi/ertangi o'yinlar", "fx:list:0")],
            [b("🔄 Hozir yangilash", "fx:sync"), b("➕ Hammasini qo'shish", "fx:all")],
            [b("⭐ Top futbolchilar", "fx:top:PL")],
            [b("🤖 Avto-qo'shishni yoq/o'chir", "set:tg:fixtures_auto")],
            [b("⬅️ Admin panel", "admin")]))

    @r.callback_query(F.data == "fx:sync")
    async def fx_sync(cb: CallbackQuery, state: FSMContext):
        await cb.answer("Yangilanmoqda…")
        try:
            n = await sync_matches()
            await sync_scorers_once_if_empty()
            await cb.message.answer(f"✅ {n} ta o'yin yangilandi.", reply_markup=kb([b("📅 Kunlik o'yinlar", "fx:menu")]))
        except Exception as e:
            _state["error"] = str(e)[:150]
            await cb.message.answer(f"❌ {escape(str(e))}", reply_markup=kb([b("📅 Kunlik o'yinlar", "fx:menu")]))

    async def sync_scorers_once_if_empty():
        if not load_fx()["scorers"]:
            t = asyncio.create_task(sync_scorers())
            m._bg_tasks.add(t)
            t.add_done_callback(m._bg_tasks.discard)

    @r.callback_query(F.data.startswith("fx:list:"))
    async def fx_list(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        ms = load_fx()["matches"]
        if not ms:
            await show(cb, "📭 Jadval bo'sh. «Hozir yangilash» ni bosing.", kb([b("🔄 Yangilash", "fx:sync")], [b("⬅️ Orqaga", "fx:menu")]))
            return
        chunk, page, pages = m.paginate(ms, int(cb.data.split(":")[2]), per=8)
        rows = [[b(f"{'✅' if _linked(x['ext_id']) else '➕'} {_label_time(x)} {x['home']}–{x['away']}"[:60], f"fx:m:{x['ext_id']}")]
                for x in chunk]
        rows.append(m.nav_row("fx:list", page, pages))
        rows.append([b("⬅️ Orqaga", "fx:menu")])
        await show(cb, f"📋 <b>O'yinlar</b> ({len(ms)} ta)\n✅ kontentda · ➕ qo'shilmagan",
                   InlineKeyboardMarkup(inline_keyboard=rows))

    @r.callback_query(F.data.startswith("fx:m:"))
    async def fx_match(cb: CallbackQuery, state: FSMContext = None):
        if state:
            await state.clear()
        ext = int(cb.data.split(":")[2])
        x = _find(ext)
        if not x:
            await cb.answer("Topilmadi", show_alert=True)
            return
        it = _linked(ext)
        text = (f"⚽ <b>{escape(x['home'])} — {escape(x['away'])}</b>\n🏆 {escape(x['league'])}\n"
                f"⏰ {_label_time(x)} · {x['status']}"
                + (f"\n📊 {x['score']}" if x["score"] else "")
                + (f"\n\n✅ Kontentda (ID {it['id']}) · havola: {'bor' if it.get('url') else '<b>yo`q</b>'}" if it else "\n\n➕ Hali kontentga qo'shilmagan"))
        rows = [[b("🔗 Havola yuborish" if it else "🔗 Havola bilan qo'shish", f"fx:u:{ext}")]]
        if not it:
            rows.append([b("➕ Havolasiz qo'shish", f"fx:add:{ext}")])
        else:
            rows.append([b("📄 Kontent kartasi", f"itm:{it['id']}")])
        rows.append([b("⬅️ Ro'yxat", "fx:list:0")])
        await show(cb, text, InlineKeyboardMarkup(inline_keyboard=rows))

    @r.callback_query(F.data.startswith("fx:add:"))
    async def fx_add(cb: CallbackQuery):
        x = _find(int(cb.data.split(":")[2]))
        if not x:
            await cb.answer("Topilmadi", show_alert=True)
            return
        create_item(x)
        await cb.answer("✅ Qo'shildi")
        await fx_match_refresh(cb, x["ext_id"])

    async def fx_match_refresh(cb, ext):
        cb.data = f"fx:m:{ext}"
        await fx_match(cb, None)

    @r.callback_query(F.data.startswith("fx:u:"))
    async def fx_url_ask(cb: CallbackQuery, state: FSMContext):
        ext = int(cb.data.split(":")[2])
        await state.set_state(FxState.url)
        await state.update_data(ext=ext)
        await show(cb, "🔗 Translyatsiya havolasini yuboring (m3u8 / mp4 / YouTube). Bekor: /cancel")

    @r.message(FxState.url, m.TEXT)
    async def fx_url_save(message: Message, state: FSMContext):
        url = message.text.strip()
        if not url.lower().startswith(("http://", "https://")):
            await message.answer("❌ Havola http(s):// bilan boshlanishi kerak. Qayta yuboring:")
            return
        ext = (await state.get_data()).get("ext")
        x = _find(ext)
        await state.clear()
        if not x:
            await message.answer("❌ O'yin topilmadi.", reply_markup=kb([b("📅 Kunlik o'yinlar", "fx:menu")]))
            return
        iid, new = create_item(x, url=url)
        await message.answer(f"✅ {'Qo`shildi' if new else 'Havola yangilandi'}: {escape(x['home'])} — {escape(x['away'])} (ID {iid})\n"
                             "Mini App'da darhol ko'rinadi.",
                             reply_markup=kb([b("📋 O'yinlar", "fx:list:0")], [b("📄 Kontent kartasi", f"itm:{iid}")]))

    @r.callback_query(F.data == "fx:all")
    async def fx_all(cb: CallbackQuery):
        n = 0
        for x in load_fx()["matches"]:
            if x["status"] in ("FINISHED", "CANCELLED", "POSTPONED"):
                continue
            if create_item(x)[1]:
                n += 1
        await cb.answer(f"✅ {n} ta o'yin qo'shildi", show_alert=True)

    @r.callback_query(F.data.startswith("fx:top:"))
    async def fx_top(cb: CallbackQuery):
        code = cb.data.split(":")[2]
        sc = load_fx()["scorers"].get(code)
        tabs = [b(("• " if c == code else "") + c, f"fx:top:{c}") for c in COMPS]
        if not sc:
            await show(cb, "📭 Ma'lumot hali yuklanmagan. «Hozir yangilash» ni bosing (bir necha daqiqa ketadi).",
                       kb(tabs[:3], tabs[3:], [b("🔄 Yangilash", "fx:sync")], [b("⬅️ Orqaga", "fx:menu")]))
            return
        lines = [f"{n}. <b>{escape(p['name'])}</b> ({escape(p['team'])}) — ⚽ {p['goals']} · 🅰️ {p['assists']}"
                 for n, p in enumerate(sc[:10], 1)]
        await show(cb, f"⭐ <b>{COMPS[code]} — top futbolchilar</b>\n\n" + "\n".join(lines),
                   kb(tabs[:3], tabs[3:], [b("⬅️ Orqaga", "fx:menu")]))
