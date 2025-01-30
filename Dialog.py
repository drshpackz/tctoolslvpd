from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from datetime import datetime, timedelta
import logging
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from functools import wraps
import urllib.parse

# Flask setup
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})  # Allow all origins

# Logging setup
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Google Sheets API setup
CREDENTIALS_FILE = 'credentials.json'  # Path to your credentials file
SPREADSHEET_ID = '1Q8AAuFuEHE85TdVTP5KUixmNVvExnegwyRXcwJHFQkI'  # Your spreadsheet ID
CADET_SPREADSHEET_ID = '11I6-tNbxB9NGSR91cqOXw9Lw4t1GyYmgRKS8QxWHWIk'

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
credentials = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
service = build('sheets', 'v4', credentials=credentials)

try:
    main_credentials = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    main_service = build('sheets', 'v4', credentials=main_credentials)

    lvpd_credentials = Credentials.from_service_account_file('lvpdcredentials.json', scopes=SCOPES)
    lvpd_service = build('sheets', 'v4', credentials=lvpd_credentials)
except Exception as e:
    logger.error(f"Error initializing Google Sheets services: {e}")
    raise


# Role-based Access
user_activity_tracker = {}  # Tracks user actions for rate limiting
EDIT_LIMIT_MINUTES = 5
EDIT_LIMIT_COUNT = 2

# Helper function to get the range for Google Sheets
def get_sheet_data_range():
    return "'Экзамены LVPD'!A:I"  # Adjust to match your sheet's structure

