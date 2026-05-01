from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

import os
from dotenv import load_dotenv

load_dotenv()

# Use DATABASE_URL for Vercel/Production, fallback to local SQLite
DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL:
    # SQLALchemy requires 'postgresql://' but many platforms provide 'postgres://'
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    SQLALCHEMY_DATABASE_URL = DATABASE_URL
else:
    if os.getenv("VERCEL"):
        SQLALCHEMY_DATABASE_URL = "sqlite:////tmp/learnloop.db"
    else:
        SQLALCHEMY_DATABASE_URL = "sqlite:///./learnloop.db"

engine_args = {
    "connect_args": {"check_same_thread": False} if SQLALCHEMY_DATABASE_URL.startswith("sqlite") else {}
}

# Fix for Supabase Transaction Mode/Poolers
if ("6543" in SQLALCHEMY_DATABASE_URL or "pooler" in SQLALCHEMY_DATABASE_URL or "supabase" in SQLALCHEMY_DATABASE_URL) and "prepare_threshold" not in SQLALCHEMY_DATABASE_URL:
    if "?" in SQLALCHEMY_DATABASE_URL:
        SQLALCHEMY_DATABASE_URL += "&prepare_threshold=0"
    else:
        SQLALCHEMY_DATABASE_URL += "?prepare_threshold=0"

if not SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    # Use NullPool to completely avoid connection exhaustion/limitations on Supabase or Serverless deployments
    from sqlalchemy.pool import NullPool
    engine_args["poolclass"] = NullPool

engine = create_engine(SQLALCHEMY_DATABASE_URL, **engine_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
