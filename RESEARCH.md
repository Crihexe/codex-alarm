# Analisi tecnica e decisioni

Verifica eseguita con Codex CLI/App Server 0.167.0 e il sorgente `openai/codex` al commit `eb485f57ba02ca8cdedfcc07aca0f9d75c8faefd`. Il protocollo può cambiare nelle versioni future: l'adattatore App Server è isolato in `codex_alarm/appserver.py`.

## Meccanismi verificati

| Componente | Risultato | Uso nel progetto |
| --- | --- | --- |
| [App Server `turn/start` con `toolOutput`](https://learn.chatgpt.com/docs/app-server#start-a-turn) | Il daemon accetta l'evento come output di tool e restituisce l'ID del turno. Un turno già attivo può ricevere l'output. | Consegna uniforme di sveglie e integrazioni esterne, senza impersonare l'utente o usare un messaggio developer dinamico. |
| [App Server `thread/read`](https://learn.chatgpt.com/docs/app-server#read-a-thread) | Riporta `loaded`/`notLoaded` e lo stato del turno; un thread scaricato senza rollout può rispondere con errore `thread not loaded`. | Approssimazione della disponibilità e monitoraggio read-only del turno. |
| [App Server `thread/resume`](https://learn.chatgpt.com/docs/app-server#resume-a-thread) | Può ricaricare un thread senza TUI. | Usato solo per `allow_headless` dopo opt-in esplicito. |
| [Hook `PreToolUse`](https://learn.chatgpt.com/docs/hooks#pretooluse) | Può riscrivere gli argomenti MCP e riceve `session_id` e `tool_use_id`. | Aggiunge contesto firmato alla chiamata; il modello non decide l'ID della chat. |
| [Hook `Interrupt`](https://learn.chatgpt.com/docs/hooks#interrupt) e `UserPromptSubmit` | Segnalano rispettivamente un'interruzione e un nuovo prompt. | Gli eventi pronti restano in attesa dopo `Ctrl+C`/`Esc`; il prompt sblocca la consegna. |
| [Goal API](https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex) | `thread/goal/get` espone stato, obiettivo e `updatedAt`; `thread/goal/set` cambia lo stato senza ricreare il goal. | `mode: sleep` mette in pausa un goal attivo e lo riattiva al risveglio se stato e obiettivo non sono cambiati. |
| [TUI shell command](https://learn.chatgpt.com/docs/developer-commands) | Il comando `!` riceve `CODEX_THREAD_ID` della chat corrente, verificato nel sorgente TUI. | `!codex-alarm stop-chat` opera su una sola chat e non consuma un turno del modello. |

Gli hook asincroni non sono uno scheduler: consegnano output al prossimo punto utile di un turno esistente e possono essere cancellati alla chiusura. Anche `SessionEnd` non è un segnale immediato di chiusura TUI. Per questo scheduler, timer e coda vivono nel servizio locale separato dal processo MCP.

## Architettura implementata

Il plugin legacy Codex contiene la skill, quattro tool MCP, quattro hook e il servizio HTTP locale. Il formato Agent Plugin con `plugin.json` alla root carica MCP e skill nella versione locale, ma non i plugin hook: il loader Codex esclude esplicitamente gli hook da quel formato. La directory `.codex-plugin/plugin.json` indica quindi i file MCP e hook del formato legacy. Un processo App Server nuovo ha rilevato entrambi; Codex richiede poi la revisione degli hook come `untrusted` nella TUI.

SQLite conserva per ogni `thread_id` allarmi, revisione, eventi e stato di sospensione. Il checker viene eseguito in un processo separato, con timeout inferiore all'intervallo. Deve stampare un solo JSON con `state: pending|done|error`; output e dettagli sono limitati. La scadenza obbligatoria genera un evento anche se il checker continua a fallire. L'API esterna accetta `event_id` stabile e `delivery_policy`; gli eventi interni ed esterni attraversano la stessa coda e lo stesso dispatcher. Lo stato di consegna e quello del turno Codex sono distinti; il tool non raccoglie dichiarazioni dell'agente sul completamento del lavoro.

Un processo `systemd --user` supervisiona il servizio su Linux. L'hook di avvio o `codex-alarm setup` installa il comando nel `PATH`, prepara la chiave HMAC locale e configura `thread_unload_delay_secs = 0`. L'MCP può essere riavviato senza perdere gli allarmi. Senza systemd il servizio usa un processo separato, ma non riceve riavvio automatico dopo un crash. Le integrazioni esterne usano il socket Unix dell'API e non devono importare il pacchetto Python.

## Compaction e più allarmi

Il database, non la memoria del modello, è la fonte autorevole. `SessionStart` può riepilogare le sveglie dopo compaction; `list` le legge sempre dal database. L'ID della chiamata MCP rende idempotente un retry della stessa invocazione. Se esiste già un allarme attivo, una nuova chiamata `arm` fallisce con `ACTIVE_ALARMS_EXIST`; il modello deve vedere `list` e riprovare con `allow_parallel: true` e `expected_revision` uguale alla revisione corrente. Non vengono confrontati i testi dei checker: equivalenza testuale e identità del job non coincidono.

## Goal e interruzioni

In `mode: continue` l'allarme non modifica il goal. In `mode: sleep` il servizio mette in pausa il goal attivo e conserva `updatedAt` e obiettivo; al risveglio riattiva quel goal solo se tali valori sono ancora uguali e non c'è un'interruzione o una sospensione della chat. Una modifica successiva lascia il goal nello stato scelto dall'utente. L'API di lettura del goal non espone la provenienza della modifica: una modifica manuale avvenuta nella stessa unità temporale di `updatedAt` resta un limite. `/goal resume` può sbloccare un'interruzione precedente perché il dispatcher osserva che il goal è tornato `active`.

L'hook `Interrupt` trattiene gli eventi pronti dopo un cancel. Il checker e la scadenza continuano, ma l'evento non avvia un nuovo turno finché non arriva un prompt o il goal non viene ripreso. Il flag di interruzione è per chat. Il percorso del goal è verificato con test controllati dell'App Server; il comportamento visuale della TUI dopo una pausa e ripresa del goal richiede ancora una prova manuale.

## Chiusura della TUI

Il servizio non usa un proxy e non riattiva per default un thread scaricato. Il parametro [`thread_unload_delay_secs = 0`](https://learn.chatgpt.com/docs/config-file/config-reference) fa scaricare presto un thread inattivo quando la TUI si scollega. Il dispatcher controlla che sia caricato, attende un secondo e controlla di nuovo prima della consegna. Questo riduce la corsa di chiusura senza eliminarla: `thread/read` espone lo stato del thread, non il numero di TUI sottoscritte. Dopo `turn/start`, il thread resta caricato perché è attivo, anche se la TUI è stata chiusa; un controllo ritardato di `loaded` non potrebbe quindi fermare in modo affidabile il turno.

Il comando `!codex-alarm stop-chat` è la via esplicita per chiudere senza future sveglie della chat corrente. Dispatcher e comando si serializzano per chat. Gli allarmi e i loro eventi non consegnati vengono cancellati; gli eventi esterni con `defer_until_available` restano differiti; `safe_to_exit` è falso se un turno da evento è già in corso. Il comando non interrompe retroattivamente un turno già iniziato né i job osservati. La riapertura della chat rimuove la sospensione, senza ricreare allarmi cancellati.

Con unload immediato, un terminale o processo `unified_exec` ancora legato alla sessione Codex può terminare quando la TUI si chiude. Una build che deve continuare deve essere avviata come job indipendente dalla sessione, sia in locale sia via SSH. L'allarme può controllare un file di esito scritto dal job remoto con `ssh zeroforge-agent`.

## Garanzie e prove

Le ricevute distinguono evento accettato, evento consegnato ad App Server e stato del turno. Non certificano che l'agente abbia completato un compito. `turn/start` e la scrittura SQLite non formano una transazione atomica: se la risposta del daemon si perde dopo che il turno è partito, un retry può ripetere l'evento. Il payload include sempre `event_id` affinché il modello possa riconoscerlo. La consegna è quindi almeno una volta nei casi di errore incerto, non esattamente una volta.

Sono stati verificati: handshake e tool MCP; rilevamento di hook e MCP in un App Server Codex nuovo; bootstrap `systemd --user`; registrazione e consegna di un evento `toolOutput` a un thread reale; differimento su thread scaricato e consegna dopo `thread/resume`; test automatici su isolamento delle chat, idempotenza, scadenza, politiche di disponibilità e conservazione delle modifiche al goal. L'approvazione degli hook e il rendering nella TUI restano passaggi manuali dell'installazione.
