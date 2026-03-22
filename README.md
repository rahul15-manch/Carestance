<div align="center">

# ✨CareStance✨

<img src="https://github.com/Yuvneet22/NEXTSTEP/blob/main/app/static/images/image.png" width="200"/>
</div>

An AI-powered career assessment and guidance platform built with FastAPI. Designed for students (Class 10th, 12th, and Above) to discover their personality archetype, explore career streams, and get personalized guidance through a multi-phase assessment and an AI chatbot.

---

## Features

- **Multi-Phase Assessment**: 4-phase structured assessment pipeline:
  - **Phase 1** – Class/Grade Selection (10th, 12th, Above 12th)
  - **Phase 2** – AI Personality Archetype Quiz (10 visual questions → 6 archetypes)
  - **Phase 3** – In-depth Scenario Analysis tailored to the user's archetype
  - **Phase 4** – Final Stream/Career Assessment with AI-powered recommendations
- **AI-Powered Analysis**: Dual AI provider system using **Google Gemini** (primary) with automatic fallback to **Groq (Llama 3.3-70B)**
- **AI Career Chatbot**: Personalized career counseling chatbot (`CareStance AI`) with token-by-token streaming and conversation history
- **Counsellor Booking**: Integrated booking system with **Razorpay** payment gateway for professional sessions
- **Live Consultations**: Real-time video calls via **Jitsi Meet** with automatic status tracking
- **Live Notifications**: Instant "Online" badge and animated join alerts when a counsellor joins the call
- **Support Ticket System**: Direct communication channel for students to raise queries and receive admin responses
- **AI Response Caching**: Integrated **Redis** caching for all LLM responses (Gemini/Groq) to provide instant load times and reduce API costs
- **Admin Dashboard**: Enhanced dashboard for user management, feedback review, and ticket resolution (Reply/Close/Delete)
- **User Authentication**: Secure signup/login with bcrypt hashing and  Google Sign-In support using GoogleOAuth

---

## System Architecture

```mermaid
graph TD
    subgraph "Client Layer (Frontend)"
        Student["Student Interface<br/>(Dashboard, Assessment, Chatbot)"]
        Admin["Admin Interface<br/>(User Mgmt, Tickets, Feedback)"]
        Counsellor["Counsellor Interface<br/>(Schedule Mgmt)"]
    end

    subgraph "Application Layer (FastAPI)"
        API["FastAPI App Logic<br/>(main.py)"]
        AUTH["Auth Module<br/>(Bcrypt, Sessions)"]
        AI_ENG["AI Engine<br/>(Gemini/Groq Fallback)"]
        PAY_MOD["Payment Module<br/>(Razorpay SDK)"]
    end

    subgraph "Data Layer"
        DB[("SQLite Database<br/>(SQLAlchemy ORM)")]
    end

    subgraph "External Integrations"
        Gemini["Google Gemini AI<br/>(Primary Analysis)"]
        Groq["Groq / Llama 3<br/>(Fallback AI)"]
        Razorpay["Razorpay API<br/>(Payments)"]
        Jitsi["Jitsi Meet<br/>(Video Consultations)"]
    end

    %% Connections
    Student --> API
    Admin --> API
    Counsellor --> API
    
    API <--> AUTH
    API <--> AI_ENG
    API <--> PAY_MOD
    API <--> DB

    AI_ENG <--> Gemini
    AI_ENG <--> Groq
    AI_ENG <--> REDIS[("Redis Cache")]
    
    PAY_MOD <--> Razorpay
    
    Student <--> Jitsi
    Counsellor <--> Jitsi

    %% Data Interactions
    DB --- Students["User Profiles"]
    DB --- Assessments["Assessment Results"]
    DB --- Appts["Appointments"]
    DB --- Tickets["Support Tickets"]
    DB --- Reviews["Feedback & Ratings"]
```

## Tech Stack

| Layer      | Technology                                     |
|------------|------------------------------------------------|
| Backend    | FastAPI, Uvicorn                               |
| Templating | Jinja2                                         |
| Database   | SQLite + SQLAlchemy ORM                        |
| Payments   | Razorpay SDK                                   |
| Video      | Jitsi Meet API                                 |
| AI (Primary) | Google Gemini (`gemini-flash-latest`)        |
| AI (Fallback) | Groq API (`llama-3.3-70b-versatile`)        |
| Caching    | Redis                                          |
| Auth       | bcrypt, Cookie-based sessions                  |
| Frontend   | HTML, Vanilla CSS, JavaScript (Tailwind CDN)   |

---

## Project Structure

