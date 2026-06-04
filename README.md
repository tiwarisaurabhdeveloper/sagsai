# SAGS AI — Complete System Documentation

> Intelligent career assistant with auth, AI agent, MCP tools, and a chat UI.

---

## Architecture Overview

```
Browser
  │
  ├─ GET  http://<host>:8002/          → auth_bot.html  (login / register / home)
  │       POST /auth/*                 → auth.py  (FastAPI, port 8002)
  │                                        └─ sags_users.db  (SQLite — users, OTPs, sessions)
  │
  └─ GET  http://<host>:8002/main_bot  → index.html  (bot chat UI, served by auth.py)
          POST/GET http://<host>:8001/* → main.py  (FastAPI, port 8001)
                                             ├─ langgraph_database_backend.py
                                             │    ├─ LangGraph agent  (create_react_agent)
                                             │    ├─ chatbot.db  (SQLite — conversation memory)
                                             │    └─ MCP client → tool_mcp_server.py  (port 8004)
                                             └─ uploads/  (user-uploaded files)
```

---

## Services & Ports

| Service | File | Port | Purpose |
|---|---|---|---|
| Auth API | `auth.py` | 8002 | User auth, OTP, JWT, serves HTML |
| Bot API | `main.py` | 8001 | Chat endpoint, thread management |
| MCP Server | `tool_mcp_server.py` | 8004 | AI tools (job search, ATS, resume) |

---

## How to Run

```bash
chmod +x start.sh
./start.sh
```

`start.sh` starts all three services:
```bash
python3 tool_mcp_server.py &        # MCP tools — port 8004
uvicorn main:app --port 8001 &      # Bot API  — port 8001
uvicorn auth:app --port 8002        # Auth API — port 8002
```

Open browser: `http://<your-ip>:8002`

---

## Environment Variables (`.env`)

```env
# JWT
JWT_SECRET=your-secret-key

# Email OTP (Gmail App Password)
SMTP_USER=you@gmail.com
SMTP_PASSWORD=xxxx-xxxx-xxxx-xxxx
SMTP_FROM_NAME=SAGS AI

# SMS OTP (Twilio — optional)
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_FROM_NUMBER=

# Google OAuth (optional)
GOOGLE_CLIENT_ID=

# LLM
GROQ_API_KEY=your-groq-key
```

> In dev mode (no SMTP set), OTPs print to the terminal.

---

## Install

```bash
pip install fastapi uvicorn bcrypt aiosqlite aiosmtplib python-jose \
            python-multipart python-dotenv google-auth \
            langchain langchain-groq langgraph langchain-mcp-adapters
```

---

## Complete User Flow

### 1. Register
1. User fills First Name, Last Name, Email (or Phone), Password on `/`
2. `auth.py` validates password (min 8 chars, 1 uppercase), checks for duplicates
3. Registration data saved to `pending_registrations` table — **NOT** users table yet
4. OTP generated → sent via `aiosmtplib` (email) or Twilio (phone)
5. Frontend navigates to OTP page with `pending_token`
6. User enters 6-digit OTP
7. `POST /auth/verify-otp` validates OTP → **creates user in `users` table** → returns JWT
8. Frontend stores JWT + user in `localStorage`, redirects to home page

> Unverified users never enter the database. Only verified users are stored.

### 2. Login
1. User enters email/phone + password
2. `POST /auth/login` checks `users` table, verifies bcrypt hash
3. Returns JWT → stored in `localStorage`

### 3. Forgot Password
1. User enters email/phone on forgot page
2. `POST /auth/forgot-password` checks user exists, sends OTP
3. User enters OTP → `POST /auth/verify-otp` validates
4. User sets new password → `POST /auth/reset-password` updates hash

### 4. Google OAuth
1. Frontend calls Google GSI SDK → gets credential token
2. `POST /auth/google` verifies token with Google, creates/updates user
3. Returns JWT same as email login

### 5. Chat with Bot
1. After login, home page loads with bot widget (bottom-right toggle button)
2. Click toggle → bot panel opens as iframe loading `/main_bot`
3. `/main_bot` served by `auth.py` → loads `index.html` (bot UI)
4. Bot UI calls `main.py` on port 8001
5. Click maximize (⛶) → panel goes fullscreen

---

## Auth Database (`sags_users.db`)

| Table | Purpose |
|---|---|
| `users` | Verified accounts (id, name, email, phone, password_hash, google_id, is_verified) |
| `otps` | OTP codes (target, otp_code, purpose, expires_at, used) |
| `sessions` | JWT session tracking |
| `pending_registrations` | Temp storage before OTP verify (auto-deleted on verify/expiry) |

---

## Bot / Agent Flow

