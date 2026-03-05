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
from . import models
from .email_utils import send_email, get_booking_template, get_cancellation_template, get_reset_password_template
from itsdangerous import URLSafeTimedSerializer
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

async def generate_content_with_fallback(prompt):
    """
    Attempts to generate content using Gemini (Async) with high-tier fallback to Groq.
    Enhanced with robust regex for cleaner JSON extraction.
    """
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
        return text
    except Exception:
        return text

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
        
        # Users table migrations
        user_cols = get_columns('users')
        if user_cols and 'profile_photo' not in user_cols:
            migrations.append("ALTER TABLE users ADD COLUMN profile_photo VARCHAR")
        
        # CounsellorProfile table migrations
        cp_cols = get_columns('counsellor_profiles')
        if cp_cols:
            if 'tnc_accepted' not in cp_cols:
                migrations.append("ALTER TABLE counsellor_profiles ADD COLUMN tnc_accepted BOOLEAN DEFAULT FALSE")
            if 'tnc_accepted_at' not in cp_cols:
                migrations.append("ALTER TABLE counsellor_profiles ADD COLUMN tnc_accepted_at TIMESTAMP")
            if 'is_blocked' not in cp_cols:
                migrations.append("ALTER TABLE counsellor_profiles ADD COLUMN is_blocked BOOLEAN DEFAULT FALSE")
            if 'block_reason' not in cp_cols:
                migrations.append("ALTER TABLE counsellor_profiles ADD COLUMN block_reason VARCHAR")
            if 'certificates' not in cp_cols:
                migrations.append("ALTER TABLE counsellor_profiles ADD COLUMN certificates TEXT")
            if 'experience' not in cp_cols:
                migrations.append("ALTER TABLE counsellor_profiles ADD COLUMN experience TEXT")
            if 'is_verified' not in cp_cols:
                migrations.append("ALTER TABLE counsellor_profiles ADD COLUMN is_verified BOOLEAN DEFAULT FALSE")
            if 'verification_status' not in cp_cols:
                migrations.append("ALTER TABLE counsellor_profiles ADD COLUMN verification_status VARCHAR DEFAULT 'pending'")
        
        if migrations:
            with engine.connect() as conn:
                for sql in migrations:
                    try:
                        conn.execute(text(sql))
                        print(f"Migration OK: {sql}")
                    except Exception as me:
                        print(f"Migration skip: {me}")
                conn.commit()
            print(f"Ran {len(migrations)} migrations successfully")
        else:
            print("No migrations needed")
    except Exception as e:
        print(f"Migration check error: {e}")

run_migrations()

app = FastAPI(title="CareStance")

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

# Mount Static & Templates
# Mount Static & Templates
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
templates.env.globals["RAZORPAY_KEY_ID"] = RAZORPAY_KEY_ID
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
    if not user_id:
        return None
    user = db.query(models.User).filter(models.User.id == int(user_id)).first()
    return user

# Routes

