"""
Backend server per "To Smile - Gestione Finanziaria" (modalita' multi-azienda)
--------------------------------------------------------------------------
- Un account MASTER (admin) puo' creare/eliminare aziende e vedere i dati
  di tutte.
- Ogni azienda ha un nome + password propri e puo' leggere/scrivere SOLO
  i propri dati.
- I dati di ogni azienda sono salvati come un unico blocco JSON (stessa
  forma che l'app usava finora in localStorage), cosi' il collegamento
  con il frontend esistente e' diretto.

Avvio in locale:
    pip install -r requirements.txt
    python app.py
Il server parte su http://127.0.0.1:5000
"""
import os
import json
import sqlite3
import uuid
import secrets
import smtplib
import datetime
from email.mime.text import MIMEText
from flask import Flask, request, jsonify, session, g, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
DB_PATH = os.path.join(DATA_DIR, "data.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = Flask(__name__, static_folder=STATIC_DIR)
app.secret_key = os.environ.get("SECRET_KEY", "cambia-questa-chiave-in-produzione-" + uuid.uuid4().hex)
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(minutes=10),
    SESSION_REFRESH_EACH_REQUEST=True,
)

from werkzeug.security import generate_password_hash, check_password_hash


# ---------------------------------------------------------------- DB setup
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute(
        """CREATE TABLE IF NOT EXISTS admin_config (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            password_hash TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS companies (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            data TEXT NOT NULL DEFAULT '{}',
            suspended INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT,
            plan TEXT NOT NULL DEFAULT 'free',
            email TEXT,
            approved INTEGER NOT NULL DEFAULT 1
        )"""
    )
    # migrazione sicura: se il database esisteva gia' (creato prima di questa
    # funzione), aggiunge solo le colonne mancanti senza toccare i dati.
    existing_cols = [r[1] for r in db.execute("PRAGMA table_info(companies)").fetchall()]
    if "suspended" not in existing_cols:
        db.execute("ALTER TABLE companies ADD COLUMN suspended INTEGER NOT NULL DEFAULT 0")
    if "expires_at" not in existing_cols:
        db.execute("ALTER TABLE companies ADD COLUMN expires_at TEXT")
    if "plan" not in existing_cols:
        db.execute("ALTER TABLE companies ADD COLUMN plan TEXT NOT NULL DEFAULT 'free'")
    if "email" not in existing_cols:
        db.execute("ALTER TABLE companies ADD COLUMN email TEXT")
    if "approved" not in existing_cols:
        # le aziende gia' esistenti (create prima di questa funzione) sono
        # gia' attive: non deve servire una nuova approvazione per loro
        db.execute("ALTER TABLE companies ADD COLUMN approved INTEGER NOT NULL DEFAULT 1")
    db.execute(
        """CREATE TABLE IF NOT EXISTS password_reset_requests (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            company_name TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            resolved INTEGER NOT NULL DEFAULT 0
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS password_reset_tokens (
            token TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0
        )"""
    )
    db.commit()
    db.close()


def send_email_safe(to_addr, subject, body):
    """Invia una email tramite Gmail SMTP. Non fa mai fallire la richiesta
    HTTP in corso se l'invio non riesce (es. SMTP non configurato): logga
    solo un avviso e restituisce False."""
    if not to_addr:
        return False
    smtp_email = os.environ.get("SMTP_EMAIL")
    smtp_password = os.environ.get("SMTP_APP_PASSWORD")
    if not smtp_email or not smtp_password:
        app.logger.warning("SMTP non configurato: email non inviata a %s (%s)", to_addr, subject)
        return False
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = smtp_email
        msg["To"] = to_addr
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as server:
            server.login(smtp_email, smtp_password)
            server.sendmail(smtp_email, [to_addr], msg.as_string())
        return True
    except Exception as e:
        app.logger.warning("Invio email fallito verso %s: %s", to_addr, e)
        return False


def get_admin_notify_email():
    return os.environ.get("ADMIN_NOTIFY_EMAIL", "")


def now_iso():
    return datetime.datetime.utcnow().isoformat() + "Z"


