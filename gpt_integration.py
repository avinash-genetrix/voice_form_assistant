import openai
import os
from dotenv import load_dotenv
from fastapi import HTTPException
import re

load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY")

# 🎤 This function uses GPT to generate natural, friendly questions for each form field.
def generate_questions(fields):
    questions = []

    for field in fields:
        label = field.get("label") or field.get("name", "")
        name = field.get("name", "")
        ftype = field.get("type", "")
        tag = field.get("tag", "")
        options = field.get("options", [])

        if not name or not label:
            continue
        
        prompt = (
                f"You're a friendly voice assistant. Write a casual, natural-sounding question "
                f"to ask the user for this form field:\n"
                f"- Label: \"{label}\"\n"
                f"- Type: \"{ftype or tag}\"\n"
            )
        if options:
            clean_options = [opt.strip() for opt in options if opt and "select" not in opt.lower()]
            if clean_options:
                prompt += f'- Options: {", ".join(clean_options)}\n'
                prompt += (
                    'Include all the options clearly and naturally in the question.\n'
                )

        prompt += (
           "Avoid robotic phrases like 'please enter'. "
            "Do not use filler phrases like 'when you get a chance', 'if you can', or 'so I can help you'. "
            "Sound like a clear, polite form assistant — efficient but friendly. "
            "No question mark at the end. No numbering.\n"
        )
        try:
            response = openai.ChatCompletion.create(
                model="gpt-4.1-mini",  # Use "gpt-4" or "gpt-3.5-turbo" if needed
                messages=[
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.4,
                max_tokens=50
            )
            question = response['choices'][0]['message']['content'].strip().rstrip("?")
            questions.append(question)
        except Exception as e:
            print(f"❌ Error for field '{name}': {e}")
            questions.append(f"{label}")

    return questions


# 🧹 This function normalizes raw speech-to-text transcripts based on the expected field type.
# For email: replaces 'at', 'dot' with '@' and '.' and extracts valid pattern.
# For phone: strips non-digits and returns 10-digit Indian number with +91 prefix.
def normalize_transcript(text, field_name):
    text = text.lower().strip()

    if 'email' in field_name.lower():
        if ' at ' in text:
            text = text.replace(' at ', '@')
        if ' dot ' in text:
            text = text.replace(' dot ', '.')
        if ' gmail' in text and '@' not in text:
            text = text.replace(' gmail', '@gmail.com')
        # Extract actual email
        match = re.search(r'\b[\w.-]+@[\w.-]+\.\w+\b', text)
        if match:
            return match.group(0)

    if 'phone' in field_name.lower():
        digits = re.sub(r'\D', '', text)
        if len(digits) >= 10:
            return '+91' + digits[-10:]

    return None  # fallback to GPT


# 🤖 This function uses GPT to extract the exact field value from user's spoken response.
# It returns only the value — no greetings, no explanation — just the clean answer.
def extract_answer_from_gpt(field_name, prompt):
    """
    Extracts the answer for a given field from the response using GPT.
    """
    try:
        completion = openai.ChatCompletion.create(
            model="gpt-4.1-mini",  
            messages=[
                {"role": "system", "content":  "You are a helpful assistant. "
                        "Your job is to extract only the exact answer from the user’s response. "
                        "Do not add any explanation, punctuation, or greeting. "
                        "Just return the value like 'abc' or '9876543210' or 'abc@gmail.com'. "
                        "Never say 'The user's name is ...' or similar. Return only the answer."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=50,
            temperature=0.2  
        )
        return completion.choices[0].message['content'].strip()
    except Exception as e:
        raise Exception(f"Error extracting answer: {str(e)}")
    