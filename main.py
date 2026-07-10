from contextlib import asynccontextmanager
import csv
import io
import os
import requests
from typing import Optional
from fastapi import FastAPI, Depends, UploadFile, File, Request
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

class LeadFormRequest(BaseModel):
    name: str
    phone: str
    email: Optional[str] = None
    source: Optional[str] = "form"

# Helper for sending WhatsApp message via Meta Cloud API
def send_whatsapp_message(to_phone: str, text: str):
    token = os.environ.get("WHATSAPP_ACCESS_TOKEN")
    phone_number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    
    if not token or not phone_number_id:
        print("[WhatsApp Mock] No credentials. Would send message to", to_phone, ":", text)
        return False
        
    url = f"https://graph.facebook.com/v17.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_phone,
        "type": "text",
        "text": {
            "body": text
        }
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers)
        print("[WhatsApp API] Status:", response.status_code, response.text)
        return response.status_code == 200
    except Exception as e:
        print("[WhatsApp API] Error:", str(e))
        return False

# --- Webhook endpoints ---

@app.post("/chat/test")
def chat_test(req: ChatRequest, db: Session = Depends(get_session)):
    # Find or create lead
    statement = select(Lead).where(Lead.phone == req.phone)
    lead = db.exec(statement).first()
    
    if not lead:
        lead = Lead(phone=req.phone, channel="test", status="fresh_leads")
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
    
    # Ensure lead status is updated correctly using the strict status bucket mapping
    status_map = {
        "Assigned Leads": "assigned_leads",
        "No Answer": "no_answer",
        "In Progress": "in_progress",
        "Hot / Qualified": "qualified",
        "Ready To Buy": "ready_to_buy"
    }
    
    if qual_data.status in status_map:
        lead.status = status_map[qual_data.status]
    else:
        # Fallback if LLM halluciantes status
        if lead.status == "fresh_leads":
            lead.status = "in_progress"
            
    if qual_data.gist:
        lead.handoff_note = qual_data.gist
        
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
            "gist": qual_data.gist
        }
    }

# Retell Call webhook & Custom Function handler
# Supports both:
# 1. Custom Tool Call payload: { "call_id": "...", "function_name": "...", "function_arguments": {...} }
# 2. General Call Ended Event: { "event": "call_ended", "call": { "call_id": "...", "disposition": "...", "from_number": "..." } }
@app.post("/webhook/retell")
async def retell_webhook(request: Request, db: Session = Depends(get_session)):
    body = await request.json()
    print("[Retell Webhook] Received payload:", body)

    # CASE A: Call Ended Event (e.g. tracking if they answered or not)
    if body.get("event") == "call_ended":
        call_info = body.get("call", {})
        phone = call_info.get("from_number") or call_info.get("to_number")
        disposition = call_info.get("disposition")

        if not phone:
            return {"status": "ignored", "reason": "No phone number found in call ended event"}

        # Find or create lead
        statement = select(Lead).where(Lead.phone == phone)
        lead = db.exec(statement).first()
        if not lead:
            lead = Lead(phone=phone, channel="voice", status="fresh_leads")
            db.add(lead)
            db.commit()
            db.refresh(lead)

        # Update status based on Retell call disposition
        if disposition in ["no-answer", "busy", "voicemail", "failed"]:
            lead.status = "no_answer"
            db.add(lead)
            db.commit()
            
            # MANDATORY FALLBACK: Auto trigger WhatsApp outreach
            fallback_text = (
                f"Hi {lead.name or 'there'}, we tried calling you regarding your property inquiry on {lead.source or 'Property Finder'}. "
                "Since we couldn't reach you, feel free to text us here to find the perfect property!"
            )
            send_whatsapp_message(lead.phone, fallback_text)
            
        elif lead.status == "fresh_leads" or lead.status == "assigned_leads":
            # If they picked up but we haven't qualified them yet
            lead.status = "in_progress"
            db.add(lead)
            db.commit()

        return {"status": "success", "event": "call_ended", "disposition": disposition}

    # CASE B: Custom Function Call from Voice Agent (mid-call or end-call extraction)
    elif "function_arguments" in body:
        args = body.get("function_arguments", {})
        phone = args.get("phone")
        if not phone:
            return {"status": "error", "message": "Phone number required"}

        # Find or create lead
        statement = select(Lead).where(Lead.phone == phone)
        lead = db.exec(statement).first()
        if not lead:
            lead = Lead(phone=phone, channel="voice", status="fresh_leads")
            db.add(lead)
            db.commit()
            db.refresh(lead)

        # Update extracted data
        # Update extracted data
        if args.get("budget"): lead.budget = args.get("budget")
        if args.get("timeline"): lead.timeline = args.get("timeline")
        if args.get("property_type"): lead.property_type = args.get("property_type")
        if args.get("location_pref"): lead.location_pref = args.get("location_pref")
        
        # Handle the new 'gist' argument
        gist = args.get("gist") or args.get("summary")
        if gist:
            lead.handoff_note = gist
            
        # Handle the strict 'status' bucket mapping
        ai_status = args.get("status")
        status_map = {
            "Assigned Leads": "assigned_leads",
            "No Answer": "no_answer",
            "In Progress": "in_progress",
            "Hot / Qualified": "qualified",
            "Ready To Buy": "ready_to_buy"
        }
        
        if ai_status and ai_status in status_map:
            lead.status = status_map[ai_status]
        else:
            # Fallback score-based logic if status wasn't provided perfectly
            lead.score = calculate_score(lead.budget, lead.timeline, lead.property_type, lead.location_pref)
            if lead.score >= 70 or args.get("ready_for_handoff"):
                lead.status = "qualified"
            else:
                lead.status = "in_progress"

        db.add(lead)
        db.commit()

        return {"status": "success", "message": "Data saved"}

    return {"status": "ignored", "reason": "Unknown payload structure"}

