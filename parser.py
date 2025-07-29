from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
import requests
from fastapi import Form, Request
import json
import logging
# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# 🔍 Extract a form inside a nested shadow DOM (2 levels deep)
async def extract_shadow_form(url):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(url, wait_until="domcontentloaded")
        shadow_form_html = await page.evaluate("""
        () => {
            const host = document.querySelector('#host');
            if (!host) return null;
            const shadowRoot1 = host.shadowRoot;
            if (!shadowRoot1) return null;
            const innerHost = shadowRoot1.getElementById('inner-host');
            if (!innerHost) return null;
            const shadowRoot2 = innerHost.shadowRoot;
            if (!shadowRoot2) return null;
            const form = shadowRoot2.getElementById('shadow-form');
            if (!form) return null;
            return form.outerHTML;
        }
        """)
        await browser.close()
        return shadow_form_html
    
# 🔍 Extract a normal HTML form from the page DOM
async def extract_normal_form(url):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(url, wait_until="domcontentloaded")
        form_html = await page.evaluate("""
        () => {
            const form = document.querySelector('form');
            if (form) return form.outerHTML;
            return null;
        }
        """)
        await browser.close()
        return form_html

# 🧠 Parse HTML of a form and extract structured metadata about all fields
def extract_fields_from_html(form_html):
    if not form_html:
        return []
    soup = BeautifulSoup(form_html, "html.parser")
    form = soup.find("form")
    if not form:
        return []

    fields = []
    radio_groups = {}

    # Helper to fetch label
    def get_label(field):
        label = ""
        if 'id' in field.attrs:
            label_tag = form.find('label', attrs={'for': field['id']})
            if label_tag:
                label = label_tag.get_text(strip=True)
        if not label:
            parent_label = field.find_parent('label')
            if parent_label:
                label = parent_label.get_text(strip=True)
        return label

    # Track processed elements to avoid double-count
    processed = set()


    # Main traversal: in DOM order
    for elem in form.descendants:
        if getattr(elem, "name", None) in ["input", "select", "textarea", "button"] and elem not in processed:
            tag = elem.name
            # Determine type, with defaults and explicit types for selects/textareas/buttons
            if tag == "input":
                type_ = elem.get("type", "text")
            elif tag == "select":
                type_ = "select-one"
            elif tag == "textarea":
                type_ = "textarea"
            elif tag == "button":
                type_ = elem.get("type", "button")
            else:
                type_ = ""

            name = elem.get("name", "")
            field = None

            # Google reCAPTCHA special case (textarea or input)
            if name == "g-recaptcha-response":
                field = {
                    "tag": tag,
                    "type": "recaptcha",
                    "name": name,
                    "label": "Google reCAPTCHA",
                    "id": elem.get("id", ""),
                    "value": "",
                    "options": [],
                    "required": elem.has_attr("required"),
                }

            # Grouped checkboxes
            elif tag == "input" and type_ == "checkbox":
                if name and name not in processed:
                    group = form.find_all("input", {"type": "checkbox", "name": name}) if name else [elem]
                    options = []
                    for cb in group:
                        options.append(get_label(cb) or cb.get("value", "") or f"Option{len(options) + 1}")
                        processed.add(cb)
                    fields.append({
                        "tag": "input",
                        "type": "checkbox",
                        "name": name,
                        "label": get_label(group[0].find_parent(['div', 'fieldset', 'ul']) or group[0]),
                        "options": options,
                        "required": any(cb.has_attr("required") for cb in group),
                        "multiple": True,
                        "id": group[0].get("id", ""),
                    })
                continue

            # Radio groups
            elif tag == "input" and type_ == "radio":
                if name and name not in radio_groups:
                    group = form.find_all("input", {"type": "radio", "name": name})
                    options = [cb.get("value", get_label(cb)) for cb in group]
                    label = get_label(group[0])
                    fields.append({
                        "tag": "input",
                        "type": "radio",
                        "name": name,
                        "options": options,
                        "label": label,
                        "id": group[0].get("id", ""),
                        "required": any(cb.has_attr("required") for cb in group),
                    })
                    for cb in group:
                        processed.add(cb)
                continue

            # Single checkbox
            elif tag == "input" and type_ == "checkbox":
                field = {
                    "tag": tag,
                    "type": type_,
                    "name": name,
                    "value": elem.get("value", ""),
                    "label": get_label(elem),
                    "id": elem.get("id", ""),
                    "options": [],
                    "required": elem.has_attr("required"),
                }

            # File input
            elif tag == "input" and type_ == "file":
                field = {
                    "tag": tag,
                    "type": "file",
                    "name": name,
                    "label": get_label(elem),
                    "id": elem.get("id", ""),
                    "options": [],
                    "required": elem.has_attr("required"),
                    "multiple": elem.has_attr("multiple"),
                }

            # Color picker
            elif tag == "input" and type_ == "color":
                field = {
                    "tag": tag,
                    "type": "color",
                    "name": name,
                    "value": elem.get("value", "#000000"),
                    "label": get_label(elem),
                    "id": elem.get("id", ""),
                    "options": [],
                    "required": elem.has_attr("required"),
                }

            # Other inputs (text, email, password, etc.)
            elif tag == "input":
                field = {
                    "tag": tag,
                    "type": type_,
                    "name": name,
                    "value": elem.get("value", ""),
                    "label": get_label(elem),
                    "id": elem.get("id", ""),
                    "options": [],
                    "min": elem.get("min", ""),
                    "max": elem.get("max", ""),
                    "minLength": elem.get("minlength", ""),
                    "maxLength": elem.get("maxlength", ""),
                    "required": elem.has_attr("required"),
                    "pattern": elem.get("pattern", ""),
                }

            # Textarea (not recaptcha)
            elif tag == "textarea":
                if name == "g-recaptcha-response":
                    field = {
                        "tag": tag,
                        "type": "recaptcha",
                        "name": name,
                        "label": "Google reCAPTCHA",
                        "id": elem.get("id", ""),
                        "value": "",
                        "options": [],
                        "required": elem.has_attr("required"),
                    }
                else:
                    field = {
                        "tag": tag,
                        "type": "textarea",
                        "name": name,
                        "value": elem.string if elem.string else "",
                        "label": get_label(elem),
                        "id": elem.get("id", ""),
                        "options": [],
                        "min": "",
                        "max": "",
                        "minLength": elem.get("minlength", ""),
                        "maxLength": elem.get("maxlength", ""),
                        "required": elem.has_attr("required"),
                        "pattern": "",
                    }

            # Select
            elif tag == "select":
                options = [option.get_text(strip=True) for option in elem.find_all("option")]
                field = {
                    "tag": tag,
                    "type": "select-one",
                    "name": name,
                    "value": "",
                    "label": get_label(elem),
                    "id": elem.get("id", ""),
                    "options": options,
                    "min": "",
                    "max": "",
                    "minLength": "",
                    "maxLength": "",
                    "required": elem.has_attr("required"),
                    "pattern": "",
                }

            # Submit button
            elif (tag == "button" and (type_ == "submit" or not elem.has_attr("type"))) or (tag == "input" and type_ == "submit"):
                field = {
                    "tag": tag,
                    "type": "submit",
                    "name": name,
                    "value": elem.get("value", elem.get_text(strip=True) if tag == "button" else ""),
                    "label": elem.get("aria-label", "") or elem.get_text(strip=True) or "Submit",
                    "id": elem.get("id", ""),
                }

            if field:
                fields.append(field)
                processed.add(elem)

    # Allowed types and tags
    allowed_types = {
        "text", "email", "file", "select-one", "textarea", "checkbox", "multi-checkbox",
        "radio", "color", "date", "tel", "number", "submit", "button", "recaptcha","time"
    }
    allowed_tags = {"select", "textarea", "button"}

    # Filter fields to only allowed types or tags
    fields = [f for f in fields if (f.get("type") in allowed_types or f.get("tag") in allowed_tags)]
    logger.info("==========================================")
    logger.info("Extracted fields: %s", fields)
    logger.info("==========================================")
    return fields

# 📥 FastAPI endpoint logic to extract form from a given URL
# Tries shadow DOM first, falls back to normal form extraction
async def extract_form(request: Request, url: str = Form(...)):
    shadow_form = await extract_shadow_form(url)
    if shadow_form:
        form_html = shadow_form
    else:
        form_html = await extract_normal_form(url)

    fields = extract_fields_from_html(form_html)
    
    # 🟣 LOGGING
    logger.info("\n======= FORM ASSISTANT LOG =======")
    logger.info(f"🔗 URL submitted: {url}")
    logger.info(f"📝 Fields extracted ({len(fields)} fields):")
    logger.info(json.dumps(fields, indent=2, ensure_ascii=False))
    logger.info("==================================\n")

    return {
        "form_html": form_html,
        "fields": fields,
        "url": url
    }