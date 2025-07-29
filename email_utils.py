import re

# 🔧 This function normalizes spoken or semi-structured email text into a proper email format.
# It handles common replacements like "at" → "@", "dot" → ".", etc., and fixes missing "@" if needed.
def normalize_email(text: str) -> str:
    text = text.lower()
    replacements = {
        " at ": "@",
        " at tha ": "@",
        " dot ": ".",
        " underscore ": "_",
        " dash ": "-",
        " space ": "",
        " attherate ": "@",
        " gmail logo": "@gmail.com",
        " at the gmail dot com": "@gmail.com",
        " at the gmail": "@gmail.com",
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    text = text.replace(" ", "")
    if text.endswith("gmail.com") and "@" not in text:
        name = text[:-9]  # remove 'gmail.com'
        text = name + "@gmail.com"

    # (OPTIONAL) Repeat for other domains:
    if text.endswith("yahoo.com") and "@" not in text:
        name = text[:-9]
        text = name + "@yahoo.com"
    if text.endswith("outlook.com") and "@" not in text:
        name = text[:-11]
        text = name + "@outlook.com"
    text = re.sub(r"@thegmail\\.com", "@gmail.com", text)
    return text


# 🔍 This function attempts to extract a valid-looking email address from transcribed speech.
# It removes common prefixes and filler words and tries to recover patterns like 'abc gmail.com'.
def extract_possible_email(text: str) -> str:
    text = text.lower().strip()
    # Remove any spoken prefix at start: "my email id is", "email address is", etc.
    prefix_pattern = r"^(my\s*)?(email(\s*(address|id))?\s*is\s*)"
    text = re.sub(prefix_pattern, "", text).strip()

    # Remove filler words
    for filler in [" enter ", " the rate ", " attherate ", " at the ", " at "]:
        text = text.replace(filler, " @ ")
    text = text.replace(" dot ", ".")
    text = text.replace(" underscore ", "_")
    text = text.replace(" dash ", "-")
    text = text.replace(" space ", "")

    # Remove extra spaces and then all spaces
    text = ' '.join(text.split())
    text = text.replace(" ", "")

    # Try to extract classic email pattern
    match = re.search(r"([a-z0-9_.+-]+)@([a-z0-9-]+\.[a-z0-9-.]+)", text)
    if match:
        return match.group(0)
    # Handle 'abc@gmail.com' spoken as 'abc gmail.com'
    match2 = re.search(r"([a-z0-9_.+-]+)gmail\.com", text)
    if match2:
        return match2.group(1) + "@gmail.com"
    # Extend for yahoo, outlook, etc. as needed

    return text  # fallback


def looks_like_email(text: str) -> bool:
    return bool(re.match(r"[^@ \t\r\n]+@[^@ \t\r\n]+\.[^@ \t\r\n]+", text))