```
CARESTANCE/
├── app/                          # Main application package
│   ├── __init__.py
│   ├── main.py                   # FastAPI app & all routes
│   ├── models.py                 # SQLAlchemy database models
│   ├── database.py               # DB engine & session
│   ├── static/                   # Static files (images, CSS, JS)
│   │   └── images/
│   │       └── assessment/       # Assessment option images (q1_a.png, etc.)
│   └── templates/                # Jinja2 HTML templates
│       ├── base.html
│       ├── login.html
│       ├── signup.html
│       ├── dashboard.html        # Student dashboard (Live Status)
│       ├── admin_dashboard.html  # Admin Panel (Ticket Management)
│       ├── counsellor_dashboard.html # Counsellor schedule management
│       ├── counsellors_list.html  # Booking interface
│       ├── ticket.html           # Support query submission
│       ├── feedback.html         # User feedback collection
│       └── ...                   # Assessment & Auth templates
├── data/                         # Question data modules
│   ├── __init__.py
│   ├── questions_data.py         # Phase 2 archetype questions
│   ├── questions_phase3.py       # Phase 3 scenario map (by archetype)
│   ├── questions_final.py        # Phase 4 questions (Class 10: 4 sections)
│   ├── questions_12th.py         # Phase 4 questions for Class 12th
│   └── questions_above_12th.py   # Phase 4 questions for Above 12th
├── scripts/                      # Utility & Migration scripts
│   ├── list_users.py             # List all registered users
│   ├── manage_test_data.py       # Seed/clean test data
│   ├── migrate_db_v2.py          # DB schema migration v2
│   ├── migrate_db_v5.py          # DB schema migration v5
│   ├── rename_images.py          # Rename assessment images
│   └── verify_classification.py  # Verify AI category classification
├── learnloop.db                  # SQLite database
├── requirements.txt              # Python dependencies
└── run.py                        # Application entry point
```

---

## Installation

### 1. Clone & Set Up
```bash
git clone https://github.com/Yuvneet22/CareStance.git
cd CareStance
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Environment Configuration
Create a `.env` file in the root directory:
```env
GEMINI_API_KEY=your-gemini-key
GROQ_API_KEY=your-groq-key
RAZORPAY_KEY_ID=your-razorpay-id
RAZORPAY_KEY_SECRET=your-razorpay-secret
ADMIN_EMAIL=admin@example.com
SECRET_KEY=your-random-secret
REDIS_URL=redis://default:password@host:port
```

### 3. Launch
```bash
python run.py
```
App URL: **`http://127.0.0.1:8000`**

---

## Usage

### Student Flow
| Action | Route |
|--------|-------|
| Assessment | `/assessment/start` | 4-phase career discovery pipeline |
| Support | `/ticket` | Raise query to administrators |
| Booking | `/counsellors` | Select and book expert sessions |
| Dashboard | `/dashboard` | View results & live meeting status |

### Admin Flow
| Action | Route |
|--------|-------|
| Management | `/admin` | Manage users, feedback, and tickets |
| Tickets | `/admin` | Reply to, resolve, or delete queries |

---

## Assessment Pipeline

```
Phase 1 ──► Phase 2 ──────────────────► Phase 3 ──────────────► Phase 4
(Class)     (10 Visual Q → Archetype)   (Scenario Analysis)     (Stream/Career)
                    │                           │                       │
                    ▼                           ▼                       ▼
              Google Gemini           Gemini / Groq Fallback    Gemini / Groq Fallback
              Classification         Work Style Analysis        Stream + AI Report
```

**6 Personality Archetypes**: Focused Specialist, Quiet Explorer, Strategic Builder, Adaptive Explorer, Visionary Leader, Dynamic Generalist

**Class 10 Streams**: Science (PCM), Science (PCB), Commerce, Arts & Humanities, Vocational Studies

**Class 12 / Above 12th**: Top 3 career paths / professional roles identified by AI

---

## Database Models

| Model | description |
|-------|-----------|
| `User` | Profile & Role (Student/Counsellor/Admin) |
| `AssessmentResult` | Multi-phase analysis & AI recommendations |
| `Appointment` | Schedule, counsellor details, & join tracking |
| `Ticket` | Support queries with admin reply history |
| `Feedback` | Ratings and qualitative user comments |

---

## Utility Scripts

Run from the project root:

```bash
# List all registered users
python scripts/list_users.py

# Seed or clean test data
python scripts/manage_test_data.py

# Run database migration (v2)
python scripts/migrate_db_v2.py

# Run database migration (v5)
python scripts/migrate_db_v5.py

# Verify AI classification output
python scripts/verify_classification.py

# Rename assessment images
python scripts/rename_images.py
```

---

## AI Fallback Strategy

The app uses a **dual-provider AI system** to maximize uptime:

1. **Primary**: Google Gemini (`gemini-flash-latest`) — classification, analysis, chatbot
2. **Fallback**: Groq API (`llama-3.3-70b-versatile`) — activates if Gemini call fails

All AI responses are cleaned with robust JSON extraction (handles markdown blocks, trailing commas, etc.)

---

## Contributing
1. Fork the repository
2. Create feature branch: `git checkout -b feature/refinement`
3. Commit and push: `git push origin feature/refinement`
4. Open a Pull Request

---

## License
MIT License - 2026 CareStance Team

---

## Support

For issues, questions, or suggestions, please open an issue on GitHub.
