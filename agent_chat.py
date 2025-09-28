# -*- coding: utf-8 -*-

"""Activation:
    source ~/tg/bin/activate    # your venv
    cd /mnt/c/Tmp/
    if ~/.bashrc does not contain the secrest -
        export OPENAI_API_KEY="sk-proj..."
        export TELEGRAM_API_ID=...
        export TELEGRAM_API_HASH="..."
    python agent_chat.py
"""

import os, sqlite3, re, asyncio, atexit
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telethon import TelegramClient, events
from openai import OpenAI

client = None
OPENAI_MODEL = None
SOURCE = None
AUTHOR_USERNAME = None
AUTHOR_SIGNATURE = None
TZ = None
START = None
END = None
oai = None
db = None

# ========================
#
#
def need(name):
    v = os.getenv(name)
    if not v: raise SystemExit(f"Missing env var: {name}")
    return v

# ========================
#
#
def _parse_dt_env(name: str, tz):
    s = (os.getenv(name) or "").strip()
    print(f"[ZZZZZZZZZ] name:{s}, tz:{tz}")

    if not s:
        return None
    try:
        # ISO formats supported:
        #   "2025-09-10" or "2025-09-10T08:30:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)   # assume your TZ if no tzinfo provided
        else:
            dt = dt.astimezone(tz)
        return dt
    except Exception:
        return None

# ========================
#
#
def _compute_window(tz):
    start = _parse_dt_env("TG_START", tz)
    end   = _parse_dt_env("TG_END", tz)
    if start and end:
        return start, end
    now = datetime.now(tz)
    end = end or now
    start = start or (end - timedelta(days=7))
    return start, end
    
# ========================
#
#
def setup_env():
    global client, OPENAI_MODEL, SOURCE, AUTHOR_USERNAME,AUTHOR_SIGNATURE,TZ,START,END, oai, db
    API_ID   = int(need("TELEGRAM_API_ID"))        # from env
    API_HASH = need("TELEGRAM_API_HASH")           # from env
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # optional

    SOURCE   = os.getenv("SOURCE", "@menathimtguvot")
    AUTHOR_USERNAME  = os.getenv("AUTHOR_USERNAME", "@adam100001")          # e.g. "@user" (groups)

    TZ = ZoneInfo("Asia/Jerusalem")
    AUTHOR_SIGNATURE = ""          # e.g. "שם בחתימה" (channels)
    #START = datetime(2025, 9, 10, 0, 0, tzinfo=TZ)
    #END   = datetime(2025, 9, 30, 23, 59, 59, tzinfo=TZ)
    START, END = _compute_window(TZ)
    print(f"[TIME WINDOW] start:{START}, end:{END}")
    
    client = TelegramClient("session", API_ID, API_HASH)
    oai = OpenAI()  # reads OPENAI_API_KEY

    db = sqlite3.connect("tg.db")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS posts(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      source TEXT, author TEXT, mid INTEGER, ts INTEGER, link TEXT, text TEXT,
      UNIQUE(source, mid))""")
    db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(
      text, source, author, tokenize = "unicode61 remove_diacritics 2")""")
    db.commit()
    
    # --- diagnostics for /ask corpus ---
    cur = db.cursor()
    try:
        cnt = cur.execute("SELECT COUNT(*) FROM posts_fts").fetchone()[0]
        print(f"[DB] posts_fts rows: {cnt}")
        for dt, link, snippet in db.execute(
        """
        SELECT datetime(ts,'unixepoch','localtime') AS dt, link, substr(text,1,160)
        FROM posts
        ORDER BY ts DESC
        LIMIT 5
        """
        ):
            print(f"[DB] {dt} | {link} | {snippet}")
    except Exception as _e:
        print(f"[DB] diagnostics error: {_e}")
    
# ========================
#
#
def norm_he(s): 
    if not s: return ""
    s = re.sub(r"[\u0591-\u05C7\u200e\u200f]", "", s)
    return " ".join(s.split()).strip()

# ========================
#
#
def in_range(dt):
    if dt.tzinfo is None: dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return START <= dt.astimezone(TZ) <= END

# ========================
#
#
def permalink(source, mid): return f"https://t.me/{source.lstrip('@')}/{mid}"

