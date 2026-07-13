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
import json
from database import create_db_and_tables, get_session, Lead, Message, WebhookLog, Booking
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
import re

def sanitize_phone(phone: str) -> str:
    # Remove everything except digits
    return re.sub(r'\D', '', phone)

def send_whatsapp_message(to_phone: str, text: str):
    token = os.environ.get("WHATSAPP_ACCESS_TOKEN") or "EAAOih8fSxwIBR7l0jQ3X8bbnPekVHrr3GkcqHkmo5ZBFo7PY8ZCi1G3aOAa1HL7M7yZCG9MOW8M0yV98OBgUVqHMJI72JwWbdlsydLfJFJobZCVdT8wo3ACJbUx1R2xpRZA09xfgKXrsTQfb3RZBfQZATIyRmNTEGUD3lddMZATGfIrsS0m4N9JuPotNBYIjbPVUpC7v2IcnAcyhIZA6mabT75UUwOuckd4ZB7vIZCoeArTFr3MEzQliRCPD47c1AFPpdZB7dGOS7ZCQlqdBNZBfaRIlqBh3MyHAZDZD"
    phone_number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID") or "1237934332731206"
    
    clean_phone = sanitize_phone(to_phone)
    if not token or not phone_number_id:
        print("[WhatsApp Mock] No credentials. Would send message to", clean_phone, ":", text)
        return False
        
    url = f"https://graph.facebook.com/v17.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": clean_phone,
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

def send_whatsapp_template(to_phone: str, template_name: str = "hello_world"):
    token = os.environ.get("WHATSAPP_ACCESS_TOKEN") or "EAAOih8fSxwIBR7l0jQ3X8bbnPekVHrr3GkcqHkmo5ZBFo7PY8ZCi1G3aOAa1HL7M7yZCG9MOW8M0yV98OBgUVqHMJI72JwWbdlsydLfJFJobZCVdT8wo3ACJbUx1R2xpRZA09xfgKXrsTQfb3RZBfQZATIyRmNTEGUD3lddMZATGfIrsS0m4N9JuPotNBYIjbPVUpC7v2IcnAcyhIZA6mabT75UUwOuckd4ZB7vIZCoeArTFr3MEzQliRCPD47c1AFPpdZB7dGOS7ZCQlqdBNZBfaRIlqBh3MyHAZDZD"
    phone_number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID") or "1237934332731206"
    
    clean_phone = sanitize_phone(to_phone)
    if not token or not phone_number_id:
        return False
        
    url = f"https://graph.facebook.com/v17.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": clean_phone,
        "type": "template",
        "template": {
            "name": template_name,
            "language": { "code": "en_US" }
        }
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers)
        print("[WhatsApp Template API] Status:", response.status_code, response.text)
        return response.status_code == 200
    except Exception as e:
        print("[WhatsApp Template API] Error:", str(e))
        return False

# --- Webhook endpoints ---

# 1. Meta Webhook Verification
@app.get("/webhook/whatsapp")
def verify_whatsapp_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode and token:
        if mode == "subscribe" and token == "asquared_whatsapp_secret":
            return int(challenge)
        return {"error": "Invalid token"}, 403
    return {"status": "ok"}

