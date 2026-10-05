# Piu' richieste in parallelo (thread) nello stesso processo: serve perche'
# l'inoltro verso /ticketassistenza non deve bloccare l'app finanziaria.
# Un solo processo -> la sessione/SECRET_KEY resta coerente come prima.
worker_class = "gthread"
workers = 1
threads = 8
timeout = 60
