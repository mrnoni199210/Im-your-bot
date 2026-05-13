# 🎭 AI Companion Bot — Setup Guide

## What This Does
- Users create up to **3 custom AI companions** via Telegram
- Each companion has: gender, age, name, appearance, personality, relationship type, scene
- **Persistent memory** via Supabase/PostgreSQL
- **Proactive messaging** — bot texts user on its own (3x daily schedule)
- **Dual API** — Groq primary, Gemini fallback (no downtime)
- Beautiful **Web UI** accessible via Telegram WebApp

---

## Folder Structure
```
bot/
├── main.py          ← Full backend (Flask + Telegram bot)
├── requirements.txt
├── Procfile         ← For Render deployment
└── static/
    └── index.html   ← Web UI
```

---

## Environment Variables (set on Render)

| Variable | Value |
|---|---|
| `BOT_TOKEN` | Your Telegram bot token from @BotFather |
| `GROQ_API_KEY` | From console.groq.com (free) |
| `GEMINI_API_KEY` | From aistudio.google.com (free) |
| `WEBHOOK_URL` | Your Render app URL e.g. `https://yourapp.onrender.com` |
| `DATABASE_URL` | Supabase PostgreSQL connection string |

---

## Deploy on Render

1. Push code to GitHub repo
2. Create **Web Service** on Render
3. Set all env vars above
4. Build command: `pip install -r requirements.txt`
5. Start command: auto-reads from `Procfile`
6. Once deployed → visit `https://yourapp.onrender.com/set_webhook`

---

## Supabase DB Setup

1. Go to supabase.com → New project
2. Settings → Database → Connection string (URI)
3. Copy and paste as `DATABASE_URL` on Render
4. Tables auto-created on first run ✅

---

## Telegram Bot Setup

1. Message @BotFather → `/newbot`
2. Set name + username
3. Copy token → `BOT_TOKEN` env var
4. Enable **Inline Mode** (optional)
5. Set **Web App** domain:
   - BotFather → `/mybots` → Bot → Bot Settings → Menu Button
   - Set URL: `https://yourapp.onrender.com`

---

## User Flow (Telegram)

```
/start
  → Choose slot (up to 3)
  → Gender (Male/Female button)
  → Age (18+ only, type number)
  → Name (type)
  → Appearance (free text, any detail)
  → Characteristics (free text, any amount)
  → Relationship (free text)
  → Scene (free text)
  → Character ready! Chat opens ✅
```

### Commands
| Command | Action |
|---|---|
| `/start` | Create/manage characters |
| `/chars` | Switch between characters |
| `/reset` | Clear current character's chat history |
| `/new` | Create new character |

---

## API Strategy (No Downtime)

- **Primary**: Groq (`llama-3.3-70b-versatile`) — fast, free tier generous
- **Fallback**: Gemini 2.0 Flash — kicks in on Groq 429/timeout
- 2 retries each before giving up
- Max tokens: 150 (keeps replies short + natural)

---

## Proactive Messaging Schedule

Bot auto-messages users at **9am, 2pm, 8pm IST** (50% chance each slot).
Only fires if user was inactive for 4+ hours.
Follow-up sent 5-8 min later if still no reply.

---

## Web UI Features

- Character switcher (tap header icon)
- Full chat history loaded on open
- Character info panel (appearance, personality, etc.)
- Works inside Telegram WebApp + standalone browser
- Dark premium design, mobile-first

---

## Notes

- `__INTRO__` and `__FOLLOWUP__` are internal signals — AI handles them naturally
- Max 3 characters per Telegram user ID
- All chat history stored per (user_id, slot) — fully isolated
- History kept last 24 messages in context window
