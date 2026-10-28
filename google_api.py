"""
Minimal Google OAuth 2.0 and API client (Gmail send, Calendar events).

Standard library only. Every endpoint can be pointed elsewhere through an
environment variable, which is how tests run against a fake Google server.

OAuth: authorization-code flow for a web client with PKCE (S256), offline
access so we receive a refresh token, and a single-use `state` value.
"""
import base64
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage

AUTH_URL = os.environ.get("GOOGLE_AUTH_URL", "https://accounts.google.com/o/oauth2/v2/auth")
TOKEN_URL = os.environ.get("GOOGLE_TOKEN_URL", "https://oauth2.googleapis.com/token")
REVOKE_URL = os.environ.get("GOOGLE_REVOKE_URL", "https://oauth2.googleapis.com/revoke")
USERINFO_URL = os.environ.get("GOOGLE_USERINFO_URL", "https://openidconnect.googleapis.com/v1/userinfo")
GMAIL_BASE = os.environ.get("GOOGLE_GMAIL_BASE", "https://gmail.googleapis.com/gmail/v1")
CALENDAR_BASE = os.environ.get("GOOGLE_CALENDAR_BASE", "https://www.googleapis.com/calendar/v3")

# Narrowest scopes that do the job: send-only Gmail (no reading the inbox) and
# calendar events (read events, mark one done). openid+email name the account.
SCOPES = [
    "openid",
    "email",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
]

CLIENT_FILE = os.environ.get(
    "GOOGLE_CLIENT_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "google_client.json"))


class GoogleError(Exception):
    def __init__(self, status, code, message):
        super().__init__("Google API %s: %s" % (status, message))
        self.status, self.code, self.message = status, code, message


def load_client():
    """OAuth client from GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET or google_client.json."""
    cid, secret = os.environ.get("GOOGLE_CLIENT_ID"), os.environ.get("GOOGLE_CLIENT_SECRET")
    if cid and secret:
        return {"client_id": cid, "client_secret": secret}
    if os.path.exists(CLIENT_FILE):
        with open(CLIENT_FILE) as f:
            data = json.load(f)
        data = data.get("web") or data.get("installed") or data
        if data.get("client_id") and data.get("client_secret"):
            return {"client_id": data["client_id"], "client_secret": data["client_secret"]}
    return None


def pkce_pair():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def auth_url(client, redirect_uri, state, challenge):
    params = {
        "client_id": client["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def _request(method, url, token=None, json_body=None, form=None, params=None, timeout=20):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
    headers = {"Accept": "application/json"}
    data = None
    if token:
        headers["Authorization"] = "Bearer " + token
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                body = json.loads(raw)
            except ValueError:
                body = {}
            err = body.get("error")
            if isinstance(err, dict):  # API style: {"error": {"code", "message", "status"}}
                code, message = err.get("status") or str(err.get("code")), err.get("message", "")
            else:  # OAuth style: {"error": "invalid_grant", "error_description": ...}
                code, message = err or str(e.code), body.get("error_description", "") or e.reason
            if attempt == 1 and (e.code == 429 or e.code >= 500):
                time.sleep(1)
                continue
            raise GoogleError(e.code, code, message)
        except urllib.error.URLError as e:
            if attempt == 1:
                time.sleep(1)
                continue
            raise GoogleError(0, "network", str(e.reason))


def _with_expiry(tok):
    tok = dict(tok)
    tok["expires_at"] = int(time.time()) + int(tok.get("expires_in", 3600))
    tok.pop("expires_in", None)
    tok.pop("id_token", None)  # not needed after sign-in; don't store it
    return tok


def exchange_code(client, code, verifier, redirect_uri):
    return _with_expiry(_request("POST", TOKEN_URL, form={
        "code": code, "client_id": client["client_id"], "client_secret": client["client_secret"],
        "redirect_uri": redirect_uri, "grant_type": "authorization_code", "code_verifier": verifier,
    }))


def refresh(client, refresh_token):
    tok = _with_expiry(_request("POST", TOKEN_URL, form={
        "client_id": client["client_id"], "client_secret": client["client_secret"],
        "refresh_token": refresh_token, "grant_type": "refresh_token",
    }))
    tok.setdefault("refresh_token", refresh_token)  # Google usually doesn't resend it
    return tok


def revoke(token):
    _request("POST", REVOKE_URL, form={"token": token})


def userinfo(access_token):
    return _request("GET", USERINFO_URL, token=access_token)


def gmail_send(access_token, to, subject, body, reply_to=None):
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return _request("POST", GMAIL_BASE + "/users/me/messages/send", token=access_token, json_body={"raw": raw})


def calendar_list(access_token, time_min, time_max, calendar_id="primary"):
    """All timed and all-day events in a window, including cancelled ones."""
    url = CALENDAR_BASE + "/calendars/%s/events" % urllib.parse.quote(calendar_id)
    events, page = [], None
    while True:
        params = {"timeMin": time_min, "timeMax": time_max, "singleEvents": "true",
                  "showDeleted": "true", "maxResults": 250}
        if page:
            params["pageToken"] = page
        data = _request("GET", url, token=access_token, params=params)
        events.extend(data.get("items", []))
        page = data.get("nextPageToken")
        if not page:
            return events


def calendar_patch(access_token, event_id, body, calendar_id="primary"):
    url = CALENDAR_BASE + "/calendars/%s/events/%s" % (
        urllib.parse.quote(calendar_id), urllib.parse.quote(event_id))
    return _request("PATCH", url, token=access_token, json_body=body)
