from sqlalchemy import Column, Integer, String, Boolean, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.sql import func

Base = declarative_base()

class ErrorLog(Base):
    __tablename__ = "error_logs"
    id = Column(Integer, primary_key=True, index=True)
    url = Column(String, nullable=False)
    error_message = Column(String, nullable=False)
    dynamic = Column(Boolean, default=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())
