from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlmodel import Session, select
from pydantic import BaseModel
from database import create_db_and_tables, get_session, Lead, Message
from qualification import generate_qualification_response, calculate_score

@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    yield

app = FastAPI(title="Asquared Real Estate AI Sales Agent", lifespan=lifespan)

class ChatRequest(BaseModel):
    phone: str
    message: str

class RetellWebhookRequest(BaseModel):
    call_id: str
    function_name: str
    function_arguments: dict

@app.post("/chat/test")
def chat_test(req: ChatRequest, db: Session = Depends(get_session)):
    # Find or create lead
    statement = select(Lead).where(Lead.phone == req.phone)
    lead = db.exec(statement).first()
    
    if not lead:
        lead = Lead(phone=req.phone, channel="test")
        db.add(lead)
        db.commit()
        db.refresh(lead)
        
    # Save user message
    user_msg = Message(lead_id=lead.id, role="user", content=req.message, channel="test")
    db.add(user_msg)
    db.commit()
    
    # Get chat history
    msgs_statement = select(Message).where(Message.lead_id == lead.id).order_by(Message.created_at)
    history_msgs = db.exec(msgs_statement).all()
    
    chat_history = [{"role": m.role, "content": m.content} for m in history_msgs]
    
    # Generate response via LLM
    try:
        qual_data = generate_qualification_response(chat_history)
    except Exception as e:
        return {"error": str(e)}
        
    # Update lead fields if new data was extracted
    if qual_data.budget: lead.budget = qual_data.budget
    if qual_data.timeline: lead.timeline = qual_data.timeline
    if qual_data.property_type: lead.property_type = qual_data.property_type
    if qual_data.location_pref: lead.location_pref = qual_data.location_pref
    
    # Calculate new score
    lead.score = calculate_score(lead.budget, lead.timeline, lead.property_type, lead.location_pref)
    
    # Ensure lead status is correctly advanced
    if lead.status == "new":
        lead.status = "engaging"
    
    if qual_data.ready_for_handoff and lead.status != "handed_off":
        lead.status = "qualified"
        lead.handoff_note = qual_data.summary
        
    db.add(lead)
    
    # Save assistant message
    asst_msg = Message(lead_id=lead.id, role="assistant", content=qual_data.reply, channel="test")
    db.add(asst_msg)
    db.commit()
    
    return {
        "reply": qual_data.reply,
        "lead_state": {
            "score": lead.score,
            "status": lead.status,
            "budget": lead.budget,
            "timeline": lead.timeline,
            "property_type": lead.property_type,
            "location_pref": lead.location_pref,
            "ready_for_handoff": qual_data.ready_for_handoff
        }
    }

# --- Dashboard APIs ---

@app.get("/api/stats")
def get_dashboard_stats(db: Session = Depends(get_session)):
    leads = db.exec(select(Lead)).all()
    total_leads = len(leads)
    qualified_leads = len([l for l in leads if l.status in ["qualified", "handed_off"]])
    conversion_rate = round((qualified_leads / total_leads * 100) if total_leads > 0 else 0, 1)
    meetings_booked = len([l for l in leads if l.status == "handed_off"])
    
    return {
        "total_leads": total_leads,
        "qualified_leads": qualified_leads,
        "conversion_rate": conversion_rate,
        "meetings_booked": meetings_booked
    }

@app.get("/api/leads")
def get_dashboard_leads(db: Session = Depends(get_session)):
    leads = db.exec(select(Lead).order_by(Lead.created_at.desc())).all()
    return leads

@app.get("/api/meetings")
def get_dashboard_meetings(db: Session = Depends(get_session)):
    # Simulating meetings for any lead that is handed_off or qualified
    meetings = db.exec(select(Lead).where(Lead.status.in_(["qualified", "handed_off"]))).all()
    return meetings

# --- Webhooks ---

@app.post("/webhook/retell")
def retell_webhook(req: RetellWebhookRequest, db: Session = Depends(get_session)):
    args = req.function_arguments
    phone = args.get("phone")
    if not phone:
        return {"status": "error", "message": "Phone number required"}

    # Find lead
    statement = select(Lead).where(Lead.phone == phone)
    lead = db.exec(statement).first()
    
    if not lead:
        # Create lead if not exists
        lead = Lead(phone=phone, channel="voice")
        db.add(lead)
        db.commit()
        db.refresh(lead)

    # Update extracted data
    if args.get("budget"): lead.budget = args.get("budget")
    if args.get("timeline"): lead.timeline = args.get("timeline")
    if args.get("property_type"): lead.property_type = args.get("property_type")
    if args.get("location_pref"): lead.location_pref = args.get("location_pref")
    
    lead.score = calculate_score(lead.budget, lead.timeline, lead.property_type, lead.location_pref)
    
    # Process handoff
    if lead.score >= 70 or args.get("ready_for_handoff"):
        lead.status = "qualified"
        if args.get("summary"):
            lead.handoff_note = args.get("summary")
    else:
        lead.status = "engaging"

    db.add(lead)
    db.commit()

    return {"status": "success", "message": "Data saved"}

@app.get("/health")
def health_check():
    return {"status": "ok"}

# Mount static files for dashboard frontend
import os
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def serve_dashboard():
    return FileResponse("static/index.html")
