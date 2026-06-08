"""
auth.py — SAGS AI Authentication Backend
═══════════════════════════════════════════════════════════════

FIX: Replaced passlib with direct bcrypt calls to fix the
     "password cannot be longer than 72 bytes" / bcrypt version error.

SETUP:
  1. pip install fastapi uvicorn bcrypt python-jose python-multipart
                 google-auth requests python-dotenv
  2. Copy .env.example → .env and fill in your credentials
  3. uvicorn auth:app --reload --port 8002

ENDPOINTS:
  GET  /                         → serves auth_bot.html
  GET  /main_bot                 → serves index.html (bot UI)
  POST /auth/register            → create account
  POST /auth/login               → login with email/phone + password
  POST /auth/google              → google oauth token → login/register
  POST /auth/send-otp            → send OTP to email or phone
  POST /auth/verify-otp          → verify OTP
  POST /auth/forgot-password     → send reset OTP
  POST /auth/reset-password      → verify OTP + set new password
  GET  /auth/me                  → get current user (JWT required)
  GET  /auth/health              → health check
"""

# import os, sqlite3, logging, re, random, string, smtplib, bcrypt
import os, sqlite3, logging, re, random, string, smtplib, bcrypt, aiosqlite
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from typing import Optional
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, validator
from jose import JWTError, jwt
import json

# Load .env file if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed, use system env vars

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── CONFIG (all from .env) ────────────────────────────────────────────────────
SECRET_KEY        = os.getenv("JWT_SECRET",        "sags-ai-super-secret-CHANGE-THIS")
ALGORITHM         = "HS256"
ACCESS_TOKEN_TTL  = 60 * 24          # 24 hours in minutes
OTP_TTL_MINUTES   = 10

GOOGLE_CLIENT_ID  = os.getenv("GOOGLE_CLIENT_ID",  "")

# Email — set these in your .env file
SMTP_HOST         = os.getenv("SMTP_HOST",         "smtp.gmail.com")
SMTP_PORT         = int(os.getenv("SMTP_PORT",     "587"))
SMTP_USER         = os.getenv("SMTP_USER",         "")   # e.g. saurabh@sags.com
SMTP_PASSWORD     = os.getenv("SMTP_PASSWORD",     "")   # Gmail App Password (not your login password)
SMTP_FROM_NAME    = os.getenv("SMTP_FROM_NAME",    "SAGS AI")
SMTP_FROM_EMAIL   = os.getenv("SMTP_FROM_EMAIL",   SMTP_USER)

# SMS (Twilio) — optional
TWILIO_SID        = os.getenv("TWILIO_ACCOUNT_SID",  "")
TWILIO_TOKEN      = os.getenv("TWILIO_AUTH_TOKEN",   "")
TWILIO_FROM       = os.getenv("TWILIO_FROM_NUMBER",  "")

DB_PATH           = os.getenv("DB_PATH",           "sags_users.db")

# ── BCRYPT (direct, no passlib) ───────────────────────────────────────────────
def hash_password(password: str) -> str:
    """Hash password using bcrypt directly — avoids passlib compatibility bug."""
    pw_bytes = password.encode("utf-8")
    salt     = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(pw_bytes, salt).decode("utf-8")

def verify_password(plain: str, hashed: str) -> bool:
    """Verify password against bcrypt hash."""
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False

# ── JWT ───────────────────────────────────────────────────────────────────────
bearer = HTTPBearer(auto_error=False)