# ---------------------------------------------------------------- helpers
def require_admin():
    return session.get("role") == "admin"


def require_company():
    return session.get("role") == "company" and session.get("company_id")


def json_error(msg, code=400):
    return jsonify({"error": msg}), code


def check_company_access():
    """Verifica sospensione/scadenza per l'azienda gia' in sessione.
    Va richiamata a OGNI richiesta autenticata (non solo al login), cosi'
    se un admin sospende un'azienda mentre un cliente e' gia' collegato,
    l'accesso si interrompe subito, non solo al prossimo login."""
    db = get_db()
    row = db.execute(
        "SELECT suspended, expires_at FROM companies WHERE id=?",
        (session.get("company_id"),)
    ).fetchone()
    if row is None:
        session.clear()
        return False, json_error("Azienda non trovata.", 404)
    if row["suspended"]:
        session.clear()
        return False, json_error("Accesso sospeso. Contatta l'amministratore.", 403)
    if row["expires_at"]:
        try:
            exp = datetime.datetime.fromisoformat(row["expires_at"].replace("Z", ""))
            if datetime.datetime.utcnow() > exp:
                session.clear()
                return False, json_error("Abbonamento scaduto. Contatta l'amministratore per rinnovare.", 403)
        except ValueError:
            pass
    return True, None


# chiavi di primo livello dei dati aziendali che sono funzioni premium
GATED_TOP_KEYS = ("incassi", "listino", "bankAccounts", "bankBalances", "statsYears")
# dentro "budget", solo l'elenco dettagliato voci/percentuali e' premium:
# i valori aggregati (fatturato stimato, ecc.) restano liberi
GATED_BUDGET_SUBKEYS = ("fixed", "varPct")


def strip_gated_data(data, plan):
    """Toglie dalla risposta le sezioni premium se l'azienda e' sul piano free.
    I dati restano salvati nel database (nel caso passi a pro in futuro),
    vengono solo nascosti nella lettura."""
    if plan == "pro" or not isinstance(data, dict):
        return data
    data = dict(data)
    for k in GATED_TOP_KEYS:
        data.pop(k, None)
    if isinstance(data.get("budget"), dict):
        budget = dict(data["budget"])
        for k in GATED_BUDGET_SUBKEYS:
            budget.pop(k, None)
        data["budget"] = budget
    return data


def merge_gated_data(incoming, existing_raw, plan):
    """Quando un'azienda free salva i propri dati, ignora qualunque modifica
    alle sezioni premium (anche se qualcuno provasse a scriverle chiamando
    l'API direttamente, scavalcando l'app) e mantiene quello che c'era
    gia' salvato per quelle sezioni."""
    if plan == "pro" or not isinstance(incoming, dict):
        return incoming
    existing = {}
    try:
        existing = json.loads(existing_raw or "{}")
    except (TypeError, ValueError):
        existing = {}
    for k in GATED_TOP_KEYS:
        if k in existing:
            incoming[k] = existing[k]
        else:
            incoming.pop(k, None)
    if isinstance(incoming.get("budget"), dict):
        existing_budget = existing.get("budget") if isinstance(existing.get("budget"), dict) else {}
        for k in GATED_BUDGET_SUBKEYS:
            if k in existing_budget:
                incoming["budget"][k] = existing_budget[k]
            else:
                incoming["budget"].pop(k, None)
    return incoming


# ================================================================= ADMIN
@app.route("/api/admin/status", methods=["GET"])
def admin_status():
    db = get_db()
    row = db.execute("SELECT 1 FROM admin_config WHERE id=1").fetchone()
    return jsonify({
        "configured": row is not None,
        "loggedIn": require_admin()
    })


@app.route("/api/admin/setup", methods=["POST"])
def admin_setup():
    db = get_db()
    row = db.execute("SELECT 1 FROM admin_config WHERE id=1").fetchone()
    if row is not None:
        return json_error("La password amministratore e' gia' stata impostata.", 409)
    body = request.get_json(force=True, silent=True) or {}
    password = (body.get("password") or "").strip()
    if len(password) < 6:
        return json_error("La password deve avere almeno 6 caratteri.")
    db.execute(
        "INSERT INTO admin_config (id, password_hash) VALUES (1, ?)",
        (generate_password_hash(password),)
    )
    db.commit()
    session.clear()
    session.permanent = True
    session["role"] = "admin"
    return jsonify({"ok": True})


