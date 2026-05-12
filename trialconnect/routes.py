# trialconnect/routes.py

import secrets
from datetime import datetime, timedelta, timezone
from flask import get_flashed_messages, render_template, request, redirect, url_for, session, current_app, abort, flash, make_response, jsonify
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
import os, uuid,requests
from .oauth_setup import oauth
from helpers import (
    add_contact_message, get_or_create_user, get_db, get_user_by_email, 
    validate_password_strength, allowed_file, 
    get_user_by_id, search_clinical_trials, get_location_from_ip, haversine, 
    remove_promoted_study, get_all_promoted_studies, get_all_promoted_studies_set, 
    add_promoted_study, log_promotion_analytic, check_user_study_match 
)

# Get the app instance from the current application context
app = current_app

# --- Page Routes ---
@app.route('/')
def index():
    query = request.args.get('query')
    
    # We ONLY get the fallback location from IP. The real search happens later.
    user_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    fallback_lat, fallback_lon = get_location_from_ip(user_ip)

    # Set a hard-coded fallback just in case the IP lookup also fails
    if not fallback_lat or not fallback_lon:
        fallback_lat = 40.7128 # Example: New York
        fallback_lon = -74.0060

    # Send the empty page with only the query and fallback location
    return render_template(
        "index.html", 
        query=query, 
        results=[],
        fallback_lat=fallback_lat,
        fallback_lon=fallback_lon,
        google_maps_api_key=current_app.config['GOOGLE_MAPS_API_KEY']
    )

@app.route('/api/search')
def api_search():
    query = request.args.get('query')
    # Get the PRECISE coordinates sent from the browser JavaScript
    user_lat = request.args.get('lat', type=float)
    user_lon = request.args.get('lon', type=float)

    search_results = []
    
    if not query:
        return jsonify({"error": "A query is required."}), 400
    if not user_lat or not user_lon:
        return jsonify({"error": "A location is required."}), 400

    try:
        # 1. This is your existing logic: Get raw API results
        search_results_raw = search_clinical_trials(
            query, 
            user_lat, 
            user_lon, 
            radius=200, 
            unit="km"
        )

        # 2. This is your existing logic: Calculate distance for each study
        # We'll put them in a new list, as the user's original logic did.
        processed_studies = []
        for study in search_results_raw:
            min_distance = float('inf')
            for location in study.get('locations', []):
                if location.get('geoPoint'):
                    dist = haversine(
                        user_lat, user_lon,
                        location['geoPoint']['lat'], location['geoPoint']['lon']
                    )
                    if dist < min_distance:
                        min_distance = dist
            
            if min_distance != float('inf'):
                study['closest_distance_km'] = round(min_distance)
                processed_studies.append(study) # Only add studies that have a valid distance
            # Note: This logic correctly filters out studies that are in the API 
            # but outside the Haversine distance you check (e.g. API geo-filter is a square, haversine is a circle)

        # 3. Get the set of all promoted NCT IDs for a fast lookup
        promoted_set = get_all_promoted_studies_set()

        promoted_list = []
        regular_list = []

        # 4. Separate all studies into two lists: promoted and regular
        for study in processed_studies:
            if study['nctId'] in promoted_set:
                study['is_promoted'] = True
                promoted_list.append(study)
                # This fulfills your analytics request:
                log_promotion_analytic(study['nctId'], query)
            else:
                study['is_promoted'] = False
                regular_list.append(study)

        # 5. Sort BOTH lists by distance (maintaining your original sort order)
        promoted_list.sort(key=lambda x: x.get('closest_distance_km', float('inf')))
        regular_list.sort(key=lambda x: x.get('closest_distance_km', float('inf')))

        # 6. Combine the lists, with promoted studies always first.
        final_sorted_list = promoted_list + regular_list
        
        # 7. Return the final sorted data as JSON
        return jsonify(final_sorted_list)

    except Exception as e:
        print(f"Error in API search: {e}")
        return jsonify({"error": "An error occurred while searching."}), 500

@app.route('/about')
def about():
    return render_template("about.html")

@app.route('/faq')
def faq():
    return render_template("faq.html")

@app.route('/terms')
def terms():
    return render_template("terms.html")

@app.route('/privacy')
def privacy():
    return render_template("privacy.html")

@app.route('/thank_you')
def thank_you():
    return render_template("thank_you.html")

@app.route('/contact', methods=['GET', 'POST'])
def contact():
    error = None
    if request.method == 'POST':
        firstName = request.form.get('firstName')
        lastName = request.form.get('lastName')
        email = request.form.get('email')
        message = request.form.get('message')

        if not all([firstName, lastName, email, message]):
            error = "All fields are required. Please fill out the entire form."
        else:
            add_contact_message(firstName, lastName, email, message)
            return redirect(url_for('thank_you'))
    
    # Pass the logged-in user's data to the template
    return render_template("contact.html", error=error)