@app.post("/api/leads/form")
def create_lead_form(req: LeadFormRequest, db: Session = Depends(get_session)):
    statement = select(Lead).where(Lead.phone == req.phone)
    lead = db.exec(statement).first()
    if lead:
        return {"status": "error", "message": "Lead already exists"}
    
    lead = Lead(
        name=req.name,
        phone=req.phone,
        email=req.email,
        source=req.source,
        channel="form",
        status="fresh_leads"
    )
    db.add(lead)
    db.commit()
    db.refresh(lead)
    
    return {"status": "success", "lead_id": lead.id}

@app.post("/api/leads/batch")
async def create_leads_batch(file: UploadFile = File(...), db: Session = Depends(get_session)):
    content = await file.read()
    decoded = content.decode('utf-8')
    csv_reader = csv.DictReader(io.StringIO(decoded))
    
    created_count = 0
    skipped_count = 0
    
    for row in csv_reader:
        phone = row.get("phone")
        if not phone:
            continue
            
        statement = select(Lead).where(Lead.phone == phone)
        lead = db.exec(statement).first()
        if lead:
            skipped_count += 1
            continue
            
        lead = Lead(
            name=row.get("name"),
            phone=phone,
            email=row.get("email"),
            source=row.get("source", "batch_import"),
            channel="batch_import",
            status="fresh_leads"
        )
        db.add(lead)
        created_count += 1
        
    db.commit()
    return {"status": "success", "created": created_count, "skipped": skipped_count}

# --- Dashboard APIs ---

@app.get("/api/stats")
def get_dashboard_stats(db: Session = Depends(get_session)):
    leads = db.exec(select(Lead)).all()
    total_leads = len(leads)
    qualified_leads = len([l for l in leads if l.status in ["qualified", "ready_to_buy"]])
    conversion_rate = round((qualified_leads / total_leads * 100) if total_leads > 0 else 0, 1)
    meetings_booked = len([l for l in leads if l.status == "ready_to_buy"])
    
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
    meetings = db.exec(select(Lead).where(Lead.status.in_(["qualified", "ready_to_buy"]))).all()
    return meetings

@app.get("/health")
def health_check():
    return {"status": "ok"}

# Mount static files for dashboard frontend
import os
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def serve_dashboard():
    return FileResponse("static/index.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/form")
def serve_form():
    return FileResponse("static/form.html")
