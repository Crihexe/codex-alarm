# API degli eventi esterni

Versione `v1`. Il servizio è incluso nel plugin e offre HTTP su un socket Unix locale, accessibile solo all'utente. Un sistema remoto può usare SSH o un gateway autenticato senza cambiare il contratto degli eventi.

## Semantica di disponibilità

La modalità predefinita è interattiva: il controller non chiama `thread/resume` quando il thread è scaricato. La verifica pubblica disponibile è lo stato `loaded`/`notLoaded` del thread App Server, **non** la presenza certa di una finestra TUI. `thread_unload_delay_secs = 0` fa scaricare presto un thread inattivo senza sottoscrittori, ma un thread attivo resta caricato anche dopo la chiusura della TUI. La corsa fra verifica e consegna è quindi una limitazione esplicita. La ripresa autonoma senza TUI è disabilitata per impostazione predefinita e configurabile per destinatario.

L'utente può eseguire `!codex-alarm stop-chat` nella TUI prima di uscire. Il comando cancella le sveglie della chat corrente e ne sospende la consegna fino alla riapertura; non tocca altre chat. La risposta include il numero di allarmi cancellati, `in_flight` e `safe_to_exit`. L'utente attende `safe_to_exit: true` prima di uscire; se un turno già avviato da una sveglia o da un evento esterno è in corso, riceve `false` e il conteggio `in_flight`. Durante la sospensione, `require_available` e `allow_headless` rispondono `409 AGENT_UNAVAILABLE` con `reason = CHAT_SUSPENDED`, mentre `defer_until_available` conserva l'evento. La sospensione esplicita dell'utente prevale sull'opzione autonoma. Un evento già consegnato prima del comando non può essere ritirato retroattivamente.

## Pubblicare un evento

```http
POST /v1/events
Content-Type: application/json
```

```json
{
  "event_id": "bounty:scan-42:finished",
  "recipient": {"thread_id": "01a12386-3da3-77d2-a458-f5f6e1e2c248"},
  "kind": "scan.finished",
  "payload": {"message": "La scansione è terminata", "report_id": "scan-42"},
  "delivery_policy": "defer_until_available",
  "expires_at": "2027-11-11T12:00:00Z",
  "wait": {"for": "delivery_submitted", "timeout_ms": 30000}
}
```

`event_id` è scelto dal produttore ed è stabile per un singolo evento. `recipient` contiene esattamente `thread_id` oppure un `alias` registrato, risolto a una sola chat; gli eventi di una chat non vengono consegnati ad altre. `kind` identifica il tipo di evento. `payload` è dato esterno: entra nella chat come output del tool `codex_alarm.event`, mai come istruzione developer. `expires_at` è facoltativo e impedisce la consegna di eventi diventati obsoleti.

`delivery_policy` può essere:

| Valore | Quando il thread non è caricato | Se diventa non disponibile prima della consegna |
| --- | --- | --- |
| `require_available` (predefinito per chiamate esterne) | Risposta `409 AGENT_UNAVAILABLE`; l'evento non è registrato. | L'evento già accettato passa a `delivery.state = failed`, con motivo `THREAD_NOT_LOADED`; non viene ritentato automaticamente. |
| `defer_until_available` | Risposta `202` con `delivery.state = deferred`; l'evento resta pronto finché la chat torna disponibile o scade. | Resta `deferred`; il dispatcher riprova alla riapertura. |
| `allow_headless` | Il servizio può chiamare `thread/resume` e avviare il modello senza TUI. | Continua in modalità autonoma. È ammesso solo se il destinatario ha abilitato questa modalità; altrimenti `403 HEADLESS_DISABLED`. |

Gli allarmi registrati dal modello usano internamente `defer_until_available`. Una chiamata esterna può scegliere la stessa politica nel corpo della richiesta. Tutti gli eventi accettati, qualunque sia l'origine, entrano nella stessa coda persistente e nello stesso dispatcher di `toolOutput`; cambiano politica di disponibilità e metadati di origine.

`wait` è facoltativo. Con `timeout_ms = 0` il servizio risponde subito. Può attendere al massimo 30 secondi per `delivery_submitted` o `turn_terminal`; allo scadere restituisce comunque la ricevuta corrente. Un timeout di `wait` non annulla né fallisce l'evento. Il client può consultare lo stato in seguito.

## Risposta e stati

