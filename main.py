from contextlib import asynccontextmanager
import csv
import io
import os
import requests
from datetime import datetime, timedelta
from typing import Optional
from fastapi import FastAPI, Depends, UploadFile, File, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlmodel import Session, select
from pydantic import BaseModel
import json
from database import create_db_and_tables, get_session, Lead, Message, WebhookLog, Booking, CallLog
from qualification import generate_qualification_response, calculate_score

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Handle GCP Credentials on Render
    gcp_creds = os.environ.get("GCP_CREDENTIALS_JSON")
    if gcp_creds:
        creds_path = "/tmp/gcp_creds.json"
        # On Windows local, use a local temp path if /tmp doesn't exist
        if os.name == 'nt':
            creds_path = os.path.join(os.environ.get("TEMP", "."), "gcp_creds.json")
        
        with open(creds_path, "w") as f:
            f.write(gcp_creds)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds_path

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
    token = os.environ.get("WHATSAPP_ACCESS_TOKEN") or "EAAOih8fSxwIBRZCPrXu1NrPxfM6T1hvBlZCIHFmQS7umApRUZCCnAzyiyAywBbFWl11HzM4CG6USwYvYgPlcdtTeKJgIWwbPKcQp9db66SHMRD0RyE4jLQ1zlOZAsnoEaABfzgykK2ekE0EAZBCcQZBBG3dDd8sTGgQ0B6qv7exsrVS6ZBu2st9JlkZA1jBc2j8Y3QAWt20NsK3OcCv42FqHJEwXBMIh3S27u8HUdGfZAcf0ufhw5W0juAK5kuH8fPRZA0TAXqSCwxMvAfbnO14rTw3j427A0ZD"
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
        response = requests.post(url, headers=headers, json=payload)
        print("[WhatsApp Response]", response.text)
        
        # Also log to database so it can be debugged remotely
        from database import get_session, WebhookLog
        import json
        
        # We create a new session just for this logging to avoid conflicts
        try:
            from sqlmodel import Session, create_engine
            engine = create_engine("sqlite:///asquared.db")
            with Session(engine) as session:
                session.add(WebhookLog(payload=json.dumps({"meta_whatsapp_response": response.json()})))
                session.commit()
        except Exception as e:
            pass
            
        return response.status_code == 200
    except Exception as e:
        print("[WhatsApp API] Error:", str(e))
        return False

def send_whatsapp_template(to_phone: str, template_name: str = "hello_world"):
    token = os.environ.get("WHATSAPP_ACCESS_TOKEN") or "EAAOih8fSxwIBRZCPrXu1NrPxfM6T1hvBlZCIHFmQS7umApRUZCCnAzyiyAywBbFWl11HzM4CG6USwYvYgPlcdtTeKJgIWwbPKcQp9db66SHMRD0RyE4jLQ1zlOZAsnoEaABfzgykK2ekE0EAZBCcQZBBG3dDd8sTGgQ0B6qv7exsrVS6ZBu2st9JlkZA1jBc2j8Y3QAWt20NsK3OcCv42FqHJEwXBMIh3S27u8HUdGfZAcf0ufhw5W0juAK5kuH8fPRZA0TAXqSCwxMvAfbnO14rTw3j427A0ZD"
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
                                    
                                if qual_data.date and qual_data.date.lower() != "null" and qual_data.time and qual_data.time.lower() != "null":
                                    booking = Booking(
                                        lead_id=lead.id,
                                        username=f"{lead.name or 'Unknown'}_{lead.id}",
                                        booking_type="agent_call",
                                        date=qual_data.date,
                                        time=qual_data.time,
                                        gist=qual_data.gist
                                    )
                                    db.add(booking)
                                    
                                db.add(lead)
                                
                                # Save assistant message
                                asst_msg = Message(lead_id=lead.id, role="assistant", content=qual_data.reply, channel="whatsapp")
                                db.add(asst_msg)
                                db.commit()
                                
                                # 5. Send reply via Meta API
                                send_whatsapp_message(sender_phone, qual_data.reply)
                                
                            except Exception as e:
                                import traceback
                                error_trace = traceback.format_exc()
                                db.add(WebhookLog(payload=json.dumps({"error_in_gemini": str(e), "traceback": error_trace})))
                                db.commit()
                                print("[Gemini Error]", str(e))
                                
        return {"status": "ok"}
    return {"status": "error", "message": "Invalid payload"}

