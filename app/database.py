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

if not SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    # On Vercel (serverless), it's best to use very small pools or disable client-side pooling
    if os.getenv("VERCEL"):
        engine_args["pool_size"] = 2
        engine_args["max_overflow"] = 0
    else:
        engine_args["pool_size"] = 20
        engine_args["max_overflow"] = 40
        
    engine_args["pool_timeout"] = 30
    engine_args["pool_recycle"] = 1800
    engine_args["pool_pre_ping"] = True

engine = create_engine(SQLALCHEMY_DATABASE_URL, **engine_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
