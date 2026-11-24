# Backup unico della suite (cassaforte su questo disco, /var/data/backup).
# Ogni notte alle 03:00 (ora italiana) salva, per To Smile e per ogni cliente della suite:
#   - dati-AAAA-MM-GG.tar.gz      tutti i dati delle app (esportati da appgestione.it)
# e solo per To Smile anche:
#   - finanza-AAAA-MM-GG.db.gz    il database di Gestione finanziaria
#   - nuovalab-AAAA-MM-GG.tar.gz  le tabelle di Nuovalab   (se c'è NUOVALAB_BACKUP_KEY)
#   - ticket-AAAA-MM-GG.tar.gz    le tabelle dell'app Ticket (se c'è TICKET_BACKUP_KEY)
#   - file-nuovalab/, file-ticket/  i file allegati: copiati una volta, poi solo i nuovi (mai cancellati)
# Solo lettura sulle app: non modifica niente. Si tengono le copie degli ultimi 30 giorni.
import os
import io
import re
import json
import gzip
import hmac
import time
import shutil
import sqlite3
import tarfile
import hashlib
import datetime
import threading
import urllib.request
import urllib.parse
from zoneinfo import ZoneInfo
from flask import request, jsonify, Response, send_file

ROMA = ZoneInfo("Europe/Rome")
TENERE = 30
SUPA = {
    "nuovalab": ("https://ncfsugpmqimukynuvzba.supabase.co", "NUOVALAB_BACKUP_KEY"),
    "ticket": ("https://pswocuqnwltwjembncnq.supabase.co", "TICKET_BACKUP_KEY"),
}


def _slug(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "tosmile").lower()) or "tosmile"


def _token(slug):
    k = os.environ.get("SSO_API_KEY", "x")
    return hmac.new(k.encode(), ("backup:" + _slug(slug)).encode(), hashlib.sha256).hexdigest()


def _autorizzato(slug):
    h = request.headers.get("Authorization", "")
    k = os.environ.get("SSO_API_KEY", "")
    ok = lambda x: bool(x) and hmac.compare_digest(h.encode(), ("Bearer " + x).encode())
    return ok(k) or ok(_token(slug))