# 2. Receive Incoming WhatsApp Messages
@app.post("/webhook/whatsapp")
async def receive_whatsapp_message(request: Request, db: Session = Depends(get_session)):
    body = await request.json()
    db.add(WebhookLog(payload=json.dumps(body)))
    db.commit()

    if body.get("object"):
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                if "messages" in value:
                    for msg in value["messages"]:
                        sender_phone = msg.get("from")
                        text_body = msg.get("text", {}).get("body")
                        
                        if text_body and sender_phone:
                            # 1. Find or create lead
                            lead = db.exec(select(Lead).where(Lead.phone == sender_phone)).first()
                            if not lead:
                                lead = Lead(phone=sender_phone, name="WhatsApp User", channel="whatsapp", status="in_progress")
                                db.add(lead)
                            else:
                                # Auto-shift to in_progress if they were previously unengaged
                                if lead.status in ["no_answer", "fresh_leads", "assigned_leads"]:
                                    lead.status = "in_progress"
                                    db.add(lead)

                            db.commit()
                            db.refresh(lead)

                            # 2. Save user message
                            user_msg = Message(lead_id=lead.id, role="user", content=text_body, channel="whatsapp")
                            db.add(user_msg)
                            db.commit()

                            # 3. Fetch chat history
                            history = db.exec(select(Message).where(Message.lead_id == lead.id).order_by(Message.created_at)).all()
                            chat_history = [{"role": m.role, "content": m.content} for m in history]

                            # 4. Generate Gemini response
                            from qualification import generate_whatsapp_response
                            try:
                                qual_data = generate_whatsapp_response(chat_history)
                                
                                # Update CRM fields based on new "change:" logic
                                if qual_data.status and qual_data.status.startswith("change:"):
                                    new_status = qual_data.status.replace("change:", "").strip()
                                    if "Hot" in new_status or "Qualified" in new_status:
                                        lead.status = "qualified"
                                    elif "Ready To Buy" in new_status:
                                        lead.status = "ready_to_buy"
                                
                                if qual_data.gist and qual_data.gist.lower() != "null": 
                                    lead.handoff_note = qual_data.gist
                                    
                                db.add(lead)
                                
                                # Save assistant message
                                asst_msg = Message(lead_id=lead.id, role="assistant", content=qual_data.reply, channel="whatsapp")
                                db.add(asst_msg)
                                db.commit()
                                
                                # 5. Send reply via Meta API
                                send_whatsapp_message(sender_phone, qual_data.reply)
                                
                            except Exception as e:
                                print("[Gemini Error]", str(e))
                                
        return {"status": "ok"}
    return {"status": "error", "message": "Invalid payload"}

@app.get("/chat")
def serve_chat_ui():
    return FileResponse("static/chat.html")

@app.get("/api/chat/simulator/history")
def get_simulator_history(username: str, db: Session = Depends(get_session)):
    lead = db.exec(select(Lead).where(Lead.username == username)).first()
    if not lead:
        return {"status": "error", "message": "User not found"}
        
    history = db.exec(select(Message).where(Message.lead_id == lead.id).order_by(Message.created_at)).all()
    chat_history = [{"role": m.role, "content": m.content, "timestamp": m.created_at.isoformat() if m.created_at else None} for m in history]
    
    # Simulate the "Hello World" fallback if they are in no_answer and have no history
    if lead.status == "no_answer" and len(chat_history) == 0:
        fallback_text = f"Hi {lead.name or 'there'}, we tried calling you regarding your property inquiry. Since we couldn't reach you, feel free to text us here to find the perfect property!"
        asst_msg = Message(lead_id=lead.id, role="assistant", content=fallback_text, channel="simulator")
        db.add(asst_msg)
        db.commit()
        db.refresh(asst_msg)
        chat_history.append({"role": "assistant", "content": fallback_text, "timestamp": asst_msg.created_at.isoformat() if asst_msg.created_at else None})
        
    return {"status": "success", "history": chat_history, "lead_status": lead.status}

class SimulatorRequest(BaseModel):
    username: str
    message: str

