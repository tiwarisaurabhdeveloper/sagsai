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

import os, sqlite3, logging, re, random, string, smtplib, bcrypt
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from typing import Optional
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, validator
from jose import JWTError, jwt

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
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c    = conn.cursor()
    c.executescript("""
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
    """)
    conn.commit()
    conn.close()
    logger.info("✅ Database ready: %s", DB_PATH)

# ── APP ───────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="SAGS AI Auth", version="2.0.0", lifespan=lifespan)
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
    target:   str
    otp_code: str
    purpose:  str

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

def store_otp(target: str, otp: str, purpose: str):
    conn = get_db()
    # Invalidate old OTPs for same target+purpose
    conn.execute("UPDATE otps SET used=1 WHERE target=? AND purpose=? AND used=0", (target, purpose))
    expires_at = (datetime.utcnow() + timedelta(minutes=OTP_TTL_MINUTES)).isoformat()
    conn.execute(
        "INSERT INTO otps (target, otp_code, purpose, expires_at) VALUES (?,?,?,?)",
        (target, otp, purpose, expires_at)
    )
    conn.commit()
    conn.close()

def validate_otp(target: str, otp_code: str, purpose: str) -> bool:
    conn = get_db()
    row  = conn.execute("""
        SELECT id FROM otps
        WHERE target=? AND otp_code=? AND purpose=? AND used=0
          AND expires_at > datetime('now')
        ORDER BY created_at DESC LIMIT 1
    """, (target, otp_code, purpose)).fetchone()
    if row:
        conn.execute("UPDATE otps SET used=1 WHERE id=?", (row["id"],))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

# ── EMAIL OTP ─────────────────────────────────────────────────────────────────
def send_otp_email(to_email: str, otp: str, purpose: str):
    """
    Send OTP email via SMTP.
    Uses SMTP_USER + SMTP_PASSWORD from .env
    For Gmail: create an App Password at https://myaccount.google.com/apppasswords
    """
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
        <!-- Header -->
        <tr>
          <td style="background:linear-gradient(135deg,#4f46e5,#0ea5e9);
                     padding:28px 40px;text-align:center;">
            <span style="font-size:24px;font-weight:800;color:white;
                         font-family:Arial,sans-serif;letter-spacing:-0.5px;">
              ✦ SAGS AI
            </span>
          </td>
        </tr>
        <!-- Body -->
        <tr>
          <td style="padding:40px;">
            <p style="font-size:16px;color:#374151;margin:0 0 8px;">
              Hello,
            </p>
            <p style="font-size:15px;color:#6b7280;margin:0 0 32px;line-height:1.6;">
              {'Here is your email verification code.' if purpose == 'verify'
               else 'Use this code to reset your password.' if purpose == 'forgot_password'
               else 'Use this code to sign in to SAGS AI.'}
            </p>
            <!-- OTP box -->
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
        <!-- Footer -->
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

    # ── DEV MODE: print OTP if SMTP not configured ──
    if not SMTP_USER or not SMTP_PASSWORD:
        logger.info("=" * 50)
        logger.info("⚡ DEV MODE — OTP for %s: %s", to_email, otp)
        logger.info("   Set SMTP_USER + SMTP_PASSWORD in .env to send real emails")
        logger.info("=" * 50)
        return

    # ── REAL EMAIL SEND ──
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"]      = subject
        msg["From"]         = f"{SMTP_FROM_NAME} <{SMTP_FROM_EMAIL}>"
        msg["To"]           = to_email
        msg["X-Priority"]   = "1"
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM_EMAIL, to_email, msg.as_bytes())

        logger.info("✅ OTP email sent to %s", to_email)

    except smtplib.SMTPAuthenticationError:
        logger.error("❌ SMTP auth failed — check SMTP_USER and SMTP_PASSWORD in .env")
        logger.info("   For Gmail, use an App Password (not your account password)")
        logger.info("   Generate at: https://myaccount.google.com/apppasswords")
        raise HTTPException(500, "Email authentication failed. Check server SMTP config.")
    except Exception as e:
        logger.error("❌ Email send error: %s", e)
        raise HTTPException(500, f"Failed to send OTP email: {e}")

def send_otp_sms(phone: str, otp: str):
    """Send OTP via Twilio SMS (optional)."""
    if TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM:
        try:
            from twilio.rest import Client
            client = Client(TWILIO_SID, TWILIO_TOKEN)
            client.messages.create(
                body=f"Your SAGS AI code: {otp}. Expires in {OTP_TTL_MINUTES} minutes.",
                from_=TWILIO_FROM,
                to=phone,
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
def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(bearer)):
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = decode_token(credentials.credentials)
    user_id = int(payload["sub"])
    conn    = get_db()
    user    = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
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