def create_access_token(user_id: int, email: str = None) -> str:
    expire  = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_TTL)
    payload = {"sub": str(user_id), "email": email or "", "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

# ── DATABASE ──────────────────────────────────────────────────────────────────
async def get_db():
    return await aiosqlite.connect(DB_PATH)

async def init_db():
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                first_name    TEXT    NOT NULL,
                last_name     TEXT    NOT NULL,
                email         TEXT    UNIQUE,
                phone         TEXT    UNIQUE,
                password_hash TEXT,
                google_id     TEXT    UNIQUE,
                avatar_url    TEXT,
                is_verified   INTEGER DEFAULT 0,
                created_at    TEXT    DEFAULT (datetime('now')),
                updated_at    TEXT    DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS otps (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                target     TEXT NOT NULL,
                otp_code   TEXT NOT NULL,
                purpose    TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used       INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                token_hash TEXT    NOT NULL,
                created_at TEXT    DEFAULT (datetime('now')),
                expires_at TEXT    NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS pending_registrations (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                token      TEXT    NOT NULL UNIQUE,
                data       TEXT    NOT NULL,
                expires_at TEXT    NOT NULL,
                created_at TEXT    DEFAULT (datetime('now'))
            );
        """)
        await conn.commit()
    logger.info("✅ Database ready: %s", DB_PATH)
# ── APP ───────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield

app = FastAPI(title="SAGS AI Auth", version="2.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="statics"), name="static")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── SERVE FRONTEND ────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def serve_auth():
    try:
        with open("auth_bot.html", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "<h2>auth_bot.html not found in the same directory as auth.py</h2>"

@app.get("/main_bot", response_class=HTMLResponse)
async def serve_bot():
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "<h2>index.html not found in the same directory as auth.py</h2>"

# ── PYDANTIC MODELS ───────────────────────────────────────────────────────────
class RegisterRequest(BaseModel):
    first_name: str
    last_name:  str
    email:      Optional[str] = None
    phone:      Optional[str] = None
    password:   str

    @validator("password")
    def password_strength(cls, v):
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter")
        return v

class LoginRequest(BaseModel):
    identifier: str
    password:   str

class GoogleAuthRequest(BaseModel):
    token: str

class SendOtpRequest(BaseModel):
    target:  str
    purpose: str   # "verify" | "forgot_password" | "login"

class VerifyOtpRequest(BaseModel):
    target:        str
    otp_code:      str
    purpose:       str
    pending_token: Optional[str] = None   # ← add this line

class ResetPasswordRequest(BaseModel):
    target:       str
    otp_code:     str
    new_password: str

    @validator("new_password")
    def password_strength(cls, v):
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter")
        return v

class TokenResponse(BaseModel):
    access_token: str
    token_type:   str = "bearer"
    user:         dict

# ── OTP HELPERS ───────────────────────────────────────────────────────────────
def generate_otp(length: int = 6) -> str:
    return "".join(random.choices(string.digits, k=length))

async def store_otp(target: str, otp: str, purpose: str):
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        expires_at = (datetime.utcnow() + timedelta(minutes=OTP_TTL_MINUTES)).isoformat()
        await conn.execute(
            "UPDATE otps SET used=1 WHERE target=? AND purpose=? AND used=0",
            (target, purpose)
        )
        await conn.execute(
            "INSERT INTO otps (target, otp_code, purpose, expires_at) VALUES (?,?,?,?)",
            (target, otp, purpose, expires_at)
        )
        await conn.commit()

async def validate_otp(target: str, otp_code: str, purpose: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("""
            SELECT id FROM otps
            WHERE target=? AND otp_code=? AND purpose=? AND used=0
              AND expires_at > datetime('now')
            ORDER BY created_at DESC LIMIT 1
        """, (target, otp_code, purpose)) as cur:
            row = await cur.fetchone()
        if row:
            await conn.execute("UPDATE otps SET used=1 WHERE id=?", (row["id"],))
            await conn.commit()
            return True
        return False

# ── EMAIL OTP ─────────────────────────────────────────────────────────────────
import asyncio
import aiosmtplib

async def send_otp_email(to_email: str, otp: str, purpose: str):
    subject_map = {
        "verify":          "SAGS AI — Verify your email",
        "forgot_password": "SAGS AI — Password reset code",
        "login":           "SAGS AI — Your login OTP",
    }
    subject = subject_map.get(purpose, "SAGS AI — Verification code")

    html_body = f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f4f5f7;font-family:'DM Sans',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f5f7;padding:40px 0;">
    <tr><td align="center">
      <table width="480" cellpadding="0" cellspacing="0"
             style="background:#ffffff;border-radius:16px;overflow:hidden;
                    box-shadow:0 4px 24px rgba(0,0,0,0.08);">
        <tr>
          <td style="background:linear-gradient(135deg,#4f46e5,#0ea5e9);
                     padding:28px 40px;text-align:center;">
            <span style="font-size:24px;font-weight:800;color:white;
                         font-family:Arial,sans-serif;letter-spacing:-0.5px;">
              ✦ SAGS AI
            </span>
          </td>
        </tr>
        <tr>
          <td style="padding:40px;">
            <p style="font-size:16px;color:#374151;margin:0 0 8px;">Hello,</p>
            <p style="font-size:15px;color:#6b7280;margin:0 0 32px;line-height:1.6;">
              {'Here is your email verification code.' if purpose == 'verify'
               else 'Use this code to reset your password.' if purpose == 'forgot_password'
               else 'Use this code to sign in to SAGS AI.'}
            </p>
            <div style="background:#f0f1f3;border:1.5px solid #e5e7eb;
                        border-radius:12px;padding:28px;text-align:center;
                        margin-bottom:32px;">
              <span style="font-size:44px;font-weight:800;letter-spacing:14px;
                           color:#4f46e5;font-family:monospace;">
                {otp}
              </span>
            </div>
            <p style="font-size:13px;color:#9ca3af;margin:0;line-height:1.6;">
              This code expires in <strong>{OTP_TTL_MINUTES} minutes</strong>.<br/>
              If you didn't request this, you can safely ignore this email.
            </p>
          </td>
        </tr>
        <tr>
          <td style="background:#f9fafb;border-top:1px solid #f0f1f3;
                     padding:18px 40px;text-align:center;">
            <p style="font-size:12px;color:#9ca3af;margin:0;">
              © {datetime.utcnow().year} SAGS AI · sags.com
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>
"""

    if not SMTP_USER or not SMTP_PASSWORD:
        logger.info("=" * 50)
        logger.info("⚡ DEV MODE — OTP for %s: %s", to_email, otp)
        logger.info("   Set SMTP_USER + SMTP_PASSWORD in .env to send real emails")
        logger.info("=" * 50)
        return

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"{SMTP_FROM_NAME} <{SMTP_FROM_EMAIL}>"
        msg["To"]      = to_email
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        import ssl
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        await aiosmtplib.send(
            msg,
            hostname=SMTP_HOST,
            port=SMTP_PORT,
            username=SMTP_USER,
            password=SMTP_PASSWORD,
            start_tls=True,
            tls_context=ssl_ctx,
        )
        logger.info("✅ OTP email sent to %s", to_email)

    except aiosmtplib.SMTPAuthenticationError:
        logger.error("❌ SMTP auth failed — check SMTP_USER and SMTP_PASSWORD in .env")
        raise HTTPException(500, "Email authentication failed. Check server SMTP config.")
    except Exception as e:
        logger.error("❌ Email send error: %s", e)
        raise HTTPException(500, f"Failed to send OTP email: {e}")


async def send_otp_sms(phone: str, otp: str):
    if TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM:
        try:
            await asyncio.to_thread(
                lambda: __import__('twilio.rest', fromlist=['Client'])
                        .Client(TWILIO_SID, TWILIO_TOKEN)
                        .messages.create(
                            body=f"Your SAGS AI code: {otp}. Expires in {OTP_TTL_MINUTES} minutes.",
                            from_=TWILIO_FROM,
                            to=phone,
                        )
            )
            logger.info("✅ OTP SMS sent to %s", phone)
        except Exception as e:
            logger.error("❌ SMS error: %s", e)
            raise HTTPException(500, f"Failed to send OTP SMS: {e}")
    else:
        logger.info("=" * 50)
        logger.info("⚡ DEV MODE — SMS OTP for %s: %s", phone, otp)
        logger.info("   Set TWILIO_* vars in .env to send real SMS")
        logger.info("=" * 50)


# ── AUTH DEPENDENCY ───────────────────────────────────────────────────────────
async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(bearer)):
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = decode_token(credentials.credentials)
    user_id = int(payload["sub"])
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT * FROM users WHERE id=?", (user_id,)) as cur:
            user = await cur.fetchone()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return dict(user)

def user_to_dict(user) -> dict:
    u = dict(user)
    u.pop("password_hash", None)
    return u

# ── ROUTES ────────────────────────────────────────────────────────────────────

@app.get("/auth/health")
async def health():
    return {"status": "ok", "service": "SAGS AI Auth v2.0"}


@app.post("/auth/register")
async def register(req: RegisterRequest):
    if not req.email and not req.phone:
        raise HTTPException(400, "Provide at least an email or phone number")

    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        if req.email:
            async with conn.execute("SELECT id FROM users WHERE email=?", (req.email,)) as cur:
                if await cur.fetchone():
                    raise HTTPException(400, "Email already registered")
        if req.phone:
            async with conn.execute("SELECT id FROM users WHERE phone=?", (req.phone,)) as cur:
                if await cur.fetchone():
                    raise HTTPException(400, "Phone already registered")

        import secrets as _secrets
        pending_token = _secrets.token_urlsafe(32)
        pending_data  = json.dumps({
            "first_name":    req.first_name,
            "last_name":     req.last_name,
            "email":         req.email,
            "phone":         req.phone,
            "password_hash": hash_password(req.password),
        })
        expires_at = (datetime.utcnow() + timedelta(minutes=OTP_TTL_MINUTES + 5)).isoformat()
        await conn.execute(
            "DELETE FROM pending_registrations WHERE data LIKE ?",
            (f'%"{req.email or req.phone}"%',)
        )
        await conn.execute(
            "INSERT INTO pending_registrations (token, data, expires_at) VALUES (?,?,?)",
            (pending_token, pending_data, expires_at)
        )
        await conn.commit()

    target = req.email if req.email else req.phone
    otp    = generate_otp()
    await store_otp(target, otp, "verify")
    if "@" in target:
        await send_otp_email(target, otp, "verify")
    else:
        await send_otp_sms(target, otp)

    return {"pending": True, "pending_token": pending_token,
            "message": f"OTP sent to {target}. Verify to complete registration."}


@app.post("/auth/login", response_model=TokenResponse)
async def login(req: LoginRequest):
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM users WHERE email=? OR phone=?",
            (req.identifier.strip(), req.identifier.strip())
        ) as cur:
            user = await cur.fetchone()

    if not user:
        raise HTTPException(404, "No account found. Please create an account first.")
    if not user["password_hash"]:
        raise HTTPException(401, "This account uses Google sign-in. Please continue with Google.")
    if not verify_password(req.password, user["password_hash"]):
        raise HTTPException(401, "Incorrect password")

    token = create_access_token(user["id"], user["email"])
    return {"access_token": token, "user": user_to_dict(user)}


