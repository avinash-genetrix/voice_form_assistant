from fastapi import FastAPI, HTTPException, Depends, WebSocket, WebSocketDisconnect, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
from sqlalchemy.orm import Session
from google.cloud import texttospeech
from datetime import datetime
from typing import Dict
import logging, os, re, traceback, asyncio, audioop
from playwright.async_api import async_playwright
import inflect, re
from number_parser import parse_ordinal
from dateutil import parser
from gpt_integration import generate_questions, extract_answer_from_gpt, normalize_transcript
from stt import transcribe_streaming
from db import SessionLocal
from models import ErrorLog, Base
from email_utils import normalize_email, extract_possible_email, looks_like_email
from parser import extract_shadow_form, extract_normal_form, extract_fields_from_html

# Global form session state
session_state: Dict[str, list] = {}

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# CORS settings for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Auto-create DB tables if SQLite file doesn't exist
if not os.path.exists("form_logs.db"):
    Base.metadata.create_all(bind=SessionLocal().bind)

class URLRequest(BaseModel):
    url: HttpUrl
    dynamic: bool = True
    
# SQLAlchemy DB session dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# 🔊 Google Cloud TTS endpoint: converts text to speech using en-IN Wavenet-D voice
@app.post("/tts-audio")
async def tts_audio(request: Request):
    data = await request.json()
    text = data.get("text")
    if not text:
        return {"error": "No text provided"}
    client = texttospeech.TextToSpeechClient()
    synthesis_input = texttospeech.SynthesisInput(text=text)
    voice = texttospeech.VoiceSelectionParams(language_code="en-IN", name="en-IN-Wavenet-D")
    audio_config = texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3)
    response = client.synthesize_speech(input=synthesis_input, voice=voice, audio_config=audio_config)
    return Response(response.audio_content, media_type="audio/mpeg")

# 🔇 Detects if the last portion of the audio is silent based on RMS energy
# Used to decide if user has finished speaking in WebSocket STT
def detect_silence_at_end(audio_data: bytes, sample_rate=16000, silence_threshold=500, window_ms=300):
    window_size = int(sample_rate * (window_ms / 1000.0)) * 2  
    if len(audio_data) < window_size:
        return False 

    last_window = audio_data[-window_size:]
    rms = audioop.rms(last_window, 2)
    print(f"🔍 End-window RMS: {rms}")
    return rms < silence_threshold


# ⏰ Parses spoken time expressions like "3 pm", "14:00", "noon" into HH:MM 24-hour format
# Used when field type is "time"
def parse_spoken_time(text):
    # Lowercase and strip
    text = text.lower().strip()
    # Replace "in the morning"/"in the evening"/etc. for easier parsing
    text = text.replace("in the morning", "am").replace("in the evening", "pm").replace("at night", "pm").replace("noon", "12:00 pm")
    # Replace "hours" with ":00"
    text = re.sub(r"(\d{1,2})\s*hours?", r"\1:00", text)
    # Replace "am"/"pm" attached
    text = re.sub(r"(\d{1,2})\s*([ap]m)", r"\1:00 \2", text)
    # Convert to a known datetime format
    patterns = [
        "%I %p",         # 7 pm
        "%I:%M %p",      # 7:30 pm
        "%H:%M",         # 15:30
        "%I",            # 7
        "%H",            # 21
        "%I%p",          # 7pm
        "%H%M",          # 1530
    ]
    for pat in patterns:
        try:
            dt = datetime.strptime(text, pat)
            return dt.strftime("%H:%M")
        except Exception:
            pass
    # Try extracting numbers and guessing
    match = re.search(r'(\d{1,2})[:. ]?(\d{2})?\s*([ap]m)?', text)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2)) if match.group(2) else 0
        ampm = match.group(3)
        if ampm == "pm" and hour < 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        return f"{hour:02}:{minute:02}"
    return ""  # fallback


p = inflect.engine()

