import warnings
from types import SimpleNamespace
from fastapi import FastAPI, Depends, HTTPException, status, Request, Form, Response, BackgroundTasks, File, UploadFile
from pydantic import BaseModel
from typing import List, Optional
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from .database import SessionLocal
import bcrypt
import re
import json
import uuid
import datetime
import shutil

import os
import json
import google.generativeai as genai
from groq import Groq
from dotenv import load_dotenv
from starlette.middleware.sessions import SessionMiddleware
from authlib.integrations.starlette_client import OAuth

from groq import AsyncGroq
import razorpay
from . import models, email_utils
from .email_utils import (
    send_email, 
    get_booking_template, 
    get_cancellation_template, 
    get_reset_password_template, 
    get_connection_request_template
)
from itsdangerous import URLSafeTimedSerializer
from .data.career_keywords import career_keywords
from .utils.resource_aggregator import ResourceAggregator
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

groq_client = AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# OAuth Setup
oauth = OAuth()
oauth.register(
    name='google',
    client_id=os.getenv('GOOGLE_CLIENT_ID'),
    client_secret=os.getenv('GOOGLE_CLIENT_SECRET'),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={
        'scope': 'openid email profile'
    }
)

# Password Reset Serializer
serializer = URLSafeTimedSerializer(os.getenv("SECRET_KEY", "a_very_secret_key_for_sessions"))

# Razorpay Client
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "rzp_test_your_key_id")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "your_key_secret")
razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

from .utils.redis_cache import ai_cache
from .utils.cache_utils import user_cache

async def generate_content_with_fallback(prompt):
    """
    Attempts to generate content using Gemini (Async) with high-tier fallback to Groq.
    Uses Redis caching to avoid repetitive API calls.
    """
    # Check Cache
    cached_response = ai_cache.get(prompt)
    if cached_response:
        print("AI CACHE HIT")
        return cached_response

    print("AI CACHE MISS")
    try:
        # Using 2.0 Flash for better reasoning speed and instruction following
        model = genai.GenerativeModel("gemini-1.5-flash") # or gemini-2.0-flash if available
        response = await model.generate_content_async(prompt)
        text = response.text
    except Exception as e:
        print(f"Gemini Error (Switching to Groq): {e}")
        if not groq_client: raise e
        
        try:
            # Fallback to high-reasoning Llama model
            chat_completion = await groq_client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model="llama-3.3-70b-versatile",
            )
            text = chat_completion.choices[0].message.content
        except Exception as groq_e:
            raise Exception(f"Dual API Failure. Gemini: {e}, Groq: {groq_e}")

    # Enhanced JSON Extraction Logic
    try:
        # Remove potential markdown wrappers
        text = re.sub(r'```json\s*|\s*```', '', text).strip()
        # Find the first { and the last }
        start_idx = text.find('{')
        end_idx = text.rfind('}')
        if start_idx != -1 and end_idx != -1:
            text = text[start_idx:end_idx + 1]
        
        # Clean trailing commas before closing braces/brackets
        text = re.sub(r",\s*([\]}])", r"\1", text)
        
        # Save to Cache
        ai_cache.set(prompt, text)
        
        return text
    except Exception:
        # Still cache raw text if extraction fails partially
        ai_cache.set(prompt, text)
        return text

async def check_content_moderation(text_content: str):
    """
    Checks if the given text contains abusive or inappropriate content using the AI model.
    Returns: (is_flagged, reason)
    """
    moderation_prompt = f"""
    Analyze the following text for abusive language, hate speech, harassment, or highly inappropriate content for a student career guidance platform.
    Text: "{text_content}"
    
    Respond STRICTLY in JSON format:
    {{
      "is_flagged": boolean,
      "reason": "string describing the violation or 'None'"
    }}
    """
    try:
        response_json_str = await generate_content_with_fallback(moderation_prompt)
        data = json.loads(response_json_str)
        return data.get("is_flagged", False), data.get("reason", "None")
    except Exception as e:
        print(f"Moderation Error: {e}")
        return False, "None"

from . import models
from .database import SessionLocal, engine, get_db
from data.questions_data import questions
from data.questions_12th import questions_12th
from data.questions_above_12th import questions_above_12th

# Create Tables
try:
    models.Base.metadata.create_all(bind=engine)
except Exception as e:
    print(f"Database initialization error: {e}")

# Auto-migrate: add missing columns to existing tables
def run_migrations():
    """Add new columns to existing tables if they don't exist."""
    from sqlalchemy import text, inspect
    
    try:
        inspector = inspect(engine)
        
        # Get existing columns for each table
        def get_columns(table_name):
            try:
                return [col['name'] for col in inspector.get_columns(table_name)]
            except Exception:
                return []
        
        migrations = []
        
        # 1. Users table
        u_cols = get_columns('users')
        if u_cols:
            if 'profile_photo' not in u_cols: migrations.append("ALTER TABLE users ADD COLUMN profile_photo VARCHAR")
            if 'bio' not in u_cols: migrations.append("ALTER TABLE users ADD COLUMN bio TEXT")
            if 'is_suspended' not in u_cols: migrations.append("ALTER TABLE users ADD COLUMN is_suspended BOOLEAN DEFAULT FALSE")
            if 'contact_number' not in u_cols: migrations.append("ALTER TABLE users ADD COLUMN contact_number VARCHAR")
            if 'full_name' not in u_cols: migrations.append("ALTER TABLE users ADD COLUMN full_name VARCHAR")
            if 'role' not in u_cols: migrations.append("ALTER TABLE users ADD COLUMN role VARCHAR")

        # 2. Counsellor Profiles
        cp_cols = get_columns('counsellor_profiles')
        if cp_cols:
            checklist = [
                ('tnc_accepted', "BOOLEAN DEFAULT FALSE"), ('tnc_accepted_at', "TIMESTAMP"),
                ('is_blocked', "BOOLEAN DEFAULT FALSE"), ('block_reason', "VARCHAR"),
                ('certificates', "TEXT"), ('experience', "TEXT"),
                ('is_verified', "BOOLEAN DEFAULT FALSE"), ('verification_status', "VARCHAR DEFAULT 'pending'"),
                ('fee_locked', "BOOLEAN DEFAULT FALSE"), ('razorpay_account_id', "VARCHAR"),
                ('onboarding_status', "VARCHAR DEFAULT 'not_started'"), ('razorpay_contact_id', "VARCHAR"),
                ('razorpay_fund_account_id', "VARCHAR"), ('average_rating', "FLOAT DEFAULT 5.0"),
                ('rating_count', "INTEGER DEFAULT 0"), ('is_founding_counsellor', "BOOLEAN DEFAULT FALSE"),
                ('founding_badge_awarded_at', "TIMESTAMP"), ('commission_free_until', "TIMESTAMP")
            ]
            for col, ty in checklist:
                if col not in cp_cols: migrations.append(f"ALTER TABLE counsellor_profiles ADD COLUMN {col} {ty}")

        # 3. Appointments
        ap_cols = get_columns('appointments')
        if ap_cols:
            for col, ty in [('counsellor_joined', 'BOOLEAN DEFAULT FALSE'), ('joined_at', 'TIMESTAMP'), 
                           ('student_joined', 'BOOLEAN DEFAULT FALSE'), ('student_joined_at', 'TIMESTAMP'),
                           ('actual_overlap_minutes', 'INTEGER DEFAULT 0')]:
                if col not in ap_cols: migrations.append(f"ALTER TABLE appointments ADD COLUMN {col} {ty}")

        # 4. Student Messages
        sm_cols = get_columns('student_messages')
        if sm_cols:
            if 'attachment_path' not in sm_cols: migrations.append("ALTER TABLE student_messages ADD COLUMN attachment_path VARCHAR")
            if 'attachment_type' not in sm_cols: migrations.append("ALTER TABLE student_messages ADD COLUMN attachment_type VARCHAR")

        # 5. Assessment Results
        ar_cols = get_columns('assessment_results')
        if ar_cols:
            for col, ty in [('selected_class', 'VARCHAR'), ('phase3_result', 'VARCHAR'), 
                           ('phase3_answers', 'JSON'), ('phase3_analysis', 'TEXT'),
                           ('final_answers', 'JSON'), ('stream_scores', 'JSON'),
                           ('recommended_stream', 'VARCHAR'), ('final_analysis', 'TEXT'),
                           ('stream_pros', 'JSON'), ('stream_cons', 'JSON')]:
                if col not in ar_cols: migrations.append(f"ALTER TABLE assessment_results ADD COLUMN {col} {ty}")

        if migrations:
            print(f"DEBUG: Found {len(migrations)} pending migrations.", flush=True)
            with engine.connect() as conn:
                for sql in migrations:
                    try:
                        conn.execute(text(sql))
                        print(f"DATABASE MIGRATION SUCCESS: {sql}", flush=True)
                    except Exception as me:
                        print(f"DATABASE MIGRATION SKIP/ERROR: {sql} -> {me}", flush=True)
                conn.commit()
            print(f"DATABASE: Finished running {len(migrations)} migration queries.", flush=True)
        else:
            print("DATABASE: No new migrations detected.", flush=True)
    except Exception as e:
        print(f"DATABASE FATAL ERROR during migration check: {e}", flush=True)
        import traceback
        traceback.print_exc()

run_migrations()

app = FastAPI(title="CareStance")

# ─── Include Split Payments Router (Razorpay Route) ───────────────────────────
from .routes.payments import router as payments_router
app.include_router(payments_router)

# Global Exception Handler for better debugging
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    import traceback
    print(f"GLOBAL ERROR: {exc}", flush=True)
    traceback.print_exc()
    return HTMLResponse(
        content=f"<html><body><h1>Internal Server Error</h1><p>{exc}</p><pre>{traceback.format_exc()}</pre></body></html>",
        status_code=500
    )

from fastapi.middleware.gzip import GZipMiddleware
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Add Session Middleware (needed for OAuth)
# On Vercel (HTTPS), cookies must have Secure flag to survive cross-site OAuth redirects
_is_production = bool(os.getenv("VERCEL") or os.getenv("BASE_URL", "").startswith("https"))
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SECRET_KEY", "a_very_secret_key_for_sessions"),
    same_site="lax",
    https_only=_is_production,
    max_age=14 * 24 * 60 * 60,  # 14 days
)

@app.middleware("http")
async def check_suspension(request: Request, call_next):
    # Paths that suspended users can still access
    exempt_paths = [
        "/suspended", 
        "/logout", 
        "/static", 
        "/login", 
        "/signup", 
        "/", 
        "/auth/google", 
        "/auth/callback",
        "/favicon.ico",
        "/ticket/submit"
    ]
    
    path = request.url.path
    # Check if the path is specifically exempt
    is_exempt = any(path == p or path.startswith("/static/") or path.startswith("/auth/") for p in exempt_paths)
    
    if not is_exempt:
        user_id = request.cookies.get("user_id")
        if user_id:
            try:
                uid = int(user_id)
                # Quick cache-only check for suspension
                cached_status = user_cache.get_user_status(uid)
                if cached_status and cached_status.get("is_suspended"):
                    return RedirectResponse(url="/suspended", status_code=status.HTTP_302_FOUND)
            except Exception: pass

    # Skip caching for static files to keep response fast but headers default
    response = await call_next(request)
    return response

# Mount Static & Templates
# Mount Static & Templates
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
# Re-enabled cache as standard practice
# pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto"]) # Removed

def verify_password(plain_password, hashed_password):
    # Ensure bytes for bcrypt
    if isinstance(hashed_password, str):
        hashed_password = hashed_password.encode('utf-8')
    return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password)

def get_password_hash(password):
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def get_current_user(request: Request, db: Session = Depends(get_db)):
    user_id = request.cookies.get("user_id")
    if not user_id: return None
    try:
        uid = int(user_id)
        # Hit DB directly to avoid any weird dict issues in templates
        return db.query(models.User).filter(models.User.id == uid).first()
    except Exception: return None

# Routes

@app.get("/ads.txt")
async def ads_txt():
    # Looks for ads.txt in the project root (one level above 'app' folder)
    root_dir = os.path.dirname(BASE_DIR)
    ads_path = os.path.join(root_dir, "ads.txt")
    if os.path.exists(ads_path):
        from fastapi.responses import FileResponse
        return FileResponse(ads_path)
    return Response(content="File not found", status_code=404)

