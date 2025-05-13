#!/usr/bin/env python3
"""
Секретная система управления задачами - Сервер
Для развертывания на собственной машине
"""

import os
import json
import sqlite3
import logging
import hmac
import hashlib
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from telegram import Bot, WebAppInfo, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
import asyncio
from werkzeug.utils import secure_filename
from threading import Thread
from urllib.parse import parse_qs

# Конфигурация
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"  # Вставьте токен вашего бота
WEBAPP_URL = "http://localhost:5000"  # URL где будет доступен WebApp
ADMIN_IDS = [123456789]  # Telegram ID администраторов
UPLOAD_FOLDER = "uploads"
ALLOWED_EXTENSIONS = {'pdf', 'doc', 'docx', 'jpg', 'jpeg', 'png'}

# Создание директории для загрузок
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Инициализация Flask
app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-here'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB максимальный размер файла
CORS(app)

# Инициализация Telegram бота
bot = Bot(token=BOT_TOKEN)

class Database:
    """Класс для работы с базой данных"""
    
    def __init__(self, db_path="task_system.db"):
        self.db_path = db_path
        self.init_db()
    
    def get_connection(self):
        return sqlite3.connect(self.db_path)
    
    def init_db(self):
        """Инициализация таблиц базы данных"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Таблица сотрудников
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS agents (
                telegram_id INTEGER PRIMARY KEY,
                callsign TEXT NOT NULL,
                role TEXT DEFAULT 'agent',
                added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Таблица задач
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                agent_id INTEGER,
                creator_id INTEGER,
                status TEXT DEFAULT 'pending',
                created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_date TIMESTAMP,
                report_file TEXT,
                FOREIGN KEY (agent_id) REFERENCES agents(telegram_id)
            )
        ''')
        
        # Таблица оповещений
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                sender_id INTEGER,
                sent_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def add_agent(self, telegram_id, callsign, role='agent'):
        """Добавление нового сотрудника"""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT OR REPLACE INTO agents (telegram_id, callsign, role) VALUES (?, ?, ?)",
                (telegram_id, callsign, role)
            )
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error adding agent: {e}")
            return False
        finally:
            conn.close()
    
    def get_agent(self, telegram_id):
        """Получение информации о сотруднике"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM agents WHERE telegram_id = ?", (telegram_id,))
        result = cursor.fetchone()
        conn.close()
        return result
    
    def get_all_agents(self):
        """Получение списка всех сотрудников"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM agents")
        results = cursor.fetchall()
        conn.close()
        return results
    
    def create_task(self, title, description, agent_id, creator_id):
        """Создание новой задачи"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO tasks (title, description, agent_id, creator_id) VALUES (?, ?, ?, ?)",
            (title, description, agent_id, creator_id)
        )
        task_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return task_id
    
    def get_tasks(self, user_id=None):
        """Получение задач"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        if user_id:
            query = """
                SELECT t.*, a.callsign as agent_name 
                FROM tasks t 
                JOIN agents a ON t.agent_id = a.telegram_id 
                WHERE t.agent_id = ? OR t.creator_id = ?
                ORDER BY t.created_date DESC
            """
            cursor.execute(query, (user_id, user_id))
        else:
            query = """
                SELECT t.*, a.callsign as agent_name 
                FROM tasks t 
                JOIN agents a ON t.agent_id = a.telegram_id
                ORDER BY t.created_date DESC
            """
            cursor.execute(query)
        
        columns = [description[0] for description in cursor.description]
        results = []
        for row in cursor.fetchall():
            results.append(dict(zip(columns, row)))
        
        conn.close()
        return results
    
    def complete_task(self, task_id, report_file=None):
        """Отметка задачи как выполненной"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE tasks 
               SET status = 'completed', 
                   completed_date = CURRENT_TIMESTAMP,
                   report_file = ?
               WHERE id = ?""",
            (report_file, task_id)
        )
        conn.commit()
        conn.close()
    
    def get_task(self, task_id):
        """Получение информации о задаче"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        columns = [description[0] for description in cursor.description]
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return dict(zip(columns, row))
        return None
    
    def add_alert(self, code, sender_id):
        """Добавление записи об оповещении"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO alerts (code, sender_id) VALUES (?, ?)",
            (code, sender_id)
        )
        conn.commit()
        conn.close()

# Инициализация базы данных
db = Database()

def validate_init_data(init_data):
    """Проверка валидности данных от Telegram WebApp"""
    try:
        # Парсим данные
        parsed_data = parse_qs(init_data)
        
        # Извлекаем hash
        received_hash = parsed_data.get('hash', [''])[0]
        
        # Удаляем hash из данных для проверки
        data_check_string = []
        for key in sorted(parsed_data.keys()):
            if key != 'hash':
                for value in parsed_data[key]:
                    data_check_string.append(f"{key}={value}")
        
        data_check_string = '\n'.join(data_check_string)
        
        # Создаем секретный ключ
        secret_key = hmac.new(
            'WebAppData'.encode(), 
            BOT_TOKEN.encode(), 
            hashlib.sha256
        ).digest()
        
        # Проверяем подпись
        calculated_hash = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256
        ).hexdigest()
        
        return calculated_hash == received_hash
    except Exception as e:
        logger.error(f"Error validating init data: {e}")
        return False

# Маршруты Flask

@app.route('/')
def index():
    """Главная страница с WebApp"""
    return send_from_directory('.', 'index.html')

@app.route('/api/check_user', methods=['POST'])
def check_user():
    """Проверка пользователя и его прав"""
    data = request.json
    user_id = data.get('user_id')
    username = data.get('username')
    init_data = data.get('init_data')
    
    # Проверяем подпись данных
    if not validate_init_data(init_data):
        return jsonify({'success': False, 'error': 'Invalid init data'}), 403
    
    # Проверяем есть ли пользователь в базе
    agent = db.get_agent(user_id)
    
    if not agent:
        # Если пользователя нет, добавляем его
        db.add_agent(user_id, f"Агент-{username or user_id}")
        agent = db.get_agent(user_id)
    
    is_admin = user_id in ADMIN_IDS
    
    return jsonify({
        'success': True,
        'is_admin': is_admin,
        'agent': {
            'telegram_id': agent[0],
            'callsign': agent[1],
            'role': agent[2]
        }
    })

@app.route('/api/tasks', methods=['GET', 'POST'])
def tasks():
    """Получение списка задач или создание новой"""
    if request.method == 'GET':
        user_id = request.args.get('user_id')
        init_data = request.args.get('init_data')
        
        # Проверяем подпись
        if not validate_init_data(init_data):
            return jsonify({'success': False, 'error': 'Invalid init data'}), 403
        
        tasks_list = db.get_tasks(user_id)
        return jsonify({'success': True, 'tasks': tasks_list})
    
    elif request.method == 'POST':
        data = request.json
        init_data = data.get('init_data')
        
        # Проверяем подпись
        if not validate_init_data(init_data):
            return jsonify({'success': False, 'error': 'Invalid init data'}), 403
        
        title = data.get('title')
        description = data.get('description')
        agent_id = data.get('agent_id')
        creator_id = data.get('creator_id')
        
        # Создаем задачу
        task_id = db.create_task(title, description, agent_id, creator_id)
        
        # Отправляем уведомление исполнителю через Telegram
        send_task_notification(agent_id, title, description)
        
        return jsonify({'success': True, 'task_id': task_id})

@app.route('/api/tasks/complete', methods=['POST'])
def complete_task():
    """Отметка задачи как выполненной"""
    task_id = request.form.get('task_id')
    user_id = request.form.get('user_id')
    init_data = request.form.get('init_data')
    
    # Проверяем подпись
    if not validate_init_data(init_data):
        return jsonify({'success': False, 'error': 'Invalid init data'}), 403
    
    # Обработка загруженного файла
    report_file = None
    if 'report' in request.files:
        file = request.files['report']
        if file and allowed_file(file.filename):
            filename = secure_filename(f"report_{task_id}_{file.filename}")
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(file_path)
            report_file = filename
    
    # Обновляем статус задачи
    db.complete_task(task_id, report_file)
    
    # Получаем информацию о задаче
    task = db.get_task(task_id)
    
    # Отправляем уведомление создателю задачи
    if task:
        send_completion_notification(task['creator_id'], task['title'], report_file)
    
    return jsonify({'success': True})

@app.route('/api/agents', methods=['GET', 'POST'])
def agents():
    """Получение списка сотрудников или добавление нового"""
    if request.method == 'GET':
        init_data = request.args.get('init_data')
        
        # Проверяем подпись
        if not validate_init_data(init_data):
            return jsonify({'success': False, 'error': 'Invalid init data'}), 403
        
        agents_list = db.get_all_agents()
        agents_data = []
        for agent in agents_list:
            agents_data.append({
                'telegram_id': agent[0],
                'callsign': agent[1],
                'role': agent[2]
            })
        return jsonify({'success': True, 'agents': agents_data})
    
    elif request.method == 'POST':
        data = request.json
        init_data = data.get('init_data')
        
        # Проверяем подпись
        if not validate_init_data(init_data):
            return jsonify({'success': False, 'error': 'Invalid init data'}), 403
        
        telegram_id = data.get('telegram_id')
        callsign = data.get('callsign')
        
        success = db.add_agent(telegram_id, callsign)
        
        return jsonify({'success': success})

@app.route('/api/alerts', methods=['POST'])
def send_alert():
    """Отправка оповещения"""
    data = request.json
    init_data = data.get('init_data')
    
    # Проверяем подпись
    if not validate_init_data(init_data):
        return jsonify({'success': False, 'error': 'Invalid init data'}), 403
    
    code = data.get('code')
    sender_id = data.get('sender_id')
    
    # Сохраняем оповещение в базе
    db.add_alert(code, sender_id)
    
    # Отправляем оповещение всем сотрудникам
    send_alert_to_all(code, sender_id)
    
    return jsonify({'success': True})

# Вспомогательные функции

def allowed_file(filename):
    """Проверка разрешенных расширений файлов"""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def send_task_notification(agent_id, title, description):
    """Отправка уведомления о новой задаче"""
    try:
        message = f"🆕 *НОВАЯ ЗАДАЧА*\n\n" \
                 f"*{title}*\n{description}\n\n" \
                 f"Откройте систему для подробной информации."
        
        keyboard = [[InlineKeyboardButton("📋 Открыть систему", 
                                        web_app=WebAppInfo(url=WEBAPP_URL))]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Создаем асинхронную функцию для отправки
        async def send_message():
            await bot.send_message(
                chat_id=agent_id,
                text=message,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
        
        # Запускаем в отдельном потоке
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(send_message())
        loop.close()
        
    except TelegramError as e:
        logger.error(f"Error sending task notification: {e}")

def send_completion_notification(creator_id, task_title, report_file):
    """Отправка уведомления о выполнении задачи"""
    try:
        message = f"✅ *ЗАДАЧА ВЫПОЛНЕНА*\n\n" \
                 f"*{task_title}*\n"
        
        if report_file:
            message += f"Отчет: {report_file}"
        
        # Создаем асинхронную функцию для отправки
        async def send_message():
            await bot.send_message(
                chat_id=creator_id,
                text=message,
                parse_mode='Markdown'
            )
        
        # Запускаем в отдельном потоке
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(send_message())
        loop.close()
        
    except TelegramError as e:
        logger.error(f"Error sending completion notification: {e}")

def send_alert_to_all(code, sender_id):
    """Отправка оповещения всем сотрудникам"""
    alert_messages = {
        'RED': '🔴 КОД КРАСНЫЙ - ЭКСТРЕННАЯ СИТУАЦИЯ',
        'YELLOW': '🟡 КОД ЖЕЛТЫЙ - ПОВЫШЕННАЯ ГОТОВНОСТЬ',
        'BLUE': '🔵 КОД СИНИЙ - СБОР ЛИЧНОГО СОСТАВА'
    }
    
    agents = db.get_all_agents()
    sender = db.get_agent(sender_id)
    sender_name = sender[1] if sender else "Система"
    
    message = f"⚠️ *ОПОВЕЩЕНИЕ*\n\n" \
             f"{alert_messages.get(code, 'СИСТЕМНОЕ ОПОВЕЩЕНИЕ')}\n\n" \
             f"Отправитель: {sender_name}"
    
    # Создаем асинхронную функцию для отправки
    async def send_messages():
        for agent in agents:
            try:
                await bot.send_message(
                    chat_id=agent[0],
                    text=message,
                    parse_mode='Markdown'
                )
            except TelegramError as e:
                logger.error(f"Error sending alert to {agent[0]}: {e}")
    
    # Запускаем в отдельном потоке
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(send_messages())
    loop.close()

# Запуск сервера
if __name__ == '__main__':
    # Создаем администратора по умолчанию
    for admin_id in ADMIN_IDS:
        db.add_agent(admin_id, f"Администратор-{admin_id}", 'admin')
    
    logger.info(f"Starting server on {WEBAPP_URL}")
    logger.info("Don't forget to:")
    logger.info("1. Set your BOT_TOKEN")
    logger.info("2. Add your Telegram ID to ADMIN_IDS")
    logger.info("3. Use HTTPS for production")
    
    app.run(host='0.0.0.0', port=5000, debug=True)