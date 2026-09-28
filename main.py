from fastapi import FastAPI, HTTPException, Depends, Request, UploadFile, File, Form, Header, Body, Query, WebSocket, WebSocketDisconnect
from fastapi.security import OAuth2PasswordBearer
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel
from typing import List, Optional
from jose import jwt
from datetime import datetime, timedelta
from passlib.context import CryptContext
from sqlalchemy import create_engine, Column, Integer, String, Float, text
from sqlalchemy.orm import declarative_base, sessionmaker
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, db
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from starlette.middleware.sessions import SessionMiddleware
import requests
import httpx
import asyncio
import random
import json
import os
import time
import hashlib
import urllib.parse
import uuid
import shutil
import traceback

db_lock = asyncio.Lock()
load_dotenv()

# ==========================================
# ⚙️ إعدادات النظام والمزودين (Config)
# ==========================================
ADMIN_USER = os.getenv("ADMIN_USERNAME")
ADMIN_PASS = os.getenv("ADMIN_PASSWORD")
SECRET_KEY = os.getenv("SECRET_KEY", "alpha-secure-key-2026")
SALT_TOKEN = os.getenv("SALT_TOKEN", "NEXUS_SECRET_KEY")

# NexusGGR Config
AGENT_CODE = os.getenv("AGENT_CODE", "Alphabet1")
AGENT_TOKEN = os.getenv("AGENT_TOKEN", "")
NEXUS_SECRET_KEY = os.getenv("NEXUS_SECRET_KEY", "")
PROVIDER_ENDPOINT = os.getenv("PROVIDER_ENDPOINT", "https://api.nexusggr.eu")

# EuroVirtuals Config
EURO_API_KEY = os.getenv("EURO_API_KEY", "RDWR6e0f1DF1ylzpxEpXzMFi.l3m1aebuSmiH6KGjiGzJf9BoQdMH37F63lGmY4TnXnLlPA3d")
EURO_APP_KEY = os.getenv("EURO_APP_KEY", "e0c39ac6-2d70-4ba8-a918-cb2a3fedd029")
EURO_BASE_URL = os.getenv("EURO_BASE_URL", "https://api.betkraft.co.uk/")

# Telegram Alerts
TELEGRAM_TOKEN = "8879806026:AAEB64RCPW4KzsUXUlDeztP_PzjtxkJv_4g"
TELEGRAM_CHAT_ID = "7700782611"

# ==========================================
# ☁️ إعدادات قواعد البيانات (Firebase + SQLite/Postgres)
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
                
        def get(self, key, default=None): return self.full_data.get(key, default)
        def __contains__(self, key): return key in self.full_data
        def __setitem__(self, key, value): self.full_data[key] = value

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
PROMO_FILE = "promo_config.json"

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./local_test.db")
if DATABASE_URL.startswith("postgres://"):
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
        conn.execute(text("ALTER TABLE transactions ADD COLUMN tx_id VARCHAR"))
except Exception:
    pass

Base.metadata.create_all(bind=engine)

# ==========================================
# 🔐 الأمان والمصادقة (Security & Auth)
# ==========================================
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
ALGORITHM = "HS256"
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/login")

def hash_password(password: str): return pwd_context.hash(password)
def verify_password(plain_password, hashed_password):
    try: return pwd_context.verify(plain_password, hashed_password)
    except Exception: return False

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
        log_entry = AuditLog(admin_username=admin_username, action_type=action_type, details=details)
        db_session.add(log_entry)
        db_session.commit()
    except Exception as e: print(f"Audit Log Error: {e}")
    finally: db_session.close()

def verify_nexus_ip(request: Request):
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for: return forwarded_for.split(",")[0].strip()
    return request.client.host

