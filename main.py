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

import html
from fastapi import FastAPI, Request, HTTPException, Depends, Query
from fastapi.security import OAuth2PasswordBearer
from slowapi import Limiter
from slowapi.util import get_remote_address

# تعريف الكائنات الأساسية إذا لم تكن موجودة مسبقاً في أعلى الملف:
app = FastAPI()
limiter = Limiter(key_func=get_remote_address)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/login")

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
class LoginRequest(BaseModel):
    username: str
    password: str
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
# 🛡️ الحزمة الأمنية الشاملة (Security & Integrity Patch)
# ==========================================

# 1. دالة الحماية الأصلية للتوقيع الرقمي (BSW/Nexus)
SALT_TOKEN = os.getenv("SALT_TOKEN", "NEXUS_SECRET_KEY")

def verify_hash(data: dict, received_hash: str) -> bool:
    filtered_data = {k: v for k, v in data.items() if k != "hash" and v is not None}
    sorted_params = sorted(filtered_data.items())
    query_string = urllib.parse.urlencode(sorted_params)
    string_to_hash = query_string + SALT_TOKEN
    calculated_hash = hashlib.md5(string_to_hash.encode("utf-8")).hexdigest()
    return calculated_hash == received_hash

# 2. تحديث دالة قراءة التوكن لمنع الحسابات المحظورة فوراً
async def get_current_user(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        
        # 🛡️ إبطال التوكن إذا تم حظر الحساب من قبل الإدارة
        db = load_db()
        user = next((u for u in db if u["username"] == username), None)
        if not user or user.get("is_blocked") == 1:
            raise HTTPException(status_code=403, detail="Compte bloqué ou introuvable")
            
        return username
    except:
        raise HTTPException(status_code=401, detail="Invalid token")

# 3. تحديث مسار تسجيل الدخول الأصلي بالكامل
@app.post("/api/login")
@limiter.limit("5/minute")
async def login_user(request: Request, req: LoginRequest):
    try:
        uname = html.escape(req.username.lower().strip())
        db = load_db()
        user = next((u for u in db if u["username"] == uname), None)

        if not user or not verify_password(req.password, user.get("password", "")):
            bad_alert = f"⚠️ <b>محاولة دخول فاشلة!</b>\n👤 الحساب: <code>{req.username}</code>"
            asyncio.create_task(send_telegram_alert(bad_alert))
            return JSONResponse(status_code=401, content={"detail": "Identifiants incorrects"})
        
        # 🛡️ الجدار الأمني: الرفض المباشر للمحظورين
        if user.get("is_blocked") == 1:
            return JSONResponse(status_code=403, content={"detail": "Ce compte est bloqué par l'administration!"})
            
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
        return JSONResponse(status_code=500, content={"detail": f"Erreur serveur: {str(e)}"})

# 4. التحديث الأمني لرفع الملفات (MIME Checker) في مسار الإيداع
@app.post("/api/admin/request-transaction")
async def request_transaction(request: Request, current_user: str = Depends(get_current_user)):
    db_session = SessionLocal()
    try:
        form = await request.form()
        target_username = form.get("target_username")
        action = form.get("action")
        amount = float(form.get("amount", 0))
        tx_id = form.get("tx_id", str(uuid.uuid4()))

        if amount <= 0:
            return JSONResponse(status_code=400, content={"detail": "Le montant doit être supérieur à zéro"})

        file_path = ""
        file = form.get("file")
        if file and hasattr(file, "filename") and file.filename:
            # 🛡️ فحص الامتداد والمحتوى لمنع الملفات الخبيثة
            allowed_extensions = ['.png', '.jpg', '.jpeg', '.webp']
            allowed_mimes = ['image/png', 'image/jpeg', 'image/webp']
            
            file_ext = os.path.splitext(file.filename)[1].lower()
            if file_ext not in allowed_extensions or file.content_type not in allowed_mimes:
                return JSONResponse(status_code=400, content={"detail": "Format non autorisé ou fichier corrompu."})
            
            os.makedirs("uploads", exist_ok=True)
            safe_filename = f"{uuid.uuid4().hex}{file_ext}"
            file_path = os.path.join("uploads", safe_filename)
            with open(file_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)

        new_tx = Transaction(
            admin_username="PENDING", target_username=target_username, action=action,
            amount=amount, tx_id=tx_id, image_path=file_path, date=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        db_session.add(new_tx)
        db_session.commit()
        return {"status": "success", "message": "Demande envoyée"}
    except Exception as e:
        db_session.rollback()
        return JSONResponse(status_code=500, content={"detail": str(e)})
    finally:
        db_session.close()
def load_tickets_db():
    if os.path.exists(TICKETS_FILE):
        try:
            with open(TICKETS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return []
    return []

def save_tickets_db(data):
    with open(TICKETS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
        
# 5. المهام الخلفية للحماية وتصفية الأرصدة (Background Integrity)
async def auto_settle_tickets():
    await asyncio.sleep(10) 
    while True:
        try:
            tickets_db = load_tickets_db()
            db = load_db()
            changes_made = False
            pending_tickets = [t for t in tickets_db if t.get("status") == "encours"]
            
            for ticket in pending_tickets:
                simulated_result = random.choice(["gagne", "perdu"]) 
                ticket["status"] = simulated_result
                changes_made = True
                if simulated_result == "gagne":
                    target_username = ticket["username"]
                    win_amount = float(ticket.get("gain", 0))
                    for u in db:
                        if u["username"] == target_username:
                            u["balance"] = float(u.get("balance", 0)) + win_amount
                            break
            if changes_made:
                save_tickets_db(tickets_db)
                save_db(db)
        except Exception:
            pass
        await asyncio.sleep(60) 

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
                                    new_tx = Transaction(
                                        admin_username="SYSTEM_CASHBACK", target_username=u["username"],
                                        action="cashback", amount=cashback_amount,
                                        date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), tx_id=f"cb_{int(time.time())}"
                                    )
                                    db_session.add(new_tx)
                                    db_session.commit()
                                except Exception:
                                    db_session.rollback()
                                finally:
                                    db_session.close()
                            
                            u["daily_deposits"] = 0
                            changes_made = True
                            
                    if changes_made:
                        save_db(db)
                await asyncio.sleep(3600)
            else:
                await asyncio.sleep(300)
        except Exception:
            await asyncio.sleep(300) 

# 6. تفعيل المهام الأمنية عند التشغيل
@app.on_event("startup")
async def start_security_tasks():
    asyncio.create_task(auto_settle_tickets()) 
    asyncio.create_task(daily_cashback_system())
    
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
# 📦 النماذج الإضافية (Pydantic Models)
# ==========================================
class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str
    phone: str = ""

class ConfigureAccountRequest(BaseModel):
    admin_username: str
    target_username: str
    rtp: int
    is_blocked: int

class ChangePlayerPasswordRequest(BaseModel):
    admin_username: str
    target_username: str
    new_password: str

class DeleteAccountRequest(BaseModel):
    admin_username: str
    target_username: str

class HandleRequestModel(BaseModel):
    transaction_id: int
    decision: str
    admin_username: str

class ApproveDepositRequest(BaseModel):
    ticket_id: str
    amount: float

class ResettleTicketRequest(BaseModel):
    ticket_id: str
    new_status: str

class HandleHugeWinRequest(BaseModel):
    tx_id: int
    decision: str

class FreeSpinRequest(BaseModel):
    target_username: str
    provider_code: str
    game_code: str
    spin_count: int
    amount: float
    bet_level: int = 1

class CancelTourRequest(BaseModel):
    tour_id: str

class PromoModel(BaseModel):
    is_active: bool
    day_of_week: str
    min_amount: float
    spins: int
    game: str
    provider: str
    max_win: float

class NotificationModel(BaseModel):
    target_user: str
    title: str
    message: str
    icon: str = "fa-bell"

class MarkReadModel(BaseModel):
    notif_id: str

class DeleteNotifModel(BaseModel):
    notif_id: str


# ==========================================
# 1️⃣ إدارة الحسابات (Account Management)
# ==========================================

@app.post("/api/admin/create-user")
@limiter.limit("60/minute")
async def create_network_user(request: Request, req: CreateUserRequest, current_user: str = Depends(get_admin_user)):
    uname = req.username.lower().strip()
    
    # 🛡️ منع الأسماء المحجوزة
    if uname in ["fethi", "admin", "owner", "system", "boss", "super_admin"]:
        raise HTTPException(status_code=400, detail="Nom d'utilisateur réservé au système!")

    async with db_lock:
        db = load_db()
        
        for u in db:
            if u["username"] == uname:
                raise HTTPException(status_code=400, detail="Nom d'utilisateur déjà pris")
                
        # 🛡️ التحقق من صلاحيات الشوب (يسمح له بإنشاء لاعبين فقط)
        creator_user = next((u for u in db if u["username"] == current_user), None)
        if creator_user and creator_user.get("role") == "shop" and req.role != "player":
            raise HTTPException(status_code=403, detail="Les shops ne peuvent créer que des joueurs")

        hashed_pwd = hash_password(req.password)
        new_id = max([int(u.get("id", 0)) for u in db]) + 1 if db else 1
        
        new_user = {
            "id": new_id,
            "username": uname, 
            "password": hashed_pwd, 
            "role": req.role, 
            "balance": 0.00,
            "rtp": 50, 
            "is_blocked": 0, 
            "created_by": current_user, # 🛡️ تم سحب المُنشئ من التوكن بأمان
            "last_spin_date": "", 
            "daily_deposits": 0.0,
            "phone": req.phone
        }
        
        db.append(new_user)
        save_db(db)
        
    log_admin_action(current_user, "CREATE_USER", f"Création de {uname} ({req.role})")
    return {"status": "success", "message": "Compte créé", "user_id": new_id}

@app.post("/api/admin/configure-account")
async def configure_account(req: ConfigureAccountRequest, current_user: str = Depends(get_admin_user)):
    db = load_db()
    for u in db:
        if u["username"] == req.target_username.lower().strip():
            u["rtp"] = req.rtp
            u["is_blocked"] = req.is_blocked
            save_db(db)
            log_admin_action(current_user, "CONFIG_ACCOUNT", f"Target: {req.target_username}, RTP: {req.rtp}, Blocked: {req.is_blocked}")
            return {"status": "success", "message": "Configuration enregistrée"}
    raise HTTPException(status_code=404, detail="Utilisateur non trouvé")

@app.post("/api/admin/change-player-password")
async def change_player_password(req: ChangePlayerPasswordRequest, current_user: str = Depends(get_admin_user)):
    db = load_db()
    for u in db:
        if u["username"] == req.target_username.lower().strip():
            u["password"] = hash_password(req.new_password)
            save_db(db)
            log_admin_action(current_user, "CHANGE_PASS", f"Mot de passe modifié pour {req.target_username}")
            return {"status": "success", "message": "Mot de passe modifié avec succès"}
    raise HTTPException(status_code=404, detail="Utilisateur non trouvé")

@app.post("/api/user/change-password")
async def change_my_password(req: ChangeMyPasswordRequest, current_user: str = Depends(get_current_user)):
    target_username = req.username.lower().strip()
    if current_user != target_username and current_user not in ["owner", "super_admin"]:
        raise HTTPException(status_code=403, detail="Non autorisé")
        
    db = load_db()
    for u in db:
        if u["username"] == target_username:
            u["password"] = hash_password(req.new_password)
            save_db(db)
            return {"status": "success", "message": "Mot de passe modifié avec succès"}
    raise HTTPException(status_code=404, detail="Utilisateur non trouvé")

@app.delete("/api/admin/delete-account")
async def delete_account(req: DeleteAccountRequest, current_user: str = Depends(get_admin_user)):
    db = load_db()
    target = req.target_username.lower().strip()
    new_db = [u for u in db if u.get("username", "").lower().strip() != target]
    if len(new_db) == len(db): 
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé")
    save_db(new_db)
    log_admin_action(current_user, "DELETE_USER", f"Suppression de {target}")
    return {"status": "success", "message": "Compte supprimé"}


# ==========================================
# 2️⃣ الكاشير والمعاملات المالية (Cashier & Finance)
# ==========================================


@app.get("/api/admin/get-pending-deposits")
async def get_pending_deposits(current_user: str = Depends(get_admin_user)):
    db_session = SessionLocal()
    try:
        sql_deposits = db_session.query(Transaction).filter(
            Transaction.admin_username == "PENDING",
            Transaction.action == "deposit_request"
        ).order_by(Transaction.id.desc()).all()
        
        result = []
        for t in sql_deposits:
            tx_parts = str(t.tx_id).split('-') if t.tx_id else ["N/A", "N/A"]
            method_name = tx_parts[0]
            code_val = tx_parts[1] if len(tx_parts) > 1 else "N/A"

            result.append({
                "ticket_id": t.id,
                "username": t.target_username,
                "method": method_name, 
                "amount": float(t.amount or 0),
                "code": code_val if code_val != "FILE" else "Reçu attaché",
                "receipt_image": t.image_path,
                "status": "pending",
                "date": str(t.date)
            })
        return result
    finally:
        db_session.close()

@app.get("/api/admin/get-pending-withdrawals")
async def get_pending_withdrawals(current_user: str = Depends(get_admin_user)):
    db_session = SessionLocal()
    try:
        txs = db_session.query(Transaction).filter(
            Transaction.admin_username == "PENDING",
            Transaction.action.ilike("%withdraw%")
        ).order_by(Transaction.id.desc()).all()
        
        result = []
        for t in txs:
            result.append({
                "id": t.id,
                "tx_id": t.tx_id or str(t.id),
                "target_username": t.target_username,
                "amount": float(t.amount or 0),
                "action": "withdraw_request",
                "date": str(t.date)
            })
        return result
    finally:
        db_session.close()

@app.post("/api/admin/handle-request")
async def handle_pending_request(req: HandleRequestModel, current_user: str = Depends(get_admin_user)):
    db_session = SessionLocal()
    try:
        tx = db_session.query(Transaction).filter(Transaction.id == req.transaction_id).first()
        if not tx or tx.admin_username != "PENDING":
            raise HTTPException(status_code=404, detail="Demande introuvable ou déjà traitée")

        if req.decision == "reject":
            db_session.delete(tx)
            db_session.commit()
            return {"status": "success", "message": "Demande rejetée"}

        async with db_lock:
            db = load_db()
            target_user = next((u for u in db if u["username"] == tx.target_username), None)
            
            if not target_user:
                raise HTTPException(status_code=404, detail="Utilisateur non trouvé")

            if tx.action == "deposit_request":
                target_user["balance"] = float(target_user.get("balance", 0)) + tx.amount
                tx.action = "charge"
            elif tx.action == "withdraw_request":
                if target_user.get("balance", 0) < tx.amount:
                    raise HTTPException(status_code=400, detail="Solde insuffisant")
                target_user["balance"] = float(target_user.get("balance", 0)) - tx.amount
                tx.action = "withdraw"

            tx.admin_username = req.admin_username
            db_session.commit()
            save_db(db)
            
        return {"status": "success", "message": "Demande approuvée"}
    finally:
        db_session.close()

@app.post("/api/admin/process-withdrawal")
async def process_withdrawal(request: Request, current_user: str = Depends(get_admin_user)):
    data = await request.json()
    request_id = data.get("request_id")
    action_type = data.get("action")
    
    db_session = SessionLocal()
    try:
        tx = db_session.query(Transaction).filter((Transaction.id == request_id) | (Transaction.tx_id == str(request_id))).first()
        if not tx or tx.admin_username != "PENDING":
            return JSONResponse(status_code=400, content={"detail": "Demande invalide ou déjà traitée"})
            
        if action_type == "approve":
            tx.admin_username = current_user
        elif action_type == "reject":
            tx.admin_username = f"REJECTED_BY_{current_user}"
            async with db_lock:
                db = load_db()
                u = next((user for user in db if user["username"] == tx.target_username), None)
                if u:
                    u["balance"] = float(u.get("balance", 0)) + float(tx.amount or 0)
                    save_db(db)
                
        db_session.commit()
        return {"status": "success", "message": "Traitement réussi"}
    finally:
        db_session.close()


@app.get("/api/user/transactions-history")
async def get_user_transactions(current_user: str = Depends(get_current_user)):
    db_session = SessionLocal()
    history = []
    try:
        sql_txs = db_session.query(Transaction).filter(Transaction.target_username == current_user.lower()).all()
        for w in sql_txs:
            action_lower = str(w.action).lower()
            if action_lower in ["bet", "win", "rollback", "adjustment"]: continue
            
            tx_type = "Dépôt" if any(x in action_lower for x in ["dépôt", "charge", "deposit"]) else "Retrait"
            status = "Approuvé" if "PENDING" not in w.admin_username.upper() else "En attente"
            if "REJECT" in w.admin_username.upper(): status = "Refusé"

            history.append({
                "date": str(w.date)[:16],
                "type": tx_type,
                "method": w.tx_id if w.tx_id else "Agent",
                "amount": float(w.amount),
                "status": status,
                "timestamp": str(w.date)
            })
    finally:
        db_session.close()
    
    history.sort(key=lambda x: x["timestamp"], reverse=True)
    return {"status": "success", "data": history}


# ==========================================
# 3️⃣ سجلات الألعاب والرياضة (Gaming & Logs)
# ==========================================

@app.get("/api/admin/get-all-tickets")
async def get_all_tickets_api(current_user: str = Depends(get_admin_user)):
    try:
        if not os.path.exists(TICKETS_FILE): return []
        with open(TICKETS_FILE, "r") as f:
            tickets_db = json.load(f)
            
        allowed_tickets = [t for t in tickets_db if t.get("type") != "deposit"]
        allowed_tickets.reverse()
        return allowed_tickets
    except:
        return []

@app.get("/api/admin/get-player-tickets")
async def get_player_tickets(username: str, current_user: str = Depends(get_admin_user)):
    all_tickets = await get_all_tickets_api(current_user)
    return [t for t in all_tickets if str(t.get("username", "")).lower() == username.lower() or str(t.get("user", "")).lower() == username.lower()]

@app.post("/api/admin/resettle-ticket")
async def resettle_ticket(req: ResettleTicketRequest, current_user: str = Depends(get_admin_user)):
    try:
        with open(TICKETS_FILE, "r") as f: tickets_db = json.load(f)
        
        ticket = next((t for t in tickets_db if str(t.get("ticket_id")) == str(req.ticket_id)), None)
        if not ticket: raise HTTPException(status_code=404, detail="Ticket introuvable")
        
        old_status = ticket.get("status")
        win_amount = float(ticket.get("gain", 0))

        async with db_lock:
            db = load_db()
            target_user = next((u for u in db if u["username"] == ticket.get("username")), None)
            
            if target_user:
                if old_status == "gagne" and req.new_status != "gagne":
                    target_user["balance"] = float(target_user.get("balance", 0)) - win_amount
                elif old_status != "gagne" and req.new_status == "gagne":
                    target_user["balance"] = float(target_user.get("balance", 0)) + win_amount
                save_db(db)

        ticket["status"] = req.new_status
        with open(TICKETS_FILE, "w") as f: json.dump(tickets_db, f, indent=4)
        
        log_admin_action(current_user, "RESET_TICKET", f"Ticket {req.ticket_id} changé en {req.new_status}")
        return {"status": "success", "message": f"Ticket modifié en {req.new_status}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/admin/casino-history")
async def get_casino_history(current_user: str = Depends(get_admin_user)):
    db_session = SessionLocal()
    try:
        txs = db_session.query(Transaction).filter(
            Transaction.action.in_(["bet", "win", "rollback"])
        ).order_by(Transaction.id.desc()).limit(500).all()
        
        return [{"id": t.id, "username": t.username or t.target_username, "action": t.action, "amount": t.amount, "date": str(t.date)} for t in txs]
    finally:
        db_session.close()


# ==========================================
# 4️⃣ الأدوات المتقدمة والإحصائيات (Owner Exclusive)
# ==========================================

@app.get("/api/admin/pending-huge-wins")
async def get_pending_huge_wins(current_user: str = Depends(get_admin_user)):
    db_session = SessionLocal()
    try:
        txs = db_session.query(Transaction).filter(Transaction.admin_username == "PENDING_HUGE_WIN").all()
        return [{"id": t.id, "target_username": t.target_username, "amount": float(t.amount), "action": t.action, "date": t.date} for t in txs]
    finally:
        db_session.close()

@app.post("/api/admin/handle-huge-win")
async def handle_huge_win(req: HandleHugeWinRequest, current_user: str = Depends(get_admin_user)):
    db_session = SessionLocal()
    try:
        tx = db_session.query(Transaction).filter(Transaction.id == req.tx_id).first()
        if not tx or tx.admin_username != "PENDING_HUGE_WIN":
            return JSONResponse(status_code=404, content={"detail": "Demande non trouvée"})

        if req.decision == "approve":
            async with db_lock:
                db = load_db()
                target_user = next((u for u in db if str(u["username"]).lower() == str(tx.target_username).lower()), None)
                if target_user:
                    target_user["balance"] = round(float(target_user.get("balance", 0)) + tx.amount, 2)
                    save_db(db)
            tx.admin_username = f"APPROVED_BY_{current_user.upper()}"
        else:
            tx.admin_username = f"REJECTED_BY_{current_user.upper()}"

        db_session.commit()
        return {"status": "success", "message": "Gain traité avec succès"}
    finally:
        db_session.close() 

@app.get("/api/admin/promo")
async def get_promo(current_user: str = Depends(get_admin_user)):
    if os.path.exists(PROMO_FILE):
        with open(PROMO_FILE, "r") as f: return json.load(f)
    return {"is_active": False, "day_of_week": "Friday", "min_amount": 50, "spins": 10, "game": "vs20doghouse", "provider": "PRAGMATIC", "max_win": 10000}

@app.post("/api/admin/promo")
async def set_promo(data: PromoModel, current_user: str = Depends(get_admin_user)):
    with open(PROMO_FILE, "w") as f: json.dump(data.dict(), f)
    return {"status": "success", "message": "Configuration enregistrée"}    

@app.get("/api/admin/fraud-detection")
async def fraud_detection(current_user: str = Depends(get_admin_user)):
    db = load_db()
    ip_map = {}; phone_map = {}
    
    for u in db:
        ip = u.get("last_ip") or u.get("ip")
        phone = u.get("phone")
        uname = u.get("username")
        
        if ip: ip_map.setdefault(ip, []).append(uname)
        if phone and phone != "00000000": phone_map.setdefault(phone, []).append(uname)
            
    suspicious_ips = {ip: users for ip, users in ip_map.items() if len(users) > 1}
    suspicious_phones = {phone: users for phone, users in phone_map.items() if len(users) > 1}
    
    return {"status": "success", "shared_ips": suspicious_ips, "shared_phones": suspicious_phones}

@app.get("/api/admin/analytics/ggr")
async def get_ggr_analytics(year: Optional[int] = Query(None), month: Optional[int] = Query(None), current_user: str = Depends(get_admin_user)):
    db_session = SessionLocal()
    try:
        txs = db_session.query(Transaction).filter(Transaction.action.in_(["bet", "win", "rollback"])).all()
        total_bets = 0; total_wins = 0
        
        for t in txs:
            if not t.date: continue
            try:
                date_part = str(t.date).split(" ")[0].split("-")
                if len(date_part) >= 2:
                    if year and int(date_part[0]) != year: continue
                    if month and int(date_part[1]) != month: continue
            except: pass
            
            if t.action == "bet": total_bets += t.amount
            elif t.action == "win": total_wins += t.amount
        
        ggr = total_bets - total_wins
        actual_rtp = (total_wins / total_bets * 100) if total_bets > 0 else 0.0
        
        return {"status": "success", "total_bets": round(total_bets, 2), "total_wins": round(total_wins, 2), "ggr": round(ggr, 2), "actual_rtp": round(actual_rtp, 2)}
    finally:
        db_session.close()

@app.post("/api/admin/send-notification")
async def send_notification(req: NotificationModel, current_user: str = Depends(get_admin_user)):
    db = load_db()
    if "notifications" not in db.full_data: db.full_data["notifications"] = []
    
    db.full_data["notifications"].append({
        "id": str(uuid.uuid4())[:8],
        "target": req.target_user.lower().strip(),
        "title": req.title, "message": req.message, "icon": req.icon,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"), "read_by": []
    })
    save_db(db)
    return {"status": "success", "message": "Notification envoyée"}

@app.get("/api/user/notifications")
async def get_user_notifications(current_user: str = Depends(get_current_user)):
    db = load_db()
    notifs = db.full_data.get("notifications", [])
    user_notifs = []; unread_count = 0
    
    for n in notifs:
        if current_user.lower() in n.get("deleted_by", []): continue
        if n.get("target") == "all" or n.get("target") == current_user.lower():
            is_read = current_user.lower() in n.get("read_by", [])
            if not is_read: unread_count += 1
            user_notifs.append({**n, "is_read": is_read})
            
    return {"unread": unread_count, "notifications": user_notifs[::-1][:15]}

@app.post("/api/user/read-notification")
async def mark_notif_read(req: MarkReadModel, current_user: str = Depends(get_current_user)):
    db = load_db()
    for n in db.full_data.get("notifications", []):
        if n["id"] == req.notif_id:
            if current_user.lower() not in n.get("read_by", []):
                n.setdefault("read_by", []).append(current_user.lower())
            break
    save_db(db)
    return {"status": "success"}

@app.post("/api/user/delete-notification")
async def delete_notification(req: DeleteNotifModel, current_user: str = Depends(get_current_user)):
    db = load_db()
    for n in db.full_data.get("notifications", []):
        if req.notif_id == "all" or n["id"] == req.notif_id:
            if current_user.lower() not in n.get("deleted_by", []):
                n.setdefault("deleted_by", []).append(current_user.lower())
    save_db(db)
    return {"status": "success"}

@app.get("/api/admin/audit-logs")
async def get_audit_logs(current_user: str = Depends(get_admin_user)):
    db_session = SessionLocal()
    try:
        logs = db_session.query(AuditLog).order_by(AuditLog.id.desc()).limit(300).all()
        return [{"id": l.id, "admin_username": l.admin_username, "action_type": l.action_type, "details": l.details, "date": l.date} for l in logs]
    finally:
        db_session.close()
 # ==========================================
# 5️⃣ المسارات التكميلية المفقودة (التاريخ المالي واللفات المجانية)
# ==========================================

@app.get("/api/admin/transactions-history")
async def get_tx_history(username: str = None, current_user: str = Depends(get_admin_user)):
    db = load_db()
    current_admin = next((u for u in db if u["username"] == current_user), None)
    current_role = current_admin.get("role", "player") if current_admin else "player"
    
    # تحديد نطاق الرؤية بناءً على الصلاحيات
    if current_role in ["owner", "system", "super_admin"]:
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

    db_session = SessionLocal()
    try:
        txs = db_session.query(Transaction).all()
        result = []
        for t in txs:
            target = t.target_username or t.target
            # التحقق مما إذا كان المستخدم المستهدف ضمن شبكة الإداري
            if target in allowed_users or t.admin_username in allowed_users:
                result.append({
                    "id": t.id,
                    "action": t.action,
                    "amount": t.amount,
                    "target_username": target,
                    "admin_username": t.admin_username,
                    "date": str(t.date),
                    "image_path": t.image_path
                })
        result.reverse()
        return result
    finally:
        db_session.close()

@app.post("/api/admin/grant-freespins")
async def grant_free_spins(req: FreeSpinRequest, current_user: str = Depends(get_admin_user)):
    db = load_db()
    admin = next((u for u in db if u["username"] == current_user), None)
    if not admin or admin.get("role") not in ["owner", "super_admin"]:
        raise HTTPException(status_code=403, detail="صلاحية الأونر أو السوبر أدمن مطلوبة")

    # تحديد انتهاء الصلاحية (بعد 7 أيام)
    expiration = (datetime.utcnow() + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    tour_id = f"fs_{uuid.uuid4().hex[:10]}"

    payload = {
        "method": "tour_create",
        "agent_code": AGENT_CODE,
        "agent_token": AGENT_TOKEN,
        "user_code": req.target_username,
        "provider_code": req.provider_code,
        "game_code": req.game_code,
        "bet_level": req.bet_level,
        "spin_count": req.spin_count,
        "amount": req.amount,
        "expiration_time": expiration,
        "tour_id": tour_id
    }

    async with httpx.AsyncClient() as client:
        try:
            # استخدام PROVIDER_ENDPOINT بدلاً من المتغير القديم
            response = await client.post(PROVIDER_ENDPOINT, json=payload, timeout=15.0)
            res_data = response.json()
            
            if res_data.get("status") == 1:
                real_win = res_data.get("real_win", 0)
                return {"status": "success", "message": f"تم إرسال {req.spin_count} لفة مجانية!", "real_win": real_win}
            else:
                error_msg = res_data.get("detail") or res_data.get("msg") or "المزود رفض الطلب"
                return JSONResponse(status_code=400, content={"detail": f"خطأ المزود: {error_msg}"})
        except Exception as e:
            return JSONResponse(status_code=500, content={"detail": f"خطأ في الاتصال: {str(e)}"}) 

@app.post("/api/admin/cancel-freespins")
async def cancel_free_spins(req: CancelTourRequest, current_user: str = Depends(get_admin_user)):
    db = load_db()
    admin = next((u for u in db if u["username"] == current_user), None)
    if not admin or admin.get("role") not in ["owner", "super_admin"]:
        raise HTTPException(status_code=403, detail="صلاحية الأونر أو السوبر أدمن مطلوبة")

    payload = {
        "method": "tour_cancel",
        "agent_code": AGENT_CODE,
        "agent_token": AGENT_TOKEN,
        "tour_id": req.tour_id
    }

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(PROVIDER_ENDPOINT, json=payload, timeout=15.0)
            res_data = response.json()
            
            if res_data.get("status") == 1:
                refund = res_data.get("canceled_money", 0)
                rest = res_data.get("rest_count", 0)
                return {"status": "success", "message": f"تم الإلغاء! استرجاع: {refund} TND (تبقى {rest} لفة غير ملعوبة)."}
            else:
                error_msg = res_data.get("detail") or res_data.get("msg") or "المزود رفض الطلب"
                return JSONResponse(status_code=400, content={"detail": f"خطأ المزود: {error_msg}"})
        except Exception as e:
            return JSONResponse(status_code=500, content={"detail": f"خطأ في الاتصال: {str(e)}"})
# ==========================================
# 🔗 مسارات المحفظة الموحدة (Seamless Wallet Callbacks)
# ==========================================

# 1. محفظة Nexus (الكازينو والرياضة)
@app.post("/gold_api")
@app.post("/gold_api/gold_api")
async def seamless_wallet_handler(request: Request):
    try:
        data = await request.json()
        method = data.get("method")
        user_code = data.get("user_code")
        
        async with db_lock:
            db = load_db()
            target_user = next((u for u in db if str(u.get("username", "")).lower() == str(user_code).lower()), None)
            
            if not target_user:
                return JSONResponse(content={"status": 0, "msg": "USER_NOT_FOUND"})
            
            player_balance = float(target_user.get("balance", 0))

            if method == "user_balance":
                return JSONResponse(content={"status": 1, "user_balance": player_balance})

            elif method == "transaction":
                game_type = data.get("game_type")
                tx_data = data.get(game_type, {})
                bet_money = float(tx_data.get("bet_money", 0))
                win_money = float(tx_data.get("win_money", 0))
                txn_type = tx_data.get("txn_type")

                # خصم الرهان
                if txn_type in ["debit", "debit_credit"]:
                    if player_balance < bet_money:
                        return JSONResponse(content={"status": 0, "msg": "INSUFFICIENT_USER_FUNDS"})
                    player_balance -= bet_money

                # إضافة الربح مع نظام حماية الأرباح الضخمة
                if txn_type in ["credit", "debit_credit"]:
                    if win_money >= 30000:
                        db_session = SessionLocal()
                        try:
                            new_tx = Transaction(
                                admin_username="PENDING_HUGE_WIN",
                                target_username=target_user["username"],
                                action="huge_win (Nexus)",
                                amount=win_money,
                                date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                tx_id=tx_data.get("txn_id", str(uuid.uuid4()))
                            )
                            db_session.add(new_tx)
                            db_session.commit()
                        except Exception:
                            db_session.rollback()
                        finally:
                            db_session.close()
                    else:
                        player_balance += win_money

                target_user["balance"] = round(player_balance, 2)
                save_db(db)
                
                # توثيق المعاملة في SQL
                db_session = SessionLocal()
                try:
                    action_name = "bet" if bet_money > 0 else "win"
                    tx_amount = bet_money if bet_money > 0 else win_money
                    if tx_amount > 0:
                        new_tx = Transaction(admin_username="NEXUS_API", target_username=target_user["username"], action=action_name, amount=tx_amount, date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), tx_id=tx_data.get("txn_id", ""))
                        db_session.add(new_tx)
                        db_session.commit()
                except:
                    db_session.rollback()
                finally:
                    db_session.close()

                return JSONResponse(content={"status": 1, "user_balance": round(player_balance, 2)})

            else:
                return JSONResponse(content={"status": 0, "msg": "UNKNOWN_METHOD"})
                
    except Exception as e:
        return JSONResponse(content={"status": 0, "msg": "INTERNAL_ERROR"})

# 2. آليات أمان EuroVirtuals
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
    if token == "invalid-token-key" or not token or not signature:
        return {"status_code": 401, "status_description": "Invalid Security Headers"}
    if signature != hash_create_euro(payload, token):
        return {"status_code": 401, "status_description": "Invalid Signature"}
    return None

# 3. محفظة EuroVirtuals (الألعاب الافتراضية)
@app.post("/api/eurovirtuals/callback/player_info")
async def eurovirtuals_player_info(request: Request):
    try:
        payload = await request.json()
        sec_err = check_eurovirtuals_security(request, payload)
        if sec_err: return JSONResponse(content=sec_err, status_code=200)

        player_id = str(payload.get("player_id", ""))
        db = load_db()
        target_user = next((u for u in db if str(u.get("username", "")).lower() == player_id.lower()), None)

        if not target_user:
            return JSONResponse(content={"status_code": 500, "status_description": "Player not found"}, status_code=200)

        return JSONResponse(content={
            "status_code": 200, "status_description": "Success",
            "data": { "balance": float(target_user.get("balance", 0.0)), "currency": "TND", "player_id": target_user["username"], "date": time.strftime("%Y-%m-%d %H:%M:%S") }
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
        bet_data = bet_data_list[0] if isinstance(bet_data_list, list) and bet_data_list else {}
        player_id = str(payload.get("player_id") or bet_data.get("player_id") or "").strip()
        transaction_id = str(payload.get("transaction_id") or bet_data.get("transaction_id") or "").strip()
        amount = float(payload.get("amount") or bet_data.get("amount") or 0.0)
        current_time = time.strftime("%Y-%m-%d %H:%M:%S")

        async with db_lock:
            db = load_db()
            target_user = next((u for u in db if str(u.get("username", "")).lower() == player_id.lower()), None)
            if not target_user: return JSONResponse(content={"status_code": 500, "status_description": "Player not found"}, status_code=200)

            current_balance = float(target_user.get("balance", 0.0))
            if current_balance < amount:
                return JSONResponse(content={"status_code": 402, "status_description": "Insufficient Balance", "data": {"balance": current_balance, "currency": "TND", "reference_id": transaction_id, "date": current_time}}, status_code=200)

            db_session = SessionLocal()
            try:
                if transaction_id and db_session.query(Transaction).filter(Transaction.tx_id == transaction_id).first():
                    return JSONResponse(content={"status_code": 200, "status_description": "Success", "data": {"balance": current_balance, "currency": "TND", "reference_id": transaction_id, "date": current_time}}, status_code=200)
                
                new_balance = round(current_balance - amount, 2)
                target_user["balance"] = new_balance
                save_db(db)
                
                new_tx = Transaction(admin_username="EUROVIRTUALS_API", target_username=player_id, action="bet", amount=amount, date=current_time, tx_id=transaction_id)
                db_session.add(new_tx)
                db_session.commit()
            except:
                db_session.rollback()
            finally:
                db_session.close()

        return JSONResponse(content={"status_code": 200, "status_description": "Success", "data": {"balance": new_balance, "currency": "TND", "reference_id": transaction_id, "date": current_time}}, status_code=200)
    except Exception as e:
        return JSONResponse(content={"status_code": 500, "status_description": "Internal Error"}, status_code=200)

@app.post("/api/eurovirtuals/callback/win")
async def eurovirtuals_win(request: Request):
    try:
        payload = await request.json()
        sec_err = check_eurovirtuals_security(request, payload)
        if sec_err: return JSONResponse(content=sec_err, status_code=200)

        tx_id = str(payload.get("transaction_id", "")).strip()
        player_id = str(payload.get("player_id", "")).strip()
        payout_amount = float(payload.get("payout_amount") or payload.get("amount") or 0.0)
        current_time = time.strftime("%Y-%m-%d %H:%M:%S")

        async with db_lock:
            db = load_db()
            target_user = next((u for u in db if str(u.get("username", "")).lower() == player_id.lower()), None)
            if not target_user: return JSONResponse(content={"status_code": 500, "status_description": "Player not found"}, status_code=200)

            curr = float(target_user.get("balance", 0.0))
            db_session = SessionLocal()
            try:
                if db_session.query(Transaction).filter(Transaction.tx_id == tx_id).first():
                    return JSONResponse(content={"status_code": 200, "status_description": "Success", "data": {"balance": curr, "currency": "TND", "reference_id": tx_id, "date": current_time}}, status_code=200)
                
                new_balance = round(curr + payout_amount, 2)
                target_user["balance"] = new_balance
                save_db(db)

                new_tx = Transaction(admin_username="EUROVIRTUALS_API", target_username=player_id, action="win", amount=payout_amount, date=current_time, tx_id=tx_id)
                db_session.add(new_tx)
                db_session.commit()
            except:
                db_session.rollback()
            finally:
                db_session.close()

        return JSONResponse(content={"status_code": 200, "status_description": "Success", "data": {"balance": new_balance, "currency": "TND", "reference_id": tx_id, "date": current_time}}, status_code=200)
    except Exception as e:
        return JSONResponse(content={"status_code": 500, "status_description": "Internal Error"}, status_code=200)
# ==========================================
# 6️⃣ المسارات الناقصة (موافقة المانجر + استرجاع وتسويات المزود)
# ==========================================

# 1. مسار موافقة المانجر المباشرة على الإيداعات
@app.post("/api/admin/approve-deposit")
async def approve_deposit(req: ApproveDepositRequest, current_user: str = Depends(get_admin_user)):
    db_session = SessionLocal()
    try:
        # البحث عن الطلب
        tx = db_session.query(Transaction).filter(
            (Transaction.id == req.ticket_id) | (Transaction.tx_id == str(req.ticket_id))
        ).first()
        
        if not tx or tx.admin_username != "PENDING" or tx.action != "deposit_request":
            return JSONResponse(status_code=400, content={"detail": "الطلب غير صالح أو تمت معالجته مسبقاً"})
        
        async with db_lock:
            db = load_db()
            target_user = next((u for u in db if str(u.get("username", "")).lower() == tx.target_username.lower()), None)
            
            if not target_user:
                return JSONResponse(status_code=404, content={"detail": "حساب اللاعب غير موجود"})
            
            # تحديث الرصيد
            target_user["balance"] = round(float(target_user.get("balance", 0)) + float(req.amount), 2)
            
            # تسجيل الإيداع اليومي للكاش باك التلقائي
            target_user["daily_deposits"] = float(target_user.get("daily_deposits", 0)) + float(req.amount)
            save_db(db)

        # تحويل حالة الطلب إلى مقبول
        tx.admin_username = current_user
        tx.action = "charge"
        tx.amount = req.amount
        db_session.commit()
        
        log_admin_action(current_user, "APPROVE_DEPOSIT", f"تمت الموافقة على إيداع {req.amount} للاعب {tx.target_username}")
        return {"status": "success", "message": f"تمت الموافقة وإضافة {req.amount} TND بنجاح"}
    except Exception as e:
        db_session.rollback()
        return JSONResponse(status_code=500, content={"detail": f"خطأ داخلي: {str(e)}"})
    finally:
        db_session.close()


# 2. مسار استرجاع الأموال عند تعطل الألعاب (EuroVirtuals Rollback)
@app.post("/api/eurovirtuals/callback/rollback")
async def eurovirtuals_rollback(request: Request):
    try:
        payload = await request.json()
        sec_err = check_eurovirtuals_security(request, payload)
        if sec_err: return JSONResponse(content=sec_err, status_code=200)

        tx_id = str(payload.get("transaction_id", "")).strip()
        player_id = str(payload.get("player_id", "")).strip()
        amount = float(payload.get("amount") or payload.get("payout_amount") or 0.0)
        action = str(payload.get("action", "")).strip()
        
        # إذا كان الاسترجاع خاصاً بربح، نقوم بخصمه. وإذا كان رهاناً ملغياً، نعيده.
        is_rollback_win = "win" in action.lower()

        async with db_lock:
            db = load_db()
            target_user = next((u for u in db if str(u.get("username", "")).lower() == player_id.lower()), None)
            if not target_user: 
                return JSONResponse(content={"status_code": 500, "status_description": "Player not found"}, status_code=200)

            curr = float(target_user.get("balance", 0.0))
            db_session = SessionLocal()
            try:
                # التحقق من عدم معالجة الاسترجاع مسبقاً (حماية من التكرار)
                if db_session.query(Transaction).filter(Transaction.tx_id == tx_id).first():
                    return JSONResponse(content={"status_code": 200, "status_description": "Success", "data": {"balance": curr, "currency": "TND", "reference_id": tx_id}}, status_code=200)
                
                # تعديل الرصيد
                new_balance = round(curr - amount, 2) if is_rollback_win else round(curr + amount, 2)
                target_user["balance"] = new_balance
                save_db(db)

                # توثيق العملية
                new_tx = Transaction(admin_username="EUROVIRTUALS_API", target_username=player_id, action="rollback", amount=amount, date=time.strftime("%Y-%m-%d %H:%M:%S"), tx_id=tx_id)
                db_session.add(new_tx)
                db_session.commit()
            except:
                db_session.rollback()
            finally:
                db_session.close()

        return JSONResponse(content={"status_code": 200, "status_description": "Success", "data": {"balance": new_balance, "currency": "TND", "reference_id": tx_id}}, status_code=200)
    except Exception as e:
        return JSONResponse(content={"status_code": 500, "status_description": "Internal Error"}, status_code=200)


# 3. مسار التسويات المالية وإصلاح الأرصدة (EuroVirtuals Adjustment)
@app.post("/api/eurovirtuals/callback/adjustment")
async def eurovirtuals_adjustment(request: Request):
    try:
        payload = await request.json()
        sec_err = check_eurovirtuals_security(request, payload)
        if sec_err: return JSONResponse(content=sec_err, status_code=200)

        tx_id = str(payload.get("transaction_id", "")).strip()
        player_id = str(payload.get("player_id", "")).strip()
        amount = abs(float(payload.get("amount", 0.0)))
        action = str(payload.get("action", "")).strip()

        async with db_lock:
            db = load_db()
            target_user = next((u for u in db if str(u.get("username", "")).lower() == player_id.lower()), None)
            if not target_user: 
                return JSONResponse(content={"status_code": 500, "status_description": "Player not found"}, status_code=200)

            curr = float(target_user.get("balance", 0.0))
            db_session = SessionLocal()
            try:
                # التحقق من عدم التكرار
                if db_session.query(Transaction).filter(Transaction.tx_id == tx_id).first():
                    return JSONResponse(content={"status_code": 200, "status_description": "Success", "data": {"balance": curr, "currency": "TND", "reference_id": tx_id}}, status_code=200)
                
                # التسوية: خصم أو إضافة حسب طلب المزود
                new_balance = round(curr - amount, 2) if action == "wallet_adjustment_debit" else round(curr + amount, 2)
                target_user["balance"] = new_balance
                save_db(db)

                # توثيق التسوية
                new_tx = Transaction(admin_username="EUROVIRTUALS_API", target_username=player_id, action="adjustment", amount=amount, date=time.strftime("%Y-%m-%d %H:%M:%S"), tx_id=tx_id)
                db_session.add(new_tx)
                db_session.commit()
            except:
                db_session.rollback()
            finally:
                db_session.close()

        return JSONResponse(content={"status_code": 200, "status_description": "Success", "data": {"balance": new_balance, "currency": "TND", "reference_id": tx_id}}, status_code=200)
    except Exception as e:
        return JSONResponse(content={"status_code": 500, "status_description": "Internal Error"}, status_code=200)
# ==========================================
# 7️⃣ النماذج المتبقية لنظام الشوب وإيداع اللاعبين
# ==========================================
class ShopWithdrawRequest(BaseModel): 
    admin_username: str
    shop_username: str
    amount: float

class HandleShopWithdrawModel(BaseModel): 
    request_id: int
    decision: str
    shop_username: str

class DepositRequest(BaseModel):
    player: str
    method: str
    amount: float
    code: str
    receipt_image: Optional[str] = None

# ==========================================
# 8️⃣ مسار إيداع اللاعبين من الواجهة الأمامية (Frontend Deposit)
# ==========================================
@app.post("/api/deposit")
@limiter.limit("1/minute")
async def create_deposit(request: Request, req: DepositRequest):
    try:
        db = load_tickets_db()
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
        db.append(new_ticket)
        
        alert_msg = f"🚨 <b>عملية إيداع جديدة من الواجهة!</b>\n👤 اللاعب: <code>{new_ticket['username']}</code>\n💰 المبلغ: <b>{new_ticket['amount']}</b>\n💳 الطريقة: {new_ticket['method']}"
        asyncio.create_task(send_telegram_alert(alert_msg))
 
        save_tickets_db(db)
        return {"status": "success", "message": "تم إرسال طلب الإيداع بنجاح"}
    except Exception as e:
        print(f"Error in create_deposit: {e}")
        return {"status": "error", "message": "حدث خطأ أثناء معالجة الطلب"}

# ==========================================
# 9️⃣ مسارات نظام الوكيل / الشوب (Shop Withdrawals)
# ==========================================
@app.post("/api/admin/request-shop-withdrawal")
async def request_shop_withdrawal(req: ShopWithdrawRequest, current_user: str = Depends(get_admin_user)):
    try:
        db = load_db()
        admin_username = req.admin_username.lower()
        shop_username = req.shop_username.strip().lower() 
        amount = float(req.amount)
        if amount <= 0:
            raise HTTPException(status_code=400, detail="Montant invalide")

        shop = next((u for u in db if str(u.get("username")).strip().lower() == shop_username and u.get("role") == "shop"), None)
        if not shop: 
            raise HTTPException(status_code=404, detail="Shop non trouvé")

        if "shop_withdrawals" not in db.full_data: 
            db.full_data["shop_withdrawals"] = []
        
        new_req = {
            "id": int(datetime.now().timestamp()),
            "admin_username": admin_username,
            "shop_username": shop_username,
            "amount": amount,
            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "pending"
        }
        
        db.full_data["shop_withdrawals"].append(new_req)
        save_db(db)
        return {"status": "success", "message": "Demande envoyée avec succès"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur interne: {str(e)}")

@app.get("/api/shop/pending-withdrawals")
async def get_shop_pending_withdrawals(username: str, current_user: str = Depends(get_current_user)):
    db = load_db()
    withdrawals = db.full_data.get("shop_withdrawals", [])
    pending_reqs = [w for w in withdrawals if str(w.get("shop_username")).lower() == username.lower() and w.get("status") == "pending"]
    pending_reqs.reverse() 
    return pending_reqs

@app.get("/api/shop/withdraw-requests")
async def get_shop_withdraw_requests(username: str, current_user: str = Depends(get_current_user)):
    db = load_db()
    withdrawals = db.full_data.get("shop_withdrawals", [])
    target_shop = username.strip().lower()
    all_my_reqs = [w for w in withdrawals if str(w.get("shop_username")).strip().lower() == target_shop]
    all_my_reqs.reverse()
    return all_my_reqs

@app.post("/api/admin/handle-shop-withdrawal") 
async def handle_shop_withdrawal(req: HandleShopWithdrawModel, current_user: str = Depends(get_admin_user)):
    db = load_db()
    withdrawals = db.full_data.get("shop_withdrawals", [])
    target_req = next((w for w in withdrawals if w.get("id") == req.request_id), None)
    
    if not target_req: raise HTTPException(status_code=404, detail="Demande non trouvée")
    
    if req.decision == "accept":
        shop = next((u for u in db if u.get("username") == target_req["shop_username"]), None)
        admin = next((u for u in db if u.get("username") == target_req["admin_username"]), None)
        if not shop or not admin: raise HTTPException(status_code=404, detail="Utilisateur introuvable")
        
        amount = target_req["amount"]
        if float(shop.get("balance", 0)) < amount: raise HTTPException(status_code=400, detail="Solde insuffisant chez le shop")
        
        shop["balance"] = round(float(shop.get("balance", 0)) - amount, 2)
        admin["balance"] = round(float(admin.get("balance", 0)) + amount, 2)
        target_req["status"] = "accepted"
    else:
        target_req["status"] = "rejected"
        
    save_db(db)
    return {"status": "success", "message": "Traité avec succès"}

@app.get("/api/admin/my-withdrawal-requests")
async def get_my_withdrawal_requests(username: str, current_user: str = Depends(get_admin_user)):
    db = load_db()
    withdrawals = db.full_data.get("shop_withdrawals", [])
    return [w for w in withdrawals if w.get("admin_username") == username.lower()]

# ==========================================
# 🔟 دوال الصيانة والخدمات المساعدة (Utilities)
# ==========================================
@app.get("/api/admin/fix-user-ids")
async def fix_missing_user_ids(current_user: str = Depends(get_admin_user)):
    try:
        db = load_db()
        current_max_id = 0
        for u in db:
            user_id = u.get("id")
            if user_id is not None and str(user_id).isdigit():
                current_max_id = max(current_max_id, int(user_id))
                
        updated_count = 0
        for u in db:
            if "id" not in u or u.get("id") is None or u.get("id") == "":
                current_max_id += 1
                u["id"] = current_max_id
                updated_count += 1
                
        if updated_count > 0:
            save_db(db)
            
        return {"status": "success", "message": f"عملية ناجحة! تم منح ID جديد لـ {updated_count} حساب/حسابات قديمة."}
    except Exception as e:
        return {"status": "error", "message": f"حدث خطأ: {str(e)}"}

@app.get("/api/get-server-ip")
async def get_server_ip():
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get("https://api.ipify.org")
            return {"server_ip": response.text}
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/provider/get-games-paged")
async def get_games_paged(provider: str = "PRAGMATIC", page: int = 1, limit: int = 50):
    current_time = time.time()
    if provider in GAMES_CACHE and (current_time - GAMES_CACHE[provider]['time']) < CACHE_TIME_LIMIT:
        data = GAMES_CACHE[provider]['data']
        # إذا كان المزود يرسل الألعاب كمصفوفة
        games_list = data.get("games") or data.get("data") or data
        if isinstance(games_list, list):
            start = (page - 1) * limit
            end = start + limit
            return {"status": 1, "games": games_list[start:end], "total": len(games_list)}
        return data

    payload = {"method": "game_list", "agent_code": AGENT_CODE, "agent_token": AGENT_TOKEN, "provider_code": provider}
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(PROVIDER_ENDPOINT, json=payload, timeout=20)
            response_data = response.json()
            if response_data.get("status") == 1 or "games" in response_data:
                GAMES_CACHE[provider] = {'time': current_time, 'data': response_data}
            return response_data
        except Exception as e:
            return {"status": 0, "msg": "Error"}

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
    
    