# ========================
#
#
async def scan():
    global db
    entity = await client.get_entity(SOURCE)
    author_obj = await client.get_entity(AUTHOR_USERNAME) if AUTHOR_USERNAME else None
    added = 0
    async for m in client.iter_messages(entity, reverse=True):
        if not in_range(m.date): continue
        if author_obj and m.sender_id != getattr(author_obj, "id", None): continue
        if AUTHOR_SIGNATURE:
            sig = norm_he(getattr(m, "post_author", "")).lower()
            if sig != norm_he(AUTHOR_SIGNATURE).lower(): continue
        txt = norm_he(m.message or m.raw_text or "")
        if not txt: continue
        link = permalink(SOURCE, m.id)
        ts = int(m.date.astimezone(TZ).timestamp())
        try:
            cur = db.cursor()
            cur.execute("INSERT OR IGNORE INTO posts(source,author,mid,ts,link,text) VALUES(?,?,?,?,?,?)",
                        (SOURCE, AUTHOR_USERNAME or AUTHOR_SIGNATURE or "", m.id, ts, link, txt))
            if cur.rowcount:
                pid = cur.lastrowid
                cur.execute("INSERT INTO posts_fts(rowid,text,source,author) VALUES(?,?,?,?)",
                            (pid, txt, SOURCE, AUTHOR_USERNAME or AUTHOR_SIGNATURE or ""))
                added += 1
            db.commit()
        except Exception:
            db.rollback()
    cnt = cur.execute("SELECT COUNT(*) FROM posts_fts").fetchone()[0]
    return added, cnt

# ========================
#
#
def search(q, k=8):
    global db
    q = norm_he(q)
    rows = db.execute("""
      SELECT p.text, p.ts, p.link
      FROM posts_fts f JOIN posts p ON p.id=f.rowid
      WHERE posts_fts MATCH ? ORDER BY bm25(posts_fts) ASC, p.ts DESC LIMIT ?""",
      (q, k)).fetchall()
    return [{"text": r[0],
             "date_str": datetime.fromtimestamp(r[1], TZ).strftime("%Y-%m-%d"),
             "link": r[2]} for r in rows]

# ========================
#
#
def window_items(limit: int = 30):
    start_ts = int(START.timestamp())
    end_ts   = int(END.timestamp())
    rows = db.execute("""
      SELECT text, ts, link
      FROM posts
      WHERE ts BETWEEN ? AND ?
      ORDER BY ts DESC
      LIMIT ?
    """, (start_ts, end_ts, limit)).fetchall()
    return [{
        "text": norm_he(r[0] or ""),
        "date_str": datetime.fromtimestamp(r[1], TZ).strftime("%Y-%m-%d"),
        "link": r[2],
    } for r in rows]

# ========================
#
#
def summarize_he(items, source, start_dt, end_dt):
    global oai
    if not items:
        return "אין מספיק נתונים לענות. נסה להרחיב את חלון התאריכים או את מילות החיפוש."
    ev = "\n".join(f"- {it['date_str']} — {it['text'][:180]} [{it['link']}]" for it in items[:12])
    prompt = f"""סכם בעברית, קצר ולעניין בלבד.
מקור: {source}, חלון: {start_dt.date()}–{end_dt.date()}.
מטרה: לענות על השאילתה בהסתמך רק על הראיות.
פורמט: 1) 2–3 שורות תקציר. 2) 3–5 נקודות מפתח. אין להמציא.
ראיות:
{ev}
"""
    r = oai.responses.create(model=OPENAI_MODEL, input=prompt)
    return r.output[0].content[0].text.strip()

setup_env()

# ========================
#
#
@client.on(events.NewMessage(chats="me", pattern=r"^/scan$"))
async def cmd_scan(e):
    added, tot = await scan()
    await e.reply(f"נסרק. נוספו {added} / {tot} פריטים. חלון: {START.date()}–{END.date()}.")

# ========================
#
#
@client.on(events.NewMessage(chats="me", pattern=r"^/ask\s+(.+)$"))
async def cmd_ask(e):
    q = e.pattern_match.group(1)
    hits = search(q, k=8)

    if not hits:
        # fallback: summarize the whole window
        hits = window_items(limit=300)
        if not hits:
            await e.reply("אין פוסטים בחלון הזמן הנוכחי.")
            return

    summary = summarize_he(hits, SOURCE, START, END)
    links = "\n".join(f"• {it['date_str']} — {it['link']}" for it in hits[:5])
    await e.reply(summary + ("\n\n— מקורות —\n" + links if links else ""))

# ========================
#
#
async def main():
    await client.start()
    print("Ready. In Saved Messages send:\n/scan\n/ask <שאלה בעברית>")
    await client.run_until_disconnected()

asyncio.run(main())