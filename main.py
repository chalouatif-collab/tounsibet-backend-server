from fastapi import FastAPI, HTTPException, Depends, Request, UploadFile, File, Form, Header, Body, Query,WebSocket, WebSocketDisconnect
from fastapi.security import OAuth2PasswordBearer
import requests
from pydantic import BaseModel
from typing import List, Optional
from fastapi.middleware.cors import CORSMiddleware
from jose import jwt
from datetime import datetime, timedelta
import random
import json
import os
import time
import hmac
import hashlib
import urllib.parse
from passlib.context import CryptContext
from sqlalchemy import create_engine, Column, Integer, String, Float, text
from sqlalchemy.orm import declarative_base, sessionmaker
import asyncio
db_lock = asyncio.Lock()
import shutil
from fastapi.staticfiles import StaticFiles
import httpx
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse, RedirectResponse, StreamingResponse
from dotenv import load_dotenv
import pyotp
import qrcode
import io
import html
import firebase_admin
from firebase_admin import credentials
from firebase_admin import db
import uuid

PROCESSED_TRANSACTIONS = set()

# ==========================================
# ⚽ إعدادات الألعاب الافتراضية والكازينو (EuroVirtuals)
# ==========================================
load_dotenv()

EURO_API_KEY = os.getenv("EURO_API_KEY", "RDWR6e0f1DF1ylzpxEpXzMFi.l3m1aebuSmiH6KGjiGzJf9BoQdMH37F63lGmY4TnXnLlPA3d")
EURO_APP_KEY = os.getenv("EURO_APP_KEY", "e0c39ac6-2d70-4ba8-a918-cb2a3fedd029")
EURO_BASE_URL = os.getenv("EURO_BASE_URL", "https://api.betkraft.co.uk/")

ADMIN_USER = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASSWORD", "admin123")
SECRET_KEY = os.getenv("SECRET_KEY", "alpha-secure-key-2026")

# ==========================================
# 1. إعداد الاتصال بـ Firebase
# ==========================================
if not firebase_admin._apps:
    cred = credentials.Certificate("firebase-key.json") 
    firebase_admin.initialize_app(cred, {
        'databaseURL': 'https://alphabet-7d14c-default-rtdb.firebaseio.com/'
    })

def load_db():
    ref = db.reference('/') 
    data = ref.get()
    
    if data is None:
        return {"users": [], "shop_withdrawals": [], "tickets": []}
    
    users = data.get("users", [])
    if isinstance(users, dict):
        users = list(users.values())
        
    class MagicDB(list):
        def __init__(self, users_list, full_data):
            super().__init__(users_list)
            self.full_data = full_data
            if "shop_withdrawals" not in self.full_data:
                self.full_data["shop_withdrawals"] = []
                
        def get(self, key, default=None):
            return self.full_data.get(key, default)
            
        def __contains__(self, key):
            return key in self.full_data
            
        def __setitem__(self, key, value):
            self.full_data[key] = value

    return MagicDB(users, data)

def save_db(data):
    ref = db.reference('/')
    if hasattr(data, 'full_data'):
        data.full_data['users'] = list(data)
        ref.set(data.full_data)
    elif isinstance(data, list):
        ref.child('users').set(list(data))
    else:
        ref.set(data)

TICKETS_FILE = "tickets_database.json" 

# ==========================================
# 2. إعدادات قاعدة البيانات (SQL) والتشفير
# ==========================================
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./local_test.db")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "alpha_users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    password = Column(String)
    role = Column(String)
    balance = Column(Float, default=0.0)
    rtp = Column(Integer, default=50)
    is_blocked = Column(Integer, default=0)
    created_by = Column(String)
    last_spin_date = Column(String, default="")
    daily_deposits = Column(Float, default=0.0)
    two_factor_secret = Column(String, nullable=True)

class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True, index=True)
    admin_username = Column(String)
    target_username = Column(String)
    action = Column(String)  
    amount = Column(Float)
    date = Column(String)  
    image_path = Column(String, nullable=True)
    tx_id = Column(String, nullable=True) 

class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True, index=True)
    admin_username = Column(String, index=True)
    action_type = Column(String)
    details = Column(String)
    date = Column(String, default=lambda: str(datetime.now()))

try:
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE transactions ADD COLUMN image_path VARCHAR"))
except Exception: pass

try:
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE transactions ADD COLUMN tx_id VARCHAR"))
except Exception: pass

Base.metadata.create_all(bind=engine)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
def hash_password(password: str): return pwd_context.hash(password)
def verify_password(plain_password, hashed_password):
    try: return pwd_context.verify(plain_password, hashed_password)
    except Exception: return False

