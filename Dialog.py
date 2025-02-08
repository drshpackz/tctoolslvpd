from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from datetime import datetime, timedelta
import logging
import threading
import json
import requests
import time
import random
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from cachetools import TTLCache
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from concurrent.futures import ThreadPoolExecutor

# ------------------- Configuration & Setup -------------------

# Caching: TTLCache for user roles (5 minutes TTL)
user_cache = TTLCache(maxsize=1000, ttl=300)
cache = TTLCache(maxsize=1000, ttl=300)

GOOGLE_API_TIMEOUT = 10  # 10 seconds

# Flask setup
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

# Setup Flask-Limiter to limit incoming requests
limiter = Limiter(key_func=get_remote_address, default_limits=["100 per minute"])
limiter.init_app(app)

# Logging setup
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Google Sheets API setup
CREDENTIALS_FILE = 'credentials.json'  # Path to your credentials file
SPREADSHEET_ID = '1Q8AAuFuEHE85TdVTP5KUixmNVvExnegwyRXcwJHFQkI'
CADET_SPREADSHEET_ID = '11I6-tNbxB9NGSR91cqOXw9Lw4t1GyYmgRKS8QxWHWIk'
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

# Create a global service instance for synchronous calls
credentials = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
service = build('sheets', 'v4', credentials=credentials)

# Optionally, instantiate other service objects if needed
try:
    main_credentials = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    main_service = build('sheets', 'v4', credentials=main_credentials)

    lvpd_credentials = Credentials.from_service_account_file('lvpdcredentials.json', scopes=SCOPES)
    lvpd_service = build('sheets', 'v4', credentials=lvpd_credentials)
except Exception as e:
    logger.error(f"Error initializing Google Sheets services: {e}")
    raise

# VK API Token
VK_API_TOKEN = "vk1.a.sGIaaqKa8_6y0XTfy7nwqdVt6ySMmnE2QwRjzvdb0I1_y92I5yuh3hGjYtYzbQLDcJGOCxV3OszjLOhEP3m6OcCA-7ko4b-q9uMjVCh_b68N_4hm2bH38BAcFJAJzO0ppjrbv4e9DQaR7K3Mc4Wj_okEO-Ck3B7iaxY5pUoGnt1Iqaz5NnHSMsGl8HKjxN3eMm3kHr3uh1wv_IaFTOuenw"
VK_API_VERSION = "5.131"
CHAT_PEER_ID = 2000000001  # Your target chat ID

# Rate-limiting for actions (in minutes/count)
EDIT_LIMIT_MINUTES = 5
EDIT_LIMIT_COUNT = 2

# Thread pool for asynchronous tasks
executor = ThreadPoolExecutor(max_workers=10)

# Thread lock if needed for shared resources (not used if each thread creates its own service)
service_lock = threading.Lock()

# ------------------- Helper Functions -------------------

def get_sheet_data_range():
    # Adjust to match your sheet's structure
    return "'Экзамены LVPD'!A:I"

def append_to_sheet_with_comment(data):
    """Append a row to the Google Sheet and add a comment for evidence."""
    try:
        range_ = get_sheet_data_range()
        row_data = data[:5] + [data[5]] + data[6:8] + [data[8]]  # Use full evidence text
        body = {'values': [row_data]}

        result = service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=range_,
            valueInputOption='USER_ENTERED',
            body=body
        ).execute()

        # Add full evidence as a note
        updated_range = result['updates']['updatedRange']
        last_row_index = int(updated_range.split(':')[1][1:])  # Extract row number
        sheet_id = get_sheet_id("Экзамены LVPD")
        if not sheet_id:
            raise ValueError("Sheet ID could not be retrieved.")

        request_body = {
            "requests": [
                {
                    "updateCells": {
                        "rows": [
                            {
                                "values": [{"note": data[5]}]
                            }
                        ],
                        "fields": "note",
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": last_row_index - 1,
                            "endRowIndex": last_row_index,
                            "startColumnIndex": 5,
                            "endColumnIndex": 6
                        }
                    }
                }
            ]
        }

        service.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body=request_body
        ).execute()
        return True
    except Exception as e:
        app.logger.error(f"Error appending to Google Sheets: {e}")
        return False

