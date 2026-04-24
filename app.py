import os
import json
import time
import re
import random
import smtplib
import imaplib
import email
import csv
import io
import threading
import queue
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder='.')

CONFIG_FILE = 'config.json'
LEADS_FILE = 'leads.json'
file_lock = threading.Lock()
send_queue = queue.Queue()

def load_json(file, default):
    if os.path.exists(file):
        with open(file, 'r') as f:
            return json.load(f)
    return default

def save_json(file, data):
    with file_lock:
        with open(file, 'w') as f:
            json.dump(data, f, indent=4)

# --- BACKGROUND SEQUENTIAL SENDER ---
def send_worker():
    while True:
        task = send_queue.get()
        if task is None: break
        
        lead_id, content, config, target_email, target_name, is_followup = task
        
        try:
            # Natural sequential delay (longer to prevent Gmail flags)
            time.sleep(random.uniform(10, 15))
            
            msg = MIMEMultipart()
            msg['From'] = f"{config.get('sender_name')} <{config.get('gmail_address')}>"
            msg['To'] = target_email
            msg['Subject'] = f"Quick question for {target_name}" if not is_followup else "Re: Quick question"
            msg.attach(MIMEText(content, 'plain'))
            
            server = smtplib.SMTP('smtp.gmail.com', 587)
            server.starttls()
            server.login(config.get('gmail_address'), config.get('gmail_app_password'))
            server.send_message(msg)
            server.quit()
            
            leads = load_json(LEADS_FILE, [])
            today_str = datetime.now().strftime('%Y-%m-%d')
            for l in leads:
                if l['id'] == lead_id:
                    l['status'] = 'Sent'
                    l['sent_date'] = today_str
                    l['follow_up_date'] = (datetime.now() + timedelta(days=3)).strftime('%Y-%m-%d')
                    l['follow_up_done'] = False
                    l['follow_up_count'] = l.get('follow_up_count', 0) + 1
            save_json(LEADS_FILE, leads)
        except Exception as e:
            leads = load_json(LEADS_FILE, [])
            for l in leads:
                if l['id'] == lead_id:
                    l['status'] = 'Failed'
                    l['error_log'] = str(e)
            save_json(LEADS_FILE, leads)
        
        send_queue.task_done()

# Start the single worker thread
threading.Thread(target=send_worker, daemon=True).start()

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/api/status')
def status():
    return jsonify({"gemini": True, "queue_size": send_queue.qsize()})

# --- AI HELPER ---
def call_ai(prompt, config, max_tokens=300):
    groq_key = config.get('groq_api_key', '')
    gemini_key = config.get('gemini_api_key', '')
    preferred_ai = config.get('preferred_ai', 'gemini')

    # Try Groq
    if (preferred_ai == 'groq' and groq_key) or (not gemini_key and groq_key):
        try:
            url = "https://api.groq.com/openai/v1/chat/completions"
            headers = {"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"}
            payload = {
                "model": "llama-3.3-70b-versatile", 
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens
            }
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
            if resp.status_code == 200:
                return resp.json()['choices'][0]['message']['content'].strip()
        except: pass

    # Try Gemini
    if gemini_key:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={gemini_key}"
            payload = {"contents": [{"parts": [{"text": prompt}]}]}
            resp = requests.post(url, json=payload, timeout=30)
            if resp.status_code == 200:
                return resp.json()['candidates'][0]['content']['parts'][0]['text'].strip()
        except: pass
    return None