@app.post("/auth/google", response_model=TokenResponse)
async def google_auth(req: GoogleAuthRequest):
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(501, "Google OAuth not configured. Set GOOGLE_CLIENT_ID in .env")
    try:
        from google.oauth2 import id_token
        from google.auth.transport import requests as google_requests
        info = id_token.verify_oauth2_token(req.token, google_requests.Request(), GOOGLE_CLIENT_ID)
    except Exception as e:
        raise HTTPException(401, f"Invalid Google token: {e}")

    google_id = info["sub"]
    email     = info.get("email", "")
    first     = info.get("given_name",  "")
    last      = info.get("family_name", "")
    avatar    = info.get("picture",     "")

    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM users WHERE google_id=? OR email=?", (google_id, email)
        ) as cur:
            user = await cur.fetchone()

        if user:
            await conn.execute(
                "UPDATE users SET google_id=?, avatar_url=?, is_verified=1 WHERE id=?",
                (google_id, avatar, user["id"])
            )
            await conn.commit()
            async with conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)) as cur:
                user = await cur.fetchone()
        else:
            async with conn.execute(
                "INSERT INTO users (first_name, last_name, email, google_id, avatar_url, is_verified) VALUES (?,?,?,?,?,1)",
                (first, last, email, google_id, avatar)
            ) as cur:
                new_id = cur.lastrowid
            await conn.commit()
            async with conn.execute("SELECT * FROM users WHERE id=?", (new_id,)) as cur:
                user = await cur.fetchone()

    token = create_access_token(user["id"], email)
    return {"access_token": token, "user": user_to_dict(user)}