# --- Authentication Routes ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        remember = request.form.get('rememberMe') == 'on' # Check the checkbox

        user = get_user_by_email(email)
        if user and check_password_hash(user['password_hash'], password):
            session['user'] = user
            flash('You have been logged in successfully.', 'success')
            
            # Create a response object
            response = make_response(redirect(url_for('index')))
            
            # If "Remember Me" is checked, set the cookie
            if remember:
                # Set a cookie that expires in 30 days
                response.set_cookie('remember_token', user['remember_token'], max_age=30*24*60*60)
            
            return response
        else:
            flash('Invalid email or password. Please try again.', 'danger')
            
    return render_template("login.html")

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        # Get the first and last name from the form
        first_name = request.form.get('firstName')
        last_name = request.form.get('lastName')
        birthYear = request.form.get('birthYear')
        sex = request.form.get('sex') 
        agreement_agreed = request.form.get('agreement')
        email = request.form.get('email')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')

        # Check for existing user
        existing_user = get_user_by_email(email)
        if existing_user:
            # User exists, so check their password
            if check_password_hash(existing_user['password_hash'], password):
                # Password is correct, so log them in directly
                session['user'] = existing_user
                flash('Welcome back! You have been logged in successfully.', 'success')
                return redirect(url_for('index'))
            else:
                # Email exists, but password is wrong. Redirect to login.
                message = 'An account with this email already exists. Please <a href="/login" class="alert-link">log in</a> or reset your password.'
                flash(message, 'warning')
                return redirect(url_for('login'))

        # Validate password strength
        strength_errors = validate_password_strength(password)
        if strength_errors:
            for error in strength_errors:
                flash(error, 'danger')
            return render_template('register.html')

        # Check if passwords match
        if password != confirm_password:
            flash("Passwords do not match. Please try again.", 'danger')
            return render_template('register.html')
        
        if not agreement_agreed:
            flash("You must agree to the Terms and Privacy Policy to register.", "danger")
            return render_template('register.html', now=datetime.now())

        # Pass the data to the helper function
        user = get_or_create_user(
            email=email,
            firstName=first_name,
            lastName=last_name,
            birthYear=birthYear,
            sex=sex,
            password=password,
            auth_provider='local'
        )

        # Log the user in by setting the session
        session['user'] = user
        return redirect(url_for('index'))
        
    return render_template("register.html", now=datetime.now())

@app.route('/login/google')
def login_google():
    redirect_uri = url_for('authorize', _external=True)
    return oauth.google.authorize_redirect(redirect_uri)

@app.route('/authorize')
def authorize():
    token = oauth.google.authorize_access_token()
    user_info = oauth.google.userinfo()

    first_name = user_info.get('given_name')
    last_name = user_info.get('family_name')
    if not first_name or not last_name:
        full_name = user_info.get('name', '').split(' ', 1)
        first_name = full_name[0]
        last_name = full_name[1] if len(full_name) > 1 else ''
    
    # Pass the picture URL to the helper function
    user = get_or_create_user(
        email=user_info['email'],
        firstName=first_name,
        lastName=last_name,
        auth_provider='google',
        provider_id=user_info['sub'],
        profile_picture_url=user_info.get('picture') # Get the picture from Google
    )
    
    session['user'] = user
    return redirect(url_for('index'))

@app.route('/logout')
def logout():
    session.pop('user', None)
    
    # Create a response object to clear the cookie
    response = make_response(redirect(url_for('index')))
    response.set_cookie('remember_token', '', expires=0)
    
    return response

@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email')
        user = get_user_by_email(email)

        if user:
            otp = ''.join(secrets.choice('0123456789') for i in range(6))
            
            # We'll store the OTP with the user's email to keep track of it
            session[f'otp_for_{email}'] = otp
            
            otp_hash = generate_password_hash(otp)
            expiration = datetime.utcnow() + timedelta(minutes=10)

            db = get_db()
            db.execute(
                "UPDATE users SET reset_token = ?, reset_token_expiration = ? WHERE id = ?",
                (otp_hash, expiration, user['id'])
            )
            db.commit()

            flash("If an account with that email exists, a password reset code has been sent.", "success")
        else:
            flash("If an account with that email exists, a password reset code has been sent.", "success")

        return redirect(url_for('index'))

    return render_template("forgot_password.html")

