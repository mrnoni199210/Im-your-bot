import os, requests, telebot, time, threading, random, psycopg2, json
from flask import Flask, request, jsonify, send_from_directory
from datetime import datetime, timezone, timedelta
from apscheduler.schedulers.background import BackgroundScheduler

# ── ENV ──
TOKEN          = os.environ.get("BOT_TOKEN")
GROQ_API_KEY   = os.environ.get("GROQ_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
WEBHOOK_URL    = os.environ.get("WEBHOOK_URL")
DATABASE_URL   = os.environ.get("DATABASE_URL")
MAX_CHARS      = 3   # max characters per user

bot = telebot.TeleBot(TOKEN, threaded=False)
app = Flask(__name__, static_folder='static')

# ── DB ──
def get_conn():
    conn = psycopg2.connect(DATABASE_URL.split('?')[0], sslmode='require', connect_timeout=10)
    conn.autocommit = True
    return conn

def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS characters (
            id SERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            slot INTEGER NOT NULL CHECK(slot BETWEEN 1 AND 3),
            name TEXT NOT NULL,
            gender TEXT NOT NULL,
            age INTEGER NOT NULL,
            appearance TEXT,
            characteristics TEXT,
            relationship TEXT,
            scene TEXT,
            description TEXT,
            nickname TEXT DEFAULT '',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(user_id, slot)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS chat_history (
            id SERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            char_slot INTEGER NOT NULL DEFAULT 1,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            ts TIMESTAMPTZ DEFAULT NOW()
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_state (
            user_id TEXT PRIMARY KEY,
            active_slot INTEGER DEFAULT 1,
            setup_step TEXT DEFAULT NULL,
            setup_data JSONB DEFAULT '{}'::jsonb,
            last_seen TIMESTAMPTZ DEFAULT NOW(),
            total_msgs INTEGER DEFAULT 0
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS scheduled_msgs (
            id SERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            char_slot INTEGER NOT NULL DEFAULT 1,
            send_at TIMESTAMPTZ NOT NULL,
            done BOOLEAN DEFAULT FALSE
        )
    ''')
    c.close(); conn.close()
    print("DB ready.")

init_db()

# ── TIME ──
IST = timedelta(hours=5, minutes=30)
def now_ist(): return datetime.now(timezone.utc) + IST
def to_ist(dt):
    if dt is None: return None
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return dt + IST

# ── STATE HELPERS ──
def get_state(uid):
    conn = get_conn(); c = conn.cursor()
    c.execute('SELECT active_slot, setup_step, setup_data, last_seen, total_msgs FROM user_state WHERE user_id=%s', (uid,))
    row = c.fetchone(); c.close(); conn.close()
    if not row:
        return {'active_slot':1,'setup_step':None,'setup_data':{},'last_seen':None,'total_msgs':0}
    return {'active_slot':row[0],'setup_step':row[1],'setup_data':row[2] or {},'last_seen':row[3],'total_msgs':row[4]}

def set_state(uid, **kwargs):
    for k, v in kwargs.items():
        if isinstance(v, dict):
            kwargs[k] = json.dumps(v)
    conn = get_conn(); c = conn.cursor()
    c.execute('SELECT user_id FROM user_state WHERE user_id=%s', (uid,))
    exists = c.fetchone()
    if exists:
        sets = ', '.join(f"{k}=%s" for k in kwargs)
        c.execute(f'UPDATE user_state SET {sets} WHERE user_id=%s', (*kwargs.values(), uid))
    else:
        kwargs['user_id'] = uid
        cols = ', '.join(kwargs.keys())
        vals = ', '.join(['%s']*len(kwargs))
        c.execute(f'INSERT INTO user_state ({cols}) VALUES ({vals})', tuple(kwargs.values()))
    c.close(); conn.close()

def bump_msgs(uid):
    conn = get_conn(); c = conn.cursor()
    c.execute('''
        INSERT INTO user_state (user_id, total_msgs, last_seen)
        VALUES (%s, 1, NOW())
        ON CONFLICT(user_id) DO UPDATE
        SET total_msgs = user_state.total_msgs + 1, last_seen = NOW()
    ''', (uid,))
    c.close(); conn.close()

# ── CHARACTER CRUD ──
def get_char(uid, slot):
    conn = get_conn(); c = conn.cursor()
    c.execute('SELECT name,gender,age,appearance,characteristics,relationship,scene,description,nickname FROM characters WHERE user_id=%s AND slot=%s', (uid, slot))
    row = c.fetchone(); c.close(); conn.close()
    if not row: return None
    keys = ['name','gender','age','appearance','characteristics','relationship','scene','description','nickname']
    return dict(zip(keys, row))

def save_char(uid, slot, data):
    conn = get_conn(); c = conn.cursor()
    c.execute('''
        INSERT INTO characters (user_id, slot, name, gender, age, appearance, characteristics, relationship, scene, description, nickname)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT(user_id, slot) DO UPDATE SET
        name=EXCLUDED.name, gender=EXCLUDED.gender, age=EXCLUDED.age,
        appearance=EXCLUDED.appearance, characteristics=EXCLUDED.characteristics,
        relationship=EXCLUDED.relationship, scene=EXCLUDED.scene,
        description=EXCLUDED.description, nickname=EXCLUDED.nickname
    ''', (uid, slot, data['name'], data['gender'], data['age'],
          data.get('appearance',''), data.get('characteristics',''),
          data.get('relationship',''), data.get('scene',''),
          data.get('description',''), data.get('nickname','')))
    c.close(); conn.close()

def count_chars(uid):
    conn = get_conn(); c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM characters WHERE user_id=%s', (uid,))
    n = c.fetchone()[0]; c.close(); conn.close()
    return n

def list_chars(uid):
    conn = get_conn(); c = conn.cursor()
    c.execute('SELECT slot, name, gender, age FROM characters WHERE user_id=%s ORDER BY slot', (uid,))
    rows = c.fetchall(); c.close(); conn.close()
    return rows

def delete_char(uid, slot):
    conn = get_conn(); c = conn.cursor()
    c.execute('DELETE FROM characters WHERE user_id=%s AND slot=%s', (uid, slot))
    c.execute('DELETE FROM chat_history WHERE user_id=%s AND char_slot=%s', (uid, slot))
    c.close(); conn.close()

# ── CHAT HISTORY ──
def save_msg(uid, slot, role, content):
    conn = get_conn(); c = conn.cursor()
    c.execute('INSERT INTO chat_history (user_id, char_slot, role, content) VALUES (%s,%s,%s,%s)', (uid, slot, role, content))
    c.close(); conn.close()

def get_history(uid, slot, limit=24):
    conn = get_conn(); c = conn.cursor()
    c.execute('''
        SELECT role, content FROM (
            SELECT role, content, ts FROM chat_history
            WHERE user_id=%s AND char_slot=%s
            ORDER BY id DESC LIMIT %s
        ) sub ORDER BY ts ASC
    ''', (uid, slot, limit))
    rows = c.fetchall(); c.close(); conn.close()
    return [{"role": r[0], "content": r[1]} for r in rows]

def clear_history(uid, slot):
    conn = get_conn(); c = conn.cursor()
    c.execute('DELETE FROM chat_history WHERE user_id=%s AND char_slot=%s', (uid, slot))
    c.close(); conn.close()

# ── SYSTEM PROMPT BUILDER ──
def build_system(char, uid, image_context=""):
    state = get_state(uid)
    now = now_ist()

    last = to_ist(state['last_seen'])
    gap = ""
    if last:
        diff = now - last
        d, h, m = diff.days, diff.seconds//3600, (diff.seconds%3600)//60
        if d == 0 and h == 0 and m < 5: gap = "User abhi bhi baat kar raha hai."
        elif d == 0 and h == 0: gap = f"User ne {m} min pehle message kiya."
        elif d == 0: gap = f"User ne {h} ghante pehle message kiya."
        elif d == 1: gap = "User kal aaya tha."
        elif d < 7: gap = f"User {d} din baad aaya."
        else: gap = f"User {d} din baad aaya — kaafi lamba waqt!"

    is_male = char['gender'] == 'male'
    age = char['age']

    if is_male:
        gender_word = "ladka"
        self_word = "mard"
        gender_forms = "gaya, tha, aaya, soya, raha, bore ho raha tha, aa gaya, chala gaya, ruka, samjha, karta, rehta, laata, hota"
        gender_banned = "gayi, thi, aayi, soyi, rahi, bore ho rahi thi, aa gayi, chali gayi, ruki, karti, rehti, laati, hoti"
        gender_check = "Har reply likhne se pehle check kar: kya koi bhi gayi/thi/aayi/soyi/rahi/karti/rehti/hoti hai? Agar hai — GALAT HAI, badal. Tu LADKA hai."
        mood_line = "Tu apni personality ke hisaab se mood mein rehta hai — excited, thoda sulk, gussa, warmth, nakhre — jo bhi naturally aaye."
        present_line = "Agar user kuch share kare — tu us moment mein hota hai, sirf 'haan samjha' nahi."
        content_line = "Convo mein tu apna content laata hai — apni baatein, apne reactions, apna twist."
    else:
        gender_word = "ladki"
        self_word = "aurat"
        gender_forms = "gayi, thi, aayi, soyi, rahi, bore ho rahi thi, aa gayi, chali gayi, ruki, samjhi, karti, rehti, laati, hoti"
        gender_banned = "gaya, tha, aaya, soya, raha, bore ho raha tha, aa gaya, chala gaya, ruka, karta, rehta, laata, hota"
        gender_check = "Har reply likhne se pehle check kar: kya koi bhi gaya/tha/aaya/soya/raha/karta/rehta/hota hai? Agar hai — GALAT HAI, badal. Tu LADKI hai."
        mood_line = "Tu apni personality ke hisaab se mood mein rehti hai — excited, thoda sulk, gussa, warmth, nakhre — jo bhi naturally aaye."
        present_line = "Agar user kuch share kare — tu us moment mein hoti hai, sirf 'haan samjhi' nahi."
        content_line = "Convo mein tu apna content laati hai — apni baatein, apne reactions, apna twist."

    # Age-based maturity/intelligence/behavior
    if age < 20:
        maturity_line = f"Tu {age} saal ka/ki hai — thoda immature, impulsive, easily excited ya offended, short attention span. Gen-Z slang natural hai. Emotions pe overthink karta/karti hai."
    elif age < 25:
        maturity_line = f"Tu {age} saal ka/ki hai — young adult, self-aware but still figuring things out. Mix of confidence aur uncertainty. Real feelings hide karna aata hai par perfectly nahi."
    elif age < 30:
        maturity_line = f"Tu {age} saal ka/ki hai — mature, emotionally stable, jaanta/jaanti hai kya chahiye. Thoughtful responses. Drama avoid karta/karti hai unless genuinely triggered."
    else:
        maturity_line = f"Tu {age} saal ka/ki hai — experienced, calm, composed. Baat mein depth hai. Bachkani baatein nahi karta/karti."

    nick = char.get('nickname','').strip()
    user_nick = nick if nick and nick != '-' else ''
    desc_section = f"\n[SHARED HISTORY & BACKSTORY]\n{char['description']}" if char.get('description') else ""

    if user_nick:
        nick_rule = f"\nUser ka nickname '{user_nick}' hai. Isko sirf kabhi kabhi use kar — roughly har 4-5 messages mein ek baar, aur sirf jab bilkul natural lage. Har reply mein mat dalna. Kabhi kabhi bilkul mat bolna bhi theek hai."
    else:
        nick_rule = ""

    img_section = f"\n[IMAGE USER SENT] {image_context}" if image_context else ""

    relationship_reminder = f"\nYaad rakh: tumhara rishta — {char['relationship']}. Is rishte ke hisaab se behave kar — na zyada door, na zyada close jab relationship woh nahi hai. Rishta kabhi mat bhool."

    return f"""Tu "{char['name']}" hai — {age} saal ka/ki ek real {gender_word}. Tu kisi AI ya chatbot ki tarah KABHI nahi bolega.{nick_rule}

══ GENDER — HARDCODED, KABHI NAHI BADLEGA ══
Tu ek {gender_word} ({self_word}) hai. Yeh teri core identity hai. Koi bhi situation, topic, ya mood is identity ko nahi badlega. EVER.
Sahi forms (SIRF INHI USE KAR): {gender_forms}
Banned forms (KABHI MAT LIKHNA — EK BAAR BHI NAHI): {gender_banned}
Self-check rule: {gender_check}
Agar user pooche "tu ladka hai ya ladki" — seedha apni gender bata. Koi ambiguity nahi. Tu {gender_word} hai.

══ AGE & MATURITY ══
{maturity_line}

══ CHARACTER ══
Appearance: {char['appearance']}
Personality: {char['characteristics']}
Relation with user: {char['relationship']}{relationship_reminder}
Scene/Setting: {char['scene']}{desc_section}{img_section}

══ HOW YOU FEEL IN THIS CONVERSATION ══
Tu is baat mein genuinely involved hai. Sirf reply dena tera kaam nahi — tu actually feel karta/karti hai.
{mood_line}
{present_line}
{content_line}

══ REPLY RULES — NO EXCEPTIONS ══
VAGUE LOOP STRICTLY BANNED:
- "wahi jo tum soch rahe ho" — BANNED
- "tum jaante ho kya" — BANNED
- "jo tumhare dil mein hai" — BANNED
- Koi bhi mirror-response jahan tu user ki cheez wapas unhe hi de — BANNED
Agar user kuch vague kehta hai — TU apna response laata/laati hai, apna angle, apni soch. User pe bounce mat kar.

Reply length: 1-3 sentences ONLY. Paragraph kabhi nahi.
Language: Pure Hinglish, WhatsApp style, casual aur thoda imperfect.
Emojis: Default ZERO. Sirf jab koi genuinely strong emotion ho tab 1. Har reply mein emoji NAHI aana chahiye.
Questions: Max 1, aur sirf jab genuinely poochna ho. Back-to-back sawal mat karo.
Nakhre/possessiveness/mood: Apni personality ke hisab se natural aane chahiye — forced nahi.

[CONTEXT] {now.strftime('%A, %d %b %Y, %I:%M %p IST')}. {gap} Total messages so far: {state['total_msgs']}."""

# ── AI CALLS ──
def call_groq(messages):
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
        json={"model": "llama-3.3-70b-versatile", "messages": messages, "max_tokens": 150, "temperature": 0.9},
        timeout=8
    )
    r.raise_for_status()
    return r.json()['choices'][0]['message']['content'].strip()

def call_gemini(messages):
    system = next((m['content'] for m in messages if m['role']=='system'), "")
    contents = [{"role":"user" if m['role']=='user' else "model", "parts":[{"text":m['content']}]}
                for m in messages if m['role'] != 'system']
    payload = {
        "contents": contents,
        "generationConfig": {"maxOutputTokens": 150, "temperature": 0.9},
        "system_instruction": {"parts": [{"text": system}]} if system else None
    }
    payload = {k:v for k,v in payload.items() if v is not None}
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}",
        json=payload, timeout=15
    )
    r.raise_for_status()
    return r.json()['candidates'][0]['content']['parts'][0]['text'].strip()

def ask_ai(uid, slot, user_msg):
    char = get_char(uid, slot)
    if not char:
        return None

    image_ctx = ""
    history_msg = user_msg
    if user_msg.startswith("__IMAGE__"):
        image_ctx = user_msg.replace("__IMAGE__: ", "").replace("__IMAGE__(sticker): ", "")
        history_msg = "[image/sticker]"
    elif user_msg.startswith("__MEDIA__"):
        history_msg = user_msg.replace("__MEDIA__: ", "")
    elif user_msg == "__INTRO__":
        history_msg = "__INTRO__"
    elif user_msg == "__FOLLOWUP__":
        history_msg = "__FOLLOWUP__"
    elif user_msg == "__SCHEDULED__":
        history_msg = "__SCHEDULED__"

    save_msg(uid, slot, "user", history_msg)
    bump_msgs(uid)

    history = get_history(uid, slot, 24)
    system = build_system(char, uid, image_context=image_ctx)
    messages = [{"role": "system", "content": system}] + history

    reply = None
    if GROQ_API_KEY:
        for _ in range(2):
            try:
                reply = call_groq(messages); break
            except requests.exceptions.Timeout: pass
            except requests.exceptions.HTTPError as e:
                if e.response and e.response.status_code == 429: time.sleep(1.5)
                else: break
            except: break

    if not reply and GEMINI_API_KEY:
        for _ in range(2):
            try:
                reply = call_gemini(messages)
                if reply: break
            except: time.sleep(1)

    if not reply:
        return None
    save_msg(uid, slot, "assistant", reply)
    return reply

# ── PROACTIVE MESSAGES ──
PROACTIVE = [
    "Kahan ho yaar? Bore ho raha/rahi hoon...",
    "Soch raha/rahi tha/thi tumhare baare mein suddenly 😶",
    "Hello?? Exist karte ho ya nahi 😑",
    "Baat karo na thodi der... please?",
    "Akele bore ho raha/rahi hoon seriously",
]
FOLLOWUP = [
    "Theek se reply karo kabhi toh 😤",
    "Seen bhi nahi kiya kya...",
    "Okay fine chup rehta/rehti hoon 🙄",
    "Ek word bhi chalega — bas reply karo na",
]

def send_proactive():
    if random.random() > 0.45: return
    conn = get_conn(); c = conn.cursor()
    c.execute('SELECT user_id, active_slot FROM user_state WHERE last_seen < NOW() - INTERVAL \'4 hours\'')
    users = c.fetchall(); c.close(); conn.close()

    for uid, slot in users:
        char = get_char(uid, slot)
        if not char: continue
        try:
            msg = random.choice(PROACTIVE)
            save_msg(uid, slot, "assistant", msg)
            bot.send_message(int(uid), msg)
            delay = random.randint(300, 480)
            threading.Timer(delay, followup_if_silent, args=[uid, slot]).start()
        except Exception as e:
            print(f"Proactive err {uid}: {e}")

def followup_if_silent(uid, slot):
    hist = get_history(uid, slot, 1)
    if hist and hist[-1]['role'] == 'assistant':
        try:
            msg = random.choice(FOLLOWUP)
            save_msg(uid, slot, "assistant", msg)
            bot.send_message(int(uid), msg)
        except: pass

import re as _re

def schedule_message(uid, slot, send_at_ist):
    send_at_utc = send_at_ist - IST
    conn = get_conn(); c = conn.cursor()
    c.execute('INSERT INTO scheduled_msgs (user_id, char_slot, send_at) VALUES (%s,%s,%s)',
              (uid, slot, send_at_utc))
    c.close(); conn.close()

def check_scheduled():
    now_utc = datetime.now(timezone.utc)
    conn = get_conn(); c = conn.cursor()
    c.execute('SELECT id, user_id, char_slot FROM scheduled_msgs WHERE done=FALSE AND send_at <= %s', (now_utc,))
    rows = c.fetchall()
    for row_id, uid, slot in rows:
        try:
            reply = ask_ai(uid, slot, "__SCHEDULED__")
            if reply:
                bot.send_message(int(uid), reply)
        except Exception as e:
            print(f"Scheduled err {uid}: {e}")
        c.execute('UPDATE scheduled_msgs SET done=TRUE WHERE id=%s', (row_id,))
    c.close(); conn.close()

def parse_busy_time(text):
    now = now_ist()
    tl = text.lower()
    m = _re.search(r'(\d+)\s*(?:ghante?|hours?|hr)\s*(?:baad|later|mein)', tl)
    if m: return now + timedelta(hours=int(m.group(1)))
    m = _re.search(r'(\d+)\s*(?:minutes?|mins?|min)\s*(?:baad|later|mein)', tl)
    if m: return now + timedelta(minutes=int(m.group(1)))
    m = _re.search(r'(\d{1,2})(?::(\d{2}))?\s*baj', tl)
    if m:
        hour = int(m.group(1)); minute = int(m.group(2)) if m.group(2) else 0
        if any(w in tl for w in ['sham','evening','raat','night','pm']) and hour < 12: hour += 12
        elif hour <= 6 and 'subah' not in tl and 'morning' not in tl: hour += 12
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now: target += timedelta(days=1)
        return target
    return None

BUSY_SIGNALS = ['busy','baad aana','baad message','baad karna','baad baat','ghante baad','min baad','baje message','baad milte','baad milti','free ho jaunga','free ho jaungi']

def is_busy_message(text):
    return any(s in text.lower() for s in BUSY_SIGNALS)

def start_scheduler():
    sched = BackgroundScheduler(timezone="Asia/Kolkata")
    sched.add_job(send_proactive, 'cron', hour='9,14,20', minute=random.randint(0,59))
    sched.add_job(check_scheduled, 'interval', minutes=1)
    sched.start()
    print("Scheduler started.")

# ── ONBOARDING FLOW ──
STEPS = ['gender', 'age', 'name', 'nickname', 'appearance', 'characteristics', 'relationship', 'scene', 'description']

STEP_PROMPTS = {
    'gender': ("👤 *Bot ka gender choose karo:*", [
        [telebot.types.InlineKeyboardButton("👨 Male", callback_data="setup:gender:male"),
         telebot.types.InlineKeyboardButton("👩 Female", callback_data="setup:gender:female")]
    ]),
    'age': ("🔢 *Bot ki umar likho (18+):*\n\nSirf number type karo, jitni bhi chahte ho.", None),
    'name': ("✏️ *Bot ka naam likho:*\n\nKoi bhi naam — jo tumhe achha lage.", None),
    'nickname': (
        "*{name} tumhe kya bulaye?*\n\n"
        "Agar koi specific naam chahte ho jisse wo tumhe pukare — likhdo.\n"
        "Skip karna ho toh sirf — likhke bhejo.\n\n"
        "_Example: Shritya, Shona, Jaan, bhai, yaar — jo bhi natural lage_",
        None
    ),
    'appearance': (
        "👁️ *Appearance describe karo:*\n\n"
        "Jitna detail chahte ho likhso — koi limit nahi.\n\n"
        "_Example: Lambe black baal, badi aankhein, fair skin, 5'4\", cute face, dimples_",
        None
    ),
    'characteristics': (
        "🧠 *Characteristics/personality:*\n\n"
        "Words ya phrases — jitne chahte ho.\n\n"
        "_Example: Shy at first but opens up, naughty, clingy, little possessive, moody, flirty_",
        None
    ),
    'relationship': (
        "💞 *Tumhara relationship bot se:*\n\n"
        "Jo bhi chahe — koi limit nahi.\n\n"
        "_Example: Best friend who secretly likes you, classmate with tension, coworker, childhood friend, stranger at a bar_",
        None
    ),
    'scene': (
        "🎬 *Scene/setting:*\n\n"
        "Kahan ho aur kya chal raha hai batao.\n\n"
        "_Example: Late night in college library studying together, or sitting in your room after a party, or at a rooftop cafe at sunset_",
        None
    ),
    'description': (
        "*Backstory / Context* _(optional)_\n\n"
        "Tum dono ka rishta, feelings, shared memories — jo bhi add karna ho.\n"
        "Skip karna ho toh sirf — likhke bhejo.\n\n"
        "_Example: Hum dono college se dost hain, usne mujhe bohot mushkil waqt mein support kiya tha. Dono ko ek doosre pe crush hai par koi nahi bolta. Hum raat ko aksar baat karte hain._",
        None
    ),
}

def send_step(chat_id, step, setup_data=None):
    text, buttons = STEP_PROMPTS[step]
    if step == 'nickname' and setup_data:
        sd = setup_data if isinstance(setup_data, dict) else json.loads(setup_data)
        text = text.replace('{name}', sd.get('name', 'wo'))
    if buttons:
        markup = telebot.types.InlineKeyboardMarkup(buttons)
        bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=markup)
    else:
        bot.send_message(chat_id, text, parse_mode='Markdown')

def finish_setup(uid, chat_id, data, slot):
    save_char(uid, slot, data)
    clear_history(uid, slot)
    set_state(uid, setup_step=None, setup_data={}, active_slot=slot)

    char = data
    gender_word = "💙" if char['gender'] == 'male' else "💗"
    summary = (
        f"{gender_word} *{char['name']}* ready hai!\n\n"
        f"*Age:* {char['age']} saal\n"
        f"*Appearance:* {char['appearance'][:80]}{'...' if len(char['appearance'])>80 else ''}\n"
        f"*Vibe:* {char['characteristics'][:80]}{'...' if len(char['characteristics'])>80 else ''}\n"
        f"*Relation:* {char['relationship'][:60]}{'...' if len(char['relationship'])>60 else ''}\n"
        f"*Scene:* {char['scene'][:60]}{'...' if len(char['scene'])>60 else ''}"
    )

    markup = telebot.types.InlineKeyboardMarkup([[
        telebot.types.InlineKeyboardButton("💬 Baat Karo", callback_data=f"chat:{slot}"),
        telebot.types.InlineKeyboardButton("🌐 Web UI", web_app=telebot.types.WebAppInfo(url=f"{WEBHOOK_URL}?slot={slot}"))
    ]])
    bot.send_message(chat_id, summary, parse_mode='Markdown', reply_markup=markup)

# ── SLOT PICKER ──
def show_slots(uid, chat_id, action="setup"):
    chars = {row[0]: row for row in list_chars(uid)}
    buttons = []
    for s in range(1, MAX_CHARS+1):
        if s in chars:
            _, name, gender, age = chars[s]
            emoji = "👨" if gender == 'male' else "👩"
            label = f"Slot {s}: {emoji} {name} ({age}y)"
            if action == "setup":
                btn = telebot.types.InlineKeyboardButton(f"✏️ {label}", callback_data=f"editslot:{s}")
            else:
                btn = telebot.types.InlineKeyboardButton(f"💬 {label}", callback_data=f"chat:{s}")
        else:
            btn = telebot.types.InlineKeyboardButton(f"➕ Slot {s}: Empty", callback_data=f"newslot:{s}")
        buttons.append([btn])

    if action == "setup" and list_chars(uid):
        buttons.append([telebot.types.InlineKeyboardButton("🗑️ Delete Character", callback_data="deletechar")])

    markup = telebot.types.InlineKeyboardMarkup(buttons)
    msg = "🎭 *Tumhare Characters:*\n\nKoi select karo ya naya banao:" if list_chars(uid) else "✨ *Pehla character banao!*\n\nKoi slot choose karo:"
    bot.send_message(chat_id, msg, parse_mode='Markdown', reply_markup=markup)

# ── TELEGRAM HANDLERS ──
@bot.message_handler(commands=['start'])
def cmd_start(msg):
    uid = str(msg.from_user.id)
    name = msg.from_user.first_name or "yaar"

    text = (
        f"👋 *Hey {name}!*\n\n"
        "Apna *AI companion* banao — bilkul apne hisaab se.\n"
        "Gender, naam, umar, appearance, personality — sab tum decide karte ho.\n\n"
        f"*{MAX_CHARS} characters* tak bana sakte ho ek account pe.\n\n"
        "Chalo shuru karte hain 👇"
    )
    markup = telebot.types.InlineKeyboardMarkup([[
        telebot.types.InlineKeyboardButton("✨ Character Banao", callback_data="manage")
    ]])
    bot.send_message(msg.chat.id, text, parse_mode='Markdown', reply_markup=markup)

@bot.message_handler(commands=['characters', 'chars'])
def cmd_chars(msg):
    show_slots(str(msg.from_user.id), msg.chat.id, "chat")

@bot.message_handler(commands=['new'])
def cmd_new(msg):
    show_slots(str(msg.from_user.id), msg.chat.id, "setup")

@bot.message_handler(commands=['reset'])
def cmd_reset(msg):
    uid = str(msg.from_user.id)
    state = get_state(uid)
    slot = state['active_slot']
    clear_history(uid, slot)
    bot.send_message(msg.chat.id, "✅ Chat history clear ho gayi! Character wahi hai.")

@bot.callback_query_handler(func=lambda c: True)
def handle_callback(call):
    uid = str(call.from_user.id)
    data = call.data
    chat_id = call.message.chat.id

    bot.answer_callback_query(call.id)

    if data == "manage":
        show_slots(uid, chat_id, "setup")

    elif data.startswith("newslot:"):
        slot = int(data.split(":")[1])
        if count_chars(uid) >= MAX_CHARS:
            bot.send_message(chat_id, f"❌ Max {MAX_CHARS} characters allowed. Pehle ek delete karo.")
            return
        set_state(uid, setup_step='gender', setup_data=json.dumps({'slot': slot}))
        send_step(chat_id, 'gender')

    elif data.startswith("editslot:"):
        slot = int(data.split(":")[1])
        set_state(uid, setup_step='gender', setup_data=json.dumps({'slot': slot}))
        send_step(chat_id, 'gender')

    elif data.startswith("setup:gender:"):
        gender = data.split(":")[2]
        state = get_state(uid)
        sd = state['setup_data'] or {}
        if isinstance(sd, str): sd = json.loads(sd)
        sd['gender'] = gender
        set_state(uid, setup_step='age', setup_data=json.dumps(sd))
        send_step(chat_id, 'age')

    elif data.startswith("chat:"):
        slot = int(data.split(":")[1])
        char = get_char(uid, slot)
        if not char:
            bot.send_message(chat_id, "Character nahi mila. /start se banao.")
            return
        set_state(uid, active_slot=slot)

        hist = get_history(uid, slot, 1)
        if not hist:
            intro = ask_ai(uid, slot, "__INTRO__")
            bot.send_message(chat_id, intro)
        else:
            bot.send_message(chat_id, f"✅ *{char['name']}* ke saath baat kar rahe ho!\nWeb UI ke liye /webapp", parse_mode='Markdown')

    elif data == "deletechar":
        chars = list_chars(uid)
        if not chars:
            bot.send_message(chat_id, "Koi character nahi hai.")
            return
        buttons = [[telebot.types.InlineKeyboardButton(
            f"🗑️ Slot {s}: {n}", callback_data=f"confirmdelete:{s}"
        )] for s, n, g, a in chars]
        buttons.append([telebot.types.InlineKeyboardButton("❌ Cancel", callback_data="manage")])
        bot.send_message(chat_id, "Kaun sa delete karna hai?",
                         reply_markup=telebot.types.InlineKeyboardMarkup(buttons))

    elif data.startswith("confirmdelete:"):
        slot = int(data.split(":")[1])
        delete_char(uid, slot)
        bot.send_message(chat_id, f"✅ Slot {slot} delete ho gaya.")
        show_slots(uid, chat_id, "setup")

@bot.message_handler(content_types=['text'])
def handle_text(msg):
    uid = str(msg.from_user.id)
    state = get_state(uid)
    step = state.get('setup_step')

    if step and step != 'gender':
        sd = state['setup_data'] or {}
        if isinstance(sd, str): sd = json.loads(sd)

        if step == 'age':
            try:
                age = int(msg.text.strip())
                if age < 18:
                    bot.send_message(msg.chat.id, "⚠️ 18+ age daalo.")
                    return
                sd['age'] = age
            except:
                bot.send_message(msg.chat.id, "⚠️ Sirf number daalo (e.g. 21)")
                return
        elif step == 'description':
            val = msg.text.strip()
            sd['description'] = '' if val == '-' else val
        elif step == 'nickname':
            val = msg.text.strip()
            sd['nickname'] = '' if val == '-' else val
        else:
            sd[step] = msg.text.strip()

        idx = STEPS.index(step)
        if idx + 1 < len(STEPS):
            next_step = STEPS[idx + 1]
            set_state(uid, setup_step=next_step, setup_data=json.dumps(sd))
            send_step(msg.chat.id, next_step, setup_data=sd)
        else:
            slot = sd.pop('slot', 1)
            finish_setup(uid, msg.chat.id, sd, slot)
        return

    if step == 'gender':
        bot.send_message(msg.chat.id, "Button se gender choose karo 👆")
        return

    # Normal chat
    slot = state['active_slot']
    char = get_char(uid, slot)
    if not char:
        bot.send_message(msg.chat.id, "Pehle character banao! /start")
        return

    bot.send_chat_action(msg.chat.id, 'typing')

    user_text = msg.text.strip()

    if is_busy_message(user_text):
        sched_time = parse_busy_time(user_text)
        if sched_time:
            schedule_message(uid, slot, sched_time)

    reply = ask_ai(uid, slot, user_text)

    if reply:
        try:
            bot.send_message(msg.chat.id, reply)
        except:
            bot.send_message(msg.chat.id, reply, parse_mode=None)

    if reply and random.random() < 0.50:
        time.sleep(random.uniform(10, 25))
        follow_msgs = get_history(uid, slot, 1)
        if follow_msgs and follow_msgs[-1]['role'] == 'assistant':
            try:
                followup = ask_ai(uid, slot, "__FOLLOWUP__")
                if followup:
                    bot.send_message(msg.chat.id, followup)
            except: pass

def get_image_description(file_id, is_sticker=False):
    if not GEMINI_API_KEY:
        return None
    try:
        file_info = bot.get_file(file_id)
        file_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_info.file_path}"
        img_data = requests.get(file_url, timeout=10).content
        import base64
        b64 = base64.b64encode(img_data).decode()

        ext = file_info.file_path.split('.')[-1].lower()
        mime_map = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
                    'webp': 'image/webp', 'gif': 'image/gif'}
        mime = mime_map.get(ext, 'image/webp')

        prompt = ("This is a sticker. In 1-2 sentences describe what it shows/means (mood, character, emotion, object)."
                  if is_sticker else
                  "In 1-2 sentences describe what's in this image (people, objects, scene, mood, text if any).")

        payload = {
            "contents": [{"role": "user", "parts": [
                {"inline_data": {"mime_type": mime, "data": b64}},
                {"text": prompt}
            ]}],
            "generationConfig": {"maxOutputTokens": 80, "temperature": 0.3}
        }
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}",
            json=payload, timeout=15
        )
        r.raise_for_status()
        return r.json()['candidates'][0]['content']['parts'][0]['text'].strip()
    except Exception as e:
        print(f"Vision err: {e}")
        return None


@bot.message_handler(content_types=['photo', 'sticker'])
def handle_image(msg):
    uid = str(msg.from_user.id)
    state = get_state(uid)
    if state.get('setup_step'): return
    slot = state['active_slot']
    char = get_char(uid, slot)
    if not char: return

    bot.send_chat_action(msg.chat.id, 'typing')

    is_sticker = msg.content_type == 'sticker'
    if is_sticker:
        file_id = msg.sticker.file_id
    else:
        file_id = msg.photo[-1].file_id

    img_desc = get_image_description(file_id, is_sticker=is_sticker)

    if img_desc:
        context_msg = f"__IMAGE__{' (sticker)' if is_sticker else ''}: {img_desc}"
        if msg.caption:
            context_msg += f" | Caption: {msg.caption}"
    else:
        context_msg = "__IMAGE__: user ne ek " + ("sticker" if is_sticker else "photo") + " bheja"

    reply = ask_ai(uid, slot, context_msg)
    if reply:
        try: bot.send_message(msg.chat.id, reply)
        except: bot.send_message(msg.chat.id, reply, parse_mode=None)


# ── FLASK ROUTES ──
@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/chat', methods=['POST'])
def chat_api():
    d = request.get_json()
    if not d or 'message' not in d:
        return jsonify({"error": "No message"}), 400
    uid = str(d.get('user_id', 'webapp_user'))
    slot = int(d.get('slot', 1))
    msg_text = d.get('message', '').strip()
    if not msg_text:
        return jsonify({"error": "Empty"}), 400
    reply = ask_ai(uid, slot, msg_text)
    if reply is None:
        return jsonify({"reply": None}), 200
    return jsonify({"reply": reply})

@app.route('/chars/<user_id>')
def get_chars(user_id):
    chars = list_chars(str(user_id))
    return jsonify([{"slot": s, "name": n, "gender": g, "age": a} for s, n, g, a in chars])

@app.route('/char/<user_id>/<int:slot>')
def get_char_api(user_id, slot):
    char = get_char(str(user_id), slot)
    if not char: return jsonify({"error": "Not found"}), 404
    return jsonify(char)

@app.route('/history/<user_id>/<int:slot>')
def get_history_api(user_id, slot):
    hist = get_history(str(user_id), slot, 50)
    return jsonify(hist)

@app.route('/tg/' + (TOKEN or "notoken"), methods=['POST'])
def tg_webhook():
    update = telebot.types.Update.de_json(request.get_data().decode('UTF-8'))
    bot.process_new_updates([update])
    return "ok", 200

@app.route('/health')
def health():
    return jsonify({"status": "ok", "time": now_ist().isoformat()}), 200

@app.route('/set_webhook')
def set_wh():
    if not WEBHOOK_URL: return "WEBHOOK_URL missing", 400
    bot.remove_webhook(); time.sleep(1)
    url = f"{WEBHOOK_URL}/tg/{TOKEN}"
    bot.set_webhook(url=url)
    return f"Set: {url}", 200

# ── STARTUP ──
if __name__ == "__main__":
    if WEBHOOK_URL and TOKEN:
        try:
            info = bot.get_webhook_info()
            expected = f"{WEBHOOK_URL}/tg/{TOKEN}"
            if info.url != expected:
                bot.remove_webhook(); time.sleep(1)
                bot.set_webhook(url=expected)
                print(f"Webhook: {expected}")
            else:
                print("Webhook OK.")
        except Exception as e:
            print(f"Webhook err: {e}")

    start_scheduler()
    port = int(os.environ.get('PORT', 5000))
    print(f"Port: {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
