import os
import re
import time
import threading
import imaplib
import email
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from flask import Flask, request, render_template, redirect, url_for, session, Response
import requests
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
import pickle
from queue import Queue

app = Flask(__name__, template_folder='templates')
app.secret_key = os.environ.get('SECRET_KEY', 'mysecretkey123')
OPENROUTER_API_KEY = os.environ.get('OPENROUTER_API_KEY', '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')

# Biến toàn cục
email_credentials = {"email": "", "password": ""}
planned_tasks = []
message_queue = Queue()  # Hàng đợi để lưu thông báo

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
            sender = msg["From"]
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                        break
            else:
                body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
            task = analyze_email(subject, body)
            if task:
                task["sender"] = sender
                tasks.append(task)
        mail.logout()
        print(f"[{datetime.now()}] Tìm thấy {len(tasks)} email hợp lệ.")
        return tasks
    except Exception as e:
        raise Exception(f"Lỗi khi đọc email: {str(e)}")

# Phân tích email
def analyze_email(subject, body):
    task = {"title": subject, "deadline": None, "description": body}
    deadline_match = re.search(r'due (\d{2}-\d{2}-\d{4})|due (\d{2}/\d{2}/\d{4})', body, re.IGNORECASE)
    if deadline_match:
        deadline = deadline_match.group(1) or deadline_match.group(2)
        deadline = deadline.replace('/', '-')
        try:
            datetime.strptime(deadline, "%d-%m-%Y")
            task["deadline"] = deadline
        except ValueError:
            print(f"[{datetime.now()}] Ngày {deadline} không hợp lệ.")
            return None
    return task if task["deadline"] else None

# Gửi email phản hồi
def send_response_email(to_email, task):
    try:
        msg = MIMEText(
            f"Xin chào,\n\n"
            f"Chúng tôi đã nhận được nhiệm vụ của bạn: '{task['title']}' với hạn chót {task['deadline']}.\n"
            f"Dưới đây là kế hoạch chi tiết:\n"
            f"- Tổng thời gian: {task['total_hours']} giờ\n"
            f"- Thời gian mỗi ngày: {task['hours_per_day']} giờ\n"
            f"- Số ngày làm: {task['days']} ngày\n"
            f"- Kế hoạch: {task['plan']}\n\n"
            f"Nhiệm vụ đã được thêm vào Google Calendar. Vui lòng kiểm tra!\n\n"
            f"Trân trọng,\nHệ thống AI Lập Kế Hoạch"
        )
        msg['Subject'] = f"Phản hồi: Kế hoạch cho '{task['title']}'"
        msg['From'] = email_credentials["email"]
        msg['To'] = to_email

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(email_credentials["email"], email_credentials["password"])
            server.send_message(msg)
        print(f"[{datetime.now()}] Đã gửi email phản hồi tới {to_email}")
    except Exception as e:
        print(f"[{datetime.now()}] Lỗi khi gửi email phản hồi: {str(e)}")

# Gọi OpenRouter API
def ai_plan_and_solve(tasks):
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    url = "https://openrouter.ai/api/v1/chat/completions"
    planned_tasks = []
    
    for task in tasks:
        deadline_date = datetime.strptime(task["deadline"], "%d-%m-%Y")
        days_until_deadline = (deadline_date - datetime.now()).days
        if days_until_deadline < 1:
            days_until_deadline = 1

        prompt = (
            f"Tạo kế hoạch chi tiết cho nhiệm vụ này:\n"
            f"Tiêu đề: {task['title']}\n"
            f"Mô tả: {task['description']}\n"
            f"Hạn chót: {task['deadline']} (định dạng DD-MM-YYYY)\n"
            f"Ước lượng thời gian hoàn thành (giờ) và phân bổ thời gian làm việc mỗi ngày trong {days_until_deadline} ngày."
        )
        data = {
            "model": "mistralai/mixtral-8x7b-instruct:free",
            "messages": [{"role": "user", "content": prompt}]
        }
        try:
            response = requests.post(url, headers=headers, json=data)
            response.raise_for_status()
            plan = response.json()["choices"][0]["message"]["content"]
            
            total_hours = extract_total_hours(plan) or 8
            hours_per_day = total_hours / days_until_deadline

            planned_task = {
                "title": task["title"],
                "deadline": task["deadline"],
                "description": task["description"],
                "plan": plan,
                "total_hours": total_hours,
                "hours_per_day": round(hours_per_day, 2),
                "days": days_until_deadline,
                "sender": task["sender"]
            }
            planned_tasks.append(planned_task)
            add_task_to_calendar(planned_task)
            send_response_email(task["sender"], planned_task)
        except Exception as e:
            print(f"[{datetime.now()}] Lỗi khi gọi OpenRouter: {str(e)}")
    return planned_tasks