@app.get("/chat")
def serve_chat_ui():
    return FileResponse("static/chat.html")

def get_lead_by_username(username: str, db: Session):
    if "_" in username:
        try:
            lead_id = int(username.split("_")[-1])
            return db.exec(select(Lead).where(Lead.id == lead_id)).first()
        except ValueError:
            pass
    return None

@app.get("/api/chat/simulator/history")
def get_simulator_history(username: str, db: Session = Depends(get_session)):
    lead = get_lead_by_username(username, db)
    if not lead:
        return {"status": "error", "message": "User not found. Try format like 'Name_1'"}
        
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
    lead = get_lead_by_username(req.username, db)
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
            
        if qual_data.date and qual_data.date.lower() != "null" and qual_data.time and qual_data.time.lower() != "null":
            booking = Booking(
                lead_id=lead.id,
                username=f"{lead.name or 'Unknown'}_{lead.id}",
                booking_type="agent_call",
                date=qual_data.date,
                time=qual_data.time,
                gist=qual_data.gist
            )
            db.add(booking)
            
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

        # Save call transcript
        transcript_raw = call_info.get("transcript") or ""
        transcript_obj = call_info.get("transcript_object") or []
        call_duration = call_info.get("duration_ms")
        call_summary = call_info.get("call_analysis", {}).get("call_summary") if call_info.get("call_analysis") else None
        
        # Build structured transcript from transcript_object if available
        structured_transcript = []
        if transcript_obj:
            for turn in transcript_obj:
                role = turn.get("role", "unknown")
                content = turn.get("content", "")
                structured_transcript.append({"role": role, "content": content})
        elif transcript_raw:
            # Fallback: parse the plain text transcript
            lines = transcript_raw.strip().split("\n")
            for line in lines:
                if line.startswith("Agent:"):
                    structured_transcript.append({"role": "agent", "content": line[6:].strip()})
                elif line.startswith("User:"):
                    structured_transcript.append({"role": "user", "content": line[5:].strip()})
                else:
                    structured_transcript.append({"role": "system", "content": line.strip()})
        
        call_log = CallLog(
            lead_id=lead.id,
            call_id=call_info.get("call_id"),
            phone=phone,
            lead_name=lead.name,
            disposition=disposition,
            duration_seconds=int(call_duration / 1000) if call_duration else None,
            transcript=json.dumps(structured_transcript) if structured_transcript else None,
            summary=call_summary
        )
        db.add(call_log)
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

    # CASE C: Retell payload with call data containing a transcript (playground or alternate format)
    # e.g. { "call": { "call_id": "...", "call_status": "...", "transcript": "...", "transcript_object": [...] } }
    call_data = body.get("call")
    if call_data and (call_data.get("transcript") or call_data.get("transcript_object")):
        call_id = call_data.get("call_id")
        
        # Check if we already saved this call
        existing = db.exec(select(CallLog).where(CallLog.call_id == call_id)).first()
        
        transcript_obj = call_data.get("transcript_object") or []
        transcript_raw = call_data.get("transcript") or ""
        call_duration = call_data.get("duration_ms")
        call_summary = call_data.get("call_analysis", {}).get("call_summary") if call_data.get("call_analysis") else None
        disposition = call_data.get("disconnection_reason") or call_data.get("call_status") or "unknown"
        phone = call_data.get("from_number") or call_data.get("to_number") or "N/A"
        
        # Build structured transcript
        structured_transcript = []
        if transcript_obj:
            for turn in transcript_obj:
                role = turn.get("role", "unknown")
                content = turn.get("content", "")
                structured_transcript.append({"role": role, "content": content})
        elif transcript_raw:
            lines = transcript_raw.strip().split("\n")
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                if line.startswith("Agent:"):
                    structured_transcript.append({"role": "agent", "content": line[6:].strip()})
                elif line.startswith("User:"):
                    structured_transcript.append({"role": "user", "content": line[5:].strip()})
        
        if structured_transcript:
            # Try to find the lead
            lead = None
            if phone and phone != "N/A":
                lead = db.exec(select(Lead).where(Lead.phone == phone)).first()
            
            if existing:
                # Update existing record with latest transcript
                existing.transcript = json.dumps(structured_transcript)
                existing.disposition = disposition
                if call_duration:
                    existing.duration_seconds = int(call_duration / 1000)
                if call_summary:
                    existing.summary = call_summary
                db.add(existing)
            else:
                call_log = CallLog(
                    lead_id=lead.id if lead else None,
                    call_id=call_id,
                    phone=phone,
                    lead_name=lead.name if lead else call_data.get("agent_name", "Unknown"),
                    disposition=disposition,
                    duration_seconds=int(call_duration / 1000) if call_duration else None,
                    transcript=json.dumps(structured_transcript),
                    summary=call_summary
                )
                db.add(call_log)
            db.commit()
            return {"status": "success", "event": "transcript_saved"}

    return {"status": "ignored", "reason": "Unknown payload structure"}