@app.get("/", response_class=HTMLResponse)
async def home(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    return templates.TemplateResponse("landing.html", {"request": request, "user": user})

@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    return templates.TemplateResponse("signup.html", {"request": request})

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
        return templates.TemplateResponse("signup.html", {"request": request, "error": "Email already exists"})
    
    # Create User
    hashed_pw = get_password_hash(password)
    new_user = models.User(email=email, hashed_password=hashed_pw, full_name=full_name, contact_number=contact_number, role=role)
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    
    # Create Counsellor Profile
    if role == "counsellor":
        c_profile = models.CounsellorProfile(user_id=new_user.id)
        db.add(c_profile)
        db.commit()
    
    # Login & Redirect
    # Redirect to Login (No auto-login)
    return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    user = db.query(models.User).filter(models.User.email == email).first()
    if not user or not verify_password(password, user.hashed_password):
         return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid credentials"})
    
    response = RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    response.set_cookie(key="user_id", value=str(user.id))
    return response

@app.get("/logout")
async def logout(response: Response):
    response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    response.delete_cookie("user_id")
    return response

@app.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    return templates.TemplateResponse("forgot_password.html", {"request": request})

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
    return templates.TemplateResponse("forgot_password.html", {
        "request": request, 
        "message": "If an account exists with that email, a reset link has been sent."
    })

@app.get("/reset-password/{token}", response_class=HTMLResponse)
async def reset_password_page(request: Request, token: str):
    try:
        # Token valid for 1 hour (3600 seconds)
        email = serializer.loads(token, salt="password-reset-salt", max_age=3600)
    except Exception:
        return templates.TemplateResponse("forgot_password.html", {
            "request": request,
            "error": "The reset link is invalid or has expired."
        })
    return templates.TemplateResponse("reset_password.html", {"request": request, "token": token})

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
         return templates.TemplateResponse("forgot_password.html", {
            "request": request,
            "error": "The reset link is invalid or has expired."
        })
    
    if password != confirm_password:
        return templates.TemplateResponse("reset_password.html", {
            "request": request, 
            "token": token, 
            "error": "Passwords do not match"
        })
    
    user = db.query(models.User).filter(models.User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    user.hashed_password = get_password_hash(password)
    db.commit()
    
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
    
    # New users must select their role first
    redirect_url = "/select-role" if is_new_user else "/dashboard"
    response = RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)
    response.set_cookie(key="user_id", value=str(user.id))
    return response

@app.get("/select-role", response_class=HTMLResponse)
async def select_role_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    # If user already has a role, skip this page
    if user.role:
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse("select_role.html", {"request": request, "user": user})

@app.post("/select-role")
async def select_role(
    request: Request,
    role: str = Form(...),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    # Validate role
    if role not in ("student", "counsellor"):
        return RedirectResponse(url="/select-role", status_code=status.HTTP_302_FOUND)
    
    user.role = role
    db.commit()
    
    # Create counsellor profile if needed
    if role == "counsellor":
        existing_profile = db.query(models.CounsellorProfile).filter(models.CounsellorProfile.user_id == user.id).first()
        if not existing_profile:
            c_profile = models.CounsellorProfile(
                user_id=user.id,
                tnc_accepted=True,
                tnc_accepted_at=datetime.datetime.utcnow()
            )
            db.add(c_profile)
            db.commit()
        else:
            existing_profile.tnc_accepted = True
            existing_profile.tnc_accepted_at = datetime.datetime.utcnow()
            db.commit()
    
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
    
    # Check/Create Result
    result = db.query(models.AssessmentResult).filter(models.AssessmentResult.user_id == user.id).first()
    if not result:
        result = models.AssessmentResult(user_id=user.id)
        db.add(result)
    
    # Save Phase 1 Selection
    result.selected_class = class_level
    db.commit()
    
    # Proceed to Phase 2 (Archetype)
    return RedirectResponse(url="/assessment", status_code=status.HTTP_302_FOUND)


@app.get("/assessment", response_class=HTMLResponse)
async def assessment_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse("assessment.html", {"request": request, "user": user, "questions": QUESTIONS})

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
            clean_text = generate_content_with_fallback(prompt)
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

    return RedirectResponse(url="/assessment/result", status_code=status.HTTP_302_FOUND)

@app.get("/assessment/result", response_class=HTMLResponse)
async def assessment_result(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    result = db.query(models.AssessmentResult).filter(models.AssessmentResult.user_id == user.id).first()
    if not result:
        return RedirectResponse(url="/assessment", status_code=status.HTTP_302_FOUND)

    return templates.TemplateResponse("result.html", {"request": request, "user": user, "result": result})

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
        return templates.TemplateResponse("counsellor_dashboard.html", {"request": request, "user": user, "profile": profile, "appointments": appointments})
    
    # Fetch assessment result to show on dashboard
    assessment = db.query(models.AssessmentResult).filter(models.AssessmentResult.user_id == user.id).first()
    
    # Fetch student appointments (active only)
    appointments = db.query(models.Appointment).filter(
        models.Appointment.student_id == user.id,
        models.Appointment.status == "scheduled"
    ).all()
    
    # Fetch student tickets
    tickets = db.query(models.Ticket).filter(models.Ticket.user_id == user.id).order_by(models.Ticket.timestamp.desc()).all()
    
    return templates.TemplateResponse("dashboard.html", {
        "request": request, 
        "user": user, 
        "assessment": assessment,
        "appointments": appointments,
        "tickets": tickets
    })

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, db: Session = Depends(get_db)):
    try:
        user = get_current_user(request, db)
        if not user:
             return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
        
        admin_email = os.getenv("ADMIN_EMAIL")
        if user.role != "admin" and (not admin_email or user.email != admin_email):
            print(f"DEBUG: Admin access denied for {user.email}")
            return RedirectResponse(url="/dashboard?error=Admin access denied", status_code=status.HTTP_302_FOUND)

        all_users = db.query(models.User).all()
        all_feedback = db.query(models.Feedback).order_by(models.Feedback.timestamp.desc()).all()
        all_tickets = db.query(models.Ticket).order_by(models.Ticket.timestamp.desc()).all()
        pending_counsellors = db.query(models.CounsellorProfile).filter(models.CounsellorProfile.verification_status == "pending").all()
        
        # Fetch all counsellor profiles with session counts
        all_counsellors = db.query(models.CounsellorProfile).all()
        for cp in all_counsellors:
            try:
                cp.session_count = db.query(models.Appointment).filter(
                    models.Appointment.counsellor_id == cp.user_id,
                    models.Appointment.status == "completed"
                ).count()
                cp.total_sessions = db.query(models.Appointment).filter(
                    models.Appointment.counsellor_id == cp.user_id
                ).count()
            except Exception:
                cp.session_count = 0
                cp.total_sessions = 0
        
        return templates.TemplateResponse("admin_dashboard.html", {
            "request": request, 
            "user": user, 
            "users": all_users,
            "feedbacks": all_feedback,
            "tickets": all_tickets,
            "pending_counsellors": pending_counsellors,
            "all_counsellors": all_counsellors
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
    fee: float = Form(0.0),
    availability_text: str = Form(""),
    bank_name: str = Form(""),
    account_num: str = Form(""),
    ifsc_code: str = Form(""),
    upi_id: str = Form(""),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if not user or user.role != "counsellor":
         return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    account_data = {
        "bank_name": bank_name,
        "account_num": account_num,
        "ifsc": ifsc_code,
        "upi": upi_id
    }
    
    profile = db.query(models.CounsellorProfile).filter(models.CounsellorProfile.user_id == user.id).first()
    if profile:
        profile.fee = fee
        profile.availability = {"text": availability_text}
        profile.account_details = account_data
    else:
        profile = models.CounsellorProfile(
            user_id=user.id, 
            fee=fee, 
            availability={"text": availability_text},
            account_details=account_data
        )
        db.add(profile)
    
    db.commit()
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)

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
    experience: str = Form(...),
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if not user or user.role != "counsellor":
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    profile = db.query(models.CounsellorProfile).filter(models.CounsellorProfile.user_id == user.id).first()
    if not profile:
        profile = models.CounsellorProfile(user_id=user.id)
        db.add(profile)
    
    cert_paths = profile.certificates if profile.certificates else []
    
    # Ensure directory exists
    upload_dir = os.path.join(BASE_DIR, "static", "uploads", "certificates")
    try:
        os.makedirs(upload_dir, exist_ok=True)

        for file in files:
            if file.filename:
                file_extension = os.path.splitext(file.filename)[1]
                filename = f"cert_{user.id}_{uuid.uuid4().hex}{file_extension}"
                file_path = os.path.join(upload_dir, filename)
                
                contents = await file.read()
                with open(file_path, "wb") as buffer:
                    buffer.write(contents)
                
                cert_paths.append(f"/static/uploads/certificates/{filename}")
        
        # Ensure SQLAlchemy detects the list change
        profile.certificates = list(cert_paths)
        profile.experience = experience
        profile.verification_status = "pending"
        db.commit()
    except Exception as e:
        print(f"CERTIFICATE UPLOAD ERROR: {e}")
        return RedirectResponse(url="/dashboard?error=Certificate upload failed", status_code=status.HTTP_302_FOUND)
    
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)

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
            
    profile = db.query(models.CounsellorProfile).filter(models.CounsellorProfile.user_id == counsellor_id).first()
    if profile:
        profile.verification_status = verification_status
        profile.is_verified = (verification_status == "approved")
        db.commit()
    
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
    
    return templates.TemplateResponse("counsellors_list.html", {"request": request, "user": user, "counsellors": counsellors})

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
        "payment_capture": 1
    }
    
    try:
        order = razorpay_client.order.create(data=data)
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
    
    return templates.TemplateResponse("appointment_success.html", {"request": request, "user": user, "appointment": appointment})

@app.get("/join_meeting/{appointment_id}")
async def join_meeting(appointment_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    appointment = db.query(models.Appointment).filter(models.Appointment.id == appointment_id).first()
    if not appointment:
        raise HTTPException(status_code=404, detail="Appointment not found")
    
    # If the current user is the counsellor, mark as joined
    if user.id == appointment.counsellor_id:
        appointment.counsellor_joined = True
        appointment.joined_at = datetime.datetime.now()
        db.commit()
    
    return RedirectResponse(url=appointment.meeting_link)

@app.get("/appointment_status/{appointment_id}")
async def appointment_status(appointment_id: int, db: Session = Depends(get_db)):
    appointment = db.query(models.Appointment).filter(models.Appointment.id == appointment_id).first()
    if not appointment:
        return {"error": "Not found"}, 404
    return {
        "counsellor_joined": appointment.counsellor_joined,
        "joined_at": appointment.joined_at.isoformat() if appointment.joined_at else None
    }

@app.post("/appointment/delete/{appointment_id}")
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
        
    appointment = models.Appointment(
        student_id=user.id,
        counsellor_id=counsellor_id,
        appointment_time=appt_time,
        status="scheduled",
        payment_status="paid",
        meeting_link=meeting_link,
        razorpay_order_id=razorpay_order_id,
        razorpay_payment_id=razorpay_payment_id
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
            "Session Paid & Confirmed! 💰", 
            get_booking_template(counsellor_user.full_name, user.full_name, appt_time_str, meeting_link, "counsellor")
        )

    print(f"DEBUG: Appointment created successfully for student {user.id} and counsellor {counsellor_id}")
    
    return templates.TemplateResponse("appointment_success.html", {"request": request, "user": user, "appointment": appointment})

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
    
    return templates.TemplateResponse("assessment_phase3.html", {
        "request": request, 
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

    return templates.TemplateResponse("assessment_final.html", context)

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
                flat_questions.append({
                    "id": q["id"],
                    "title": q["question"],
                    "options": [{"value": o["value"], "text": o["text"]} for o in q["options"]],
                    "section": section_data["title"],
                    "type": "mcq"
                })
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
    
    return templates.TemplateResponse("chatbot.html", {"request": request, "user": user, "history": history})

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

    user_message = chat_req.message
    
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
    return templates.TemplateResponse("feedback.html", {"request": request, "user": user})

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
    return templates.TemplateResponse("ticket.html", {"request": request, "user": user})

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
    
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)


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
    You are an expert Career Architect.
    
    Student Profile:
    - Current Stage: {current_class}
    - Archetype: {archetype}
    - Personality: {personality}
    - Goal Career: {path_req.career_title}
    - Psychometric Analysis Bits: {phase3_insight[:300]}
    - Recommendation Context: {final_insight[:300]}

    TASK:
    Generate a highly realistic, comprehensive "Success Roadmap" from their current stage to a professional role in {path_req.career_title}.
    Provide 6 detailed steps.
    
    For EACH step, include:
    1. Action Name (Concise)
    2. Description (2-3 sentences of strategic advice)
    3. Skills to acquire (List 3 specific skills)
    4. Recommended Courses/Learning (List 2 specific platforms or certifications)
    5. Project Idea (1 practical project to build)
    6. Timeline (Estimated duration or month/year)

    Also provide:
    - Internships: 2 specific types of internship roles or companies to target.
    - Career Outlook:
        - Entry-level vs Senior Salary estimates (Range in INR or USD based on location).
        - Top 3 Companies known for hiring this role.
        - Future Scope: A 2-sentence outlook on growth potential and industry shifts.

    OUTPUT FORMAT (VALID JSON ONLY):
    {{
      "career_title": "{path_req.career_title}",
      "path_steps": [
        {{ 
          "step": 1, 
          "action": "Action Name", 
          "description": "Description...", 
          "skills": ["Skill 1", "Skill 2", "Skill 3"],
          "courses": ["Course 1", "Course 2"],
          "project": "Project idea...",
          "timeline": "e.g. 3-6 Months",
          "completed": false
        }},
        ...
      ],
      "internships": ["Internship 1", "Internship 2"],
      "career_outlook": {{
        "salary_range": "e.g. ₹5L - ₹25L+",
        "top_companies": ["Company 1", "Company 2", "Company 3"],
        "future_scope": "Scope details..."
      }},
      "reminders": [
        {{ "milestone": "Milestone Name", "reminder": "Specific alert/advice" }},
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
    if "steps" in data and 0 <= step_index < len(data["steps"]):
        # Toggle completed state
        is_completed = data["steps"][step_index].get("completed", False)
        data["steps"][step_index]["completed"] = not is_completed
        
        # Update progress percentage (optional but good)
        completed_count = sum(1 for s in data["steps"] if s.get("completed", False))
        data["progress_percentage"] = int((completed_count / len(data["steps"])) * 100)
    
    # Re-assign to trigger SQLAlchemy JSON detection
    path.path_data = dict(data)
    db.commit()
    
    return {"success": True, "completed": data["steps"][step_index]["completed"], "progress": data.get("progress_percentage", 0)}

@app.get("/career/roadmaps", response_class=HTMLResponse)
async def view_roadmaps(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    paths = db.query(models.CareerPath).filter(models.CareerPath.user_id == user.id).all()
    return templates.TemplateResponse("career_roadmaps.html", {"request": request, "user": user, "paths": paths})

@app.get("/career/roadmap/{path_id}", response_class=HTMLResponse)
async def view_roadmap_detail(path_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    
    path = db.query(models.CareerPath).filter(models.CareerPath.id == path_id, models.CareerPath.user_id == user.id).first()
    if not path:
        raise HTTPException(status_code=404, detail="Roadmap not found")
        
    return templates.TemplateResponse("career_roadmap_detail.html", {"request": request, "user": user, "path": path})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