@app.route('/reset_with_token', methods=['GET', 'POST'])
def reset_with_token():
    if request.method == 'POST':
        email = request.form.get('email')
        otp = request.form.get('otp')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')

        user = get_user_by_email(email)

        # --- Existing validation for user, OTP, and expiration ---
        if not user or not user['reset_token'] or not check_password_hash(user['reset_token'], otp):
            flash("Invalid email or reset code.", "danger")
            return redirect(url_for('reset_with_token'))

        expiration_time = datetime.fromisoformat(user['reset_token_expiration']).replace(tzinfo=timezone.utc)
        if expiration_time < datetime.now(timezone.utc):
            flash("The reset code has expired. Please request a new one.", "danger")
            session.pop(f'otp_for_{email}', None)
            return redirect(url_for('forgot_password'))
        
        # --- Server-side password strength validation ---
        strength_errors = validate_password_strength(new_password)
        if strength_errors:
            for error in strength_errors:
                flash(error, 'danger')
            return redirect(url_for('reset_with_token')) # Redirect back if weak

        if new_password != confirm_password:
            flash("Passwords do not match.", "danger")
            return redirect(url_for('reset_with_token'))
            
        new_password_hash = generate_password_hash(new_password)
        db = get_db()
        db.execute(
            "UPDATE users SET password_hash = ?, reset_token = NULL, reset_token_expiration = NULL WHERE id = ?",
            (new_password_hash, user['id'])
        )
        db.commit()

        session.pop(f'otp_for_{email}', None)

        flash("Your password has been successfully reset. Please log in.", "success")
        return redirect(url_for('login'))

    return render_template("reset_with_token.html")

@app.route('/profile', methods=['GET', 'POST'])
def profile():
    if 'user' not in session:
        flash("You must be logged in to view this page.", "warning")
        return redirect(url_for('login'))

    user_id = session['user']['id']
    
    if request.method == 'POST':
        db = get_db()
        
        # Get ALL the profile fields from the form
        firstName = request.form.get('firstName')
        lastName = request.form.get('lastName')
        birthYear = request.form.get('birthYear')
        sex = request.form.get('sex')

        # This SQL query is now corrected to update all four fields
        db.execute(
            "UPDATE users SET firstName = ?, lastName = ?, birthYear = ?, sex = ? WHERE id = ?",
            (firstName, lastName, birthYear, sex, user_id)
        )

        
        if 'profile_picture' in request.files:
            file = request.files['profile_picture']
            if file and file.filename and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                unique_filename = str(uuid.uuid4()) + "_" + filename
                filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], unique_filename)
                file.save(filepath)
                db.execute("UPDATE users SET profile_picture_url = ? WHERE id = ?", (url_for('static', filename=f'profile_pictures/{unique_filename}'), user_id))
        
        # --- Update Password ---
        new_password = request.form.get('new_password')
        if new_password:
            current_password = request.form.get('current_password')
            confirm_password = request.form.get('confirm_password')
            user = get_user_by_id(user_id)
            
            if not user or not user.get('password_hash') or not check_password_hash(user['password_hash'], current_password):
                flash("Your current password was incorrect.", "danger")
            elif validate_password_strength(new_password):
                flash("Your new password does not meet the strength requirements.", "danger")
            elif new_password != confirm_password:
                flash("The new passwords do not match.", "danger")
            else:
                # All password checks passed
                new_password_hash = generate_password_hash(new_password)
                db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_password_hash, user_id))
                flash("Your password was updated successfully!", "success")

        db.commit()
        
        # Refresh session and redirect
        session['user'] = get_user_by_id(user_id)
        
        # Only flash "profile saved" if we didn't just flash a password message
        password_flashed = any(cat in ['success', 'danger'] for cat, msg in (get_flashed_messages(with_categories=True) or []) if "password" in msg.lower())
        if not password_flashed:
            flash("Your profile has been saved.", "success")
            
        return redirect(url_for('profile'))

    user_data = get_user_by_id(user_id)
    return render_template(
        "profile.html", 
        user=user_data, 
        now=datetime.now(timezone.utc)
    )

@app.route('/delete_account', methods=['POST'])
def delete_account():
    # Ensure user is logged in
    if 'user' not in session:
        abort(403) 
    
    user_id = session['user']['id']
    email = session['user']['email']

    # Prevent the primary admin from deleting their own account from this form
    if email == 'frenchieeap@gmail.com':
        flash("The primary admin account cannot be deleted.", "danger")
        return redirect(url_for('profile'))

    # Delete the user from the database
    db = get_db()
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()

    # Log the user out and clear the session completely
    session.clear()

    flash("Your account has been permanently deleted.", "success")
    return redirect(url_for('index'))