# Backfill: Parse existing webhook logs and extract call transcripts into CallLog
@app.post("/api/calls/backfill")
def backfill_calls_from_webhooks(db: Session = Depends(get_session)):
    logs = db.exec(select(WebhookLog).order_by(WebhookLog.created_at)).all()
    backfilled = 0
    
    for log in logs:
        try:
            payload = json.loads(log.payload)
        except:
            continue
        
        # Check for call data with transcript
        call_data = None
        if payload.get("event") == "call_ended":
            call_data = payload.get("call", {})
        elif payload.get("call") and (payload["call"].get("transcript") or payload["call"].get("transcript_object")):
            call_data = payload["call"]
        
        if not call_data:
            continue
        
        call_id = call_data.get("call_id")
        if not call_id:
            continue
            
        # Skip if already backfilled
        existing = db.exec(select(CallLog).where(CallLog.call_id == call_id)).first()
        if existing:
            continue
        
        transcript_obj = call_data.get("transcript_object") or []
        transcript_raw = call_data.get("transcript") or ""
        
        structured_transcript = []
        if transcript_obj:
            for turn in transcript_obj:
                structured_transcript.append({"role": turn.get("role", "unknown"), "content": turn.get("content", "")})
        elif transcript_raw:
            for line in transcript_raw.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                if line.startswith("Agent:"):
                    structured_transcript.append({"role": "agent", "content": line[6:].strip()})
                elif line.startswith("User:"):
                    structured_transcript.append({"role": "user", "content": line[5:].strip()})
        
        if not structured_transcript:
            continue
        
        phone = call_data.get("from_number") or call_data.get("to_number") or "N/A"
        disposition = call_data.get("disposition") or call_data.get("disconnection_reason") or call_data.get("call_status") or "unknown"
        call_duration = call_data.get("duration_ms")
        call_summary = call_data.get("call_analysis", {}).get("call_summary") if call_data.get("call_analysis") else None
        
        lead = None
        if phone and phone != "N/A":
            lead = db.exec(select(Lead).where(Lead.phone == phone)).first()
        
        call_log = CallLog(
            lead_id=lead.id if lead else None,
            call_id=call_id,
            phone=phone,
            lead_name=lead.name if lead else call_data.get("agent_name", "Unknown"),
            disposition=disposition,
            duration_seconds=int(call_duration / 1000) if call_duration else None,
            transcript=json.dumps(structured_transcript),
            summary=call_summary,
            created_at=log.created_at
        )
        db.add(call_log)
        backfilled += 1
    
    db.commit()
    return {"status": "success", "backfilled": backfilled}

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