@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    db = get_db()
    row = db.execute("SELECT password_hash FROM admin_config WHERE id=1").fetchone()
    if row is None:
        return json_error("Nessuna password amministratore configurata. Esegui prima il setup.", 409)
    body = request.get_json(force=True, silent=True) or {}
    password = body.get("password") or ""
    if not check_password_hash(row["password_hash"], password):
        return json_error("Password errata.", 401)
    session.clear()
    session.permanent = True
    session["role"] = "admin"
    return jsonify({"ok": True})


@app.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/admin/companies", methods=["GET"])
def admin_list_companies():
    if not require_admin():
        return json_error("Non autorizzato.", 401)
    db = get_db()
    rows = db.execute(
        "SELECT id, name, email, created_at, suspended, expires_at, plan, approved FROM companies ORDER BY created_at DESC"
    ).fetchall()
    return jsonify({"companies": [dict(r) for r in rows]})


@app.route("/api/admin/companies", methods=["POST"])
def admin_create_company():
    if not require_admin():
        return json_error("Non autorizzato.", 401)
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    password = (body.get("password") or "").strip()
    email = (body.get("email") or "").strip() or None
    if not name:
        return json_error("Il nome dell'azienda e' obbligatorio.")
    if len(password) < 4:
        return json_error("La password deve avere almeno 4 caratteri.")
    db = get_db()
    cid = uuid.uuid4().hex[:12]
    # creata direttamente dal master: gia' approvata, non serve revisione
    db.execute(
        "INSERT INTO companies (id, name, email, password_hash, created_at, data, suspended, expires_at, plan, approved) "
        "VALUES (?,?,?,?,?,?,0,NULL,'free',1)",
        (cid, name, email, generate_password_hash(password), now_iso(), "{}")
    )
    db.commit()
    return jsonify({"id": cid, "name": name})


@app.route("/api/admin/companies/<cid>/approve", methods=["PUT"])
def admin_approve_company(cid):
    if not require_admin():
        return json_error("Non autorizzato.", 401)
    db = get_db()
    row = db.execute("SELECT name, email FROM companies WHERE id=?", (cid,)).fetchone()
    if row is None:
        return json_error("Azienda non trovata.", 404)
    db.execute("UPDATE companies SET approved=1 WHERE id=?", (cid,))
    db.commit()
    if row["email"]:
        send_email_safe(
            row["email"],
            "Il tuo account e' stato attivato",
            "Ciao,\nil tuo account \"" + row["name"] + "\" e' stato approvato: da ora puoi accedere normalmente."
        )
    return jsonify({"ok": True})