@app.get("/", response_class=HTMLResponse)
async def home(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    try:
        # Bypassing Starlette's TemplateResponse to avoid internal dict vs string ambiguity
        template = templates.get_template("landing.html")
        content = template.render({"request": request, "user": user})
        return HTMLResponse(content=content)
    except Exception as e:
        import traceback
        return HTMLResponse(content=f"Template Error: {e}<br><pre>{traceback.format_exc()}</pre>", status_code=500)

@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    return templates.TemplateResponse(request=request, name="signup.html")

@app.post("/signup")
async def signup(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(...),
    contact_number: str = Form(...),
    role: str = Form("student"),
    db: Session = Depends(get_db)
):
    # Check existing user
    user = db.query(models.User).filter(models.User.email == email).first()
    if user:
        return templates.TemplateResponse(request=request, name="signup.html", context={"error": "Email already exists"})
    
    try:
        # Create User
        hashed_pw = get_password_hash(password)
        new_user = models.User(email=email, hashed_password=hashed_pw, full_name=full_name, contact_number=contact_number, role=role)
        db.add(new_user)
        db.flush()
        
        # Create Counsellor Profile
        if role == "counsellor":
            c_profile = models.CounsellorProfile(user_id=new_user.id)
            db.add(c_profile)
        
        db.commit()
    except Exception as e:
        print(f"Signup error: {e}")
        db.rollback()
        return templates.TemplateResponse(request=request, name="signup.html", context={"error": "An error occurred during signup. Please try again."})
    
    return RedirectResponse(url="/login?message=Account created! Please login.", status_code=status.HTTP_302_FOUND)

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html")

@app.post("/login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    user = db.query(models.User).filter(models.User.email == email).first()
    if not user or not verify_password(password, user.hashed_password):
         return templates.TemplateResponse(request=request, name="login.html", context={"error": "Invalid credentials"})
    
    response = RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    # 30 day persistent session
    response.set_cookie(
        key="user_id", 
        value=str(user.id), 
        max_age=30 * 24 * 60 * 60,
        httponly=True,
        samesite="lax"
    )
    return response

@app.get("/logout")
async def logout(response: Response):
    response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    response.delete_cookie("user_id")
    return response

@app.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    return templates.TemplateResponse(request=request, name="forgot_password.html")

@app.post("/forgot-password")
async def forgot_password(
    request: Request,
    background_tasks: BackgroundTasks,
    email: str = Form(...),
    db: Session = Depends(get_db)
):
    user = db.query(models.User).filter(models.User.email == email).first()
    if user:
        token = serializer.dumps(email, salt="password-reset-salt")
        reset_link = f"{request.base_url}reset-password/{token}"
        
        # Send Email
        background_tasks.add_task(
            send_email,
            email,
            "Reset Your CareStance Password 🔒",
            get_reset_password_template(user.full_name, reset_link)
        )
    
    # Always show success message for security (don't reveal if email exists)
    return templates.TemplateResponse(request=request, name="forgot_password.html", context={
        "message": "If an account exists with that email, a reset link has been sent."
    })

@app.get("/reset-password/{token}", response_class=HTMLResponse)
async def reset_password_page(request: Request, token: str):
    try:
        # Token valid for 1 hour (3600 seconds)
        email = serializer.loads(token, salt="password-reset-salt", max_age=3600)
    except Exception:
        return templates.TemplateResponse(request=request, name="forgot_password.html", context={
            "error": "The reset link is invalid or has expired."
        })
    return templates.TemplateResponse(request=request, name="reset_password.html", context={"token": token})

@app.post("/reset-password/{token}")
async def reset_password(
    request: Request,
    token: str,
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db)
):
    try:
        email = serializer.loads(token, salt="password-reset-salt", max_age=3600)
    except Exception:
         return templates.TemplateResponse(request=request, name="forgot_password.html", context={
            "error": "The reset link is invalid or has expired."
        })
    
    if password != confirm_password:
        return templates.TemplateResponse(request=request, name="reset_password.html", context={
            "token": token, 
            "error": "Passwords do not match"
        })
    
    user = db.query(models.User).filter(models.User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    try:
        user.hashed_password = get_password_hash(password)
        db.commit()
    except Exception as e:
        print(f"Password reset error: {e}")
        db.rollback()
        return templates.TemplateResponse(request=request, name="reset_password.html", context={
            "token": token, 
            "error": "Failed to update password. Please try again."
        })
    
    return RedirectResponse(url="/login?message=Password updated successfully", status_code=status.HTTP_302_FOUND)

@app.get("/login/google")
async def login_google(request: Request):
    if not os.getenv('GOOGLE_CLIENT_ID'):
        print("ERROR: GOOGLE_CLIENT_ID not found in environment!")
        return RedirectResponse(url='/login?error=Configuration missing', status_code=status.HTTP_302_FOUND)
    
    # Build redirect_uri: prefer BASE_URL env var, fallback to request-based URL
    base_url = os.getenv("BASE_URL")
    if base_url:
        redirect_uri = f"{base_url.rstrip('/')}/auth/callback"
    else:
        redirect_uri = str(request.url_for('auth_callback'))
        if "vercel.app" in str(request.base_url) or os.getenv("VERCEL"):
            redirect_uri = redirect_uri.replace("http://", "https://")
    
    print(f"DEBUG: OAuth Redirect URI: {redirect_uri}")
    return await oauth.google.authorize_redirect(request, redirect_uri)

@app.get("/auth/callback")
async def auth_callback(request: Request, db: Session = Depends(get_db)):
    try:
        # authlib retrieves redirect_uri from session automatically — do NOT pass it again
        token = await oauth.google.authorize_access_token(request)
    except Exception as e:
        import traceback
        print(f"OAuth Token Exchange Error: {e}")
        traceback.print_exc()
        error_msg = str(e).replace(" ", "+")[:200]
        return RedirectResponse(url=f'/login?error={error_msg}', status_code=status.HTTP_302_FOUND)
    
    user_info = token.get('userinfo')
    if not user_info:
        return RedirectResponse(url='/login?error=No user info', status_code=status.HTTP_302_FOUND)
    
    email = user_info.get('email')
    full_name = user_info.get('name', 'Google User')
    
    # Check if user exists, otherwise create with no role (will be selected next)
    user = db.query(models.User).filter(models.User.email == email).first()
    is_new_user = False
    if not user:
        hashed_pw = get_password_hash(os.urandom(24).hex())
        user = models.User(email=email, hashed_password=hashed_pw, full_name=full_name, contact_number=None, role=None)
        db.add(user)
        db.commit()
        db.refresh(user)
        is_new_user = True
    
    # Users without a role must select it first
    redirect_url = "/select-role" if (is_new_user or not user.role) else "/dashboard"
    response = RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)
    response.set_cookie(key="user_id", value=str(user.id))
    return response

@app.get("/suspended", response_class=HTMLResponse)
async def suspended_page(request: Request):
    return templates.TemplateResponse(request=request, name="suspended.html")

@app.get("/select-role", response_class=HTMLResponse)
async def select_role_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    # If user already has a role, skip this page
    if user.role:
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse(request=request, name="select_role.html", context={"user": user})

@app.post("/select-role")
async def select_role(
    request: Request,
    role: str = Form(...),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    try:
        if role not in ("student", "counsellor"):
            return RedirectResponse(url="/select-role", status_code=status.HTTP_302_FOUND)
            
        user.role = role
        db.add(user)
        
        # Create counsellor profile if needed
        if role == "counsellor":
            existing_profile = db.query(models.CounsellorProfile).filter(models.CounsellorProfile.user_id == user.id).first()
            if not existing_profile:
                profile = models.CounsellorProfile(user_id=user.id)
                db.add(profile)
        
        db.commit()
        user_cache.invalidate_user(user.id)
    except Exception as e:
        print(f"Role selection error: {e}")
        db.rollback()
        return templates.TemplateResponse(request=request, name="select_role.html", context={
            "error": "An error occurred while saving your role. Please try again."
        })
    
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)

# --- Assessment Data ---

QUESTIONS = [
    {
        "id": "Q1_SocialBattery",
        "title": "Social Battery",
        "options": [
            {"value": "A", "text": "A quiet morning with a book/coffee.", "image": "/static/images/assessment/q1_a.png"},
            {"value": "B", "text": "A high-energy concert with a crowd.", "image": "/static/images/assessment/q1_b.png"}
        ]
    },
    {
        "id": "Q2_Communication",
        "title": "Communication",
        "options": [
            {"value": "A", "text": "Sending a carefully crafted email.", "image": "/static/images/assessment/q2_a.png"},
            {"value": "B", "text": "Hopping on a quick, 'face-to-face' video call.", "image": "/static/images/assessment/q2_b.png"}
        ]
    },
    {
        "id": "Q3_Workspace",
        "title": "Workspace",
        "options": [
            {"value": "A", "text": "A private pod with noise-canceling headphones.", "image": "/static/images/assessment/q3_a.png"},
            {"value": "B", "text": "A bustling co-working space with open desks.", "image": "/static/images/assessment/q3_b.png"}
        ]
    },
    {
        "id": "Q4_ProblemSolving",
        "title": "Problem Solving",
        "options": [
            {"value": "A", "text": "Digging through Google/Manuals solo.", "image": "/static/images/assessment/q4_a.png"},
            {"value": "B", "text": "Bouncing ideas off a group on a whiteboard.", "image": "/static/images/assessment/q4_b.png"}
        ]
    },
    {
        "id": "Q5_MeetingRole",
        "title": "Meeting Role",
        "options": [
            {"value": "A", "text": "The person taking detailed, silent notes.", "image": "/static/images/assessment/q5_a.png"},
            {"value": "B", "text": "The person leading the brainstorm out loud.", "image": "/static/images/assessment/q5_b.png"}
        ]
    },
 {
        "id": "Q6_LearningStyle",
        "title": "Learning Style",
        "options": [
            {"value": "A", "text": "Watching a deep-dive documentary alone.", "image": "/static/images/assessment/q6_a.png"},
            {"value": "B", "text": "Attending a live, interactive workshop.", "image": "/static/images/assessment/q6_b.png"}
        ]
    },
    {
        "id": "Q7_GoalPath",
        "title": "Goal: Path",
        "options": [
            {"value": "A", "text": "A straight highway with a clear destination.", "image": "/static/images/assessment/q7_a.png"},
            {"value": "B", "text": "A winding trail through a beautiful forest.", "image": "/static/images/assessment/q7_b.png"}
        ]
    },
    {
        "id": "Q8_GoalVision",
        "title": "Goal: Vision",
        "options": [
            {"value": "A", "text": "I have a 'Dream Job' title in my head.", "image": "/static/images/assessment/q8_a.png"},
            {"value": "B", "text": "I have a 'Lifestyle' I want, but the job is tbd.", "image": "/static/images/assessment/q8_b.png"}
        ]
    },
    {
        "id": "Q9_GoalSpeed",
        "title": "Goal: Speed",
        "options": [
            {"value": "A", "text": "I want to specialize and be the best at one thing.", "image": "/static/images/assessment/q9_a.png"},
            {"value": "B", "text": "I want to be a 'Jack of all trades' and know a bit of everything.", "image": "/static/images/assessment/q9_b.png"}
        ]
    },
    {
        "id": "Q10_GoalChoice",
        "title": "Goal: Choice",
        "options": [
            {"value": "A", "text": "I’d pick the 'Safe & Known' successful path.", "image": "/static/images/assessment/q10_a.png"},
            {"value": "B", "text": "I’d pick the 'Wildcard' path with high potential.", "image": "/static/images/assessment/q10_b.png"}
        ]
    }
]


# --- Assessment Routes ---

@app.get("/assessment/start")
async def assessment_start(request: Request, class_level: str, db: Session = Depends(get_db)):
    """Phase 1: Class Selection"""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    try:
        # Check/Create Result
        result = db.query(models.AssessmentResult).filter(models.AssessmentResult.user_id == user.id).first()
        if not result:
            result = models.AssessmentResult(user_id=user.id)
            db.add(result)
        
        # Save Phase 1 Selection
        result.selected_class = class_level
        db.commit()
    except Exception as e:
        print(f"Assessment start error: {e}")
        db.rollback()
        # Fallback to landing or dashboard
        return RedirectResponse(url="/dashboard?error=Assessment+failed+to+start", status_code=status.HTTP_302_FOUND)
    
    # Proceed to Phase 2 (Archetype)
    return RedirectResponse(url="/assessment", status_code=status.HTTP_302_FOUND)


@app.get("/assessment", response_class=HTMLResponse)
async def assessment_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse(request=request, name="assessment.html", context={"user": user, "questions": QUESTIONS})

@app.post("/assessment/submit")
async def assessment_submit(
    request: Request,
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    # 1. Collect Answers & Map to Text
    form_data = await request.form()
    user_answers_data = {}
    
    # helper map
    questions_map = {q["id"]: q for q in questions}

    for key, value in form_data.items():
        if key in questions_map:
            q_data = questions_map[key]
            # Find the selected option text
            selected_option = next((opt for opt in q_data["options"] if opt["value"] == value), None)
            if selected_option:
                user_answers_data[key] = selected_option["text"]
            else:
                 user_answers_data[key] = value # Fallback
        else:
             user_answers_data[key] = value

    # 2. Construct Prompt
    prompt = f"""
    You are an expert student career psychologist.

    Your task:
    1. Identify the user's Personality Type based on Q1–Q6:
       - Introvert
       - Ambivert
       - Extrovert

    2. Identify Goal Status based on Q7–Q10:
       - Goal Aware
       - Exploring

    3. Combine them into ONE of these 6 categories (Phase 2 Category):
       - Focused Specialist
       - Quiet Explorer
       - Strategic Builder
       - Adaptive Explorer
       - Visionary Leader
       - Dynamic Generalist

    Rules:
    - Do NOT invent traits.
    - Use majority patterns, but handle mixed answers intelligently.
    - Output must be VALID JSON only. Do not include markdown formatting like ```json.
    
    Structure:
    {{
      "personality": "String",
      "goal_status": "String",
      "phase_2_category": "String",
      "confidence": Float (0.0-1.0),
      "reasoning": "String (2-3 sentences max)"
    }}

    User Answers (Text Descriptions of Visual Choices):
    {json.dumps(user_answers_data, indent=2)}
    """

    # 3. Call Gemini
    if not GEMINI_API_KEY:
        # Fallback Mock for Demo if Key Missing
        result_data = {
            "personality": "Ambivert",
            "goal_status": "Exploring",
            "phase_2_category": "Adaptive Explorer",
            "confidence": 0.85,
            "reasoning": "Demo Mode: API Key missing. You showed balanced traits."
        }
    else:
        # Generate Analysis using Fallback Strategy
        try:
            clean_text = await generate_content_with_fallback(prompt)
            result_data = json.loads(clean_text)
        except Exception as e:
            print(f"Analysis Error: {e}")
            result_data = {
                "phase_2_category": "Focused Specialist",
                "personality": "Ambivert",
                "goal_status": "Exploring",
            "confidence": 0.5,
                "reasoning": "AI Analysis unavailable. Default profile assigned based on answers."
            }

    # 4. Save to DB
    try:
        existing_result = db.query(models.AssessmentResult).filter(models.AssessmentResult.user_id == user.id).first()
        
        if existing_result:
            # Update existing
            existing_result.phase_2_category = result_data.get("phase_2_category")
            existing_result.personality = result_data.get("personality")
            existing_result.goal_status = result_data.get("goal_status")
            existing_result.confidence = result_data.get("confidence")
            existing_result.reasoning = result_data.get("reasoning")
            existing_result.raw_answers = user_answers_data # This overwrites phase 1 raw answers if any, but selected_class is separate column
        else:
            # Create new
            new_result = models.AssessmentResult(
                user_id=user.id,
                phase_2_category=result_data.get("phase_2_category"),
                personality=result_data.get("personality"),
                goal_status=result_data.get("goal_status"),
                confidence=result_data.get("confidence"),
                reasoning=result_data.get("reasoning"),
                raw_answers=user_answers_data
            )
            db.add(new_result)
        
        db.commit()
    except Exception as e:
        print(f"Assessment save error: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to save assessment results. Please try again.")

    return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)

@app.get("/assessment/result", response_class=HTMLResponse)
async def assessment_result(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    result = db.query(models.AssessmentResult).filter(models.AssessmentResult.user_id == user.id).first()
    if not result:
        return RedirectResponse(url="/assessment", status_code=status.HTTP_302_FOUND)

    return templates.TemplateResponse(request=request, name="result.html", context={"user": user, "result": result})

@app.get("/share/report/{result_id}", response_class=HTMLResponse)
async def share_report(result_id: int, request: Request, mode: str = "full", db: Session = Depends(get_db)):
    """Publicly shareable route for career reports."""
    result = db.query(models.AssessmentResult).filter(models.AssessmentResult.id == result_id).first()
    if not result:
        raise HTTPException(status_code=404, detail="Report not found")
    
    owner = db.query(models.User).filter(models.User.id == result.user_id).first()
    current_user = get_current_user(request, db)
    
    return templates.TemplateResponse(request=request, name="result.html", context={
        "user": current_user, 
        "owner": owner,
        "result": result,
        "is_public_share": True,
        "mode": mode
    })

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    # New OAuth users must select a role first
    if not user.role:
        return RedirectResponse(url="/select-role", status_code=status.HTTP_302_FOUND)
    
    if user.role == "counsellor":
        profile = db.query(models.CounsellorProfile).filter(models.CounsellorProfile.user_id == user.id).first()
        # Only show active/scheduled appointments on dashboard
        appointments = db.query(models.Appointment).filter(
            models.Appointment.counsellor_id == user.id,
            models.Appointment.status == "scheduled"
        ).all()
        # Fetch unread notifications for this counsellor
        notifications = db.query(models.Notification).filter(
            models.Notification.user_id == user.id,
            models.Notification.is_read == False
        ).order_by(models.Notification.created_at.desc()).all()

        # --- Overview Panel Metrics ---
        from sqlalchemy import func
        from datetime import date, timedelta
        
        today = date.today()
        month_start = today.replace(day=1)

        # 👥 Total Clients (Unique students who have booked at least one session)
        total_clients = db.query(models.Appointment.student_id).filter(
            models.Appointment.counsellor_id == user.id
        ).distinct().count()

        # 📅 Today’s Sessions
        today_sessions = db.query(models.Appointment).filter(
            models.Appointment.counsellor_id == user.id,
            func.date(models.Appointment.appointment_time) == today,
            models.Appointment.status == "scheduled"
        ).count()

        # 💰 Earnings
        # Daily Earnings (Transfers processed today)
        earnings_daily = db.query(func.sum(models.Transfer.amount)).filter(
            models.Transfer.counsellor_id == user.id,
            func.date(models.Transfer.created_at) == today,
            models.Transfer.status == "processed"
        ).scalar() or 0.0

        # Monthly Earnings (Transfers processed this month)
        earnings_monthly = db.query(func.sum(models.Transfer.amount)).filter(
            models.Transfer.counsellor_id == user.id,
            models.Transfer.created_at >= month_start,
            models.Transfer.status == "processed"
        ).scalar() or 0.0

        # 📈 Active vs Inactive Clients (Active = had a session in the last 30 days)
        last_30_days = today - timedelta(days=30)
        active_clients = db.query(models.Appointment.student_id).filter(
            models.Appointment.counsellor_id == user.id,
            models.Appointment.appointment_time >= last_30_days
        ).distinct().count()
        inactive_clients = total_clients - active_clients

        # ⭐ Rating
        avg_rating = profile.average_rating if (profile and profile.average_rating is not None) else 5.0

        # Fetch recent reviews
        recent_reviews = db.query(models.CounselorRating).filter(
            models.CounselorRating.counsellor_id == user.id
        ).order_by(models.CounselorRating.timestamp.desc()).limit(5).all()

        dashboard_stats = {
            "total_clients": total_clients,
            "today_sessions": today_sessions,
            "earnings_daily": earnings_daily,
            "earnings_monthly": earnings_monthly,
            "active_clients": active_clients,
            "inactive_clients": max(0, inactive_clients),
            "avg_rating": avg_rating
        }

        return templates.TemplateResponse(request=request, name="counsellor_dashboard.html", context={
            "user": user, 
            "profile": profile, 
            "appointments": appointments, 
            "notifications": notifications,
            "stats": dashboard_stats,
            "reviews": recent_reviews
        })
    
    # Fetch assessment result to show on dashboard
    assessment = db.query(models.AssessmentResult).filter(models.AssessmentResult.user_id == user.id).first()
    
    # Fetch student appointments (scheduled & completed for rating)
    appointments = db.query(models.Appointment).filter(
        models.Appointment.student_id == user.id,
        models.Appointment.status.in_(["scheduled", "completed"])
    ).order_by(models.Appointment.appointment_time.desc()).all()
    
    # Fetch student tickets
    tickets = db.query(models.Ticket).filter(models.Ticket.user_id == user.id).order_by(models.Ticket.timestamp.desc()).all()
    
    # Count pending connection requests for badge
    pending_conn_count = db.query(models.StudentConnection).filter(
        models.StudentConnection.receiver_id == user.id,
        models.StudentConnection.status == "pending"
    ).count()
    
    return templates.TemplateResponse(request=request, name="dashboard.html", context={
        "user": user, 
        "assessment": assessment,
        "appointments": appointments,
        "tickets": tickets,
        "pending_conn_count": pending_conn_count
    })

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request, 
    db: Session = Depends(get_db),
    user_page: int = 1,
    feedback_page: int = 1,
    ticket_page: int = 1,
    page_size: int = 20
):
    try:
        current_user = get_current_user(request, db)
        if not current_user:
             return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
        
        admin_email = os.getenv("ADMIN_EMAIL")
        if current_user.role != "admin" and (not admin_email or current_user.email != admin_email):
            print(f"DEBUG: Admin access denied for {current_user.email}")
            return RedirectResponse(url="/dashboard?error=Admin access denied", status_code=status.HTTP_302_FOUND)

        # ─── Paginated Data ──────────────────────────────────────────────
        all_users = db.query(models.User).order_by(models.User.id.desc()).offset((user_page - 1) * page_size).limit(page_size).all()
        total_users = db.query(models.User).count()

        all_feedback = db.query(models.Feedback).order_by(models.Feedback.timestamp.desc()).offset((feedback_page - 1) * page_size).limit(page_size).all()
        total_feedback = db.query(models.Feedback).count()

        all_tickets = db.query(models.Ticket).order_by(models.Ticket.timestamp.desc()).offset((ticket_page - 1) * page_size).limit(page_size).all()
        total_tickets = db.query(models.Ticket).count()

        pending_counsellors = db.query(models.CounsellorProfile).filter(models.CounsellorProfile.verification_status == "pending").all()
        
        # ─── Optimized Counsellor Stats (Single Query) ───────────────────
        from sqlalchemy import func as sql_func
        
        # Get all completed sessions count per counsellor
        completed_sessions = db.query(
            models.Appointment.counsellor_id,
            sql_func.count(models.Appointment.id).label("count")
        ).filter(models.Appointment.status == "completed").group_by(models.Appointment.counsellor_id).all()
        completed_map = {row.counsellor_id: row.count for row in completed_sessions}

        # Get total sessions count per counsellor
        total_sessions = db.query(
            models.Appointment.counsellor_id,
            sql_func.count(models.Appointment.id).label("count")
        ).group_by(models.Appointment.counsellor_id).all()
        total_map = {row.counsellor_id: row.count for row in total_sessions}

        all_counsellors = db.query(models.CounsellorProfile).all()
        for cp in all_counsellors:
            cp.session_count = completed_map.get(cp.user_id, 0)
            cp.total_sessions = total_map.get(cp.user_id, 0)

        # ─── Payment Split Analytics ──────────────────────────────────────
        try:
            all_payments = db.query(models.Payment).order_by(models.Payment.created_at.desc()).limit(20).all()
            
            # Using scalars directly for performance
            total_revenue = db.query(sql_func.sum(models.Payment.amount)).filter(
                models.Payment.status == "captured"
            ).scalar() or 0.0

            total_counselor_payouts = db.query(sql_func.sum(models.Transfer.amount)).filter(
                models.Transfer.status == "processed"
            ).scalar() or 0.0

            platform_commission = total_revenue - total_counselor_payouts

            pending_transfers = db.query(models.Transfer).filter(models.Transfer.status == "pending").count()
            failed_transfers = db.query(models.Transfer).filter(models.Transfer.status == "failed").count()
            captured_payments_count = db.query(models.Payment).filter(models.Payment.status == "captured").count()
        except Exception as pe:
            print(f"Payment analytics error: {pe}")
            all_payments, total_revenue, total_counselor_payouts, platform_commission = [], 0.0, 0.0, 0.0
            pending_transfers, failed_transfers, captured_payments_count = 0, 0, 0
        
        # Fetch Moderation Flags (Limited for performance)
        moderation_flags = db.query(models.ModerationFlag).order_by(models.ModerationFlag.timestamp.desc()).limit(50).all()

        return templates.TemplateResponse(request=request, name="admin_dashboard.html", context={
            "user": current_user, 
            "users": all_users,
            "total_users": total_users,
            "user_page": user_page,
            "feedbacks": all_feedback,
            "total_feedback": total_feedback,
            "feedback_page": feedback_page,
            "tickets": all_tickets,
            "total_tickets": total_tickets,
            "ticket_page": ticket_page,
            "page_size": page_size,
            "pending_counsellors": pending_counsellors,
            "all_counsellors": all_counsellors,
            "all_payments": all_payments,
            "total_revenue": total_revenue,
            "total_counselor_payouts": total_counselor_payouts,
            "platform_commission": platform_commission,
            "pending_transfers": pending_transfers,
            "failed_transfers": failed_transfers,
            "captured_payments_count": captured_payments_count,
            "moderation_flags": moderation_flags
        })
    except Exception as e:
        import traceback
        print(f"ADMIN DASHBOARD ERROR: {traceback.format_exc()}")
        return RedirectResponse(url=f"/dashboard?error=Admin+Error:+{str(e)[:100]}", status_code=status.HTTP_302_FOUND)

@app.post("/admin/users/{user_id}/delete")
async def delete_user(user_id: int, request: Request, db: Session = Depends(get_db)):
    # 1. Check admin auth
    current_user = get_current_user(request, db)
    if not current_user or current_user.role != "admin":
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    # 2. Get User
    user_to_delete = db.query(models.User).filter(models.User.id == user_id).first()
    if not user_to_delete:
         raise HTTPException(status_code=404, detail="User not found")

    # 3. Delete Assessment Result first (ForeignKey)
    if user_to_delete.assessment:
        db.delete(user_to_delete.assessment)
    
    # 4. Delete User
    db.delete(user_to_delete)
    db.commit()
    
    return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)

