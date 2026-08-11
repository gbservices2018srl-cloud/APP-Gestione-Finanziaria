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
DB_PATH = os.path.join(BASE_DIR, "data.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = Flask(__name__, static_folder=STATIC_DIR)
app.secret_key = os.environ.get("SECRET_KEY", "cambia-questa-chiave-in-produzione-" + uuid.uuid4().hex)
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
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
            data TEXT NOT NULL DEFAULT '{}'
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
    rows = db.execute("SELECT id, name, created_at FROM companies ORDER BY created_at DESC").fetchall()
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
        "INSERT INTO companies (id, name, password_hash, created_at, data) VALUES (?,?,?,?,?)",
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
    row = db.execute("SELECT id, password_hash, name FROM companies WHERE name=?", (name,)).fetchone()
    if row is None or not check_password_hash(row["password_hash"], password):
        return json_error("Nome azienda o password errati.", 401)
    session.clear()
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
    db = get_db()
    row = db.execute("SELECT name FROM companies WHERE id=?", (session["company_id"],)).fetchone()
    if row is None:
        session.clear()
        return jsonify({"loggedIn": False})
    return jsonify({"loggedIn": True, "name": row["name"]})


@app.route("/api/company/data", methods=["GET"])
def company_get_data():
    if not require_company():
        return json_error("Non autorizzato.", 401)
    db = get_db()
    row = db.execute("SELECT data FROM companies WHERE id=?", (session["company_id"],)).fetchone()
    if row is None:
        return json_error("Azienda non trovata.", 404)
    return jsonify({"data": json.loads(row["data"])})


@app.route("/api/company/data", methods=["PUT"])
def company_save_data():
    if not require_company():
        return json_error("Non autorizzato.", 401)
    body = request.get_json(force=True, silent=True)
    if body is None or "data" not in body:
        return json_error("Corpo della richiesta non valido: manca 'data'.")
    db = get_db()
    db.execute(
        "UPDATE companies SET data=? WHERE id=?",
        (json.dumps(body["data"]), session["company_id"])
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
