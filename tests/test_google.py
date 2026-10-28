"""Google connector tests against a fake Google server (no network, no real account).

    python3 tests/test_google.py
"""
import base64
import email
import hashlib
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
TMP = tempfile.mkdtemp()


class Fake:
    """State of the fake Google."""
    def reset(self):
        self.challenge = None
        self.codes = {}
        self.valid_access = set()
        self.refresh_tokens = {"rt-1"}
        self.counter = 0
        self.sent = []
        self.events = []
        self.revoked = []
        self.patches = []
        self.grant_scopes = ("openid https://www.googleapis.com/auth/userinfo.email "
                             "https://www.googleapis.com/auth/gmail.send "
                             "https://www.googleapis.com/auth/calendar.events")


FAKE = Fake()
FAKE.reset()


class GoogleHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authed(self):
        tok = (self.headers.get("Authorization") or "").replace("Bearer ", "")
        if tok not in FAKE.valid_access:
            self.reply(401, {"error": {"code": 401, "message": "Invalid Credentials", "status": "UNAUTHENTICATED"}})
            return False
        return True

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n).decode() if n else ""

    def new_access(self):
        FAKE.counter += 1
        tok = "at-%d" % FAKE.counter
        FAKE.valid_access.add(tok)
        return tok

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        raw = self.body()
        if path == "/token":
            f = dict(urllib.parse.parse_qsl(raw))
            if f.get("grant_type") == "authorization_code":
                expect = FAKE.codes.pop(f.get("code"), None)
                challenge = base64.urlsafe_b64encode(
                    hashlib.sha256(f.get("code_verifier", "").encode()).digest()).rstrip(b"=").decode()
                if not expect or challenge != expect:
                    return self.reply(400, {"error": "invalid_grant", "error_description": "Bad code or verifier"})
                return self.reply(200, {"access_token": self.new_access(), "refresh_token": "rt-1",
                                        "expires_in": 3599, "scope": FAKE.grant_scopes, "token_type": "Bearer",
                                        "id_token": "x.y.z"})
            if f.get("grant_type") == "refresh_token":
                if f.get("refresh_token") not in FAKE.refresh_tokens:
                    return self.reply(400, {"error": "invalid_grant", "error_description": "Token has been revoked."})
                return self.reply(200, {"access_token": self.new_access(), "expires_in": 3599})
            return self.reply(400, {"error": "unsupported_grant_type"})
        if path == "/revoke":
            tok = dict(urllib.parse.parse_qsl(raw)).get("token")
            FAKE.revoked.append(tok)
            FAKE.refresh_tokens.discard(tok)
            return self.reply(200, {})
        if path == "/gmail/v1/users/me/messages/send":
            if not self.authed():
                return
            msg = email.message_from_bytes(base64.urlsafe_b64decode(json.loads(raw)["raw"]))
            FAKE.sent.append({"to": msg["To"], "subject": msg["Subject"], "body": msg.get_payload(decode=True).decode()})
            return self.reply(200, {"id": "gm-%d" % len(FAKE.sent), "threadId": "th"})
        self.reply(404, {"error": {"code": 404, "message": "not found"}})

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/userinfo":
            if self.authed():
                self.reply(200, {"email": "owner@korastudio.test", "email_verified": True})
            return
        if url.path == "/calendar/v3/calendars/primary/events":
            if not self.authed():
                return
            qs = dict(urllib.parse.parse_qsl(url.query))
            assert qs["singleEvents"] == "true" and qs["showDeleted"] == "true"
            # Two pages, to exercise pagination.
            half = len(FAKE.events) // 2
            if qs.get("pageToken") == "p2":
                return self.reply(200, {"items": FAKE.events[half:]})
            return self.reply(200, {"items": FAKE.events[:half], "nextPageToken": "p2"})
        self.reply(404, {"error": {"code": 404, "message": "not found"}})

    def do_PATCH(self):
        url = urllib.parse.urlparse(self.path)
        if not self.authed():
            return
        eid = urllib.parse.unquote(url.path.rsplit("/", 1)[-1])
        patch = json.loads(self.body())
        FAKE.patches.append((eid, patch))
        for e in FAKE.events:
            if e["id"] == eid:
                e.update(patch)
                return self.reply(200, e)
        self.reply(404, {"error": {"code": 404, "message": "not found"}})