# --- Admin Routes ---
@app.route('/admin')
def admin():
    if not session.get('user') or session['user'].get('email') != 'frenchieeap@gmail.com':
        abort(403)

    db = get_db()
    users_raw = db.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    contacts = db.execute("SELECT * FROM contacts ORDER BY id DESC").fetchall()
    
    # 1. Get the raw list from the DB
    promoted_list_raw = get_all_promoted_studies()
    
    # 2. Process the promoted list to convert timestamps
    promoted_list = []
    for study_row in promoted_list_raw:
        study = dict(study_row) # Convert Row to dict
        try:
            # Use strptime to parse the 'YYYY-MM-DD HH:MM:SS' format from SQLite
            study['added_at'] = datetime.strptime(study['added_at'], '%Y-%m-%d %H:%M:%S')
        except (ValueError, TypeError, KeyError):
            # Fallback if the timestamp is somehow invalid
            study['added_at'] = datetime.now(timezone.utc)
        promoted_list.append(study)

    
    # --- Pre-process user data ---
    processed_users = []
    for user_row in users_raw:
        user = dict(user_row)
        user['token_status'] = 'none' # Default status

        if user.get('reset_token_expiration'):
            try:
                expiration_time = datetime.fromisoformat(user['reset_token_expiration']) 
                
                if expiration_time > datetime.now(timezone.utc):
                    user['token_status'] = 'valid'
                    user['formatted_expiration'] = expiration_time.strftime('%H:%M:%S UTC')
                else:
                    user['token_status'] = 'expired'
            except (ValueError, TypeError):
                user['token_status'] = 'error'

        processed_users.append(user)
    
    # 3. Pass all the processed lists to the template
    return render_template(
        "admin.html", 
        users=processed_users, 
        contacts=contacts,
        promoted_list=promoted_list
    )


@app.route('/admin/delete_user/<int:user_id>', methods=['POST'])
def delete_user(user_id):
    # Secure the route to the admin user
    if not session.get('user') or session['user'].get('email') != 'frenchieeap@gmail.com':
        abort(403)

    db = get_db()
    user_to_delete = db.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()

    # Prevent the admin from deleting their own account
    if user_to_delete and user_to_delete['email'] == 'frenchieeap@gmail.com':
        flash("You cannot delete the primary admin account.", "danger")
        return redirect(url_for('admin'))

    # Proceed with deletion
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    flash("User has been successfully deleted.", "success")
    return redirect(url_for('admin'))


@app.route('/admin/delete_contact/<int:contact_id>', methods=['POST'])
def delete_contact(contact_id):
    # Secure the route
    if not session.get('user') or session['user'].get('email') != 'frenchieeap@gmail.com':
        abort(403)
    
    db = get_db()
    db.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
    db.commit()
    flash("Contact message has been successfully deleted.", "success")
    return redirect(url_for('admin'))

@app.route('/admin/clear_data', methods=['POST'])
def clear_data():
    # Double-check authorization before performing a destructive action
    if not session.get('user') or session['user'].get('email') != 'frenchieeap@gmail.com':
        abort(403)
    
    db = get_db()
    # Delete all users except for the admin
    db.execute("DELETE FROM users WHERE email != ?", ('frenchieeap@gmail.com',))
    # Delete all messages from the contacts table
    db.execute("DELETE FROM contacts")
    db.commit()
    
    # Provide feedback to the admin
    flash("All user and contact entries (except for the admin account) have been successfully deleted.", "success")
    return redirect(url_for('admin'))

@app.route("/admin/promote/add", methods=["POST"])
def add_promotion():
    # Secure the route
    if not session.get('user') or session['user'].get('email') != 'frenchieeap@gmail.com':
        abort(403)
        
    nct_id = request.form.get("nct_id")
    if nct_id:
        add_promoted_study(nct_id)
        flash(f"{nct_id} added to promotions.", "success")
    else:
        flash("NCT ID cannot be empty.", "danger")
        
    return redirect(url_for('admin'))

@app.route("/admin/promote/remove/<nct_id>", methods=["POST"])
def remove_promotion(nct_id):
    # Secure the route
    if not session.get('user') or session['user'].get('email') != 'frenchieeap@gmail.com':
        abort(403)
        
    remove_promoted_study(nct_id)
    flash(f"{nct_id} removed from promotions.", "success")
    return redirect(url_for('admin'))

@app.route('/api/check_match/<nct_id>')
def api_check_match(nct_id):
    # This feature is only for logged-in users
    if not session.get('user'):
        return jsonify({"status": "NOT_LOGGED_IN"}), 401
    
    # Get the full user profile (from session or DB)
    user_data = session.get('user')
    
    # If user has no birth year or sex set, don't bother checking
    if not user_data.get('birthYear') or not user_data.get('sex'):
         return jsonify({"status": "NO_DATA", "reason": "User profile is incomplete."})
         
    if not nct_id:
        return jsonify({"error": "NCT ID is required."}), 400

    # Call your new helper function
    match_result = check_user_study_match(user_data, nct_id)
    
    return jsonify(match_result)