async def send_telegram_alert(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"})
    except Exception as e: print(f"Telegram Alert Error: {e}")

# ==========================================
# 🚀 تهيئة تطبيق FastAPI
# ==========================================
limiter = Limiter(key_func=get_remote_address)
app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # تم الفتح لتسهيل الدمج ويمكنك تقييده لاحقاً
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

# ==========================================
# 🌐 مسارات الواجهة والتوجيه الذكي
# ==========================================
@app.get("/owner.html")
async def redirect_owner(): return RedirectResponse(url="/panel/owner/", status_code=303)
@app.get("/super_admin.html")
async def redirect_super_admin(): return RedirectResponse(url="/panel/super_admin/", status_code=303)
@app.get("/admin.html")
async def redirect_admin(): return RedirectResponse(url="/panel/admin/", status_code=303)
@app.get("/shop.html")
async def redirect_shop(): return RedirectResponse(url="/panel/shop/", status_code=303)

@app.get("/panel/{role}", response_class=HTMLResponse)
@app.get("/panel/{role}/", response_class=HTMLResponse)
async def get_panels(role: str):
    valid_roles = ["owner", "super_admin", "admin", "shop", "manager"]
    if role in valid_roles:
        with open(f"panel/{role}/index.html", "r", encoding="utf-8") as f:
            return f.read()
    return HTMLResponse("Panel not found", status_code=404)

@app.get("/", response_class=HTMLResponse)
async def admin_home(request: Request):
    role = request.session.get("role")
    if role in ["owner", "manager", "super_admin", "admin", "shop"]: 
        return RedirectResponse(url=f"/panel/{role}", status_code=303)
    with open("index.html", "r", encoding="utf-8") as f: return f.read()

# ==========================================
# 👤 النماذج ومسارات الإدارة (Models & Admin Routes)
# ==========================================
class LoginRequest(BaseModel): username: str; password: str
class RegisterRequest(BaseModel): username: str; password: str; role: str; created_by: str; phone: str = ""
class ConfigureAccountRequest(BaseModel): admin_username: str; target_username: str; rtp: int; is_blocked: int
class UpdateBalanceRequest(BaseModel): admin_username: str; target_username: str; action: str; amount: float
class ChangePlayerPasswordRequest(BaseModel): admin_username: str; target_username: str; new_password: str
class HandleRequestModel(BaseModel): transaction_id: int; decision: str; admin_username: str
class DeleteAccountRequest(BaseModel): admin_username: str; target_username: str
class ProviderRequest(BaseModel): provider_code: str
class ChangeMyPasswordRequest(BaseModel): username: str; new_password: str

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
            "message": "success", 
            "username": user["username"],
            "role": user["role"],
            "access_token": access_token,
            "balance": float(user.get("balance", 0.0))
        })
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": f"خطأ داخلي: {str(e)}"})