@app.route("/api/admin/companies/<cid>", methods=["DELETE"])
def admin_delete_company(cid):
    if not require_admin():
        return json_error("Non autorizzato.", 401)
    db = get_db()
    db.execute("DELETE FROM companies WHERE id=?", (cid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/companies/<cid>/rename", methods=["PUT"])
def admin_rename_company(cid):
    if not require_admin():
        return json_error("Non autorizzato.", 401)
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return json_error("Il nome non puo' essere vuoto.")
    db = get_db()
    db.execute("UPDATE companies SET name=? WHERE id=?", (name, cid))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/companies/<cid>/password", methods=["PUT"])
def admin_reset_company_password(cid):
    if not require_admin():
        return json_error("Non autorizzato.", 401)
    body = request.get_json(force=True, silent=True) or {}
    password = (body.get("password") or "").strip()
    if len(password) < 4:
        return json_error("La password deve avere almeno 4 caratteri.")
    db = get_db()
    db.execute("UPDATE companies SET password_hash=? WHERE id=?", (generate_password_hash(password), cid))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/companies/<cid>/suspend", methods=["PUT"])
def admin_suspend_company(cid):
    if not require_admin():
        return json_error("Non autorizzato.", 401)
    db = get_db()
    db.execute("UPDATE companies SET suspended=1 WHERE id=?", (cid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/companies/<cid>/unsuspend", methods=["PUT"])
def admin_unsuspend_company(cid):
    if not require_admin():
        return json_error("Non autorizzato.", 401)
    db = get_db()
    db.execute("UPDATE companies SET suspended=0 WHERE id=?", (cid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/companies/<cid>/expiry", methods=["PUT"])
def admin_set_company_expiry(cid):
    if not require_admin():
        return json_error("Non autorizzato.", 401)
    body = request.get_json(force=True, silent=True) or {}
    expires_at = body.get("expiresAt") or None  # stringa data "YYYY-MM-DD", o None per togliere la scadenza
    db = get_db()
    db.execute("UPDATE companies SET expires_at=? WHERE id=?", (expires_at, cid))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/companies/<cid>/plan", methods=["PUT"])
def admin_set_company_plan(cid):
    if not require_admin():
        return json_error("Non autorizzato.", 401)
    body = request.get_json(force=True, silent=True) or {}
    plan = body.get("plan")
    if plan not in ("free", "pro"):
        return json_error("Piano non valido: deve essere 'free' o 'pro'.")
    db = get_db()
    db.execute("UPDATE companies SET plan=? WHERE id=?", (plan, cid))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/companies/<cid>/email", methods=["PUT"])
def admin_set_company_email(cid):
    if not require_admin():
        return json_error("Non autorizzato.", 401)
    body = request.get_json(force=True, silent=True) or {}
    email = (body.get("email") or "").strip() or None
    if email and "@" not in email:
        return json_error("Indirizzo email non valido.")
    db = get_db()
    db.execute("UPDATE companies SET email=? WHERE id=?", (email, cid))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/password-reset-requests", methods=["GET"])
def admin_list_reset_requests():
    if not require_admin():
        return json_error("Non autorizzato.", 401)
    db = get_db()
    rows = db.execute(
        "SELECT id, company_id, company_name, requested_at, resolved FROM password_reset_requests "
        "WHERE resolved=0 ORDER BY requested_at DESC"
    ).fetchall()
    return jsonify({"requests": [dict(r) for r in rows]})


@app.route("/api/admin/password-reset-requests/<rid>/resolve", methods=["PUT"])
def admin_resolve_reset_request(rid):
    if not require_admin():
        return json_error("Non autorizzato.", 401)
    db = get_db()
    db.execute("UPDATE password_reset_requests SET resolved=1 WHERE id=?", (rid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/companies/<cid>/data", methods=["GET"])
def admin_view_company_data(cid):
    if not require_admin():
        return json_error("Non autorizzato.", 401)
    db = get_db()
    row = db.execute("SELECT data FROM companies WHERE id=?", (cid,)).fetchone()
    if row is None:
        return json_error("Azienda non trovata.", 404)
    return jsonify({"data": json.loads(row["data"])})


# =============================================================== COMPANY
@app.route("/api/company/login", methods=["POST"])
def company_login():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    password = body.get("password") or ""
    db = get_db()
    row = db.execute(
        "SELECT id, password_hash, name, suspended, expires_at, approved FROM companies WHERE name=?",
        (name,)
    ).fetchone()
    if row is None or not check_password_hash(row["password_hash"], password):
        return json_error("Nome azienda o password errati.", 401)
    if not row["approved"]:
        return json_error("Il tuo account e' in attesa di approvazione. Riceverai una email quando sara' attivo.", 403)
    if row["suspended"]:
        return json_error("Accesso sospeso. Contatta l'amministratore.", 403)
    if row["expires_at"]:
        try:
            exp = datetime.datetime.fromisoformat(row["expires_at"].replace("Z", ""))
            if datetime.datetime.utcnow() > exp:
                return json_error("Abbonamento scaduto. Contatta l'amministratore per rinnovare.", 403)
        except ValueError:
            pass
    session.clear()
    session.permanent = True
    session["role"] = "company"
    session["company_id"] = row["id"]
    return jsonify({"ok": True, "name": row["name"]})


@app.route("/api/company/register", methods=["POST"])
def company_register():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip()
    password = (body.get("password") or "").strip()
    if not name:
        return json_error("Inserisci il nome della tua azienda.")
    if not email or "@" not in email:
        return json_error("Inserisci un indirizzo email valido.")
    if len(password) < 4:
        return json_error("La password deve avere almeno 4 caratteri.")
    db = get_db()
    existing = db.execute("SELECT id FROM companies WHERE name=?", (name,)).fetchone()
    if existing is not None:
        return json_error("Esiste gia' un account con questo nome azienda. Scegline un altro o contatta l'assistenza.")
    cid = uuid.uuid4().hex[:12]
    db.execute(
        "INSERT INTO companies (id, name, email, password_hash, created_at, data, suspended, expires_at, plan, approved) "
        "VALUES (?,?,?,?,?,?,0,NULL,'free',0)",
        (cid, name, email, generate_password_hash(password), now_iso(), "{}")
    )
    db.commit()
    # riepilogo al cliente, mandato SOLO ora: e' l'unico momento in cui il
    # server vede la password in chiaro, prima di cifrarla per sempre
    send_email_safe(
        email,
        "Richiesta ricevuta - " + name,
        "Ciao,\nabbiamo ricevuto la tua richiesta di accesso. Ecco il riepilogo dei dati che hai inserito:\n\n"
        "Nome azienda: " + name + "\n"
        "Email: " + email + "\n"
        "Password scelta: " + password + "\n\n"
        "Conservali in un posto sicuro: sono le credenziali che userai per accedere.\n"
        "La richiesta e' ora in attesa di approvazione: riceverai un'altra email quando l'account sara' attivo."
    )
    send_email_safe(
        get_admin_notify_email(),
        "Nuova richiesta di accesso: " + name,
        "L'azienda \"" + name + "\" (" + email + ") ha richiesto un account.\n"
        "Vai sul pannello master per approvarla, prima che possa accedere."
    )
    return jsonify({"ok": True})


@app.route("/api/company/logout", methods=["POST"])
def company_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/company/me", methods=["GET"])
def company_me():
    if not require_company():
        return jsonify({"loggedIn": False})
    ok, err = check_company_access()
    if not ok:
        return jsonify({"loggedIn": False})
    db = get_db()
    row = db.execute("SELECT name, plan FROM companies WHERE id=?", (session["company_id"],)).fetchone()
    if row is None:
        session.clear()
        return jsonify({"loggedIn": False})
    return jsonify({"loggedIn": True, "name": row["name"], "plan": row["plan"] or "free"})


@app.route("/api/company/data", methods=["GET"])
def company_get_data():
    if not require_company():
        return json_error("Non autorizzato.", 401)
    ok, err = check_company_access()
    if not ok:
        return err
    db = get_db()
    row = db.execute("SELECT data, plan FROM companies WHERE id=?", (session["company_id"],)).fetchone()
    if row is None:
        return json_error("Azienda non trovata.", 404)
    plan = row["plan"] or "free"
    data = strip_gated_data(json.loads(row["data"]), plan)
    return jsonify({"data": data, "plan": plan})


@app.route("/api/company/data", methods=["PUT"])
def company_save_data():
    if not require_company():
        return json_error("Non autorizzato.", 401)
    ok, err = check_company_access()
    if not ok:
        return err
    body = request.get_json(force=True, silent=True)
    if body is None or "data" not in body:
        return json_error("Corpo della richiesta non valido: manca 'data'.")
    db = get_db()
    row = db.execute("SELECT data, plan FROM companies WHERE id=?", (session["company_id"],)).fetchone()
    if row is None:
        return json_error("Azienda non trovata.", 404)
    plan = row["plan"] or "free"
    incoming = merge_gated_data(body["data"], row["data"], plan)
    db.execute(
        "UPDATE companies SET data=? WHERE id=?",
        (json.dumps(incoming), session["company_id"])
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/company/change-password", methods=["PUT"])
def company_change_password():
    if not require_company():
        return json_error("Non autorizzato.", 401)
    ok, err = check_company_access()
    if not ok:
        return err
    body = request.get_json(force=True, silent=True) or {}
    current = body.get("currentPassword") or ""
    new = (body.get("newPassword") or "").strip()
    db = get_db()
    row = db.execute("SELECT password_hash FROM companies WHERE id=?", (session["company_id"],)).fetchone()
    if row is None:
        return json_error("Azienda non trovata.", 404)
    if not check_password_hash(row["password_hash"], current):
        return json_error("Password attuale errata.", 401)
    if len(new) < 4:
        return json_error("La nuova password deve avere almeno 4 caratteri.")
    db.execute(
        "UPDATE companies SET password_hash=? WHERE id=?",
        (generate_password_hash(new), session["company_id"])
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/company/forgot-password", methods=["POST"])
def company_forgot_password():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return json_error("Inserisci il nome della tua azienda.")
    db = get_db()
    row = db.execute("SELECT id, name, email FROM companies WHERE name=?", (name,)).fetchone()
    # rispondiamo sempre ok, azienda trovata o no: cosi' chi prova nomi a
    # caso non scopre quali aziende esistono davvero
    if row is not None:
        if row["email"]:
            token = secrets.token_urlsafe(32)
            expires = (datetime.datetime.utcnow() + datetime.timedelta(hours=1)).isoformat()
            db.execute(
                "INSERT INTO password_reset_tokens (token, company_id, created_at, expires_at, used) "
                "VALUES (?,?,?,?,0)",
                (token, row["id"], now_iso(), expires)
            )
            db.commit()
            reset_link = request.host_url.rstrip("/") + "/?reset=" + token
            send_email_safe(
                row["email"],
                "Reimposta la password - " + row["name"],
                "Ciao,\nhai richiesto di reimpostare la password per \"" + row["name"] + "\".\n\n"
                "Clicca qui per sceglierne una nuova (valido 1 ora):\n" + reset_link + "\n\n"
                "Se non sei stato tu a richiederlo, ignora pure questa email: la tua password attuale resta valida."
            )
        else:
            # nessuna email registrata (es. account creato a mano dall'admin
            # prima di questa funzione): resta il percorso di riserva, visibile
            # nel pannello master
            rid = uuid.uuid4().hex[:12]
            db.execute(
                "INSERT INTO password_reset_requests (id, company_id, company_name, requested_at, resolved) "
                "VALUES (?,?,?,?,0)",
                (rid, row["id"], row["name"], now_iso())
            )
            db.commit()
    return jsonify({"ok": True})


@app.route("/api/company/reset-password", methods=["POST"])
def company_reset_password_with_token():
    body = request.get_json(force=True, silent=True) or {}
    token = (body.get("token") or "").strip()
    new_password = (body.get("newPassword") or "").strip()
    if not token:
        return json_error("Link non valido.")
    if len(new_password) < 4:
        return json_error("La nuova password deve avere almeno 4 caratteri.")
    db = get_db()
    row = db.execute(
        "SELECT company_id, expires_at, used FROM password_reset_tokens WHERE token=?", (token,)
    ).fetchone()
    if row is None:
        return json_error("Link non valido o gia' utilizzato.", 400)
    if row["used"]:
        return json_error("Questo link e' gia' stato utilizzato. Richiedine uno nuovo se ti serve.", 400)
    try:
        exp = datetime.datetime.fromisoformat(row["expires_at"])
        if datetime.datetime.utcnow() > exp:
            return json_error("Questo link e' scaduto (valido 1 ora). Richiedine uno nuovo.", 400)
    except ValueError:
        return json_error("Link non valido.", 400)
    db.execute(
        "UPDATE companies SET password_hash=? WHERE id=?",
        (generate_password_hash(new_password), row["company_id"])
    )
    db.execute("UPDATE password_reset_tokens SET used=1 WHERE token=?", (token,))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- static
@app.route("/", methods=["GET"])
def serve_index():
    return send_from_directory(STATIC_DIR, "app.html")


@app.route("/admin", methods=["GET"])
def serve_admin():
    return send_from_directory(STATIC_DIR, "admin.html")


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