# 🧹 Cleans a date string by removing dots and extra spaces.
# Example: "12. July  2023" → "12 July 2023"
def clean_date_str(s):
    return re.sub(r'[.]', '', s).replace('  ', ' ').strip()


# 🔢 Replaces ordinal words like 'first', 'second', 'twenty-third' with numeric values (1, 2, 23)
# This helps the parser understand spoken dates like "twenty fifth July"
def replace_ordinals(text):
    words = text.lower().replace('-', ' ').split()
    new_words = []
    for word in words:
        try:
            num = parse_ordinal(word)
            new_words.append(str(num))
        except Exception:
            new_words.append(word)
    return ' '.join(new_words)

# 📅 Parses a fuzzy, spoken-style date string (e.g., "fifth of July") into ISO format YYYY-MM-DD
# Returns "" if parsing fails
def parse_spoken_date(text):
    try:
        dt = parser.parse(text, fuzzy=True, dayfirst=True)
        return dt.strftime('%Y-%m-%d')
    except Exception as e:
        print("Date parse failed:", text, e)
        return ""

# 🔁 Matches user transcript with one of the predefined options (radio/checkbox)
# Returns the matched option string if found
def match_spoken_option(transcript, options):
    transcript = transcript.lower().strip()
    options = [str(opt).lower().strip() for opt in options]
    for opt in options:
        if opt in transcript or transcript in opt:
            return opt
    return None