@app.post("/api/register")
@limiter.limit("60/minute") # رفعنا الحد لتسهيل الإنشاء
async def register_user(request: Request, req: RegisterRequest):
    uname = req.username.lower().strip()
    if uname in ["fethi", "admin", "owner", "system", "boss", "super_admin"]:
        raise HTTPException(status_code=400, detail="Ce nom d'utilisateur est réservé au système!")

    db = load_db()
    for u in db:
        if u["username"] == uname:
            raise HTTPException(status_code=400, detail="Nom d'utilisateur déjà pris")
            
    new_id = max([int(u.get("id", 0)) for u in db]) + 1 if db else 1
    new_user = {
        "id": new_id,
        "username": uname, 
        "password": hash_password(req.password), 
        "role": req.role, 
        "balance": 0.00,
        "rtp": 50, 
        "is_blocked": 0, 
        "created_by": req.created_by, 
        "last_spin_date": "", 
        "daily_deposits": 0.0,
        "phone": req.phone
    }
    db.append(new_user)
    save_db(db)
    log_admin_action(req.created_by, "CREATE_USER", f"Created {uname} with role {req.role}")
    return {"status": "success", "message": "Compte créé", "user_id": new_id}

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
            raise HTTPException(status_code=403, detail="Accès refusé. Ce compte ne vous appartient pas.")

        if req.action == "charge":
            if not is_global_admin:
                if float(admin_user.get("balance", 0)) < amount: 
                    raise HTTPException(status_code=400, detail="Solde insuffisant")
                admin_user["balance"] = round(float(admin_user.get("balance", 0)) - amount, 2)
            target_user["balance"] = round(float(target_user.get("balance", 0)) + amount, 2)
            if current_user.lower() != "system":
                target_user["daily_deposits"] = float(target_user.get("daily_deposits", 0)) + amount

        elif req.action == "withdraw":
            if float(target_user.get("balance", 0)) < amount: 
                raise HTTPException(status_code=400, detail="Solde insuffisant chez le joueur")
            target_user["balance"] = round(float(target_user.get("balance", 0)) - amount, 2)
            if not is_global_admin:
                admin_user["balance"] = round(float(admin_user.get("balance", 0)) + amount, 2)

        db_session = SessionLocal()
        try:
            record_action = "dépôt" if req.action == "charge" else "retrait"
            new_tx = Transaction(
                admin_username=current_user.lower().strip(),
                target_username=target,
                action=record_action,
                amount=amount,
                date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                tx_id=str(uuid.uuid4())
            )
            db_session.add(new_tx)
            db_session.commit()
        except Exception as e:
            db_session.rollback()
            raise HTTPException(status_code=500, detail="Erreur base de données.")
        finally:
            db_session.close()

        save_db(db)
        
    log_admin_action(current_user, "BALANCE_UPDATE", f"Target: {target}, Action: {req.action}, Amount: {amount}")
    return {"status": "success", "message": "Opération réussie"}

# ==========================================
# 🎰 نظام المزودين (NexusGGR & EuroVirtuals)
# ==========================================
GAMES_CACHE = {}
CACHE_TIME_LIMIT = 3600  

@app.post("/api/get-providers")
async def get_real_games(request: ProviderRequest):
    provider_code = request.provider_code
    current_time = time.time()
    
    if provider_code in GAMES_CACHE and (current_time - GAMES_CACHE[provider_code]['time']) < CACHE_TIME_LIMIT:
        return GAMES_CACHE[provider_code]['data']

    payload = {"method": "game_list", "agent_code": AGENT_CODE, "agent_token": AGENT_TOKEN, "provider_code": provider_code}
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(PROVIDER_ENDPOINT, json=payload, timeout=20)
            response_data = response.json()
            if response_data.get("status") == 1 or "games" in response_data:
                GAMES_CACHE[provider_code] = {'time': current_time, 'data': response_data}
            return response_data
        except:
            return GAMES_CACHE.get(provider_code, {}).get('data', {"status": 0, "msg": "Error"})

@app.post("/api/provider/launch-casino")
async def launch_casino(request: Request):
    try:
        data = await request.json()
        payload = {
            "method": "game_launch", "agent_code": AGENT_CODE, "agent_token": AGENT_TOKEN,
            "user_code": data.get("user_code", "test_user"), "provider_code": data.get("provider_code"),
            "game_code": data.get("game_code"), "lang": "fr", "lobby_url": "https://alphabet216.com/#casino"
        }
        response = requests.post(PROVIDER_ENDPOINT.rstrip('/'), json=payload, headers={"Content-Type": "application/json"})
        response_data = response.json()
        if response.status_code == 200:
            game_url = response_data.get("url") or response_data.get("launch_url") or (response_data.get("data", {}).get("url"))
            if game_url: return {"launch_url": game_url}
        return {"error": "المزود رفض الطلب", "details": response_data}
    except Exception as e: return {"error": str(e)}