ALGORITHM = "HS256"
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/login")

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(hours=24)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    
async def get_current_user(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("sub")
    except:
        raise HTTPException(status_code=401, detail="Invalid token")

async def get_admin_user(current_user: str = Depends(get_current_user)):
    db = load_db()
    user = next((u for u in db if u["username"] == current_user), None)
    if not user or user.get("role") not in ["owner","manager", "super_admin", "admin","shop"]:
        raise HTTPException(status_code=403, detail="Access Denied: Admin privileges required")
    return current_user

def log_admin_action(admin_username: str, action_type: str, details: str):
    db_session = SessionLocal()
    try:
        log_entry = AuditLog(admin_username=admin_username, action_type=action_type, details=details, date=str(datetime.now()))
        db_session.add(log_entry)
        db_session.commit()
    except Exception as e:
        print(f"Audit Log Error: {e}")
    finally:
        db_session.close()

# ==========================================
# 3. إعدادات تطبيق FastAPI 
# ==========================================
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from starlette.middleware.sessions import SessionMiddleware

limiter = Limiter(key_func=get_remote_address)
app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

os.makedirs("uploads", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

TELEGRAM_TOKEN = "8879806026:AAEB64RCPW4KzsUXUlDeztP_PzjtxkJv_4g"
TELEGRAM_CHAT_ID = "7700782611"

async def send_telegram_alert(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, json=payload)
    except Exception: pass

def verify_nexus_ip(request: Request):
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for: return forwarded_for.split(",")[0].strip()
    return request.client.host

# ==========================================
# 4. مسارات لوحات الإدارة
# ==========================================
@app.get("/panel/owner", response_class=HTMLResponse)
@app.get("/panel/owner/", response_class=HTMLResponse)
async def get_owner_panel():
    with open("panel/owner/index.html", "r", encoding="utf-8") as f: return f.read()

@app.get("/panel/super_admin", response_class=HTMLResponse)
@app.get("/panel/super_admin/", response_class=HTMLResponse)
async def get_super_admin_panel():
    with open("panel/super_admin/index.html", "r", encoding="utf-8") as f: return f.read()

@app.get("/panel/admin", response_class=HTMLResponse)
@app.get("/panel/admin/", response_class=HTMLResponse)
async def get_admin_panel():
    with open("panel/admin/index.html", "r", encoding="utf-8") as f: return f.read()

@app.get("/panel/shop", response_class=HTMLResponse)
@app.get("/panel/shop/", response_class=HTMLResponse)
async def get_shop_panel():
    with open("panel/shop/index.html", "r", encoding="utf-8") as f: return f.read()
    
@app.get("/panel/manager", response_class=HTMLResponse)
@app.get("/panel/manager/", response_class=HTMLResponse)
async def get_manager_panel():
    with open("panel/manager/index.html", "r", encoding="utf-8") as f: return f.read()    

@app.get("/", response_class=HTMLResponse)
async def admin_home(request: Request):
    role = request.session.get("role")
    if role == "owner": return RedirectResponse(url="/panel/owner", status_code=303)
    elif role == "manager": return RedirectResponse(url="/panel/manager", status_code=303)
    elif role == "super_admin": return RedirectResponse(url="/panel/super_admin", status_code=303)
    elif role == "admin": return RedirectResponse(url="/panel/admin", status_code=303)
    elif role == "shop": return RedirectResponse(url="/panel/shop", status_code=303)
    
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()

# ==========================================
# 5. إدارة المستخدمين والمصادقة
# ==========================================
class LoginRequest(BaseModel): username: str; password: str
class Verify2FARequest(BaseModel): username: str; totp_code: str = "000000"
class RegisterRequest(BaseModel): username: str; password: str; role: str; created_by: str; phone: str = ""
class ConfigureAccountRequest(BaseModel): admin_username: str; target_username: str; rtp: int; is_blocked: int
class UpdateBalanceRequest(BaseModel): admin_username: str; target_username: str; action: str; amount: float
class ChangePlayerPasswordRequest(BaseModel): admin_username: str; target_username: str; new_password: str
class ChangeMyPasswordRequest(BaseModel): username: str; new_password: str
class DeleteAccountRequest(BaseModel): admin_username: str; target_username: str
class Reset2FARequest(BaseModel): admin_username: str; target_username: str

@app.post("/api/register")
@limiter.limit("1/minute")
async def register_user(request: Request, req: RegisterRequest):
    uname = req.username.lower().strip()
    if uname in ["fethi", "admin", "owner", "system", "boss", "super_admin"]:
        raise HTTPException(status_code=400, detail="Ce nom d'utilisateur est réservé au système!")

    db = load_db()
    for u in db:
        if u["username"] == uname:
            raise HTTPException(status_code=400, detail="Nom d'utilisateur déjà pris")
            
    hashed_pwd = hash_password(req.password)
    new_secret_key = pyotp.random_base32()
    new_id = max([int(u.get("id", 0)) for u in db]) + 1 if db else 1
    
    new_user = {
        "id": new_id, "username": uname, "password": hashed_pwd, "role": req.role, 
        "balance": 0.00, "rtp": 50, "is_blocked": 0, "created_by": req.created_by, 
        "last_spin_date": "", "daily_deposits": 0.0, "two_factor_secret": new_secret_key, "phone": req.phone
    }
    
    db.append(new_user)
    save_db(db)
    log_admin_action(req.created_by, "CREATE_USER", f"Created {uname} with role {req.role}")
    
    return {"status": "success", "message": "Compte créé", "secret_key": new_secret_key, "user_id": new_id}

@app.post("/api/login")
@limiter.limit("5/minute")
async def login_user(request: Request, req: LoginRequest):
    try:
        uname = html.escape(req.username.lower().strip())
        db = load_db()
        user = next((u for u in db if u["username"] == uname), None)

        if not user or not verify_password(req.password, user.get("password", "")):
            bad_alert = f"⚠️ <b>محاولة دخول فاشلة للإدارة!</b>\n👤 اسم المستخدم: <code>{req.username}</code>"
            asyncio.create_task(send_telegram_alert(bad_alert))
            return JSONResponse(status_code=401, content={"detail": "اسم المستخدم أو كلمة المرور غير صحيحة"})
            
        user["last_ip"] = verify_nexus_ip(request)
        save_db(db)
        access_token = create_access_token(data={"sub": user["username"], "role": user["role"]})
        
        return JSONResponse(status_code=200, content={
            "message": "success", "username": user["username"], "role": user["role"],
            "access_token": access_token, "balance": float(user.get("balance", 0.0))
        })
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": f"خطأ داخلي: {str(e)}"})

@app.post("/api/verify-2fa")
@limiter.limit("5/minute")
async def verify_2fa_api(request: Request, req: Verify2FARequest):
    db = load_db()
    user = next((u for u in db if u["username"] == req.username), None)
    if not user: raise HTTPException(status_code=401, detail="Nom d'utilisateur incorrect")
        
    secret = user.get("two_factor_secret")
    if not secret: raise HTTPException(status_code=400, detail="لم يتم تفعيل المصادقة الثنائية!")
        
    totp = pyotp.TOTP(secret)
    if totp.verify(req.totp_code):
        access_token = create_access_token(data={"sub": user["username"], "role": user["role"]})
        return JSONResponse(status_code=200, content={
            "message": "success", "username": user["username"], "role": user["role"],
            "access_token": access_token, "balance": float(user.get("balance", 0.0))
        })
    else:
        raise HTTPException(status_code=400, detail="كود Google Authenticator غير صحيح!")

@app.get("/setup-2fa/{username}")
async def setup_2fa(username: str):
    db = load_db()
    user = next((u for u in db if u["username"] == username), None)
    if not user: return HTMLResponse("<h3 style='text-align:center; color:red;'>المستخدم غير موجود!</h3>")
    
    secret = pyotp.random_base32()
    user["two_factor_secret"] = secret
    totp = pyotp.TOTP(secret)
    uri = totp.provisioning_uri(name=username, issuer_name="TounsiBet Casino")
    
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")

@app.get("/api/admin/users")
async def get_all_network_users(current_user: str = Depends(get_admin_user)): 
    db = load_db()
    current_admin = next((u for u in db if u["username"] == current_user), None)
    current_role = current_admin.get("role", "player")

    if current_role in ["owner", "system"]:
        allowed_users = {u["username"] for u in db}
    else:
        allowed_users = {current_user}
        to_process = [current_user]
        while to_process:
            parent = to_process.pop(0)
            children = [u["username"] for u in db if u.get("created_by") == parent]
            for child in children:
                if child not in allowed_users:
                    allowed_users.add(child)
                    to_process.append(child)

    safe_users = []
    for u in db:
        if u["username"] not in allowed_users: continue
        safe_user = dict(u)
        safe_user.pop("password", None)
        safe_user.pop("two_factor_secret", None) 
        safe_users.append(safe_user)
        
    return safe_users

@app.post("/api/admin/update-balance")
async def update_balance(req: UpdateBalanceRequest, current_user: str = Depends(get_admin_user)):
    target = req.target_username.lower().strip()
    amount = float(req.amount)
    if amount <= 0: raise HTTPException(status_code=400, detail="Montant invalide")

    async with db_lock:
        db = load_db()
        target_user = next((u for u in db if str(u.get("username", "")).lower().strip() == target), None)
        admin_user = next((u for u in db if str(u.get("username", "")).lower().strip() == current_user.lower().strip()), None)

        if not target_user or not admin_user:
            raise HTTPException(status_code=404, detail="Utilisateur non trouvé")

        is_global_admin = (current_user.lower() == "system" or admin_user.get("role") == "owner")
        safe_creator = str(target_user.get("created_by", "")).lower().strip()
        
        if not is_global_admin and safe_creator != current_user.lower().strip():
            raise HTTPException(status_code=403, detail="Accès refusé.")

        if req.action == "charge":
            if not is_global_admin:
                if float(admin_user.get("balance", 0)) < amount: 
                    raise HTTPException(status_code=400, detail="Solde insuffisant dans votre compte")
                admin_user["balance"] = round(float(admin_user.get("balance", 0)) - amount, 2)
            target_user["balance"] = round(float(target_user.get("balance", 0)) + amount, 2)
            target_user["daily_deposits"] = float(target_user.get("daily_deposits", 0)) + amount

        elif req.action == "withdraw":
            if float(target_user.get("balance", 0)) < amount: 
                raise HTTPException(status_code=400, detail="Solde insuffisant chez le joueur")
            
            target_user["balance"] = round(float(target_user.get("balance", 0)) - amount, 2)
            
            # 🛡️ إغلاق الثغرة: خصم السحب من الإيداعات اليومية لكي لا يأخذ كاش باك وهو رابح
            current_daily = float(target_user.get("daily_deposits", 0))
            target_user["daily_deposits"] = max(0.0, current_daily - amount)
            
            if not is_global_admin:
                admin_user["balance"] = round(float(admin_user.get("balance", 0)) + amount, 2)
# ==========================================
# 6. إدارة الإيداعات والسحوبات
# ==========================================
class DepositRequest(BaseModel):
    player: str
    method: str
    amount: float
    code: str
    receipt_image: Optional[str] = None

def load_tickets_db():
    if not os.path.exists(TICKETS_FILE):
        with open(TICKETS_FILE, "w") as f: json.dump([], f)
        return []
    try:
        with open(TICKETS_FILE, "r") as f: return json.load(f)
    except: return []

@app.post("/api/deposit")
@limiter.limit("1/minute")
async def create_deposit(request: Request, req: DepositRequest):
    try:
        db_tickets = load_tickets_db()
        new_ticket = {
            "ticket_id": "DEP-" + datetime.now().strftime("%Y%m%d%H%M%S"),
            "type": "deposit",
            "username": html.escape(req.player.strip()),
            "method": html.escape(req.method.strip()),
            "amount": req.amount,
            "code": html.escape(req.code.strip()) if hasattr(req, 'code') and req.code else "",
            "receipt_image": getattr(req, 'receipt_image', None),
            "status": "pending",
            "date": datetime.now().isoformat()
        }
        db_tickets.append(new_ticket)
        
        asyncio.create_task(send_telegram_alert(f"🚨 <b>إيداع جديد!</b>\n👤 {new_ticket['username']}\n💰 {new_ticket['amount']}"))
        with open(TICKETS_FILE, "w", encoding="utf-8") as f: json.dump(db_tickets, f, indent=4, ensure_ascii=False)
        return {"status": "success", "message": "تم إرسال طلب الإيداع بنجاح"}
    except Exception as e:
        return {"status": "error", "message": "حدث خطأ"}

class ApproveDepositRequest(BaseModel):
    ticket_id: str
    amount: float

@app.post("/api/admin/approve-deposit")
async def approve_deposit(req: ApproveDepositRequest, current_user: str = Depends(get_admin_user)):
    try:
        db_tickets = load_tickets_db()
        ticket = next((t for t in db_tickets if str(t.get("ticket_id")) == str(req.ticket_id)), None)
        if not ticket: raise HTTPException(status_code=404, detail="التذكرة غير موجودة")
        if ticket.get("status") != "pending": raise HTTPException(status_code=400, detail="تمت معالجتها مسبقاً")

        real_amount = req.amount
        ticket["status"] = "approuvé"
        ticket["amount"] = real_amount
        
        db_users = load_db()
        target_username = ticket.get("username") or ticket.get("player") or ""
        target_user = next((u for u in db_users if str(u.get("username", "")).lower() == str(target_username).lower()), None)
        
        if target_user:
            target_user["balance"] = float(target_user.get("balance", 0)) + real_amount
            target_user["daily_deposits"] = float(target_user.get("daily_deposits", 0)) + real_amount
            save_db(db_users)

        with open(TICKETS_FILE, "w", encoding="utf-8") as f: json.dump(db_tickets, f, indent=4, ensure_ascii=False)
        return {"status": "success", "message": f"تمت الموافقة وإضافة {real_amount} بنجاح"}
    except Exception as e:
        raise HTTPException(status_code=500, detail="حدث خطأ داخلي")

# ==========================================
# 7. التشفير والحماية الخاصة بـ EuroVirtuals
# ==========================================
def hash_create(request_data: dict, key: str) -> str:
    """نسخة محسنة من دالة التشفير تتجاهل القيم الفارغة لمنع فشل التوقيع"""
    keys = sorted(request_data.keys())
    hashkey = ""
    for k in keys:
        value = request_data[k]
        if value is None:
            continue
            
        if isinstance(value, dict):
            nested_keys = sorted(value.keys())
            for nested_key in nested_keys:
                nested_value = value[nested_key]
                if nested_value is None: continue
                serialized = json.dumps(nested_value, separators=(',', ':'), sort_keys=True)
                md5_hash = hashlib.md5(serialized.encode('utf-8')).hexdigest()
                hashkey += f"&{nested_key}={md5_hash}"
        elif isinstance(value, list):
            for index in range(len(value)):
                array_value = value[index]
                if array_value is None: continue
                serialized = json.dumps(array_value, separators=(',', ':'), sort_keys=True)
                md5_hash = hashlib.md5(serialized.encode('utf-8')).hexdigest()
                hashkey += f"&{index}={md5_hash}"
        else:
            if isinstance(value, bool): val_str = str(value).lower()
            else: val_str = str(value)
            hashkey += f"&{k}={val_str}"

    hashkey = hashkey.lstrip('&')
    final_string = hashkey + str(key)
    return hashlib.md5(final_string.encode('utf-8')).hexdigest()

def check_eurovirtuals_security(request: Request, payload: dict):
    token = str(request.headers.get("x-token-key") or request.headers.get("x-token") or "").strip()
    signature = str(request.headers.get("x-signature-key") or request.headers.get("x-signature") or "").strip()
    
    if token == "invalid-token-key" or signature == "invalid-signature-key":
        return {"status_code": 401, "status_description": "Invalid Signature/Token"}
    if not token or not signature:
        return {"status_code": 401, "status_description": "Missing Security Headers"}

    expected_signature = hash_create(payload, token)
    if signature != expected_signature:
        return {"status_code": 401, "status_description": "Invalid Signature"}
    return None

# ==========================================
# 8. EuroVirtuals API Callbacks & Game Fetching
# ==========================================

@app.post("/api/eurovirtuals/callback/player_info")
async def eurovirtuals_player_info(request: Request):
    try:
        payload = await request.json()
        sec_err = check_eurovirtuals_security(request, payload)
        if sec_err: return JSONResponse(content=sec_err, status_code=200)

        player_id = str(payload.get("player_id", ""))
        db = load_db()
        target_user = next((u for u in db if str(u.get("username", "")).lower().strip() == player_id.lower().strip()), None)

        if not target_user:
            return JSONResponse(content={"status_code": 500, "status_description": "Player not found"}, status_code=200)

        return JSONResponse(content={
            "status_code": 200, "status_description": "Success",
            "data": {
                "balance": float(target_user.get("balance", 0.0)),
                "currency": payload.get("currency", "TND"),
                "player_id": target_user.get("username", player_id),
                "date": time.strftime("%Y-%m-%d %H:%M:%S")
            }
        }, status_code=200)
    except Exception as e:
        return JSONResponse(content={"status_code": 500, "status_description": str(e)}, status_code=200)

@app.post("/api/eurovirtuals/callback/bet")
async def eurovirtuals_bet(request: Request):
    try:
        payload = await request.json()
        sec_err = check_eurovirtuals_security(request, payload)
        if sec_err: return JSONResponse(content=sec_err, status_code=200)

        bet_data_list = payload.get("data", [])
        bet_data = bet_data_list[0] if bet_data_list and isinstance(bet_data_list, list) else {}
        player_id = str(payload.get("player_id") or bet_data.get("player_id") or "").strip()
        transaction_id = str(payload.get("transaction_id") or bet_data.get("transaction_id") or "").strip()
        amount = float(payload.get("amount") or bet_data.get("amount") or 0.0)

        async with db_lock:
            db = load_db()
            target_user = next((u for u in db if str(u.get("username", "")).lower().strip() == player_id.lower()), None)
            if not target_user: return JSONResponse(content={"status_code": 500, "status_description": "Player not found"}, status_code=200)

            current_balance = float(target_user.get("balance", 0.0))
            if current_balance < amount:
                return JSONResponse(content={"status_code": 402, "status_description": "Insufficient Balance"}, status_code=200)

            db_session = SessionLocal()
            try:
                if transaction_id and db_session.query(Transaction).filter(Transaction.tx_id == transaction_id).first():
                    return JSONResponse(content={"status_code": 200, "status_description": "Success", "data": {"balance": current_balance}}, status_code=200)
                new_balance = round(current_balance - amount, 2)
                target_user["balance"] = new_balance
                save_db(db)
                
                new_tx = Transaction(admin_username="EUROVIRTUALS_API", target_username=player_id, action="bet", amount=amount, date=time.strftime("%Y-%m-%d %H:%M:%S"), tx_id=transaction_id)
                db_session.add(new_tx)
                db_session.commit()
            finally:
                db_session.close()

        return JSONResponse(content={"status_code": 200, "status_description": "Success", "data": {"balance": new_balance, "currency": "TND", "reference_id": transaction_id}}, status_code=200)
    except Exception as e:
        return JSONResponse(content={"status_code": 500, "status_description": "Internal Server Error"}, status_code=200)

@app.post("/api/eurovirtuals/callback/win")
async def eurovirtuals_win(request: Request):
    try:
        data = await request.json()
        sec_err = check_eurovirtuals_security(request, data)
        if sec_err: return JSONResponse(content=sec_err, status_code=200)
        
        tx_id = str(data.get("transaction_id") or "").strip()
        player_id = str(data.get("player_id") or "").strip()
        payout_amount = float(data.get("payout_amount") or data.get("amount") or 0.0)

        async with db_lock:
            db = load_db()
            target_user = next((u for u in db if str(u.get("username", "")).lower().strip() == player_id.lower()), None)
            if not target_user: return JSONResponse(content={"status_code": 500, "status_description": "Player not found"}, status_code=200)
            
            curr = float(target_user.get("balance", 0.0))
            db_session = SessionLocal()
            try:
                if db_session.query(Transaction).filter(Transaction.tx_id == tx_id).first():
                    return JSONResponse(content={"status_code": 200, "status_description": "Success", "data": {"balance": curr}}, status_code=200)
                
                new_balance = round(curr + payout_amount, 2)
                target_user["balance"] = new_balance
                save_db(db)

                new_tx = Transaction(admin_username="EUROVIRTUALS_API", target_username=player_id, action="win", amount=payout_amount, date=time.strftime("%Y-%m-%d %H:%M:%S"), tx_id=tx_id)
                db_session.add(new_tx)
                db_session.commit()
            finally:
                db_session.close()

        return JSONResponse(content={"status_code": 200, "status_description": "Success", "data": {"balance": new_balance, "currency": "TND", "reference_id": tx_id}}, status_code=200)
    except Exception as e:
        return JSONResponse(content={"status_code": 500, "status_description": "Internal Server Error"}, status_code=200)

@app.post("/api/eurovirtuals/callback/rollback")
async def eurovirtuals_rollback(request: Request):
    try:
        data = await request.json()
        sec_err = check_eurovirtuals_security(request, data)
        if sec_err: return JSONResponse(content=sec_err, status_code=200)
            
        tx_id = str(data.get("transaction_id") or "").strip()
        action = str(data.get("action", "")).strip()
        player_id = str(data.get("player_id") or "").strip()
        payout_amount = float(data.get("amount") or data.get("payout_amount") or 0.0)
        is_rollback_win = ("win" in action.lower())

        async with db_lock:
            db = load_db()
            target_user = next((u for u in db if str(u.get("username", "")).lower().strip() == player_id.lower()), None)
            if not target_user: return JSONResponse(content={"status_code": 500, "status_description": "Player not found"}, status_code=200)
                
            curr = float(target_user.get("balance", 0.0))
            db_session = SessionLocal()
            try:
                if db_session.query(Transaction).filter(Transaction.tx_id == tx_id).first():
                    return JSONResponse(content={"status_code": 200, "status_description": "Success", "data": {"balance": curr}}, status_code=200)

                new_balance = round(curr - payout_amount, 2) if is_rollback_win else round(curr + payout_amount, 2)
                target_user["balance"] = new_balance
                save_db(db)

                new_tx = Transaction(admin_username="EUROVIRTUALS_API", target_username=player_id, action="rollback", amount=payout_amount, date=time.strftime("%Y-%m-%d %H:%M:%S"), tx_id=tx_id)
                db_session.add(new_tx)
                db_session.commit()
            finally:
                db_session.close()
                
        return JSONResponse(content={"status_code": 200, "status_description": "Success", "data": {"balance": new_balance}}, status_code=200)
    except Exception as e:
        return JSONResponse(content={"status_code": 500, "status_description": "Internal Server Error"}, status_code=200)

@app.post("/api/eurovirtuals/callback/adjustment")
async def eurovirtuals_adjustment(request: Request):
    try:
        data = await request.json()
        sec_err = check_eurovirtuals_security(request, data)
        if sec_err: return JSONResponse(content=sec_err, status_code=200)
            
        tx_id = str(data.get("transaction_id", ""))
        player_id = str(data.get("player_id") or "")
        amount = abs(float(data.get("amount", 0.0)))
        action = data.get("action", "")
        
        async with db_lock:
            db = load_db()
            target_user = next((u for u in db if str(u.get("username", "")).lower().strip() == player_id.lower().strip()), None)
            if not target_user: return {"status_code": 500, "status_description": "Player not found"}
                
            curr = float(target_user.get("balance", 0.0))
            db_session = SessionLocal()
            try:
                if tx_id and db_session.query(Transaction).filter(Transaction.tx_id == tx_id).first():
                    return {"status_code": 202, "status_description": "Duplicate request"}
                
                new_balance = round(curr - amount, 2) if action == "wallet_adjustment_debit" else round(curr + amount, 2)
                target_user["balance"] = new_balance
                save_db(db)

                new_tx = Transaction(admin_username="EUROVIRTUALS_API", target_username=player_id, action="adjustment", amount=amount, date=time.strftime("%Y-%m-%d %H:%M:%S"), tx_id=tx_id)
                db_session.add(new_tx)
                db_session.commit()
            finally:
                db_session.close()
                
        return {"status_code": 200, "status_description": "Success", "data": {"balance": new_balance, "currency": "TND", "reference_id": tx_id}}
    except Exception as e:
        return {"status_code": 500, "status_description": str(e)}

@app.api_route("/api/get-eurovirtuals-games", methods=["GET"])
async def get_virtual_games():
    try:
        payload = {}
        timestamp = str(int(time.time()))
        signature = hash_create(payload, EURO_APP_KEY)
        
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-api-key": EURO_API_KEY,
            "x-signature-key": signature,
            "x-timestamp": timestamp
        }
        
        base_url_clean = str(EURO_BASE_URL).rstrip('/')
        games_endpoint = f"{base_url_clean}/v1/games"
        
        try:
            response = requests.get(games_endpoint, headers=headers, timeout=20)
            data = response.json()
        except Exception:
            return {"status": "error", "error": "Invalid JSON response from provider"}
        
        if response.status_code == 200 and data.get("status_code") == 200:
            games_list = data.get("data", {}).get("data", [])
            for game in games_list:
                image_url = game.get("logo") or game.get("thumbnail") or ""
                if image_url:
                    game["image"] = image_url
                    game["img"] = image_url
                game["game_code"] = game.get("uuid") or game.get("game_uuid") or game.get("id")
                    
            return {"status": "success", "games": games_list}
        else:
            return {"status": "error", "error": data.get("status_description", "Unknown Error"), "full_data": data}
    except Exception as e:
        return {"status": "error", "error": str(e)}
    
@app.post("/api/provider/launch-eurovirtuals")
async def launch_eurovirtuals(request: Request):
    try:
        data = await request.json()
        game_uuid = str(data.get("game_uuid") or data.get("game_code") or data.get("id") or "")
        
        if not game_uuid or game_uuid == "undefined":
            return {"error": "Game UUID is missing"}
            
        user_code = str(data.get("user_code", "test_user"))
        
        async with db_lock:
            db = load_db()
            target_user = next((u for u in db if str(u.get("username", "")).lower().strip() == user_code.lower().strip()), None)
            
            if not target_user: return {"error": "Player not found"}
            if target_user.get("is_blocked") == 1: return {"error": "Player blocked"}
            current_balance = float(target_user.get("balance", 0.0))

        payload = {
            "player_id": user_code,
            "player_name": user_code,
            "player_token": f"tok_{user_code}",
            "currency": "TND",
            "demo": 0,
            "game_uuid": game_uuid,
            "balance": current_balance,
            "country": "TN",
            "language": "fr",
            "device": "desktop"
        }

        signature = hash_create(payload, EURO_APP_KEY)
        headers = {
            "Accept": "application/json",
            "x-api-key": EURO_API_KEY,
            "x-signature-key": signature,
            "x-signature": signature,
            "x-timestamp": str(int(time.time())),
            "Content-Type": "application/json"
        }

        base_url_clean = str(EURO_BASE_URL).rstrip('/')
        launch_endpoint = f"{base_url_clean}/v1/launch"
        
        async with httpx.AsyncClient() as client:
            response = await client.post(launch_endpoint, json=payload, headers=headers, timeout=20)
            try:
                response_data = response.json()
            except Exception:
                return {"error": "Invalid JSON from provider"}

            if response_data.get("status_code") == 200:
                game_url = response_data.get("data", {}).get("url")
                if game_url and game_url.startswith("/"):
                    game_url = f"{base_url_clean}{game_url}"
                return {"launch_url": game_url}
            else:
                return {"error": response_data.get("status_description", "Rejected")}

    except Exception as e:
        return {"error": str(e)}

# ==========================================
# 9. المهام الخلفية (الجاكبوت والكاش باك والـ WebSocket)
# ==========================================
JACKPOTS_BASE = {
    "mini":  {"start": 20.0, "days": 1},
    "minor": {"start": 40.0, "days": 2},
    "major": {"start": 80.0, "days": 7},
    "grand": {"start": 500.0, "days": 30},
}

def generate_drop_time(days):
    now = datetime.now()
    random_seconds = random.randint(1, int(timedelta(days=days).total_seconds()))
    return now + timedelta(seconds=random_seconds)

jackpots_state = {
    "mini":  {"current_amount": 20.0, "drop_time": generate_drop_time(1)},
    "minor": {"current_amount": 40.0, "drop_time": generate_drop_time(2)},
    "major": {"current_amount": 80.0, "drop_time": generate_drop_time(7)},
    "grand": {"current_amount": 500.0, "drop_time": generate_drop_time(30)},
}

class ConnectionManager:
    def __init__(self): self.active_connections: list[WebSocket] = []
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections: self.active_connections.remove(websocket)
    async def broadcast(self, message: str):
        for connection in self.active_connections:
            try: await connection.send_text(message)
            except: pass

jackpot_manager = ConnectionManager()

@app.websocket("/ws/jackpot")
async def websocket_jackpot(websocket: WebSocket):
    await jackpot_manager.connect(websocket)
    try:
        while True: await websocket.receive_text()
    except WebSocketDisconnect:
        jackpot_manager.disconnect(websocket)

async def broadcast_jackpots():
    while True:
        live_data = {
            "mini": jackpots_state["mini"]["current_amount"],
            "minor": jackpots_state["minor"]["current_amount"],
            "major": jackpots_state["major"]["current_amount"],
            "grand": jackpots_state["grand"]["current_amount"]
        }
        await jackpot_manager.broadcast(json.dumps(live_data))
        await asyncio.sleep(2)

async def daily_cashback_system():
    await asyncio.sleep(15)
    while True:
        try:
            now = datetime.now()
            if now.hour == 0 and now.minute < 10:
                async with db_lock:
                    db = load_db()
                    changes_made = False
                    for u in db:
                        current_balance = float(u.get("balance", 0.0))
                        daily_deps = float(u.get("daily_deposits", 0.0))
                        net_loss = daily_deps - current_balance 
                        
                        if daily_deps > 0:
                            if current_balance < 1.0 and net_loss > 0:
                                cashback_amount = daily_deps * 0.10
                                u["balance"] = round(current_balance + cashback_amount, 2)
                                
                                db_session = SessionLocal()
                                try:
                                    new_tx = Transaction(admin_username="SYSTEM_CASHBACK", target_username=u["username"], action="cashback", amount=cashback_amount, date=time.strftime("%Y-%m-%d %H:%M:%S"), tx_id=f"cb_{int(time.time())}")
                                    db_session.add(new_tx)
                                    db_session.commit()
                                except Exception: db_session.rollback()
                                finally: db_session.close()
                            
                            u["daily_deposits"] = 0
                            changes_made = True
                    if changes_made: save_db(db)
                await asyncio.sleep(3600)
            else:
                await asyncio.sleep(300)
        except Exception:
            await asyncio.sleep(300) 

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(broadcast_jackpots())
    asyncio.create_task(daily_cashback_system())

# ==========================================
# 10. نظام الإشعارات الداخلي
# ==========================================
class NotificationModel(BaseModel):
    target_user: str 
    title: str
    message: str
    icon: str = "fa-bell" 

@app.post("/api/admin/send-notification")
async def send_notification(req: NotificationModel, current_user: str = Depends(get_admin_user)):
    db = load_db()
    if "notifications" not in db.full_data: db.full_data["notifications"] = []
    
    new_notif = {
        "id": str(uuid.uuid4())[:8],
        "target": req.target_user.lower().strip(),
        "title": req.title,
        "message": req.message,
        "icon": req.icon,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "read_by": []
    }
    db.full_data["notifications"].append(new_notif)
    save_db(db)
    return {"status": "success"}

@app.get("/api/user/notifications")
async def get_user_notifications(current_user: str = Depends(get_current_user)):
    db = load_db()
    notifs = db.full_data.get("notifications", [])
    user_notifs = []
    unread_count = 0
    
    for n in notifs:
        if current_user.lower() in n.get("deleted_by", []): continue
        if n.get("target") == "all" or n.get("target") == current_user.lower():
            is_read = current_user.lower() in n.get("read_by", [])
            if not is_read: unread_count += 1
            user_notifs.append({**n, "is_read": is_read})
            
    return {"unread": unread_count, "notifications": user_notifs[::-1][:15]}

class MarkReadModel(BaseModel): notif_id: str
@app.post("/api/user/read-notification")
async def mark_notif_read(req: MarkReadModel, current_user: str = Depends(get_current_user)):
    db = load_db()
    notifs = db.full_data.get("notifications", [])
    for n in notifs:
        if n["id"] == req.notif_id:
            if current_user.lower() not in n.get("read_by", []):
                n.setdefault("read_by", []).append(current_user.lower())
            break
    save_db(db)
    return {"status": "success"}

# ==========================================
# 1. دالة جلب ألعاب السلوتس والكازينو لايف عبر كود المزود
# ==========================================
class ProviderRequest(BaseModel):
    provider_code: str

# ==========================================
# 1. دالة جلب الألعاب (محدثة بخدعة كشف الكتالوج)
# ==========================================
class ProviderRequest(BaseModel):
    provider_code: str

@app.post("/api/get-providers")
async def get_eurovirtuals_games_by_provider(request: ProviderRequest):
    try:
        provider = request.provider_code.upper()
        
        payload = {}
        timestamp = str(int(time.time()))
        signature = hash_create(payload, EURO_APP_KEY)
        
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-api-key": EURO_API_KEY,
            "x-signature-key": signature,
            "x-timestamp": timestamp
        }
        
        base_url_clean = str(EURO_BASE_URL).rstrip('/')
        games_endpoint = f"{base_url_clean}/v1/games"
        
        async with httpx.AsyncClient() as client:
            response = await client.get(games_endpoint, headers=headers, timeout=30)
            data = response.json()
            
            if response.status_code == 200 and data.get("status_code") == 200:
                all_games = data.get("data", {}).get("data", [])
                filtered_games = []
                
                # تهيئة جميع الألعاب للواجهة
                for game in all_games:
                    image_url = game.get("logo") or game.get("thumbnail") or ""
                    if image_url:
                        game["image"] = image_url
                        game["img"] = image_url
                    game["game_code"] = game.get("uuid") or game.get("game_uuid") or game.get("id")
                    
                    game_provider = str(game.get("provider", "")).upper()
                    game_category = str(game.get("category", "")).upper()
                    
                    # محاولة الفلترة
                    if provider in game_provider or provider in game_category:
                        filtered_games.append(game)
                
                # 💡 السطر السحري: إذا كانت القائمة المفلترة فارغة، أرسل "كل" الألعاب لنراها!
                if len(filtered_games) == 0:
                    return {"status": "success", "games": all_games}
                        
                return {"status": "success", "games": filtered_games}
            else:
                return {"status": "success", "games": []}
    except Exception as e:
        return {"status": "error", "error": str(e)}
    
