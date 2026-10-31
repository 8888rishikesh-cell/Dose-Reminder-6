import os
import json
import html
import re
import uuid
from datetime import date, datetime
from cryptography.fernet import Fernet, InvalidToken
import extra_streamlit_components as stx
import streamlit as st
import streamlit.components.v1 as components
import resend
from supabase import create_client
from supabase.lib.client_options import SyncClientOptions

st.set_page_config(page_title="DOSE REMINDER", page_icon="💊", layout="centered")

AUTH_COOKIE_NAME = "medelder_auth"
AUTH_COOKIE_MAX_AGE = 400 * 24 * 60 * 60
WEEKDAYS = (
    (0, "M", "Monday", "सोमवार"),
    (1, "T", "Tuesday", "मंगलवार"),
    (2, "W", "Wednesday", "बुधवार"),
    (3, "T", "Thursday", "गुरुवार"),
    (4, "F", "Friday", "शुक्रवार"),
    (5, "S", "Saturday", "शनिवार"),
    (6, "S", "Sunday", "रविवार")
)

def tr(en, hi):
    return hi if st.session_state.get("language", "English") == "Hindi" else en

def safe_email_error_details(error, api_key=""):
    message = str(getattr(error, "message", "") or error)
    if api_key:
        message = message.replace(api_key, "[redacted]")
    message = re.sub(
        r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?\S+",
        r"\1[redacted]",
        message,
    )
    message = re.sub(r"(?i)\bbearer\s+\S+", "Bearer [redacted]", message)
    message = re.sub(r"(?i)\b(?:re|rk|sk)_[A-Za-z0-9_-]{8,}\b", "[redacted]", message)
    message = re.sub(
        r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
        "[redacted token]",
        message,
    )
    message = re.sub(
        r"(?i)\b(?:access[_ -]?token|refresh[_ -]?token|api[_ -]?key|"
        r"secret|password|token)\s*[:=]\s*\S+",
        "[redacted]",
        message,
    )
    message = re.sub(r"(?i)(https?://[^\s?]+)\?[^\s]+", r"\1?[redacted]", message)
    message = re.sub(
        r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        "[email redacted]",
        message,
        flags=re.IGNORECASE,
    )
    message = re.sub(r"[\r\n\t]+", " ", message)
    details = []
    status = str(
        getattr(error, "code", "")
        or getattr(error, "status_code", "")
        or getattr(error, "status", "")
    )
    if status.isdigit():
        details.append(f"HTTP {status}")
    error_type = str(getattr(error, "error_type", ""))
    if re.fullmatch(r"[A-Za-z0-9_-]+", error_type):
        details.append(f"type {error_type}")
    suffix = f" ({', '.join(details)})" if details else ""
    safe_message = message[:400] or "No additional error detail."
    return type(error).__name__, f"{suffix}: {safe_message}"

def resend_error_category(error):
    error_type = str(getattr(error, "error_type", "")).lower()
    code = str(
        getattr(error, "code", "")
        or getattr(error, "status_code", "")
        or getattr(error, "status", "")
    ).lower()
    class_name = type(error).__name__.lower()
    message = str(getattr(error, "message", "") or error).lower()

    if code == "429" or "rate" in class_name or "rate_limit" in error_type:
        return "Rate limit or sending quota"
    if (
        code in ("401", "403")
        and any(
            term in message
            for term in ("only send testing emails", "domain", "recipient")
        )
    ):
        return "Sender/recipient restriction"
    if (
        code == "401"
        or "api_key" in error_type
        or "apikey" in class_name
        or error_type == "missing_api_key"
    ):
        return "Resend authentication/API key failure"
    if code == "403":
        return "Resend authentication/permission failure"
    if any(term in class_name for term in ("timeout", "connection", "network")):
        return "Network/API error"
    if code or error_type:
        return "Resend API error"
    return "Network/API error"