@app.post("/api/chat/simulator/send")
def send_simulator_message(req: SimulatorRequest, db: Session = Depends(get_session)):
    lead = db.exec(select(Lead).where(Lead.username == req.username)).first()
    if not lead:
        return {"status": "error", "message": "User not found"}

    # Auto-shift to in_progress if they were previously unengaged
    if lead.status in ["no_answer", "fresh_leads", "assigned_leads"]:
        lead.status = "in_progress"
        db.add(lead)
        db.commit()

    # Save user message
    user_msg = Message(lead_id=lead.id, role="user", content=req.message, channel="simulator")
    db.add(user_msg)
    db.commit()

    # Fetch chat history
    history = db.exec(select(Message).where(Message.lead_id == lead.id).order_by(Message.created_at)).all()
    chat_history = [{"role": m.role, "content": m.content} for m in history]

    # Generate Gemini response
    from qualification import generate_whatsapp_response
    try:
        qual_data = generate_whatsapp_response(chat_history)
        
        # Update CRM fields based on new "change:" logic
        if qual_data.status and qual_data.status.startswith("change:"):
            new_status = qual_data.status.replace("change:", "").strip()
            if "Hot" in new_status or "Qualified" in new_status:
                lead.status = "qualified"
            elif "Ready To Buy" in new_status:
                lead.status = "ready_to_buy"
        
        if qual_data.gist and qual_data.gist.lower() != "null": 
            lead.handoff_note = qual_data.gist
            
        db.add(lead)
        
        # Save assistant message
        asst_msg = Message(lead_id=lead.id, role="assistant", content=qual_data.reply, channel="simulator")
        db.add(asst_msg)
        db.commit()
        
        return {"status": "success", "reply": qual_data.reply}
    except Exception as e:
        return {"status": "error", "message": str(e)}

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
    
    # Save raw webhook log
    db.add(WebhookLog(payload=json.dumps(body)))
    db.commit()

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
            # Meta Sandbox requires the FIRST message to be a template like "hello_world"
            send_whatsapp_template(lead.phone, "hello_world")
            
        elif lead.status == "fresh_leads" or lead.status == "assigned_leads":
            # If they picked up but we haven't qualified them yet
            lead.status = "in_progress"
            db.add(lead)
            db.commit()

        return {"status": "success", "event": "call_ended", "disposition": disposition}

    # CASE B: Custom Function Call from Voice Agent (mid-call or end-call extraction)
    elif "args" in body or "function_arguments" in body:
        args = body.get("args") or body.get("function_arguments", {})
        username = args.get("username")
        phone = args.get("phone")
        
        lead = None
        
        # 1. Try to find lead by username (Format: Name_ID, e.g. "Rishi_1")
        if username and "_" in username:
            try:
                lead_id = int(username.split("_")[-1])
                lead = db.exec(select(Lead).where(Lead.id == lead_id)).first()
            except ValueError:
                pass
                
        # 2. Fallback to phone number if username lookup failed or was missing
        if not lead and phone:
            lead = db.exec(select(Lead).where(Lead.phone == phone)).first()
            
        # 3. Create new lead if still not found
        if not lead:
            if not phone and not username:
                return {"status": "error", "message": "Phone number or username required to create a new lead"}
            
            # Use 'Unknown' for phone if only username is provided
            lead = Lead(phone=phone or "Unknown", name=username or "Unknown User", username=username, channel="voice", status="fresh_leads")
            db.add(lead)
            db.commit()
            db.refresh(lead)

        function_name = body.get("name")
        if function_name == "not_answered":
            lead.status = "no_answer"
            db.add(lead)
            db.commit()
            
            # MANDATORY FALLBACK: Auto trigger WhatsApp outreach
            # Meta Sandbox requires the FIRST message to be a template like "hello_world"
            send_whatsapp_template(lead.phone, "hello_world")
            return {"status": "success", "event": "not_answered"}

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

        # Create a calendar booking if date and time were provided
        booking_date = args.get("date") or args.get("Date")
        booking_time = args.get("time") or args.get("Time")
        if booking_date and booking_time and booking_date != "N/A" and booking_time != "N/A":
            # Detect booking type from gist
            gist_lower = (gist or "").lower()
            if "site" in gist_lower or "visit" in gist_lower or "viewing" in gist_lower:
                booking_type = "site_visit"
            else:
                booking_type = "agent_call"
            
            booking = Booking(
                lead_id=lead.id,
                username=username or f"{lead.name or 'Unknown'}_{lead.id}",
                booking_type=booking_type,
                date=booking_date,
                time=booking_time,
                gist=gist
            )
            db.add(booking)
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
    meetings_booked = len(db.exec(select(Booking)).all())
    
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

@app.get("/api/logs/webhooks")
def get_webhook_logs(db: Session = Depends(get_session)):
    logs = db.exec(select(WebhookLog).order_by(WebhookLog.created_at.desc()).limit(50)).all()
    return logs

@app.get("/api/bookings")
def get_bookings(db: Session = Depends(get_session)):
    bookings = db.exec(select(Booking).order_by(Booking.created_at.desc())).all()
    return bookings

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