@app.post("/admin/users/{user_id}/suspend")
async def suspend_user(user_id: int, request: Request, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user or current_user.role != "admin":
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user:
        user.is_suspended = True
        db.commit()
        user_cache.invalidate_user(user.id)
    return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)

@app.post("/admin/users/{user_id}/unsuspend")
async def unsuspend_user(user_id: int, request: Request, db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user or current_user.role != "admin":
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user:
        user.is_suspended = False
        db.commit()
        user_cache.invalidate_user(user.id)
    return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)

@app.post("/admin/flags/{flag_id}/action")
async def handle_flag(flag_id: int, request: Request, action: str = Form(...), db: Session = Depends(get_db)):
    current_user = get_current_user(request, db)
    if not current_user or current_user.role != "admin":
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    flag = db.query(models.ModerationFlag).filter(models.ModerationFlag.id == flag_id).first()
    if flag:
        if action == "dismiss":
            flag.status = "dismissed"
        elif action == "resolve":
            flag.status = "action_taken"
        db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)

# --- Counsellor Routes ---

@app.post("/counsellor/accept-tnc")
async def accept_tnc(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user or user.role != "counsellor":
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    
    profile = db.query(models.CounsellorProfile).filter(models.CounsellorProfile.user_id == user.id).first()
    if profile:
        profile.tnc_accepted = True
        profile.tnc_accepted_at = datetime.datetime.utcnow()
        db.commit()
    
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)

@app.post("/counsellor/update")
async def counsellor_update(
    request: Request,
    fee: str = Form(None),
    availability_text: str = Form(None),
    bank_name: str = Form(None),
    account_num: str = Form(None),
    ifsc_code: str = Form(None),
    upi_id: str = Form(None),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if not user or user.role != "counsellor":
         return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    
    try:
        profile = db.query(models.CounsellorProfile).filter(models.CounsellorProfile.user_id == user.id).first()
        if not profile:
            profile = models.CounsellorProfile(user_id=user.id)
            db.add(profile)
            db.flush()
        
        # Update Basic Info (only if provided and not empty)
        if fee is not None and fee.strip() != "" and not profile.fee_locked:
            try:
                profile.fee = float(fee)
            except ValueError:
                pass # Ignore invalid fee format
        
        if availability_text is not None:
            profile.availability = {"text": availability_text}
        
        # Update Account Details (merge with existing)
        # Check if any account-related fields were sent in the form
        if any(x is not None for x in [bank_name, account_num, ifsc_code, upi_id]):
            account_data = profile.account_details or {}
            # We treat empty string as clearing the value or just setting it to empty
            if bank_name is not None: account_data["bank_name"] = bank_name
            if account_num is not None: account_data["account_num"] = account_num
            if ifsc_code is not None: account_data["ifsc"] = ifsc_code
            if upi_id is not None: account_data["upi"] = upi_id
            profile.account_details = account_data
        
        db.commit()
    except Exception as e:
        print(f"Error updating counsellor profile: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Error updating profile. Please ensure all data is correct.")
    
    return RedirectResponse(url="/dashboard?msg=Profile updated successfully", status_code=status.HTTP_302_FOUND)

@app.post("/profile/upload-photo")
async def upload_profile_photo(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    if not file.filename:
        return RedirectResponse(url="/dashboard?error=No file selected", status_code=status.HTTP_302_FOUND)

    file_extension = os.path.splitext(file.filename)[1]
    filename = f"user_{user.id}_{uuid.uuid4().hex}{file_extension}"
    
    # Ensure directory exists
    upload_dir = os.path.join(BASE_DIR, "static", "uploads", "profile_photos")
    try:
        os.makedirs(upload_dir, exist_ok=True)
        file_path = os.path.join(upload_dir, filename)
        
        contents = await file.read()
        with open(file_path, "wb") as buffer:
            buffer.write(contents)
        
        user.profile_photo = f"/static/uploads/profile_photos/{filename}"
        db.commit()
    except Exception as e:
        print(f"UPLOAD ERROR: {e}")
        return RedirectResponse(url="/dashboard?error=Upload failed", status_code=status.HTTP_302_FOUND)
    
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)

@app.post("/counsellor/upload-certificates")
async def upload_certificates(
    request: Request,
    experience: str = Form(None),
    files: List[UploadFile] = File(None),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if not user or user.role != "counsellor":
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    try:
        profile = db.query(models.CounsellorProfile).filter(models.CounsellorProfile.user_id == user.id).first()
        if not profile:
            profile = models.CounsellorProfile(user_id=user.id)
            db.add(profile)
            db.flush()
        
        existing_certs = profile.certificates if profile.certificates else []
        new_certs = list(existing_certs)
        
        # Ensure directory exists
        upload_dir = os.path.join(BASE_DIR, "static", "uploads", "certificates")
        os.makedirs(upload_dir, exist_ok=True)

        if files:
            for file in files:
                if file.filename and file.filename.strip() != "":
                    file_extension = os.path.splitext(file.filename)[1]
                    filename = f"cert_{user.id}_{uuid.uuid4().hex}{file_extension}"
                    file_path = os.path.join(upload_dir, filename)
                    
                    contents = await file.read()
                    with open(file_path, "wb") as buffer:
                        buffer.write(contents)
                    
                    new_certs.append(f"/static/uploads/certificates/{filename}")
        
        profile.certificates = new_certs
        if experience:
            profile.experience = experience
        profile.verification_status = "pending"
        db.commit()
    except Exception as e:
        print(f"CERTIFICATE UPLOAD ERROR: {e}")
        db.rollback()
        return RedirectResponse(url="/dashboard?error=Upload failed. Please try again.", status_code=status.HTTP_302_FOUND)
    
    return RedirectResponse(url="/dashboard?msg=Certificates uploaded successfully", status_code=status.HTTP_302_FOUND)

@app.post("/admin/verify-counsellor/{counsellor_id}")
async def verify_counsellor(
    counsellor_id: int,
    request: Request,
    verification_status: str = Form(...), # "approved" or "rejected"
    db: Session = Depends(get_db)
):
    current_user = get_current_user(request, db)
    if not current_user or current_user.role != "admin":
         # Safety Check: Allow access if user email matches ADMIN_EMAIL env var
        admin_email = os.getenv("ADMIN_EMAIL")
        if not admin_email or current_user.email != admin_email:
            return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
            
    try:
        profile = db.query(models.CounsellorProfile).filter(models.CounsellorProfile.user_id == counsellor_id).first()
        if profile:
            profile.verification_status = verification_status
            profile.is_verified = (verification_status == "approved")
            
            # Send Notification to Counsellor
            msg = "Congratulations! Your profile has been verified successfully. You can now start accepting booking requests." if verification_status == "approved" else "Your profile verification was not approved. Please ensure all your documents are correct and try again."
            
            notif = models.Notification(
                user_id=counsellor_id,
                type="system",
                message=msg
            )
            db.add(notif)
            db.commit()
    except Exception as e:
        print(f"Admin Verification Error: {e}")
        db.rollback()
    
    return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)

@app.post("/admin/block-counsellor/{counsellor_id}")
async def block_counsellor(
    counsellor_id: int,
    request: Request,
    block_reason: str = Form(...),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(request, db)
    if not current_user or current_user.role != "admin":
        admin_email = os.getenv("ADMIN_EMAIL")
        if not admin_email or current_user.email != admin_email:
            return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    
    profile = db.query(models.CounsellorProfile).filter(models.CounsellorProfile.user_id == counsellor_id).first()
    if profile:
        profile.is_blocked = True
        profile.block_reason = block_reason
        profile.is_verified = False  # Remove from public listing
        db.commit()
    
    return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)

@app.post("/admin/unblock-counsellor/{counsellor_id}")
async def unblock_counsellor(
    counsellor_id: int,
    request: Request,
    db: Session = Depends(get_db)
):
    current_user = get_current_user(request, db)
    if not current_user or current_user.role != "admin":
        admin_email = os.getenv("ADMIN_EMAIL")
        if not admin_email or current_user.email != admin_email:
            return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    
    profile = db.query(models.CounsellorProfile).filter(models.CounsellorProfile.user_id == counsellor_id).first()
    if profile:
        profile.is_blocked = False
        profile.block_reason = None
        db.commit()
    
    return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)

@app.post("/admin/give-founding-badge/{counsellor_id}")
async def give_founding_badge(
    counsellor_id: int,
    request: Request,
    db: Session = Depends(get_db)
):
    current_user = get_current_user(request, db)
    if not current_user or current_user.role != "admin":
        admin_email = os.getenv("ADMIN_EMAIL")
        if not admin_email or current_user.email != admin_email:
            return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    
    # Check if limit of 100 is reached
    founding_count = db.query(models.CounsellorProfile).filter(models.CounsellorProfile.is_founding_counsellor == True).count()
    if founding_count >= 100:
        return RedirectResponse(url="/admin?error=Founding+counsellor+limit+reached", status_code=status.HTTP_302_FOUND)

    profile = db.query(models.CounsellorProfile).filter(models.CounsellorProfile.user_id == counsellor_id).first()
    if profile:
        profile.is_founding_counsellor = True
        profile.founding_badge_awarded_at = datetime.datetime.now()
        # 2 months free commission
        profile.commission_free_until = datetime.datetime.now() + datetime.timedelta(days=60)
        
        # Add Notification for the counsellor
        notification = models.Notification(
            user_id=counsellor_id,
            type="founding_badge_awarded",
            message="Congratulations! You have been awarded the Founding Counsellor badge with 2 months of 0% commission! 🏆"
        )
        db.add(notification)
        db.commit()
    
    return RedirectResponse(url="/admin?message=Founding+badge+awarded", status_code=status.HTTP_302_FOUND)

@app.post("/admin/take-founding-badge/{counsellor_id}")
async def take_founding_badge(
    counsellor_id: int,
    request: Request,
    db: Session = Depends(get_db)
):
    current_user = get_current_user(request, db)
    if not current_user or current_user.role != "admin":
        admin_email = os.getenv("ADMIN_EMAIL")
        if not admin_email or current_user.email != admin_email:
            return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    
    profile = db.query(models.CounsellorProfile).filter(models.CounsellorProfile.user_id == counsellor_id).first()
    if profile:
        profile.is_founding_counsellor = False
        profile.commission_free_until = None

        # Add Notification for the counsellor
        notification = models.Notification(
            user_id=counsellor_id,
            type="founding_badge_removed",
            message="Your Founding Counsellor badge has been removed by admin."
        )
        db.add(notification)
        db.commit()
    
    return RedirectResponse(url="/admin?message=Founding+badge+removed", status_code=status.HTTP_302_FOUND)

@app.post("/admin/update-counsellor-fee/{counsellor_id}")
async def admin_update_counsellor_fee(
    counsellor_id: int,
    request: Request,
    new_fee: float = Form(...),
    db: Session = Depends(get_db)
):
    current_user = get_current_user(request, db)
    if not current_user or current_user.role != "admin":
        admin_email = os.getenv("ADMIN_EMAIL")
        if not admin_email or current_user.email != admin_email:
            return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)

    profile = db.query(models.CounsellorProfile).filter(models.CounsellorProfile.user_id == counsellor_id).first()
    if profile:
        old_fee = profile.fee or 0.0
        profile.fee = new_fee
        profile.fee_locked = True  # Lock fee so counsellor cannot change it
        db.commit()

        # Notify counsellor about the fee change
        if old_fee != new_fee:
            if new_fee < old_fee:
                message = f"Your session fee has been reduced by admin from ₹{old_fee:.0f} to ₹{new_fee:.0f}. If you have questions, please raise a support ticket."
            elif new_fee > old_fee:
                message = f"Your session fee has been increased by admin from ₹{old_fee:.0f} to ₹{new_fee:.0f}."
            else:
                message = f"Your session fee has been updated by admin to ₹{new_fee:.0f}."

            notification = models.Notification(
                user_id=counsellor_id,
                type="fee_change",
                message=message,
            )
            db.add(notification)
            db.commit()

    return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)

