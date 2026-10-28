"""Lettore del bilancio a 4 sezioni (situazione contabile) in PDF: conti del conto economico e dello stato patrimoniale con il loro saldo."""
import re, io
from datetime import date

RE_COD = r"(?:\d{2}/(?:\d{2}|\*\*)/(?:\d{3}|\*\*\*)|\d{2}/\d{5})"
RE_VOCE = re.compile(r"(?P<cod>" + RE_COD + r")\s+(?P<descr>\S.*?)\s{2,}(?P<val>-?[\d.]+,\d{2}-?)(?=\s{2,}|\s*$)")
RE_TOT = re.compile(r"(TOTALE\s+ATTIVIT\S*|TOTALE\s+PASSIVIT\S*|TOTALE\s+COSTI|TOTALE\s+RICAVI|UTILE\s+DI\s+ESERCIZIO|PERDITA\s+DI\s+ESERCIZIO|UTILE\s+D'ESERCIZIO|PERDITA\s+D'ESERCIZIO)\s+(-?[\d.]+,\d{2})")


def numero(s):
    s = s.strip()
    neg = s.endswith("-") or s.startswith("-")
    v = float(s.strip("-").replace(".", "").replace(",", "."))
    return -v if neg else v


def leggi_testo_pagine(pdf_bytes):
    import pdfplumber
    out = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for p in pdf.pages:
            out.append(p.extract_text(layout=True, x_density=4, y_density=13) or "")
    return out


def leggi_bilancio(pdf_bytes=None, pagine=None):
    pagine = pagine if pagine is not None else leggi_testo_pagine(pdf_bytes)
    voci, gruppi, totali = {}, {}, {}
    ditta, al, dal = "", None, None
    for testo in pagine:
        su = testo.upper()
        if not ditta:
            m = re.search(r"Ditta\s+(.+?)\s{3,}", testo)
            if m: ditta = re.sub(r"\s+", " ", m.group(1)).strip()
        economica = "SITUAZIONE" in su and "ECONOMICA" in su and "RIDETERMINAZIONE" not in su
        patrimoniale = "SITUAZIONE" in su and "PATRIMONIALE" in su and not economica and "RIDETERMINAZIONE" not in su
        if not al:
            m = re.search(r"\bAL\s+(\d{2})/(\d{2})/(\d{4})", su)
            if m: al = date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
            m = re.search(r"\bDAL\s+(\d{2})/(\d{2})/(\d{4})", su)
            if m: dal = date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        if not economica and not patrimoniale:
            continue
        righe = testo.splitlines()
        # dove inizia la colonna dei ricavi: la seconda intestazione "CONTO" (o "RICAVI E PROFITTI")
        soglia = None
        for r in righe:
            ru = r.upper()
            if "DESCRIZIONE CONTO" in ru and ru.count("CONTO") >= 4:
                idx = [m.start() for m in re.finditer(r"\bCONTO\b", ru)]
                if len(idx) >= 3: soglia = idx[2] - 6; break
        if soglia is None:
            for r in righe:
                i = r.upper().find("RICAVI E PROFITTI")
                if i > 0: soglia = i - 25; break
        if soglia is None:
            soglia = max((len(r) for r in righe), default=160) // 2 - 10
        for r in righe:
            for m in RE_TOT.finditer(r.upper()):
                k = re.sub(r"\s+", " ", m.group(1)).replace("D'", "DI ")
                k = "TOTALE ATTIVITA" if k.startswith("TOTALE ATTIVIT") else "TOTALE PASSIVITA" if k.startswith("TOTALE PASSIVIT") else k
                if patrimoniale and k.startswith("UTILE") or patrimoniale and k.startswith("PERDITA"):
                    k = "SP " + k
                totali[k] = numero(m.group(2))
            for m in RE_VOCE.finditer(r):
                cod, descr, val = m.group("cod"), re.sub(r"\s+", " ", m.group("descr")).strip(), numero(m.group("val"))
                destra = m.start() >= soglia
                lato = ("passivo" if destra else "attivo") if patrimoniale else ("ricavo" if destra else "costo")
                if "*" in cod:
                    gruppi[(cod, lato)] = descr
                    continue
                voci[(cod, lato)] = {"codice": cod, "descr": descr, "lato": lato, "saldo": round(val, 2)}
    # gruppo (mastro) di ogni voce: "68/**/***" → COSTI PER SERVIZI; conto "68/05/***" → sottogruppo
    lista, sp = [], []
    for (cod, lato), v in voci.items():
        if lato in ("attivo", "passivo"):
            v["mastro"] = gruppi.get((cod[:2] + "/**/***", lato), "")
            v["conto"] = gruppi.get((cod[:5] + "/***", lato), "") if len(cod) == 9 else ""
            v["id"] = ("A:" if lato == "attivo" else "P:") + cod
            sp.append(v)
            continue
        mastro = cod[:2]
        v["mastro"] = gruppi.get((mastro + "/**/***", lato), "")
        v["conto"] = gruppi.get((cod[:5] + "/***", lato), "") if len(cod) == 9 else ""
        v["id"] = cod + ("R" if lato == "ricavo" and any(k == cod and l == "costo" for (k, l) in voci) else "")
        lista.append(v)
    lista.sort(key=lambda v: (v["lato"] != "ricavo", v["codice"]))
    ric = round(sum(v["saldo"] for v in lista if v["lato"] == "ricavo"), 2)
    cos = round(sum(v["saldo"] for v in lista if v["lato"] == "costo"), 2)
    utile_doc = totali.get("UTILE DI ESERCIZIO")
    if utile_doc is None and "PERDITA DI ESERCIZIO" in totali: utile_doc = -totali["PERDITA DI ESERCIZIO"]
    controllo = {"ricavi": ric, "costi": cos, "utile": round(ric - cos, 2),
                 "ricaviDoc": totali.get("TOTALE RICAVI"), "costiDoc": totali.get("TOTALE COSTI"), "utileDoc": utile_doc}
    controllo["ok"] = all(controllo[a] is None or abs(controllo[a] - controllo[b]) < 1 for a, b in
                          (("ricaviDoc", "ricavi"), ("costiDoc", "costi"), ("utileDoc", "utile"))) and bool(lista)
    if not dal and al: dal = al[:4] + "-01-01"
    # stato patrimoniale: attivo = passivo (fondi compresi) + utile
    sp.sort(key=lambda v: (v["lato"] != "attivo", v["codice"]))
    att = round(sum(v["saldo"] for v in sp if v["lato"] == "attivo"), 2)
    pas = round(sum(v["saldo"] for v in sp if v["lato"] == "passivo"), 2)
    controllo_sp = {"attivo": att, "passivo": pas, "attivoDoc": totali.get("TOTALE ATTIVITA"), "passivoDoc": totali.get("TOTALE PASSIVITA")}
    controllo_sp["ok"] = bool(sp) and all(controllo_sp[a] is None or abs(controllo_sp[a] - controllo_sp[b]) < 1 for a, b in
                                          (("attivoDoc", "attivo"), ("passivoDoc", "passivo"))) and abs(att - pas - controllo["utile"]) < 1
    return {"ditta": ditta, "dal": dal, "al": al, "voci": lista, "controllo": controllo, "patrimoniale": sp, "controlloSP": controllo_sp}