# Trích xuất tổng giờ
def extract_total_hours(plan):
    match = re.search(r'tổng thời gian.*?(\d+\.?\d*) giờ', plan, re.IGNORECASE)
    return float(match.group(1)) if match else None

# Kiểm tra email định kỳ
def check_emails_periodically():
    global planned_tasks
    while True:
        if not email_credentials["email"] or not email_credentials["password"]:
            print(f"[{datetime.now()}] Chưa đăng nhập. Đang chờ...")
            time.sleep(60)
            continue
        
        try:
            message_queue.put("Bot đang đọc email...")  # Thông báo khi bắt đầu đọc
            tasks = get_emails(email_credentials["email"], email_credentials["password"])
            if tasks:
                print(f"[{datetime.now()}] Đã tìm thấy {len(tasks)} email mới.")
                new_planned_tasks = ai_plan_and_solve(tasks)
                if new_planned_tasks:
                    planned_tasks = new_planned_tasks
                message_queue.put(f"Đã xử lý xong {len(tasks)} email.")
            else:
                message_queue.put("Không có email mới hoặc nhiệm vụ hợp lệ.")
        except Exception as e:
            message_queue.put(f"Lỗi: {str(e)}")
            print(f"[{datetime.now()}] Lỗi trong quá trình kiểm tra email: {str(e)}")
        
        time.sleep(60)

# Google Calendar
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

def add_task_to_calendar(task):
    try:
        service = get_calendar_service()
        start_date = datetime.now().date()
        for day in range(task["days"]):
            event_date = start_date + timedelta(days=day)
            event = {
                'summary': f"{task['title']} - Ngày {day + 1}/{task['days']}",
                'description': (
                    f"Mô tả: {task.get('description', '')}\n"
                    f"Kế hoạch: {task.get('plan', '')}\n"
                    f"Thời gian làm hôm nay: {task['hours_per_day']} giờ\n"
                    f"Tổng thời gian: {task['total_hours']} giờ\n"
                    f"Số ngày làm: {task['days']} ngày"
                ),
                'start': {'date': event_date.strftime("%Y-%m-%d")},
                'end': {'date': event_date.strftime("%Y-%m-%d")}
            }
            service.events().insert(calendarId='primary', body=event).execute()
        print(f"[{datetime.now()}] Đã thêm nhiệm vụ '{task['title']}' vào Google Calendar.")
    except Exception as e:
        print(f"[{datetime.now()}] Lỗi khi thêm vào Google Calendar: {str(e)}")

# Endpoint SSE để gửi thông báo
@app.route('/stream')
def stream():
    def event_stream():
        while True:
            if not message_queue.empty():
                message = message_queue.get()
                yield f"data: {message}\n\n"
            time.sleep(1)  # Giảm tải CPU
    return Response(event_stream(), mimetype="text/event-stream")

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
        session['logged_in'] = True
        
        print(f"[{datetime.now()}] Đăng nhập thành công với {email_user}")
        if not any(t.name == 'email_thread' for t in threading.enumerate()):
            email_thread = threading.Thread(target=check_emails_periodically, name='email_thread', daemon=True)
            email_thread.start()
            print(f"[{datetime.now()}] Luồng kiểm tra email đã khởi động")
        
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
