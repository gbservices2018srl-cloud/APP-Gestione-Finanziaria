"""
App di bilancio (Controllo di gestione) — vive nello stesso servizio della Gestione finanziaria.

- Si entra solo con l'accesso unico di appgestione.it, con il permesso dell'app "bilancio"
  (ruolo: Amministratore = carica bilanci e modifica; Utente = consulta).
- Le aziende visibili sono quelle assegnate alla persona nella Gestione finanziaria (stesse assegnazioni).
- I dati di ogni azienda sono un blocco JSON (voci del piano dei conti con la riclassificazione,
  bilancio di riferimento, budget, consuntivi caricati): i calcoli (KPI, BEP, scostamenti) li fa la pagina.
- Il PDF del bilancio a 4 sezioni viene letto qui (bilancio_lettore) e restituito come elenco di voci.
"""
import json
import time
from flask import request, jsonify, session, send_from_directory

MAX_PDF = 15 * 1024 * 1024


def registra(app, h):
    get_db, json_error, sso_call, sso_enabled = h["get_db"], h["json_error"], h["sso_call"], h["sso_enabled"]
    SSO_URL, sso_company_ids, now_iso, STATIC_DIR = h["SSO_URL"], h["sso_company_ids"], h["now_iso"], h["STATIC_DIR"]

    db0 = h["connect"]()
    db0.execute("""CREATE TABLE IF NOT EXISTS bilancio (
        company_id TEXT PRIMARY KEY,
        data TEXT NOT NULL DEFAULT '{}',
        versione INTEGER NOT NULL DEFAULT 0,
        aggiornato_il TEXT,
        aggiornato_da TEXT
    )""")
    db0.commit()
    db0.close()

    def verifica():
        """(stato, dati) dall'accesso unico per l'app "bilancio"."""
        token = request.cookies.get("ag_sso")
        if not token or not sso_enabled():
            return 401, {}
        return sso_call("/api/sso/verify", {"token": token, "app": "bilancio"})

    def chi():
        """Persona e azienda dell'App di bilancio (ricontrollate ogni minuto), oppure (None, errore)."""
        if not session.get("bil_uid") or not session.get("bil_company"):
            return None, json_error("Accesso richiesto.", 401)
        if time.time() - float(session.get("bil_checked") or 0) > 60:
            code, data = verifica()
            u = data.get("user") if code == 200 else None
            if not u or u.get("id") != session.get("bil_uid") or session.get("bil_company") not in sso_company_ids(u["id"]):
                for k in ("bil_uid", "bil_company", "bil_checked", "bil_role", "bil_name"):
                    session.pop(k, None)
                return None, json_error("Accesso scaduto o non più abilitato." if code != 403 else "L'App di bilancio non è disponibile per te.", 401 if code != 403 else 403)
            session["bil_checked"] = time.time()
            session["bil_role"] = data.get("role") or "user"
        return {"uid": session["bil_uid"], "company": session["bil_company"], "admin": session.get("bil_role") == "admin",
                "name": session.get("bil_name") or ""}, None

    @app.route("/bilancio", methods=["GET"])
    @app.route("/bilancio/", methods=["GET"])
    def bil_pagina():
        r = send_from_directory(STATIC_DIR, "bilancio.html")
        r.headers["Cache-Control"] = "no-cache"
        return r

    @app.route("/api/bilancio/accesso", methods=["GET"])
    def bil_accesso():
        """Per la pagina: chi sei, quali aziende puoi aprire (e se ne hai una sola entri subito)."""
        out = {"home": SSO_URL, "loginUrl": SSO_URL + "/accedi?next=" + request.host_url.rstrip("/") + "/bilancio"}
        if not sso_enabled():
            return jsonify({**out, "stato": "nosso"})
        code, data = verifica()
        if code == 403:
            return jsonify({**out, "stato": "negato", "spenta": bool(data.get("spenta")) if isinstance(data, dict) else False})
        if code != 200:
            return jsonify({**out, "stato": "login"})
        u = data["user"]
        ids = sso_company_ids(u["id"])
        rows = get_db().execute(
            "SELECT id, name FROM companies WHERE approved=1 AND suspended=0 AND id IN (%s) ORDER BY name" % ",".join("?" * len(ids)),
            ids).fetchall() if ids else []
        return jsonify({**out, "stato": "ok", "user": u, "role": data.get("role"), "companies": [dict(r) for r in rows],
                        "attuale": session.get("bil_company") if session.get("bil_uid") == u["id"] else None})

    @app.route("/api/bilancio/entra", methods=["POST"])
    def bil_entra():
        body = request.get_json(force=True, silent=True) or {}
        code, data = verifica()
        if code != 200:
            return json_error("Accedi da appgestione.it." if code != 403 else "Non sei abilitato all'App di bilancio.", 401 if code != 403 else 403)
        u = data["user"]
        ids = sso_company_ids(u["id"])
        cid = body.get("company_id") or (ids[0] if len(ids) == 1 else None)
        if not cid or cid not in ids:
            return json_error("Scegli un'azienda tra quelle assegnate.", 400)
        row = get_db().execute("SELECT id, name FROM companies WHERE id=? AND approved=1 AND suspended=0", (cid,)).fetchone()
        if row is None:
            return json_error("Azienda non disponibile.", 404)
        session.permanent = True
        session["bil_uid"] = u["id"]
        session["bil_company"] = row["id"]
        session["bil_role"] = data.get("role") or "user"
        session["bil_name"] = (u.get("firstName", "") + " " + u.get("lastName", "")).strip() or u.get("email", "")
        session["bil_checked"] = time.time()
        return jsonify({"ok": True, "name": row["name"]})

    @app.route("/api/bilancio/dati", methods=["GET"])
    def bil_dati():
        me, err = chi()
        if err:
            return err
        db = get_db()
        az = db.execute("SELECT name FROM companies WHERE id=?", (me["company"],)).fetchone()
        r = db.execute("SELECT data, versione, aggiornato_il, aggiornato_da FROM bilancio WHERE company_id=?", (me["company"],)).fetchone()
        return jsonify({"data": json.loads(r["data"]) if r else {}, "versione": r["versione"] if r else 0,
                        "aggiornatoIl": r["aggiornato_il"] if r else None, "aggiornatoDa": r["aggiornato_da"] if r else None,
                        "azienda": az["name"] if az else "", "utente": me["name"], "admin": me["admin"],
                        "aziende": len(sso_company_ids(me["uid"])), "home": SSO_URL})

    @app.route("/api/bilancio/dati", methods=["PUT"])
    def bil_salva():
        me, err = chi()
        if err:
            return err
        if not me["admin"]:
            return json_error("Puoi consultare l'App di bilancio, ma le modifiche le fa l'amministratore.", 403)
        body = request.get_json(force=True, silent=True) or {}
        data = body.get("data")
        if not isinstance(data, dict):
            return json_error("Dati non validi.")
        testo = json.dumps(data, ensure_ascii=False)
        if len(testo) > 8_000_000:
            return json_error("Dati troppo grandi.")
        db = get_db()
        r = db.execute("SELECT versione FROM bilancio WHERE company_id=?", (me["company"],)).fetchone()
        attuale = r["versione"] if r else 0
        # due persone sulla stessa azienda: chi salva su una versione vecchia riceve quella nuova invece di sovrascriverla
        if int(body.get("versione") or 0) != attuale:
            r2 = db.execute("SELECT data, aggiornato_da FROM bilancio WHERE company_id=?", (me["company"],)).fetchone()
            return jsonify({"error": "Nel frattempo %s ha salvato delle modifiche: ricarico i dati aggiornati." % ((r2["aggiornato_da"] if r2 else "") or "qualcuno"),
                            "conflitto": True, "data": json.loads(r2["data"]) if r2 else {}, "versione": attuale}), 409
        nuova = attuale + 1
        db.execute("""INSERT INTO bilancio (company_id, data, versione, aggiornato_il, aggiornato_da) VALUES (?,?,?,?,?)
            ON CONFLICT(company_id) DO UPDATE SET data=excluded.data, versione=excluded.versione, aggiornato_il=excluded.aggiornato_il, aggiornato_da=excluded.aggiornato_da""",
                   (me["company"], testo, nuova, now_iso(), me["name"]))
        db.commit()
        return jsonify({"ok": True, "versione": nuova})

    @app.route("/api/bilancio/leggi", methods=["POST"])
    def bil_leggi():
        """Legge il PDF del bilancio a 4 sezioni e restituisce le voci del conto economico (non salva nulla)."""
        me, err = chi()
        if err:
            return err
        if not me["admin"]:
            return json_error("Solo l'amministratore carica i bilanci.", 403)
        f = request.files.get("pdf")
        if f is None:
            return json_error("Scegli il PDF del bilancio.")
        dati = f.read(MAX_PDF + 1)
        if len(dati) > MAX_PDF:
            return json_error("Il PDF è troppo grande (massimo 15 MB).")
        if not dati.startswith(b"%PDF"):
            return json_error("Il file non è un PDF.")
        try:
            from bilancio_lettore import leggi_bilancio
            r = leggi_bilancio(dati)
        except Exception as e:
            print("Bilancio: lettura PDF non riuscita:", e)
            return json_error("Non riesco a leggere questo PDF. Serve il bilancio a 4 sezioni (situazione contabile) esportato dal gestionale, non una scansione.")
        if not r["voci"]:
            return json_error("Nel PDF non ho trovato le voci del conto economico (situazione economica con codici conto e saldi).")
        r["file"] = (f.filename or "bilancio.pdf")[:120]
        return jsonify(r)
