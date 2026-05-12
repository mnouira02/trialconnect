import datetime
import sqlite3, os, re, math
from werkzeug.security import generate_password_hash
from flask import g
import secrets, requests, warnings

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'trialconnect', 'data', 'trial_connect.db')

# --- Constant for allowed file types ---
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

def get_db():
    """Opens a new database connection if there is none yet for the current application context."""
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

def close_db(e=None):
    """Closes the database again at the end of the request."""
    db = g.pop('db', None)
    if db is not None:
        db.close()

def init_db():
    """Initializes the database and creates tables if they don't exist."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    
    db = sqlite3.connect(DB_PATH)
    cursor = db.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            firstName TEXT NOT NULL,
            lastName TEXT NOT NULL,
            email TEXT NOT NULL,
            message TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            firstName TEXT NOT NULL,
            lastName TEXT NOT NULL,
            birthYear INTEGER,
            sex TEXT,
            password_hash TEXT,
            auth_provider TEXT NOT NULL,
            provider_id TEXT,
            profile_picture_url TEXT,
            remember_token TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reset_token TEXT,
            reset_token_expiration TIMESTAMP
        )
    """)

        # Stores the master list of promoted study IDs ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS promoted_studies (
            nct_id TEXT PRIMARY KEY NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # --- Logs when a promoted study is returned in a search ---
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS promotion_analytics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nct_id TEXT NOT NULL,
            search_term TEXT NOT NULL,
            view_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db.commit()
    db.close()
    print("Database initialized successfully.")

def add_contact_message(firstName, lastName, email, message):
    """Inserts a new contact message into the database."""
    db = get_db()
    db.execute(
        "INSERT INTO contacts (firstName, lastName, email, message) VALUES (?, ?, ?, ?)",
        (firstName, lastName, email, message)
    )
    db.commit()

