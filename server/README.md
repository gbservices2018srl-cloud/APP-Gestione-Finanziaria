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

## Come attivare l'invio email (registrazioni, reset password)

L'app può registrare nuovi clienti da sola e mandare loro un link per reimpostare
la password dimenticata — ma per farlo il server deve poter inviare email vere.
Se non configuri questo passaggio, l'app continua a funzionare lo stesso: le
richieste di reset per i clienti senza email finiscono comunque nel pannello
master, come riserva.

1. Sul tuo account Google (quello con cui vuoi mandare le email, es. quello
   dell'attività), attiva la **verifica in due passaggi** se non ce l'hai già
   (Impostazioni Google → Sicurezza → Verifica in due passaggi).
2. Vai su [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
   e genera una **password per le app**: scegli un nome a piacere (es. "To
   Smile server") e copia la password di 16 caratteri che ti mostra.
3. Su Render, vai su "Environment" del tuo servizio e aggiungi **tre** nuove
   variabili:
   - `SMTP_EMAIL` → l'indirizzo Gmail che userai per inviare (es.
     `tuonome@gmail.com`)
   - `SMTP_APP_PASSWORD` → la password per le app di 16 caratteri appena
     generata (non la tua password normale di Gmail)
   - `ADMIN_NOTIFY_EMAIL` → l'indirizzo dove vuoi ricevere l'avviso quando
     un cliente si registra (es. `consultingsardegna@gmail.com`)
4. Salva: Render riavvia il servizio da solo con le nuove variabili attive.

Da quel momento: chi si registra riceve l'email di attivazione appena lo
approvi, e chi clicca "Password dimenticata" (se ha un'email registrata)
riceve un link diretto per sceglierne una nuova, senza bisogno di te.

## Come attivare i pagamenti veri (abbonamento 29€/mese)

L'app può far scegliere al cliente, in fase di registrazione, tra piano gratuito
e piano a pagamento (29€/mese, disdicibile quando vuole). Il pagamento vero
passa da **Stripe**: carta di credito, Google Pay e Apple Pay sono già tutti
inclusi nella loro pagina di pagamento, senza bisogno di collegarli uno per uno.
Finché non completi questa configurazione, il pulsante "piano completo" mostra
un errore controllato (non rompe l'app) invitando a riprovare più tardi.

1. Crea un account su [stripe.com](https://stripe.com) (gratuito, si paga solo
   una piccola commissione per transazione quando incassi davvero).
2. Nel pannello Stripe, vai su "Product catalog" → "Add product". Crea un
   prodotto (es. "To Smile - Abbonamento mensile"), prezzo 29€, ricorrenza
   "Monthly". Salva e apri il prezzo appena creato: copia il suo **Price ID**
   (una stringa tipo `price_1AbCdEfGhIjKlMnO`).
3. Vai su "Developers" → "API keys": copia la **Secret key** (inizia con
   `sk_live_...` per i pagamenti veri, o `sk_test_...` se vuoi prima provare in
   modalità test senza soldi veri).
4. Vai su "Developers" → "Webhooks" → "Add endpoint". Come URL scrivi:
   `https://tuoindirizzo.onrender.com/api/stripe/webhook` (sostituisci con il
   tuo indirizzo vero). Come eventi da ascoltare, aggiungi almeno:
   `checkout.session.completed` e `customer.subscription.deleted`. Salva, poi
   apri l'endpoint appena creato e copia il **Signing secret** (inizia con
   `whsec_...`).
5. Su Render, vai su "Environment" e aggiungi tre variabili:
   - `STRIPE_SECRET_KEY` → la Secret key del passo 3
   - `STRIPE_PRICE_ID` → il Price ID del passo 2
   - `STRIPE_WEBHOOK_SECRET` → il Signing secret del passo 4
6. Salva: Render riavvia da solo con le nuove variabili attive.

Da questo momento: chi sceglie il piano a pagamento viene mandato sulla vera
pagina di Stripe, e appena paga il suo account si attiva **da solo, subito**,
senza bisogno che tu approvi nulla — riceve anche un'email di conferma. Se in
futuro disdice l'abbonamento (lo fa lui stesso dal link che Stripe gli manda,
o tu puoi disdirlo per lui dal pannello Stripe), il suo account torna da solo
al piano gratuito, senza perdere l'accesso.

**Consiglio**: prova prima tutto con le chiavi "test" di Stripe (quelle che
iniziano con `sk_test_` — Stripe mette a disposizione carte di prova finte per
simulare un pagamento senza soldi veri) prima di passare alle chiavi vere
`sk_live_`.

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