def get_sheet_data(sheet_name=None):
    """Retrieve all data from a Google Sheet; if sheet_name provided, use that range."""
    try:
        range_ = f"'{sheet_name}'!A:Z" if sheet_name else get_sheet_data_range()
        response = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=range_,
            valueRenderOption='UNFORMATTED_VALUE',
            dateTimeRenderOption='FORMATTED_STRING'
        ).execute()
        rows = response.get('values', [])
        valid_rows = [row for row in rows if row and row[0].strip()]
        if sheet_name:
            cache[f"sheet_data_{sheet_name}"] = valid_rows
        return valid_rows
    except Exception as e:
        app.logger.error(f"Error retrieving sheet data: {e}")
        return []

def get_sheet_id(sheet_name):
    """Retrieve the sheet ID for a given sheet name."""
    try:
        response = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
        sheets = response.get('sheets', [])
        for sheet in sheets:
            if sheet['properties']['title'] == sheet_name:
                return sheet['properties']['sheetId']
        return None
    except Exception as e:
        app.logger.error(f"Error retrieving sheet ID: {e}")
        return None

def get_user_role(username):
    sheet_data = get_sheet_data("ScriptUserAuth")
    for row in sheet_data[1:]:
        if row[0].lower() == username.lower():
            return int(row[1])
    return None

# Function to send a message in VK chat
def send_message_to_vk(message):
    url = "https://api.vk.com/method/messages.send"
    params = {
        "access_token": VK_API_TOKEN,
        "v": VK_API_VERSION,
        "peer_id": CHAT_PEER_ID,
        "message": message,
        "random_id": 0
    }
    
    response = requests.post(url, params=params).json()
    return response

# ------------------- Rate-Limiting & Activity Tracking -------------------

user_activity_tracker = {}  # For tracking instructor activity

def is_action_allowed(username):
    role = get_user_role(username)
    if role == 3:
        return {"allowed": False, "reason": "Access Denied: User is blocked",
                "can_open": False, "can_edit": False, "can_edit_buttons": False}
    if role == 2:
        return {"allowed": True, "reason": "Admin access granted",
                "can_open": True, "can_edit": True, "can_edit_buttons": True}
    if role == 1:
        now = datetime.now()
        if username not in user_activity_tracker:
            user_activity_tracker[username] = []
        user_activity_tracker[username] = [ts for ts in user_activity_tracker[username]
                                           if now - ts < timedelta(minutes=EDIT_LIMIT_MINUTES)]
        if len(user_activity_tracker[username]) < EDIT_LIMIT_COUNT:
            user_activity_tracker[username].append(now)
            return {"allowed": True, "reason": "Instructor access granted",
                    "can_open": True, "can_edit": False, "can_edit_buttons": True}
        return {"allowed": True, "reason": "Access granted: Edit limit exceeded, view only",
                "can_open": True, "can_edit": False, "can_edit_buttons": False}
    return {"allowed": False, "reason": "User role not recognized",
            "can_open": False, "can_edit": False, "can_edit_buttons": False}

