[README.md](https://github.com/user-attachments/files/31450832/README.md)
# To Smile — Gestione Finanziaria (versione server, multi-azienda)

Questa cartella contiene **tutto** quello che serve per mettere online la versione
"con licenze": un server con un vero database, un pannello master dove tu crei ed
elimini le aziende, e l'app finanziaria collegata al server invece che al browser
di ognuno.

## Cosa c'è dentro

```
server/
├── app.py              → il server (Python/Flask)
├── requirements.txt     → le librerie che servono al server
├── Procfile             → dice a Render come avviare il server
└── static/
    ├── app.html          → l'app finanziaria completa, collegata al server
    └── admin.html         → il pannello master (crea/elimina aziende)
```

**Importante**: questa è una versione *diversa* dal file HTML singolo che usi oggi
in locale. Quel file (quello che apri con doppio clic sul tuo computer, dati salvati
nel browser) resta come backup/uso offline e non serve più toccarlo. Questa nuova
versione richiede il server sempre acceso per funzionare.

## Come funziona in pratica

- Tu vai su `https://tuoindirizzo.onrender.com/admin` → crei un'azienda dandole un
  nome e una password.
- Dai al cliente l'indirizzo normale `https://tuoindirizzo.onrender.com/` (senza
  `/admin`) insieme al nome azienda e password che hai scelto.
- Il cliente entra e lavora sulla sua azienda — vede e modifica solo i propri dati.
- Tu, dal pannello `/admin`, puoi in ogni momento vedere l'elenco delle aziende,
  guardare i dati di ciascuna, rinominarle, resettare la password o eliminarle.

## Passo 1 — Metti il server online (gratis, con Render)

1. Vai su render.com e crea un account gratuito (puoi registrarti anche con
   GitHub).
2. Se non hai già un account GitHub, creane uno gratis su github.com.
3. Su GitHub, crea un nuovo repository (anche privato va bene) e carica dentro
   tutti i file di questa cartella `server/` (compresa la sottocartella `static/`
   con i suoi due file).
4. Torna su Render → "New +" → "Web Service" → collega il repository GitHub appena
   creato.
5. Nelle impostazioni del servizio:
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app` (Render la legge già da sola dal
     `Procfile`, ma verifica che sia questa)
6. Nella sezione "Environment" del servizio, aggiungi **due** variabili:
   - Nome: `SECRET_KEY` — Valore: una qualsiasi stringa lunga e a caso (es. copiala
     da un generatore di password online) — serve per proteggere gli accessi, non
     deve essere quella di esempio nel codice.
   - Nome: `DATA_DIR` — Valore: `/var/data` — dice al server dove salvare il
     database (deve corrispondere esattamente al percorso del disco al punto
     successivo).
7. Nella sezione "Disks", clicca "Add Disk" e compila così:
   - Name: un nome a piacere, es. `dati-app`
   - Mount Path: `/var/data` (identico alla variabile `DATA_DIR` sopra)
   - Size: 1 GB basta e avanza, è nel piano gratuito

   **Questo passaggio è fondamentale**: senza un disco montato su un percorso
   che il server conosce (tramite `DATA_DIR`), il database vive solo dentro al
   container e **sparisce a ogni riavvio o nuovo deploy** — è esattamente quello
   che succede se lo si salta, quindi non va saltato.
8. Premi "Create Web Service". Dopo qualche minuto avrai un indirizzo tipo
   `https://tosmile-gestione.onrender.com`.

## Passo 2 — Prima attivazione

1. Vai su `https://tuoindirizzo.onrender.com/admin`.
2. La prima volta ti chiede di impostare la password amministratore: scegline una
   e tienila al sicuro, è la chiave del pannello master. Questo passaggio si fa una
   sola volta.
3. Da qui puoi già creare la prima azienda con "+ Crea azienda".

## Passo 3 — Dai l'accesso a un cliente

1. Dal pannello master, crea un'azienda con nome e password.
2. Manda al cliente: l'indirizzo `https://tuoindirizzo.onrender.com/` (senza
   `/admin`), il nome azienda e la password.
3. Il cliente apre quel link da telefono o computer, inserisce nome e password, e
   lavora sulla sua app — identica a quella che conosci già, con tutte le pagine
   (Budget, Consuntivo, Scostamenti, Listino, Statistiche storiche, ecc.).

## Ho già un servizio online senza disco persistente: come lo aggiungo ora?

Se hai già seguito i passi sopra senza il disco (o senza `DATA_DIR`) e hai perso
dei dati, sistemalo così:

1. Vai sulla pagina del tuo servizio su Render → "Environment" → aggiungi la
   variabile `DATA_DIR` = `/var/data` (se non c'è già).
2. Vai su "Disks" → "Add Disk" → Mount Path: `/var/data`, Size: 1 GB → salva.
3. Render farà ripartire da solo il servizio. **Da questo momento in poi** i dati
   restano tra un riavvio e l'altro — quelli persi prima purtroppo non si
   recuperano, vanno ricreati.

## Domande frequenti

**Il cliente vede i dati di altre aziende?**
No. Ogni azienda, una volta loggata, può leggere e scrivere solo i propri dati —
verificato con test automatici prima di consegnartelo.

**Cosa vedo io dal pannello master?**
L'elenco di tutte le aziende, e per ciascuna un pulsante "Vedi dati" che mostra un
riepilogo (numero di sedi, voci di listino, conti correnti, ecc.) e il dettaglio
completo in formato tecnico.

**Posso eliminare un'azienda?**
Sì, dal pannello master, con conferma — l'operazione non si può annullare.

**È una vera protezione anti-copia/licenza?**
È un vero sistema di accesso con password verificate dal server (molto più solido
di quello che avevamo nel file HTML da solo). Non impedisce però, per esempio, che
un cliente condivida la propria password con altri: per una gestione delle licenze
più sofisticata (scadenze, numero massimo di utenti, ecc.) si può costruire in un
secondo momento, partendo comunque da questa base.

## Le rotte del server, per riferimento

**Amministratore**
- `POST /api/admin/setup` — imposta la password admin (solo la prima volta)
- `POST /api/admin/login` / `POST /api/admin/logout`
- `GET /api/admin/companies` — elenco aziende
- `POST /api/admin/companies` — crea azienda `{name, password}`
- `PUT /api/admin/companies/<id>/rename` — rinomina
- `PUT /api/admin/companies/<id>/password` — reset password
- `DELETE /api/admin/companies/<id>` — elimina
- `GET /api/admin/companies/<id>/data` — vedi i dati di un'azienda

**Azienda**
- `POST /api/company/login` `{name, password}` / `POST /api/company/logout`
- `GET /api/company/me` — chi sono
- `GET /api/company/data` / `PUT /api/company/data` — leggi/salva i propri dati