# 🎤 WebSocket STT handler: receives real-time audio, triggers STT,
@app.websocket("/stt")
async def websocket_stt(websocket: WebSocket):
    await websocket.accept()
    audio_queue = asyncio.Queue()
    transcript_buffer = ""
    buffer_start_time = datetime.now()
    last_transcript = ""
    buffered_audio = b''
    last_voice_time = datetime.now()
    start_voice_time = datetime.now()
    SILENCE_GAP = 2.0
    phone_digit_buffer = ""
    MAX_WAIT = 6.0

    # 🧠 Background task to process buffered audio and call STT when silence or timeout is detected
    async def process_audio():
        nonlocal buffered_audio, start_voice_time, last_transcript,last_voice_time
        while True:
            if not audio_queue.empty():
                audio_data = await audio_queue.get()
                buffered_audio += audio_data

                rms = audioop.rms(audio_data, 2)

                # Update last voice time if speaking
                if rms > 200:
                    last_voice_time = datetime.now()

                now = datetime.now()
                time_since_last_voice = (now - last_voice_time).total_seconds()
                total_speaking_time = (now - start_voice_time).total_seconds()

                # 🧠 Check if silence or max duration exceeded
                min_bytes = 51200       
                if (
                    len(buffered_audio) >= min_bytes and (
                        time_since_last_voice > SILENCE_GAP or
                        total_speaking_time > MAX_WAIT or
                        detect_silence_at_end(buffered_audio, window_ms=300)
                    )
                ):
                    await asyncio.sleep(0.5)
                    if len(buffered_audio) < 4096:
                        buffered_audio = b"\x00" * 2048 + buffered_audio

                    print(f"📤 Triggering STT | Buffer Size: {len(buffered_audio)} | Time: {total_speaking_time:.2f}s")
                    transcript = await asyncio.to_thread(transcribe_streaming,buffered_audio)
                    if not transcript and len(buffered_audio) >= 51200:
                        print("🔁 Empty STT — retrying once...")
                        transcript = await asyncio.to_thread(transcribe_streaming, buffered_audio)
                    print("📏 Sending audio of duration (ms):", len(buffered_audio) / (16000 * 2) * 1000)
                    print("🎙️ Final Transcript:", transcript)
                    await process_transcript(transcript)

                    # Reset
                    buffered_audio = b''
                    start_voice_time = datetime.now()

            await asyncio.sleep(0.1)

    # 🧠 Processes final transcript to extract form field answers
    # Uses normalization, fallback email/phone logic, GPT if needed, and sends result to frontend
    async def process_transcript(transcript):
        nonlocal last_transcript, transcript_buffer, buffer_start_time,phone_digit_buffer
        final = transcript.strip()
        if not final:
            return  # Skip logging or processing for blank
        # Append to buffer
        transcript_buffer += " " + final
        buffer_start_time = datetime.now()
        current_field = session_state.get("current_field")
        if "email" in current_field and not looks_like_email(normalize_email(transcript_buffer)):
            print("Waiting for complete email...")
            return
        if "phone" in current_field and (not transcript_buffer.replace(" ", "").isdigit() or len(transcript_buffer.replace(" ", "")) < 10):
            print(" Waiting for complete phone number...")
            return
        print("Final Transcript in process_transcript :", final)  # Always log final, even if short
        extracted_answers = {}
        current_field = session_state.get("current_field")
        if not current_field:
            return
        question = session_state["field_questions"].get(current_field, f"Question for {current_field}")
        field_type = session_state["field_types"].get(current_field, "text")
        field_options = session_state["field_options"].get(current_field, [])
        if field_type == "checkbox" and field_options:
            # Multi-select: split transcript by "and", ",", or just space
            spoken = final.lower().replace(" and ", ",").replace(" & ", ",")
            user_choices = [opt.strip() for opt in spoken.split(",") if opt.strip()]
            matched_options = []
            for choice in user_choices:
                for opt in field_options:
                    if choice in opt.lower() or opt.lower() in choice:
                        matched_options.append(opt)
            if matched_options:
                answer = ",".join(matched_options)
                extracted_answers[current_field] = {"question": question, "answer": answer}
                await websocket.send_json({
                    "type": "fill_field",
                    "field_name": current_field,
                    "value": answer
                })
                # Move to next field
                fields = session_state.get("fields", [])
                idx = fields.index(current_field) if current_field in fields else -1
                session_state["current_field"] = fields[idx + 1] if idx + 1 < len(fields) else None
                transcript_buffer = ""
                last_transcript = ""
                return
            else:
                await websocket.send_json({
                    "transcript": final,
                    "answers": {},
                    "retry": True,
                    "message": f"Please say one or more of: {', '.join(field_options)}"
                })
                return
        elif field_type == "radio" and field_options:
            matched_option = match_spoken_option(final, field_options)
            print(f"Matched spoken option: {matched_option}")
            if matched_option:
                answer = matched_option
                extracted_answers[current_field] = {"question": question, "answer": answer}
                await websocket.send_json({
                    "type": "fill_field",
                    "field_name": current_field,
                    "value": answer
                })
                # Move to next field
                fields = session_state.get("fields", [])
                idx = fields.index(current_field) if current_field in fields else -1
                session_state["current_field"] = fields[idx + 1] if idx + 1 < len(fields) else None
                transcript_buffer = ""
                last_transcript = ""
                return
            else:
                await websocket.send_json({
                    "transcript": final,
                    "answers": {},
                    "retry": True,
                    "message": f"Please say one of: {', '.join(field_options)}"
                })
                return
        if "email" in current_field:
            # 1. Try to extract and normalize from latest transcript only
            possible_email = extract_possible_email(final)
            email_candidate = normalize_email(possible_email)
            print("Final Email is:", email_candidate)
            if looks_like_email(email_candidate):
                final = email_candidate
                transcript_buffer = ""  # Success: clear buffer
            else:
                # 2. Fallback: try the full buffer
                possible_email_buf = extract_possible_email(transcript_buffer)
                email_candidate_buf = normalize_email(possible_email_buf)
                print("Fallback Email from buffer:", email_candidate_buf)
                if looks_like_email(email_candidate_buf):
                    final = email_candidate_buf
                    transcript_buffer = ""  # Success: clear buffer
                else:
                    print(" Waiting for complete email...")
                    transcript_buffer = ""
                    return
        if "phone" in current_field:
            # Extract digits from current transcript (ignore non-digits)
            digits = ''.join(filter(str.isdigit, transcript))
            phone_digit_buffer += digits
            print(f"Buffered phone digits: {phone_digit_buffer}")
            if len(phone_digit_buffer) < 10:
                print("Waiting for complete phone number...")
                return
            # Accept first 10 digits as phone number
            final = phone_digit_buffer[:10]
            phone_digit_buffer = ""
        if field_type == "time":
            norm_time = parse_spoken_time(final)
            if norm_time:
                answer = norm_time
                extracted_answers[current_field] = {"question": question, "answer": answer}
                await websocket.send_json({
                    "type": "fill_field",
                    "field_name": current_field,
                    "value": answer
                })
                # Move to next field
                fields = session_state.get("fields", [])
                idx = fields.index(current_field) if current_field in fields else -1
                session_state["current_field"] = fields[idx + 1] if idx + 1 < len(fields) else None
                transcript_buffer = ""
                last_transcript = ""
                return
            else:
                await websocket.send_json({
                    "transcript": final,
                    "answers": {},
                    "retry": True,
                    "message": "Please say a time (e.g., 3 pm, 14:30, 7 in the morning)"
                })
                return
            
        if field_type == "date":
            norm_date = parse_spoken_date(final)
            print(f"Parsed date: {norm_date}")
            if norm_date:
                answer = norm_date
                extracted_answers[current_field] = {"question": question, "answer": answer}
                await websocket.send_json({
                    "type": "fill_field",
                    "field_name": current_field,
                    "value": answer  # Format: YYYY-MM-DD
                })
                # Move to next field and RESET buffers!
                fields = session_state.get("fields", [])
                idx = fields.index(current_field) if current_field in fields else -1
                session_state["current_field"] = fields[idx + 1] if idx + 1 < len(fields) else None
                transcript_buffer = ""
                last_transcript = ""
                return
            else:
                await websocket.send_json({
                    "transcript": final,
                    "answers": {},
                    "retry": True,
                    "message": "Please say a date, for example: 4th July, tomorrow, or July 4 2024."
                })
                return
        #  Call GPT only if needed
        normalized = normalize_transcript(final, current_field)
        if normalized:
            answer = normalized
        else:
            answer = extract_answer_from_gpt(current_field, final)
            print(f"GPT Answer for '{current_field}':", answer)
        extracted_answers[current_field] = {"question": question, "answer": answer}
        current_field = session_state.get("current_field")
        if current_field and current_field in extracted_answers:
            # Only send the answer string, not the object!
            answer_str = extracted_answers[current_field].get("answer", "")
            await websocket.send_json({
                "type": "fill_field",
                "field_name": current_field,
                "value": answer_str
            })
        # Move to next field
        fields = session_state.get("fields", [])
        idx = fields.index(current_field) if current_field in fields else -1
        session_state["current_field"] = fields[idx + 1] if idx + 1 < len(fields) else None
        transcript_buffer = ""
        last_transcript = ""

    asyncio.create_task(process_audio())

    try:
        while True:
            audio_data = await websocket.receive_bytes()
            await audio_queue.put(audio_data)
    except WebSocketDisconnect:
        print("❌ Client disconnected")
    except Exception as e:
        print(f"❌ WebSocket error: {e}")
        
        