@app.post("/notifications/{notif_id}/dismiss")
async def dismiss_notification(
    notif_id: int,
    request: Request,
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    notif = db.query(models.Notification).filter(
        models.Notification.id == notif_id,
        models.Notification.user_id == user.id
    ).first()
    if notif:
        notif.is_read = True
        db.commit()

    return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)

@app.get("/counsellors", response_class=HTMLResponse)
async def list_counsellors(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
        
    # Only show verified AND non-blocked counsellors to students
    counsellors = db.query(models.CounsellorProfile).filter(
        models.CounsellorProfile.is_verified == True,
        models.CounsellorProfile.is_blocked == False
    ).all()
    
    return templates.TemplateResponse(request=request, name="counsellors_list.html", context={"user": user, "counsellors": counsellors})

import datetime
import uuid

@app.post("/create_razorpay_order/{counsellor_id}")
async def create_razorpay_order(counsellor_id: int, request: Request, fee: float = Form(...), db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if fee < 1.0:
         raise HTTPException(status_code=400, detail="Minimum fee for Razorpay is ₹1.00. Please update the counsellor profile fee.")
         
    # Create Razorpay Order
    data = {
        "amount": int(fee * 100), # amount in paise
        "currency": "INR",
        "receipt": f"receipt_{uuid.uuid4().hex[:10]}",
        "payment_capture": 0 # AUTHORIZATION ONLY (Wait for counsellor approval)
    }
    
    try:
        order = razorpay_client.order.create(data=data)

        # ── Record Payment in DB for admin split tracking ──────────────────
        try:
            payment_record = models.Payment(
                razorpay_order_id=order["id"],
                amount=fee,
                status="created"
            )
            db.add(payment_record)
            db.commit()
            db.refresh(payment_record)
            print(f"DEBUG: Payment record created: order={order['id']}, amount=₹{fee}")
        except Exception as pe:
            print(f"DEBUG: Payment record creation skipped: {pe}")

        return order
    except Exception as e:
        print(f"Razorpay Error: {e}")
        raise HTTPException(status_code=500, detail="Could not create payment order")

@app.post("/book_free_counsellor/{counsellor_id}")
async def book_free_counsellor(counsellor_id: int, request: Request, background_tasks: BackgroundTasks, appointment_time: str = Form(...), db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
         return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
         
    # Verify counsellor is free
    counsellor_profile = db.query(models.CounsellorProfile).filter(models.CounsellorProfile.user_id == counsellor_id).first()
    if not counsellor_profile or (counsellor_profile.fee > 0):
        # Allow a small margin for float comparison
        if counsellor_profile and counsellor_profile.fee > 0.01:
            raise HTTPException(status_code=400, detail="This counsellor is not free. Please use the payment booking flow.")
        
    # Meeting link generation (Jitsi Meet for instant, working rooms)
    meeting_id = str(uuid.uuid4())[:12]
    meeting_link = f"https://meet.jit.si/CareStance_{meeting_id}"
    
    try:
        appt_time = datetime.datetime.fromisoformat(appointment_time)
    except ValueError:
        # Fallback if the format is slightly different
        appt_time = datetime.datetime.now() + datetime.timedelta(days=1)
    
    appointment = models.Appointment(
        student_id=user.id,
        counsellor_id=counsellor_id,
        appointment_time=appt_time,
        status="scheduled",
        payment_status="free",
        meeting_link=meeting_link
    )
    db.add(appointment)
    db.commit()
    db.refresh(appointment)
    
    # Send Emails
    student_email = user.email
    counsellor_user = db.query(models.User).filter(models.User.id == counsellor_id).first()
    if counsellor_user:
        appt_time_str = appt_time.strftime('%b %d, %I:%M %p')
        # To Student
        background_tasks.add_task(
            send_email, 
            student_email, 
            "CareStance Session Confirmed! 🚀", 
            get_booking_template(user.full_name, counsellor_user.full_name, appt_time_str, meeting_link, "student")
        )
        # To Counsellor
        background_tasks.add_task(
            send_email, 
            counsellor_user.email, 
            "New Coaching Session Booked! 📆", 
            get_booking_template(counsellor_user.full_name, user.full_name, appt_time_str, meeting_link, "counsellor")
        )

    print(f"DEBUG: Free Appointment created successfully for student {user.id} and counsellor {counsellor_id}")
    
    return templates.TemplateResponse(request=request, name="appointment_success.html", context={"user": user, "appointment": appointment})

@app.get("/join_meeting/{appointment_id}")
async def join_meeting(appointment_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    appointment = db.query(models.Appointment).filter(models.Appointment.id == appointment_id).first()
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found")
    
    return RedirectResponse(url=f"/meeting/{appointment_id}")

@app.get("/meeting/{appointment_id}", response_class=HTMLResponse)
async def meeting_page(appointment_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    appointment = db.query(models.Appointment).filter(models.Appointment.id == appointment_id).first()
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found")
    
    other_user_id = appointment.student_id if user.id == appointment.counsellor_id else appointment.counsellor_id
    other_user = db.query(models.User).filter(models.User.id == other_user_id).first()
    
    return templates.TemplateResponse(request=request, name="meeting.html", context={
        "user": user, 
        "appointment": appointment,
        "other_user": other_user
    })

@app.post("/appointment/track-join/{appointment_id}")
async def track_join(appointment_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401)
    
    appointment = db.query(models.Appointment).filter(models.Appointment.id == appointment_id).first()
    if not appointment:
        raise HTTPException(status_code=404)
    
    now = datetime.datetime.now()
    if user.id == appointment.counsellor_id:
        appointment.counsellor_joined = True
        appointment.joined_at = now
    elif user.id == appointment.student_id:
        appointment.student_joined = True
        appointment.student_joined_at = now
    
    db.commit()
    return {"status": "ok"}

@app.post("/appointment/heartbeat/{appointment_id}")
async def appointment_heartbeat(appointment_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401)
    
    appointment = db.query(models.Appointment).filter(models.Appointment.id == appointment_id).first()
    if not appointment:
        raise HTTPException(status_code=404)
    
    if appointment.status == "scheduled":
        # Increment overlap counter (called every minute by frontend if both present)
        appointment.actual_overlap_minutes += 1
        
        # Mark as completed if overlap >= 5 minutes
        if appointment.actual_overlap_minutes >= 5:
            appointment.status = "completed"
            
        db.commit()
        
    return {"status": appointment.status, "overlap_mins": appointment.actual_overlap_minutes}

@app.get("/appointment_status/{appointment_id}")
async def appointment_status(appointment_id: int, db: Session = Depends(get_db)):
    appointment = db.query(models.Appointment).filter(models.Appointment.id == appointment_id).first()
    if not appointment:
        return {"error": "Not found"}, 404
    return {
        "counsellor_joined": appointment.counsellor_joined,
        "joined_at": appointment.joined_at.isoformat() if appointment.joined_at else None
    }

@app.api_route("/appointment/delete/{appointment_id}", methods=["GET", "POST"])
async def delete_appointment(appointment_id: int, request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    appointment = db.query(models.Appointment).filter(models.Appointment.id == appointment_id).first()
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found")
    
    # Check if user is either the student or the counsellor for this appointment
    if user.id != appointment.student_id and user.id != appointment.counsellor_id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this appointment")
    
    # Send Cancellation Emails
    student = db.query(models.User).filter(models.User.id == appointment.student_id).first()
    counsellor = db.query(models.User).filter(models.User.id == appointment.counsellor_id).first()
    appt_time_str = appointment.appointment_time.strftime('%b %d, %I:%M %p') if appointment.appointment_time else "To Be Decided"
    
    if student and counsellor:
        # To Student
        background_tasks.add_task(
            send_email,
            student.email,
            "CareStance Session Cancelled ⚠️",
            get_cancellation_template(student.full_name, counsellor.full_name, appt_time_str, "student")
        )
        # To Counsellor
        background_tasks.add_task(
            send_email,
            counsellor.email,
            "Session Cancellation Alert ⚠️",
            get_cancellation_template(counsellor.full_name, student.full_name, appt_time_str, "counsellor")
        )

    db.delete(appointment)
    db.commit()
    
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)

@app.post("/appointment/complete/{appointment_id}")
async def complete_appointment(appointment_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    # Only counsellors can mark sessions as complete
    if not user or user.role != "counsellor":
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    appointment = db.query(models.Appointment).filter(
        models.Appointment.id == appointment_id, 
        models.Appointment.counsellor_id == user.id
    ).first()
    
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found or not yours")
    
    appointment.status = "completed"
    db.commit()
    
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)

@app.post("/appointment/rate/{appointment_id}")
async def rate_appointment(appointment_id: int, request: Request, rating: int = Form(...), review: str = Form(None), db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    # Verify appointment student
    appointment = db.query(models.Appointment).filter(
        models.Appointment.id == appointment_id, 
        models.Appointment.student_id == user.id
    ).first()
    
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found or not yours")
    
    if appointment.status != "completed":
        raise HTTPException(status_code=400, detail="Only completed appointments can be rated")
    
    # Check for existing rating
    existing = db.query(models.CounselorRating).filter(models.CounselorRating.appointment_id == appointment_id).first()
    if existing:
        return RedirectResponse(url="/dashboard?error=Session already rated", status_code=status.HTTP_302_FOUND)
    
    try:
        # Create rating record
        new_rating = models.CounselorRating(
            appointment_id=appointment_id,
            counsellor_id=appointment.counsellor_id,
            student_id=user.id,
            rating=rating,
            review=review
        )
        db.add(new_rating)
        
        # Update Counselor profile stats
        profile = db.query(models.CounsellorProfile).filter(models.CounsellorProfile.user_id == appointment.counsellor_id).first()
        if profile:
            # Handle potential None values safely
            avg = profile.average_rating if profile.average_rating is not None else 5.0
            count = profile.rating_count if profile.rating_count is not None else 0
            
            old_total = avg * count
            profile.rating_count = count + 1
            profile.average_rating = (old_total + rating) / profile.rating_count
        
        db.commit()
    except Exception as e:
        print(f"Rating error: {e}")
        db.rollback()
        return RedirectResponse(url="/dashboard?error=Failed to submit rating. Please try again.", status_code=status.HTTP_302_FOUND)
        
    return RedirectResponse(url="/dashboard?msg=Thank you for your feedback!", status_code=status.HTTP_302_FOUND)

@app.post("/admin/tickets/{ticket_id}/reply")
async def reply_ticket(ticket_id: int, request: Request, reply_content: str = Form(None), db: Session = Depends(get_db)):
    if reply_content is None:
        return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)
    user = get_current_user(request, db)
    admin_email = os.getenv("ADMIN_EMAIL")
    if not user or (user.role != "admin" and user.email != admin_email):
        raise HTTPException(status_code=403, detail="Not authorized")
    
    ticket = db.query(models.Ticket).filter(models.Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    
    ticket.admin_reply = reply_content
    db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)

@app.post("/admin/tickets/{ticket_id}/close")
async def close_ticket(ticket_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    admin_email = os.getenv("ADMIN_EMAIL")
    if not user or (user.role != "admin" and user.email != admin_email):
        raise HTTPException(status_code=403, detail="Not authorized")
    
    ticket = db.query(models.Ticket).filter(models.Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    
    ticket.status = "Closed"
    db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)

@app.post("/admin/tickets/{ticket_id}/delete")
async def delete_ticket(ticket_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    admin_email = os.getenv("ADMIN_EMAIL")
    if not user or (user.role != "admin" and user.email != admin_email):
        raise HTTPException(status_code=403, detail="Not authorized")
    
    ticket = db.query(models.Ticket).filter(models.Ticket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    
    db.delete(ticket)
    db.commit()
    return RedirectResponse(url="/admin", status_code=status.HTTP_302_FOUND)

@app.post("/verify_payment")
async def verify_payment(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
         return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
         
    form_data = await request.form()
    razorpay_payment_id = form_data.get("razorpay_payment_id")
    razorpay_order_id = form_data.get("razorpay_order_id")
    razorpay_signature = form_data.get("razorpay_signature")
    counsellor_id = int(form_data.get("counsellor_id"))
    appointment_time_str = form_data.get("appointment_time") # Should be passed from frontend
    
    # Verify Signature
    params_dict = {
        'razorpay_order_id': razorpay_order_id,
        'razorpay_payment_id': razorpay_payment_id,
        'razorpay_signature': razorpay_signature
    }
    
    try:
        razorpay_client.utility.verify_payment_signature(params_dict)
    except Exception as e:
        print(f"Payment verification failed: {e}")
        return RedirectResponse(url="/counsellors?error=Payment verification failed", status_code=status.HTTP_302_FOUND)
        
    # Payment Successful, create appointment
    meeting_id = str(uuid.uuid4())[:12]
    meeting_link = f"https://meet.jit.si/CareStance_{meeting_id}"
    
    # Parse appointment time or use a default
    if appointment_time_str:
        appt_time = datetime.datetime.fromisoformat(appointment_time_str)
    else:
        appt_time = datetime.datetime.now() + datetime.timedelta(days=1)
        
    # ── Record Payment + Transfer and Appointment in one transaction ─────────
    try:
        # 1. Create Appointment
        appointment = models.Appointment(
            student_id=user.id,
            counsellor_id=counsellor_id,
            appointment_time=appt_time,
            status="requested", 
            payment_status="authorized", 
            meeting_link=meeting_link,
            razorpay_order_id=razorpay_order_id,
            razorpay_payment_id=razorpay_payment_id
        )
        db.add(appointment)
        db.flush() # Get appointment.id

        # 2. Update/Create Payment record
        counsellor_profile = db.query(models.CounsellorProfile).filter(
            models.CounsellorProfile.user_id == counsellor_id
        ).first()
        fee_amount = counsellor_profile.fee if counsellor_profile else 0.0

        payment_record = db.query(models.Payment).filter(
            models.Payment.razorpay_order_id == razorpay_order_id
        ).first()

        if payment_record:
            payment_record.razorpay_payment_id = razorpay_payment_id
            payment_record.status = "captured"
            payment_record.session_id = appointment.id
        else:
            payment_record = models.Payment(
                session_id=appointment.id,
                razorpay_order_id=razorpay_order_id,
                razorpay_payment_id=razorpay_payment_id,
                amount=fee_amount,
                status="captured"
            )
            db.add(payment_record)
        
        db.flush() # Get payment_record.id

        # 3. Create Transfer record (Commission logic)
        is_founding_free = False
        if counsellor_profile and counsellor_profile.is_founding_counsellor:
            if counsellor_profile.commission_free_until and counsellor_profile.commission_free_until > datetime.datetime.now():
                is_founding_free = True

        if is_founding_free:
            counselor_share = round(fee_amount, 2)
        else:
            counselor_share = round(fee_amount * 0.70, 2)

        transfer_record = models.Transfer(
            payment_id=payment_record.id,
            counsellor_id=counsellor_id,
            amount=counselor_share,
            status="pending"
        )
        db.add(transfer_record)

        # 4. Create Notification
        notif = models.Notification(
            user_id=counsellor_id,
            type="booking_request",
            message=f"New booking request from {user.full_name} for {appt_time.strftime('%b %d, %I:%M %p')}."
        )
        db.add(notif)

        # 5. Final Commit
        db.commit()
    except Exception as e:
        print(f"Payment verification DB error: {e}")
        db.rollback()
        return RedirectResponse(url="/counsellors?error=Database error during payment processing", status_code=status.HTTP_302_FOUND)

    return templates.TemplateResponse(request=request, name="appointment_success.html", context={"user": user, "appointment": appointment, "is_request": True})

@app.post("/appointment/accept/{appt_id}")
async def accept_appointment(appt_id: int, request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user or user.role != "counsellor":
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    appt = db.query(models.Appointment).filter(models.Appointment.id == appt_id, models.Appointment.counsellor_id == user.id).first()
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found")
    
    # 1. Capture Payment if it was authorized
    if appt.payment_status == "authorized" and appt.razorpay_payment_id:
        try:
            # Get payment details to find amount
            payment_info = razorpay_client.payment.fetch(appt.razorpay_payment_id)
            amount = payment_info['amount']
            razorpay_client.payment.capture(appt.razorpay_payment_id, amount)
            appt.payment_status = "paid"
        except Exception as e:
            print(f"Razorpay Capture Error: {e}")
            # Even if capture fails, we might want to know why. 
            # If it's already captured, just proceed.
            if "already captured" in str(e).lower():
                appt.payment_status = "paid"
            else:
                return RedirectResponse(url="/dashboard?error=Payment+capture+failed", status_code=status.HTTP_302_FOUND)

    # 2. Update Status
    appt.status = "scheduled"
    db.commit()

    # 3. Notify Student
    student = db.query(models.User).filter(models.User.id == appt.student_id).first()
    if student:
        appt_time_str = appt.appointment_time.strftime('%b %d, %I:%M %p')
        background_tasks.add_task(
            send_email,
            student.email,
            "Booking Accepted! 🚀",
            f"Hi {student.full_name}, your session with {user.full_name} on {appt_time_str} has been accepted and confirmed."
        )
        # Add internal notification
        notif = models.Notification(
            user_id=student.id,
            type="booking_accepted",
            message=f"Counsellor {user.full_name} accepted your session for {appt_time_str}."
        )
        db.add(notif)
        db.commit()

    return RedirectResponse(url="/dashboard?message=Appointment+accepted", status_code=status.HTTP_302_FOUND)

@app.post("/appointment/reject/{appt_id}")
async def reject_appointment(appt_id: int, request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user or user.role != "counsellor":
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    appt = db.query(models.Appointment).filter(models.Appointment.id == appt_id, models.Appointment.counsellor_id == user.id).first()
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found")
    
    # 1. Release Payment (Refund if authorized)
    # Note: If only authorized, Razorpay doesn't have a direct "void" in the client lib sometimes, 
    # but not capturing it is the primary way. However, refunding an authorized payment voids it.
    if appt.payment_status == "authorized" and appt.razorpay_payment_id:
        try:
            # Refunding an authorized payment effectively releases the hold
            razorpay_client.payment.refund(appt.razorpay_payment_id, {})
            appt.payment_status = "refunded/released"
        except Exception as e:
            print(f"Razorpay Release Error: {e}")

    # 2. Update Status
    appt.status = "rejected"
    db.commit()

    # 3. Notify Student
    student = db.query(models.User).filter(models.User.id == appt.student_id).first()
    if student:
        appt_time_str = appt.appointment_time.strftime('%b %d, %I:%M %p')
        background_tasks.add_task(
            send_email,
            student.email,
            "Booking Notification ⚠️",
            f"Hi {student.full_name}, unfortunately, {user.full_name} is unable to take the session on {appt_time_str}. Your payment hold has been released."
        )
        notif = models.Notification(
            user_id=student.id,
            type="booking_rejected",
            message=f"Counsellor {user.full_name} rejected your session for {appt_time_str}. Hold released."
        )
        db.add(notif)
        db.commit()

    return RedirectResponse(url="/dashboard?message=Appointment+rejected", status_code=status.HTTP_302_FOUND)

@app.post("/career/roadmap/delete/{path_id}")
async def delete_roadmap(path_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    path = db.query(models.CareerPath).filter(models.CareerPath.id == path_id, models.CareerPath.user_id == user.id).first()
    if not path:
        raise HTTPException(status_code=404, detail="Roadmap not found")
    
    db.delete(path)
    db.commit()
    return RedirectResponse(url="/career/roadmaps?message=Roadmap+deleted", status_code=status.HTTP_302_FOUND)

    if counsellor_user:
        appt_time_str = appt_time.strftime('%b %d, %I:%M %p')
        # To Student
        background_tasks.add_task(
            send_email, 
            student_email, 
            "CareStance Session Confirmed! 🚀", 
            get_booking_template(user.full_name, counsellor_user.full_name, appt_time_str, meeting_link, "student")
        )
        # To Counsellor
        background_tasks.add_task(
            send_email, 
            counsellor_user.email, 
            "Session Paid & Confirmed! 💰", 
            get_booking_template(counsellor_user.full_name, user.full_name, appt_time_str, meeting_link, "counsellor")
        )

    print(f"DEBUG: Appointment created successfully for student {user.id} and counsellor {counsellor_id}")
    
    return templates.TemplateResponse(request=request, name="appointment_success.html", context={"user": user, "appointment": appointment})

# --- Phase 3 Routes ---

from data.questions_phase3 import CATEGORY_SCENARIOS_MAP

@app.get("/assessment/phase3", response_class=HTMLResponse)
async def assessment_phase3(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    # Get user's Phase 2 result
    result = db.query(models.AssessmentResult).filter(models.AssessmentResult.user_id == user.id).first()
    if not result or not result.phase_2_category:
        # No category to deep dive into
        return RedirectResponse(url="/assessment/result", status_code=status.HTTP_302_FOUND)

    category = result.phase_2_category
    scenarios = CATEGORY_SCENARIOS_MAP.get(category)
    
    if not scenarios:
        # Fallback if category not found or has no scenarios yet
        # For now, maybe just show Focused Specialist as default or error
        # Be safe and redirect w/ maybe a flash message (not impl yet)
        return RedirectResponse(url="/assessment/result", status_code=status.HTTP_302_FOUND)
    
    return templates.TemplateResponse(request=request, name="assessment_phase3.html", context={
        "user": user, 
        "scenarios": scenarios,
        "category_name": category
    })

@app.post("/assessment/phase3/submit")
async def assessment_phase3_submit(
    request: Request, 
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    try:
        form_data = await request.form()
    except Exception:
        # Client disconnected or bad request
        return RedirectResponse(url="/assessment/phase3", status_code=status.HTTP_302_FOUND)
    answers = {}
    
    for key, value in form_data.items():
        answers[key] = value

    # Save to DB
    result = db.query(models.AssessmentResult).filter(models.AssessmentResult.user_id == user.id).first()
    if not result:
        # Should likely have a phase 2 result first, but if not, create new?
        # For simplicity, we assume result exists. If not, redirect to start.
        return RedirectResponse(url="/assessment", status_code=status.HTTP_302_FOUND)
    
    result.phase3_answers = answers
    result.phase3_result = "Phase 3 Completed" # Placeholder
    
    # Generate Phase 3 Analysis using Gemini
    category = result.phase_2_category
    # Inside assessment_phase3_submit route
    prompt_p3 = f"""
    You are an expert Career Mentor. Deep-dive into the scenario responses for a '{category}' profile.

    SCENARIO RESPONSES:
    {json.dumps(answers, indent=2)}

    TASK:
    Write a personal, narrative analysis (no bullet points).
    1. Acknowledge their specific choice in the most challenging scenario.
    2. Explain what this reveals about their 'Internal Compass' and leadership style.
    3. Use warm, sophisticated language that builds the student's confidence.

    STRICT RULE: No quotes, no markdown headers, max 4 sentences. 
    Start directly with: "It is fascinating to observe how you navigate..."
    """
    
    try:
         raw_text = await generate_content_with_fallback(prompt_p3)
         result.phase3_analysis = raw_text.replace('"', '').replace("'", "")
    except Exception as e:
         result.phase3_analysis = f"Analysis unavailable at this time. ({str(e)})"
        
    db.commit()

    return RedirectResponse(url="/assessment/result", status_code=status.HTTP_302_FOUND)


# --- Phase 3 AI Chat Endpoint ---

class Phase3ChatRequest(BaseModel):
    message: str
    current_index: int = 0
    answers: dict = {}

COUNSELLOR_SYSTEM_PROMPT = """
You are an empathetic, professional career counsellor conducting a personality assessment.
Your role is to present scenario questions warmly and professionally.
- Keep your response concise (3-5 sentences max).
- When presenting a scenario, clearly state the two options (Option A and Option B) on separate lines.
- After the user selects an option, briefly acknowledge their choice with an encouraging sentence, then say you're moving to the next scenario.
- Do NOT make up new questions. Only work with the scenario data you are given.
"""

@app.post("/assessment/phase3/chat")
async def phase3_chat(request: Request, chat_req: Phase3ChatRequest, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    result = db.query(models.AssessmentResult).filter(models.AssessmentResult.user_id == user.id).first()
    if not result or not result.phase_2_category:
        return jsonify({"error": "No phase 2 category found"})
    
    from fastapi.responses import JSONResponse
    category = result.phase_2_category
    question_list = CATEGORY_SCENARIOS_MAP.get(category, [])
    total = len(question_list)
    current_idx = chat_req.current_index

    # All done
    if current_idx >= total:
        return JSONResponse({
            "response": "Thank you for completing all the scenarios! Click **Finish Assessment** below to generate your personalised profile.",
            "current_index": current_idx,
            "answers": chat_req.answers,
            "done": True
        })

    current_scenario = question_list[current_idx]

    # Build the prompt
    scenario_text = f"""
Scenario {current_idx + 1} of {total}:
Title: {current_scenario['title']}
Story: {current_scenario['story']}
Option A: {current_scenario['options'][0]['text']}
Option B: {current_scenario['options'][1]['text']}
"""

    if chat_req.message and chat_req.message.strip():
        # User replied to previous question — acknowledge and show next
        next_idx = current_idx + 1
        if next_idx >= total:
            prompt = f"""{COUNSELLOR_SYSTEM_PROMPT}

The user just answered the previous scenario. Their reply was: "{chat_req.message}"
Acknowledge their answer warmly in 1-2 sentences, then let them know they have completed all scenarios and should click Finish."""
            new_idx = next_idx
            done = True
        else:
            next_scenario = question_list[next_idx]
            next_scenario_text = f"""
Scenario {next_idx + 1} of {total}:
Title: {next_scenario['title']}
Story: {next_scenario['story']}
Option A: {next_scenario['options'][0]['text']}
Option B: {next_scenario['options'][1]['text']}
"""
            prompt = f"""{COUNSELLOR_SYSTEM_PROMPT}

The user just answered the previous scenario. Their reply was: "{chat_req.message}"
Acknowledge their answer warmly in 1 sentence, then present the next scenario below.

{next_scenario_text}

Present the scenario story first, then clearly list Option A and Option B on separate lines."""
            new_idx = next_idx
            done = False
    else:
        # Initial load — present first scenario
        prompt = f"""{COUNSELLOR_SYSTEM_PROMPT}

Welcome the user warmly (1 sentence), then present this scenario:

{scenario_text}

Present the scenario story first, then clearly list Option A and Option B on separate lines."""
        new_idx = current_idx
        done = False

    # Call Gemini (with Groq fallback)
    try:
        if GEMINI_API_KEY:
            try:
                model_ai = genai.GenerativeModel("gemini-2.0-flash")
                response = await model_ai.generate_content_async(prompt)
                ai_text = response.text
            except Exception:
                model_ai = genai.GenerativeModel("gemini-1.5-flash")
                response = await model_ai.generate_content_async(prompt)
                ai_text = response.text
        elif groq_client:
            completion = await groq_client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model="llama-3.3-70b-versatile",
            )
            ai_text = completion.choices[0].message.content
        else:
            ai_text = f"[Demo Mode] {scenario_text}"
    except Exception as e:
        ai_text = f"I'm having a moment of reflection. ({str(e)}) Please try again."

    return JSONResponse({
        "response": ai_text,
        "current_index": new_idx,
        "answers": chat_req.answers,
        "done": done
    })


# --- Phase 4 Routes (Final Stream Assessment) ---

from data.questions_final import all_questions, section_a_questions, section_b_questions, section_c_questions, section_d_questions
from data.questions_12th import questions_12th
from data.questions_above_12th import questions_above_12th

@app.get("/assessment/final", response_class=HTMLResponse)
async def assessment_final(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    # Get user class selection
    result = db.query(models.AssessmentResult).filter(models.AssessmentResult.user_id == user.id).first()
    selected_class = result.selected_class if result else "10th" # Default to 10th if not found

    context = {
        "request": request, 
        "user": user,
    }

    if selected_class == "12th":
        context["mode"] = "12th"
        context["questions"] = questions_12th
    elif selected_class == "Above 12th": # Ensure this matches the exact string saved in Phase 1
        context["mode"] = "above"
        context["questions"] = questions_above_12th
    else:
        # Default to Class 10th (Existing Logic)
        context["mode"] = "10th"
        context["sections"] = all_questions

    return templates.TemplateResponse(request=request, name="assessment_final.html", context=context)

@app.post("/assessment/final/submit")
async def assessment_final_submit(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    form_data = await request.form()
    answers = {}
    mode = form_data.get("mode", "10th") # Default to 10th if missing
    print(f"DEBUG: Submitting Final Assessment. Mode: {mode}")

    for key, value in form_data.items():
        if key != "mode":
            answers[key] = value
        
    # fetch result object
    result = db.query(models.AssessmentResult).filter(models.AssessmentResult.user_id == user.id).first()
    
    # --- Logic Branching based on Mode ---
    
    if mode == "10th":
        # ... EXISTING LOGIC FOR CLASS 10 (PCM/PCB/COMM/ARTS/VOC) ...
        # (Keeping the original rule-based scoring for Class 10 reliability)
        scores = { "PCM": 0, "PCB": 0, "COMM": 0, "ARTS": 0, "VOC": 0 }
        
        def add_points(streams, points=1):
            for s in streams:
                if s in scores: scores[s] += points

        # 1. Section A
        for q in section_a_questions:
            if answers.get(q["id"]) == q["correct_value"]:
                add_points(q["mapped_streams"], points=2)

        # 2. Preference Sections
        preference_questions = section_b_questions + section_c_questions + section_d_questions
        for q in preference_questions:
            user_ans = answers.get(q["id"])
            if not user_ans: continue
            selected_opt = next((opt for opt in q["options"] if opt["value"] == user_ans), None)
            if selected_opt and "stream" in selected_opt:
                 add_points([selected_opt["stream"]], points=1)
            else:
                if user_ans == "a":
                    txt = q["question"] + " " + (selected_opt["text"] if selected_opt else "")
                    if any(x in txt.lower() for x in ["plant", "health", "bio", "nutri", "species", "cures"]):
                         add_points(["PCB"], points=1)
                    else:
                         add_points(["PCM"], points=1)
                elif user_ans == "b": add_points(["COMM"], points=1)
                elif user_ans == "c": add_points(["ARTS"], points=1)
                elif user_ans == "d": add_points(["VOC"], points=1)

        # 3. Phase 2 Influence
        if result and result.phase_2_category:
            cat = result.phase_2_category
            if cat == "Focused Specialist": add_points(["PCM", "PCB"], points=3)
            elif cat == "Quiet Explorer": add_points(["PCB", "ARTS"], points=3)
            elif cat == "Visionary Leader": add_points(["COMM", "ARTS"], points=3)
            elif cat == "Strategic Builder": add_points(["PCM", "COMM"], points=3)
            elif cat == "Adaptive Explorer": add_points(["ARTS", "VOC"], points=3)
            elif cat == "Dynamic Generalist": add_points(["COMM", "VOC"], points=3)

        sorted_scores = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        winner_code = sorted_scores[0][0]
        code_map = { "PCM": "Science (PCM)", "PCB": "Science (PCB)", "COMM": "Commerce", "ARTS": "Arts & Humanities", "VOC": "Vocational Studies" }
        winner_name = code_map.get(winner_code, winner_code)
        
        # Save Score & Result
        if result:
            result.stream_scores = scores
            result.recommended_stream = winner_name

    else:
        # --- Logic for Class 12th & Above (No fixed scoring, pure AI Analysis) ---
        # We don't have a specific "stream" to recommend in the same way, but we will use the field for the primary recommendation
        scores = {} # Not used
        winner_name = "See Analysis" # Placeholder
        if result:
            result.stream_scores = {}
            result.recommended_stream = "AI Analyzing..." # Temporary

    # --- Common Save ---
    if result:
        result.final_answers = answers
        
        # --- AI Analysis (Gemini) ---
        if GEMINI_API_KEY:
            try:
                # Prepare Prompt based on Mode
                readable_answers = []
                
                if mode == "10th":
                     def get_question_text(q_id):
                        for section in all_questions.values():
                            for q in section["questions"]:
                                if q["id"] == q_id: return q["question"], q["options"]
                        return None, None
                     for q_id, ans_value in answers.items():
                        q_text, options = get_question_text(q_id)
                        if q_text:
                            selected_option = next((opt for opt in options if opt["value"] == ans_value), None)
                            ans_text = selected_option["text"] if selected_option else "Unknown"
                            readable_answers.append(f"Question: {q_text}\nSelected Answer: {ans_text}")
                
                elif mode == "12th":
                     # Use questions_12th data
                     q_map = {q["id"]: q for q in questions_12th}
                     for q_id, ans_text in answers.items():
                         if q_id in q_map:
                             readable_answers.append(f"Scenario: {q_map[q_id]['title']}\nInsight: {q_map[q_id]['insight']}\nUser Response: {ans_text}")

                elif mode == "above":
                     # Use questions_above_12th data
                     q_map = {q["id"]: q for q in questions_above_12th}
                     for q_id, ans_text in answers.items():
                         if q_id in q_map:
                             readable_answers.append(f"Question: {q_map[q_id]['title']}\nContext: {q_map[q_id]['insight']}\nUser Response: {ans_text}")

                answers_summary = "\n\n".join(readable_answers)
                phase2_cat = result.phase_2_category or "Unknown"
                
                # Dynamic Prompt Construction based on Class
                if mode == "10th":
                    task_instruction = f"""
                    1. The student's calculated best fit based on answers is "{winner_name}". Validate and Analyze this choice.
                    2. Provide a "Final Analysis" (approx 150 words) explaining WHY {winner_name} is the best fit based on their answers.
                    3. Provide 3 "Pros" (Why {winner_name} is good for the student).
                    4. Provide 3 "Cons" (Challenges to consider).
                    """
                    output_format = """
                    {
                      "recommended_stream": "Exact Stream Name",
                      "final_analysis": "Detailed explanation...",
                      "stream_pros": ["Pro 1", "Pro 2", "Pro 3"],
                      "stream_cons": ["Con 1", "Con 2", "Con 3"]
                    }
                    """
                elif mode == "12th":
                    task_instruction = """
                    1. Identify the Top 3 Career Goals / University Majors best suited for this student based on their scenarios.
                    2. For EACH goal, provide a specific "Reason" why they should go for that.
                    3. For EACH goal, provide 2 "Pros" (Advantages) and 2 "Cons" (Challenges).
                    4. Provide a "Final Analysis" (approx 100 words) summarizing their potential.
                    """
                    output_format = """
                    {
                      "recommended_stream": "Primary Field (e.g. Technology, Healthcare, Creative Arts)",
                      "final_analysis": "Summary...",
                      "goal_options": [
                        {
                            "title": "Option 1 Title", 
                            "reason": "Why they should choose this...",
                            "pros": ["Pro 1", "Pro 2"],
                            "cons": ["Con 1", "Con 2"]
                        },
                        {
                            "title": "Option 2 Title", 
                            "reason": "Why they should choose this...",
                            "pros": ["Pro 1", "Pro 2"],
                            "cons": ["Con 1", "Con 2"]
                        },
                        {
                            "title": "Option 3 Title", 
                            "reason": "Why they should choose this...",
                            "pros": ["Pro 1", "Pro 2"],
                            "cons": ["Con 1", "Con 2"]
                        }
                      ]
                    }
                    """
                else: # Above 12th
                    task_instruction = """
                    1. Identify the Top 3 Professional Roles / Niche Career Paths best suited for this student.
                    2. For EACH goal, provide a specific "Reason" why they should pursue it.
                    3. For EACH goal, provide 2 "Pros" (Advantages) and 2 "Cons" (Challenges).
                    4. Provide a "Final Analysis" (approx 100 words) on their professional outlook.
                    """
                    output_format = """
                    {
                      "recommended_stream": "Primary Field / Industry",
                      "final_analysis": "Summary...",
                      "goal_options": [
                        {
                            "title": "Role 1 Title", 
                            "reason": "Why this fits...",
                            "pros": ["Pro 1", "Pro 2"],
                            "cons": ["Con 1", "Con 2"]
                        },
                        {
                            "title": "Role 2 Title", 
                            "reason": "Why this fits...",
                            "pros": ["Pro 1", "Pro 2"],
                            "cons": ["Con 1", "Con 2"]
                        },
                        {
                            "title": "Role 3 Title", 
                            "reason": "Why this fits...",
                            "pros": ["Pro 1", "Pro 2"],
                            "cons": ["Con 1", "Con 2"]
                        }
                      ]
                    }
                    """

                prompt = f"""
                You are a fascinating and expert career counselor mentor. 
                Analyze this profile for a {mode} grade student with deep curiosity and professional empathy.

                Profile:
                - Archetype: {phase2_cat}
                - Insights Table:
                {answers_summary}

                Task:
                {task_instruction}
                
                CRITICAL ADDITION:
                In addition to the top 3 recommendations requested above, YOU MUST provide a 4th recommendation directly related to the student's hobbies and extracurricular interests (e.g. {answers.get('PI1_Hobbies', 'N/A')}, {answers.get('PI2_Extracurricular', 'N/A')}).
                
                Even if it's outside the standard academic path, suggest how their hobbies could lead to a professional career.
                
                The 4th recommendation should be returned in the same format as the others, but clearly labeled as "Hobby-based Recommendation" in your reasoning.

                You must speak with authority yet warmth. Output MUST be raw JSON only matching this structure. 
                {output_format}
                """
                
                # Generate Content with Fallback
                text = await generate_content_with_fallback(prompt)
                print(f"DEBUG: AI Raw Text: {text}")
                ai_data = json.loads(text)
                
                if mode != "10th" and "recommended_stream" in ai_data: 
                     result.recommended_stream = ai_data["recommended_stream"]
                if "final_analysis" in ai_data: result.final_analysis = ai_data["final_analysis"]
                
                # Handling Data Mapping
                if mode == "10th":
                    if "stream_pros" in ai_data: result.stream_pros = ai_data["stream_pros"]
                    if "stream_cons" in ai_data: result.stream_cons = ai_data["stream_cons"]
                    # Add hobby recommendation to analysis text
                    if "goal_options" in ai_data and len(ai_data["goal_options"]) > 0:
                        hobby_rec = ai_data["goal_options"][-1]
                        if isinstance(hobby_rec, dict):
                            title = hobby_rec.get('title', 'Alternative Path')
                            reason = hobby_rec.get('reason', '')
                            # Ensure final_analysis is a string before appending
                            if not result.final_analysis:
                                result.final_analysis = ""
                            result.final_analysis += f"\n\n**Special Interest Recommendation:** Based on your hobbies, you might also consider a career as a {title}. {reason}"
                else:
                    # Map 'goal_options' to 'stream_pros' for storage
                    if "goal_options" in ai_data: result.stream_pros = ai_data["goal_options"]
                    result.stream_cons = [] # Not used for 12th/Above
                    
            except Exception as e:
                print(f"AI Analysis Failed: {e}")
                result.final_analysis = f"AI Analysis Unavailable. (Error: {str(e)})"
        else:
             result.final_analysis = "AI Analysis Unavailable (API Key missing)."

        db.commit()

    return RedirectResponse(url="/assessment/result", status_code=status.HTTP_302_FOUND)


# --- Final Phase AI Chat Endpoint ---

class FinalChatRequest(BaseModel):
    message: str
    current_index: int = 0
    answers: dict = {}
    mode: str = "10th"

FINAL_COUNSELLOR_PROMPT = """
You are a warm and insightful AI career counsellor conducting a final career assessment.
Your tone is encouraging, curious, and professional.
- Keep responses concise (3-5 sentences).
- For Multiple Choice questions (10th mode), present the question then list ALL options clearly (A, B, C, D or as given).
- For open-ended scenarios (12th / above mode), present the scenario warmly, then invite the student to share their thoughts.
- After the student replies, give a brief, encouraging acknowledgement (1 sentence) before presenting the next question.
- Never make up questions. Only use the question data provided to you.
"""

@app.post("/assessment/final/chat")
async def final_chat(request: Request, chat_req: FinalChatRequest, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    from fastapi.responses import JSONResponse
    mode = chat_req.mode
    current_idx = chat_req.current_index

    # Build flat question list based on mode
    if mode == "10th":
        flat_questions = []
        for section_id, section_data in all_questions.items():
            for q in section_data["questions"]:
                q_dict = {
                    "id": q["id"],
                    "title": q["question"],
                    "section": section_data["title"]
                }
                if q.get("type") == "open":
                    q_dict["type"] = "open"
                else:
                    q_dict["type"] = "mcq"
                    q_dict["options"] = [{"value": o["value"], "text": o["text"]} for o in q["options"]]
                flat_questions.append(q_dict)
    elif mode == "12th":
        flat_questions = [
            {"id": q["id"], "title": q["title"], "text": q["text"], "insight": q["insight"], "type": "open"}
            for q in questions_12th
        ]
    else:  # above
        flat_questions = [
            {"id": q["id"], "title": q["title"], "text": q["text"], "insight": q["insight"], "type": "open"}
            for q in questions_above_12th
        ]

    total = len(flat_questions)

    # All done
    if current_idx >= total:
        return JSONResponse({
            "response": "Wonderful! You've answered all the questions. Click **Get My Career Path** to generate your personalised AI career insights! 🎯",
            "current_index": current_idx,
            "answers": chat_req.answers,
            "done": True
        })

    # Format current question for the prompt
    q = flat_questions[current_idx]
    if q["type"] == "mcq":
        options_text = "\n".join([f"  Option {o['value'].upper()}: {o['text']}" for o in q["options"]])
        current_q_text = f"""Question {current_idx + 1} of {total} [{q['section']}]:
{q['title']}
{options_text}"""
    else:
        current_q_text = f"""Question {current_idx + 1} of {total}:
Title: {q['title']}
Scenario: {q['text']}
(Focus: {q['insight']})"""

    if chat_req.message and chat_req.message.strip():
        # User replied — move to next
        next_idx = current_idx + 1
        if next_idx >= total:
            prompt = f"""{FINAL_COUNSELLOR_PROMPT}

The student just answered a question. Their response: "{chat_req.message}"
Give a warm 1-sentence acknowledgement, then tell them they've completed all questions and should click the button to get their results."""
            new_idx = next_idx
            done = True
        else:
            next_q = flat_questions[next_idx]
            if next_q["type"] == "mcq":
                next_opts = "\n".join([f"  Option {o['value'].upper()}: {o['text']}" for o in next_q["options"]])
                next_q_text = f"""Question {next_idx + 1} of {total} [{next_q['section']}]:
{next_q['title']}
{next_opts}"""
            else:
                next_q_text = f"""Question {next_idx + 1} of {total}:
Title: {next_q['title']}
Scenario: {next_q['text']}
(Focus: {next_q['insight']})"""
            prompt = f"""{FINAL_COUNSELLOR_PROMPT}

The student just answered the previous question. Their response: "{chat_req.message}"
Acknowledge in 1 warm sentence, then present the next question:

{next_q_text}"""
            new_idx = next_idx
            done = False
    else:
        # First load — present first question
        prompt = f"""{FINAL_COUNSELLOR_PROMPT}

Welcome the student warmly (1 sentence), then present this first question:

{current_q_text}"""
        new_idx = current_idx
        done = False

    # Call Gemini with Groq fallback
    try:
        if GEMINI_API_KEY:
            try:
                model_ai = genai.GenerativeModel("gemini-2.0-flash")
                response = await model_ai.generate_content_async(prompt)
                ai_text = response.text
            except Exception:
                model_ai = genai.GenerativeModel("gemini-1.5-flash")
                response = await model_ai.generate_content_async(prompt)
                ai_text = response.text
        elif groq_client:
            completion = await groq_client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model="llama-3.3-70b-versatile",
            )
            ai_text = completion.choices[0].message.content
        else:
            ai_text = f"[Demo Mode] {current_q_text}"
    except Exception as e:
        ai_text = f"I seem to be in deep thought right now. ({str(e)}) Please try again."

    return JSONResponse({
        "response": ai_text,
        "current_index": new_idx,
        "answers": chat_req.answers,
        "done": done
    })


# --- Bark TTS Route ---

import httpx

@app.get("/api/tts")
async def generate_tts(text: str):
    """
    Generates high-quality audio using stable models (MMS) or expressive models (Bark).
    """
    HF_TOKEN = os.getenv("HF_TOKEN")
    
    # Switch to facebook/mms-tts-eng for a MUCH more stable, professional, and clear voice.
    # Bark (suno/bark) is artistic but often "cracky" or "glitchy" on the serverless API.
    API_URL = "https://api-inference.huggingface.co/models/facebook/mms-tts-eng"
    
    # Optional: If you REALLY want Bark, we can add a prompt to it, 
    # but MMS is recommended for clear educational/assessment questions.
    # API_URL = "https://api-inference.huggingface.co/models/suno/bark-small"

    if not HF_TOKEN:
        return {"error": "HF_TOKEN missing in .env", "fallback": True}

    headers = {"Authorization": f"Bearer {HF_TOKEN}"}
    
    async with httpx.AsyncClient() as client:
        try:
            # For Bark, we'd want to prepend a voice preset like "[v2/en_speaker_6]"
            # But for MMS, we just send the text.
            payload = {"inputs": text}
            
            response = await client.post(API_URL, headers=headers, json=payload, timeout=30.0)
            
            if response.status_code == 200:
                return Response(content=response.content, media_type="audio/wav")
            elif response.status_code == 503:
                return {"error": "Model is currently loading on Hugging Face. Please try again in a few seconds.", "loading": True}
            else:
                # If MMS fails, maybe try Bark small as a backup?
                return {"error": f"HF API Error: {response.text}", "status": response.status_code}
        except Exception as e:
            return {"error": str(e)}


# --- Chatbot Routes ---

class ChatRequest(BaseModel):
    message: str

class ResolveVoiceRequest(BaseModel):
    transcript: str
    options: list

@app.get("/chatbot", response_class=HTMLResponse)
async def chatbot_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    # Fetch History
    history = db.query(models.ChatMessage).filter(models.ChatMessage.user_id == user.id).order_by(models.ChatMessage.timestamp).all()
    
    return templates.TemplateResponse(request=request, name="chatbot.html", context={"user": user, "history": history})

@app.post("/assessment/resolve-voice")
async def resolve_voice(req: ResolveVoiceRequest):
    """
    Uses AI to match a voice transcript to one of the provided multiple-choice options.
    """
    prompt = f"""
    The student spoke this answer for a career assessment question: "{req.transcript}"
    
    Which of these options best matches what they said?
    Options:
    {json.dumps(req.options, indent=2)}
    
    Output ONLY valid JSON with the field "best_match" containing the "value" of the matching option.
    If no good match exists, return the most likely one based on interest.
    """
    
    try:
        clean_text = await generate_content_with_fallback(prompt)
        result = json.loads(clean_text)
        return result
    except Exception as e:
        print(f"Voice Resolution Error: {e}")
        # Default to the first option if AI fails
        return {"best_match": req.options[0]["value"] if req.options else "A"}

# --- Chatbot Routes ---

@app.post("/chatbot/message")
async def chatbot_message(request: Request, chat_req: ChatRequest, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    if user.is_suspended:
        return {"error": "Your account has been suspended for violating our content policies."}

    user_message = chat_req.message
    
    # Content Moderation Check
    is_flagged, reason = await check_content_moderation(user_message)
    if is_flagged:
        flag = models.ModerationFlag(user_id=user.id, content=user_message, chat_type="ai", status="pending_review")
        db.add(flag)
        db.commit()
        return {"error": "Your message was flagged as inappropriate. Repeated violations will lead to account suspension."}

    # Save User Message
    user_msg_db = models.ChatMessage(user_id=user.id, sender="user", content=user_message)
    db.add(user_msg_db)
    db.commit()
    
    # 1. Build Context from DB
    result = db.query(models.AssessmentResult).filter(models.AssessmentResult.user_id == user.id).first()
    
    context_str = f"User Name: {user.full_name}\n"
    if result:
        if result.selected_class:
            context_str += f"Class/Grade: {result.selected_class}\n"
        if result.phase_2_category:
            context_str += f"Personality Archetype: {result.phase_2_category}\n"
        if result.recommended_stream:
            context_str += f"Recommended Path: {result.recommended_stream}\n"
        
        # Add slight detail if available
        if result.phase3_analysis:
             context_str += f"Work Style Analysis: {result.phase3_analysis[:200]}...\n"

    # Fetch recent history for context
    recent_history = db.query(models.ChatMessage).filter(models.ChatMessage.user_id == user.id).order_by(models.ChatMessage.timestamp.desc()).limit(10).all()
    recent_history.reverse() # Oldest first
    history_str = "\n".join([f"{msg.sender.upper()}: {msg.content}" for msg in recent_history])

    # 2. Construct System Prompt
    prompt = f"""
You are the 'CareStance Mentor'. You are professional, deeply empathetic, and highly knowledgeable about global career trends.

STUDENT PROFILE:
- Name: {user.full_name}
- Grade Level: {result.selected_class if result else 'Not Selected'}
- Archetype: {result.phase_2_category if result else 'Analyzing...'}
- Calculated Recommendation: {result.recommended_stream if result else 'In progress'}

GUIDELINES:
1. PERSONALIZATION: If the student asks 'What should I do?', refer to their Archetype ({result.phase_2_category}) specifically.
2. TONE: Be a mentor, not an encyclopedia. Use phrases like "Looking at your preference for {result.personality if result else 'collaboration'}..."
3. STRUCTURE: Use bold text for key career roles and bullet points for steps.
4. SCOPE: Focus 100% on careers, education, and professional growth.

LATEST STUDENT MESSAGE: "{user_message}"
CONVERSATION HISTORY: {history_str}

Response (Concise, Markdown formatted):
"""
    
    # 3. Stream AI Response with Fallback
    user_id = user.id
    async def generate():
        full_response_text = ""
        # Create a new local session for the generator because the route session 
        # closes after the response object is returned but before streaming finishes.
        local_db = SessionLocal()
        try:
            # TRY GEMINI FIRST
            if GEMINI_API_KEY:
                try:
                    print(f"AI Chat for User {user_id}: Trying Gemini...")
                    model = genai.GenerativeModel("gemini-1.5-flash")
                    response = await model.generate_content_async(prompt, stream=True)
                    async for chunk in response:
                        if chunk.text:
                            text_chunk = chunk.text
                            full_response_text += text_chunk
                            yield text_chunk
                except Exception as gemini_e:
                    print(f"Chatbot Gemini Error: {gemini_e}. Trying Groq fallback.")
                    # FALLBACK TO GROQ
                    if groq_client:
                        try:
                            stream = await groq_client.chat.completions.create(
                                messages=[{"role": "user", "content": prompt}],
                                model="llama-3.3-70b-versatile",
                                stream=True,
                            )
                            async for chunk in stream:
                                if chunk.choices[0].delta.content:
                                    text_chunk = chunk.choices[0].delta.content
                                    full_response_text += text_chunk
                                    yield text_chunk
                        except Exception as groq_e:
                            print(f"Chatbot Groq Error: {groq_e}")
                            yield f"I'm sorry, both AI services are currently unavailable. (Gemini: {str(gemini_e)}, Groq: {str(groq_e)})"
                    else:
                        yield f"AI Service error: {str(gemini_e)}"
            else:
                 # Demo Mode Simulation
                 fake_response = "I'm in demo mode (No API Key). Based on your profile, I'd suggest exploring based on your interests! (Please set GEMINI_API_KEY to get real AI responses)"
                 for word in fake_response.split():
                     text_chunk = word + " "
                     full_response_text += text_chunk
                     yield text_chunk
                     import asyncio
                     await asyncio.sleep(0.05) 
            
            # Save AI Message using local_db
            if full_response_text:
                ai_msg_db = models.ChatMessage(user_id=user_id, sender="ai", content=full_response_text)
                local_db.add(ai_msg_db)
                local_db.commit()
                print(f"AI Chat for User {user_id}: Message saved.")

        except Exception as e:
            print(f"Chat Error in generator: {e}")
            error_msg = f"I'm having a little trouble thinking right now. (Error: {str(e)})"
            yield error_msg
            ai_msg_db = models.ChatMessage(user_id=user_id, sender="ai", content=error_msg)
            local_db.add(ai_msg_db)
            local_db.commit()
        finally:
            local_db.close()

    return StreamingResponse(generate(), media_type="text/plain")


# --- Feedback Routes ---

@app.get("/feedback", response_class=HTMLResponse)
async def feedback_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse(request=request, name="feedback.html", context={"user": user})

@app.post("/feedback")
async def submit_feedback(
    request: Request,
    content: str = Form(...),
    rating: int = Form(...),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    new_feedback = models.Feedback(
        user_id=user.id,
        content=content,
        rating=rating
    )
    db.add(new_feedback)
    db.commit()
    
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)

# --- Ticket System Routes ---

@app.get("/ticket", response_class=HTMLResponse)
async def ticket_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    tickets = db.query(models.Ticket).filter(models.Ticket.user_id == user.id).order_by(models.Ticket.timestamp.desc()).all()
    return templates.TemplateResponse(request=request, name="ticket.html", context={"user": user, "tickets": tickets})

@app.post("/ticket/submit")
async def submit_ticket(
    request: Request,
    subject: str = Form(...),
    description: str = Form(...),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    new_ticket = models.Ticket(
        user_id=user.id,
        subject=subject,
        description=description
    )
    db.add(new_ticket)
    db.commit()
    
    if user.is_suspended:
        return RedirectResponse(url="/suspended?ticket_submitted=true", status_code=status.HTTP_302_FOUND)
        
    return RedirectResponse(url="/ticket", status_code=status.HTTP_302_FOUND)


# --- Career Path Generation ---

class CareerPathRequest(BaseModel):
    career_title: str

@app.post("/assessment/generate_path")
async def generate_career_path(request: Request, path_req: CareerPathRequest, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    result = db.query(models.AssessmentResult).filter(models.AssessmentResult.user_id == user.id).first()
    if not result:
        raise HTTPException(status_code=404, detail="Assessment results not found")

    archetype = result.phase_2_category or "Explorer"
    personality = result.personality or "Ambivert"
    current_class = result.selected_class or "10th"
    phase3_insight = result.phase3_analysis or ""
    final_insight = result.final_analysis or ""

    prompt = f"""
    You are an expert 'Student Success Architect' and Career Mentor.
    
    Student Profile:
    - Current Stage: {current_class} (Handle this as the starting point)
    - Archetype: {archetype} (Influences the learning style and interaction)
    - Personality: {personality} (Determines the type of environment suggested)
    - Goal Career: {path_req.career_title}
    - Deep Analysis Context: {phase3_insight[:400]}
    - Recommendation Engine Notes: {final_insight[:400]}

    TASK:
    Create a "Zero-to-Hero" Career Roadmap. The journey MUST start from absolute BASICS (Phase 1-2) and evolve into PROFESSIONAL/PRO level (Phase 5-6).
    
    Tone: 
    - Student-friendly, encouraging, and visionary. 
    - Use "We" and "You" to make it feel like a partnership. 
    - Avoid dry corporate jargon where simple, inspiring words work better.

    Provide exactly 6 Milestone Steps:
    - Step 1-2: Foundations (The "Basics" - Learning, early exploration, building mindset). 
    - Step 3-4: Intermediate (Core skill building, first real projects, networking).
    - Step 5-6: Professional (Specialization, portfolio polishing, high-level internships, job readiness).

    For EACH step, include:
    1. Action Name (Catchy & motivating)
    2. Description (Explain WHY this step matters for their specific profile - 3 sentences)
    3. Skills to acquire (3 specific skills relevant to {path_req.career_title})
    4. Resources (MUST provide 2 specific, HIGHLY ACCURATE resources. EACH resource MUST be an object with a "name" and a functional "url". PRIORITIZE DIRECT LINKS to the **most viewed/popular** YouTube videos or verified courses (Coursera, Udemy, Official Docs). Use HIGHLY SPECIFIC search queries ONLY as a secondary fallback if a direct video link is absolutely unavailable for the specific topic. Plain text without URLs is FORBIDDEN.)
    5. Student Project (1 "Masterpiece" project. MUST include: A catchy Name, a precise Description, specific **Tools to use** (libraries/software), and key **Parameters/Challenges** to think about for a professional finish. Format it clearly.)
    6. Detailed Task (A precise, actionable, and detailed task for the student to complete for this specific step)
    7. Timeline (Realistic estimate, e.g., "Months 1-3")

    Additional Career Insights:
    - Internships: 2 specific "Dream Internships" or types of roles to hunt for.
    - Career Outlook:
        - Salary Journey: Entry-level to Senior potential (in INR or USD as appropriate).
        - Top 3 Companies: Famous places that hire this role.
        - Hiring Trends: A detailed paragraph on current hiring demand, specific roles being filled, and what recruiters look for in {path_req.career_title}.
        - Future Scope: Why this career is a "Safe Bet" or "High Growth" path for the next decade.

    OUTPUT FORMAT (VALID JSON ONLY):
    {{
      "career_title": "{path_req.career_title}",
      "path_steps": [
        {{ 
          "step": 1, 
          "action": "...", 
          "description": "...", 
          "skills": ["...", "...", "..."],
          "courses": [
            {{ "name": "...", "url": "..." }},
            {{ "name": "...", "url": "..." }}
          ],
          "project": {{
            "name": "Catchy Project Name",
            "description": "Short, inspiring project description...",
            "tools": ["Tool 1", "Tool 2"],
            "parameters": ["Key Factor 1", "Key Factor 2"]
          }},
          "detailed_task": "A precise, step-by-step action for this specific goal...",
          "timeline": "...",
          "completed": false
        }},
        ... (Total 6)
      ],
      "internships": ["...", "..."],
      "career_outlook": {{
        "salary_range": "...",
        "top_companies": ["...", "...", "..."],
        "hiring_trends": "Detailed hiring trends and recruiter expectations...",
        "future_scope": "..."
      }},
      "reminders": [
        {{ "milestone": "...", "reminder": "..." }},
        ...
      ]
    }}
    """

    try:
        clean_text = await generate_content_with_fallback(prompt)
        path_data = json.loads(clean_text)
        
        # Save to DB
        new_path = models.CareerPath(
            user_id=user.id,
            career_title=path_data.get("career_title", path_req.career_title),
            path_data=path_data.get("path_steps", []),
            reminders=path_data.get("reminders", []),
            # We can store the extra info in the path_data or reminders, 
            # but let's keep it clean by putting everything in one JSON if possible, 
            # or use path_data for the core steps and add a new column if needed.
            # Actually, I'll put the career_outlook and internships into the reminders or a new field if I update the model.
            # Let's update the model to have a general 'meta_data' field or just expand path_data.
        )
        # To avoid database schema migration immediately, I'll wrap everything into path_data
        # and reminders.
        
        # Merge outlook into reminders as a special entry if needed, or just keep it in path_data.
        # Let's put everything extra into a special 'extra_info' key in path_data or just use JSON column flexibility.
        
        full_path_data = {
            "steps": path_data.get("path_steps", []),
            "internships": path_data.get("internships", []),
            "career_outlook": path_data.get("career_outlook", {}),
            "reminders": path_data.get("reminders", [])
        }
        
        new_path.path_data = full_path_data
        
        db.add(new_path)
        db.commit()
        db.refresh(new_path)

        return {"success": True, "path_id": new_path.id}
    except Exception as e:
        print(f"Career Path Generation Error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate career path: {str(e)}")

@app.post("/career/roadmap/{path_id}/step/{step_index}/toggle")
async def toggle_step_completion(path_id: int, step_index: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    path = db.query(models.CareerPath).filter(models.CareerPath.id == path_id, models.CareerPath.user_id == user.id).first()
    if not path:
        raise HTTPException(status_code=404, detail="Roadmap not found")
    
    # Update path_data
    data = path.path_data
    if data is None:
        raise HTTPException(status_code=400, detail="Path data is empty")

    steps = []
    if isinstance(data, dict) and "steps" in data:
        steps = data["steps"]
    elif isinstance(data, list):
        steps = data
    else:
        # Fallback for unexpected format
        raise HTTPException(status_code=400, detail="Invalid path data format")

    if 0 <= step_index < len(steps):
        # Toggle completed state
        is_completed = steps[step_index].get("completed", False)
        steps[step_index]["completed"] = not is_completed
        
        # Update progress percentage if it's a dict with steps
        if isinstance(data, dict) and "steps" in data:
            completed_count = sum(1 for s in data["steps"] if s.get("completed", False))
            data["progress_percentage"] = int((completed_count / len(data["steps"])) * 100)
    else:
        raise HTTPException(status_code=400, detail="Invalid step index")
    
    path.path_data = data
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(path, "path_data")
    
    db.add(path) # Ensure it's in the session correctly
    db.commit()
    db.refresh(path)
    
    return {
        "success": True, 
        "completed": steps[step_index]["completed"], 
        "progress": data.get("progress_percentage", 0) if isinstance(data, dict) else 0
    }

@app.get("/career/roadmaps", response_class=HTMLResponse)
async def view_roadmaps(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    paths = db.query(models.CareerPath).filter(models.CareerPath.user_id == user.id).all()
    return templates.TemplateResponse(request=request, name="career_roadmaps.html", context={"user": user, "paths": paths})

@app.get("/career/roadmap/{path_id}", response_class=HTMLResponse)
async def view_roadmap_detail(path_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    path = db.query(models.CareerPath).filter(models.CareerPath.id == path_id, models.CareerPath.user_id == user.id).first()
    if not path:
        raise HTTPException(status_code=404, detail="Roadmap not found")
        
    assessment = db.query(models.AssessmentResult).filter(models.AssessmentResult.user_id == user.id).first()
    appointments = db.query(models.Appointment).filter(models.Appointment.student_id == user.id).all()
        
    return templates.TemplateResponse(request=request, name="career_roadmap_v2.html", context={
        "user": user, 
        "path": path,
        "assessment": assessment,
        "appointments": appointments
    })



@app.get("/career/roadmap/{path_id}/resources", response_class=HTMLResponse)
async def view_roadmap_resources(path_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    path = db.query(models.CareerPath).filter(models.CareerPath.id == path_id, models.CareerPath.user_id == user.id).first()
    if not path:
        raise HTTPException(status_code=404, detail="Roadmap not found")
        
    career_title = path.career_title
    keywords = career_keywords.get(career_title, [career_title]) # Fallback to title if not in keywords mapping
    
    resources = {
        "ndli": ResourceAggregator.get_ndli_link(keywords),
        "arxiv": ResourceAggregator.get_arxiv_link(keywords),
        "youtube": ResourceAggregator.get_youtube_link(keywords),
        "scholar": ResourceAggregator.get_google_scholar_link(keywords)
    }
    
    # NEW: Fetch AI recommendations
    ai_recommendations = await ResourceAggregator.get_ai_recommendations(career_title, generate_content_with_fallback)
    
    return templates.TemplateResponse(request=request, name="resources_dashboard.html", context={
        "user": user, 
        "path": path,
        "resources": resources,
        "ai_recommendations": ai_recommendations,
        "keywords": keywords
    })

# --- College Recommendation Routes ---

class CollegeRecRequest(BaseModel):
    career_title: str

@app.post("/career/colleges/generate")
async def generate_college_recommendations(request: Request, req: CollegeRecRequest, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    result = db.query(models.AssessmentResult).filter(models.AssessmentResult.user_id == user.id).first()

    current_class = result.selected_class if result else "12th"
    archetype = result.phase_2_category if result else "Explorer"
    personality = result.personality if result else "Ambivert"

    prompt = f"""
    You are an expert 'College Admission Strategist' and Academic Mentor for Indian and global students.

    Student Profile:
    - Current Stage: {current_class}
    - Archetype: {archetype}
    - Personality: {personality}
    - Target Career: {req.career_title}

    TASK:
    Recommend the Top 5 Colleges/Institutes (mix of Indian and International) that are BEST suited 
    for a student aiming to become a "{req.career_title}".

    For EACH college, provide:
    1. "name" — Full official name
    2. "location" — City, Country
    3. "ranking" — A short ranking label (e.g. "Top 3 in India", "#12 Globally")
    4. "admission_criteria" — Detailed admission requirements (exams, cutoffs, key dates, eligibility). 3-4 sentences.
    5. "courses_offered" — List of 3-5 specific relevant degree programs (e.g. "B.Tech Computer Science", "M.Sc Data Science")
    6. "placement_rate" — Percentage or descriptor (e.g. "95%", "Near 100%")
    7. "avg_package" — Average salary package for graduates (in INR or USD)
    8. "top_recruiters" — List of 3-4 top companies that recruit from this institute
    9. "highlights" — 2-3 sentence overview of what makes this institute special for this career
    10. "website" — Official website URL

    Also provide:
    - "preparation_tips" — 3-4 bullet points of actionable advice for gaining admission to these institutes

    OUTPUT FORMAT (VALID JSON ONLY):
    {{
      "colleges": [
        {{
          "name": "...",
          "location": "...",
          "ranking": "...",
          "admission_criteria": "...",
          "courses_offered": ["...", "...", "..."],
          "placement_rate": "...",
          "avg_package": "...",
          "top_recruiters": ["...", "...", "..."],
          "highlights": "...",
          "website": "https://..."
        }}
      ],
      "preparation_tips": ["...", "...", "...", "..."]
    }}
    """

    try:
        clean_text = await generate_content_with_fallback(prompt)
        college_data = json.loads(clean_text)

        new_rec = models.CollegeRecommendation(
            user_id=user.id,
            career_title=req.career_title,
            college_data=college_data,
        )
        db.add(new_rec)
        db.commit()
        db.refresh(new_rec)

        return {"success": True, "rec_id": new_rec.id}
    except Exception as e:
        print(f"College Recommendation Error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate college recommendations: {str(e)}")


@app.get("/career/colleges", response_class=HTMLResponse)
async def view_college_recommendations(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    recs = db.query(models.CollegeRecommendation).filter(models.CollegeRecommendation.user_id == user.id).order_by(models.CollegeRecommendation.created_at.desc()).all()
    return templates.TemplateResponse(request=request, name="college_recommendations.html", context={"user": user, "recs": recs})


@app.get("/career/colleges/{rec_id}", response_class=HTMLResponse)
async def view_college_detail(rec_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    rec = db.query(models.CollegeRecommendation).filter(
        models.CollegeRecommendation.id == rec_id,
        models.CollegeRecommendation.user_id == user.id
    ).first()
    if not rec:
        raise HTTPException(status_code=404, detail="College recommendation not found")

    return templates.TemplateResponse(request=request, name="college_detail.html", context={"user": user, "rec": rec})


# ─── Student Community & Connection Routes ────────────────────────────────────

@app.get("/community", response_class=HTMLResponse)
async def community_page(request: Request, db: Session = Depends(get_db)):
    """Community page: discover students grouped by archetype."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    # Get current user's archetype
    my_assessment = db.query(models.AssessmentResult).filter(
        models.AssessmentResult.user_id == user.id
    ).first()
    my_archetype = my_assessment.phase_2_category if my_assessment else None

    # Get all students (except current user) who have completed their assessment
    students_with_assessments = (
        db.query(models.User, models.AssessmentResult)
        .join(models.AssessmentResult, models.User.id == models.AssessmentResult.user_id)
        .filter(
            models.User.role == "student",
            models.User.id != user.id,
            models.AssessmentResult.phase_2_category.isnot(None)
        )
        .all()
    )

    # Build connection status map for current user
    my_connections = db.query(models.StudentConnection).filter(
        (models.StudentConnection.requester_id == user.id) |
        (models.StudentConnection.receiver_id == user.id)
    ).all()

    connection_map = {}  # user_id -> {"status": ..., "conn_id": ..., "is_requester": bool}
    for conn in my_connections:
        other_id = conn.receiver_id if conn.requester_id == user.id else conn.requester_id
        connection_map[other_id] = {
            "status": conn.status,
            "conn_id": conn.id,
            "is_requester": conn.requester_id == user.id
        }

    # Group students by archetype
    similar_students = []
    other_archetypes = {}
    for student, assessment in students_with_assessments:
        student_data = {
            "user": student,
            "assessment": assessment,
            "connection": connection_map.get(student.id)
        }
        if my_archetype and assessment.phase_2_category == my_archetype:
            similar_students.append(student_data)
        else:
            archetype = assessment.phase_2_category
            if archetype not in other_archetypes:
                other_archetypes[archetype] = []
            other_archetypes[archetype].append(student_data)

    # Count pending received requests
    pending_count = db.query(models.StudentConnection).filter(
        models.StudentConnection.receiver_id == user.id,
        models.StudentConnection.status == "pending"
    ).count()

    return templates.TemplateResponse(request=request, name="community.html", context={
        "user": user,
        "my_archetype": my_archetype,
        "similar_students": similar_students,
        "other_archetypes": other_archetypes,
        "pending_count": pending_count,
    })


@app.get("/student/{user_id}", response_class=HTMLResponse)
async def student_profile(user_id: int, request: Request, db: Session = Depends(get_db)):
    """Public profile page for a student."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    student = db.query(models.User).filter(
        models.User.id == user_id,
        models.User.role == "student"
    ).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    assessment = db.query(models.AssessmentResult).filter(
        models.AssessmentResult.user_id == user_id
    ).first()

    # Connection status between current user and this student
    connection = db.query(models.StudentConnection).filter(
        ((models.StudentConnection.requester_id == user.id) & (models.StudentConnection.receiver_id == user_id)) |
        ((models.StudentConnection.requester_id == user_id) & (models.StudentConnection.receiver_id == user.id))
    ).first()

    conn_info = None
    if connection:
        conn_info = {
            "status": connection.status,
            "conn_id": connection.id,
            "is_requester": connection.requester_id == user.id
        }

    # Get this student's accepted connections (for sidebar)
    accepted_connections = db.query(models.StudentConnection).filter(
        ((models.StudentConnection.requester_id == user_id) |
         (models.StudentConnection.receiver_id == user_id)),
        models.StudentConnection.status == "accepted"
    ).all()

    connected_users = []
    for conn in accepted_connections:
        other_id = conn.receiver_id if conn.requester_id == user_id else conn.requester_id
        other_user = db.query(models.User).filter(models.User.id == other_id).first()
        if other_user:
            connected_users.append(other_user)

    is_own_profile = (user.id == user_id)

    return templates.TemplateResponse(request=request, name="student_profile.html", context={
        "user": user,
        "student": student,
        "assessment": assessment,
        "connection": conn_info,
        "connected_users": connected_users,
        "is_own_profile": is_own_profile,
    })


@app.post("/connect/{user_id}")
async def send_connection_request(user_id: int, request: Request, db: Session = Depends(get_db)):
    """Send a connection request to another student."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    if user.id == user_id:
        return RedirectResponse(url="/community", status_code=status.HTTP_302_FOUND)

    # Check receiver exists and is a student
    receiver = db.query(models.User).filter(models.User.id == user_id, models.User.role == "student").first()
    if not receiver:
        raise HTTPException(status_code=404, detail="Student not found")

    # Check for existing connection
    existing = db.query(models.StudentConnection).filter(
        ((models.StudentConnection.requester_id == user.id) & (models.StudentConnection.receiver_id == user_id)) |
        ((models.StudentConnection.requester_id == user_id) & (models.StudentConnection.receiver_id == user.id))
    ).first()

    if existing:
        # Already exists – redirect back
        referer = request.headers.get("referer", "/community")
        return RedirectResponse(url=referer, status_code=status.HTTP_302_FOUND)

    new_conn = models.StudentConnection(requester_id=user.id, receiver_id=user_id, status="pending")
    db.add(new_conn)
    db.commit()

    # Send Email Notification
    try:
        app_url = os.getenv("APP_URL", str(request.base_url).rstrip("/"))
        profile_link = f"{app_url}/student/{user.id}"
        email_body = email_utils.get_connection_request_template(
            receiver.full_name, 
            user.full_name, 
            profile_link
        )
        email_utils.send_email(receiver.email, f"New Connection Request from {user.full_name}", email_body)
    except Exception as e:
        print(f"FAILED TO SEND CONNECTION EMAIL: {e}")

    referer = request.headers.get("referer", "/community")
    return RedirectResponse(url=referer, status_code=status.HTTP_302_FOUND)


@app.post("/connection/{conn_id}/accept")
async def accept_connection(conn_id: int, request: Request, db: Session = Depends(get_db)):
    """Accept a pending connection request."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    conn = db.query(models.StudentConnection).filter(
        models.StudentConnection.id == conn_id,
        models.StudentConnection.receiver_id == user.id,
        models.StudentConnection.status == "pending"
    ).first()

    if not conn:
        raise HTTPException(status_code=404, detail="Connection request not found")

    conn.status = "accepted"
    db.commit()

    referer = request.headers.get("referer", "/my-connections")
    return RedirectResponse(url=referer, status_code=status.HTTP_302_FOUND)


@app.post("/connection/{conn_id}/reject")
async def reject_connection(conn_id: int, request: Request, db: Session = Depends(get_db)):
    """Reject a pending connection request."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    conn = db.query(models.StudentConnection).filter(
        models.StudentConnection.id == conn_id,
        models.StudentConnection.receiver_id == user.id,
        models.StudentConnection.status == "pending"
    ).first()

    if not conn:
        raise HTTPException(status_code=404, detail="Connection request not found")

    db.delete(conn)
    db.commit()

    referer = request.headers.get("referer", "/my-connections")
    return RedirectResponse(url=referer, status_code=status.HTTP_302_FOUND)


@app.post("/connection/{conn_id}/withdraw")
async def withdraw_connection(conn_id: int, request: Request, db: Session = Depends(get_db)):
    """Withdraw a pending connection request sent by the current user."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    conn = db.query(models.StudentConnection).filter(
        models.StudentConnection.id == conn_id,
        models.StudentConnection.requester_id == user.id,
        models.StudentConnection.status == "pending"
    ).first()

    if not conn:
        raise HTTPException(status_code=404, detail="Connection request not found or cannot be withdrawn")

    db.delete(conn)
    db.commit()

    referer = request.headers.get("referer", "/community")
    return RedirectResponse(url=referer, status_code=status.HTTP_302_FOUND)


@app.post("/disconnect/{user_id}")
async def disconnect_user(user_id: int, request: Request, db: Session = Depends(get_db)):
    """Remove an existing connection."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    conn = db.query(models.StudentConnection).filter(
        ((models.StudentConnection.requester_id == user.id) & (models.StudentConnection.receiver_id == user_id)) |
        ((models.StudentConnection.requester_id == user_id) & (models.StudentConnection.receiver_id == user.id)),
        models.StudentConnection.status == "accepted"
    ).first()

    if conn:
        db.delete(conn)
        db.commit()

    referer = request.headers.get("referer", "/community")
    return RedirectResponse(url=referer, status_code=status.HTTP_302_FOUND)


@app.post("/profile/update-bio")
async def update_bio(request: Request, bio: str = Form(""), db: Session = Depends(get_db)):
    """Update the current user's bio."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    user.bio = bio.strip()[:500]  # Limit to 500 chars
    db.commit()

    referer = request.headers.get("referer", "/dashboard")
    return RedirectResponse(url=referer, status_code=status.HTTP_302_FOUND)


@app.get("/my-connections", response_class=HTMLResponse)
async def my_connections_page(request: Request, db: Session = Depends(get_db)):
    """View accepted connections and pending requests."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    # Accepted connections
    accepted = db.query(models.StudentConnection).filter(
        ((models.StudentConnection.requester_id == user.id) |
         (models.StudentConnection.receiver_id == user.id)),
        models.StudentConnection.status == "accepted"
    ).all()

    connected_users = []
    for conn in accepted:
        other_id = conn.receiver_id if conn.requester_id == user.id else conn.requester_id
        other_user = db.query(models.User).filter(models.User.id == other_id).first()
        if other_user:
            other_assessment = db.query(models.AssessmentResult).filter(
                models.AssessmentResult.user_id == other_id
            ).first()
            connected_users.append({
                "user": other_user,
                "assessment": other_assessment,
                "conn_id": conn.id
            })

    # Pending received requests
    pending_received = db.query(models.StudentConnection).filter(
        models.StudentConnection.receiver_id == user.id,
        models.StudentConnection.status == "pending"
    ).all()

    pending_requests = []
    for conn in pending_received:
        requester = db.query(models.User).filter(models.User.id == conn.requester_id).first()
        if requester:
            req_assessment = db.query(models.AssessmentResult).filter(
                models.AssessmentResult.user_id == conn.requester_id
            ).first()
            pending_requests.append({
                "user": requester,
                "assessment": req_assessment,
                "conn_id": conn.id
            })

    # Pending sent requests
    pending_sent = db.query(models.StudentConnection).filter(
        models.StudentConnection.requester_id == user.id,
        models.StudentConnection.status == "pending"
    ).all()

    sent_requests = []
    for conn in pending_sent:
        receiver = db.query(models.User).filter(models.User.id == conn.receiver_id).first()
        if receiver:
            sent_requests.append({
                "user": receiver,
                "conn_id": conn.id
            })

    return templates.TemplateResponse(request=request, name="my_connections.html", context={
        "user": user,
        "connected_users": connected_users,
        "pending_requests": pending_requests,
        "sent_requests": sent_requests,
    })


@app.get("/connection/{conn_id}/chat", response_class=HTMLResponse)
async def student_chat_page(conn_id: int, request: Request, db: Session = Depends(get_db)):
    """Private chat page between connected students."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    # Verify connection exists and is accepted
    conn = db.query(models.StudentConnection).filter(
        models.StudentConnection.id == conn_id,
        ((models.StudentConnection.requester_id == user.id) | (models.StudentConnection.receiver_id == user.id)),
        models.StudentConnection.status == "accepted"
    ).first()

    if not conn:
        raise HTTPException(status_code=403, detail="Not connected or unauthorized")

    other_id = conn.receiver_id if conn.requester_id == user.id else conn.requester_id
    other_user = db.query(models.User).filter(models.User.id == other_id).first()

    # Get message history
    messages = db.query(models.StudentMessage).filter(
        ((models.StudentMessage.sender_id == user.id) & (models.StudentMessage.receiver_id == other_id)) |
        ((models.StudentMessage.sender_id == other_id) & (models.StudentMessage.receiver_id == user.id))
    ).order_by(models.StudentMessage.timestamp.asc()).all()

    # Mark as read
    db.query(models.StudentMessage).filter(
        models.StudentMessage.receiver_id == user.id,
        models.StudentMessage.sender_id == other_id,
        models.StudentMessage.is_read == False
    ).update({models.StudentMessage.is_read: True})
    db.commit()

    return templates.TemplateResponse(request=request, name="student_chat.html", context={
        "user": user,
        "other_user": other_user,
        "messages": messages,
        "conn_id": conn_id
    })


@app.post("/connection/{conn_id}/chat/send")
async def send_student_message(
    conn_id: int, 
    request: Request, 
    content: str = Form(None),
    file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db)
):
    """Send a private message to a connected student."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    if user.is_suspended:
        return RedirectResponse(url="/suspended", status_code=status.HTTP_302_FOUND)

    # If JSON request (XHR)
    if request.headers.get("content-type") == "application/json":
        data = await request.json()
        content = data.get("content")

    if not content:
        raise HTTPException(status_code=400, detail="Message content is required")

    # Verify connection
    conn = db.query(models.StudentConnection).filter(
        models.StudentConnection.id == conn_id,
        ((models.StudentConnection.requester_id == user.id) | (models.StudentConnection.receiver_id == user.id)),
        models.StudentConnection.status == "accepted"
    ).first()

    if not conn:
        raise HTTPException(status_code=403, detail="Not connected or unauthorized")

    receiver_id = conn.receiver_id if conn.requester_id == user.id else conn.requester_id

    # Moderation Check
    is_flagged, reason = await check_content_moderation(content)
    if is_flagged:
        flag = models.ModerationFlag(user_id=user.id, content=content, chat_type="p2p", status="pending_review")
        db.add(flag)
        db.commit()
        # For P2P we return an error state
        if request.headers.get("content-type") == "application/json":
            return {"error": "Your message was flagged as inappropriate. Repeated violations will lead to account suspension."}
        return RedirectResponse(url=f"/connection/{conn_id}/chat?error=flagged", status_code=status.HTTP_302_FOUND)

    attachment_path = None
    attachment_type = None

    if file:
        try:
            upload_dir = os.path.join(BASE_DIR, "static", "uploads", "chat")
            os.makedirs(upload_dir, exist_ok=True)
            
            file_ext = os.path.splitext(file.filename)[1]
            unique_filename = f"{uuid.uuid4()}{file_ext}"
            file_path = os.path.join(upload_dir, unique_filename)
            
            with open(file_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
            
            attachment_path = f"/static/uploads/chat/{unique_filename}"
            attachment_type = "image" if file.content_type.startswith("image") else "file"
            
            # If no content, use filename
            if not content:
                content = file.filename
        except Exception as e:
            print(f"File upload error: {e}")

    new_msg = models.StudentMessage(
        sender_id=user.id,
        receiver_id=receiver_id,
        content=content,
        attachment_path=attachment_path,
        attachment_type=attachment_type
    )
    db.add(new_msg)
    db.commit()

    if request.headers.get("content-type") == "application/json":
        return {"success": True, "message_id": new_msg.id}

    return RedirectResponse(url=f"/connection/{conn_id}/chat", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/connection/{conn_id}/chat/messages")
async def get_student_messages(conn_id: int, request: Request, after_id: int = 0, db: Session = Depends(get_db)):
    """Fetch new messages for real-time polling."""
    user = get_current_user(request, db)
    if not user:
        return {"error": "Unauthorized"}

    conn = db.query(models.StudentConnection).filter(
        models.StudentConnection.id == conn_id,
        ((models.StudentConnection.requester_id == user.id) | (models.StudentConnection.receiver_id == user.id)),
        models.StudentConnection.status == "accepted"
    ).first()

    if not conn:
        return {"error": "Forbidden"}

    other_id = conn.receiver_id if conn.requester_id == user.id else conn.requester_id

    messages = db.query(models.StudentMessage).filter(
        ((models.StudentMessage.sender_id == user.id) & (models.StudentMessage.receiver_id == other_id)) |
        ((models.StudentMessage.sender_id == other_id) & (models.StudentMessage.receiver_id == user.id)),
        models.StudentMessage.id > after_id
    ).order_by(models.StudentMessage.timestamp.asc()).all()

    return {
        "messages": [
            {
                "id": m.id,
                "sender_id": m.sender_id,
                "content": m.content,
                "attachment_path": m.attachment_path,
                "attachment_type": m.attachment_type,
                "timestamp": m.timestamp.isoformat()
            }
            for m in messages
        ]
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    return templates.TemplateResponse(request=request, name="privacy.html", context={"user": user})

@app.get("/terms", response_class=HTMLResponse)
async def terms_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    return templates.TemplateResponse(request=request, name="terms.html", context={"user": user})

@app.get("/debug/migrate")
async def debug_migrate(request: Request, db: Session = Depends(get_db)):
    """Manually trigger migrations and return status."""
    try:
        run_migrations()
        return {"status": "success", "message": "Migrations triggered. Check console/logs for details."}
    except Exception as e:
        import traceback
        return {"status": "error", "message": str(e), "traceback": traceback.format_exc()}