@app.route('/api/check-replies', methods=['POST'])
def check_replies():
    config = load_json(CONFIG_FILE, {})
    leads = load_json(LEADS_FILE, [])
    user = config.get('gmail_address')
    password = config.get('gmail_app_password')
    
    if not user or not password:
        return jsonify({"error": "Gmail settings missing"}), 400

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(user, password)
        mail.select("inbox")
        
        since_date = (datetime.now() - timedelta(days=7)).strftime("%d-%b-%Y")
        _, data = mail.search(None, f'(SINCE "{since_date}")')
        
        updated_count = 0
        for num in data[0].split():
            _, msg_data = mail.fetch(num, "(RFC822)")
            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)
            from_email = email.utils.parseaddr(msg['From'])[1].lower()
            
            lead = next((l for l in leads if l['email'].lower() == from_email), None)
            if lead and lead['status'] not in ['Interested', 'Not Interested']:
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode()
                            break
                else:
                    body = msg.get_payload(decode=True).decode()

                prompt = f"""Classify this email reply from a lead. 
Reply Body: "{body}"
Categories: Interested, Not Interested, Question, Out of Office.
Output ONLY the category name."""
                
                category = call_ai(prompt, config, max_tokens=10)
                if category:
                    category = category.strip()
                    if "Interested" in category:
                        lead['status'] = "Interested"
                        lead['last_reply'] = body
                    elif "Not Interested" in category:
                        lead['status'] = "Not Interested"
                    elif "Question" in category:
                        lead['status'] = "Question"
                        lead['last_reply'] = body
                    updated_count += 1

        save_json(LEADS_FILE, leads)
        mail.logout()
        return jsonify({"status": "success", "updated": updated_count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/generate-email', methods=['POST'])
def generate_email():
    lead = request.json
    config = load_json(CONFIG_FILE, {})
    sender_name = config.get('sender_name', 'Ntokozo')
    
    is_followup = lead.get('follow_up_count', 0) > 0
    
    prompt = f"""Write a professional cold email from {sender_name} at FieldAI to the owner or sales manager of {lead['name']}.

Use this exact structure:

Subject line: Write one compelling subject line starting with "Subject:"

Body:
- Opening: Address them by business name only, never use [First Name] or any placeholder. Start with what FieldAI's AutoFlow system does for their specific business and how.
- Problem: One sentence naming the exact pain — property enquiries and leads go cold when follow-up is slow or missed entirely.
- Solution: FieldAI sets up WhatsApp automation (via our AutoFlow service) that responds to every lead within 60 seconds, follows up automatically over 3 days, and collects reviews after every closed deal.
- Result: More deals closed, no lost leads, no extra staff needed.
- CTA: One soft question asking if they are open to a free 10-minute call this week to see exactly how it works for {lead['name']}.
- Sign off: "Warm regards,\\n{sender_name}\\nFieldAI"

Rules:
- Under 120 words in the body
- Professional, warm, direct tone
- No fake experience or fake clients
- No word count notes, no meta commentary, no asterisks
- Output subject line first, then body — nothing else"""

    text = call_ai(prompt, config)
    if text:
        return jsonify({"email": text})
    return jsonify({"error": "AI failed."}), 500

@app.route('/api/suggest-reply', methods=['POST'])
def suggest_reply():
    data = request.json
    config = load_json(CONFIG_FILE, {})
    lead_name = data.get('name')
    last_reply = data.get('last_reply')
    prompt = f"""A lead named {lead_name} replied with: "{last_reply}". Draft a short, helpful, professional response from {config.get('sender_name')} at FieldAI to book a meeting. Short and human. Body only."""
    text = call_ai(prompt, config)
    return jsonify({"reply": text})

@app.route('/api/send-email', methods=['POST'])
def send_email():
    data = request.json
    lead_id = data.get('lead_id')
    content = data.get('content')
    config = load_json(CONFIG_FILE, {})
    leads = load_json(LEADS_FILE, [])
    
    target_lead = next((l for l in leads if l['id'] == lead_id), None)
    if not target_lead: return jsonify({"error": "Lead not found"}), 404

    if target_lead.get('status') == 'Not Interested':
        return jsonify({"error": "Lead is marked as Not Interested."}), 400

    is_followup = target_lead.get('follow_up_count', 0) > 0
    
    # IMMEDIATELY update status to "Sending"
    for l in leads:
        if l['id'] == lead_id:
            l['status'] = 'Sending'
    save_json(LEADS_FILE, leads)
    
    # Add to sequential queue
    send_queue.put((lead_id, content, config, target_lead['email'], target_lead['name'], is_followup))
    
    return jsonify({"status": "queued"})

@app.route('/api/import-csv', methods=['POST'])
def import_csv():
    if 'file' not in request.files: return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    try:
        stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
        csv_input = csv.DictReader(stream)
        existing_leads = load_json(LEADS_FILE, [])
        email_status_map = {l['email'].lower(): l['status'] for l in existing_leads}
        new_leads = []
        for row in csv_input:
            name, email_addr = row.get('name'), row.get('email', '').strip().lower()
            if email_addr:
                status = "Pending"
                if email_status_map.get(email_addr) == "Sent": status = "Already Contacted"
                if any(nl['email'].lower() == email_addr for nl in new_leads): continue
                new_leads.append({"id": str(int(time.time()*1000))+str(random.randint(100,999)), "name": name or "Unknown", "email": email_addr, "status": status, "generated_email": "", "follow_up_count": 0})
        all_leads = existing_leads + new_leads
        save_json(LEADS_FILE, all_leads)
        return jsonify({"status": "success", "count": len(new_leads)})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        save_json(CONFIG_FILE, request.json)
        return jsonify({"status": "success"})
    return jsonify(load_json(CONFIG_FILE, {}))

@app.route('/api/leads', methods=['GET', 'POST'])
def manage_leads():
    if request.method == 'POST':
        save_json(LEADS_FILE, request.json)
        return jsonify({"status": "success"})
    return jsonify(load_json(LEADS_FILE, []))

@app.route('/api/scrape', methods=['POST'])
def scrape_leads():
    data = request.json
    niche, city = data.get('niche', ''), data.get('city', '')
    config = load_json(CONFIG_FILE, {})
    serper_key = config.get('serper_api_key', '')

    prompt = f"List 15 real {niche} businesses in {city}, South Africa. Plain list, names only, one per line."
    raw = call_ai(prompt, config)
    
    if not raw: 
        return jsonify({"error": "AI failed to generate business names. Check your API keys."}), 500
    
    business_names = [l.strip().lstrip('-•# 1234567890. ') for l in raw.strip().split('\n') if len(l.strip()) > 2][:15]
    
    if not business_names:
        return jsonify({"error": f"AI generated an empty list for {niche} in {city}."}), 500

    leads, existing_leads = [], load_json(LEADS_FILE, [])
    seen_emails = {l['email'].lower() for l in existing_leads}
    duplicate_count = 0
    
    # Enhanced browser headers
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.google.com/",
        "DNT": "1"
    }

    # More aggressive email regex
    email_pattern = r'[a-zA-Z0-9.\-_%]+@[a-zA-Z0-9.\-_]+\.[a-zA-Z]{2,6}'

    for name in business_names:
        email_addr = None
        # Try a more specific query
        query = f'"{name}" {city} "email" OR "contact"'
        
        try:
            if serper_key:
                url = "https://google.serper.dev/search"
                payload = json.dumps({"q": query, "num": 10})
                s_headers = {'X-API-KEY': serper_key, 'Content-Type': 'application/json'}
                r = requests.post(url, headers=s_headers, data=payload, timeout=10)
                search_text = r.text
            else:
                # Add a longer delay to try and trick Google
                time.sleep(random.uniform(3, 6))
                r = requests.get(f"https://www.google.com/search?q={requests.utils.quote(query)}&num=10", headers=headers, timeout=10)
                search_text = r.text

            emails = re.findall(email_pattern, search_text)
            
            for e in emails:
                e_lower = e.lower()
                # Filter out clearly wrong stuff
                if any(j in e_lower for j in ['google', 'sentry', 'facebook', 'instagram', 'twitter', 'schema.org', 'w3.org', 'example.com', 'png', 'jpg']): 
                    continue
                email_addr = e
                break
        except Exception as e:
            continue

        if email_addr:
            if email_addr.lower() in seen_emails:
                duplicate_count += 1
            else:
                seen_emails.add(email_addr.lower())
                leads.append({
                    "id": str(int(time.time()*1000))+str(random.randint(100,999)), 
                    "name": name, 
                    "email": email_addr, 
                    "status": "Pending", 
                    "generated_email": "", 
                    "follow_up_count": 0
                })
            
    save_json(LEADS_FILE, existing_leads + leads)
    
    # Debug message if no leads found at all
    if not leads and duplicate_count == 0:
        return jsonify({"error": f"Search found {len(business_names)} businesses but 0 email addresses. Google is likely blocking the bot. Please add a Serper.dev key in Settings for 100% reliability."}), 400

    return jsonify({"new_leads": leads, "duplicates": duplicate_count, "total_scanned": len(business_names)})

@app.route('/api/reminders', methods=['GET'])
def get_reminders():
    leads = load_json(LEADS_FILE, [])
    today = datetime.now().date()
    reminders = []
    for l in leads:
        if l.get('status') == 'Sent' and l.get('follow_up_date') and not l.get('follow_up_done'):
            f_date = datetime.strptime(l['follow_up_date'], '%Y-%m-%d').date()
            if f_date <= today:
                l['days_since_sent'] = (today - datetime.strptime(l['sent_date'], '%Y-%m-%d').date()).days
                reminders.append(l)
    return jsonify(reminders)

@app.route('/api/reminders/mark-done', methods=['POST'])
def mark_reminder_done():
    lead_id = request.json.get('lead_id')
    leads = load_json(LEADS_FILE, [])
    for l in leads:
        if l['id'] == lead_id: l['follow_up_done'] = True
    save_json(LEADS_FILE, leads)
    return jsonify({"status": "success"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
