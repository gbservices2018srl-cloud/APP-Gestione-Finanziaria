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
import datetime
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
            plan TEXT NOT NULL DEFAULT 'free'
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
    db.execute(
        """CREATE TABLE IF NOT EXISTS password_reset_requests (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            company_name TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            resolved INTEGER NOT NULL DEFAULT 0
        )"""
    )
    db.commit()
    db.close()


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
        "SELECT id, name, created_at, suspended, expires_at, plan FROM companies ORDER BY created_at DESC"
    ).fetchall()
    return jsonify({"companies": [dict(r) for r in rows]})


@app.route("/api/admin/companies", methods=["POST"])
def admin_create_company():
    if not require_admin():
        return json_error("Non autorizzato.", 401)
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    password = (body.get("password") or "").strip()
    if not name:
        return json_error("Il nome dell'azienda e' obbligatorio.")
    if len(password) < 4:
        return json_error("La password deve avere almeno 4 caratteri.")
    db = get_db()
    cid = uuid.uuid4().hex[:12]
    db.execute(
        "INSERT INTO companies (id, name, password_hash, created_at, data, suspended, expires_at, plan) "
        "VALUES (?,?,?,?,?,0,NULL,'free')",
        (cid, name, generate_password_hash(password), now_iso(), "{}")
    )
    db.commit()
    return jsonify({"id": cid, "name": name})


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
        "SELECT id, password_hash, name, suspended, expires_at FROM companies WHERE name=?",
        (name,)
    ).fetchone()
    if row is None or not check_password_hash(row["password_hash"], password):
        return json_error("Nome azienda o password errati.", 401)
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
    row = db.execute("SELECT id, name FROM companies WHERE name=?", (name,)).fetchone()
    # rispondiamo sempre ok, azienda trovata o no: cosi' chi prova nomi a
    # caso non scopre quali aziende esistono davvero
    if row is not None:
        rid = uuid.uuid4().hex[:12]
        db.execute(
            "INSERT INTO password_reset_requests (id, company_id, company_name, requested_at, resolved) "
            "VALUES (?,?,?,?,0)",
            (rid, row["id"], row["name"], now_iso())
        )
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
