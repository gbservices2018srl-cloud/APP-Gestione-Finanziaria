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
import hmac
import os
import re
import json
import sqlite3
import uuid
import secrets
import smtplib
import datetime
import html as html_module
import urllib.parse
from email.mime.text import MIMEText
from flask import Flask, request, jsonify, session, g, send_from_directory, redirect

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
            password_hash TEXT NOT NULL,
            privacy_policy_text TEXT,
            terms_of_service_text TEXT
        )"""
    )
    admin_cols = [r[1] for r in db.execute("PRAGMA table_info(admin_config)").fetchall()]
    if "privacy_policy_text" not in admin_cols:
        db.execute("ALTER TABLE admin_config ADD COLUMN privacy_policy_text TEXT")
    if "terms_of_service_text" not in admin_cols:
        db.execute("ALTER TABLE admin_config ADD COLUMN terms_of_service_text TEXT")
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
            approved INTEGER NOT NULL DEFAULT 1,
            phone TEXT,
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            privacy_accepted_at TEXT,
            billing_ragione_sociale TEXT,
            billing_piva TEXT,
            billing_address TEXT
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
    if "phone" not in existing_cols:
        db.execute("ALTER TABLE companies ADD COLUMN phone TEXT")
    if "stripe_customer_id" not in existing_cols:
        db.execute("ALTER TABLE companies ADD COLUMN stripe_customer_id TEXT")
    if "stripe_subscription_id" not in existing_cols:
        db.execute("ALTER TABLE companies ADD COLUMN stripe_subscription_id TEXT")
    if "privacy_accepted_at" not in existing_cols:
        db.execute("ALTER TABLE companies ADD COLUMN privacy_accepted_at TEXT")
    if "billing_ragione_sociale" not in existing_cols:
        db.execute("ALTER TABLE companies ADD COLUMN billing_ragione_sociale TEXT")
    if "billing_piva" not in existing_cols:
        db.execute("ALTER TABLE companies ADD COLUMN billing_piva TEXT")
    if "billing_address" not in existing_cols:
        db.execute("ALTER TABLE companies ADD COLUMN billing_address TEXT")
    # numero di versione dei dati: se due finestre salvano, la seconda viene avvisata invece di sovrascrivere
    if "rev" not in existing_cols:
        db.execute("ALTER TABLE companies ADD COLUMN rev INTEGER NOT NULL DEFAULT 0")
    # storico delle versioni (compresso): si torna indietro per una sola azienda, senza toccare le altre
    db.execute(
        """CREATE TABLE IF NOT EXISTS company_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL,
            saved_at TEXT NOT NULL,
            motivo TEXT NOT NULL DEFAULT '',
            data BLOB NOT NULL
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS company_versions_idx ON company_versions (company_id, saved_at)")
    db.execute(
        """CREATE TABLE IF NOT EXISTS password_reset_requests (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            company_name TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            resolved INTEGER NOT NULL DEFAULT 0
        )"""
    )
    # accesso unico del gruppo (appgestione.it): quali aziende vede ogni persona
    db.execute(
        """CREATE TABLE IF NOT EXISTS sso_company_links (
            sso_user_id TEXT NOT NULL,
            company_id TEXT NOT NULL,
            PRIMARY KEY (sso_user_id, company_id)
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
def require_company():
    if not (session.get("role") == "company" and session.get("company_id")):
        return False
    # Chi e' entrato con l'accesso unico viene ricontrollato ogni minuto:
    # se l'amministratore lo disattiva o gli toglie l'azienda, esce subito.
    if session.get("sso_uid"):
        import time
        if time.time() - float(session.get("sso_checked") or 0) > 60:
            u = sso_current_user()
            if not u or u.get("id") != session.get("sso_uid") or session.get("company_id") not in sso_company_ids(u["id"]):
                session.clear()
                return False
            session["sso_checked"] = time.time()
    return True


# ---------------------------------------------------------------- accesso unico (appgestione.it)
# Le persone del gruppo entrano con la loro email e password di appgestione.it.
# Il cookie "ag_sso" vale per tutti i sottodomini: qui lo facciamo verificare
# al servizio dell'accesso unico, con la chiave condivisa SSO_API_KEY.
# I clienti esterni (registrazione + Stripe) continuano a entrare come prima.
SSO_URL = os.environ.get("SSO_URL", "https://appgestione.it").rstrip("/")
SSO_COOKIE = "ag_sso"


def sso_enabled():
    return bool(os.environ.get("SSO_API_KEY"))


def sso_call(path, payload):
    import urllib.request as _rq
    import urllib.error as _er
    req = _rq.Request(SSO_URL + path, data=json.dumps(payload).encode("utf-8"), method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + os.environ.get("SSO_API_KEY", ""),
    })
    try:
        with _rq.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except _er.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        print("Accesso unico non raggiungibile:", e)
        return 0, {}


def sso_check():
    """(stato, dati): 200 = puo' entrare in Finanza, 403 = collegato ma senza Finanza, altro = non collegato."""
    token = request.cookies.get(SSO_COOKIE)
    if not token or not sso_enabled():
        return 401, {}
    return sso_call("/api/sso/verify", {"token": token, "app": "finanza"})


def sso_current_user():
    code, data = sso_check()
    return data.get("user") if code == 200 else None


def sso_company_ids(uid):
    rows = get_db().execute("SELECT company_id FROM sso_company_links WHERE sso_user_id=?", (uid,)).fetchall()
    return [r["company_id"] for r in rows]


DEFAULT_PRIVACY_TEXT = """Bozza — da far rivedere da un consulente privacy o da un avvocato prima della pubblicazione definitiva.

Informativa ai sensi degli articoli 13 e 14 del Regolamento (UE) 2016/679 (GDPR).

1. Titolare del trattamento
Consulting Sardegna Srl — [DA COMPLETARE: sede legale, P.IVA, email di contatto per la privacy].

2. Dati raccolti
Dati di registrazione: nome dell'azienda, indirizzo email, numero di telefono (facoltativo), password (conservata sempre in forma cifrata, mai leggibile).
Dati di pagamento: gestiti direttamente dal nostro fornitore Stripe; non conserviamo i dati della carta di credito sui nostri sistemi.
Dati gestionali inseriti nell'app: le informazioni finanziarie della tua attività (fatturato, costi, budget) che inserisci volontariamente per usare il servizio.
Dati tecnici: indirizzo IP, log di accesso, cookie di sessione strettamente necessari al funzionamento (nessun cookie di profilazione o pubblicitario).

3. Finalità e base giuridica
Erogazione del servizio e gestione dell'account: esecuzione del contratto.
Fatturazione e adempimenti fiscali: obbligo di legge.
Comunicazioni relative al servizio (conferme, avvisi, assistenza): esecuzione del contratto.
Eventuali comunicazioni promozionali: solo con consenso specifico, facoltativo e revocabile in ogni momento.

4. Destinatari dei dati (responsabili del trattamento)
I dati possono essere trattati da fornitori terzi che agiscono come responsabili del trattamento per nostro conto: Stripe Inc. (pagamenti), Render Services Inc. (hosting), Google LLC (invio email). [DA VERIFICARE: che siano in essere i relativi accordi di trattamento dati/DPA con ciascun fornitore].

5. Trasferimento dati extra-UE
Alcuni fornitori indicati potrebbero trattare dati al di fuori dello Spazio Economico Europeo. In tal caso il trasferimento avviene sulla base di clausole contrattuali standard approvate dalla Commissione Europea o di altre garanzie equivalenti.

6. Periodo di conservazione
I dati sono conservati per la durata del rapporto contrattuale e, successivamente, per il tempo previsto dagli obblighi di legge (i dati fiscali/contabili vanno tipicamente conservati per 10 anni).

7. Diritti dell'interessato
In ogni momento puoi richiedere: accesso ai tuoi dati, rettifica, cancellazione, limitazione del trattamento, portabilità, opposizione. Scrivi a [DA COMPLETARE: email]. Hai inoltre diritto di reclamo al Garante per la Protezione dei Dati Personali (garanteprivacy.it).

8. Modifiche
Questa informativa può essere aggiornata nel tempo. La versione più recente è sempre disponibile in questa pagina."""

DEFAULT_TERMS_TEXT = """Bozza — da far rivedere da un consulente privacy o da un avvocato prima della pubblicazione definitiva.

1. Oggetto
I presenti Termini regolano l'utilizzo del servizio fornito da Consulting Sardegna Srl [DA COMPLETARE: P.IVA, sede legale].

2. Piani e prezzi
Il servizio è offerto in modalità Gratuita (funzioni limitate) e Completa a pagamento (29 EUR/mese, [DA COMPLETARE: IVA inclusa o esclusa]), rinnovata automaticamente ogni mese fino a disdetta.

3. Registrazione e account
Per usare il servizio è necessario registrarsi fornendo dati veritieri. Sei responsabile della riservatezza delle tue credenziali di accesso.

4. Pagamento e fatturazione
I pagamenti sono elaborati tramite Stripe. L'abbonamento si rinnova automaticamente ogni mese fino a disdetta.

5. Disdetta
Puoi disdire l'abbonamento in qualsiasi momento dalla sezione "Impostazioni e Backup". Il piano completo resta attivo fino al termine del periodo già pagato; non sono previsti rimborsi per periodi parziali, salvo diversa indicazione di legge.

6. Approvazione dell'account
Ogni nuova registrazione è soggetta ad approvazione da parte del Titolare del servizio.

7. Limitazione di responsabilità
Il servizio viene fornito "così com'è". Pur impegnandoci a garantirne la continuità e l'accuratezza, non garantiamo l'assenza di interruzioni o errori e non siamo responsabili per decisioni economiche prese sulla base dei dati elaborati dall'app, che restano sotto la responsabilità dell'utente.

8. Sospensione e cessazione dell'account
Ci riserviamo il diritto di sospendere o cessare un account in caso di violazione dei presenti Termini, mancato pagamento, o uso improprio del servizio.

9. Legge applicabile e foro competente
I presenti Termini sono regolati dalla legge italiana. Per ogni controversia è competente il foro di [DA COMPLETARE].

10. Modifiche
Ci riserviamo il diritto di modificare i presenti Termini, dandone comunicazione agli utenti."""


def text_to_safe_html(text):
    paragraphs = [p.strip() for p in (text or "").split("\n\n") if p.strip()]
    parts = []
    for p in paragraphs:
        escaped = html_module.escape(p).replace("\n", "<br>")
        parts.append("<p>" + escaped + "</p>")
    return "".join(parts)


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
@app.route("/api/legal-content", methods=["GET"])
def get_legal_content():
    db = get_db()
    row = db.execute(
        "SELECT privacy_policy_text, terms_of_service_text FROM admin_config WHERE id=1"
    ).fetchone()
    privacy_text = (row["privacy_policy_text"] if row and row["privacy_policy_text"] else DEFAULT_PRIVACY_TEXT)
    terms_text = (row["terms_of_service_text"] if row and row["terms_of_service_text"] else DEFAULT_TERMS_TEXT)
    return jsonify({
        "privacyHtml": text_to_safe_html(privacy_text),
        "termsHtml": text_to_safe_html(terms_text)
    })


# ---------------------------------------------------------------- storico delle versioni
# A ogni salvataggio si tiene la versione precedente (al massimo una ogni 15 minuti per azienda).
# Si conservano: tutte quelle degli ultimi 2 giorni, poi l'ultima di ogni giorno fino a 30 giorni.
VERSIONE_OGNI_MIN = 15


def _comprimi(testo):
    import zlib
    return zlib.compress(testo.encode("utf-8"), 6)


def _decomprimi(blob):
    import zlib
    return zlib.decompress(blob).decode("utf-8")


def salva_versione(db, company_id, data_text, motivo="", forza=False):
    if not forza:
        ultima = db.execute("SELECT saved_at FROM company_versions WHERE company_id=? ORDER BY id DESC LIMIT 1", (company_id,)).fetchone()
        if ultima:
            try:
                t = datetime.datetime.fromisoformat(ultima["saved_at"].rstrip("Z"))
                if datetime.datetime.utcnow() - t < datetime.timedelta(minutes=VERSIONE_OGNI_MIN):
                    return
            except Exception:
                pass
    db.execute("INSERT INTO company_versions (company_id, saved_at, motivo, data) VALUES (?,?,?,?)",
               (company_id, now_iso(), motivo, _comprimi(data_text or "{}")))
    # Le copie vecchie NON si cancellano più (24/11/2026): servono per ritrovare i dati. Si toglieranno solo con l'OK di Giancarlo.


# copia di sicurezza di tutte le aziende in un file (prima di operazioni importanti)
# ================================================================ SUITE (appgestione.it/gestione)
# Le aziende si gestiscono dal pannello della suite: qui solo le chiamate tra i due servizi,
# protette dalla chiave condivisa SSO_API_KEY. Nessuna di queste cancella dati.
def _suite_ok():
    k = os.environ.get("SSO_API_KEY", "")
    h = request.headers.get("Authorization", "")
    return bool(k) and hmac.compare_digest(h.encode(), ("Bearer " + k).encode())


def _azienda_dict(r, gruppo):
    d = dict(r)
    d["suspended"] = bool(d.get("suspended"))
    d["approved"] = bool(d.get("approved"))
    d["gruppo"] = d["id"] in gruppo
    return d


@app.route("/api/_suite/aziende", methods=["GET"])
def suite_aziende():
    if not _suite_ok():
        return json_error("Non autorizzato.", 401)
    db = get_db()
    gruppo = {r["company_id"] for r in db.execute("SELECT DISTINCT company_id FROM sso_company_links").fetchall()}
    rows = db.execute("SELECT id, name, email, phone, created_at, suspended, expires_at, plan, approved FROM companies ORDER BY name").fetchall()
    return jsonify({"aziende": [_azienda_dict(r, gruppo) for r in rows]})


@app.route("/api/_suite/aziende", methods=["POST"])
def suite_crea_azienda():
    if not _suite_ok():
        return json_error("Non autorizzato.", 401)
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()[:120]
    if len(name) < 2:
        return json_error("Il nome dell'azienda e' obbligatorio.")
    db = get_db()
    if db.execute("SELECT 1 FROM companies WHERE LOWER(TRIM(name))=LOWER(?)", (name,)).fetchone():
        return json_error("Esiste gia' un'azienda con questo nome in Gestione finanziaria.", 409)
    password = secrets.token_urlsafe(9)
    cid = uuid.uuid4().hex[:12]
    db.execute(
        "INSERT INTO companies (id, name, email, phone, password_hash, created_at, data, suspended, expires_at, plan, approved) "
        "VALUES (?,?,?,?,?,?,?,0,?,'pro',1)",
        (cid, name, (body.get("email") or "").strip() or None, (body.get("phone") or "").strip() or None,
         generate_password_hash(password), now_iso(), "{}", body.get("expires_at") or None))
    db.commit()
    return jsonify({"id": cid, "name": name, "password": password})


@app.route("/api/_suite/aziende/<cid>", methods=["PATCH"])
def suite_aggiorna_azienda(cid):
    if not _suite_ok():
        return json_error("Non autorizzato.", 401)
    body = request.get_json(force=True, silent=True) or {}
    db = get_db()
    if db.execute("SELECT 1 FROM companies WHERE id=?", (cid,)).fetchone() is None:
        return json_error("Azienda non trovata.", 404)
    out = {"ok": True}
    if "name" in body:
        n = (body.get("name") or "").strip()[:120]
        if len(n) < 2:
            return json_error("Il nome non puo' essere vuoto.")
        db.execute("UPDATE companies SET name=? WHERE id=?", (n, cid))
    if "email" in body:
        e = (body.get("email") or "").strip() or None
        if e and "@" not in e:
            return json_error("Indirizzo email non valido.")
        db.execute("UPDATE companies SET email=? WHERE id=?", (e, cid))
    if "phone" in body:
        db.execute("UPDATE companies SET phone=? WHERE id=?", ((body.get("phone") or "").strip() or None, cid))
    if "suspended" in body:
        db.execute("UPDATE companies SET suspended=? WHERE id=?", (1 if body.get("suspended") else 0, cid))
    if "expires_at" in body:
        db.execute("UPDATE companies SET expires_at=? WHERE id=?", (body.get("expires_at") or None, cid))
    if body.get("plan") in ("free", "pro"):
        db.execute("UPDATE companies SET plan=? WHERE id=?", (body["plan"], cid))
    if body.get("approva"):
        db.execute("UPDATE companies SET approved=1 WHERE id=?", (cid,))
    if body.get("nuova_password"):
        pw = str(body.get("password") or "").strip() or secrets.token_urlsafe(9)
        if len(pw) < 6:
            return json_error("La password deve avere almeno 6 caratteri.")
        db.execute("UPDATE companies SET password_hash=? WHERE id=?", (generate_password_hash(pw), cid))
        db.execute("UPDATE password_reset_requests SET resolved=1 WHERE company_id=?", (cid,))
        out["password"] = pw
    db.commit()
    return jsonify(out)


@app.route("/api/_suite/aziende/<cid>/versioni", methods=["GET"])
def suite_versioni(cid):
    if not _suite_ok():
        return json_error("Non autorizzato.", 401)
    rows = get_db().execute("SELECT id, saved_at, motivo, length(data) AS dim FROM company_versions WHERE company_id=? ORDER BY id DESC LIMIT 300", (cid,)).fetchall()
    return jsonify({"versioni": [dict(r) for r in rows]})


@app.route("/api/_suite/aziende/<cid>/versioni/<int:vid>", methods=["GET"])
def suite_versione_scarica(cid, vid):
    if not _suite_ok():
        return json_error("Non autorizzato.", 401)
    row = get_db().execute("SELECT saved_at, data FROM company_versions WHERE id=? AND company_id=?", (vid, cid)).fetchone()
    if row is None:
        return json_error("Versione non trovata.", 404)
    from flask import Response
    return Response(_decomprimi(row["data"]), mimetype="application/json",
                    headers={"Content-Disposition": 'attachment; filename="versione_%s_%s.json"' % (cid[:8], row["saved_at"][:16].replace(":", "-"))})


@app.route("/api/_suite/aziende/<cid>/versioni/<int:vid>/ripristina", methods=["POST"])
def suite_versione_ripristina(cid, vid):
    if not _suite_ok():
        return json_error("Non autorizzato.", 401)
    db = get_db()
    v = db.execute("SELECT data FROM company_versions WHERE id=? AND company_id=?", (vid, cid)).fetchone()
    cur = db.execute("SELECT data FROM companies WHERE id=?", (cid,)).fetchone()
    if v is None or cur is None:
        return json_error("Versione non trovata.", 404)
    testo = _decomprimi(v["data"])
    json.loads(testo)
    # i dati di adesso restano nello storico: il ripristino si puo' sempre annullare
    salva_versione(db, cid, cur["data"], "prima del ripristino", forza=True)
    db.execute("UPDATE companies SET data=?, rev=rev+1 WHERE id=?", (testo, cid))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/_suite/richieste-password", methods=["GET"])
def suite_richieste_password():
    if not _suite_ok():
        return json_error("Non autorizzato.", 401)
    rows = get_db().execute("SELECT id, company_id, company_name, requested_at FROM password_reset_requests WHERE resolved=0 ORDER BY requested_at").fetchall()
    return jsonify({"richieste": [dict(r) for r in rows]})


@app.route("/api/_suite/richieste-password/<rid>", methods=["POST"])
def suite_richiesta_password_gestita(rid):
    if not _suite_ok():
        return json_error("Non autorizzato.", 401)
    db = get_db()
    db.execute("UPDATE password_reset_requests SET resolved=1 WHERE id=?", (rid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/_suite/persone", methods=["GET"])
def suite_persone():
    """Persone del gruppo abilitate a Finanza (o all'App di bilancio), con le aziende che vedono."""
    if not _suite_ok():
        return json_error("Non autorizzato.", 401)
    users = []
    for app_id in ("finanza", "bilancio"):
        code, data = sso_call("/api/sso/users", {"app": app_id})
        if code != 200:
            return json_error("Accesso unico non raggiungibile, riprova tra poco.", 502)
        visti = {u["id"] for u in users}
        users += [u for u in data.get("users", []) if u["id"] not in visti]
    links = {}
    for r in get_db().execute("SELECT sso_user_id, company_id FROM sso_company_links").fetchall():
        links.setdefault(r["sso_user_id"], []).append(r["company_id"])
    return jsonify({"persone": [{"id": u["id"], "nome": (u.get("firstName", "") + " " + u.get("lastName", "")).strip(), "email": u.get("email"),
                                 "aziende": links.get(u["id"], [])} for u in users]})


@app.route("/api/_suite/persone/<uid>", methods=["PUT"])
def suite_persona_aziende(uid):
    if not _suite_ok():
        return json_error("Non autorizzato.", 401)
    body = request.get_json(force=True, silent=True) or {}
    ids = [str(x) for x in (body.get("aziende") or [])][:200]
    db = get_db()
    valid = {r["id"] for r in db.execute("SELECT id FROM companies").fetchall()}
    db.execute("DELETE FROM sso_company_links WHERE sso_user_id=?", (uid[:64],))
    for cid in ids:
        if cid in valid:
            db.execute("INSERT OR IGNORE INTO sso_company_links (sso_user_id, company_id) VALUES (?,?)", (uid[:64], cid))
    db.commit()
    return jsonify({"ok": True, "aziende": [c for c in ids if c in valid]})


# =============================================================== COMPANY
@app.route("/_suite/entra", methods=["GET"])
def suite_entra():
    """Un cliente della suite arriva dalla sua pagina «Le mie app» con un codice monouso: entra nella sua azienda senza seconda password."""
    codice = (request.args.get("c") or "").strip()
    if not codice or not sso_enabled():
        return redirect("/")
    code, data = sso_call("/api/_suite/finanza-ticket", {"codice": codice})
    if code != 200 or not data.get("company_id"):
        return redirect("/")
    row = get_db().execute("SELECT id, approved FROM companies WHERE id=?", (data["company_id"],)).fetchone()
    if row is None or not row["approved"]:
        return redirect("/")
    session.clear()
    session.permanent = True
    session["role"] = "company"
    session["company_id"] = row["id"]
    session["suite_home"] = data.get("home") or SSO_URL
    session["suite_utente"] = (data.get("utente") or {}).get("email") or ""
    ok, _err = check_company_access()
    if not ok:
        return redirect("/")
    return redirect("/bilancio" if data.get("pagina") == "bilancio" else "/")


@app.route("/api/sso/status", methods=["GET"])
def sso_status():
    """Per la pagina di accesso: la persona e' gia' collegata ad appgestione.it? Quali aziende puo' aprire?"""
    out = {"enabled": sso_enabled(), "home": SSO_URL,
           "loginUrl": SSO_URL + "/accedi?next=" + urllib.parse.quote(request.host_url, safe="")}
    if not sso_enabled():
        return jsonify(out)
    code, data = sso_check()
    if code == 403:
        out.update({"user": data.get("user"), "denied": True})
    elif code == 200:
        u = data["user"]
        ids = sso_company_ids(u["id"])
        rows = get_db().execute(
            "SELECT id, name FROM companies WHERE approved=1 AND suspended=0 AND id IN (%s) ORDER BY name" % ",".join("?" * len(ids)),
            ids).fetchall() if ids else []
        out.update({"user": u, "companies": [dict(r) for r in rows]})
    return jsonify(out)


@app.route("/api/sso/enter", methods=["POST"])
def sso_enter():
    import time
    body = request.get_json(force=True, silent=True) or {}
    code, data = sso_check()
    if code != 200:
        return json_error("Accedi prima da appgestione.it." if code != 403 else "Non sei abilitato alla Gestione finanziaria.", 401)
    u = data["user"]
    ids = sso_company_ids(u["id"])
    cid = body.get("company_id") or (ids[0] if len(ids) == 1 else None)
    if not cid or cid not in ids:
        return json_error("Scegli un'azienda tra quelle assegnate.", 400)
    row = get_db().execute("SELECT id, name, approved FROM companies WHERE id=?", (cid,)).fetchone()
    if row is None or not row["approved"]:
        return json_error("Azienda non disponibile.", 404)
    session.clear()
    session.permanent = True
    session["role"] = "company"
    session["company_id"] = row["id"]
    ok, err = check_company_access()
    if not ok:
        return err
    session["sso_uid"] = u["id"]
    session["sso_checked"] = time.time()
    return jsonify({"ok": True, "name": row["name"]})


@app.route("/api/company/login", methods=["POST"])
def company_login():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    password = body.get("password") or ""
    db = get_db()
    # Accesso tollerante: il campo puo' contenere il nome azienda (senza distinguere
    # maiuscole/minuscole) oppure l'email dell'account; la password viene provata sia
    # cosi' com'e' sia senza spazi ai bordi (registrazione e reset admin la salvano "strippata").
    candidates = db.execute(
        "SELECT id, password_hash, name, suspended, expires_at, approved FROM companies "
        "WHERE name=? OR LOWER(TRIM(name))=LOWER(?) OR LOWER(TRIM(email))=LOWER(?) "
        "ORDER BY (name=?) DESC",
        (name, name, name, name)
    ).fetchall() if name else []
    pw_variants = [password]
    if password.strip() != password:
        pw_variants.append(password.strip())
    row = None
    for cand in candidates:
        if any(check_password_hash(cand["password_hash"], pw) for pw in pw_variants):
            row = cand
            break
    if row is None:
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


@app.route("/api/company/support-request", methods=["POST"])
def support_request():
    if not require_company():
        return json_error("Non autorizzato.", 401)
    ok, err = check_company_access()
    if not ok:
        return err
    body = request.get_json(force=True, silent=True) or {}
    nome = (body.get("nome") or "").strip()
    cognome = (body.get("cognome") or "").strip()
    note = (body.get("note") or "").strip()
    if not nome or not cognome:
        return json_error("Inserisci nome e cognome.")
    if not note:
        return json_error("Scrivi una descrizione della richiesta.")
    db = get_db()
    row = db.execute(
        "SELECT name, email FROM companies WHERE id=?", (session["company_id"],)
    ).fetchone()
    company_name = row["name"] if row else "azienda sconosciuta"
    company_email = row["email"] if row and row["email"] else "nessuna email registrata"
    sent = send_email_safe(
        get_admin_notify_email(),
        "Richiesta di assistenza - " + company_name,
        "Nuova richiesta di assistenza dall'app:\n\n"
        "Azienda: " + company_name + "\n"
        "Email azienda: " + company_email + "\n"
        "Richiesta di: " + nome + " " + cognome + "\n\n"
        "Note:\n" + note
    )
    if not sent:
        return json_error("Impossibile inviare la richiesta al momento. Riprova tra poco o contatta direttamente l'assistenza.", 500)
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
    row = db.execute("SELECT name, plan, expires_at FROM companies WHERE id=?", (session["company_id"],)).fetchone()
    if row is None:
        session.clear()
        return jsonify({"loggedIn": False})
    return jsonify({"loggedIn": True, "name": row["name"], "plan": row["plan"] or "free", "scadenza": (row["expires_at"] or "")[:10] or None,
                    "sso": bool(session.get("sso_uid") or session.get("suite_home")),
                    "ssoHome": session.get("suite_home") or (SSO_URL if session.get("sso_uid") else None)})


@app.route("/api/company/data", methods=["GET"])
def company_get_data():
    if not require_company():
        return json_error("Non autorizzato.", 401)
    ok, err = check_company_access()
    if not ok:
        return err
    db = get_db()
    row = db.execute("SELECT data, plan, rev FROM companies WHERE id=?", (session["company_id"],)).fetchone()
    if row is None:
        return json_error("Azienda non trovata.", 404)
    plan = row["plan"] or "free"
    data = strip_gated_data(json.loads(row["data"]), plan)
    return jsonify({"data": data, "plan": plan, "rev": row["rev"] or 0})


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
    row = db.execute("SELECT data, plan, rev FROM companies WHERE id=?", (session["company_id"],)).fetchone()
    if row is None:
        return json_error("Azienda non trovata.", 404)
    rev = row["rev"] or 0
    # dati cambiati da un'altra finestra o da un altro dispositivo dopo che questa li ha aperti: non si sovrascrive
    if body.get("rev") is not None and int(body.get("rev")) != rev:
        return jsonify({"error": "I dati sono stati modificati da un'altra finestra o da un altro dispositivo.", "conflict": True, "rev": rev}), 409
    plan = row["plan"] or "free"
    incoming = merge_gated_data(body["data"], row["data"], plan)
    # registro di AlfaDocs (quali caselle ha scritto e quali sono bloccate): lo gestisce solo il server
    try:
        esistente = json.loads(row["data"] or "{}").get("alfadocs")
        if isinstance(incoming, dict):
            if esistente is not None:
                incoming["alfadocs"] = esistente
            else:
                incoming.pop("alfadocs", None)
    except Exception:
        pass
    salva_versione(db, session["company_id"], row["data"])
    db.execute(
        "UPDATE companies SET data=?, rev=? WHERE id=?",
        (json.dumps(incoming), rev + 1, session["company_id"])
    )
    db.commit()
    return jsonify({"ok": True, "rev": rev + 1})


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
    email = (body.get("email") or "").strip()
    if not name:
        return json_error("Inserisci il nome della tua azienda.")
    if not email:
        return json_error("Inserisci l'email con cui ti sei registrato.")
    db = get_db()
    row = db.execute("SELECT id, name, email FROM companies WHERE name=?", (name,)).fetchone()
    # rispondiamo sempre ok, azienda trovata o no, email corrispondente o no:
    # cosi' chi prova nomi/email a caso non scopre quali aziende esistono
    # davvero, ne' quale email e' associata a un nome azienda
    email_matches = (
        row is not None and row["email"] is not None
        and row["email"].strip().lower() == email.lower()
    )
    if row is not None and email_matches:
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
    elif row is not None and row["email"] is None:
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
# L'app vive sulla radice del suo sottodominio (finanza.appgestione.it), non sotto un percorso.
@app.route("/", methods=["GET"])
def serve_index():
    r = send_from_directory(STATIC_DIR, "app.html")
    r.headers["Cache-Control"] = "no-cache"  # sempre la versione aggiornata dell'app
    return r


# Solo compatibilita': il percorso /finanziario (usato per un paio di giorni) rimanda alla radice
# conservando i parametri (?reset=..., ?checkout=...), cosi' i link gia' inviati continuano a funzionare.
@app.route("/finanziario", methods=["GET"])
@app.route("/finanziario/", methods=["GET"])
def legacy_finanziario():
    qs = request.query_string.decode("utf-8", "ignore")
    return redirect("/" + ("?" + qs if qs else ""), code=302)


@app.route("/admin", methods=["GET"])
def serve_admin():
    # il vecchio pannello non c'e' piu': le aziende si gestiscono da appgestione.it/gestione
    return redirect(SSO_URL + "/gestione")


# ---------------------------------------------------------------- altre app sullo stesso dominio
# /ticketassistenza -> app "Assistenza Studi" (servizio Render separato).
# Qui si fa solo da ponte: la richiesta viene girata all'altra app e la risposta
# restituita cosi' com'e'. Cookie e sessione di questa app NON vengono inoltrati.
import urllib.request
import urllib.error
import urllib.parse
from flask import Response

TICKET_UPSTREAM = os.environ.get("TICKET_UPSTREAM", "https://app-ticket-assistenza.onrender.com").rstrip("/")
_PROXY_TIMEOUT = 25
_PROXY_REQ_HEADERS = ("Accept", "Accept-Encoding", "Accept-Language", "Content-Type",
                      "User-Agent", "Cache-Control", "If-None-Match", "If-Modified-Since", "Range")
_PROXY_SKIP_RESP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "set-cookie"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # i redirect vanno restituiti al browser, non seguiti qui


_proxy_opener = urllib.request.build_opener(_NoRedirect)


@app.route("/ticketassistenza", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@app.route("/ticketassistenza/", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@app.route("/ticketassistenza/<path:sub>", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def proxy_ticketassistenza(sub=""):
    path = urllib.parse.quote(request.path, safe="/:@!$&'()*+,;=-._~%")
    url = TICKET_UPSTREAM + path
    if request.query_string:
        url += "?" + request.query_string.decode("utf-8", "ignore")

    headers = {h: request.headers[h] for h in _PROXY_REQ_HEADERS if h in request.headers}
    headers["X-Forwarded-Host"] = request.host
    headers["X-Forwarded-Proto"] = "https"
    body = request.get_data() if request.method not in ("GET", "HEAD") else None

    req = urllib.request.Request(url, data=body, headers=headers, method=request.method)
    try:
        resp = _proxy_opener.open(req, timeout=_PROXY_TIMEOUT)
    except urllib.error.HTTPError as e:  # 3xx/4xx/5xx dell'app ticket: passano uguali
        resp = e
    except Exception:
        # tipico: l'app ticket e' in "sospensione" e impiega qualche secondo a svegliarsi
        page = ("<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<meta http-equiv='refresh' content='6'><title>TO Smile</title>"
                "<body style='font-family:sans-serif;text-align:center;padding:60px 20px;color:#33333D'>"
                "<h2>Sto avviando l'app...</h2><p>La pagina si ricarica da sola tra pochi secondi.</p></body>")
        return Response(page, status=503, mimetype="text/html", headers={"Retry-After": "6"})

    data = resp.read()
    out = Response(data, status=resp.status if hasattr(resp, "status") else resp.code)
    for k, v in resp.headers.items():
        if k.lower() in _PROXY_SKIP_RESP:
            continue
        if k.lower() == "location":
            v = v.replace(TICKET_UPSTREAM, request.host_url.rstrip("/"))
        out.headers[k] = v
    return out


# ---------------------------------------------------------------- trasloco del disco (es. da Oregon a Francoforte)
# Vecchio servizio: con TRASLOCO_KEY impostata espone GET /api/_trasloco/export (solo lettura): una copia coerente
# del database (backup SQLite) + gli altri file del disco, in un .tar.gz con l'elenco delle righe di ogni tabella.
# Nuovo servizio: con COPIA_DA (indirizzo dell'export) e TRASLOCO_KEY, all'avvio scarica la copia, controlla impronta,
# integrita' del database e numero di righe tabella per tabella; solo se tutto torna la mette sul disco.
# Il database gia' presente sul nuovo disco non viene mai cancellato: viene rinominato. Senza le variabili non fa nulla.
# MANUTENZIONE=true sul vecchio servizio: nessuno scrive piu' (pagina di attesa), l'export resta disponibile.
import hashlib, tarfile, tempfile, shutil, io as _io

def _trasloco_chiave_ok(req):
    key = os.environ.get("TRASLOCO_KEY", "")
    given = (req.headers.get("Authorization") or "").replace("Bearer ", "", 1)
    return len(key) >= 24 and secrets.compare_digest(given, key)

def _conta_righe(db_path):
    con = sqlite3.connect(db_path)
    try:
        tabelle = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {tb: con.execute('SELECT COUNT(*) FROM "%s"' % tb.replace('"', '""')).fetchone()[0] for tb in tabelle}
    finally:
        con.close()

@app.before_request
def _manutenzione():
    if os.environ.get("MANUTENZIONE") == "true" and request.path not in ("/api/_trasloco/export", "/healthz"):
        return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                "<title>Aggiornamento in corso</title><body style='font-family:system-ui,sans-serif;display:grid;place-items:center;min-height:90vh;text-align:center'>"
                "<div><h1>Stiamo aggiornando la Gestione finanziaria</h1><p>Torniamo tra pochi minuti. I dati sono al sicuro: riprova un po' piu' tardi.</p></div>",
                503, {"Retry-After": "600", "Cache-Control": "no-store"})

# ---------------------------------------------------------------- per le app To Smile (Scheda personale: incentivi)
# Solo lettura, protetto dalla chiave condivisa con l'accesso unico (SSO_API_KEY). Restituisce, per l'azienda scelta,
# il fatturato a consuntivo di ogni sede in un mese e il fatturato mensile di budget (il target).
def _chiave_app_ok():
    k = os.environ.get("SSO_API_KEY", "")
    h = request.headers.get("Authorization", "")
    return bool(k) and hmac.compare_digest(h.encode(), ("Bearer " + k).encode())


@app.route("/api/_appgestione/aziende", methods=["GET"])
def appgestione_aziende():
    if not _chiave_app_ok():
        return json_error("Non autorizzato.", 401)
    rows = get_db().execute("SELECT id, name FROM companies ORDER BY name").fetchall()
    return jsonify({"aziende": [{"id": r["id"], "nome": r["name"]} for r in rows]})


@app.route("/api/_appgestione/consuntivo", methods=["GET"])
def appgestione_consuntivo():
    if not _chiave_app_ok():
        return json_error("Non autorizzato.", 401)
    cid = request.args.get("azienda", "")
    try:
        anno = int(request.args.get("anno", "0"))
        mese = int(request.args.get("mese", "0"))
    except ValueError:
        return json_error("Mese non valido.", 400)
    if not 1 <= mese <= 12:
        return json_error("Mese non valido.", 400)
    row = get_db().execute("SELECT name, data FROM companies WHERE id=?", (cid,)).fetchone()
    if row is None:
        return json_error("Azienda non trovata.", 404)
    d = json.loads(row["data"] or "{}")
    anno_corrente = d.get("currentYear")
    cons = (d.get("consuntivo") or {}).get("revenue") or {}
    bud = (d.get("budget") or {}).get("revenue") or {}
    cat_fin = next((c for c in (d.get("varCats") or []) if re.search(r"finanziament.*pazient", c.get("name") or "", re.I)), None)
    # Gestione ASO: «Materiale di consumo» (% del Budget sul fatturato e spesa a consuntivo, mese per mese)
    cat_mat = next((c for c in (d.get("varCats") or []) if re.search(r"material.*consumo", c.get("name") or "", re.I)), None)
    var_pct = (d.get("budget") or {}).get("varPct") or {}
    sedi = []
    for s in d.get("sites") or []:
        sid = s.get("id")
        mesi = cons.get(sid) or []
        fatt = mesi[mese - 1] if anno == anno_corrente and len(mesi) >= mese else None
        try:
            fatt = float(fatt) if fatt is not None else None
        except (TypeError, ValueError):
            fatt = None
        try:
            target = float(bud.get(sid) or 0)
        except (TypeError, ValueError):
            target = 0
        # CRM Commerciale: euro a consuntivo di «Tassi finanziamento verso pazienti» e fatturato, mese per mese
        fin_mesi = [_cons_var_mese(d, cat_fin, sid, m) for m in range(12)] if (cat_fin and anno == anno_corrente) else None
        fatt_mesi = [_num(mesi[m]) if len(mesi) > m else 0.0 for m in range(12)] if anno == anno_corrente else None
        sedi.append({"id": sid, "nome": s.get("name") or "", "fatturato": fatt, "target": target,
                     "finanziamenti": fin_mesi[mese - 1] if fin_mesi else None,
                     "finanziamentiMesi": fin_mesi, "fatturatoMesi": fatt_mesi,
                     "materialePct": _num((var_pct.get(cat_mat.get("id")) or {}).get(sid)) if cat_mat else None,
                     "materialeMesi": [_cons_var_mese(d, cat_mat, sid, m) for m in range(12)] if (cat_mat and anno == anno_corrente) else None,
                     "produzioneMesi": [_num(((d.get("consuntivo") or {}).get("produzione") or {}).get(sid, [])[m]) if len(((d.get("consuntivo") or {}).get("produzione") or {}).get(sid, [])) > m else 0.0 for m in range(12)] if anno == anno_corrente else None})
    # sede direzionale (impostata nella Gestione finanziaria) e sedi su cui si ripartiscono i suoi costi: solo lettura
    dz = d.get("direzione") or {}
    ids = {s.get("id") for s in d.get("sites") or []}
    direzione = None
    if dz.get("sedeId") in ids:
        rip = [x for x in (dz.get("sedi") or []) if x in ids and x != dz.get("sedeId")]
        if rip:
            direzione = {"sedeId": dz.get("sedeId"), "sedi": rip, "criterio": dz.get("criterio") or "base"}
    return jsonify({"azienda": row["name"], "anno": anno, "mese": mese, "annoNelGestionale": anno_corrente,
                    "voceFinanziamenti": cat_fin.get("name") if cat_fin else None,
                    "voceMateriale": cat_mat.get("name") if cat_mat else None, "sedi": sedi, "direzione": direzione})


# AlfaDocs → Consuntivo: incassato (= fatturato) e produzione di ogni sede, SOLO nel mese indicato.
# Salva prima una copia nello storico versioni. Se in una casella c'era un numero scritto a mano diverso, lo segnala.
# Una casella «rimessa a mano» (bloccata) non viene più sovrascritta in quel mese.
@app.route("/api/_appgestione/alfadocs", methods=["POST"])
def appgestione_alfadocs():
    if not _chiave_app_ok():
        return json_error("Non autorizzato.", 401)
    body = request.get_json(force=True, silent=True) or {}
    cid = str(body.get("azienda") or "")
    try:
        anno = int(body.get("anno") or 0)
        mese = int(body.get("mese") or 0)
    except (TypeError, ValueError):
        return json_error("Dati non validi.", 400)
    if not 1 <= mese <= 12:
        return json_error("Mese non valido.", 400)
    db = get_db()
    row = db.execute("SELECT data, rev FROM companies WHERE id=?", (cid,)).fetchone()
    if row is None:
        return json_error("Azienda non trovata.", 404)
    d = json.loads(row["data"] or "{}")
    if d.get("currentYear") != anno:
        return json_error("Nella Gestione finanziaria il consuntivo aperto è del %s, non del %s." % (d.get("currentYear"), anno), 409)
    ids = {s.get("id") for s in d.get("sites") or []}
    cons = d.setdefault("consuntivo", {})
    reg = d.setdefault("alfadocs", {}).setdefault("%04d-%02d" % (anno, mese), {})
    esiti, differenze, cambiato = [], [], False
    for x in body.get("sedi") or []:
        sid = str(x.get("sedeId") or "")
        if sid not in ids:
            esiti.append({"sedeId": sid, "errore": "sede non trovata"})
            continue
        r = reg.setdefault(sid, {})
        es = {"sedeId": sid}
        for campo in ("revenue", "produzione"):
            v = x.get("incassato" if campo == "revenue" else "produzione")
            if v is None:
                continue
            try:
                v = round(float(v), 2)
            except (TypeError, ValueError):
                continue
            if v < 0 or v > 100000000:
                continue
            if r.get(campo + "_bloccato"):
                es[campo] = {"bloccato": True, "alfadocs": v}
                continue
            arr = list((cons.setdefault(campo, {})).get(sid) or [])
            while len(arr) < 12:
                arr.append(0)
            prima = _num(arr[mese - 1])
            ultimo = r.get(campo)
            # numero scritto a mano: diverso da quello che aveva messo AlfaDocs l'ultima volta (o, la prima volta, già presente)
            a_mano = (ultimo is None and prima != 0) or (ultimo is not None and abs(prima - float(ultimo)) >= 0.01)
            if a_mano and abs(prima - v) >= 1:
                differenze.append({"sedeId": sid, "campo": campo, "prima": prima, "alfadocs": v})
            if abs(prima - v) >= 0.005:
                arr[mese - 1] = v
                cons[campo][sid] = arr
                cambiato = True
            r[campo] = v
            r[campo + "_il"] = now_iso()
            es[campo] = {"prima": prima, "dopo": v}
        esiti.append(es)
    if cambiato:
        salva_versione(db, cid, row["data"], "prima dell'aggiornamento da AlfaDocs", forza=True)
    db.execute("UPDATE companies SET data=?, rev=? WHERE id=?", (json.dumps(d), (row["rev"] or 0) + (1 if cambiato else 0), cid))
    db.commit()
    return jsonify({"ok": True, "esiti": esiti, "differenze": differenze, "cambiato": cambiato})


# AlfaDocs: «rimetti il mio valore» in una casella (resta bloccata per quel mese: AlfaDocs non la sovrascrive più)
@app.route("/api/_appgestione/alfadocs/ripristina", methods=["POST"])
def appgestione_alfadocs_ripristina():
    if not _chiave_app_ok():
        return json_error("Non autorizzato.", 401)
    body = request.get_json(force=True, silent=True) or {}
    cid, sid, campo = str(body.get("azienda") or ""), str(body.get("sedeId") or ""), str(body.get("campo") or "")
    try:
        anno, mese, valore = int(body.get("anno")), int(body.get("mese")), round(float(body.get("valore")), 2)
    except (TypeError, ValueError):
        return json_error("Dati non validi.", 400)
    if campo not in ("revenue", "produzione") or not 1 <= mese <= 12 or valore < 0:
        return json_error("Dati non validi.", 400)
    db = get_db()
    row = db.execute("SELECT data, rev FROM companies WHERE id=?", (cid,)).fetchone()
    if row is None:
        return json_error("Azienda non trovata.", 404)
    d = json.loads(row["data"] or "{}")
    if d.get("currentYear") != anno or not any(s.get("id") == sid for s in d.get("sites") or []):
        return json_error("Sede o anno non validi.", 409)
    arr = list(((d.setdefault("consuntivo", {})).setdefault(campo, {})).get(sid) or [])
    while len(arr) < 12:
        arr.append(0)
    arr[mese - 1] = valore
    d["consuntivo"][campo][sid] = arr
    r = d.setdefault("alfadocs", {}).setdefault("%04d-%02d" % (anno, mese), {}).setdefault(sid, {})
    r[campo + "_bloccato"] = True
    salva_versione(db, cid, row["data"], "prima di rimettere il valore a mano (AlfaDocs)", forza=True)
    db.execute("UPDATE companies SET data=?, rev=? WHERE id=?", (json.dumps(d), (row["rev"] or 0) + 1, cid))
    db.commit()
    return jsonify({"ok": True})


# REC · Pannello cassa: le spese di cassa (uscite) si aggiungono alla voce del Consuntivo della sede, nel loro mese.
# Si AGGIUNGE (delta) a quello che c'è già, mai sovrascritto; uno storno è un delta negativo. Voce trovata per nome
# tra le voci fisse e le categorie variabili; nelle variabili con fornitori va nella riga fornitore «Cassa (REC)».
def _norm_voce(x):
    return re.sub(r"[^a-z0-9]", "", (x or "").lower().replace("à", "a"))


def _trova_voce(d, nome):
    n = _norm_voce(nome)
    fx = [it for it in d.get("fixedItems") or [] if _norm_voce(it.get("name")) == n]
    vc = [c for c in d.get("varCats") or [] if _norm_voce(c.get("name")) == n]
    if len(fx) + len(vc) != 1:
        return None, None
    return ("fisso", fx[0]) if fx else ("variabile", vc[0])


@app.route("/api/_appgestione/voci", methods=["GET"])
def appgestione_voci():
    if not _chiave_app_ok():
        return json_error("Non autorizzato.", 401)
    row = get_db().execute("SELECT data FROM companies WHERE id=?", (request.args.get("azienda", ""),)).fetchone()
    if row is None:
        return json_error("Azienda non trovata.", 404)
    d = json.loads(row["data"] or "{}")
    cerca = [x for x in request.args.get("nomi", "").split("|") if x.strip()]
    trovate = {}
    for nome in cerca:
        tipo, v = _trova_voce(d, nome)
        trovate[nome] = {"tipo": tipo, "nome": v.get("name")} if v else None
    return jsonify({"anno": d.get("currentYear"), "voci": trovate})


@app.route("/api/_appgestione/costi-cassa", methods=["POST"])
def appgestione_costi_cassa():
    if not _chiave_app_ok():
        return json_error("Non autorizzato.", 401)
    body = request.get_json(force=True, silent=True) or {}
    cid = str(body.get("azienda") or "")
    try:
        anno = int(body.get("anno") or 0)
    except (TypeError, ValueError):
        return json_error("Anno non valido.", 400)
    mov = body.get("movimenti") or []
    if not isinstance(mov, list) or not mov or len(mov) > 500:
        return json_error("Nessuna spesa da mandare.", 400)
    db = get_db()
    row = db.execute("SELECT data, rev FROM companies WHERE id=?", (cid,)).fetchone()
    if row is None:
        return json_error("Azienda non trovata.", 404)
    d = json.loads(row["data"] or "{}")
    if d.get("currentYear") != anno:
        return json_error("Nella Gestione finanziaria il consuntivo aperto è del %s, non del %s." % (d.get("currentYear"), anno), 409)
    siti = {s.get("id") for s in d.get("sites") or []}
    # prima si controlla tutto, poi si scrive: o vanno tutte o nessuna
    piano = []
    for m in mov:
        try:
            mese = int(m.get("mese") or 0)
            delta = round(float(m.get("delta")), 2)
        except (TypeError, ValueError):
            return json_error("Dati non validi.", 400)
        sid = str(m.get("sedeId") or "")
        if sid not in siti:
            return json_error("Sede non trovata nella Gestione finanziaria.", 404)
        if not 1 <= mese <= 12 or abs(delta) > 1000000:
            return json_error("Dati non validi.", 400)
        tipo, v = _trova_voce(d, m.get("voce"))
        if not v:
            return json_error("La voce «%s» non c'è nella Gestione finanziaria (o ce n'è più di una con lo stesso nome)." % m.get("voce"), 404)
        piano.append((tipo, v, sid, mese, delta))
    cons = d.setdefault("consuntivo", {})
    for tipo, v, sid, mese, delta in piano:
        if tipo == "fisso":
            arr = cons.setdefault("fixed", {}).setdefault(v["id"], {}).get(sid)
            arr = list(arr or [])
            while len(arr) < 12:
                arr.append(0)
            arr[mese - 1] = round(_num(arr[mese - 1]) + delta, 2)
            cons["fixed"][v["id"]][sid] = arr
        else:
            e = cons.setdefault("varCat", {}).setdefault(v["id"], {}).get(sid) or {"total": [0] * 12, "suppliers": []}
            if e.get("suppliers"):
                riga = next((x for x in e["suppliers"] if x.get("name") == "Cassa (REC)"), None)
                if riga is None:
                    riga = {"id": "sup_rec_" + sid[-6:], "name": "Cassa (REC)", "values": [0] * 12}
                    e["suppliers"].append(riga)
                vals = list(riga.get("values") or [])
                while len(vals) < 12:
                    vals.append(0)
                vals[mese - 1] = round(_num(vals[mese - 1]) + delta, 2)
                riga["values"] = vals
            else:
                tot = list(e.get("total") or [])
                while len(tot) < 12:
                    tot.append(0)
                tot[mese - 1] = round(_num(tot[mese - 1]) + delta, 2)
                e["total"] = tot
            cons["varCat"][v["id"]][sid] = e
    rev = row["rev"] or 0
    salva_versione(db, cid, row["data"], "prima delle spese di cassa da REC", forza=True)
    db.execute("UPDATE companies SET data=?, rev=? WHERE id=?", (json.dumps(d), rev + 1, cid))
    db.commit()
    return jsonify({"ok": True, "scritte": len(piano)})


def _num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


# stessa regola della pagina Consuntivo: se la voce ha fornitori si sommano quelli, altrimenti il totale
def _cons_var_mese(d, cat, sid, m):
    e = (((d.get("consuntivo") or {}).get("varCat") or {}).get(cat.get("id")) or {}).get(sid)
    if not e:
        return 0.0
    sup = e.get("suppliers") or []
    if sup:
        return sum(_num((x.get("values") or [])[m] if len(x.get("values") or []) > m else 0) for x in sup)
    tot = e.get("total") or []
    return _num(tot[m]) if len(tot) > m else 0.0


# ---------- persone registrate nelle app To Smile → Gestione finanziaria ----------
# Medici a partita IVA: tra i dottori dei costi variabili («Costi per dottori»), spuntati nelle loro sedi.
# Dipendenti CCNL e partite IVA non medici: negli «Stipendi fissi lordi», un collaboratore per ogni loro sede.
# Si aggiunge e basta: niente si toglie o si rinomina, gli importi non si toccano. Chi è già presente con lo stesso
# nome (anche messo a mano) viene collegato invece di essere duplicato.
_TITOLI = {"dr", "dott", "dottor", "dottore", "dottssa", "dottoressa", "doc", "prof", "sig", "sigra"}
def _parole_nome(n):
    # «MURTAS SILVIA (RAP)» = «Silvia Murtas»: il ruolo tra parentesi non conta
    t = re.sub(r"\([^)]*\)", " ", str(n or "").lower())
    t = re.sub(r"[^a-zà-ü' ]", " ", t.replace(".", ""))
    return frozenset(w for w in t.split() if w and w not in _TITOLI)
def _stesso_nome(a, b):
    pa, pb = _parole_nome(a), _parole_nome(b)
    return bool(pa) and pa == pb
def _norm_sede_fin(x):
    return re.sub(r"[^a-z0-9]", "", re.sub(r"to\s*smile|studio|sede|ambulatorio", "", str(x or "").lower()))
def _trova_sede_fin(sites, nome):
    n = _norm_sede_fin(nome)
    if not n:
        return None
    L = [(s, _norm_sede_fin(s.get("name"))) for s in sites]
    es = [s for s, k in L if k and k == n]
    if len(es) == 1:
        return es[0]
    c = [s for s, k in L if k and (k in n or n in k)]
    return c[0] if len(c) == 1 else None


@app.route("/api/_appgestione/persone", methods=["POST"])
def appgestione_persone():
    if not _chiave_app_ok():
        return json_error("Non autorizzato.", 401)
    body = request.get_json(force=True, silent=True) or {}
    cid = str(body.get("azienda") or "")
    prova = bool(body.get("prova"))
    persone = body.get("persone") or []
    if not isinstance(persone, list) or len(persone) > 500:
        return json_error("Dati non validi.", 400)
    db = get_db()
    row = db.execute("SELECT data, rev FROM companies WHERE id=?", (cid,)).fetchone()
    if row is None:
        return json_error("Azienda non trovata.", 404)
    d = json.loads(row["data"] or "{}")
    if d.get("_editingArchivedYear") not in (None, ""):
        return json_error("In Gestione finanziaria è aperto un anno archiviato: riprova quando torni all'anno in corso.", 409)
    sites = d.get("sites") or []
    cat_dott = next((c for c in d.get("varCats") or [] if c.get("suppliers") and re.search(r"dottor|medic", c.get("name") or "", re.I)), None)
    cat_stip = next((c for c in d.get("fixedCats") or [] if re.search(r"stipend", c.get("name") or "", re.I)), None)
    M = d.setdefault("supplierMaster", [])
    items = d.setdefault("fixedItems", [])
    esiti, cambiato = [], False
    for p in persone:
        ref = str(p.get("ref") or "")[:64]
        nome = re.sub(r"\s+", " ", str(p.get("nome") or "")).strip()[:120]
        tipo = p.get("tipo")
        if not ref or not nome or tipo not in ("medico", "stipendio"):
            continue
        es = {"ref": ref, "nome": nome, "tipo": tipo, "sedi": [], "nuove": [], "senzaSede": [], "azione": "presente"}
        for sn in p.get("sedi") or []:
            s = _trova_sede_fin(sites, sn)
            if s:
                es["sedi"].append(s["id"])
            else:
                es["senzaSede"].append(str(sn))
        if tipo == "medico":
            if not cat_dott:
                es["azione"] = "manca la voce «Costi per dottori»"; esiti.append(es); continue
            m = next((x for x in M if x.get("catId") == cat_dott["id"] and x.get("ref") == ref), None)
            if m is None:
                m = next((x for x in M if x.get("catId") == cat_dott["id"] and not x.get("ref") and _stesso_nome(x.get("name"), nome)), None)
                if m is not None:
                    es["azione"] = "collegato"
                    if not prova:
                        m["ref"] = ref; cambiato = True
            if m is None:
                es["azione"] = "nuovo"
                if not prova:
                    m = {"id": "msup_" + uuid.uuid4().hex[:10], "catId": cat_dott["id"], "name": nome, "sites": {}, "parked": {}, "ref": ref}
                    M.append(m); cambiato = True
            gia = (m or {}).get("sites") or {}
            for sid in es["sedi"]:
                if not gia.get(sid):
                    es["nuove"].append(sid)
                    if not prova and m is not None:
                        m.setdefault("sites", {})[sid] = True; cambiato = True
        else:
            if not cat_stip:
                es["azione"] = "manca la categoria «Stipendi»"; esiti.append(es); continue
            nuovo_tutto = True
            for sid in es["sedi"]:
                it = next((x for x in items if x.get("catId") == cat_stip["id"] and x.get("siteId") == sid and x.get("ref") == ref), None)
                if it is None:
                    it = next((x for x in items if x.get("catId") == cat_stip["id"] and x.get("siteId") == sid and not x.get("ref") and _stesso_nome(x.get("name"), nome)), None)
                    if it is not None:
                        nuovo_tutto = False
                        if es["azione"] == "presente": es["azione"] = "collegato"
                        if not prova:
                            it["ref"] = ref; cambiato = True
                else:
                    nuovo_tutto = False
                if it is None:
                    es["nuove"].append(sid)
                    if not prova:
                        iid = "fitem_" + uuid.uuid4().hex[:10]
                        items.append({"id": iid, "catId": cat_stip["id"], "name": nome, "siteId": sid, "ref": ref})
                        for bg in (d.get("budget"), d.get("nextYearBudget"), d.get("_budgetLiveBackup")):
                            if isinstance(bg, dict):
                                bg.setdefault("fixed", {})[iid] = {s["id"]: 0 for s in sites}
                        d.setdefault("consuntivo", {}).setdefault("fixed", {})[iid] = {s["id"]: [0] * 12 for s in sites}
                        cambiato = True
            if es["nuove"] and nuovo_tutto:
                es["azione"] = "nuovo"
        nomi = {s["id"]: s.get("name") for s in sites}
        es["sedi"] = [nomi.get(x, x) for x in es["sedi"]]
        es["nuove"] = [nomi.get(x, x) for x in es["nuove"]]
        esiti.append(es)
    if cambiato and not prova:
        salva_versione(db, cid, row["data"], "prima delle persone registrate nelle app", forza=True)
        db.execute("UPDATE companies SET data=?, rev=? WHERE id=?", (json.dumps(d), (row["rev"] or 0) + 1, cid))
        db.commit()
    return jsonify({"ok": True, "prova": prova, "scritto": bool(cambiato and not prova), "esiti": esiti})


# ---------- Budget dell'anno dopo: persone che durante l'anno hanno cambiato sede ----------
# Durante l'anno non si toglie mai niente. Qui si confrontano le sedi della persona in Gestione finanziaria con le sue
# sedi di oggi in Gestione accessi: chi non c'è più in una sede si può togliere da quella sede SOLO alla chiusura dell'anno
# (lo fa la pagina, dopo aver archiviato l'anno). Questa chiamata non scrive niente.
@app.route("/api/company/cambi-sede", methods=["GET"])
def company_cambi_sede():
    if not require_company():
        return json_error("Non autorizzato.", 401)
    ok, err = check_company_access()
    if not ok:
        return err
    row = get_db().execute("SELECT data FROM companies WHERE id=?", (session["company_id"],)).fetchone()
    if row is None:
        return json_error("Azienda non trovata.", 404)
    d = json.loads(row["data"] or "{}")
    sites = d.get("sites") or []
    nomi = {s.get("id"): s.get("name") for s in sites}
    cat_dott = {c.get("id") for c in d.get("varCats") or [] if c.get("suppliers")}
    # (ref, chiave, nome, voce, sedeId)
    righe = []
    for m in d.get("supplierMaster") or []:
        if m.get("ref") and m.get("catId") in cat_dott:
            for sid, on in (m.get("sites") or {}).items():
                if on and sid in nomi:
                    righe.append((m["ref"], "sup:%s:%s" % (m["id"], sid), m.get("name") or "", "Costi per dottori", sid))
    voce_cat = {c.get("id"): c.get("name") or "" for c in d.get("fixedCats") or []}
    for it in d.get("fixedItems") or []:
        if it.get("ref") and it.get("siteId") in nomi:
            righe.append((it["ref"], "it:%s" % it["id"], it.get("name") or "", voce_cat.get(it.get("catId"), ""), it["siteId"]))
    if not righe:
        return jsonify({"persone": [], "collegate": False})
    if not sso_enabled():
        return json_error("Collegamento con Gestione accessi non configurato.", 503)
    st, r = sso_call("/api/_suite/finanza-persone-sedi", {})
    if st != 200:
        return json_error("Non riesco a leggere le sedi di oggi da Gestione accessi: riprova tra poco.", 502)
    oggi = {p.get("ref"): p for p in r.get("persone") or []}
    prima = {}
    for ref, _, _, _, sid in righe:
        prima.setdefault(ref, set()).add(sid)
    out = []
    for ref, chiave, nome, voce, sid in righe:
        p = oggi.get(ref)
        attivo = bool(p and p.get("attivo"))
        sedi_oggi = set()
        for sn in (p or {}).get("sedi") or []:
            s = _trova_sede_fin(sites, sn)
            if s:
                sedi_oggi.add(s["id"])
        if attivo and sid in sedi_oggi:
            continue
        if attivo and not (p or {}).get("sedi"):
            continue   # senza sedi in Gestione accessi: non si capisce, non si propone niente
        out.append({"chiave": chiave, "nome": nome, "voce": voce, "sede": nomi.get(sid, ""), "attivo": attivo,
                    "prima": sorted(nomi.get(x, "") for x in prima[ref]),
                    "oggi": sorted(nomi.get(x, "") for x in sedi_oggi),
                    # di base: spuntato se ha cambiato sede, non spuntato se non è più attivo
                    "spunta": attivo})
    out.sort(key=lambda x: (x["nome"].lower(), x["sede"]))
    return jsonify({"persone": out, "collegate": True})


# chiusura dell'anno: copia di sicurezza prima, poi gli avvisi al proprietario per ogni persona tolta
@app.route("/api/company/cambi-sede/versione", methods=["POST"])
def company_cambi_sede_versione():
    if not require_company():
        return json_error("Non autorizzato.", 401)
    ok, err = check_company_access()
    if not ok:
        return err
    db = get_db()
    row = db.execute("SELECT data FROM companies WHERE id=?", (session["company_id"],)).fetchone()
    if row is None:
        return json_error("Azienda non trovata.", 404)
    salva_versione(db, session["company_id"], row["data"], "prima della chiusura dell'anno", forza=True)
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/company/cambi-sede/avviso", methods=["POST"])
def company_cambi_sede_avviso():
    if not require_company():
        return json_error("Non autorizzato.", 401)
    ok, err = check_company_access()
    if not ok:
        return err
    righe = (request.get_json(force=True, silent=True) or {}).get("righe") or []
    if isinstance(righe, list) and righe and sso_enabled():
        sso_call("/api/_suite/finanza-avviso", {"righe": [str(x)[:300] for x in righe[:200]]})
    return jsonify({"ok": True})


# ---------- Compenso medici (app To Smile): il compenso pagato va nel Consuntivo ----------
# Riga del medico nei «Costi per dottori», nella sede del compenso, nel mese del pagamento: si SOMMA a quello che c'è.
# Ogni pagamento ha un riferimento: se arriva due volte non si somma di nuovo.
@app.route("/api/_appgestione/compensi-medici", methods=["POST"])
def appgestione_compensi_medici():
    if not _chiave_app_ok():
        return json_error("Non autorizzato.", 401)
    body = request.get_json(force=True, silent=True) or {}
    cid = str(body.get("azienda") or "")
    pag = body.get("pagamenti") or []
    if not isinstance(pag, list) or not pag or len(pag) > 500:
        return json_error("Nessun pagamento da mandare.", 400)
    db = get_db()
    row = db.execute("SELECT data, rev FROM companies WHERE id=?", (cid,)).fetchone()
    if row is None:
        return json_error("Azienda non trovata.", 404)
    d = json.loads(row["data"] or "{}")
    if d.get("_editingArchivedYear") not in (None, ""):
        return json_error("In Gestione finanziaria è aperto un anno archiviato: riprova quando torni all'anno in corso.", 409)
    cat = next((c for c in d.get("varCats") or [] if c.get("suppliers") and re.search(r"dottor|medic", c.get("name") or "", re.I)), None)
    if not cat:
        return json_error("In Gestione finanziaria non trovo la voce «Costi per dottori».", 404)
    sites = d.get("sites") or []
    M = d.setdefault("supplierMaster", [])
    fatti = d.setdefault("_compensiPagati", [])
    # prima si controlla tutto, poi si scrive: o vanno tutti o nessuno
    piano = []
    for p in pag:
        ref = str(p.get("ref") or "")[:80]
        try:
            anno = int(p.get("anno") or 0); mese = int(p.get("mese") or 0)
            imp = round(float(p.get("importo")), 2)
        except (TypeError, ValueError):
            return json_error("Dati non validi.", 400)
        if not ref or not 1 <= mese <= 12 or not 0 < imp <= 1000000:
            return json_error("Dati non validi.", 400)
        if ref in fatti:
            continue
        if d.get("currentYear") != anno:
            return json_error("Nella Gestione finanziaria il consuntivo aperto è del %s, il pagamento è del %s." % (d.get("currentYear"), anno), 409)
        s = _trova_sede_fin(sites, p.get("sede"))
        if not s:
            return json_error("La sede «%s» non c'è nella Gestione finanziaria." % p.get("sede"), 404)
        m = next((x for x in M if x.get("catId") == cat["id"] and x.get("ref") == str(p.get("medicoRef") or "")), None)
        if m is None:
            return json_error("%s non è ancora tra i medici della Gestione finanziaria." % (p.get("nome") or "Il medico"), 404)
        piano.append((ref, m, s["id"], mese, imp))
    if not piano:
        return jsonify({"ok": True, "scritti": 0})
    cons = d.setdefault("consuntivo", {}).setdefault("varCat", {}).setdefault(cat["id"], {})
    for ref, m, sid, mese, imp in piano:
        m.setdefault("sites", {})[sid] = True
        e = cons.get(sid)
        if not isinstance(e, dict):
            e = {"total": [0] * 12, "suppliers": []}
            cons[sid] = e
        if not isinstance(e.get("suppliers"), list):
            e["suppliers"] = []
        if not isinstance(e.get("total"), list):
            e["total"] = [0] * 12
        riga = next((x for x in e["suppliers"] if x.get("id") == m["id"]), None)
        if riga is None:
            # come fa la pagina: valori parcheggiati della riga, oppure il totale se la voce non aveva ancora righe
            pk = (m.get("parked") or {}).get(sid)
            if isinstance(pk, dict) and isinstance(pk.get("values"), list):
                vals = [_num(v) for v in pk["values"]]; m["parked"].pop(sid, None)
            elif not e["suppliers"] and any(_num(v) for v in e["total"]):
                vals = [_num(v) for v in e["total"]]; e["total"] = [0] * 12
            else:
                vals = [0] * 12
            riga = {"id": m["id"], "name": m.get("name"), "values": vals}
            e["suppliers"].append(riga)
        vals = list(riga.get("values") or [])
        while len(vals) < 12:
            vals.append(0)
        vals[mese - 1] = round(_num(vals[mese - 1]) + imp, 2)
        riga["values"] = vals
        fatti.append(ref)
    salva_versione(db, cid, row["data"], "prima dei compensi medici pagati", forza=True)
    db.execute("UPDATE companies SET data=?, rev=? WHERE id=?", (json.dumps(d), (row["rev"] or 0) + 1, cid))
    db.commit()
    return jsonify({"ok": True, "scritti": len(piano)})


@app.route("/api/_trasloco/export", methods=["GET"])
def _trasloco_export():
    if not _trasloco_chiave_ok(request):
        return jsonify({"error": "Non autorizzato"}), 401
    tmp = tempfile.mkdtemp()
    try:
        copia = os.path.join(tmp, "data.db")
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(copia)
        with dst:
            src.backup(dst)  # copia coerente anche mentre il server lavora
        dst.close(); src.close()
        manifest = {"creato": datetime.datetime.utcnow().isoformat() + "Z", "righe": _conta_righe(copia), "altri_file": []}
        buf = _io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(copia, arcname="data.db")
            for nome in sorted(os.listdir(DATA_DIR)):
                pieno = os.path.join(DATA_DIR, nome)
                if nome.startswith("data.db") or nome.startswith(".trasloco") or nome in ("lost+found",):
                    continue
                tar.add(pieno, arcname="altri/" + nome)
                manifest["altri_file"].append(nome)
            m = json.dumps(manifest, ensure_ascii=False).encode()
            info = tarfile.TarInfo("manifest.json"); info.size = len(m)
            tar.addfile(info, _io.BytesIO(m))
        dati = buf.getvalue()
        print("Trasloco: export di %d byte, righe %s" % (len(dati), manifest["righe"]), flush=True)
        return dati, 200, {"Content-Type": "application/gzip", "X-Sha256": hashlib.sha256(dati).hexdigest(), "Cache-Control": "no-store"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def _trasloco_import():
    url, key = os.environ.get("COPIA_DA", "").strip(), os.environ.get("TRASLOCO_KEY", "")
    if not url or len(key) < 24:
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    marcatore = os.path.join(DATA_DIR, ".trasloco_ok")
    if os.path.exists(marcatore) and os.environ.get("COPIA_FORZA") != "true":
        print("Trasloco: gia' fatto, non ricopio (togli COPIA_DA).", flush=True)
        return
    import urllib.request as _rq
    print("Trasloco: scarico la copia dal vecchio servizio (sola lettura)...", flush=True)
    req = _rq.Request(url, headers={"Authorization": "Bearer " + key})
    with _rq.urlopen(req, timeout=300) as r:
        dati = r.read()
        atteso = r.headers.get("X-Sha256", "")
    if hashlib.sha256(dati).hexdigest() != atteso:
        raise RuntimeError("Trasloco: impronta diversa, copia scartata")
    tmp = tempfile.mkdtemp(dir=DATA_DIR)
    try:
        with tarfile.open(fileobj=_io.BytesIO(dati), mode="r:gz") as tar:
            for m in tar.getmembers():
                if m.name.startswith("/") or ".." in m.name.split("/") or not (m.isfile() or m.isdir()):
                    raise RuntimeError("Trasloco: archivio non valido")
            tar.extractall(tmp)
        manifest = json.load(open(os.path.join(tmp, "manifest.json")))
        nuovo = os.path.join(tmp, "data.db")
        con = sqlite3.connect(nuovo)
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        con.close()
        if ok != "ok":
            raise RuntimeError("Trasloco: database copiato non integro (%s)" % ok)
        righe = _conta_righe(nuovo)
        if righe != manifest["righe"]:
            raise RuntimeError("Trasloco: righe diverse %s / %s" % (righe, manifest["righe"]))
        # il database che c'era sul nuovo disco (vuoto, creato all'avvio) non si cancella: si rinomina
        stamp = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
        for suff in ("", "-wal", "-shm"):
            if os.path.exists(DB_PATH + suff):
                os.replace(DB_PATH + suff, DB_PATH + suff + ".prima-del-trasloco-" + stamp)
        os.replace(nuovo, DB_PATH)
        for nome in manifest.get("altri_file", []):
            sorgente = os.path.join(tmp, "altri", nome)
            if os.path.exists(sorgente):
                dest = os.path.join(DATA_DIR, nome)
                if os.path.exists(dest):
                    os.replace(dest, dest + ".prima-del-trasloco-" + stamp)
                os.replace(sorgente, dest)
        tot = sum(righe.values())
        with open(marcatore, "w") as f:
            json.dump({"fatto": datetime.datetime.utcnow().isoformat() + "Z", "righe": righe, "altri_file": manifest.get("altri_file", [])}, f)
        print("Trasloco: OK · %d tabelle, %d righe, %d altri file · %s" % (len(righe), tot, len(manifest.get("altri_file", [])), righe), flush=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


_trasloco_import()

init_db()

# Backup unico della suite: copia notturna di tutte le app (To Smile e clienti) su questo disco
import backup as _backup  # noqa: E402
_backup.registra(app, DATA_DIR, DB_PATH)



# Pulizia approvata da Giancarlo il 24/11/2026 («rimetti tutto a posto»), una volta sola:
# toglie SOLO le righe doppie create dall'allineamento persone del 23/11 (non c'erano nella copia 61, hanno il collegamento
# alla persona e sono a zero ovunque: budget, bozza, consuntivo) quando nella stessa sede c'è già la riga vecchia della
# stessa persona; il collegamento passa alla riga vecchia, che tiene nome e importi. Le persone davvero nuove restano.
# Prima salva una copia («prima della pulizia dei doppioni»), che resta per tornare indietro.
def _pulizia_doppioni_20261124():
    try:
        db = sqlite3.connect(DB_PATH); db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE IF NOT EXISTS operazioni_fatte (chiave TEXT PRIMARY KEY, il TEXT)")
        if db.execute("SELECT 1 FROM operazioni_fatte WHERE chiave='pulizia-doppioni-20261124'").fetchone():
            db.close(); return
        full = db.execute("SELECT id FROM companies WHERE id LIKE 'cd1ac58f%'").fetchone()
        base = full and db.execute("SELECT data FROM company_versions WHERE company_id=? AND id=61 AND motivo LIKE 'prima delle persone%'", (full["id"],)).fetchone()
        if not full or not base:
            print("Pulizia doppioni: copia 61 non trovata, non faccio nulla", flush=True); db.close(); return
        cid = full["id"]
        row = db.execute("SELECT data, rev FROM companies WHERE id=?", (cid,)).fetchone()
        d = json.loads(row["data"] or "{}")
        if d.get("_editingArchivedYear") not in (None, "") or d.get("_editingNextYearBudget"):
            print("Pulizia doppioni: anno archiviato o bozza aperta, rimando", flush=True); db.close(); return
        prima = json.loads(_decomprimi(base["data"]))
        ids_prima = {x.get("id") for x in prima.get("fixedItems") or []}
        sup_prima = {x.get("id") for x in prima.get("supplierMaster") or []}
        sites = {x.get("id"): x.get("name") for x in d.get("sites") or []}
        bgs = [bg for bg in (d.get("budget"), d.get("nextYearBudget"), d.get("_budgetLiveBackup")) if isinstance(bg, dict)]
        cons = d.setdefault("consuntivo", {})
        def zero_fisso(iid):
            for bg in bgs:
                if any(float(v or 0) for v in ((bg.get("fixed") or {}).get(iid) or {}).values()): return False
            return not any(float(x or 0) for v in ((cons.get("fixed") or {}).get(iid) or {}).values() for x in (v or []))
        tolte, collegate, restano = [], 0, []
        items = d.get("fixedItems") or []
        nuove = [x for x in items if x.get("id") not in ids_prima and x.get("ref")]
        via = set()
        for n in nuove:
            vecchie = [x for x in items if x.get("id") in ids_prima and x.get("catId") == n.get("catId") and x.get("siteId") == n.get("siteId")
                       and not x.get("ref") and _stesso_nome(x.get("name"), n.get("name"))]
            if len(vecchie) == 1 and zero_fisso(n.get("id")):
                vecchie[0]["ref"] = n["ref"]; collegate += 1; via.add(n["id"])
                tolte.append("%s: %s (resta %s)" % (sites.get(n.get("siteId"), "?"), n.get("name"), vecchie[0].get("name")))
            else:
                restano.append("%s: %s" % (sites.get(n.get("siteId"), "?"), n.get("name")))
        if via:
            d["fixedItems"] = [x for x in items if x.get("id") not in via]
            for bg in bgs:
                for iid in via: (bg.get("fixed") or {}).pop(iid, None)
            for iid in via: (cons.get("fixed") or {}).pop(iid, None)
        # medici (Costi per dottori): stessa regola
        M = d.get("supplierMaster") or []
        vc = cons.get("varCat") or {}
        def zero_sup(m):
            if any(float(x or 0) for p in (m.get("parked") or {}).values() for x in ((p or {}).get("values") or [])): return False
            for e in (vc.get(m.get("catId")) or {}).values():
                for sp in (e or {}).get("suppliers") or [] if isinstance(e, dict) else []:
                    if sp.get("id") == m.get("id") and any(float(x or 0) for x in sp.get("values") or []): return False
            return True
        via_m = set()
        for m in [x for x in M if x.get("id") not in sup_prima and x.get("ref")]:
            vecchi = [x for x in M if x.get("id") in sup_prima and x.get("catId") == m.get("catId") and not x.get("ref") and _stesso_nome(x.get("name"), m.get("name"))]
            if len(vecchi) == 1 and zero_sup(m):
                vecchi[0]["ref"] = m["ref"]; via_m.add(m["id"]); collegate += 1
                tolte.append("medici: %s (resta %s)" % (m.get("name"), vecchi[0].get("name")))
            else:
                restano.append("medici: %s" % m.get("name"))
        if via_m:
            d["supplierMaster"] = [x for x in M if x.get("id") not in via_m]
            for per in vc.values():
                for e in (per or {}).values():
                    if isinstance(e, dict) and e.get("suppliers"):
                        e["suppliers"] = [sp for sp in e["suppliers"] if sp.get("id") not in via_m]
        if tolte:
            salva_versione(db, cid, row["data"], "prima della pulizia dei doppioni", forza=True)
            db.execute("UPDATE companies SET data=?, rev=? WHERE id=?", (json.dumps(d), (row["rev"] or 0) + 1, cid))
        db.execute("INSERT INTO operazioni_fatte (chiave, il) VALUES ('pulizia-doppioni-20261124', ?)", (now_iso(),))
        db.commit(); db.close()
        print("Pulizia doppioni: tolte %d righe doppie vuote (collegate alla riga vecchia). TOLTE: %s || RESTANO (persone nuove o non sicure): %s" % (len(tolte), " ; ".join(tolte), " ; ".join(restano)), flush=True)
    except Exception as e:
        print("Pulizia doppioni: errore, non fatto nulla:", e, flush=True)


_pulizia_doppioni_20261124()

# App di bilancio (Controllo di gestione): pagina /bilancio e API /api/bilancio/... nello stesso servizio
import bilancio_api
bilancio_api.registra(app, {
    "get_db": get_db, "json_error": json_error, "sso_call": sso_call, "sso_enabled": sso_enabled, "SSO_URL": SSO_URL,
    "sso_company_ids": sso_company_ids, "now_iso": now_iso, "STATIC_DIR": STATIC_DIR,
    "connect": lambda: (os.makedirs(DATA_DIR, exist_ok=True), sqlite3.connect(DB_PATH))[1],
})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