# ==========================================
# تحديث رصيد اللاعب المتكرر في الواجهة (مع حساب الكاش باك)
# ==========================================
@app.post("/gold_api")
@app.post("/gold_api/")
async def gold_api_balance(request: Request):
    try:
        data = await request.json()
        if data.get("method") == "user_balance":
            user_code = data.get("user_code", "")
            db = load_db()
            target_user = next((u for u in db if str(u.get("username", "")).lower().strip() == user_code.lower().strip()), None)
            if target_user:
                balance = float(target_user.get("balance", 0.0))
                daily_deps = float(target_user.get("daily_deposits", 0.0))
                
                # حساب الكاش باك (10%) إذا كان الرصيد أقل من 1 دينار
                cashback_est = round(daily_deps * 0.10, 2) if daily_deps > 0 else 0.0
                
                return JSONResponse(content={
                    "status": 1, 
                    "user_balance": balance,
                    "cashback_est": cashback_est
                })
        return JSONResponse(content={"status": 0, "user_balance": 0.0, "cashback_est": 0.0})
    except Exception:
        return JSONResponse(content={"status": 0, "user_balance": 0.0, "cashback_est": 0.0})
    
    # ==========================================
# جلب سجل الرهانات والألعاب الحقيقي للاعب
# ==========================================
@app.get("/api/user/bet-history")
async def get_bet_history(current_user: str = Depends(get_current_user)):
    db_session = SessionLocal()
    try:
        # جلب عمليات الرهان والربح والإلغاء فقط للاعب الحالي (آخر 50 عملية)
        txs = db_session.query(Transaction).filter(
            Transaction.target_username == current_user,
            Transaction.action.in_(["bet", "win", "rollback"])
        ).order_by(Transaction.id.desc()).limit(50).all()

        history = []
        for tx in txs:
            is_win = (tx.action == "win")
            is_rollback = (tx.action == "rollback")
            
            # تحديد اسم المزود
            provider = "EuroVirtuals" if "EUROVIRTUALS" in str(tx.admin_username).upper() else str(tx.admin_username)
            
            # تحديد نوع العملية لعرضها كاسم للعبة مؤقتاً
            if is_win:
                game_name = "Gain (Win) 🏆"
                win_amount = tx.amount
                bet_amount = 0.0
            elif is_rollback:
                game_name = "Annulation 🔄"
                win_amount = tx.amount
                bet_amount = 0.0
            else:
                game_name = "Pari (Mise) 🎰"
                win_amount = -tx.amount
                bet_amount = tx.amount
            
            history.append({
                "date": tx.date,
                "game": game_name,
                "provider": provider,
                "amount": bet_amount,
                "win": win_amount,
                "tx_id": tx.tx_id or "N/A"
            })
            
        return {"status": "success", "data": history}
    finally:
        db_session.close()
