# codex-alarm

Sveglie persistenti per Codex CLI. Un checker Python o Bash osserva una condizione locale o remota senza consumare turni del modello. Quando la condizione è soddisfatta, o arriva la scadenza di backup, Codex riceve un `toolOutput` nella chat corretta. Lo stesso servizio accetta eventi da altri programmi tramite un'API locale.

## Requisiti

Linux, Python 3.11+, Codex CLI 0.167.0 o successiva, App Server daemon locale e `~/.local/bin` nel `PATH`. Il progetto non richiede pacchetti Python esterni. `systemd --user` è consigliato per riavviare automaticamente lo scheduler.

## Installazione

```sh
codex plugin marketplace add git@github.com:Crihexe/codex-alarm.git
codex plugin add codex-alarm@crihexe-codex-alarm
```

Per sviluppare dal checkout locale, usare `codex plugin marketplace add .` dalla root della repository, poi lo stesso `codex plugin add`. Aprire una nuova TUI dopo l'installazione: Codex chiede di rivedere e approvare gli hook del plugin. Gli hook servono ad associare le chiamate MCP alla chat reale e a registrare interruzioni e riprese. Dopo l'approvazione, l'hook `SessionStart` prepara il comando nel `PATH`, la chiave locale, il servizio utente e imposta `thread_unload_delay_secs = 0` nella configurazione Codex. Il comando `codex-alarm setup` permette di ripetere il setup in seguito.

Verificare con:

```sh
codex-alarm health
systemctl --user status codex-alarm.service
```

Se `systemd --user` non è disponibile, il plugin avvia un processo separato e conserva gli allarmi in SQLite; in quel caso il riavvio automatico dopo un crash non è garantito. `codex-alarm uninstall` rimuove comando e servizio, conservando il database. La disinstallazione del plugin da Codex è separata: `codex plugin remove codex-alarm@crihexe-codex-alarm`.

## Uso nella chat

La skill `codex-alarm` insegna all'agente a usare i tool MCP `arm`, `list`, `cancel` e `status`. Per esempio, dopo aver avviato una build persistente, l'agente può registrare un checker che osserva un file di stato e una scadenza di backup. `mode: continue` permette di proseguire altro lavoro; `mode: sleep` conclude il turno e mette in pausa un goal attivo. Al risveglio il servizio riattiva quel goal solo se non è stato modificato nel frattempo.

Ogni checker stampa un JSON con `state` uguale a `pending`, `done` o `error`. Gli errori vengono registrati e ritentati; la scadenza di backup resta attiva. Per una build su `zeroforge-agent`, il checker può usare `ssh zeroforge-agent` per leggere un esito salvato sul server. La build deve sopravvivere alla disconnessione SSH e produrre un segnale durevole, come un file con exit code. Il checker viene eseguito sulla macchina che ospita Codex, non sull'host SSH.

Un allarme è sempre legato all'ID della chat. Dopo una compaction, `list` legge il database e mostra gli allarmi già attivi. Per aggiungerne un altro serve una conferma esplicita con `allow_parallel: true` e la revisione corrente fornita da `list`. Il testo del checker non viene usato per indovinare se due allarmi osservano lo stesso job.

## Chiudere la TUI

Prima di uscire da una chat per cui sono attive delle sveglie, eseguire nella TUI:

```text
!codex-alarm stop-chat
```

Il comando cancella gli allarmi della chat corrente e sospende la consegna per quella chat. `safe_to_exit: true` indica che non c'è un turno già avviato da una sveglia o da un evento esterno ancora in corso. Se restituisce `false`, il comando esce con codice 2 e mostra `in_flight`. La riapertura della chat rimuove la sospensione; gli allarmi cancellati non ricompaiono. `!codex-alarm resume-chat` la rimuove esplicitamente.

Senza `stop-chat`, la modalità interattiva non ricarica thread scaricati: l'evento resta `deferred` finché la chat torna disponibile. Lo stato `loaded` di Codex non certifica che la TUI sia ancora aperta; una chiusura esattamente durante la consegna può lasciare partire un turno in background. Il controllo prima di `turn/start` e il ritardo di unload pari a zero riducono questa finestra, ma non la eliminano. Vedi [analisi tecnica](RESEARCH.md).

`thread_unload_delay_secs = 0` può terminare processi ancora gestiti dalla sessione Codex quando l'ultima TUI si scollega. Avviare build che devono sopravvivere come job indipendenti, in locale o sul server remoto.

## Eventi da altri programmi

L'API HTTP usa il socket Unix `$XDG_RUNTIME_DIR/codex-alarm/api.sock`. Se Codex non passa `XDG_RUNTIME_DIR` al processo MCP, il plugin usa `/run/user/<uid>/codex-alarm/api.sock` quando quella directory appartiene all'utente; solo se non è disponibile ripiega su `~/.local/state/codex-alarm/run/api.sock`. Il socket ha permessi `0600`. Un programma sul server remoto può inviare l'evento tramite SSH alla macchina su cui gira Codex; non serve esporre il socket sulla rete.

```sh
codex-alarm alias my-research-agent --thread THREAD_ID
cat event.json | codex-alarm publish
codex-alarm status EVENT_ID
```

Nel file JSON si sceglie `delivery_policy`: `require_available` rifiuta subito se la chat non è caricata, `defer_until_available` conserva l'evento, `allow_headless` ricarica la chat solo dopo `codex-alarm headless on --thread THREAD_ID`. L'opzione headless è disabilitata per impostazione predefinita. L'API restituisce lo stato di consegna e del turno Codex; non richiede all'agente di dichiarare il completamento del lavoro. Contratto, errori ed esempi sono in [docs/API.md](docs/API.md).

## Sviluppo

```sh
python3 -m unittest discover -s tests -v
ruff check codex_alarm tests
```

Il core SQLite, lo scheduler, il bridge App Server e l'adattatore MCP sono nello stesso plugin. Integrazioni Telegram o bug bounty possono pubblicare eventi tramite l'API senza importare codice interno né aggiungere dipendenze a `codex-alarm`. La verifica delle API e delle scelte architetturali è documentata in [RESEARCH.md](RESEARCH.md).