@app.get("/api/analytics")
def get_analytics(db: Session = Depends(get_session)):
    
    leads = db.exec(select(Lead)).all()
    bookings = db.exec(select(Booking)).all()
    messages = db.exec(select(Message)).all()
    
    total = len(leads)
    
    # Pipeline breakdown
    pipeline = {
        "Assigned Leads": 0,
        "No Answer": 0,
        "In Progress": 0,
        "Hot / Qualified": 0,
        "Ready To Buy": 0
    }
    status_map = {
        "fresh_leads": "Assigned Leads",
        "assigned_leads": "Assigned Leads",
        "no_answer": "No Answer",
        "in_progress": "In Progress",
        "qualified": "Hot / Qualified",
        "ready_to_buy": "Ready To Buy",
        "new": "Assigned Leads"
    }
    
    sources = {}
    channels = {}
    
    for lead in leads:
        bucket = status_map.get(lead.status, "Assigned Leads")
        pipeline[bucket] += 1
        
        src = lead.source or "unknown"
        sources[src] = sources.get(src, 0) + 1
        
        ch = lead.channel or "unknown"
        channels[ch] = channels.get(ch, 0) + 1
    
    # Daily lead intake (last 14 days)
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    daily_leads = {}
    for i in range(13, -1, -1):
        day = today - timedelta(days=i)
        day_str = day.strftime("%d %b")
        daily_leads[day_str] = 0
    
    for lead in leads:
        day_str = lead.created_at.strftime("%d %b")
        if day_str in daily_leads:
            daily_leads[day_str] += 1
    
    # Booking stats
    total_bookings = len(bookings)
    agent_calls = len([b for b in bookings if b.booking_type == "agent_call"])
    site_visits = len([b for b in bookings if b.booking_type == "site_visit"])
    
    # Message stats
    total_messages = len(messages)
    user_messages = len([m for m in messages if m.role == "user"])
    assistant_messages = len([m for m in messages if m.role == "assistant"])
    
    # Avg messages per lead
    leads_with_msgs = len(set(m.lead_id for m in messages))
    avg_msgs_per_lead = round(total_messages / leads_with_msgs, 1) if leads_with_msgs > 0 else 0
    
    # Qualification rate
    qualified_count = pipeline["Hot / Qualified"] + pipeline["Ready To Buy"]
    qualification_rate = round((qualified_count / total * 100) if total > 0 else 0, 1)
    
    # Response rate (leads that have at least 1 user message = they responded)
    responded_leads = len(set(m.lead_id for m in messages if m.role == "user"))
    response_rate = round((responded_leads / total * 100) if total > 0 else 0, 1)
    
    return {
        "total_leads": total,
        "pipeline": pipeline,
        "sources": sources,
        "channels": channels,
        "daily_leads": daily_leads,
        "bookings": {
            "total": total_bookings,
            "agent_calls": agent_calls,
            "site_visits": site_visits
        },
        "messages": {
            "total": total_messages,
            "user": user_messages,
            "assistant": assistant_messages,
            "avg_per_lead": avg_msgs_per_lead
        },
        "rates": {
            "qualification": qualification_rate,
            "response": response_rate
        }
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

@app.get("/api/debug-db")
def debug_db():
    try:
        from sqlmodel import create_engine
        import sqlite3
        conn = sqlite3.connect("asquared.db")
        cursor = conn.cursor()
        
        # Get list of tables
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [t[0] for t in cursor.fetchall()]
        
        db_info = {}
        for table in tables:
            # Get columns
            cursor.execute(f"PRAGMA table_info({table});")
            columns = [c[1] for c in cursor.fetchall()]
            
            # Get count
            cursor.execute(f"SELECT COUNT(*) FROM {table};")
            count = cursor.fetchone()[0]
            
            db_info[table] = {
                "columns": columns,
                "count": count
            }
            
        conn.close()
        return {"status": "ok", "tables": db_info}
    except Exception as e:
        import traceback
        return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

@app.get("/api/bookings")
def get_bookings(db: Session = Depends(get_session)):
    bookings = db.exec(select(Booking).order_by(Booking.created_at.desc())).all()
    return bookings

@app.get("/api/calls")
def get_calls(db: Session = Depends(get_session)):
    calls = db.exec(select(CallLog).order_by(CallLog.created_at.desc())).all()
    result = []
    for c in calls:
        result.append({
            "id": c.id,
            "lead_id": c.lead_id,
            "call_id": c.call_id,
            "phone": c.phone,
            "lead_name": c.lead_name,
            "disposition": c.disposition,
            "duration_seconds": c.duration_seconds,
            "transcript": json.loads(c.transcript) if c.transcript else [],
            "summary": c.summary,
            "created_at": c.created_at.isoformat() if c.created_at else None
        })
    return result

class ManualBookingRequest(BaseModel):
    username: str
    booking_type: str = "agent_call"
    date: str
    time: str
    gist: Optional[str] = "Manual Booking"

@app.post("/api/bookings")
def create_manual_booking(req: ManualBookingRequest, db: Session = Depends(get_session)):
    # Try to find the lead to link to
    lead = get_lead_by_username(req.username, db)
    
    booking = Booking(
        lead_id=lead.id if lead else None,
        username=req.username,
        booking_type=req.booking_type,
        date=req.date,
        time=req.time,
        gist=req.gist,
        status="confirmed"
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)
    return {"status": "success", "booking_id": booking.id}

@app.delete("/api/bookings/{booking_id}")
def delete_booking(booking_id: int, db: Session = Depends(get_session)):
    booking = db.exec(select(Booking).where(Booking.id == booking_id)).first()
    if not booking:
        return {"status": "error", "message": "Booking not found"}
    db.delete(booking)
    db.commit()
    return {"status": "success"}

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