@app.post("/auth/register", response_model=TokenResponse)
async def register(req: RegisterRequest):
    if not req.email and not req.phone:
        raise HTTPException(400, "Provide at least an email or phone number")

    conn = get_db()
    if req.email:
        if conn.execute("SELECT id FROM users WHERE email=?", (req.email,)).fetchone():
            conn.close()
            raise HTTPException(400, "Email already registered")
    if req.phone:
        if conn.execute("SELECT id FROM users WHERE phone=?", (req.phone,)).fetchone():
            conn.close()
            raise HTTPException(400, "Phone already registered")

    pw_hash = hash_password(req.password)
    cursor  = conn.execute(
        "INSERT INTO users (first_name, last_name, email, phone, password_hash) VALUES (?,?,?,?,?)",
        (req.first_name, req.last_name, req.email, req.phone, pw_hash)
    )
    user_id = cursor.lastrowid
    conn.commit()
    user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()

    # Auto-send verification OTP if email provided
    if req.email:
        try:
            otp = generate_otp()
            store_otp(req.email, otp, "verify")
            send_otp_email(req.email, otp, "verify")
        except Exception as e:
            logger.warning("OTP send failed (non-fatal): %s", e)

    token = create_access_token(user_id, req.email)
    return {"access_token": token, "user": user_to_dict(user)}

@app.post("/auth/login", response_model=TokenResponse)
async def login(req: LoginRequest):
    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE email=? OR phone=?",
        (req.identifier.strip(), req.identifier.strip())
    ).fetchone()
    conn.close()

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

    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE google_id=? OR email=?", (google_id, email)
    ).fetchone()

    if user:
        conn.execute(
            "UPDATE users SET google_id=?, avatar_url=?, is_verified=1 WHERE id=?",
            (google_id, avatar, user["id"])
        )
        conn.commit()
        user = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    else:
        cursor = conn.execute(
            "INSERT INTO users (first_name, last_name, email, google_id, avatar_url, is_verified) VALUES (?,?,?,?,?,1)",
            (first, last, email, google_id, avatar)
        )
        user = conn.execute("SELECT * FROM users WHERE id=?", (cursor.lastrowid,)).fetchone()
        conn.commit()

    conn.close()
    token = create_access_token(user["id"], email)
    return {"access_token": token, "user": user_to_dict(user)}

@app.post("/auth/send-otp")
async def send_otp(req: SendOtpRequest):
    # For login purpose — verify user exists in DB first
    if req.purpose == "login":
        conn = get_db()
        user = conn.execute(
            "SELECT id FROM users WHERE email=? OR phone=?",
            (req.target, req.target)
        ).fetchone()
        conn.close()
        if not user:
            raise HTTPException(404, "No account found. Please register first.")

    otp = generate_otp()
    store_otp(req.target, otp, req.purpose)

    if "@" in req.target:
        send_otp_email(req.target, otp, req.purpose)
    else:
        send_otp_sms(req.target, otp)

    return {"message": f"OTP sent to {req.target}"}

@app.post("/auth/verify-otp")
async def verify_otp_route(req: VerifyOtpRequest):
    if not validate_otp(req.target, req.otp_code, req.purpose):
        raise HTTPException(400, "Invalid or expired OTP")
    conn = get_db()
    conn.execute(
        "UPDATE users SET is_verified=1 WHERE email=? OR phone=?",
        (req.target, req.target)
    )
    conn.commit()
    conn.close()
    return {"message": "OTP verified successfully", "verified": True}

@app.post("/auth/forgot-password")
async def forgot_password(req: SendOtpRequest):
    conn = get_db()
    user = conn.execute(
        "SELECT id FROM users WHERE email=? OR phone=?",
        (req.target, req.target)
    ).fetchone()
    conn.close()
    if not user:
        raise HTTPException(404, "No account found with this email or phone")

    otp = generate_otp()
    store_otp(req.target, otp, "forgot_password")

    if "@" in req.target:
        send_otp_email(req.target, otp, "forgot_password")
    else:
        send_otp_sms(req.target, otp)

    return {"message": "Password reset OTP sent"}

@app.post("/auth/reset-password")
async def reset_password(req: ResetPasswordRequest):
    if not validate_otp(req.target, req.otp_code, "forgot_password"):
        raise HTTPException(400, "Invalid or expired OTP")

    pw_hash = hash_password(req.new_password)
    conn    = get_db()
    result  = conn.execute(
        "UPDATE users SET password_hash=?, updated_at=datetime('now') WHERE email=? OR phone=?",
        (pw_hash, req.target, req.target)
    )
    conn.commit()
    conn.close()
    if result.rowcount == 0:
        raise HTTPException(404, "User not found")
    return {"message": "Password reset successfully"}

@app.get("/auth/me")
async def get_me(user: dict = Depends(get_current_user)):
    u = dict(user)
    u.pop("password_hash", None)
    return u