@app.post("/auth/send-otp")
async def send_otp(req: SendOtpRequest):
    if req.purpose == "login":
        async with aiosqlite.connect(DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                "SELECT id FROM users WHERE email=? OR phone=?", (req.target, req.target)
            ) as cur:
                if not await cur.fetchone():
                    raise HTTPException(404, "No account found. Please register first.")

    otp = generate_otp()
    await store_otp(req.target, otp, req.purpose)
    if "@" in req.target:
        await send_otp_email(req.target, otp, req.purpose)
    else:
        await send_otp_sms(req.target, otp)
    return {"message": f"OTP sent to {req.target}"}


@app.post("/auth/verify-otp")
async def verify_otp_route(req: VerifyOtpRequest):
    if not await validate_otp(req.target, req.otp_code, req.purpose):
        raise HTTPException(400, "Invalid or expired OTP")

    if req.purpose == "verify":
        async with aiosqlite.connect(DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            row = None
            if req.pending_token:
                async with conn.execute(
                    "SELECT * FROM pending_registrations WHERE token=? AND expires_at > datetime('now')",
                    (req.pending_token,)
                ) as cur:
                    row = await cur.fetchone()
            if not row:
                async with conn.execute(
                    "SELECT * FROM pending_registrations WHERE data LIKE ? AND expires_at > datetime('now')",
                    (f'%{req.target}%',)
                ) as cur:
                    row = await cur.fetchone()
            if not row:
                raise HTTPException(400, "Registration session expired. Please register again.")

            data = json.loads(row["data"])

            if data.get("email"):
                async with conn.execute("SELECT id FROM users WHERE email=?", (data["email"],)) as cur:
                    if await cur.fetchone():
                        await conn.execute("DELETE FROM pending_registrations WHERE id=?", (row["id"],))
                        await conn.commit()
                        raise HTTPException(400, "Email already registered")
            if data.get("phone"):
                async with conn.execute("SELECT id FROM users WHERE phone=?", (data["phone"],)) as cur:
                    if await cur.fetchone():
                        await conn.execute("DELETE FROM pending_registrations WHERE id=?", (row["id"],))
                        await conn.commit()
                        raise HTTPException(400, "Phone already registered")

            async with conn.execute(
                "INSERT INTO users (first_name, last_name, email, phone, password_hash, is_verified) VALUES (?,?,?,?,?,1)",
                (data["first_name"], data["last_name"], data.get("email"), data.get("phone"), data["password_hash"])
            ) as cur:
                user_id = cur.lastrowid

            await conn.execute("DELETE FROM pending_registrations WHERE id=?", (row["id"],))
            await conn.commit()
            async with conn.execute("SELECT * FROM users WHERE id=?", (user_id,)) as cur:
                user = await cur.fetchone()

        token = create_access_token(user_id, data.get("email"))
        return {"message": "Account created and verified successfully",
                "verified": True, "access_token": token,
                "token_type": "bearer", "user": user_to_dict(user)}

    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE users SET is_verified=1 WHERE email=? OR phone=?",
            (req.target, req.target)
        )
        await conn.commit()
    return {"message": "OTP verified successfully", "verified": True}


