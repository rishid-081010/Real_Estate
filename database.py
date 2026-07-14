from datetime import datetime
from typing import Optional, List
from sqlmodel import Field, Session, SQLModel, create_engine, select

class Lead(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    bitrix_lead_id: Optional[str] = None
    name: Optional[str] = None
    phone: str = Field(index=True)
    email: Optional[str] = None
    source: Optional[str] = "unknown"
    channel: Optional[str] = "unknown"
    status: str = Field(default="new") # new -> engaging -> qualified/disqualified -> handed_off
    budget: Optional[str] = None
    timeline: Optional[str] = None
    property_type: Optional[str] = None
    location_pref: Optional[str] = None
    score: int = Field(default=0)
    assigned_agent: Optional[str] = None
    handoff_note: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class Message(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    lead_id: int = Field(foreign_key="lead.id")
    role: str # "user", "assistant", "system"
    content: str
    channel: str # e.g., "whatsapp", "voice", "form"
    created_at: datetime = Field(default_factory=datetime.utcnow)

class WebhookLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    payload: str

class Booking(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    lead_id: Optional[int] = Field(default=None, foreign_key="lead.id")
    username: str
    booking_type: str = "agent_call"  # "agent_call" or "site_visit"
    date: str  # DD/MM/YYYY
    time: str  # HH:MM (24h)
    gist: Optional[str] = None
    status: str = "confirmed"  # confirmed, completed, cancelled
    created_at: datetime = Field(default_factory=datetime.utcnow)

class CallLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    lead_id: Optional[int] = Field(default=None, foreign_key="lead.id")
    call_id: Optional[str] = None
    phone: Optional[str] = None
    lead_name: Optional[str] = None
    disposition: Optional[str] = None  # "answered", "no-answer", "busy", etc.
    duration_seconds: Optional[int] = None
    transcript: Optional[str] = None  # JSON string of [{role, content}, ...]
    summary: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

# Use SQLite for now (Render Postgres can be swapped in later)
sqlite_file_name = "database.sqlite"
sqlite_url = f"sqlite:///{sqlite_file_name}"

connect_args = {"check_same_thread": False}
engine = create_engine(sqlite_url, connect_args=connect_args)

def create_db_and_tables():
    SQLModel.metadata.create_all(engine)

def get_session():
    with Session(engine) as session:
        yield session