def get_auth_cookie_cipher():
    try:
        key = st.secrets.get(
            "AUTH_COOKIE_ENCRYPTION_KEY",
            os.getenv("AUTH_COOKIE_ENCRYPTION_KEY", "")
        )
    except Exception:
        key = os.getenv("AUTH_COOKIE_ENCRYPTION_KEY", "")
    if not key:
        st.error(
            "Persistent login setup is missing. Add AUTH_COOKIE_ENCRYPTION_KEY "
            "to Streamlit Secrets or the environment."
        )
        st.code(
            "python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
        st.stop()
    try:
        return Fernet(key.encode())
    except (TypeError, ValueError):
        st.error("AUTH_COOKIE_ENCRYPTION_KEY must be a valid Fernet key.")
        st.stop()

auth_cookie_cipher = get_auth_cookie_cipher()

@st.fragment
def get_cookie_manager():
    return stx.CookieManager(key="medelder-auth-cookie-manager")

cookie_manager = get_cookie_manager()

def save_auth_cookie(access_token, refresh_token, user_id, user_email):
    encrypted_session = auth_cookie_cipher.encrypt(json.dumps({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "user_id": user_id,
        "user_email": user_email
    }).encode()).decode()
    cookie_manager.set(
        AUTH_COOKIE_NAME,
        encrypted_session,
        key="save_medelder_auth",
        max_age=AUTH_COOKIE_MAX_AGE,
        secure=True,
        same_site="lax"
    )

def delete_auth_cookie():
    if cookie_manager.get(AUTH_COOKIE_NAME):
        cookie_manager.delete(AUTH_COOKIE_NAME, key="delete_medelder_auth")

def get_client():
    try:
        url = st.secrets.get("SUPABASE_URL", os.getenv("SUPABASE_URL", ""))
        key = st.secrets.get("SUPABASE_ANON_KEY", os.getenv("SUPABASE_ANON_KEY", ""))
    except Exception:
        url = os.getenv("SUPABASE_URL", "")
        key = os.getenv("SUPABASE_ANON_KEY", "")
    if not url or not key:
        st.error(
            "Supabase connection details are missing. In Streamlit Cloud, open "
            "your app → Settings → Secrets and add SUPABASE_URL and SUPABASE_ANON_KEY."
        )
        st.code('SUPABASE_URL = "https://YOUR-PROJECT.supabase.co"\nSUPABASE_ANON_KEY = "YOUR-PUBLISHABLE-KEY"')
        st.stop()
    return create_client(
        url,
        key,
        options=SyncClientOptions(auto_refresh_token=False)
    )

def save_auth(result):
    session = getattr(result, "session", None)
    user = getattr(result, "user", None)
    if session and user:
        st.session_state["access_token"] = session.access_token
        st.session_state["refresh_token"] = session.refresh_token
        st.session_state["user_id"] = user.id
        st.session_state["user_email"] = user.email or ""
        save_auth_cookie(
            session.access_token,
            session.refresh_token,
            user.id,
            user.email or ""
        )
        return True
    return False

sb = get_client()

saved_access_token = st.session_state.get("access_token")
saved_refresh_token = st.session_state.get("refresh_token")
if not saved_access_token or not saved_refresh_token:
    saved_cookie = cookie_manager.get(AUTH_COOKIE_NAME)
    if saved_cookie:
        try:
            saved_session = json.loads(
                auth_cookie_cipher.decrypt(saved_cookie.encode())
            )
            cookie_access_token = saved_session["access_token"]
            cookie_refresh_token = saved_session["refresh_token"]
            cookie_user_id = saved_session["user_id"]
            if not all(
                isinstance(value, str) and value
                for value in (
                    cookie_access_token,
                    cookie_refresh_token,
                    cookie_user_id
                )
            ):
                raise ValueError("Saved login cookie has invalid data.")
            saved_access_token = cookie_access_token
            saved_refresh_token = cookie_refresh_token
            st.session_state["user_id"] = saved_session["user_id"]
            st.session_state["user_email"] = saved_session.get("user_email", "")
        except (InvalidToken, ValueError, KeyError, TypeError, json.JSONDecodeError):
            saved_access_token = None
            saved_refresh_token = None
            delete_auth_cookie()

if saved_access_token and saved_refresh_token:
    try:
        auth_result = sb.auth.set_session(saved_access_token, saved_refresh_token)
        if not save_auth(auth_result):
            for key in ("access_token", "refresh_token", "user_id", "user_email"):
                st.session_state.pop(key, None)
            st.session_state.pop("_auth_restore_error", None)
            delete_auth_cookie()
    except Exception as error:
        if getattr(error, "status", None) in (400, 401):
            for key in ("access_token", "refresh_token", "user_id", "user_email"):
                st.session_state.pop(key, None)
            st.session_state.pop("_auth_restore_error", None)
            delete_auth_cookie()
        else:
            st.session_state["_auth_restore_error"] = str(error)

@st.dialog("Caretaker Email Settings")
def show_caretaker_settings(dialog_id):
    st.subheader("Caretaker Email Settings")
    st.write(
        "This email address will be used for future caretaker reminders. "
        "No emails are sent by this setting."
    )

    caretaker_user_id = st.session_state.get("user_id")
    if not caretaker_user_id:
        st.error("Sign in to view caretaker settings.")
        return
    try:
        settings_rows = (
            sb.table("user_settings")
            .select("caretaker_email")
            .eq("user_id", caretaker_user_id)
            .limit(1)
            .execute()
            .data or []
        )
    except Exception as error:
        st.error("Could not load caretaker settings from Supabase.")
        st.caption(f"Load error: {error}")
        return

    saved_email = (
        settings_rows[0].get("caretaker_email") or ""
        if settings_rows
        else ""
    )
    email = st.text_input(
        "Caretaker email address",
        value=saved_email,
        key=f"caretaker_email_input_{dialog_id}",
        placeholder="name@example.com",
    )

    if st.button(
        "Send Test Email",
        key=f"send_test_email_{dialog_id}",
        use_container_width=True,
    ):
        try:
            resend_api_key = st.secrets.get("RESEND_API_KEY", "")
        except Exception:
            resend_api_key = ""

        if not isinstance(resend_api_key, str) or not resend_api_key.strip():
            st.error("Resend is not configured. Add RESEND_API_KEY to Streamlit Secrets.")
        else:
            resend_api_key = resend_api_key.strip()
            try:
                auth_user = sb.auth.get_user().user
            except Exception as error:
                error_type, safe_message = safe_email_error_details(error)
                st.error(
                    "Supabase authentication failure: could not verify your "
                    "signed-in email. Please sign in again and retry."
                )
                st.code(f"{error_type}{safe_message}", language=None)
            else:
                recipient_email = getattr(auth_user, "email", None)
                if (
                    not auth_user
                    or getattr(auth_user, "id", None) != caretaker_user_id
                    or not isinstance(recipient_email, str)
                    or not recipient_email.strip()
                    or not getattr(auth_user, "email_confirmed_at", None)
                ):
                    st.error(
                        "A verified email address is not available for your signed-in "
                        "Supabase account."
                    )
                else:
                    try:
                        resend.api_key = resend_api_key
                        send_result = resend.Emails.send({
                            "from": "onboarding@resend.dev",
                            "to": [recipient_email.strip()],
                            "subject": "DOSE REMINDER - Test Email",
                            "text": (
                                "This is a test of the caretaker reminder email system."
                            ),
                        })
                        email_id = (
                            send_result.get("id")
                            if isinstance(send_result, dict)
                            else getattr(send_result, "id", None)
                        )
                    except Exception as error:
                        error_type, safe_message = safe_email_error_details(
                            error, resend_api_key
                        )
                        category = resend_error_category(error)
                        st.error(f"{category}: the test email was not accepted.")
                        st.code(f"{error_type}{safe_message}", language=None)
                    else:
                        if email_id:
                            st.success("Test email accepted by Resend.")
                        else:
                            st.error(
                                "Resend returned no email ID, so acceptance could "
                                "not be confirmed. No success is reported."
                            )

    save_column, close_column = st.columns(2)
    if save_column.button(
        "Save Email",
        key=f"save_caretaker_email_{dialog_id}",
        use_container_width=True,
    ):
        email = email.strip()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            st.error("Enter a valid email address before saving.")
        else:
            try:
                (
                    sb.table("user_settings")
                    .upsert(
                        {
                            "user_id": caretaker_user_id,
                            "caretaker_email": email,
                        },
                        on_conflict="user_id",
                    )
                    .execute()
                )
            except Exception as error:
                st.error("Could not save caretaker email to Supabase.")
                st.caption(f"Save error: {error}")
            else:
                st.success("Caretaker email saved.")
    if close_column.button(
        "Close",
        key=f"close_caretaker_settings_{dialog_id}",
        use_container_width=True,
    ):
        st.rerun()

st.markdown(
    """
    <style>
        .stApp {
            background-color: #050914 !important;
        }

        [data-testid="stAppViewContainer"] {
            background-color: #050914 !important;
            background-image: radial-gradient(
                ellipse 85% 75% at 50% 30%,
                rgba(37, 99, 235, 0.48) 0%,
                rgba(30, 64, 175, 0.32) 32%,
                rgba(15, 23, 42, 0.78) 65%,
                #050914 100%
            ) !important;
            background-attachment: fixed !important;
            background-repeat: no-repeat !important;
            background-size: cover !important;
        }

        [data-testid="stAppViewContainer"] > .main,
        [data-testid="stMain"] {
            background: transparent !important;
        }
        
        .block-container {
            padding-top: 1.5rem;
            padding-left: 1rem;
            padding-right: 1rem;
        }
        .stTabs [data-baseweb="tab-list"] {
            gap: 0.25rem;
            overflow-x: auto;
        }
        .stTabs [data-baseweb="tab"] {
            padding: 0.45rem 0.6rem;
            font-size: 0.95rem;
            white-space: nowrap;
        }
        [data-testid="stImage"] img {
            width: 88px;
            height: 88px;
            object-fit: cover;
            border-radius: 12px;
        }
        [class*="st-key-everyday_selector"] {
            background: rgba(30, 64, 175, 0.16);
            border: 1px solid rgba(96, 165, 250, 0.3);
            border-left: 3px solid #60a5fa;
            border-radius: 10px;
            padding: 0.25rem 0.75rem;
        }
        [class*="st-key-everyday_selector"] [data-testid="stCheckbox"] label {
            font-size: 0.9rem;
            font-weight: 600;
        }
        [class*="st-key-weekday_selector_done"] [data-testid="stBaseButton-primary"] {
            background: #2563eb !important;
            border-color: #2563eb !important;
            color: #ffffff !important;
        }
        [class*="st-key-weekday_selector_done"] [data-testid="stBaseButton-primary"]:hover {
            background: #1d4ed8 !important;
            border-color: #1d4ed8 !important;
            color: #ffffff !important;
        }
        [class*="st-key-taken_"] button,
        [class*="st-key-skipped_"] button {
            border-radius: 10px;
            font-weight: 600;
            background: rgba(15, 23, 42, 0.68) !important;
            border: 1px solid rgba(148, 163, 184, 0.35) !important;
            color: #e2e8f0 !important;
        }
        [class*="st-key-taken_"] button:hover,
        [class*="st-key-skipped_"] button:hover {
            background: rgba(30, 41, 59, 0.9) !important;
            border-color: rgba(96, 165, 250, 0.55) !important;
            color: #f8fafc !important;
        }
        [class*="st-key-taken_"] [data-testid="stBaseButton-primary"] {
            background: #15803d !important;
            border-color: #15803d !important;
            color: #ffffff !important;
        }
        [class*="st-key-taken_"] [data-testid="stBaseButton-primary"]:hover {
            background: #166534 !important;
            border-color: #166534 !important;
            color: #ffffff !important;
        }
        [class*="st-key-skipped_"] [data-testid="stBaseButton-primary"] {
            background: #dc2626 !important;
            border-color: #dc2626 !important;
            color: #ffffff !important;
        }
        [class*="st-key-skipped_"] [data-testid="stBaseButton-primary"]:hover {
            background: #b91c1c !important;
            border-color: #b91c1c !important;
            color: #ffffff !important;
        }
        [class*="st-key-caretaker_settings"] button {
            background: rgba(15, 23, 42, 0.68) !important;
            border: 1px solid rgba(96, 165, 250, 0.35) !important;
            color: #e2e8f0 !important;
        }
        [class*="st-key-caretaker_settings"] button:hover {
            background: rgba(30, 41, 59, 0.9) !important;
            border-color: rgba(96, 165, 250, 0.65) !important;
            color: #f8fafc !important;
        }
        @media (max-width: 640px) {
            [class*="st-key-schedule_card_"] [data-testid="stHorizontalBlock"] {
                flex-wrap: wrap;
            }
            [class*="st-key-schedule_card_"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:first-child {
                flex: 0 0 96px !important;
                min-width: 96px !important;
            }
            [class*="st-key-schedule_card_"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:nth-child(2) {
                flex: 1 1 calc(100% - 104px) !important;
                min-width: 0 !important;
            }
            [class*="st-key-schedule_card_"] [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:nth-child(3) {
                flex: 1 1 100% !important;
                min-width: 0 !important;
            }
        }
        @media (max-width: 640px) {
            .block-container {
                padding-top: 1.25rem;
            }
        }
                /* Blend the top Streamlit toolbar into the background */
        header[data-testid="stHeader"] {
            background: transparent !important;
            border: none !important;
            box-shadow: none !important;
        }

        [data-testid="stToolbar"] {
            background: transparent !important;
            border: none !important;
            box-shadow: none !important;
        }

        /* Remove the visible top divider/border */
        [data-testid="stAppViewContainer"],
        [data-testid="stMain"],
        .stApp {
            border-top: none !important;
            box-shadow: none !important;
        }

        /* Give the sidebar expand/collapse arrow a circular shape */
        [data-testid="stSidebarCollapsedControl"] button,
        [data-testid="stSidebar"] button[kind="header"] {
            border-radius: 50% !important;
            width: 36px !important;
            height: 36px !important;
            min-width: 36px !important;
            background: rgba(30, 41, 59, 0.75) !important;
            border: 1px solid rgba(148, 163, 184, 0.25) !important;
            box-shadow: none !important;
        }

        /* Give the top-right three-dot menu a circular shape */
        [data-testid="stToolbar"] button,
        header[data-testid="stHeader"] button {
            border-radius: 50% !important;
            width: 36px !important;
            height: 36px !important;
            min-width: 36px !important;
            background: rgba(30, 41, 59, 0.75) !important;
            border: 1px solid rgba(148, 163, 184, 0.25) !important;
            box-shadow: none !important;
        }

        /* Smooth hover effect for both controls */
        [data-testid="stSidebarCollapsedControl"] button:hover,
        [data-testid="stSidebar"] button[kind="header"]:hover,
        [data-testid="stToolbar"] button:hover,
        header[data-testid="stHeader"] button:hover {
            background: rgba(59, 130, 246, 0.25) !important;
            border-color: rgba(96, 165, 250, 0.5) !important;
        }
    </style>
    """,
    unsafe_allow_html=True
)

st.markdown(
    """
    <h1 style="
        font-size: 32px;
        font-weight: 700;
        text-align: center;
        line-height: 1.15;
        margin: 0 0 12px 0;
    ">
        💊 DOSE REMINDER
    </h1>
    """,
    unsafe_allow_html=True
)
st.markdown(
    f'<div style="text-align: center;">{tr("Private account-based medicine schedule and dose tracker.", "Private account-based medicine schedule aur dose tracker.")}</div>',
    unsafe_allow_html=True
)

with st.sidebar:
    st.session_state["language"] = st.selectbox(
        "Language / भाषा", ["English", "Hindi"],
        index=0 if st.session_state.get("language", "English") == "English" else 1
    )
    if st.session_state.get("user_id"):
        if st.button(
            "Caretaker Settings",
            key="caretaker_settings",
            use_container_width=True,
        ):
            dialog_id = st.session_state.get(
                "caretaker_settings_dialog_id", 0
            ) + 1
            st.session_state["caretaker_settings_dialog_id"] = dialog_id
            show_caretaker_settings(dialog_id)

def auth_screen():
    st.subheader(tr("Sign in or create an account", "Login karein ya naya account banayein"))
    tab1, tab2 = st.tabs([tr("Sign in", "Login"), tr("Sign up", "Naya account")])
    with tab1:
        with st.form("signin_form"):
            email = st.text_input(tr("Email", "Email"))
            password = st.text_input(tr("Password", "Password"), type="password")
            submitted = st.form_submit_button(tr("Sign in", "Login karein"), use_container_width=True)
        if submitted:
            if not email or not password:
                st.warning(tr("Enter your email and password.", "Email aur password daalein."))
            else:
                try:
                    result = sb.auth.sign_in_with_password({"email": email.strip(), "password": password})
                    if save_auth(result):
                        st.rerun()
                    st.error(tr("Login did not return an active session. Please try again.",
                                "Login session nahi mila. Dobara try karein."))
                except Exception as e:
                    st.error(tr("Sign-in failed. Check your email and password.",
                                "Login nahi hua. Email aur password check karein."))
    with tab2:
        with st.form("signup_form"):
            new_email = st.text_input(tr("Email address", "Email address"), key="signup_email")
            new_password = st.text_input(tr("Create password (at least 8 characters)", "Password banayein (kam se kam 8 characters)"), type="password", key="signup_password")
            confirm = st.text_input(tr("Confirm password", "Password dobara daalein"), type="password")
            submitted_signup = st.form_submit_button(tr("Create account", "Account banayein"), use_container_width=True)
        if submitted_signup:
            if not new_email or not new_password:
                st.warning(tr("Fill in both fields.", "Dono fields bharein."))
            elif new_password != confirm:
                st.warning(tr("Passwords do not match.", "Dono passwords match nahi karte."))
            elif len(new_password) < 8:
                st.warning(tr("Use a password with at least 8 characters.", "Kam se kam 8 characters ka password rakhein."))
            else:
                try:
                    result = sb.auth.sign_up({"email": new_email.strip(), "password": new_password})
                    if save_auth(result):
                        st.success(tr("Account created.", "Account ban gaya."))
                        st.rerun()
                    else:
                        st.success(tr("Account request created. Check your email for a confirmation link, then return here and sign in.",
                                      "Account request ban gayi. Email mein confirmation link check karein, phir yahan login karein."))
                except Exception:
                    st.error(tr("Could not create the account. The email may already be registered, or the password may not meet the requirements.",
                                "Account nahi bana. Ho sakta hai email pehle se registered ho ya password requirements poori na hon."))

if not st.session_state.get("user_id"):
    
    auth_screen()
    st.stop()

user_id = st.session_state["user_id"]
user_email = st.session_state.get("user_email", "")
if st.session_state.get("_auth_restore_error"):
    st.warning(
        "Supabase could not refresh your saved login right now. Your saved "
        "credentials have been kept; please retry shortly."
    )

def scheduled_weekdays(medicine):
    saved_days = medicine.get("scheduled_days")
    if saved_days is None:
        return tuple(range(7))
    if not isinstance(saved_days, (list, tuple)):
        return ()
    return tuple(sorted({
        day for day in saved_days
        if type(day) is int and 0 <= day < 7
    }))

def is_scheduled_on(medicine, day):
    return day.weekday() in scheduled_weekdays(medicine)

def render_weekday_row(container, medicine):
    selected_days = set(scheduled_weekdays(medicine))
    day_labels = []
    for day, short_label, english, hindi in WEEKDAYS:
        color = "#93c5fd" if day in selected_days else "#64748b"
        weight = "700" if day in selected_days else "400"
        title = html.escape(tr(english, hindi), quote=True)
        day_labels.append(
            f'<span title="{title}" aria-label="{title}" '
            f'style="color:{color};font-weight:{weight};">{short_label}</span>'
        )
    container.markdown(
        f'<div aria-label="{html.escape(tr("Scheduled weekdays", "निर्धारित दिन"), quote=True)}" '
        f'style="display:flex;align-items:center;gap:8px;margin:4px 0;">'
        f'{"".join(day_labels)}</div>',
        unsafe_allow_html=True
    )

def toggle_scheduled_day(
    day,
    selected_days_key="new_medicine_weekdays",
    everyday_key="weekday_everyday"
):
    selected_days = set(st.session_state.get(selected_days_key, range(7)))
    if day in selected_days:
        selected_days.remove(day)
    else:
        selected_days.add(day)
    st.session_state[selected_days_key] = sorted(selected_days)
    st.session_state[everyday_key] = len(selected_days) == len(WEEKDAYS)

def toggle_everyday(
    selected_days_key="new_medicine_weekdays",
    everyday_key="weekday_everyday"
):
    st.session_state[selected_days_key] = (
        list(range(len(WEEKDAYS)))
        if st.session_state[everyday_key]
        else []
    )

def render_weekday_picker(
    selected_days_key,
    everyday_key,
    widget_prefix,
    everyday_container_key
):
    selected_days = set(st.session_state.get(selected_days_key, range(7)))
    st.session_state.setdefault(
        everyday_key,
        len(selected_days) == len(WEEKDAYS)
    )
    with st.container(key=everyday_container_key):
        st.checkbox(
            tr("Everyday", "हर दिन"),
            key=everyday_key,
            on_change=toggle_everyday,
            args=(selected_days_key, everyday_key)
        )
    weekday_columns = st.columns(2)
    for day, _, english, hindi in WEEKDAYS:
        weekday_columns[day % 2].button(
            tr(english, hindi),
            key=f"{widget_prefix}_weekday_toggle_{day}",
            type="primary" if day in selected_days else "secondary",
            use_container_width=True,
            on_click=toggle_scheduled_day,
            args=(day, selected_days_key, everyday_key)
        )

@st.dialog(tr("Select Days", "दिन चुनें"))
def show_weekday_selector():
    render_weekday_picker(
        "new_medicine_weekdays",
        "weekday_everyday",
        "add",
        "everyday_selector"
    )
    if st.button(
        tr("Done", "हो गया"),
        key="weekday_selector_done",
        type="primary",
        use_container_width=True
    ):
        st.rerun()

def save_medicine_changes(medicine, updates, image_action, uploaded_image):
    old_image_path = medicine.get("image_path")
    new_image_path = old_image_path
    uploaded_image_path = None

    if image_action == "remove":
        new_image_path = None
    elif image_action == "replace":
        if uploaded_image is None:
            raise ValueError(tr(
                "Choose an image to replace the current photo.",
                "मौजूदा फोटो बदलने के लिए नई फोटो चुनें।"
            ))
        if len(uploaded_image.getvalue()) > 5 * 1024 * 1024:
            raise ValueError(tr(
                "The photo must be 5 MB or smaller.",
                "फोटो 5 MB या उससे छोटी होनी चाहिए।"
            ))
        extension = uploaded_image.name.rsplit(".", 1)[-1].lower()
        image_types = {
            "jpg": ("jpg", "image/jpeg"),
            "jpeg": ("jpg", "image/jpeg"),
            "png": ("png", "image/png"),
            "webp": ("webp", "image/webp")
        }
        if extension not in image_types:
            raise ValueError(tr(
                "Choose a JPG, JPEG, PNG, or WebP photo.",
                "JPG, JPEG, PNG, या WebP फोटो चुनें।"
            ))
        stored_extension, content_type = image_types[extension]
        uploaded_image_path = (
            f"{user_id}/{uuid.uuid4().hex}.{stored_extension}"
        )
        sb.storage.from_("medicine-images").upload(
            uploaded_image_path,
            uploaded_image.getvalue(),
            {"content-type": content_type}
        )
        new_image_path = uploaded_image_path

    updates["image_path"] = new_image_path
    try:
        result = (
            sb.table("medicines")
            .update(updates)
            .eq("id", medicine["id"])
            .eq("user_id", user_id)
            .select("id")
            .execute()
        )
        if not result.data:
            raise RuntimeError(tr(
                "The medicine was not updated. Check your account permissions and try again.",
                "दवा अपडेट नहीं हुई। अपने अकाउंट की अनुमति जाँचें और फिर कोशिश करें।"
            ))
    except Exception as update_error:
        if uploaded_image_path:
            try:
                sb.storage.from_("medicine-images").remove([uploaded_image_path])
            except Exception as cleanup_error:
                raise RuntimeError(
                    f"{update_error} {tr('The new upload could not be removed:', 'नई अपलोड की गई फोटो हटाई नहीं जा सकी:')} {cleanup_error}"
                ) from update_error
        raise

    if old_image_path and old_image_path != new_image_path:
        try:
            sb.storage.from_("medicine-images").remove([old_image_path])
        except Exception as cleanup_error:
            return cleanup_error
    return None

def initialize_medicine_editor(medicine):
    medicine_id = str(medicine["id"])
    prefix = f"edit_medicine_{medicine_id}"
    st.session_state["editing_medicine_id"] = medicine_id
    st.session_state[f"{prefix}_name"] = medicine.get("name", "")
    try:
        st.session_state[f"{prefix}_time"] = datetime.strptime(
            str(medicine.get("scheduled_time", ""))[:8], "%H:%M:%S"
        ).time()
    except ValueError:
        st.session_state[f"{prefix}_time"] = datetime.strptime(
            str(medicine.get("scheduled_time", ""))[:5], "%H:%M"
        ).time()
    selected_days = list(scheduled_weekdays(medicine))
    st.session_state[f"{prefix}_days"] = selected_days
    st.session_state[f"{prefix}_everyday"] = len(selected_days) == len(WEEKDAYS)
    st.session_state[f"{prefix}_days_open"] = False
    st.session_state[f"{prefix}_notes"] = medicine.get("notes") or ""
    st.session_state[f"{prefix}_image_action"] = "keep"

@st.dialog(tr("Edit medicine", "दवा संपादित करें"))
def show_medicine_editor(medicine):
    medicine_id = str(medicine["id"])
    prefix = f"edit_medicine_{medicine_id}"
    if st.session_state.get(f"{prefix}_days_open"):
        st.caption(tr("Scheduled weekdays", "Dawa ke din"))
        render_weekday_picker(
            f"{prefix}_days",
            f"{prefix}_everyday",
            prefix,
            f"{prefix}_everyday_selector"
        )
        if st.button(
            tr("Done", "हो गया"),
            key=f"{prefix}_days_done",
            use_container_width=True
        ):
            st.session_state[f"{prefix}_days_open"] = False
            st.rerun()
        return

    st.text_input(
        tr("Medicine name", "Medicine ka naam"),
        key=f"{prefix}_name"
    )
    st.time_input(
        tr("Scheduled time", "Dawa ka time"),
        key=f"{prefix}_time"
    )
    st.text_area(
        tr("Notes (optional)", "Notes (optional)"),
        key=f"{prefix}_notes"
    )
    selected_days = set(st.session_state[f"{prefix}_days"])
    selected_labels = [
        tr(english, hindi)
        for day, _, english, hindi in WEEKDAYS
        if day in selected_days
    ]
    st.caption(
        f"{tr('Scheduled weekdays', 'Dawa ke din')}: "
        f"{', '.join(selected_labels) or tr('None selected', 'कोई दिन नहीं चुना')}"
    )
    if st.button(
        tr("Select Days", "दिन चुनें"),
        key=f"{prefix}_select_days",
        use_container_width=True
    ):
        st.session_state[f"{prefix}_days_open"] = True
        st.rerun()

    if medicine.get("image_path"):
        try:
            image_result = sb.storage.from_("medicine-images").create_signed_url(
                medicine["image_path"], 3600
            )
            image_url = (
                image_result.get("signedURL")
                or image_result.get("signedUrl")
            )
            if image_url:
                st.image(image_url, width=96)
        except Exception as error:
            st.warning(tr(
                "The current photo could not be loaded.",
                "मौजूदा फोटो लोड नहीं हो सकी।"
            ))
            st.caption(f"Image storage error: {error}")

    st.radio(
        tr("Medicine photo", "दवा की फोटो"),
        options=["keep", "replace", "remove"],
        format_func=lambda action: tr(
            {"keep": "Keep current", "replace": "Replace", "remove": "Remove"}[action],
            {"keep": "मौजूदा रखें", "replace": "बदलें", "remove": "हटाएँ"}[action]
        ),
        key=f"{prefix}_image_action",
        horizontal=True
    )
    image_action = st.session_state[f"{prefix}_image_action"]
    uploaded_image = None
    if image_action == "replace":
        uploaded_image = st.file_uploader(
            tr("Upload replacement image", "नई फोटो अपलोड करें"),
            type=["jpg", "jpeg", "png", "webp"],
            max_upload_size=5,
            key=f"{prefix}_image_upload"
        )

    save_column, cancel_column = st.columns(2)
    if save_column.button(
        tr("Save Changes", "बदलाव सहेजें"),
        key=f"{prefix}_save",
        type="primary",
        use_container_width=True
    ):
        name = st.session_state[f"{prefix}_name"].strip()
        selected_days = st.session_state[f"{prefix}_days"]
        if not name:
            st.error(tr(
                "Please enter a medicine name.",
                "Medicine ka naam daalein."
            ))
        elif not selected_days:
            st.error(tr(
                "Select at least one weekday.",
                "Kam se kam ek din chunein."
            ))
        else:
            updates = {
                "name": name,
                "scheduled_time": st.session_state[f"{prefix}_time"].strftime(
                    "%H:%M:%S"
                ),
                "scheduled_days": selected_days,
                "notes": st.session_state[f"{prefix}_notes"].strip()
            }
            try:
                cleanup_error = save_medicine_changes(
                    medicine,
                    updates,
                    image_action,
                    uploaded_image
                )
            except Exception as error:
                st.error(tr(
                    "Could not save medicine changes.",
                    "दवा के बदलाव सहेजे नहीं जा सके।"
                ))
                st.caption(f"Save error: {error}")
            else:
                if cleanup_error:
                    st.warning(tr(
                        "The medicine was updated, but its previous photo could not be removed.",
                        "दवा अपडेट हो गई, लेकिन पुरानी फोटो हटाई नहीं जा सकी।"
                    ))
                    st.caption(f"Storage cleanup error: {cleanup_error}")
                st.session_state.pop("editing_medicine_id", None)
                st.rerun()
    if cancel_column.button(
        tr("Cancel", "रद्द करें"),
        key=f"{prefix}_cancel",
        use_container_width=True
    ):
        st.session_state.pop("editing_medicine_id", None)
        st.rerun()

def load_medicines():
    medicines = sb.table("medicines").select("*").eq("user_id", user_id).eq("active", True).order("scheduled_time").execute().data or []
    for medicine in medicines:
        medicine["scheduled_days"] = list(scheduled_weekdays(medicine))
    return medicines

def load_old_medicines():
    medicines = sb.table("medicines").select("*").eq("user_id", user_id).eq("active", False).order("scheduled_time").execute().data or []
    for medicine in medicines:
        medicine["scheduled_days"] = list(scheduled_weekdays(medicine))
    return medicines

def load_logs_for_day(day):
    logs = sb.table("dose_logs").select("*").eq("user_id", user_id).eq("scheduled_date", str(day)).order("scheduled_time").execute().data or []
    meds = sb.table("medicines").select("id,name,notes,image_path").eq("user_id", user_id).execute().data or []
    med_map = {m["id"]: m for m in meds}
    for log in logs:
        medicine = med_map.get(log["medicine_id"], {})
        log["medicine_name"] = medicine.get("name", "Medicine")
        log["medicine_notes"] = medicine.get("notes")
        log["medicine_image_path"] = medicine.get("image_path")
    return logs

@st.dialog("Medicine Dose History")
def show_dose_history_dialog(medicine_id, medicine_name, taken_only=False):
    st.subheader(f"💊 {medicine_name}")
    if taken_only:
        st.caption("Taken dose history · date and time")
    else:
        st.caption("Previous doses and their saved status")

    dose_query = (
        sb.table("dose_logs")
        .select("scheduled_date, scheduled_time, status")
        .eq("user_id", user_id)
        .eq("medicine_id", medicine_id)
        .order("scheduled_date", desc=True)
        .order("scheduled_time", desc=True)
    )
    if taken_only:
        dose_query = dose_query.eq("status", "Taken")
    dose_logs = dose_query.execute().data or []

    if not dose_logs:
        if taken_only:
            st.info("No doses marked as taken were found for this medicine.")
        else:
            st.info("No dose history found for this medicine.")
        return

    if taken_only:
        taken_rows = []
        for dose in dose_logs:
            raw_date = str(dose.get("scheduled_date") or "Unknown date")
            try:
                display_date = date.fromisoformat(raw_date).strftime("%b %d, %Y")
            except ValueError:
                display_date = raw_date

            raw_time = str(dose.get("scheduled_time") or "")[:8]
            try:
                display_time = datetime.strptime(
                    raw_time, "%H:%M:%S"
                ).strftime("%I:%M %p").lstrip("0")
            except ValueError:
                display_time = raw_time or "Unknown time"

            taken_rows.append({
                "Date": display_date,
                "Time": display_time,
                "Status": "Taken",
            })

        st.dataframe(
            taken_rows,
            use_container_width=True,
            hide_index=True,
        )
        return

    for dose in dose_logs:
        status = dose.get("status", "Unconfirmed")
        dose_date = dose.get("scheduled_date", "Unknown date")
        raw_time = dose.get("scheduled_time", "")[:8]

        try:
            display_time = datetime.strptime(
                raw_time, "%H:%M:%S"
            ).strftime("%I:%M %p").lstrip("0")
        except ValueError:
            display_time = raw_time

        if status == "Taken":
            st.success(f"✓ Taken · {dose_date} · {display_time}")
        elif status == "Skipped":
            st.error(f"✕ Skipped · {dose_date} · {display_time}")
        else:
            st.info(f"○ Unconfirmed · {dose_date} · {display_time}")

@st.dialog("Permanently Delete Medicine")
def show_delete_medicine_dialog(medicine_id, medicine_name):
    st.warning(
        f"Permanently delete {medicine_name} and all of its dose history? "
        "This cannot be undone."
    )
    confirm_column, cancel_column = st.columns(2)
    if cancel_column.button(
        "Cancel",
        key=f"cancel_delete_medicine_{medicine_id}",
        use_container_width=True,
    ):
        st.rerun()

    if confirm_column.button(
        "Delete Permanently",
        key=f"confirm_delete_medicine_{medicine_id}",
        type="primary",
        use_container_width=True,
    ):
        try:
            medicine_rows = (
                sb.table("medicines")
                .select("id,image_path")
                .eq("id", medicine_id)
                .eq("user_id", user_id)
                .eq("active", False)
                .limit(1)
                .execute()
                .data or []
            )
            if not medicine_rows:
                st.error(
                    "This archived medicine could not be found in your account."
                )
                return

            (
                sb.table("dose_logs")
                .delete()
                .eq("user_id", user_id)
                .eq("medicine_id", medicine_id)
                .execute()
            )
            remaining_logs = (
                sb.table("dose_logs")
                .select("id")
                .eq("user_id", user_id)
                .eq("medicine_id", medicine_id)
                .limit(1)
                .execute()
                .data or []
            )
            if remaining_logs:
                st.error(
                    "Could not remove this medicine's dose history. Check the "
                    "dose_logs DELETE policy in Supabase."
                )
                return
            deleted_medicines = (
                sb.table("medicines")
                .delete()
                .eq("id", medicine_id)
                .eq("user_id", user_id)
                .eq("active", False)
                .select("id")
                .execute()
                .data or []
            )
            if not deleted_medicines:
                st.error(
                    "The medicine was not deleted. Its dose history may already "
                    "have been removed."
                )
                return
        except Exception as error:
            st.error(
                "Could not permanently delete this medicine. Check your Supabase "
                "DELETE permissions and try again."
            )
            st.caption(f"Supabase error: {error}")
            return

        image_cleanup_failed = False
        image_path = medicine_rows[0].get("image_path")
        if image_path:
            try:
                sb.storage.from_("medicine-images").remove([image_path])
            except Exception:
                image_cleanup_failed = True

        if str(st.session_state.get("selected_history_medicine_id")) == str(medicine_id):
            st.session_state.pop("selected_history_medicine_id", None)
        st.session_state["medicine_delete_notice"] = {
            "image_cleanup_failed": image_cleanup_failed,
        }
        st.rerun()

def ensure_today_logs(medicines):
    today = date.today()
    existing = sb.table("dose_logs").select("medicine_id,scheduled_time").eq("user_id", user_id).eq("scheduled_date", str(today)).execute().data or []
    existing_keys = {(x["medicine_id"], x["scheduled_time"][:8]) for x in existing}
    for med in medicines:
        if not is_scheduled_on(med, today):
            continue
        medicine_id = med.get("id")
        scheduled_time = med.get("scheduled_time")
        if medicine_id is None or not scheduled_time:
            continue
        scheduled_time = str(scheduled_time)
        key = (medicine_id, scheduled_time[:8])
        if key not in existing_keys:
            sb.table("dose_logs").insert({
                "user_id": user_id,
                "medicine_id": medicine_id,
                "scheduled_date": str(today),
                "scheduled_time": scheduled_time,
                "status": "Unconfirmed"
            }).execute()
            existing_keys.add(key)

def render_dose_notifications(medicines):
    schedules = []
    for medicine in medicines:
        scheduled_time = medicine.get("scheduled_time")
        medicine_id = medicine.get("id")
        if not scheduled_time or medicine_id is None:
            continue
        try:
            parsed_time = datetime.strptime(str(scheduled_time)[:8], "%H:%M:%S")
        except ValueError:
            try:
                parsed_time = datetime.strptime(str(scheduled_time)[:5], "%H:%M")
            except ValueError:
                continue
        schedules.append({
            "id": str(medicine_id),
            "name": str(medicine.get("name") or tr("Medicine", "दवा")),
            "time": parsed_time.strftime("%H:%M"),
            "days": list(scheduled_weekdays(medicine))
        })

    if not schedules:
        return

    config = {
        "userId": str(user_id),
        "schedules": schedules,
        "locale": "hi-IN" if st.session_state.get("language") == "Hindi" else "en-US",
        "title": tr("Medicine reminder", "दवा लेने का समय"),
        "soundButton": tr("Enable sound", "ध्वनि चालू करें"),
        "soundDisableButton": tr("Disable sound", "ध्वनि बंद करें"),
        "soundEnabled": tr("Sound enabled", "ध्वनि चालू है"),
        "soundDisabled": tr("Sound disabled.", "ध्वनि बंद है।"),
        "soundBlocked": tr(
            "Sound is blocked. Click Enable sound to allow the alert.",
            "ध्वनि नहीं चली। अलर्ट के लिए ध्वनि चालू करें पर क्लिक करें।"
        ),
        "notificationButton": tr(
            "Enable browser notifications",
            "ब्राउज़र नोटिफ़िकेशन चालू करें"
        ),
        "notificationDisableButton": tr(
            "Disable browser notifications",
            "ब्राउज़र नोटिफ़िकेशन बंद करें"
        ),
        "notificationEnabled": tr(
            "Browser notifications are enabled.",
            "ब्राउज़र नोटिफ़िकेशन चालू हैं।"
        ),
        "notificationDisabled": tr(
            "Browser notifications are disabled for this app.",
            "इस ऐप के लिए ब्राउज़र नोटिफ़िकेशन बंद हैं।"
        ),
        "notificationDenied": tr(
            "Notifications are blocked. Allow them in your browser settings.",
            "नोटिफ़िकेशन ब्लॉक हैं। ब्राउज़र सेटिंग्स में अनुमति दें।"
        ),
        "notificationUnavailable": tr(
            "Browser notifications are unavailable here; in-page reminders will still appear.",
            "यहाँ ब्राउज़र नोटिफ़िकेशन उपलब्ध नहीं हैं; ऐप में रिमाइंडर दिखेंगे।"
        ),
        "notificationPermissionNeeded": tr(
            "Click Enable browser notifications to receive browser alerts.",
            "ब्राउज़र अलर्ट पाने के लिए ब्राउज़र नोटिफ़िकेशन चालू करें पर क्लिक करें।"
        ),
        "dueMessage": tr(
            "It's time to take {medicine} · {time}",
            "अब {medicine} लेने का समय है · {time}"
        ),
        "dismissButton": tr("Dismiss", "बंद करें")
    }
    escaped_config = html.escape(json.dumps(config), quote=True)
    components.html(
        f"""
        <div id="dose-reminder" data-config="{escaped_config}"></div>
        <style>
            body {{ margin: 0; font-family: sans-serif; color: #f8fafc; }}
            #dose-reminder-panel {{
                box-sizing: border-box;
                padding: 12px;
                border: 1px solid rgba(148, 163, 184, .35);
                border-radius: 12px;
                background: rgba(15, 23, 42, .88);
            }}
            #dose-reminder-heading {{
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 12px;
                margin-bottom: 8px;
                font-weight: 700;
                flex-wrap: wrap;
            }}
            #dose-reminder-controls {{
                display: flex;
                flex-wrap: wrap;
                gap: 6px;
            }}
            #dose-reminder button {{
                border: 1px solid rgba(148, 163, 184, .5);
                border-radius: 8px;
                padding: 6px 10px;
                color: #f8fafc;
                background: #1e3a8a;
                cursor: pointer;
            }}
            #dose-reminder button.dose-reminder-toggle.is-enabled {{
                border-color: #22c55e;
                background: #15803d;
            }}
            #dose-reminder-status {{ margin: 4px 0; color: #cbd5e1; font-size: 13px; }}
            .dose-reminder-card {{
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 10px;
                margin-top: 8px;
                padding: 10px;
                border-radius: 8px;
                background: rgba(30, 64, 175, .38);
            }}
            .dose-reminder-message {{ font-weight: 600; }}
        </style>
        <script>
            (() => {{
                const root = document.getElementById("dose-reminder");
                const config = JSON.parse(root.dataset.config);
                const pad = (value) => String(value).padStart(2, "0");
                const dateKey = (date) =>
                    `${{date.getFullYear()}}-${{pad(date.getMonth() + 1)}}-${{pad(date.getDate())}}`;
                const prefix = `dose-reminder:${{encodeURIComponent(config.userId)}}:`;
                const getStorage = () => {{
                    for (const storageName of ["localStorage", "sessionStorage"]) {{
                        try {{
                            const storage = window[storageName];
                            const testKey = prefix + "storage-check";
                            storage.setItem(testKey, "1");
                            storage.removeItem(testKey);
                            return storage;
                        }} catch (_) {{
                            // Try session storage if persistent storage is unavailable.
                        }}
                    }}
                    return null;
                }};
                const storage = getStorage();
                let notificationWindow = window;
                try {{
                    if (window.parent && window.parent.Notification) {{
                        notificationWindow = window.parent;
                    }}
                }} catch (_) {{
                    // Fall back to the component window if the parent is inaccessible.
                }}
                let activeDate = "";
                let stateKey = "";
                const settingsKey = prefix + "settings";
                let state = {{ seen: [], dismissed: [] }};
                let settings = {{ notificationsEnabled: false, soundEnabled: false }};
                let audioContext;

                const readState = (key) => {{
                    try {{
                        const saved = JSON.parse(storage ? storage.getItem(key) || "{{}}" : "{{}}");
                        return {{
                            seen: Array.isArray(saved.seen) ? saved.seen : [],
                            dismissed: Array.isArray(saved.dismissed) ? saved.dismissed : []
                        }};
                    }} catch (_) {{
                        return {{ seen: [], dismissed: [] }};
                    }}
                }};
                const saveState = () => {{
                    try {{
                        if (storage) storage.setItem(stateKey, JSON.stringify(state));
                    }} catch (_) {{
                        // Keep this page session functional when browser storage is unavailable.
                    }}
                }};
                const saveSettings = () => {{
                    try {{
                        if (storage) storage.setItem(settingsKey, JSON.stringify(settings));
                    }} catch (_) {{
                        // Keep the toggles functional for this page session when storage is unavailable.
                    }}
                }};
                const makeBeep = async () => {{
                    try {{
                        const AudioContext = window.AudioContext || window.webkitAudioContext;
                        if (!AudioContext) return false;
                        audioContext = audioContext || new AudioContext();
                        if (audioContext.state === "suspended") await audioContext.resume();
                        const oscillator = audioContext.createOscillator();
                        const gain = audioContext.createGain();
                        const start = audioContext.currentTime;
                        oscillator.frequency.value = 880;
                        gain.gain.setValueAtTime(0.0001, start);
                        gain.gain.exponentialRampToValueAtTime(0.15, start + 0.02);
                        gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.28);
                        oscillator.connect(gain);
                        gain.connect(audioContext.destination);
                        oscillator.start(start);
                        oscillator.stop(start + 0.29);
                        return true;
                    }} catch (_) {{
                        return false;
                    }}
                }};

                root.innerHTML = `
                    <section id="dose-reminder-panel" aria-live="polite">
                        <div id="dose-reminder-heading">
                            <span>💊 ${{config.title}}</span>
                            <div id="dose-reminder-controls">
                                <button id="dose-reminder-notifications" class="dose-reminder-toggle" type="button"></button>
                                <button id="dose-reminder-enable" class="dose-reminder-toggle" type="button"></button>
                            </div>
                        </div>
                        <div id="dose-reminder-status"></div>
                        <div id="dose-reminder-alerts"></div>
                    </section>`;
                const alerts = root.querySelector("#dose-reminder-alerts");
                const status = root.querySelector("#dose-reminder-status");
                const notificationButton = root.querySelector("#dose-reminder-notifications");
                const soundButton = root.querySelector("#dose-reminder-enable");
                try {{
                    const savedSettings = JSON.parse(
                        storage ? storage.getItem(settingsKey) || "{{}}" : "{{}}"
                    );
                    settings.notificationsEnabled = savedSettings.notificationsEnabled === true;
                    settings.soundEnabled = savedSettings.soundEnabled === true;
                }} catch (_) {{
                    // Invalid or unavailable saved preferences leave both controls disabled.
                }}
                const renderToggleButtons = () => {{
                    notificationButton.textContent = settings.notificationsEnabled
                        ? config.notificationDisableButton
                        : config.notificationButton;
                    notificationButton.classList.toggle("is-enabled", settings.notificationsEnabled);
                    notificationButton.setAttribute("aria-pressed", String(settings.notificationsEnabled));
                    soundButton.textContent = settings.soundEnabled
                        ? config.soundDisableButton
                        : config.soundButton;
                    soundButton.classList.toggle("is-enabled", settings.soundEnabled);
                    soundButton.setAttribute("aria-pressed", String(settings.soundEnabled));
                }};
                renderToggleButtons();
                notificationButton.addEventListener("click", async () => {{
                    if (settings.notificationsEnabled) {{
                        settings.notificationsEnabled = false;
                        saveSettings();
                        renderToggleButtons();
                        status.textContent = config.notificationDisabled;
                        return;
                    }}
                    if (!notificationWindow.Notification) {{
                        status.textContent = config.notificationUnavailable;
                        return;
                    }}
                    try {{
                        const permission = await notificationWindow.Notification.requestPermission();
                        if (permission === "granted") {{
                            settings.notificationsEnabled = true;
                            saveSettings();
                            renderToggleButtons();
                            status.textContent = config.notificationEnabled;
                        }} else {{
                            status.textContent = config.notificationDenied;
                        }}
                    }} catch (_) {{
                        status.textContent = config.notificationUnavailable;
                    }}
                }});
                soundButton.addEventListener("click", async () => {{
                    if (settings.soundEnabled) {{
                        settings.soundEnabled = false;
                        saveSettings();
                        renderToggleButtons();
                        status.textContent = config.soundDisabled;
                    }} else if (await makeBeep()) {{
                        settings.soundEnabled = true;
                        saveSettings();
                        renderToggleButtons();
                        status.textContent = config.soundEnabled;
                    }} else {{
                        status.textContent = config.soundBlocked;
                    }}
                }});

                const renderAlerts = () => {{
                    alerts.replaceChildren();
                    for (const medicine of config.schedules) {{
                        const token = `${{medicine.id}}|${{medicine.time}}`;
                        if (!state.seen.includes(token) || state.dismissed.includes(token)) continue;
                        const card = document.createElement("div");
                        card.className = "dose-reminder-card";
                        const message = document.createElement("span");
                        message.className = "dose-reminder-message";
                        const scheduled = new Date();
                        const [hour, minute] = medicine.time.split(":").map(Number);
                        scheduled.setHours(hour, minute, 0, 0);
                        message.textContent = config.dueMessage
                            .replace("{{medicine}}", medicine.name)
                            .replace("{{time}}", scheduled.toLocaleTimeString(config.locale, {{
                                hour: "numeric", minute: "2-digit"
                            }}));
                        const dismiss = document.createElement("button");
                        dismiss.type = "button";
                        dismiss.textContent = config.dismissButton;
                        dismiss.addEventListener("click", () => {{
                            state.dismissed.push(token);
                            saveState();
                            renderAlerts();
                        }});
                        card.append(message, dismiss);
                        alerts.append(card);
                    }}
                }};

                const checkSchedule = async () => {{
                    const now = new Date();
                    const today = dateKey(now);
                    if (today !== activeDate) {{
                        activeDate = today;
                        stateKey = prefix + today;
                        state = readState(stateKey);
                        renderAlerts();
                    }}

                    const currentTime = `${{pad(now.getHours())}}:${{pad(now.getMinutes())}}`;
                    for (const medicine of config.schedules) {{
                        const token = `${{medicine.id}}|${{medicine.time}}`;
                        if (!medicine.days.includes((now.getDay() + 6) % 7)) continue;
                        if (medicine.time !== currentTime || state.seen.includes(token)) continue;
                        state.seen.push(token);
                        saveState();
                        renderAlerts();
                        if (settings.notificationsEnabled && !notificationWindow.Notification) {{
                            status.textContent = config.notificationUnavailable;
                        }} else if (
                            settings.notificationsEnabled
                            && notificationWindow.Notification.permission === "granted"
                        ) {{
                            try {{
                                const scheduled = new Date();
                                const [hour, minute] = medicine.time.split(":").map(Number);
                                scheduled.setHours(hour, minute, 0, 0);
                                const body = config.dueMessage
                                    .replace("{{medicine}}", medicine.name)
                                    .replace("{{time}}", scheduled.toLocaleTimeString(config.locale, {{
                                        hour: "numeric", minute: "2-digit"
                                    }}));
                                new notificationWindow.Notification(config.title, {{ body }});
                            }} catch (_) {{
                                status.textContent = config.notificationUnavailable;
                            }}
                        }} else if (settings.notificationsEnabled) {{
                            status.textContent = config.notificationPermissionNeeded;
                        }}
                        if (settings.soundEnabled && !(await makeBeep())) {{
                            status.textContent = config.soundBlocked;
                        }}
                    }}
                }};

                try {{
                    for (let index = storage ? storage.length - 1 : -1; storage && index >= 0; index--) {{
                        const key = storage.key(index);
                        if (key && key.startsWith(prefix) && key !== prefix + dateKey(new Date())) {{
                            storage.removeItem(key);
                        }}
                    }}
                }} catch (_) {{
                    // Storage is optional; the in-memory fallback still deduplicates this page.
                }}
                checkSchedule();
                window.setInterval(checkSchedule, 5000);
            }})();
        </script>
        """,
        height=120,
        scrolling=True
    )

with st.sidebar:
    st.write(tr("Signed in as", "Login account"), f"**{user_email}**")
    if st.button(tr("Sign out", "Logout"), use_container_width=True):
        try:
            sb.auth.sign_out()
        except Exception:
            pass
        delete_auth_cookie()
        for key in ("access_token", "refresh_token", "user_id", "user_email"):
            st.session_state.pop(key, None)
        st.session_state.pop("_auth_restore_error", None)
        st.rerun()

try:
    medicines = load_medicines()
    old_medicines = load_old_medicines()
    render_dose_notifications(medicines)
    delete_notice = st.session_state.pop("medicine_delete_notice", None)
    if delete_notice:
        st.success("Medicine and its dose history were permanently deleted.")
        if delete_notice["image_cleanup_failed"]:
            st.warning(
                "The medicine photo could not be removed from Supabase Storage."
            )
    today = date.today()
    ensure_today_logs(medicines)
    editing_medicine_id = st.session_state.get("editing_medicine_id")
    if editing_medicine_id is not None:
        medicine_to_edit = next(
            (
                medicine for medicine in medicines
                if str(medicine["id"]) == str(editing_medicine_id)
            ),
            None
        )
        if medicine_to_edit is not None:
            show_medicine_editor(medicine_to_edit)

    # Main navigation: use tabs instead of scrolling through every section.
    today_tab, add_tab, medicines_tab, old_medicines_tab, history_tab = st.tabs([
        tr("🏠 Today's Schedule", "🏠 Aaj ka Schedule"),
        tr("➕ Add Medicine", "➕ Dawa Add Karein"),
        tr("💊 My Medicines", "💊 Meri Dawaein"),
        tr("🗃️ Old Medicines", "🗃️ Purani Dawaein"),
        tr("📋 Dose History", "📋 Dose History")
    ])
    selected_medicine_id = st.session_state.get("selected_history_medicine_id")

    with today_tab:
        st.subheader(tr("Today's schedule", "Aaj ka schedule"))
        active_medicines_by_id = {
            medicine["id"]: medicine for medicine in medicines
        }
        logs = [
            log for log in load_logs_for_day(today)
            if log["medicine_id"] in active_medicines_by_id
            and is_scheduled_on(
                active_medicines_by_id[log["medicine_id"]], today
            )
            and str(log.get("scheduled_time", ""))[:8]
            == str(
                active_medicines_by_id[log["medicine_id"]].get(
                    "scheduled_time", ""
                )
            )[:8]
        ]
        if not logs:
            st.info(tr("No active medicines scheduled today. Open the Add Medicine tab to add one.",
                       "Aaj ke liye koi active medicine schedule nahi hai. Dawa add karne ke liye Add Medicine tab kholein."))
        image_error = None
        for log in logs:
            with st.container(
                border=True,
                key=f"schedule_card_{log['id']}"
            ):
                image_column, medicine_column, actions_column = st.columns(
                    [1, 3.5, 1.5]
                )
                image_path = log.get("medicine_image_path")
                if image_path:
                    try:
                        image_result = sb.storage.from_("medicine-images").create_signed_url(
                            image_path, 3600
                        )
                        image_url = (
                            image_result.get("signedURL")
                            or image_result.get("signedUrl")
                        )
                        if image_url:
                            image_column.image(image_url, width=64)
                        else:
                            image_column.markdown("💊")
                    except Exception as error:
                        image_error = image_error or error
                        image_column.markdown("💊")
                else:
                    image_column.markdown("💊")
                
                if medicine_column.button(
                    f"💊 {log['medicine_name']} · View history",
                    key=f"open_dose_history_{log['id']}",
                    use_container_width=True
                ):
                    show_dose_history_dialog(
                        log["medicine_id"],
                        log["medicine_name"]
                    )
                if medicine_column.button(
                    tr("Edit", "संपादित करें"),
                    key=f"edit_medicine_{log['medicine_id']}",
                    use_container_width=False
                ):
                    initialize_medicine_editor(
                        active_medicines_by_id[log["medicine_id"]]
                    )
                    st.rerun()

                medicine_column.caption(
                    f"Time: {datetime.strptime(log['scheduled_time'][:8], '%H:%M:%S').strftime('%I:%M %p').lstrip('0')}"
                )
                render_weekday_row(
                    medicine_column,
                    active_medicines_by_id[log["medicine_id"]]
                )
                if log.get("medicine_notes"):
                    medicine_column.write(log["medicine_notes"])
                medicine_column.caption(
                    f"{tr('Status', 'Status')}: {log['status']}"
                )
                if actions_column.button(
                    tr("✓ Taken", "✓ Le li"),
                    key=f"taken_{log['id']}",
                    type="primary" if log.get("status") == "Taken" else "secondary",
                    use_container_width=True
                ):
                    sb.table("dose_logs").update({
                        "status": "Taken",
                        "updated_at": datetime.now().astimezone().isoformat()
                    }).eq("id", log["id"]).eq("user_id", user_id).execute()
                    st.rerun()
                if actions_column.button(
                    tr("✕ Skipped", "✕ Skip"),
                    key=f"skipped_{log['id']}",
                    type="primary" if log.get("status") == "Skipped" else "secondary",
                    use_container_width=True
                ):
                    sb.table("dose_logs").update({
                        "status": "Skipped",
                        "updated_at": datetime.now().astimezone().isoformat()
                    }).eq("id", log["id"]).eq("user_id", user_id).execute()
                    st.rerun()
        if image_error:
            st.warning(tr(
                "Medicine data loaded, but a photo could not be displayed. Check access to the private medicine-images bucket.",
                "Medicine data load ho gaya, lekin photo nahi dikh saki. Private medicine-images bucket ka access check karein."
            ))
            st.caption(f"Image storage error: {image_error}")

    with add_tab:
        st.subheader(tr("Add a medicine", "Medicine add karein"))
        st.session_state.setdefault("new_medicine_weekdays", list(range(7)))
        st.session_state.setdefault("add_medicine_form_version", 0)
        form_version = st.session_state["add_medicine_form_version"]
        with st.form("add_medicine", clear_on_submit=False):
            name = st.text_input(
                tr("Medicine name", "Medicine ka naam"),
                key=f"medicine_name_{form_version}"
            )
            medicine_image = st.file_uploader(
                tr("Upload Medicine Image", "Medicine ki photo upload karein"),
                type=["jpg", "jpeg", "png", "webp"],
                key=f"medicine_image_{form_version}",
                max_upload_size=5,
                help=tr(
                    "Optional. Choose a JPG, JPEG, PNG, or WebP image (maximum 5 MB).",
                    "Optional. JPG, JPEG, PNG, ya WebP image chunein (maximum 5 MB)."
                )
            )
            open_weekday_selector = st.form_submit_button(
                tr("Select Days", "दिन चुनें"),
                key="open_weekday_selector",
                use_container_width=True
            )
            selected_weekdays = [
                day for day, _, _, _ in WEEKDAYS
                if day in st.session_state["new_medicine_weekdays"]
            ]
            selected_day_names = [
                tr(english, hindi)
                for day, _, english, hindi in WEEKDAYS
                if day in selected_weekdays
            ]
            st.caption(
                tr("Selected: ", "चुने गए दिन: ")
                + (", ".join(selected_day_names) or tr("None", "कोई नहीं"))
            )
            time_value = st.time_input(
                tr("Scheduled time", "Dawa ka time"),
                key=f"scheduled_time_{form_version}"
            )
            notes = st.text_area(
                tr("Notes (optional)", "Notes (optional)"),
                placeholder=tr("Example: after food", "Jaise: khane ke baad"),
                key=f"medicine_notes_{form_version}"
            )
            add = st.form_submit_button(tr("Add medicine", "Medicine add karein"),
                                        use_container_width=True)
        if open_weekday_selector:
            show_weekday_selector()
        if add:
            selected_weekdays = [
                day for day, _, _, _ in WEEKDAYS
                if day in st.session_state["new_medicine_weekdays"]
            ]
            if not selected_weekdays:
                st.warning(tr(
                    "Select at least one weekday.",
                    "Kam se kam ek din chunein."
                ))
            elif not name.strip():
                st.warning(tr("Please enter a medicine name.", "Medicine ka naam daalein."))
            elif medicine_image and len(medicine_image.getvalue()) > 5 * 1024 * 1024:
                st.warning(tr(
                    "The photo must be 5 MB or smaller.",
                    "Photo 5 MB ya usse chhoti honi chahiye."
                ))
            else:
                extension = medicine_image.name.rsplit(".", 1)[-1].lower() if medicine_image else None
                image_types = {
                    "jpg": ("jpg", "image/jpeg"),
                    "jpeg": ("jpg", "image/jpeg"),
                    "png": ("png", "image/png"),
                    "webp": ("webp", "image/webp")
                }
                if medicine_image and extension not in image_types:
                    st.warning(tr(
                        "Choose a JPG, JPEG, PNG, or WebP photo.",
                        "JPG, JPEG, PNG, ya WebP photo chunein."
                    ))
                else:
                    image_path = None
                    upload_failed = False
                    if medicine_image:
                        stored_extension, content_type = image_types[extension]
                        image_path = f"{user_id}/{uuid.uuid4().hex}.{stored_extension}"
                        try:
                            sb.storage.from_("medicine-images").upload(
                                image_path,
                                medicine_image.getvalue(),
                                {"content-type": content_type}
                            )
                        except Exception as error:
                            upload_failed = True
                            st.error(tr(
                                "Photo upload failed. Check the medicine-images bucket and your upload permissions.",
                                "Photo upload nahi hui. medicine-images bucket aur upload permissions check karein."
                            ))
                            st.caption(f"Storage upload error: {error}")

                    if not upload_failed:
                        medicine_data = {
                            "user_id": user_id,
                            "name": name.strip(),
                            "scheduled_time": time_value.strftime("%H:%M:%S"),
                            "scheduled_days": selected_weekdays,
                            "notes": notes.strip(),
                            "active": True
                        }
                        if image_path:
                            medicine_data["image_path"] = image_path
                        try:
                            sb.table("medicines").insert(medicine_data).execute()
                        except Exception as error:
                            if image_path:
                                try:
                                    sb.storage.from_("medicine-images").remove([image_path])
                                except Exception as cleanup_error:
                                    st.warning(tr(
                                        "The medicine was not saved, and the uploaded photo could not be removed. Contact the app administrator.",
                                        "Medicine save nahi hui aur upload ki gayi photo remove nahi ho saki. App administrator se sampark karein."
                                    ))
                                    st.caption(f"Storage cleanup error: {cleanup_error}")
                            st.error(tr(
                                "Could not save the medicine. Check the medicines table and its access policies.",
                                "Medicine save nahi hui. medicines table aur uski access policies check karein."
                            ))
                            st.caption(f"Database insert error: {error}")
                            st.caption(tr(
                                "For weekday schedules, confirm the scheduled_days migration in the README has been applied to Supabase.",
                                "Weekday schedule ke liye Supabase par README wali scheduled_days migration apply hui hai, yeh check karein."
                            ))
                        else:
                            st.success(tr("Medicine added.", "Medicine add ho gayi."))
                            st.session_state["new_medicine_weekdays"] = list(range(7))
                            st.session_state["weekday_everyday"] = True
                            st.session_state["add_medicine_form_version"] += 1
                            st.rerun()

    with medicines_tab:
        st.subheader(tr("Your active medicines", "Aapki active medicines"))
        if medicines:
            for med in medicines:
                with st.container(border=True):
                    if st.button(
                        f"💊 {med['name']} — {med['scheduled_time'][:5]}",
                        key=f"view_old_history_{med['id']}",
                        use_container_width=True
                    ):
                        st.session_state["selected_history_medicine_id"] = med["id"]
                        st.session_state["open_dose_history"] = True
                        st.rerun()
                    render_weekday_row(st, med)
                    if med.get("notes"):
                        st.caption(med["notes"])
                    if st.button(tr("Deactivate", "Band karein"),
                                 key=f"deactivate_{med['id']}"):
                        sb.table("medicines").update({"active": False}).eq(
                            "id", med["id"]
                        ).eq("user_id", user_id).execute()
                        st.rerun()
        else:
            st.caption(tr("No active medicines.", "Koi active medicine nahi hai."))

    with old_medicines_tab:
        st.subheader(tr("Old medicines", "Purani dawaein"))
        if old_medicines:
            for med in old_medicines:
                with st.container(border=True):
                    if st.button(
                        f"💊 {med['name']} — {med['scheduled_time'][:5]}",
                        key=f"view_active_history_{med['id']}",
                        use_container_width=True
                    ):
                        show_dose_history_dialog(
                            med["id"],
                            med["name"],
                            taken_only=True,
                        )
                    render_weekday_row(st, med)
                    if med.get("notes"):
                        st.caption(med["notes"])
                    reactivate_column, delete_column = st.columns(2)
                    if reactivate_column.button(
                        tr("Reactivate", "Dobara chalu karein"),
                        key=f"reactivate_{med['id']}",
                        use_container_width=True,
                    ):
                        sb.table("medicines").update({"active": True}).eq(
                            "id", med["id"]
                        ).eq("user_id", user_id).execute()
                        st.rerun()
                    if delete_column.button(
                        tr("Delete permanently", "स्थायी रूप से हटाएँ"),
                        key=f"delete_old_medicine_{med['id']}",
                        type="secondary",
                        use_container_width=True,
                    ):
                        show_delete_medicine_dialog(med["id"], med["name"])
        else:
            st.caption(tr("No old medicines.", "Koi purani dawa nahi hai."))


    with history_tab:
        st.subheader(tr("Dose history", "Dose history"))

        selected_medicine_id = st.session_state.get(
            "selected_history_medicine_id"
        )

        all_medicines = (
            sb.table("medicines")
            .select("id,name")
            .eq("user_id", user_id)
            .execute()
            .data or []
        )

        medicine_options = {
            med["id"]: med["name"] for med in all_medicines
        }

        if selected_medicine_id in medicine_options:
            selected_name = medicine_options[selected_medicine_id]
            st.info(
                tr("Selected medicine: ", "Chuni hui dawa: ")
                + selected_name
            )

        history_date = st.date_input(
            tr("Choose a date", "Date chunein"),
            value=date.today(),
            key="history_date"
        )

        history_logs = load_logs_for_day(history_date)

        if selected_medicine_id:
            history_logs = [
                log for log in history_logs
                if str(log["medicine_id"]) == str(selected_medicine_id)
            ]

        if history_logs:
            st.dataframe(
                [{
                    "Medicine": x["medicine_name"],
                    "Date": str(history_date),
                    "Time": x["scheduled_time"][:5],
                    "Status": x["status"]
                } for x in history_logs],
                use_container_width=True,
                hide_index=True
            )
        else:
            st.caption(
                tr(
                    "No records found for this medicine on this date.",
                    "Is date par is dawa ka koi record nahi mila."
                )
            )

        if selected_medicine_id and st.button(
            tr("Show all medicines", "Sabhi dawaein dikhayein"),
            key="clear_history_filter"
        ):
            st.session_state.pop("selected_history_medicine_id", None)
            st.rerun()

except Exception as error:
    st.error(tr(
        "Could not load data. Check that your Supabase tables and RLS policies were created, and that Streamlit Secrets contain the correct Project URL and publishable key.",
        "Data load nahi hua. Check karein ki Supabase tables/RLS policies bani hain aur Streamlit Secrets mein sahi Project URL aur publishable key hai."
    ))
    st.caption(f"Supabase error: {error}")
    st.caption(tr("For troubleshooting, open the app logs in Streamlit Cloud. Never paste your secret key into the app or GitHub.",
                  "Problem solve karne ke liye Streamlit Cloud app logs dekhein. Secret key ko app ya GitHub mein kabhi paste na karein."))

# Footer
st.markdown(
    """
    <div style="
        text-align: center;
        color: #9CA3AF;
        font-size: 12px;
        margin-top: 40px;
        padding: 14px 0 8px 0;
        border-top: 1px solid #374151;
    ">
        © 2026 DOSE REMINDER · Developed by CARE.EXE
        <br>
        <span style="font-size: 10px; color: #6B7280;">
            All rights reserved.
        </span>
    </div>
    """,
    unsafe_allow_html=True
)