def get_or_create_user(email, firstName, lastName, birthYear=None, sex=None, auth_provider='local', profile_picture_url=None, provider_id=None, password=None):
    """Finds an existing user or creates a new one, generates a remember_token, and returns the user as a dict."""
    db = get_db()
    
    user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

    if user:
        return dict(user)
    
    password_hash = None
    if password:
        password_hash = generate_password_hash(password)

    remember_token = secrets.token_hex(16)

    cursor = db.execute(
        """
        INSERT INTO users (email, firstName, lastName, birthYear, sex, password_hash, auth_provider, provider_id, profile_picture_url, remember_token)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (email, firstName, lastName, birthYear, sex, password_hash, auth_provider, provider_id, profile_picture_url, remember_token)
    )
    db.commit()
    
    new_user = db.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(new_user)

def get_user_by_email(email):
    """Finds a user by their email address and returns their data or None."""
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if user:
        return dict(user)
    return None

def get_user_by_remember_token(token):
    """Finds a user by their remember_token."""
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE remember_token = ?", (token,)).fetchone()
    if user:
        return dict(user)
    return None

def validate_password_strength(password):
    """
    Returns a list of error messages if the password is weak, 
    or an empty list if it's strong.
    """
    errors = []
    if len(password) < 8:
        errors.append("Password must be at least 8 characters long.")
    if not re.search(r"[A-Z]", password):
        errors.append("Password must contain at least one uppercase letter.")
    if not re.search(r"[a-z]", password):
        errors.append("Password must contain at least one lowercase letter.")
    if not re.search(r"[0-9]", password):
        errors.append("Password must contain at least one number.")
    return errors

def allowed_file(filename):
    """Checks if the uploaded file has an allowed extension."""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_user_by_email(email):
    """Finds a user by their email address and returns their data or None."""
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if user:
        return dict(user)
    return None

def get_user_by_id(user_id):
    """Finds a user by their ID and returns their data or None."""
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user:
        return dict(user)
    return None

def search_clinical_trials(query, user_lat, user_lon, radius=100, unit="km"):
    """
    Searches the ClinicalTrials.gov API for a given query, filtering for
    studies that are not yet recruiting or are actively recruiting AND
    are within a specified radius of the user's location.
    """

    base_url = "https://clinicaltrials.gov/api/v2/studies"

    # 1. Validate the unit. Default to 'km' if anything else is passed.
    safe_unit = "mi" if str(unit).lower() == "mi" else "km"
    
    # 2. Validate the radius. Ensure it's a number, fall back to 100 otherwise.
    try:
        # Values from forms often come as strings
        safe_radius = int(radius)
    except (ValueError, TypeError):
        safe_radius = 200

    # Format the geographic filter string for the API
    geo_filter = f"distance({user_lat},{user_lon},{safe_radius}{safe_unit})"

    params = {
        "query.term": query,
        "filter.overallStatus": "RECRUITING,NOT_YET_RECRUITING,AVAILABLE",
        "filter.geo": geo_filter,  
        "pageSize": 1000, 
        "format": "json"
    }

    try:
        response = requests.get(base_url, params=params, verify=False)
        response.raise_for_status()  
        data = response.json()
    except requests.exceptions.RequestException as e:
        print(f"API request failed: {e}")
        return []

    formatted_trials = []
    if "studies" in data:
        for study in data["studies"]:
            protocol = study.get("protocolSection", {})

            # --- Extract Basic Information ---
            id_module = protocol.get("identificationModule", {})
            status_module = protocol.get("statusModule", {})
            conditions_module = protocol.get("conditionsModule", {})
            
            # --- Extract Location Information ---
            locations_list = []
            contacts_locations_module = protocol.get("contactsLocationsModule", {})
            api_locations = contacts_locations_module.get("locations", [])

            if api_locations:
                for loc in api_locations:
                    location_data = {
                        "facility": loc.get("facility"),
                        "city": loc.get("city"),
                        "state": loc.get("state"),
                        "zip": loc.get("zip"),
                        "country": loc.get("country"),
                        "geoPoint": loc.get("geoPoint")
                    }
                    locations_list.append({k: v for k, v in location_data.items() if v is not None})

            # --- Assemble the final dictionary for this trial ---
            formatted_trials.append({
                "nctId": id_module.get("nctId"),
                "title": id_module.get("briefTitle"),
                "status": status_module.get("overallStatus"),
                "conditions": ", ".join(conditions_module.get("conditions", [])),
                "locations": locations_list
            })
            
    return formatted_trials

def haversine(lat1, lon1, lat2, lon2):
    """Calculate the distance in kilometers between two points on Earth."""
    R = 6371  # Earth radius in km
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def get_location_from_ip(ip_address):
    """
    Gets approximate location from an IP address using a free service.
    NOTE: In a real-world application, consider a more robust, paid service.
    This will not work for '127.0.0.1', so we use a fallback for local testing.
    """
    # For local development, this will use a public IP to get a sample location.
    if ip_address == '127.0.0.1':
        ip_address = '8.8.8.8'

    try:
        # Use a free and simple IP geolocation API
        response = requests.get(f"http://ip-api.com/json/{ip_address}?fields=status,lat,lon")
        response.raise_for_status()
        data = response.json()
        if data.get('status') == 'success':
            return data.get('lat'), data.get('lon')
    except requests.exceptions.RequestException:
        return None, None # Handle API errors gracefully
    return None, None

# --- NEW PROMOTION HELPER FUNCTIONS ---

def get_all_promoted_studies():
    """Gets all promoted studies for the admin panel list."""
    db = get_db()
    studies = db.execute("SELECT * FROM promoted_studies ORDER BY added_at DESC").fetchall()
    return [dict(study) for study in studies]

def get_all_promoted_studies_set():
    """Gets a Python Set of all promoted NCT IDs for fast O(1) lookups during sorting."""
    db = get_db()
    # Fetchall() returns a list of Row objects (tuples). We select the first item [0] from each tuple.
    studies_tuples = db.execute("SELECT nct_id FROM promoted_studies").fetchall()
    return {item[0] for item in studies_tuples}

def add_promoted_study(nct_id):
    """Adds a new NCT ID to the promoted list. IGNOREs duplicates due to PRIMARY KEY."""
    db = get_db()
    try:
        db.execute("INSERT INTO promoted_studies (nct_id) VALUES (?)", (nct_id.strip().upper(),))
        db.commit()
    except sqlite3.IntegrityError:
        # This just means the NCT ID is already in the list, which is fine.
        pass

def remove_promoted_study(nct_id):
    """Removes an NCT ID from the promoted list."""
    db = get_db()
    db.execute("DELETE FROM promoted_studies WHERE nct_id = ?", (nct_id,))
    db.commit()

def log_promotion_analytic(nct_id, search_term):
    """Logs that a specific promoted study was returned for a search term."""
    db = get_db()
    db.execute(
        "INSERT INTO promotion_analytics (nct_id, search_term) VALUES (?, ?)",
        (nct_id, search_term)
    )
    db.commit()

def check_user_study_match(user_profile, nct_id):
    """
    Checks a logged-in user's profile (age/sex) against a study's eligibility criteria.
    Returns: 'MATCH', 'NO_MATCH', or 'NO_DATA'
    """
    
    # 1. Check User Profile
    if not user_profile or not user_profile.get('birthYear') or not user_profile.get('sex'):
        # We can't make a determination if the user is missing data
        return {'status': 'NO_DATA', 'reason': 'User profile incomplete.'}

    try:
        current_year = datetime.datetime.now().year
        user_age = current_year - int(user_profile['birthYear'])
        user_sex = user_profile['sex'].upper() # 'MALE', 'FEMALE', etc.
    except (TypeError, ValueError):
        return {'status': 'NO_DATA', 'reason': 'Invalid user profile data.'}


    # 2. Fetch Study Eligibility Data from API
    try:
        url = f"https://clinicaltrials.gov/api/v2/studies/{nct_id}?fields=EligibilityModule"
        # Suppress insecure request warning (same as your search function)
        warnings.filterwarnings('ignore', message='Unverified HTTPS request')
        response = requests.get(url, timeout=5, verify=False)
        response.raise_for_status()
        data = response.json()
        eligibility = data.get("protocolSection", {}).get("eligibilityModule", {})
    except requests.exceptions.RequestException as e:
        print(f"Failed to fetch eligibility for {nct_id}: {e}")
        return {'status': 'NO_DATA', 'reason': 'API fetch failed.'}

    if not eligibility:
        return {'status': 'NO_DATA', 'reason': 'No eligibility data available.'}

    
    # 3. Perform Match Logic
    
    # --- SEX MATCH ---
    is_sex_match = False
    study_sex = eligibility.get('sex', 'ALL').upper()
    
    if study_sex == 'ALL':
        is_sex_match = True
    elif study_sex == 'MALE' and user_sex == 'MALE':
        is_sex_match = True
    elif study_sex == 'FEMALE' and user_sex == 'FEMALE':
        is_sex_match = True
    # Note: This logic conservatively counts 'Non-binary' etc. as NO_MATCH 
    # unless the study explicitly accepts 'ALL'.

    # --- AGE MATCH ---
    is_age_match = False
    min_age_str = eligibility.get('minimumAge', '0 Years')
    max_age_str = eligibility.get('maximumAge') # Can be None

    # Use regex to find the first number in the age string
    min_age_match = re.search(r'\d+', min_age_str)
    min_age = int(min_age_match.group(0)) if min_age_match else 0

    max_age = 150 # Default to a super-high number if no max is listed
    if max_age_str:
        max_age_match = re.search(r'\d+', max_age_str)
        if max_age_match:
            max_age = int(max_age_match.group(0))

    if user_age >= min_age and user_age <= max_age:
        is_age_match = True

    # --- FINAL DECISION ---
    if is_age_match and is_sex_match:
        return {'status': 'MATCH', 'reason': f'User (Age: {user_age}, Sex: {user_sex}) matches study (Age: {min_age}-{max_age}, Sex: {study_sex})'}
    else:
        # Provide a reason for the mismatch
        reason = "User did not match: "
        if not is_age_match:
            reason += f"Age ({user_age}) outside range ({min_age}-{max_age}). "
        if not is_sex_match:
            reason += f"Sex ({user_sex}) does not match study requirement ({study_sex})."
        return {'status': 'NO_MATCH', 'reason': reason}