# Submit Form Endpoint
@app.post("/submit-form")
async def submit_form(request: Request):
    """Submit the filled form data to the target URL"""
    try:
        data = await request.json()
        target_url = data.get("target_url")
        form_data = data.get("form_data", {})
        if not target_url or not form_data:
            raise HTTPException(status_code=400, detail="Missing target_url or form_data")
        # Submit the form data to the target URL
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.goto(target_url, wait_until="domcontentloaded")
            # Fill all form fields
            for field_name, field_value in form_data.items():
                try:
                    # Try different selectors for the field
                    selectors = [
                        f'input[name="{field_name}"]',
                        f'select[name="{field_name}"]',
                        f'textarea[name="{field_name}"]',
                        f'#{field_name}',
                    ]
                    field_filled = False
                    for selector in selectors:
                        try:
                            element = await page.query_selector(selector)
                            if element:
                                element_type = await element.get_attribute('type')
                                tag_name = await element.evaluate('el => el.tagName.toLowerCase()')
                                if tag_name == 'select':
                                    await element.select_option(field_value)
                                elif element_type in ['checkbox', 'radio']:
                                    if field_value.lower() in ['true', 'yes', '1']:
                                        await element.check()
                                elif tag_name in ['input', 'textarea']:
                                    await element.fill(str(field_value))
                                field_filled = True
                                break
                        except Exception as e:
                            continue
                    if not field_filled:
                        logger.warning(f"Could not fill field: {field_name}")
                except Exception as e:
                    logger.error(f"Error filling field {field_name}: {e}")
            # Submit the form
            try:
                # Try to find and click submit button
                submit_selectors = [
                    'input[type="submit"]',
                    'button[type="submit"]',
                    'button:has-text("Submit")',
                    'button:has-text("Send")',
                    'form button:last-child'
                ]
                submitted = False
                for selector in submit_selectors:
                    try:
                        submit_btn = await page.query_selector(selector)
                        if submit_btn:
                            await submit_btn.click()
                            submitted = True
                            break
                    except:
                        continue
                if not submitted:
                    # Fallback: submit the form directly
                    await page.evaluate('document.querySelector("form").submit()')
                # Wait for navigation or response
                await page.wait_for_timeout(2000)
                # Get the final URL and status
                final_url = page.url
                await browser.close()
                return {
                    "success": True,
                    "message": "Form submitted successfully",
                    "final_url": final_url,
                    "submitted_data": form_data
                }
            except Exception as e:
                await browser.close()
                return {
                    "success": False,
                    "message": f"Error submitting form: {str(e)}",
                    "submitted_data": form_data
                }
    except Exception as e:
        logger.error(f"Error in submit_form: {e}")
        raise HTTPException(status_code=500, detail=f"Submission failed: {str(e)}")