@app.post("/api/provider/launch-sportsbook")
async def launch_sportsbook(request: Request):
    try:
        data = await request.json()
        user_code = str(data.get("user_code", "test_user"))
        payload = {
            "method": "game_launch", "agent_code": AGENT_CODE, "agent_token": AGENT_TOKEN,
            "provider_code": str(data.get("provider_code", "SPORTSBOOK")), 
            "game_code": str(data.get("game_code", "SPORTSBOOK")),
            "user_code": user_code, "lang": "fr", "lobby_url": "https://alphabet216.com/"
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(PROVIDER_ENDPOINT.rstrip('/'), json=payload, headers={"Content-Type": "application/json"}, timeout=20)
            response_data = response.json()
            game_url = response_data.get("url") or response_data.get("launch_url") or (response_data.get("data", {}).get("url"))
            if game_url: return {"launch_url": game_url}
            return {"error": "المزود رفض الطلب", "details": response_data}
    except Exception as e: return {"error": str(e)}

# --- EuroVirtuals Integration ---
def hash_create_euro(request_data: dict, key: str) -> str:
    keys = sorted(request_data.keys())
    hashkey = ""
    for k in keys:
        value = request_data[k]
        if value is None: continue
        if isinstance(value, dict):
            for nested_key in sorted(value.keys()):
                if value[nested_key] is None: continue
                md5_hash = hashlib.md5(json.dumps(value[nested_key], separators=(',', ':'), sort_keys=True).encode('utf-8')).hexdigest()
                hashkey += f"&{nested_key}={md5_hash}"
        elif isinstance(value, list):
            for index, array_value in enumerate(value):
                if array_value is None: continue
                md5_hash = hashlib.md5(json.dumps(array_value, separators=(',', ':'), sort_keys=True).encode('utf-8')).hexdigest()
                hashkey += f"&{index}={md5_hash}"
        else:
            val_str = str(value).lower() if isinstance(value, bool) else str(value)
            hashkey += f"&{k}={val_str}"
    return hashlib.md5((hashkey.lstrip('&') + str(key)).encode('utf-8')).hexdigest()

def check_eurovirtuals_security(request: Request, payload: dict):
    token = str(request.headers.get("x-token-key") or request.headers.get("x-token") or "").strip()
    signature = str(request.headers.get("x-signature-key") or request.headers.get("x-signature") or "").strip()
    if not token or not signature or token == "invalid-token-key":
        return {"status_code": 401, "status_description": "Invalid Security Headers"}
    if signature != hash_create_euro(payload, token):
        return {"status_code": 401, "status_description": "Invalid Signature"}
    return None

@app.api_route("/api/get-eurovirtuals-games", methods=["GET"])
async def get_virtual_games():
    try:
        timestamp = str(int(time.time()))
        headers = {
            "Accept": "application/json", "Content-Type": "application/json",
            "x-api-key": EURO_API_KEY, "x-signature-key": hash_create_euro({}, EURO_APP_KEY), "x-timestamp": timestamp
        }
        response = requests.get(f"{str(EURO_BASE_URL).rstrip('/')}/v1/games", headers=headers, timeout=20)
        data = response.json()
        if response.status_code == 200 and data.get("status_code") == 200:
            games_list = data.get("data", {}).get("data", [])
            for game in games_list:
                game["game_code"] = game.get("uuid") or game.get("game_uuid") or game.get("id")
            return {"status": "success", "games": games_list}
        return {"status": "error", "error": data.get("status_description", "Unknown Error")}
    except Exception as e: return {"status": "error", "error": str(e)}

@app.post("/api/provider/launch-eurovirtuals")
async def launch_eurovirtuals(request: Request):
    try:
        data = await request.json()
        game_uuid = str(data.get("game_uuid") or data.get("game_code") or data.get("id") or "")
        user_code = str(data.get("user_code", "test_user"))
        
        async with db_lock:
            db = load_db()
            target_user = next((u for u in db if str(u.get("username", "")).lower() == user_code.lower()), None)
            if not target_user or target_user.get("is_blocked") == 1: return {"error": "Player unavailable"}
            current_balance = float(target_user.get("balance", 0.0))

        payload = {
            "player_id": user_code, "player_name": user_code, "player_token": f"tok_{user_code}",
            "currency": "TND", "demo": 0, "game_uuid": game_uuid, "balance": current_balance,
            "country": "TN", "language": "fr", "device": "desktop"
        }
        signature = hash_create_euro(payload, EURO_APP_KEY)
        headers = {
            "Accept": "application/json", "Content-Type": "application/json",
            "x-api-key": EURO_API_KEY, "x-signature-key": signature, "x-signature": signature, "x-timestamp": str(int(time.time()))
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(f"{str(EURO_BASE_URL).rstrip('/')}/v1/launch", json=payload, headers=headers, timeout=20)
            response_data = response.json()
            if response_data.get("status_code") == 200:
                game_url = response_data.get("data", {}).get("url")
                return {"launch_url": f"{str(EURO_BASE_URL).rstrip('/')}{game_url}" if game_url.startswith("/") else game_url}
            return {"error": response_data.get("status_description", "Rejected")}
    except Exception as e: return {"error": str(e)}

# ==========================================
# 🎁 محرك الجاكبوت ونظام WebSocket الموحد
# ==========================================
JACKPOTS_BASE = {
    "mini":  {"start": 20.0, "days": 1},
    "minor": {"start": 40.0, "days": 2},
    "major": {"start": 80.0, "days": 7},
    "grand": {"start": 500.0, "days": 30},
}

def generate_drop_time(days): return datetime.now() + timedelta(seconds=random.randint(1, int(timedelta(days=days).total_seconds())))

jackpots_state = {
    "mini":  {"current_amount": 20.0, "drop_time": generate_drop_time(1)},
    "minor": {"current_amount": 40.0, "drop_time": generate_drop_time(2)},
    "major": {"current_amount": 80.0, "drop_time": generate_drop_time(7)},
    "grand": {"current_amount": 500.0, "drop_time": generate_drop_time(30)},
}

class ConnectionManager:
    def __init__(self): self.active_connections: list[WebSocket] = []
    async def connect(self, websocket: WebSocket): await websocket.accept(); self.active_connections.append(websocket)
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
        live_data = {k: round(v["current_amount"], 2) for k, v in jackpots_state.items()}
        await jackpot_manager.broadcast(json.dumps(live_data))
        await asyncio.sleep(2)

async def time_based_jackpot_engine():
    await asyncio.sleep(10) 
    while True:
        try:
            now = datetime.now()
            for level in jackpots_state:
                jackpots_state[level]["current_amount"] += 0.03
                if now >= jackpots_state[level]["drop_time"]:
                    await trigger_jackpot_drop(level, jackpots_state[level]["current_amount"])
                    jackpots_state[level]["current_amount"] = JACKPOTS_BASE[level]["start"]
                    jackpots_state[level]["drop_time"] = generate_drop_time(JACKPOTS_BASE[level]["days"])
        except Exception as e: pass
        await asyncio.sleep(60)

async def trigger_jackpot_drop(level, total_amount):
    async with db_lock:
        db = load_db()
        eligible_users = [u for u in db if str(u.get("role")) == "player" and str(u.get("is_blocked")) != "1" and float(u.get("balance", 0.0)) > 0]
        if not eligible_users: return 
        winners = random.sample(eligible_users, min(5, len(eligible_users)))
        win_per_user = round(total_amount / len(winners), 2)
        
        db_session = SessionLocal()
        try:
            for winner in winners:
                winner["balance"] = round(float(winner.get("balance", 0.0)) + win_per_user, 2)
                db_session.add(Transaction(admin_username="SYSTEM_JACKPOT", target_username=winner["username"], action=f"jackpot_win ({level})", amount=win_per_user, date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), tx_id=f"jp_{uuid.uuid4().hex[:8]}"))
            db_session.commit()
            save_db(db)
            
            drop_data = {"type": "jackpot_drop", "level": level.upper(), "total_amount": total_amount, "win_per_user": win_per_user, "winners": [f"{str(w['username'])[:4]}***" for w in winners]}
            asyncio.create_task(jackpot_manager.broadcast(json.dumps(drop_data)))
        except Exception as e: db_session.rollback()
        finally: db_session.close()

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(broadcast_jackpots())
    asyncio.create_task(time_based_jackpot_engine())