# ------------------- Flask Routes -------------------

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/api/auth', methods=['POST'])
@limiter.limit("10 per minute")  # Limit this endpoint
def check_auth():
    data = request.json
    username = data.get("username")
    if not username:
        return jsonify({"error": "Username is required"}), 400

    timestamp = datetime.now().strftime('%d.%m.%y // %H:%M:%S')
    try:
        cached_data = user_cache.get(username.lower())
        if cached_data:
            role, last_seen = cached_data
        else:
            sheet_data = get_sheet_data("ScriptUserAuth")
            user_row = next((row for row in sheet_data if row[0].lower() == username.lower()), None)
            if user_row:
                role = int(user_row[1])
                last_seen = user_row[3] if len(user_row) > 3 else None
            else:
                role = 0
                last_seen = timestamp
                new_row = [username, role, '', timestamp]
                service.spreadsheets().values().append(
                    spreadsheetId=SPREADSHEET_ID,
                    range="'ScriptUserAuth'!A:D",
                    valueInputOption='USER_ENTERED',
                    body={'values': [new_row]}
                ).execute()
            user_cache[username.lower()] = (role, last_seen)

       

        if role == 3:
            permissions = {"error": "Access Denied: User is blocked",
                           "can_open": False, "can_edit": False, "can_edit_buttons": False,
                           "status_code": 403}
        elif role == 2:
            permissions = {"message": "Admin access granted",
                           "can_open": True, "can_edit": True, "can_edit_buttons": True,
                           "status_code": 200}
        elif role == 1:
            now = datetime.now()
            if username not in user_activity_tracker:
                user_activity_tracker[username] = []
            user_activity_tracker[username] = [
                ts for ts in user_activity_tracker[username]
                if now - ts < timedelta(minutes=EDIT_LIMIT_MINUTES)
            ]
            if len(user_activity_tracker[username]) < EDIT_LIMIT_COUNT:
                user_activity_tracker[username].append(now)
                permissions = {"message": "Instructor access granted",
                               "can_open": True, "can_edit": False, "can_edit_buttons": True,
                               "status_code": 200}
            else:
                permissions = {"message": "Instructor access granted, edit limit exceeded",
                               "can_open": True, "can_edit": False, "can_edit_buttons": False,
                               "status_code": 200}
        elif role == 0:
            permissions = {"message": "User added to pending list",
                           "can_open": False, "can_edit": False, "can_edit_buttons": False,
                           "status_code": 403}
        else:
            permissions = {"error": "User role not recognized",
                           "can_open": False, "can_edit": False, "can_edit_buttons": False,
                           "status_code": 403}
        return jsonify(permissions), permissions.get('status_code', 200)
    except Exception as e:
        app.logger.error(f"Error in check_auth: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/api/add_user', methods=['POST'])
@limiter.limit("5 per minute")
def add_user():
    try:
        data = request.json
        username = data.get("username")
        role = data.get("role", 0)
        if not username:
            return jsonify({"error": "Username is required"}), 400

        sheet_data = get_sheet_data("ScriptUserAuth")
        for row in sheet_data[1:]:
            if row[0].lower() == username.lower():
                return jsonify({"message": "User already exists"}), 200

        new_row = [username, role]
        body = {'values': [new_row]}
        service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="'ScriptUserAuth'!A:B",
            valueInputOption='USER_ENTERED',
            body=body
        ).execute()
        return jsonify({"message": "User added to pending list"}), 200
    except Exception as e:
        app.logger.error(f"Error adding user: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/dialogue', methods=['POST'])
@limiter.limit("20 per minute")
def receive_dialogue():
    try:
        raw_data = request.data
        app.logger.info(f"Received raw data: {raw_data}")

        data = json.loads(raw_data.decode('utf-8'))
        app.logger.info(f"Decoded JSON: {json.dumps(data, ensure_ascii=False, indent=4)}")

        # Extract messages and format them properly
        formatted_messages = []
        for msg in data.get('messages', []):
            chat_type = msg.get('type', 'normal')
            speaker = msg.get('speaker', 'Unknown')
            text = msg.get('text', '')

            formatted_messages.append(f"[{chat_type}] {speaker}: {text}")

        # Properly join messages for Google Sheets
        dialogue_text = "\n".join(formatted_messages) if formatted_messages else "No evidence provided"
        app.logger.info(f"Formatted Messages:\n{dialogue_text}")

        # Insert into Google Sheets
        timestamp = datetime.now().strftime('%d.%m.%Y %H:%M:%S')
        row_data = [
            timestamp,
            data.get('logged_user_nickname', 'Unknown'),
            data.get('instructor_nickname', 'Unknown'),
            data.get('purpose', 'Dialogue'),
            data.get('rating', 'N/A'),
            dialogue_text,  # Now properly formatted messages
            'N/A',
            'На рассмотрении',
            ''
        ]

        success = append_to_sheet_with_comment(row_data)

        if success:
            return jsonify({"message": "Dialogue added successfully"}), 200
        else:
            return jsonify({"error": "Failed to append dialogue to Google Sheets"}), 500

    except Exception as e:
        app.logger.error(f"Error in receive_dialogue: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/send_cadet_info', methods=['POST'])
def send_cadet_info():
    data = request.json
    requester = data.get('requester', 'Unknown')
    cadet_name = data.get('cadetName', 'Unknown')
    forma = data.get('forma', 'Unknown').upper()  # Convert to uppercase
    lecture = data.get('lecture', 'Not Passed')
    theory = data.get('theory', 'Not Passed')
    traffic_stop = data.get('trafficStop', 'Not Passed')
    arrest = data.get('arrest', 'Not Passed')
    note = data.get('note', '')

    # Reformat the message
    formatted_message = (
        "\n"
        "━━━━━━ 🚔 TC-TOOLS  ━━━━━━\n"
        "\n"
        f"👤 ЗАЯВИТЕЛЬ: {requester}\n"
        f"🎖 КАДЕТ: {cadet_name}\n"
        f"📚 ФОРМА ОБУЧЕНИЯ: {forma}\n"
        "\n"
        "📖 ПРОЙДЕННЫЕ ЭТАПЫ:\n"
        f"{'✅' if lecture == 'Passed' else '❌'} ЛЕКЦИЯ\n"
        f"{'✅' if theory == 'Passed' else '❌'} ТЕОРИЯ\n"
        f"{'✅' if traffic_stop == 'Passed' else '❌'} ТРАФИК-СТОП (10-55)\n"
        f"{'✅' if arrest == 'Passed' else '❌'} АРЕСТ\n"
        "\n"
        "📝 КОММЕНТАРИЙ:\n"
        f"➡ {requester}: {note}\n"
        "\n"
        "━━━━━━ 🚔 TC-TOOLS  ━━━━━━\n"
        "\n"
    )

    response = send_message_to_vk(formatted_message)
    
    if 'error' in response:
        return jsonify({"status": "failed", "error": response['error']['error_msg']}), 500
    else:
        return jsonify({"status": "success", "message": "Cadet information sent to VK chat"}), 200

@app.route('/api/update_status', methods=['POST'])
@limiter.limit("20 per minute")
def update_status():
    try:
        data = request.json
        if not data or 'timestamp' not in data or 'reviewer' not in data or 'status' not in data:
            return jsonify({"error": "Invalid data. 'timestamp', 'reviewer', and 'status' are required."}), 400
        success = update_sheet_row(data['timestamp'], data['reviewer'], data['status'])
        if success:
            return jsonify({"message": "Status updated successfully"}), 200
        else:
            return jsonify({"error": "Record not found for the given timestamp"}), 404
    except Exception as e:
        app.logger.error(f"Error in update_status: {e}")
        return jsonify({"error": str(e)}), 500

def update_sheet_row(timestamp, reviewer, status):
    try:
        data = get_sheet_data()
        for row_index, row in enumerate(data[1:], start=2):  # Skip header row
            if row[0] == timestamp:
                update_range = f"'Экзамены LVPD'!G{row_index}:H{row_index}"
                body = {'values': [[reviewer, status]]}
                service.spreadsheets().values().update(
                    spreadsheetId=SPREADSHEET_ID,
                    range=update_range,
                    valueInputOption='USER_ENTERED',
                    body=body
                ).execute()
                return True
        return False
    except Exception as e:
        app.logger.error(f"Error updating Google Sheets: {e}")
        return False

@app.route('/api/pending', methods=['GET'])
@limiter.limit("30 per minute")
def get_pending_records():
    try:
        data = get_sheet_data("Экзамены LVPD")
        pending_records = []
        for row in data[1:]:
            if len(row) > 7 and row[7] == "На рассмотрении":
                timestamp = row[0] if len(row) > 0 and row[0] else "Unknown Timestamp"
                cadet = row[2] if len(row) > 2 and row[2] else "Unknown Cadet"
                instructor = row[1] if len(row) > 1 and row[1] else "Unknown Instructor"
                event_type = row[3] if len(row) > 3 and row[3] else "Unknown Event Type"
                score = row[4] if len(row) > 4 and row[4] else "No Score"
                evidence = row[5] if len(row) > 5 and row[5] else "No evidence provided"
                reviewer = row[6] if len(row) > 6 and row[6] else "No Reviewer"
                status = row[7]
                notes = row[8] if len(row) > 8 and row[8] else "No Notes"
                pending_records.append([timestamp, cadet, instructor, event_type, score, evidence, reviewer, status, notes])
        return jsonify(pending_records), 200
    except Exception as e:
        app.logger.error(f"Error fetching pending records: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/approved', methods=['GET'])
@limiter.limit("30 per minute")
def get_approved_records():
    try:
        data = get_sheet_data()
        approved_records = [row for row in data[1:] if len(row) > 7 and row[7] == 'Одобрено']
        return jsonify(approved_records), 200
    except Exception as e:
        app.logger.error(f"Error fetching approved records: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/declined', methods=['GET'])
@limiter.limit("30 per minute")
def get_declined_records():
    try:
        data = get_sheet_data()
        declined_records = [row for row in data[1:] if len(row) > 7 and row[7] == 'Отклонено']
        return jsonify(declined_records), 200
    except Exception as e:
        app.logger.error(f"Error fetching declined records: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/cadet_corps', methods=['GET'])
@limiter.limit("30 per minute")
def get_cadet_corps():
    try:
        range_ = "'CadetsSysLog'!A:F"
        response = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=range_,
            valueRenderOption='FORMATTED_VALUE'
        ).execute()
        rows = response.get('values', [])
        if not rows:
            return jsonify({"error": "No data found in CadetsSysLog"}), 404

        headers = [header.strip().lower().replace(" ", "_") for header in rows[0]]
        header_mapping = {
            "nick_names": "nickname",
            "nick_name": "nickname",
            "nick names": "nickname",
            "nick name": "nickname",
            "lecture": "lecture",
            "teory": "theory",
            "1055": "1055",
            "arrest": "arrest",
            "forma": "forma"
        }
        standardized_headers = [header_mapping.get(h, h) for h in headers]
        processed_data = []
        for row in rows[1:]:
            if not any(row):
                continue
            cadet_data = {
                "nickname": row[standardized_headers.index("nickname")] if "nickname" in standardized_headers else "Unknown",
                "lecture": (True if row[standardized_headers.index("lecture")] == "TRUE" else False
                            if row[standardized_headers.index("lecture")] == "FALSE" else None)
                            if "lecture" in standardized_headers else None,
                "theory": (True if row[standardized_headers.index("theory")] == "TRUE" else False
                           if row[standardized_headers.index("theory")] == "FALSE" else None)
                           if "theory" in standardized_headers else None,
                "1055": (True if row[standardized_headers.index("1055")] == "TRUE" else False
                         if row[standardized_headers.index("1055")] == "FALSE" else None)
                         if "1055" in standardized_headers else None,
                "arrest": (True if row[standardized_headers.index("arrest")] == "TRUE" else False
                           if row[standardized_headers.index("arrest")] == "FALSE" else None)
                           if "arrest" in standardized_headers else None,
                "forma": row[standardized_headers.index("forma")] if "forma" in standardized_headers else "unknown"
            }
            processed_data.append(cadet_data)
        return jsonify({"success": True, "data": processed_data}), 200
    except Exception as e:
        app.logger.error(f"Error fetching Cadet Corps data: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/check_online', methods=['POST'])
@limiter.limit("30 per minute")
def check_online():
    try:
        data = request.json
        online_players = data.get('online_players', [])

        # Запрашиваем данные из таблицы (с таймаутом)
        range_ = "'CadetsSysLog'!A:F"
        try:
            response = service.spreadsheets().values().get(
                spreadsheetId=SPREADSHEET_ID,
                range=range_,
                valueRenderOption='FORMATTED_VALUE'
            ).execute(timeout=GOOGLE_API_TIMEOUT)
        except requests.exceptions.Timeout:
            logging.error("Google Sheets API timeout")
            return jsonify({"success": False, "error": "Google Sheets API timeout"}), 500

        rows = response.get('values', [])
        if not rows:
            return jsonify({"success": True, "online_cadets": []})

        # Формируем список кадетов, которые онлайн
        cadets = rows[1:]
        online_cadets = []
        for row in cadets:
            if row[0].lower().strip().replace('_', ' ') in [p.lower() for p in online_players]:
                online_cadets.append({
                    "nickname": row[0] if len(row) > 0 else "Unknown",
                    "lecture": row[1] == "TRUE" if len(row) > 1 else False,
                    "theory": row[2] == "TRUE" if len(row) > 2 else False,
                    "1055": row[3] == "TRUE" if len(row) > 3 else False,
                    "arrest": row[4] == "TRUE" if len(row) > 4 else False,
                    "forma": row[5] if len(row) > 5 else "Unknown"
                })

        return jsonify({"success": True, "online_cadets": online_cadets})

    except Exception as e:
        logging.error(f"Error in /api/check_online: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

# ------------------- Main Application Runner -------------------

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)