@app.post("/auth/forgot-password")
async def forgot_password(req: SendOtpRequest):
    # Normalize target — strip spaces
    target = req.target.strip()

    if not target:
        raise HTTPException(400, "Please enter your email or phone number")

    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT id FROM users WHERE email=? OR phone=?",
            (target, target)
        ) as cur:
            user = await cur.fetchone()

    if not user:
        raise HTTPException(
            404,
            "No account found with this email or phone. "
            "Please create an account first."
        )

    otp = generate_otp()
    await store_otp(target, otp, "forgot_password")
    if "@" in target:
        await send_otp_email(target, otp, "forgot_password")
    else:
        await send_otp_sms(target, otp)
    return {"message": "Password reset OTP sent"}


@app.post("/auth/reset-password")
async def reset_password(req: ResetPasswordRequest):
    # OTP was already verified on the verify-otp step.
    # We just need a valid reset_token to confirm the session.
    # Check if a verified (used=1) OTP exists for this target recently.
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("""
            SELECT id FROM otps
            WHERE target=? AND otp_code=? AND purpose='forgot_password'
              AND used=1
              AND created_at > datetime('now', '-15 minutes')
            ORDER BY created_at DESC LIMIT 1
        """, (req.target, req.otp_code)) as cur:
            row = await cur.fetchone()
    if not row:
        raise HTTPException(400, "Reset session expired. Please request a new OTP.")

    pw_hash = hash_password(req.new_password)
    async with aiosqlite.connect(DB_PATH) as conn:
        result = await conn.execute(
            "UPDATE users SET password_hash=?, updated_at=datetime('now') WHERE email=? OR phone=?",
            (pw_hash, req.target, req.target)
        )
        await conn.commit()
        if result.rowcount == 0:
            raise HTTPException(404, "User not found")
    return {"message": "Password reset successfully"}


@app.get("/auth/me")
async def get_me(user: dict = Depends(get_current_user)):
    u = dict(user)
    u.pop("password_hash", None)
    return u