def registra(app, data_dir, db_path):
    base = os.path.join(data_dir, "backup")
    principale = os.environ.get("APPGESTIONE_URL", "https://appgestione.it").rstrip("/")
    os.makedirs(base, exist_ok=True)
    blocco = threading.Lock()

    def cartella(slug):
        p = os.path.join(base, _slug(slug))
        os.makedirs(p, exist_ok=True)
        return p

    def leggi_stato(slug):
        try:
            with open(os.path.join(cartella(slug), "stato.json")) as f:
                return json.load(f)
        except Exception:
            return {}

    def scrivi_stato(slug, st):
        p = os.path.join(cartella(slug), "stato.json")
        with open(p + ".part", "w") as f:
            json.dump(st, f)
        os.replace(p + ".part", p)

    def pulisci(slug, prefisso):
        file = sorted(x for x in os.listdir(cartella(slug)) if x.startswith(prefisso + "-") and not x.endswith(".part"))
        for x in file[:-TENERE]:
            os.remove(os.path.join(cartella(slug), x))

    def scarica_in(url, headers, dest):
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=600) as r, open(dest + ".part", "wb") as f:
            shutil.copyfileobj(r, f, 1024 * 1024)
        os.replace(dest + ".part", dest)
        return os.path.getsize(dest)

    def chiedi_json(url, headers, metodo="GET", corpo=None):
        dati = json.dumps(corpo).encode() if corpo is not None else None
        h = dict(headers)
        if dati is not None:
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=dati, headers=h, method=metodo)
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode() or "null"), dict(r.headers)

    # ---- Supabase (Nuovalab, Ticket): tabelle via REST e file allegati ----
    def esporta_supabase(nome, giorno, st):
        url, var = SUPA[nome]
        chiave = os.environ.get(var, "")
        if not chiave:
            st["parti"][nome] = "chiave mancante (" + var + ")"
            return
        H = {"apikey": chiave, "Authorization": "Bearer " + chiave}
        spec, _ = chiedi_json(url + "/rest/v1/", H)
        tabelle = sorted((spec or {}).get("definitions", {}).keys())
        dest = os.path.join(cartella("tosmile"), f"{nome}-{giorno}.tar.gz")
        righe_tot = 0
        with tarfile.open(dest + ".part", "w:gz") as tar:
            for t in tabelle:
                buf, inizio = io.BytesIO(), 0
                while True:
                    h = dict(H, **{"Range-Unit": "items", "Range": f"{inizio}-{inizio + 999}"})
                    righe, _ = chiedi_json(f"{url}/rest/v1/{urllib.parse.quote(t)}?select=*", h)
                    if not righe:
                        break
                    for r in righe:
                        buf.write((json.dumps(r, ensure_ascii=False) + "\n").encode())
                    righe_tot += len(righe)
                    if len(righe) < 1000:
                        break
                    inizio += 1000
                info = tarfile.TarInfo(f"{nome}/{t}.ndjson")
                info.size = buf.tell()
                info.mtime = int(time.time())
                buf.seek(0)
                tar.addfile(info, buf)
        os.replace(dest + ".part", dest)
        pulisci("tosmile", nome)
        st["parti"][nome] = {"tabelle": len(tabelle), "righe": righe_tot, "byte": os.path.getsize(dest)}
        # file allegati: solo quelli nuovi
        dir_file = os.path.join(cartella("tosmile"), "file-" + nome)
        nuovi, saltati = 0, 0
        secchi, _ = chiedi_json(url + "/storage/v1/bucket", H)
        for b in secchi or []:
            coda = [""]
            while coda:
                pref = coda.pop()
                offset = 0
                while True:
                    voci, _ = chiedi_json(f"{url}/storage/v1/object/list/{b['id']}", H, "POST",
                                          {"prefix": pref, "limit": 1000, "offset": offset, "sortBy": {"column": "name", "order": "asc"}})
                    for v in voci or []:
                        percorso = (pref + "/" if pref else "") + v["name"]
                        if v.get("id") is None:
                            coda.append(percorso)
                            continue
                        dest_f = os.path.join(dir_file, b["id"], *percorso.split("/"))
                        if os.path.exists(dest_f):
                            continue
                        if shutil.disk_usage(base).free < 300 * 1024 * 1024:
                            saltati += 1
                            continue
                        os.makedirs(os.path.dirname(dest_f), exist_ok=True)
                        scarica_in(f"{url}/storage/v1/object/{b['id']}/{urllib.parse.quote(percorso)}", H, dest_f)
                        nuovi += 1
                    if not voci or len(voci) < 1000:
                        break
                    offset += 1000
        st["parti"]["file-" + nome] = {"nuovi": nuovi, "saltati_spazio": saltati}
        if saltati:
            st["errori"].append(f"{nome}: {saltati} file non copiati per spazio sul disco insufficiente")

    def notte(solo=None):
        if not blocco.acquire(blocking=False):
            return
        try:
            giorno = datetime.datetime.now(ROMA).strftime("%Y-%m-%d")
            k = os.environ.get("SSO_API_KEY", "")
            try:
                elenco, _ = chiedi_json(principale + "/api/_backup/clienti", {"Authorization": "Bearer " + k})
                clienti = elenco.get("clienti") or ["tosmile"]
            except Exception as e:
                clienti = ["tosmile"]
                print("Backup: elenco clienti non letto:", e, flush=True)
            for slug in clienti:
                if solo and slug != solo:
                    continue
                st = {"ultimo": datetime.datetime.now(datetime.timezone.utc).isoformat(), "ok": True, "errori": [], "parti": {}}
                try:
                    dest = os.path.join(cartella(slug), f"dati-{giorno}.tar.gz")
                    st["parti"]["dati"] = {"byte": scarica_in(f"{principale}/api/_backup/esporta?cliente={slug}", {"Authorization": "Bearer " + k}, dest)}
                    pulisci(slug, "dati")
                except Exception as e:
                    st["errori"].append("dati: " + str(e))
                if slug == "tosmile":
                    try:  # database di Gestione finanziaria (copia coerente anche mentre si lavora)
                        tmp = os.path.join(cartella(slug), "finanza.tmp.db")
                        src, dst = sqlite3.connect(db_path), sqlite3.connect(tmp)
                        src.backup(dst)
                        src.close()
                        dst.close()
                        dest = os.path.join(cartella(slug), f"finanza-{giorno}.db.gz")
                        with open(tmp, "rb") as fi, gzip.open(dest + ".part", "wb") as fo:
                            shutil.copyfileobj(fi, fo)
                        os.replace(dest + ".part", dest)
                        os.remove(tmp)
                        pulisci(slug, "finanza")
                        st["parti"]["finanza"] = {"byte": os.path.getsize(dest)}
                    except Exception as e:
                        st["errori"].append("finanza: " + str(e))
                    for nome in SUPA:
                        try:
                            esporta_supabase(nome, giorno, st)
                        except Exception as e:
                            st["errori"].append(nome + ": " + str(e))
                st["ok"] = not st["errori"]
                scrivi_stato(slug, st)
                print(f"Backup {slug}: {'OK' if st['ok'] else 'con errori'} {json.dumps(st['parti'])[:300]}", flush=True)
        finally:
            blocco.release()

    def pianifica():
        while True:
            ora = datetime.datetime.now(ROMA)
            dopo = ora.replace(hour=3, minute=0, second=0, microsecond=0)
            if dopo <= ora:
                dopo += datetime.timedelta(days=1)
            time.sleep(max(60, (dopo - ora).total_seconds()))
            try:
                notte()
            except Exception as e:
                print("Backup notturno non riuscito:", e, flush=True)

    if os.environ.get("BACKUP_NOTTURNO", "true") == "true":
        threading.Thread(target=pianifica, daemon=True).start()
        if not leggi_stato("tosmile"):  # prima accensione: la prima copia si fa subito, senza aspettare la notte
            threading.Timer(90, notte).start()

    @app.route("/api/_backup/stato", methods=["GET"])
    def backup_stato():
        slug = _slug(request.args.get("cliente"))
        if not _autorizzato(slug):
            return jsonify({"error": "Non autorizzato"}), 401
        st = leggi_stato(slug)
        st["libero_mb"] = shutil.disk_usage(base).free // (1024 * 1024)
        return jsonify(st)

    @app.route("/api/_backup/ultimo", methods=["GET"])
    def backup_ultimo():
        slug = _slug(request.args.get("cliente"))
        parte = request.args.get("parte", "dati")
        if not _autorizzato(slug):
            return jsonify({"error": "Non autorizzato"}), 401
        if parte not in ("dati", "finanza", "nuovalab", "ticket", "file") or (parte != "dati" and slug != "tosmile"):
            return jsonify({"error": "Parte non valida"}), 400
        d = cartella(slug)
        if parte == "file":  # tutti i file allegati, impacchettati al momento
            def gen():
                class Uscita:
                    def __init__(self):
                        self.parti = []
                    def write(self, b):
                        self.parti.append(bytes(b))
                        return len(b)
                    def flush(self):
                        pass
                u = Uscita()
                with tarfile.open(fileobj=u, mode="w|gz") as tar:
                    for nome in ("file-nuovalab", "file-ticket"):
                        radice = os.path.join(d, nome)
                        if not os.path.isdir(radice):
                            continue
                        for cart, _, files in os.walk(radice):
                            for f in files:
                                p = os.path.join(cart, f)
                                tar.add(p, arcname=os.path.relpath(p, d))
                                while u.parti:
                                    yield u.parti.pop(0)
                while u.parti:
                    yield u.parti.pop(0)
            giorno = datetime.datetime.now(ROMA).strftime("%Y-%m-%d")
            return Response(gen(), mimetype="application/gzip", headers={"Content-Disposition": f'attachment; filename="backup-tosmile-file-{giorno}.tar.gz"', "Cache-Control": "no-store"})
        pref = parte + "-"
        file = sorted(x for x in os.listdir(d) if x.startswith(pref) and not x.endswith(".part"))
        if not file:
            return jsonify({"error": "Nessuna copia ancora"}), 404
        return send_file(os.path.join(d, file[-1]), as_attachment=True, download_name=f"backup-{slug}-{file[-1]}", max_age=0)

    @app.route("/api/_backup/adesso", methods=["POST"])
    def backup_adesso():
        if not _autorizzato("__"):
            return jsonify({"error": "Non autorizzato"}), 401
        solo = request.args.get("cliente")
        threading.Thread(target=notte, kwargs={"solo": _slug(solo) if solo else None}, daemon=True).start()
        return jsonify({"ok": True, "avviato": True})
