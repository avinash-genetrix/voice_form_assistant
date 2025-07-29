from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# SQLite for local demo; switch to PostgreSQL in prod
DATABASE_URL = "sqlite:///./form_logs.db"

engine = create_engine(
    DATABASE_URL, connect_args={"check_same_thread": False}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
