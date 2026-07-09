import json
import os
from typing import List, Optional
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

# Requires GOOGLE_APPLICATION_CREDENTIALS, PROJECT_ID, and LOCATION in environment variables
# For Vertex AI mode, we initialize the client with vertexai=True
# We assume the user has set these environment variables properly.
# e.g., os.environ["GEMINI_PROJECT_ID"], os.environ["GEMINI_LOCATION"]

def get_client():
    return genai.Client(
        vertexai=True,
        project=os.environ.get("GEMINI_PROJECT_ID"),
        location=os.environ.get("GEMINI_LOCATION", "us-central1")
    )

class QualificationData(BaseModel):
    reply: str = Field(description="The next natural, short, WhatsApp/voice-style reply to the user. One question at a time. Warm tone.")
    budget: Optional[str] = Field(description="Extracted budget if mentioned, else null")
    timeline: Optional[str] = Field(description="Extracted timeline if mentioned, else null")
    property_type: Optional[str] = Field(description="Extracted property type (e.g. villa, apartment) if mentioned, else null")
    location_pref: Optional[str] = Field(description="Extracted location preference if mentioned, else null")
    ready_for_handoff: bool = Field(description="True if budget, timeline, property_type, and location_pref are all confidently known")
    summary: Optional[str] = Field(description="A 1-2 sentence summary for the human agent, only provided if ready_for_handoff is true")

def load_knowledge_base():
    try:
        with open("knowledge_base.json", "r") as f:
            return f.read()
    except FileNotFoundError:
        return "[]"

SYSTEM_PROMPT_TEMPLATE = """You are an AI Sales Qualification Agent for Asquared Real Estate in Dubai.
Your goal is to have a natural, conversational chat (via WhatsApp/Voice) to qualify a lead.
Extract their budget, timeline, preferred property type, and location preference.
Only ask ONE question at a time. Keep replies very short and conversational.
Do not quote final prices, negotiate, or attempt to close. 
Once you have all 4 pieces of information, smoothly state that an agent will follow up to arrange a viewing or finalize details.

Here is the knowledge base of current listings you can reference if relevant:
{knowledge_base}
"""

def generate_qualification_response(chat_history: List[dict]) -> QualificationData:
    """
    chat_history: list of dicts with 'role' ('user' or 'model') and 'parts'
    """
    client = get_client()
    kb_str = load_knowledge_base()
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(knowledge_base=kb_str)
    
    # We use gemini-2.5-pro or gemini-2.5-flash for structured output
    model = "gemini-2.5-flash"
    
    # Format history for the SDK
    contents = []
    for msg in chat_history:
        role = "user" if msg["role"] == "user" else "model"
        contents.append(types.Content(role=role, parts=[types.Part.from_text(text=msg["content"])]))

    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        response_mime_type="application/json",
        response_schema=QualificationData,
        temperature=0.3
    )

    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=config,
    )
    
    data = json.loads(response.text)
    return QualificationData(**data)

def calculate_score(budget, timeline, property_type, location_pref) -> int:
    score = 0
    if budget: score += 30
    if timeline: score += 25
    if property_type: score += 20
    if location_pref: score += 25
    return score