GOOGLE = ThreadingHTTPServer(("127.0.0.1", 0), GoogleHandler)
threading.Thread(target=GOOGLE.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % GOOGLE.server_address[1]

os.environ.update({
    "INVISIBLE_DB": os.path.join(TMP, "test.db"),
    "INVISIBLE_LLM": "off",
    "INVISIBLE_KEY_FILE": os.path.join(TMP, "keys", "master.key"),
    "GOOGLE_CLIENT_ID": "test-client.apps.googleusercontent.com",
    "GOOGLE_CLIENT_SECRET": "test-secret",
    "GOOGLE_AUTH_URL": BASE + "/auth",
    "GOOGLE_TOKEN_URL": BASE + "/token",
    "GOOGLE_REVOKE_URL": BASE + "/revoke",
    "GOOGLE_USERINFO_URL": BASE + "/userinfo",
    "GOOGLE_GMAIL_BASE": BASE + "/gmail/v1",
    "GOOGLE_CALENDAR_BASE": BASE + "/calendar/v3",
})

import server as s  # noqa: E402

APP = ThreadingHTTPServer(("127.0.0.1", 0), s.Handler)
threading.Thread(target=APP.serve_forever, daemon=True).start()
APP_BASE = "http://127.0.0.1:%d" % APP.server_address[1]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def get_redirect(path):
    opener = urllib.request.build_opener(NoRedirect)
    try:
        opener.open(APP_BASE + path)
    except urllib.error.HTTPError as e:
        assert e.code == 302, e.code
        return e.headers["Location"]
    raise AssertionError("expected a redirect")


def post(path, body=None):
    req = urllib.request.Request(APP_BASE + path, data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def gtime(dt):
    return dt.astimezone().isoformat(timespec="seconds")


def event(eid, title, start, desc="", attendee="ada@client.test", status="confirmed", name=None):
    return {"id": eid, "status": status, "summary": title, "description": desc,
            "start": {"dateTime": gtime(start)}, "end": {"dateTime": gtime(start + timedelta(hours=1))},
            "attendees": [{"email": "owner@korastudio.test", "self": True},
                          {"email": attendee, "displayName": name or attendee.split("@")[0].title()}]}


def connect():
    """Run the OAuth dance: start -> (user consents) -> callback."""
    loc = get_redirect("/oauth/google/start")
    qs = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(loc).query))
    FAKE.codes["code-1"] = qs["code_challenge"]
    back = get_redirect("/oauth/google/callback?" + urllib.parse.urlencode({"code": "code-1", "state": qs["state"]}))
    return loc, qs, back


def bot_messages():
    return [m["body"] for m in s.q("SELECT body FROM chat WHERE sender='bot' ORDER BY id")]