```
User types message
      │
      ▼
POST /chat  (main.py)
  ├─ Files uploaded? → saved to uploads/ → filename appended to message
  ├─ Web search toggle? → passed as form field (currently logged, tools handle it)
  │
  ▼
LangGraph agent  (langgraph_database_backend.py)
  ├─ LLM: Groq (gpt-oss-20b)
  ├─ Memory: AsyncSqliteSaver → chatbot.db (keeps last 5 Q-A pairs via trim_memory)
  ├─ System prompt: main_prompt  (from prompt.py)
  │
  └─ Tool routing (via MCP → tool_mcp_server.py on port 8004):
       ├─ file + job query     → file_job_search
       ├─ file + ATS query     → ats_checker_resume
       ├─ file + summary       → file_summary
       └─ no file + job query  → linkedin_job_search
      │
      ▼
_build_response()  (main.py)
  ├─ Scans ToolMessages for job tool IDs
  ├─ If jobs found → response_type: "jobs" → renders job cards in UI
  └─ Otherwise    → response_type: "text" → renders plain bubble
```

---

## Bot UI Features (`index.html`)

- **Sidebar** — conversation history, new chat button, thread labels
- **Thread persistence** — each conversation has a UUID, stored in `chatbot.db`
- **File upload** — attach PDF/DOC/image via `+` menu, sent as `multipart/form-data`
- **Web search toggle** — enables web search flag in the request
- **Job cards** — structured cards with title, company, location, date, apply link
- **Markdown table parser** — converts LLM markdown tables → job cards automatically
- **Typing indicator** — animated dots while agent is processing
- **Fullscreen mode** — expand bot panel to full viewport

---

## CORS & Network

Both HTML files use dynamic `API_BASE`:
```javascript
// auth_bot.html
var API = window.location.origin.startsWith('file')
  ? 'http://127.0.0.1:8002'
  : window.location.origin;

// index.html
const BOT_API = window.location.origin.startsWith('file')
  ? 'http://127.0.0.1:8001'
  : window.location.origin.replace(':8002', ':8001');
```

This means the app works on any IP/host without hardcoding — `localhost`, `127.0.0.1`, or `10.0.x.x` on a local network.

---

## Security Notes

- Passwords hashed with `bcrypt` (rounds=12), no passlib dependency
- JWT tokens expire in 24 hours
- OTPs expire in 10 minutes, single-use, invalidated on reuse
- Users only created in DB after OTP verification
- SSL cert verification disabled in `aiosmtplib` for local dev — re-enable in production:
  ```python
  ssl_ctx.verify_mode = ssl.CERT_REQUIRED
  ssl_ctx.check_hostname = True
  ```

---

## File Structure

```
project/
├── auth.py                      # Auth FastAPI app (port 8002)
├── auth_bot.html                # Auth UI + Home + Bot widget
├── main.py                      # Bot FastAPI app (port 8001)
├── index.html                   # Bot chat UI
├── langgraph_database_backend.py # LangGraph agent + memory
├── tool_mcp_server.py           # MCP tools server (port 8004)
├── prompt.py                    # main_prompt system prompt
├── start.sh                     # Starts all 3 services
├── statics/
│   └── sagsai.jpeg              # Bot avatar image
├── uploads/                     # User-uploaded files (auto-created)
├── sags_users.db                # Auth SQLite database (auto-created)
├── chatbot.db                   # Conversation memory (auto-created)
└── .env                         # Secrets (never commit this)
```

---

## Adding a New Tool

1. Add the tool function in `tool_mcp_server.py`
2. Add its name to `JOB_TOOLS` set in `main.py` if it returns job cards
3. Add routing logic in `main_prompt` in `prompt.py`

---

## Common Errors

| Error | Cause | Fix |
|---|---|---|
| `SSL_CERTIFICATE_VERIFY_FAILED` | aiosmtplib SSL on Windows/Mac dev | Add `ssl_ctx.check_hostname=False, verify_mode=CERT_NONE` |
| `ERR_CONNECTION_REFUSED` | Hardcoded `localhost` in API_BASE | Use `window.location.origin` (already fixed) |
| `bcrypt ValueError 72 bytes` | passlib + bcrypt 4.x incompatibility | Use `bcrypt` directly, no passlib (already fixed) |
| `Agent not ready 503` | MCP server not running | Start `tool_mcp_server.py` first |
| `500 Internal Server Error` on register | DB or SMTP error | Check terminal logs for `DEV OTP` or error trace |













<!--  future addition  -->



<!-- # auth.py — instead of returning token in JSON body
from fastapi.responses import JSONResponse

response = JSONResponse(content={"user": user_to_dict(user)})
response.set_cookie(
    key="sags_token",
    value=token,
    httponly=True,      # JS cannot read this
    secure=True,        # HTTPS only
    samesite="strict",  # no cross-site requests
    max_age=86400       # 24 hours
)
return response -->