import os
import re
import time
import threading
import imaplib
import email
from datetime import datetime
from flask import Flask, request, render_template, redirect, url_for, session
import requests
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
import pickle

app = Flask(__name__, template_folder='templates')
app.secret_key = os.environ.get('SECRET_KEY', 'mysecretkey123')
OPENROUTER_API_KEY = os.environ.get('OPENROUTER_API_KEY', 'your_openrouter_api_key_here')

# Biến toàn cục
email_credentials = {"email": "", "password": ""}
planned_tasks = []

# Hàm đọc email qua IMAP
def get_emails(email_user, email_pass):
    tasks = []
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(email_user, email_pass)
        mail.select("inbox")
        status, data = mail.search(None, "UNSEEN")
        if status != "OK":
            print(f"[{datetime.now()}] Không tìm thấy email nào.")
            return tasks

        email_ids = data[0].split()
        for email_id in email_ids:
            status, msg_data = mail.fetch(email_id, "(RFC822)")
            if status != "OK":
                continue
            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)
            subject = msg["Subject"]
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                        break
            else:
                body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
            task = analyze_email(subject, body)
            if task:
                tasks.append(task)
        mail.logout()
        print(f"[{datetime.now()}] Tìm thấy {len(tasks)} email hợp lệ.")
        return tasks
    except Exception as e:
        raise Exception(f"Lỗi khi đọc email: {str(e)}")

# Phân tích email để tìm deadline
def analyze_email(subject, body):
    task = {"title": subject, "deadline": None, "description": body}
    deadline_match = re.search(r'due (\d{4}-\d{2}-\d{2})', body, re.IGNORECASE)
    if deadline_match:
        task["deadline"] = deadline_match.group(1)
    return task if task["deadline"] else None

# Gọi OpenRouter API để lập kế hoạch
def ai_plan_and_solve(tasks):
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    url = "https://openrouter.ai/api/v1/chat/completions"
    planned_tasks = []
    
    for task in tasks:
        prompt = f"Create a detailed plan for this task:\nTitle: {task['title']}\nDescription: {task['description']}\nDeadline: {task['deadline']}"
        data = {
            "model": "mistralai/mixtral-8x7b-instruct:free",
            "messages": [{"role": "user", "content": prompt}]
        }
        try:
            response = requests.post(url, headers=headers, json=data)
            response.raise_for_status()
            plan = response.json()["choices"][0]["message"]["content"]
            planned_tasks.append({
                "title": task["title"],
                "deadline": task["deadline"],
                "description": task["description"],
                "plan": plan
            })
        except Exception as e:
            print(f"[{datetime.now()}] Lỗi khi gọi OpenRouter: {str(e)}")
    return planned_tasks

# Kiểm tra email định kỳ
def check_emails_periodically():
    global planned_tasks
    while True:
        if not email_credentials["email"] or not email_credentials["password"]:
            print(f"[{datetime.now()}] Chưa đăng nhập. Đang chờ...")
            time.sleep(60)
            continue
        
        try:
            tasks = get_emails(email_credentials["email"], email_credentials["password"])
            if tasks:
                print(f"[{datetime.now()}] Đã tìm thấy {len(tasks)} email mới.")
                new_planned_tasks = ai_plan_and_solve(tasks)
                if new_planned_tasks:
                    planned_tasks = new_planned_tasks
            else:
                print(f"[{datetime.now()}] Không có email mới hoặc task hợp lệ.")
        except Exception as e:
            print(f"[{datetime.now()}] Lỗi trong quá trình kiểm tra email: {str(e)}")
        
        time.sleep(60)

# Google Calendar (tùy chọn, giữ nguyên nếu cần)
def get_calendar_service():
    creds = None
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', ['https://www.googleapis.com/auth/calendar'])
            creds = flow.run_local_server(port=0)
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)
    return build('calendar', 'v3', credentials=creds)

def add_to_calendar(service, plan):
    event = {
        'summary': plan['title'],
        'description': plan.get('description', '') + '\n\nPlan:\n' + plan.get('plan', ''),
        'start': {'date': plan['deadline']},
        'end': {'date': plan['deadline']}
    }
    service.events().insert(calendarId='primary', body=event).execute()

# Routes
@app.route('/')
def index():
    return render_template('index.html', error=None)

@app.route('/login', methods=['POST'])
def login():
    email_user = request.form['email']
    email_pass = request.form['password']
    
    try:
        tasks = get_emails(email_user, email_pass)
        email_credentials["email"] = email_user
        email_credentials["password"] = email_pass
        session['logged_in'] = True  # Dùng session để kiểm tra trạng thái đăng nhập
        
        print(f"[{datetime.now()}] Đăng nhập thành công với {email_user}")
        if not any(t.name == 'email_thread' for t in threading.enumerate()):
            email_thread = threading.Thread(target=check_emails_periodically, name='email_thread', daemon=True)
            email_thread.start()
            print(f"[{datetime.now()}] Thread kiểm tra email đã khởi động")
        
        return redirect(url_for('dashboard'))
    except Exception as e:
        error_msg = f"Đăng nhập thất bại: {str(e)}. Vui lòng kiểm tra email/mật khẩu."
        print(f"[{datetime.now()}] {error_msg}")
        return render_template('index.html', error=error_msg)

@app.route('/dashboard')
def dashboard():
    if not session.get('logged_in'):
        return redirect(url_for('index'))
    return render_template('dashboard.html', plans=planned_tasks)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))