# 📄 Analyzes a form URL and extracts input fields using Playwright 
# Generates natural questions using GPT and initializes session state
@app.post("/analyze-form")
async def analyze_form(request: URLRequest, db: Session = Depends(get_db)):
    try:
        # Always fetch dynamically, ignore static rendering
        url = str(request.url)
        # Store the target URL in session
        session_state["target_url"] = url
        form_html = await extract_shadow_form(url)
        if not form_html:
            form_html = await extract_normal_form(url)
        fields = extract_fields_from_html(form_html)
        if not fields:
            raise HTTPException(status_code=400, detail="No input fields found.")
        questions = generate_questions(fields)
        session_state["fields"] = [f['name'] for f in fields if f['name']]
        session_state["field_questions"] = {f['name']: q for f, q in zip(fields, questions) if f['name']}
        session_state["field_types"] = {f['name']: f.get('type', 'text') for f in fields if f['name']}
        session_state["field_options"] = {f['name']: f.get('options', []) for f in fields if f['name']}
        session_state["current_field"] = session_state["fields"][0]
        logger.info("Fields extracted: %s", fields)
        logger.info("Questions generated: %s", questions)
        return {"fields": fields, "questions": questions, "extracted_answers": {}}
    except Exception as e:
        error_message = f"Error occurred: {str(e)}\n{traceback.format_exc()}"
        print(f"Error: {error_message}")
        error = ErrorLog(url=str(request.url), error_message=error_message, dynamic=True)
        db.add(error)
        db.commit()
        raise HTTPException(status_code=500, detail="Error logged and returned.")

# 🗂️ Mounts the frontend static files (HTML/JS/CSS) from the /static directory
app.mount("/", StaticFiles(directory="static", html=True), name="static")