class GoogleFlow(unittest.TestCase):
    def setUp(self):
        s.reset()  # may revoke the previous test's token at the fake, so reset the fake after
        FAKE.reset()
        now = datetime.now().replace(second=0, microsecond=0)
        FAKE.events = [
            event("past-done", "Old job #done", now - timedelta(days=3), "Amount: 500", name="Ada Lovelace"),
            event("job-1", "Strategy session", now - timedelta(hours=2), "Amount: $1,250", name="Ada Lovelace"),
            event("soon", "Kickoff call", now + timedelta(hours=3), "", attendee="ben@client.test"),
            event("allday-ish", "No guests", now + timedelta(days=2), "Amount: 90", attendee="owner@korastudio.test"),
        ]

    def test_start_redirect_uses_pkce_offline_and_minimal_scopes(self):
        loc = get_redirect("/oauth/google/start")
        qs = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(loc).query))
        self.assertTrue(loc.startswith(BASE + "/auth?"))
        self.assertEqual(qs["code_challenge_method"], "S256")
        self.assertEqual(qs["access_type"], "offline")
        self.assertEqual(qs["redirect_uri"], s.redirect_uri())
        self.assertEqual(set(qs["scope"].split()), set(s.google.SCOPES))
        self.assertNotIn("gmail.readonly", qs["scope"])

    def test_callback_rejects_unknown_or_reused_state(self):
        back = get_redirect("/oauth/google/callback?code=x&state=forged")
        self.assertIn("google_error", back)
        loc, qs, back = connect()
        self.assertEqual(back, "/audit?google=connected")
        again = get_redirect("/oauth/google/callback?" + urllib.parse.urlencode({"code": "code-1", "state": qs["state"]}))
        self.assertIn("expired%20or%20was%20already%20used", again)

    def test_tokens_are_encrypted_at_rest(self):
        connect()
        row = s.google_row()
        self.assertEqual(row["account"], "owner@korastudio.test")
        self.assertNotIn("rt-1", row["secret"])
        self.assertNotIn("at-", row["secret"])
        tok = s.get_vault().open("google", row["secret"])
        self.assertEqual(tok["refresh_token"], "rt-1")
        self.assertNotIn("id_token", tok)
        key_file = os.environ["INVISIBLE_KEY_FILE"]
        self.assertEqual(stat.S_IMODE(os.stat(key_file).st_mode), 0o600)
        with open(key_file, "rb") as f:
            master = f.read().strip()
        dump = "\n".join(str(tuple(r)) for t in ("settings", "connections") for r in s.db().execute("SELECT * FROM " + t))
        self.assertNotIn(master.decode(), dump)
        # A blob sealed for another purpose can't be swapped in.
        with self.assertRaises(s.vault.VaultError):
            s.get_vault().open("slack", row["secret"])
        # The browser never sees tokens.
        status, state = post("/api/chat", {"text": "help"})
        self.assertNotIn("rt-1", json.dumps(state))
        self.assertNotIn("at-1", json.dumps(state))

    def test_missing_scope_is_refused_and_revoked(self):
        FAKE.grant_scopes = "openid https://www.googleapis.com/auth/calendar.events"
        loc, qs, back = connect()
        self.assertIn("gmail.send", urllib.parse.unquote(back))
        self.assertIsNone(s.google_row())
        self.assertIn("rt-1", FAKE.revoked)

    def test_first_sync_is_a_baseline_and_mirrors_events(self):
        connect()
        self.assertEqual(s.get_setting("clock"), "real")
        evs = {e["external_id"]: e for e in s.q("SELECT * FROM cal_events WHERE source='google'")}
        self.assertEqual(set(evs), {"past-done", "job-1", "soon", "allday-ish"})
        self.assertEqual(evs["past-done"]["status"], "completed")
        self.assertEqual(evs["job-1"]["amount"], 1250)
        self.assertIsNone(evs["allday-ish"]["contact_id"])
        ada = s.q1("SELECT * FROM contacts WHERE email='ada@client.test'")
        self.assertEqual(ada["name"], "Ada Lovelace")
        # Already-done event did not trigger an invoice on connect.
        self.assertFalse(any("Old job" in m for m in bot_messages()))
        # The sandbox calendar is hidden while Google is connected.
        _, state = post("/api/chat", {"text": "help"})
        self.assertTrue(all(e["source"] == "google" for e in state["events"]))

    def test_done_tag_in_google_invoices_via_gmail_in_test_mode(self):
        connect()
        FAKE.events[1]["summary"] = "Strategy session #done"
        s.tick_real()
        pending = s.q("SELECT * FROM proposals WHERE status='pending' AND summary LIKE '%Strategy%'")
        self.assertEqual(len(pending), 1)
        self.assertIn("first message to Ada Lovelace", pending[0]["reasons"])
        s.owner_message("approve #%d" % pending[0]["id"])
        self.assertEqual(len(FAKE.sent), 1)
        sent = FAKE.sent[0]
        self.assertEqual(sent["to"], "owner@korastudio.test")  # test mode: to the owner
        self.assertTrue(sent["subject"].startswith("[Test, for ada@client.test] Invoice INV-1003"))
        self.assertIn("$1,250", sent["body"])
        out = s.q1("SELECT * FROM outbox ORDER BY id DESC")
        self.assertEqual(out["external_id"], "gm-1")
        self.assertIn("Test mode", out["note"])

    def test_live_email_goes_to_client_and_undo_sends_correction(self):
        connect()
        post("/api/settings/live_email", {"on": True})
        FAKE.events[1]["summary"] = "Strategy session #done"
        s.tick_real()
        inv = s.q1("SELECT id FROM proposals WHERE status='pending' AND summary LIKE '%Strategy%'")
        s.owner_message("approve #%d" % inv["id"])
        self.assertEqual(FAKE.sent[-1]["to"], "ada@client.test")
        s.owner_message("undo")
        self.assertEqual(FAKE.sent[-1]["to"], "ada@client.test")
        self.assertIn("disregard", FAKE.sent[-1]["subject"].lower())

    def test_mark_done_in_app_patches_google(self):
        connect()
        job = s.q1("SELECT * FROM cal_events WHERE external_id='job-1'")
        status, state = post("/api/sandbox/event/complete", {"id": job["id"]})
        self.assertEqual(status, 200)
        self.assertEqual(FAKE.patches, [("job-1", {"summary": "Strategy session #done"})])
        self.assertTrue(any("Strategy session" in m and "Approve?" in m for m in bot_messages()))

    def test_expired_access_token_is_refreshed_transparently(self):
        connect()
        FAKE.valid_access.clear()  # Google rejects the stored access token
        FAKE.events[1]["summary"] = "Strategy session #done"
        s.tick_real()
        self.assertEqual(s.google_row()["status"], "connected")
        self.assertIsNone(s.google_row()["last_error"])
        tok = s.get_vault().open("google", s.google_row()["secret"])
        self.assertEqual(tok["refresh_token"], "rt-1")
        self.assertIn(tok["access_token"], FAKE.valid_access)

    def test_revoked_access_asks_owner_to_reconnect(self):
        connect()
        FAKE.valid_access.clear()
        FAKE.refresh_tokens.clear()
        s.tick_real()
        self.assertEqual(s.google_row()["status"], "needs_reconnect")
        self.assertTrue(any("lost access" in m for m in bot_messages()))
        self.assertIn("revoked", s.google_row()["last_error"])
        s.tick_real()  # no repeated alerts
        self.assertEqual(sum("lost access" in m for m in bot_messages()), 1)

    def test_cancelled_in_google_withdraws_pending_reminder(self):
        connect()
        # Kickoff call is 3 hours away with a new guest: the reminder asks first.
        s.tick_real()
        pend = s.q("SELECT * FROM proposals WHERE status='pending' AND summary LIKE '%Kickoff%'")
        self.assertEqual(len(pend), 1)
        self.assertIn("Email a reminder to ben@client.test", pend[0]["draft"])  # no phone -> email
        FAKE.events[2] = {"id": "soon", "status": "cancelled"}
        s.tick_real()
        self.assertEqual(s.q1("SELECT status FROM proposals WHERE id=?", (pend[0]["id"],))["status"], "cancelled")

    def test_disconnect_revokes_and_restores_demo(self):
        connect()
        status, state = post("/api/connections/google/disconnect")
        self.assertEqual(status, 200)
        self.assertIn("rt-1", FAKE.revoked)
        self.assertIsNone(s.google_row())
        self.assertEqual(state["clock"], "simulated")
        self.assertTrue(all(e["source"] == "sandbox" for e in state["events"]))

    def test_clock_cannot_be_moved_while_connected(self):
        connect()
        status, body = post("/api/sandbox/clock/advance", {"hours": 24})
        self.assertEqual(status, 400)
        self.assertIn("real", body["error"])

    def test_amount_parsing(self):
        self.assertEqual(s.parse_amount("Amount: 1,200"), 1200)
        self.assertEqual(s.parse_amount("fee = ₦75000"), 75000)
        self.assertEqual(s.parse_amount("Workshop, $450 agreed"), 450)
        self.assertEqual(s.parse_amount("no money here"), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