# Function to append data to Google Sheets
def append_to_sheet_with_comment(data):
    """Append a row to the Google Sheet and add a comment for evidence."""
    try:
        range_ = get_sheet_data_range()

        # Prepare data with full evidence text
        row_data = data[:5] + [data[5]] + data[6:8] + [data[8]]  # Use full evidence text
        body = {'values': [row_data]}

        # Append row
        result = service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=range_,
            valueInputOption='USER_ENTERED',
            body=body
        ).execute()

        # Add full evidence as a note
        updated_range = result['updates']['updatedRange']
        last_row_index = int(updated_range.split(':')[1][1:])  # Extract the row number
        sheet_id = get_sheet_id("Экзамены LVPD")
        if not sheet_id:
            raise ValueError("Sheet ID could not be retrieved.")

        request_body = {
            "requests": [
                {
                    "updateCells": {
                        "rows": [
                            {
                                "values": [
                                    {"note": data[5]}  # Full evidence text as a note
                                ]
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

# Function to fetch all data from Google Sheets
def get_sheet_data(sheet_name=None):
    """Retrieve all data from a Google Sheet. If sheet_name is provided, fetch data from the specified sheet."""
    try:
        range_ = f"'{sheet_name}'!A:Z" if sheet_name else get_sheet_data_range()
        response = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=range_,
            valueRenderOption='UNFORMATTED_VALUE',  # This will return the full text of cells, including those with notes
            dateTimeRenderOption='FORMATTED_STRING'
        ).execute()
        # Filter out rows that are empty or don't have sufficient columns
        rows = response.get('values', [])
        valid_rows = [row for row in rows if len(row) > 0 and row[0].strip()]
        return valid_rows
    except Exception as e:
        app.logger.error(f"Error retrieving sheet data: {e}")
        return []

def get_user_role(username):
    sheet_data = get_sheet_data("ScriptUserAuth")
    for row in sheet_data[1:]:
        if row[0].lower() == username.lower():
            return int(row[1])  # Return the role
    return None  # Return None if the user is not found

def is_action_allowed(username):
    """Check if a user is allowed to perform an action."""
    role = get_user_role(username)

    if role == 3:  # Blocked user
        return {
            "allowed": False,
            "reason": "Access Denied: User is blocked",
            "can_open": False,
            "can_edit": False,
            "can_edit_buttons": False,  # Blocked user has no button access
        }

    if role == 2:  # Admin
        return {
            "allowed": True,
            "reason": "Admin access granted",
            "can_open": True,
            "can_edit": True,  # Admins have full edit access
            "can_edit_buttons": True,  # Admins can edit status buttons
        }

    if role == 1:  # Instructor
        now = datetime.now()
        if username not in user_activity_tracker:
            user_activity_tracker[username] = []

        # Clean up timestamps older than the limit
        user_activity_tracker[username] = [
            ts for ts in user_activity_tracker[username]
            if now - ts < timedelta(minutes=EDIT_LIMIT_MINUTES)
        ]

        if len(user_activity_tracker[username]) < EDIT_LIMIT_COUNT:
            user_activity_tracker[username].append(now)
            return {
                "allowed": True,
                "reason": "Access granted: Instructor edit allowed",
                "can_open": True,
                "can_edit": False,  # Instructor cannot make global edits
                "can_edit_buttons": True,  # Instructors can edit status buttons
            }

        # Exceeded edit limit, can view but not edit
        return {
            "allowed": True,
            "reason": "Access granted: Edit limit exceeded, view only",
            "can_open": True,
            "can_edit": False,  # View-only access
            "can_edit_buttons": False,  # No button access in view-only mode
        }

    # Default case for unrecognized roles
    return {
        "allowed": False,
        "reason": "User role not recognized",
        "can_open": False,
        "can_edit": False,
        "can_edit_buttons": False,
    }
# Function to fetch notess from Google Sheets
def get_sheet_notes(sheet_name):
    """
    Retrieve all rows and their notes from the specified sheet.
    :param sheet_name: The name of the sheet to retrieve data from.
    :return: A list of rows with their notes included.
    """
    try:
        # Get the sheet ID for the specified sheet name
        sheet_id = get_sheet_id(sheet_name)
        if not sheet_id:
            raise ValueError(f"Sheet ID for {sheet_name} could not be retrieved.")

        # Fetch all data including notes
        response = service.spreadsheets().get(
            spreadsheetId=SPREADSHEET_ID,
            fields="sheets(data(rowData(values(note,userEnteredValue))))"
        ).execute()

        # Extract notes and user-entered values
        rows = []
        for row_data in response["sheets"][0]["data"][0]["rowData"]:
            row = []
            for cell in row_data.get("values", []):
                user_value = cell.get("userEnteredValue", {}).get("stringValue", "")
                note = cell.get("note", "")
                row.append({"value": user_value, "note": note})
            rows.append(row)

        return rows
    except Exception as e:
        app.logger.error(f"Error retrieving sheet notes: {e}")
        return []

# Function to get the sheetId for a specific sheet
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

# Function to update a row in Google Sheets
def update_sheet_row(timestamp, reviewer, status):
    try:
        data = get_sheet_data()
        for row_index, row in enumerate(data[1:], start=2):  # Skip header
            if row[0] == timestamp:
                update_range = f"'Экзамены LVPD'!G{row_index}:H{row_index}"  # Columns G and H
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

# Flask Routes
@app.route('/')
def home():
    return render_template('index.html')

@app.route('/api/auth', methods=['POST'])
def check_auth():
    """Authenticate user and determine their permissions."""
    data = request.json
    username = data.get("username")
    if not username:
        return jsonify({"error": "Username is required"}), 400

    role = get_user_role(username)

    if role is None:
        # Add the user to the pending list if not found
        try:
            new_row = [username, 0]  # Add username with role=0 (pending)
            body = {'values': [new_row]}
            service.spreadsheets().values().append(
                spreadsheetId=SPREADSHEET_ID,
                range="'ScriptUserAuth'!A:B",
                valueInputOption='USER_ENTERED',
                body=body
            ).execute()
            return jsonify({
                "message": "User added to pending list",
                "can_open": False,
                "can_edit": False,
                "can_edit_buttons": False  # Explicitly set to False
            }), 403
        except Exception as e:
            app.logger.error(f"Error adding user to pending list: {e}")
            return jsonify({"error": "Failed to add user to pending list"}), 500

    if role == 3:  # Blocked user
        return jsonify({
            "error": "Access Denied: User is blocked",
            "can_open": False,
            "can_edit": False,
            "can_edit_buttons": False  # Explicitly set to False
        }), 403

    if role == 2:  # Admin
        return jsonify({
            "message": "Admin access granted",
            "can_open": True,
            "can_edit": True,
            "can_edit_buttons": True  # Admin has full button access
        }), 200

    if role == 1:  # Instructor
        now = datetime.now()
        if username not in user_activity_tracker:
            user_activity_tracker[username] = []

        # Clean up timestamps older than the limit
        user_activity_tracker[username] = [
            ts for ts in user_activity_tracker[username]
            if now - ts < timedelta(minutes=EDIT_LIMIT_MINUTES)
        ]

        if len(user_activity_tracker[username]) < EDIT_LIMIT_COUNT:
            user_activity_tracker[username].append(now)
            return jsonify({
                "message": "Access granted: Instructor edit allowed",
                "can_open": True,
                "can_edit": False,  # Instructor cannot make global edits
                "can_edit_buttons": True  # Instructors can edit status buttons
            }), 200

        # Exceeded edit limit, can view but not edit
        return jsonify({
            "message": "Access granted: Edit limit exceeded, view only",
            "can_open": True,
            "can_edit": False,
            "can_edit_buttons": False  # No button access in view-only mode
        }), 200

    # Default case for unrecognized roles
    return jsonify({
        "error": "User role not recognized",
        "can_open": False,
        "can_edit": False,
        "can_edit_buttons": False
    }), 403

@app.route('/api/add_user', methods=['POST'])
def add_user():
    """Add a new user to the Google Sheet with 'pending' access."""
    try:
        data = request.json
        username = data.get("username")
        role = data.get("role", 0)  # Default role to 0 (pending)

        if not username:
            return jsonify({"error": "Username is required"}), 400

        # Check if the user already exists
        sheet_data = get_sheet_data("ScriptUserAuth")
        for row in sheet_data[1:]:
            if row[0].lower() == username.lower():
                return jsonify({"message": "User already exists"}), 200

        # Append the new user with pending status
        new_row = [username, role]  # Add username and role
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
def receive_dialogue():
    """Receive a dialogue entry and append it to Google Sheets."""
    try:
        app.logger.info(f"Received request: {request.data.decode('utf-8')}")

        data = request.json if request.is_json else {
            'instructor_nickname': 'Unknown',
            'logged_user_nickname': 'Unknown',
            'purpose': 'Dialogue',
            'text': request.data.decode('utf-8'),
            'rating': 0
        }

        app.logger.info(f"Processed data: {data}")

        timestamp = datetime.now().strftime('%d.%m.%Y %H:%M:%S')
        row_data = [
            timestamp,
            data.get('logged_user_nickname', 'Unknown'),
            data.get('instructor_nickname', 'Unknown'),
            data.get('purpose', 'Dialogue'),
            data.get('rating', 'N/A'),
            data.get('text', 'No evidence provided'),
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

@app.route('/api/update_status', methods=['POST'])
def update_status():
    """Update the status and reviewer of a record in Google Sheets."""
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

@app.route('/api/pending', methods=['GET'])
def get_pending_records():
    """Fetch rows with the status 'На рассмотрении'."""
    try:
        # Fetch all data from the Google Sheet
        data = get_sheet_data("Экзамены LVPD")
        pending_records = []

        for row in data[1:]:  # Skip the header row
            # Check if the status column (index 7) has 'На рассмотрении'
            if len(row) > 7 and row[7] == "На рассмотрении":
                # Safeguard against missing or empty fields
                timestamp = row[0] if len(row) > 0 and row[0] else "Unknown Timestamp"
                cadet = row[2] if len(row) > 2 and row[2] else "Unknown Cadet"
                instructor = row[1] if len(row) > 1 and row[1] else "Unknown Instructor"
                event_type = row[3] if len(row) > 3 and row[3] else "Unknown Event Type"
                score = row[4] if len(row) > 4 and row[4] else "No Score"
                evidence = row[5] if len(row) > 5 and row[5] else "No evidence provided"
                reviewer = row[6] if len(row) > 6 and row[6] else "No Reviewer"
                status = row[7]
                notes = row[8] if len(row) > 8 and row[8] else "No Notes"

                # Construct row data
                row_data = [
                    timestamp,
                    cadet,
                    instructor,
                    event_type,
                    score,
                    evidence,
                    reviewer,
                    status,
                    notes
                ]
                pending_records.append(row_data)

        return jsonify(pending_records), 200
    except Exception as e:
        app.logger.error(f"Error fetching pending records: {e}")
        return jsonify({"error": str(e)}), 500






@app.route('/api/approved', methods=['GET'])
def get_approved_records():
    """Fetch rows with the status 'Одобрено'."""
    try:
        data = get_sheet_data()
        approved_records = [
            row for row in data[1:] if len(row) > 7 and row[7] == 'Одобрено'
        ]
        return jsonify(approved_records), 200
    except Exception as e:
        app.logger.error(f"Error fetching approved records: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/declined', methods=['GET'])
def get_declined_records():
    """Fetch rows with the status 'Отклонено'."""
    try:
        data = get_sheet_data()
        declined_records = [
            row for row in data[1:] if len(row) > 7 and row[7] == 'Отклонено'
        ]
        return jsonify(declined_records), 200
    except Exception as e:
        app.logger.error(f"Error fetching declined records: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/cadet_corps', methods=['GET'])
def get_cadet_corps():
    """Fetch structured data from the 'CadetsSysLog' tab with support for Nick_Name & Nick Name."""
    try:
        range_ = "'CadetsSysLog'!A:F"  # Ensure the correct sheet range
        
        response = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=range_,
            valueRenderOption='FORMATTED_VALUE'
        ).execute()

        rows = response.get('values', [])
        
        if not rows:
            return jsonify({"error": "No data found in CadetsSysLog"}), 404

        # Extract headers (first row) and normalize them
        headers = [header.strip().lower().replace(" ", "_") for header in rows[0]]

        # Map different variations of headers to standardized keys
        header_mapping = {
            "nick_names": "nickname",
            "nick_name": "nickname",
            "nick names": "nickname",
            "nick name": "nickname",
            "lecture": "lecture",
            "teory": "theory",  # Fix "teory" -> "theory"
            "1055": "1055",
            "arrest": "arrest",
            "forma": "forma"
        }

        # Translate headers to standard names
        standardized_headers = [header_mapping.get(h, h) for h in headers]

        processed_data = []
        for row in rows[1:]:  # Skip header row
            if not any(row):  # Skip empty rows
                continue

            # Assign "null" if the field is missing or not required for the cadet
            cadet_data = {
                "nickname": row[standardized_headers.index("nickname")] if "nickname" in standardized_headers else "Unknown",
                "lecture": (
                    True if row[standardized_headers.index("lecture")] == "TRUE" 
                    else False if row[standardized_headers.index("lecture")] == "FALSE" 
                    else None
                ) if "lecture" in standardized_headers else None,
                "theory": (
                    True if row[standardized_headers.index("theory")] == "TRUE" 
                    else False if row[standardized_headers.index("theory")] == "FALSE" 
                    else None
                ) if "theory" in standardized_headers else None,
                "1055": (
                    True if row[standardized_headers.index("1055")] == "TRUE" 
                    else False if row[standardized_headers.index("1055")] == "FALSE" 
                    else None
                ) if "1055" in standardized_headers else None,
                "arrest": (
                    True if row[standardized_headers.index("arrest")] == "TRUE" 
                    else False if row[standardized_headers.index("arrest")] == "FALSE" 
                    else None
                ) if "arrest" in standardized_headers else None,
                "forma": row[standardized_headers.index("forma")] if "forma" in standardized_headers else "unknown"
            }
            processed_data.append(cadet_data)

        return jsonify({
            "success": True,
            "data": processed_data
        }), 200

    except Exception as e:
        app.logger.error(f"Error fetching Cadet Corps data: {e}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/api/fetch_cadet_info', methods=['POST'])
def fetch_cadet_info():
    try:
        data = request.json
        nickname = data.get('nickname')

        if not nickname:
            return jsonify({'success': False, 'error': 'No nickname provided'}), 400

        # Normalize the nickname
        normalized_nickname = nickname.lower().replace('_', ' ')

        # Fetch cadets from Google Sheets
        range_ = "'CadetsSysLog'!A:F"
        response = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=range_,
            valueRenderOption='FORMATTED_VALUE'
        ).execute()

        rows = response.get('values', [])
        if not rows:
            return jsonify({'success': False, 'error': 'No data found in CadetsSysLog'}), 404

        # Find the cadet
        cadet = None
        for row in rows[1:]:  # Skip header row
            if len(row) > 0 and row[0].lower().replace('_', ' ') == normalized_nickname:
                cadet = {
                    'nickname': row[0],
                    'forma': row[5] if len(row) > 5 else 'Unknown',
                    'lecture': row[1] == 'TRUE' if len(row) > 1 else False,
                    'theory': row[2] == 'TRUE' if len(row) > 2 else False,
                    '1055': row[3] == 'TRUE' if len(row) > 3 else False,
                    'arrest': row[4] == 'TRUE' if len(row) > 4 else False
                }
                break

        if cadet:
            return jsonify({'success': True, 'cadet': cadet})
        else:
            return jsonify({'success': False, 'error': 'Cadet not found'}), 404

    except Exception as e:
        app.logger.error(f"Error in fetch_cadet_info: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    
@app.route('/api/check_online', methods=['POST'])
def check_online():
    """Compare online players with the cadets table."""
    try:
        data = request.json
        online_players = data.get('online_players', [])

        # Debug: Log received data
        app.logger.info(f"Received online players: {online_players}")

        # Normalize player names: replace underscores with spaces and convert to lowercase
        normalized_online_players = [name.lower().replace('_', ' ') for name in online_players]

        # Debug: Log normalized names
        app.logger.info(f"Normalized online players: {normalized_online_players}")

        # Fetch cadets from Google Sheets
        range_ = "'CadetsSysLog'!A:F"
        response = service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=range_,
            valueRenderOption='FORMATTED_VALUE'
        ).execute()

        rows = response.get('values', [])
        if not rows:
            return jsonify({"success": True, "online_cadets": [], "debug_received_players": online_players})

        # Match online players with cadets
        cadets = rows[1:]  # Skip header row
        online_cadets = []
        for row in cadets:
            if len(row) > 0:
                sheet_nickname = row[0].lower().strip()
                if sheet_nickname in normalized_online_players:
                    cadet = {
                        "nickname": row[0],  # Keep original capitalization
                        "lecture": row[1] == "TRUE" if len(row) > 1 else False,
                        "theory": row[2] == "TRUE" if len(row) > 2 else False,
                        "1055": row[3] == "TRUE" if len(row) > 3 else False,
                        "arrest": row[4] == "TRUE" if len(row) > 4 else False,
                        "forma": row[5] if len(row) > 5 else "unknown"
                    }
                    online_cadets.append(cadet)

        # Debug: Log matched cadets
        app.logger.info(f"Matched online cadets: {online_cadets}")

        return jsonify({
            "success": True, 
            "online_cadets": online_cadets, 
            "debug_received_players": online_players
        })
    except Exception as e:
        app.logger.error(f"Error in /api/check_online: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