Un evento registrato risponde `202 Accepted` con una ricevuta, anche se `wait` ha osservato la consegna:

```json
{
  "event_id": "bounty:scan-42:finished",
  "delivery": {"state": "deferred", "reason": "THREAD_NOT_LOADED", "turn_id": null},
  "turn": {"state": "none", "id": null},
  "status_url": "/v1/events/bounty%3Ascan-42%3Afinished"
}
```

`GET /v1/events/{event_id}` restituisce la ricevuta aggiornata. `delivery.state` è `queued`, `deferred`, `submitted`, `failed` o `expired`; `submitted` significa che App Server ha accettato il `toolOutput`. `turn.state` è `none`, `in_progress`, `completed`, `interrupted` o `failed`, con l'ID del turno quando disponibile. Questi stati sono automatici e descrivono **se la sveglia ha raggiunto Codex e se il turno è partito o terminato**. Se l'output viene aggiunto a un turno già attivo, `in_progress` non prova che il modello abbia già letto quel preciso evento. La notifica `turn/completed` indica la fine del turno, non il successo di un'attività richiesta dall'evento.

## Errori

Formato comune:

```json
{"error":{"code":"AGENT_UNAVAILABLE","reason":"THREAD_NOT_LOADED","retryable":true}}
```

| HTTP | `code` | Significato |
| --- | --- | --- |
| `400` | `BAD_REQUEST` | Campi non validi o politica sconosciuta. |
| `403` | `HEADLESS_DISABLED` | Il destinatario non consente `allow_headless`. |
| `404` | `RECIPIENT_NOT_FOUND` | Thread o alias sconosciuto. |
| `409` | `AGENT_UNAVAILABLE` | `require_available` e thread non caricato (`THREAD_NOT_LOADED`) o sospeso dall'utente (`CHAT_SUSPENDED`). |
| `409` | `EVENT_ID_CONFLICT` | `event_id` già usato con contenuto diverso. |

Inviare di nuovo lo stesso `event_id` con contenuto identico restituisce la ricevuta esistente, senza creare una seconda sveglia. Un errore prima dell'ammissione non riserva l'ID; si può ritentare lo stesso evento quando l'agente torna disponibile. Se un evento `require_available` è stato accettato con `202` e poi fallisce per la corsa di chiusura, il client deve consultare lo stato e pubblicare un nuovo tentativo con un nuovo `event_id` se vuole ritentare. Nessuna risposta sincrona può garantire che la TUI resti aperta dopo l'ammissione.

Se il processo API non è in esecuzione, il client riceve un errore di connessione al socket, non una risposta JSON `AGENT_UNAVAILABLE`. Il comando `codex-alarm health` lo avvia se necessario e ne verifica la disponibilità.

## Socket ed endpoint di gestione

L'API ascolta su `$XDG_RUNTIME_DIR/codex-alarm/api.sock`, oppure `~/.local/state/codex-alarm/run/api.sock` se `XDG_RUNTIME_DIR` manca. Il socket è accessibile solo all'utente (`0600`); non espone una porta TCP. Per inviare un evento senza usare il client incluso:

```sh
curl --unix-socket "$XDG_RUNTIME_DIR/codex-alarm/api.sock" \
  -H 'Content-Type: application/json' -d @event.json \
  http://localhost/v1/events
```

| Metodo e percorso | Scopo |
| --- | --- |
| `GET /health` | Versione, PID e stato di supervisione del servizio. |
| `POST /v1/threads/alias` | Registra `{"thread_id":"...","alias":"..."}`; un alias appartiene a un solo thread. |
| `POST /v1/threads/headless` | Imposta `{"thread_id":"...","enabled":true|false}` per l'opt-in alla ripresa autonoma. |
| `POST /v1/threads/stop` | Sospende una chat, cancella le sveglie interne non consegnate e restituisce `safe_to_exit`. |
| `POST /v1/threads/resume` | Rimuove la sospensione manuale senza ricreare gli allarmi cancellati. |
| `GET /v1/threads/{thread_id}/alarms` | Elenca gli allarmi ancora attivi e la revisione. |
| `GET /v1/events/{event_id}` | Ricevuta corrente di un evento. |

Gli endpoint `register`, `interrupt` e `user-prompt` sono usati dagli hook del plugin. Gli endpoint MCP degli allarmi richiedono un contesto HMAC generato da `PreToolUse`; un client esterno deve usare `POST /v1/events`.
