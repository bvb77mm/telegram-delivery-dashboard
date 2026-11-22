import logging
import os
import threading
import time

import requests

import db

log = logging.getLogger("bot")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
API = f"https://api.telegram.org/bot{TOKEN}"

HELP = (
    "ربات تحویل\n"
    "/new مشتری | آدرس - ثبت سریع (یا دکمه تحویل جدید)\n"
    "/list - آخرین تحویل‌ها\n"
    "/status شناسه - نمایش یک تحویل\n"
    "/set شناسه pending|in_transit|delivered|cancelled - تغییر وضعیت"
)


def fmt(r):
    return f"{ICON[r['status']]} #{r['id']} {r['customer']} - {r['address']}\nوضعیت: {db.LABELS[r['status']]}"


ICON = {"pending": "🕓", "in_transit": "🚚", "delivered": "✅", "cancelled": "❌"}
BTN_NEW, BTN_LIST, BTN_STATS, BTN_CANCEL = "➕ تحویل جدید", "📋 لیست تحویل‌ها", "📊 آمار", "↩️ انصراف"
MENU = {"keyboard": [[{"text": BTN_NEW}, {"text": BTN_LIST}], [{"text": BTN_STATS}]], "resize_keyboard": True}
CANCEL_KB = {"keyboard": [[{"text": BTN_CANCEL}]], "resize_keyboard": True}
STATE = {}  # chat_id -> {"step": "customer"|"address", "customer": str}


def inline(r):
    btns = [
        {"text": f"{ICON[s]} {db.LABELS[s]}", "callback_data": f"s:{r['id']}:{s}"}
        for s in db.STATUSES
        if s != r["status"]
    ]
    return {"inline_keyboard": [btns[:2], btns[2:]]}


def send(chat, text, markup=None):
    body = {"chat_id": chat, "text": text}
    if markup:
        body["reply_markup"] = markup
    requests.post(f"{API}/sendMessage", json=body, timeout=10)


def show(chat, r):
    send(chat, fmt(r), inline(r))


def on_message(m):
    chat, text = m["chat"]["id"], m["text"].strip()
    st = STATE.get(chat)
    if text == BTN_CANCEL:
        STATE.pop(chat, None)
        return send(chat, "لغو شد.", MENU)
    if st and not text.startswith("/"):
        if st["step"] == "customer":
            STATE[chat] = {"step": "address", "customer": text}
            return send(chat, "آدرس تحویل را بفرستید:", CANCEL_KB)
        STATE.pop(chat)
        did = db.add(st["customer"], text)
        send(chat, f"تحویل #{did} ثبت شد.", MENU)
        return show(chat, db.get(did))
    if text in (BTN_NEW, "/new"):
        STATE[chat] = {"step": "customer"}
        return send(chat, "نام مشتری را بفرستید:", CANCEL_KB)
    if text in (BTN_LIST, "/list"):
        rows = db.list_all(5)
        if not rows:
            return send(chat, "هنوز تحویلی ثبت نشده", MENU)
        send(chat, "آخرین تحویل‌ها:", MENU)
        for r in reversed(rows):
            show(chat, r)
        return
    if text in (BTN_STATS, "/stats"):
        c = db.counts()
        return send(chat, "\n".join(f"{ICON[s]} {db.LABELS[s]}: {c[s]}" for s in db.STATUSES), MENU)
    parts = text.split(None, 1)
    if parts[0].split("@")[0].lower() == "/status" and len(parts) > 1 and parts[1].strip().isdigit():
        r = db.get(int(parts[1]))
        return show(chat, r) if r else send(chat, "پیدا نشد", MENU)
    reply = handle(text)
    if reply:
        send(chat, reply, MENU)


def on_callback(q):
    try:
        _, did, status = q["data"].split(":")
        did = int(did)
    except ValueError:
        return
    ok = db.set_status(did, status)
    requests.post(
        f"{API}/answerCallbackQuery",
        json={"callback_query_id": q["id"], "text": "بروزرسانی شد" if ok else "پیدا نشد"},
        timeout=10,
    )
    r = db.get(did)
    msg = q.get("message")
    if ok and r and msg:
        requests.post(
            f"{API}/editMessageText",
            json={
                "chat_id": msg["chat"]["id"],
                "message_id": msg["message_id"],
                "text": fmt(r),
                "reply_markup": inline(r),
            },
            timeout=10,
        )


def handle(text):
    parts = text.strip().split(None, 1)
    if not parts:
        return None
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/new":
        if "|" not in arg:
            return "استفاده: /new مشتری | آدرس"
        cust, addr = [s.strip() for s in arg.split("|", 1)]
        if not cust or not addr:
            return "استفاده: /new مشتری | آدرس"
        return f"تحویل #{db.add(cust, addr)} ثبت شد"
    if cmd == "/list":
        rows = db.list_all(10)
        return "\n".join(fmt(r) for r in rows) or "هنوز تحویلی ثبت نشده"
    if cmd == "/status":
        if not arg.isdigit():
            return "استفاده: /status شناسه"
        r = db.get(int(arg))
        return fmt(r) if r else "پیدا نشد"
    if cmd == "/set":
        p = arg.split()
        if len(p) != 2 or not p[0].isdigit():
            return "استفاده: /set شناسه وضعیت"
        if p[1] not in db.STATUSES:
            return "وضعیت باید یکی از این‌ها باشد: " + ", ".join(db.STATUSES)
        return "بروزرسانی شد" if db.set_status(int(p[0]), p[1]) else "پیدا نشد"
    return "دستور نامعتبر. /help را بفرستید"


def run():
    offset = 0
    while True:
        try:
            r = requests.get(
                f"{API}/getUpdates",
                params={"timeout": 30, "offset": offset, "allowed_updates": '["message","callback_query"]'},
                timeout=40,
            )
            for u in r.json().get("result", []):
                offset = u["update_id"] + 1
                try:
                    if "callback_query" in u:
                        on_callback(u["callback_query"])
                    elif "text" in (u.get("message") or {}):
                        on_message(u["message"])
                except Exception as e:
                    log.warning("update error: %s", e)
        except Exception as e:  # keep polling on any error
            log.warning("poll error: %s", e)
            time.sleep(5)


def start():
    if not TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN not set; bot disabled, dashboard only")
        return
    threading.Thread(target=run, daemon=True).start()
