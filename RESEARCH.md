# codex-alarm: analisi di fattibilità

Stato: analisi, nessuna implementazione. Riferimento locale: Codex CLI e App Server 0.167.0. Il sorgente `openai/codex` è stato esaminato al commit `eb485f57ba02ca8cdedfcc07aca0f9d75c8faefd`; gli schemi JSON sono stati generati dalla versione installata, così da distinguere il comportamento del ramo `main` da quello disponibile localmente.

## Obiettivo

Permettere a Codex di registrare una condizione da controllare periodicamente, proseguire con lavoro indipendente oppure terminare il turno, e ricevere un evento quando la condizione si verifica. Ogni sveglia ha una scadenza obbligatoria. Se l'utente interrompe il turno, l'eventuale esito resta pronto e viene consegnato alla ripresa; se chiude la TUI, l'allarme viene sospeso senza riattivare il thread. Il checker può esaminare file, processi, build locali, host SSH, VM o servizi; l'attesa e i controlli non richiedono chiamate al modello.

## Cosa offre Codex oggi

| Meccanismo | Risultato verificato | Implicazione |
| --- | --- | --- |
| [MCP](https://learn.chatgpt.com/docs/developer-commands#codex-mcp) | Codex registra server di tool locali in `~/.codex/config.toml`. | `codex-alarm` può esporre `arm`, `status`, `list` e `cancel` come tool. |
| [Hook `PreToolUse`](https://learn.chatgpt.com/docs/hooks#pretooluse) | Riceve `session_id`, `turn_id` e argomenti del tool; può riscrivere gli argomenti di un tool MCP. | Un hook può aggiungere al tool l'ID della sessione reale. Il modello non deve scegliere né falsificare il destinatario. Per un subagente, `session_id` identifica la sessione padre: la prima versione dovrebbe limitarsi all'agente principale. |
| [`codex queue --resume`](https://learn.chatgpt.com/docs/developer-commands#codex-queue) | Accoda testo a una sessione e tenta di ricaricarla nel daemon. Il test CLI del sorgente verifica che il messaggio raggiunga il modello anche dopo un cold resume. | Fallback semplice, ma il testo appare come input utente e `queue` accetta solo testo. Inoltre richiede il daemon condiviso. |
| [App Server `turn/start` con `toolOutput`](https://learn.chatgpt.com/docs/app-server#start-a-turn) | Avvia un turno con un output di tool; se un turno normale è attivo, l'output viene aggiunto a quel turno. Il sorgente usa `start_or_steer_turn`. | È il canale preferito per l'evento della sveglia: è attribuito a `codex-alarm`, non all'utente. Se il thread è scaricato, occorre prima `thread/resume`. |
| [App Server `thread/inject_items`](https://learn.chatgpt.com/docs/app-server#inject-items-into-a-thread) | Può inserire item grezzi, inclusi messaggi `developer`, nella cronologia visibile al modello. Non avvia un turno. La conservazione dei messaggi developer immessi dal client è una feature non attiva nella versione locale. | Tecnica possibile ma fragile e con priorità eccessiva per un messaggio scritto dal modello o derivato da output esterno. L'evento dinamico deve restare output di tool. |
| [Hooks asincroni](https://learn.chatgpt.com/docs/hooks#run-hooks-in-the-background) | L'output arriva al prossimo punto sicuro del turno attivo; senza turno attivo attende il prossimo turno utente. Gli hook pendenti vengono cancellati quando la sessione finisce. | Non sono un scheduler persistente e non risvegliano una sessione inattiva. |
| [Hook `SessionEnd`](https://learn.chatgpt.com/docs/hooks#sessionend) | Non scatta immediatamente per `thread/unsubscribe`; l'App Server può tenere caricato un thread inattivo senza sottoscrittori per 60 secondi di default. | Utile per pulizia finale, insufficiente come unico segnale di chiusura della TUI. |
| [Hook `Interrupt`](https://learn.chatgpt.com/docs/hooks#interrupt) | Scatta quando si interrompe un turno attivo principale e riceve `session_id` e `turn_id`. I test della TUI confermano che `Ctrl+C` ed `Esc` pausano un goal attivo. | Permette di trattenere gli eventi dell'allarme dopo un cancel volontario, senza svegliare di nuovo il modello. |
| [Hook `Stop`](https://learn.chatgpt.com/docs/hooks#stop) | Può richiedere una continuazione immediata tramite un nuovo prompt con ruolo utente. | Utile per controlli sul termine di un turno, non per attendere un evento futuro. |
| [Goals](https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex#how-goals-are-designed-in-codex) | Un goal attivo può continuare automaticamente quando il thread è inattivo. Quando l'utente lo chiede, il modello può chiamare `update_goal({"status":"paused"})`: è verificato anche dal test `thread_resume_keeps_tool_paused_goal_paused` nel sorgente Codex. Il protocollo locale espone inoltre `thread/goal/set` con `status` e `origin`. | Il sonno può usare la pausa richiesta al modello. La ripresa automatica deve rispettare eventuali modifiche successive dell'utente. |

Il daemon locale risulta già in esecuzione alla versione 0.167.0 (`codex app-server daemon version`). Questo rende plausibile un controller che si colleghi allo stesso App Server usato dalla TUI; la consegna degli eventi in una TUI reale va ancora provata con un test di integrazione, senza usare la sessione di lavoro corrente.

## Architettura proposta

1. **Tool MCP e hook**: il modello registra la sveglia e può ispezionarla o annullarla. Il checker, l'intervallo e il messaggio di risveglio sono scelti dal modello; il tool impone un limite alla durata del singolo controllo e una scadenza di backup obbligatoria. `PreToolUse` associa ogni richiesta alla sessione Codex reale e sovrascrive qualunque ID fornito dal modello.
2. **Scheduler**: conserva le sveglie in SQLite, esegue i checker senza interrogare il modello e sopravvive a disconnessioni SSH. Ogni checker produce uno stato strutturato: `pending`, `done` o `error`. Tutte le operazioni sono partizionate per ID del thread: una chat non può vedere o modificare le sveglie di un'altra.
3. **Controller di consegna e presenza**: se il thread è caricato e non è stato interrotto dall'utente, alla condizione `done` o alla scadenza usa `turn/start` con `toolOutput` denominato `codex_alarm.event`. L'output contiene ID, motivo (`condition_met`, `deadline`, `checker_error`), messaggio scelto in fase di registrazione e un riassunto limitato del checker. Se il modello è occupato, l'evento entra nel turno in corso al successivo punto utile; se il thread è inattivo, ne avvia uno nuovo. Per garantire che la TUI sia davvero aperta servirebbe un segnale di presenza esplicito; la versione semplice può usare lo stato caricato con i limiti descritti sotto.

L'MCP server può essere riavviato da Codex e non deve possedere lo scheduler. Una sveglia deve continuare a esistere quando il processo MCP si chiude.

## Contratto preliminare del checker

La chiamata `arm` dovrebbe accettare uno script Python o Bash, directory di lavoro, intervallo, scadenza assoluta e messaggio da consegnare. Per esempio:

```json
{
  "language": "python",
  "source": "...",
  "interval_seconds": 30,
  "deadline_at": "2026-11-06T17:00:00Z",
  "wake_message": "La compilazione dovrebbe essere terminata; verifica l'esito.",
  "mode": "continue"
}
```

`mode` distingue `continue` (l'agente fa altro mentre l'allarme corre) da `sleep` (conclude il turno e attende l'evento). Il checker scrive un solo JSON del tipo `{"state":"pending"}`, `{"state":"done","detail":"..."}` o `{"state":"error","detail":"..."}`. Un exit code zero senza JSON valido non significa `done`. Il singolo controllo ha un timeout più breve dell'intervallo; lo scheduler non sovrappone due esecuzioni dello stesso checker. Un limite minimo all'intervallo evita controlli troppo frequenti.

Gli output grandi vengono troncati nel messaggio e conservati separatamente. Errori di SSH o del checker restano distinti da `pending`: possono causare retry, ma non cancellano il timer di backup. La scadenza è verificata dal controller anche se il checker è rotto o bloccato. Allo scadere si genera `deadline`, con l'ultimo errore disponibile; il modello può decidere se registrare un nuovo allarme.

Per gli host remoti, un checker locale può eseguire `ssh zeroforge-agent ...`. L'eventuale processo osservato deve però sopravvivere alla disconnessione SSH e scrivere un esito recuperabile. `codex-alarm` controlla condizioni; un futuro helper per avviare job persistenti potrebbe semplificare le build, senza rendere l'allarme specifico per esse.

## Sonno, goal e concorrenza

- **Lavoro parallelo**: `arm` restituisce subito un ID e Codex continua. Il controller consegna l'evento quando pronto.
- **Attesa senza goal**: Codex registra l'allarme e conclude il turno. L'evento successivo avvia un nuovo turno con output di tool.
- **Attesa con goal attivo**: il goal continuerebbe automaticamente, quindi va pausato. Il modello può chiamare `update_goal({"status":"paused"})` quando il flusso di lavoro richiesto dall'utente lo autorizza. In alternativa il tool può gestire la pausa come parte esplicita della modalità `sleep`. Se non interviene un cancel dell'utente, `thread/goal/set` con obiettivo omesso e stato `active` riprende il goal esistente senza ricrearlo: se l'obiettivo è cambiato, riprende quello aggiornato. Dopo `Ctrl+C`, `Esc`, `/goal pause` o `/goal clear` volontari l'allarme non deve riattivarlo. `blocked` ha un significato diverso e non dovrebbe rappresentare una build che procede normalmente.

La TUI invia i comandi manuali `/goal` all'App Server con `origin: "user"`, ma `thread/goal/get` e `thread/goal/updated` espongono lo stato senza tale provenienza. Il solo `updatedAt` non permette di distinguere in modo affidabile una pausa manuale da un'altra modifica. Un adattatore trasparente fra TUI e daemon può osservare le richieste `/goal` riuscite: `/goal pause` e `/goal clear` pongono un veto alla ripresa automatica; una modifica del solo obiettivo può lasciare valida la ripresa dello stesso goal aggiornato. Se invece l'utente scrive un prompt che induce il modello a chiamare `update_goal`, la provenienza non è altrettanto esplicita: un aggiornamento inatteso a `paused` deve essere trattato conservativamente come veto. Le modifiche fatte da client non osservati non possono essere classificate con certezza tramite l'API pubblica. Questa parte va verificata in integrazione.

### Interruzione del turno

Un cancel con `Ctrl+C` o `Esc` durante un turno attivo può essere registrato dall'hook `Interrupt`, indipendentemente dalla provenienza esposta dal goal. Lo scheduler marca **solo gli allarmi di quel thread** come `held_after_interrupt`. Può continuare a eseguire il checker e, se la condizione o la scadenza arriva, memorizza l'evento `ready` senza chiamare il modello. Quando l'utente invia un nuovo prompt o riprende il goal, il controller rimuove il blocco e consegna l'evento pronto come `toolOutput` nel turno ripreso. Se il checker è ancora `pending`, l'allarme continua normalmente. Aprire semplicemente la chat non conta come ripresa del lavoro.

L'hook `Interrupt` non scatta per thread già inattivi o subagenti. Per le interruzioni della TUI principale è il segnale diretto; l'adattatore della TUI può anche osservare `turn/interrupt` e la successiva ripresa. Non serve chiedere al modello di indovinare dal solo stato del goal perché sia in pausa.

Una sveglia può scattare mentre un turno termina, un'altra sveglia scatta o il daemon riparte. Il database deve conservare l'evento fino alla consegna; l'evento porta un ID stabile e il controller ritenta. Senza una chiave di idempotenza per `toolOutput` nel protocollo, è più onesto promettere consegna almeno una volta e rendere innocui gli eventuali duplicati, non promettere esattamente una volta.

Il controller dovrebbe limitare il proprio numero di consegne simultanee per thread. Un evento consegnato include un ID stabile, così Codex può riconoscere un duplicato. Il daemon non deve perdere un evento se `thread/resume` o `turn/start` fallisce: lo conserva e ritenta. Se l'utente archivia o elimina il thread, l'allarme va fermato o marcato come non consegnabile, senza crearne uno nuovo.

## Compaction, memoria e allarmi multipli

La compaction può eliminare dal contesto del modello la chiamata che ha creato l'allarme. Il database dello scheduler resta la fonte autorevole: la compaction non cancella, ricrea né modifica sveglie. L'evento di risveglio include ID, scopo, scadenza e istruzione di ripresa, così è comprensibile anche senza la cronologia precedente.

Un [hook `SessionStart`](https://learn.chatgpt.com/docs/hooks#sessionstart) riceve `source: "compact"` e può aggiungere al nuovo contesto developer un riepilogo breve degli allarmi attivi della sessione. Serve come aiuto al modello, mentre il blocco dei duplicati deve stare nel tool e funzionare anche se l'hook fallisce.

Non è corretto confrontare i testi dei checker per stabilire equivalenza: script quasi identici possono controllare job diversi; script diversi possono controllare lo stesso job. Nemmeno una chiave del job inventata dal modello garantisce che venga riutilizzata dopo una compaction. Il protocollo dovrebbe usare:

1. Un `alarm_id` immutabile assegnato dal tool, mostrato da `list` e `status`.
2. Un ID della chiamata del tool, aggiunto dall'hook, per rendere idempotenti i retry della stessa invocazione.
3. Ogni `arm` interroga **solo** gli allarmi attivi del thread chiamante. Se ce ne sono già, il tool presenta l'elenco e non crea nulla finché il modello non richiama `arm` con la revisione dello stato appena vista e `allow_parallel: true`. Per modificare una sveglia esistente si usa `update(alarm_id)`. La revisione evita corse fra controllo e creazione.

Il riepilogo al modello e la protezione del tool sono complementari: la prima riduce errori di pianificazione, la seconda impedisce creazioni inconsapevoli. Non c'è un limite artificiale di una sola sveglia per sessione.

## Chiusura della TUI

L'uscita dalla CLI dovrebbe sospendere gli allarmi della chat e impedire nuovi turni automatici. Gli eventuali job esterni già avviati non vengono terminati da `codex-alarm`. Lo stato dell'allarme resta leggibile al ritorno nella chat, ma la consegna richiede una ripresa esplicita dell'utente. La scadenza di backup resta registrata; se passa durante la chiusura, non riapre la chat.

### Opzione semplice senza proxy

Il controller non chiama mai `thread/resume` per consegnare una sveglia. Controlla `thread/read`: se lo stato è `notLoaded`, conserva l'evento come pronto e lo consegna solo dopo una ripresa esplicita dell'utente; se il thread è caricato e non è bloccato da `Interrupt`, usa `turn/start` con `toolOutput`. Il parametro documentato [`thread_unload_delay_secs`](https://learn.chatgpt.com/docs/config-file/config-reference) può essere impostato a `0`, così un thread inattivo senza sottoscrittori viene scaricato appena possibile invece che dopo i 60 secondi predefiniti. I test della TUI nella versione locale usano questa configurazione.

Questa opzione è piccola e probabilmente sufficiente per una prima versione, ma **non offre una garanzia assoluta**: il thread può essere ancora caricato mentre la TUI si sta chiudendo, e `thread/read` seguito da `turn/start` non è un'operazione atomica. Inoltre, passando a un'altra chat nella stessa TUI, il thread precedente può essere scaricato; la sua sveglia non avvierebbe automaticamente un turno finché non ci si torna. Il controller può conservare l'esito senza perderlo. Va misurato l'effetto dell'unload immediato sulla normale esperienza CLI prima di impostarlo globalmente.

### Opzione rigorosa con presenza della TUI

`SessionEnd` da solo non garantisce un segnale immediato. Il Codex TUI installato usa `thread/unsubscribe` durante l'uscita, e la [documentazione App Server](https://learn.chatgpt.com/docs/app-server#unsubscribe-from-a-loaded-thread) prevede un ritardo prima del successivo `thread/closed`. Per conoscere subito la chiusura della TUI senza modificare Codex, la proposta è avviarla tramite un piccolo proxy locale verso il daemon usando `codex --remote unix://...`, opzione [documentata da OpenAI](https://learn.chatgpt.com/docs/app-server#connect-the-cli-terminal-ui). Il proxy inoltra il protocollo senza cambiarlo e osserva `thread/start`, `thread/resume`, `thread/unsubscribe`, chiusura della connessione e richieste `/goal` con `origin: "user"`. La chiusura della connessione TUI sospende le sveglie dei thread associati a quella istanza, salvo che un'altra TUI integrata li stia ancora usando. Il semplice cambio di chat nella stessa TUI non equivale a chiudere Codex e non sospende gli allarmi delle altre chat.

Il [trasporto Unix dell'App Server](https://learn.chatgpt.com/docs/app-server#protocol) usa WebSocket sopra il socket locale, con handshake HTTP Upgrade e messaggi JSON-RPC nei frame. Quindi il proxy non può leggere righe JSONL sul socket. L'implementazione preferibile inoltra i byte senza modificarli e osserva una copia dei frame; il parser di osservazione non deve bloccare il traffico della TUI. Se non riesce a identificare con certezza il thread o perde lo stato della connessione, l'auto-risveglio va sospeso anziché assumere che la TUI sia ancora aperta.

Senza rilevare la chiusura, un controller che usa `thread/resume` e `turn/start` **può riattivare il modello anche senza il processo TUI**: il daemon e i thread persistenti sono separati dalla finestra terminale. Rinunciare a `thread/resume` non dà la stessa garanzia: un thread può rimanere caricato per il ritardo di unload e ricevere un evento in quella finestra. Il proxy serve quindi per una garanzia precisa di non continuazione dopo l'uscita; se la prosecuzione headless fosse desiderata, potrebbe essere una modalità esplicita separata.

Senza questo adattatore, il protocollo pubblico non espone un conteggio immediato dei client collegati al thread. La soluzione semplice sopra resta utilizzabile con i limiti dichiarati; il proxy aggiungerebbe una garanzia più forte e manterrebbe il risveglio delle chat non visualizzate mentre la TUI resta aperta.

### Costi e compatibilità del proxy

- Il wrapper deve avviare la TUI attraverso il socket del proxy; una TUI avviata direttamente non fornisce il segnale di presenza. Si può offrire un alias shell, lasciando intatti i sottocomandi non interattivi.
- Codex può cambiare metodi App Server, struttura dei messaggi o negoziazione WebSocket. Il proxy deve controllare la versione e avere test di compatibilità; quando l'osservazione non è affidabile, deve inoltrare il traffico ma fermare l'auto-risveglio.
- Tutto l'output passa attraverso il proxy. Il percorso di inoltro deve avere backpressure e non accumulare log di build in memoria; l'osservatore può scartare payload grandi che non contengono metadati necessari.
- Caduta del proxy e riavvio del daemon richiedono riconnessione o una chiusura ordinata della TUI. Un proxy per ogni istanza TUI deve registrare correttamente più chat e più istanze aperte sullo stesso thread.
- Il proxy vede prompt e risultati di tool. Va tenuto su socket Unix locale con permessi restrittivi, registrando solo metadati di presenza e stato, mai il contenuto delle conversazioni.

Un'altra alternativa è avviare un App Server separato per ogni TUI e fermarlo quando la TUI esce. La chiusura diventerebbe facile da rilevare, ma ci sarebbero più server che condividono sessioni, database e configurazione Codex; il daemon condiviso attuale evita questa complessità. Il proxy non è una dipendenza obbligatoria della prima versione: va introdotto solo se la soluzione semplice non soddisfa i requisiti verificati con la TUI reale.

## Ruolo dei messaggi

Il modello non deve impersonare l'utente. `codex queue` lo farebbe apparire come input utente ed è quindi solo un fallback. Un messaggio `developer` è tecnicamente iniettabile, ma non è necessario per il risultato desiderato e darebbe priorità alta a testo che il modello stesso o un checker può aver scritto. Un output di tool con provenienza chiara rappresenta meglio la sveglia. Eventuali istruzioni **fisse** su come usare gli allarmi possono invece stare nella descrizione dei tool o in un hook `SessionStart` come contesto developer.

## Prove necessarie prima dell'implementazione completa

1. Registrare un allarme di pochi secondi in una sessione TUI di prova, lasciare il turno inattivo e verificare che `turn/start` con `toolOutput` lo riattivi e mostri l'evento nella TUI.
2. Ripetere con turno attivo, TUI chiusa, daemon riavviato, due allarmi quasi simultanei e checker SSH che fallisce temporaneamente. Verificare che un allarme non riapra una chat dopo la chiusura, anche se scatta entro il ritardo di unload.
3. Verificare che `PreToolUse` consegni l'ID giusto anche con più sessioni nella stessa directory, e che non attribuisca a un subagente la sessione sbagliata.
4. Testare separatamente il sonno con goal: nessuna continuazione prima della sveglia; ripresa dello stesso goal se cambia solo l'obiettivo; nessuna ripresa dopo `/goal pause` o `/goal clear` manuale.
5. Interrompere con `Ctrl+C` ed `Esc` prima e dopo che il checker diventa `done`; verificare che nessun turno parta da solo e che l'evento pronto sia consegnato al primo prompt o `/goal resume` successivo.
6. Forzare una compaction subito dopo `arm`, verificare che il contesto nuovo riceva l'elenco degli allarmi e che un secondo `arm` non crei un duplicato senza una decisione esplicita. Ripetere con due allarmi distinti, per verificare che `allow_parallel` funzioni.

## Decisioni ancora aperte

- Far partire la prima versione con soli checker scritti dal modello, oppure includere anche un piccolo helper per processi persistenti.
- Stabilire come presentare nella TUI una sveglia sospesa dopo la riapertura: `SessionStart` può aggiungere contesto al prossimo turno, mentre un comando CLI `codex-alarm list` è disponibile subito.
- Abilitare subito la pausa automatica dei goal, o introdurla dopo aver verificato il risveglio base.

## Esperienza utente: senza proxy e con proxy

| Situazione | Prima versione senza proxy | Eventuale versione con proxy |
| --- | --- | --- |
| Avvio di Codex | Comando `codex` abituale. | Avvio tramite wrapper o alias che inserisce il proxy. |
| Sveglia mentre si lavora nella stessa chat | Evento `toolOutput` durante il turno, oppure nuovo turno se la chat è inattiva ma caricata. | Stesso risultato. |
| Passaggio a un'altra chat | La chat precedente può essere scaricata; l'esito viene conservato fino al suo ritorno. | Il proxy sa che la TUI è ancora aperta e può consentire un risveglio in background della chat precedente. |
| Chiusura della CLI | Non viene ricaricato un thread scaricato; con `thread_unload_delay_secs = 0` resta una piccola corsa fra chiusura e consegna. | La disconnessione della TUI sospende subito gli allarmi associati. |
| Impatto operativo | Nessun intermediario sul traffico Codex. | Tutto il traffico passa attraverso un processo aggiuntivo che può fallire o richiedere aggiornamenti. |

La soluzione senza proxy usa API pubbliche e rimane semplice da distribuire; non è una scorciatoia strutturalmente sbagliata. L'unico punto debole è usare `loaded` come approssimazione di “utente ancora nella chat”, perché l'App Server non espone una condizione atomica “avvia questo turno solo se un client TUI è ancora collegato”. La scelta va presentata come una politica esplicita, non come rilevamento esatto della chiusura.

### Verifica della chiusura e ritardo di unload

Un controllo **dopo** `turn/start` non risolve la corsa di chiusura: dal codice App Server verificato, un thread attivo non viene scaricato anche quando non ha più sottoscrittori. Se la TUI esce appena dopo la consegna, `thread/read` continuerà quindi a restituire `active` durante il lavoro del modello. Il protocollo pubblico non espone il numero di sottoscrittori, e nessuna attesa di alcuni secondi permette di dedurlo da `loaded`. L'eventuale `thread/closed` arriva soltanto dopo che il thread torna inattivo.

Con `thread_unload_delay_secs = 0`, un thread senza sottoscrittori viene scaricato appena è inattivo. Prima di consegnare si può attendere una breve finestra di stabilizzazione e rileggere lo stato, ma la chiusura può sempre avvenire tra l'ultima lettura e `turn/start`. La finestra riduce la probabilità della corsa senza fornire una garanzia. In assenza di proxy, il contratto deve dichiarare questa limitazione; non deve attribuire il significato “TUI certamente aperta” allo stato `loaded`. La precedente formulazione “rileviamo quando la CLI si chiude” era imprecisa: rileviamo lo **scaricamento del thread inattivo**, non la disconnessione immediata della TUI.

Esempio della corsa: alle 10:00:00.000 il controller vede il thread caricato; alle 10:00:00.010 invia `turn/start`; alle 10:00:00.020 la TUI si chiude. Alle 10:01:00 il thread può essere ancora `active` perché il modello sta lavorando: `thread/read`, `thread/loaded/list` e `SessionEnd` non attestano la disconnessione. Se il turno finisce prima del controllo, il thread viene scaricato ma è troppo tardi per interrompere quel turno. `turn/interrupt` esiste, ma manca il segnale affidabile che dica quando usarlo. Il ritardo zero non cambia questo vincolo.

La decisione per la prima versione è `thread_unload_delay_secs = 0`. Quando il thread si scarica, App Server chiama `shutdown_and_wait`; lo shutdown della sessione interrompe i processi `unified_exec` ancora gestiti da quel thread e gli hook asincroni. Le build remote avviate in modo indipendente dalla connessione SSH non dipendono da quei processi, ma una build locale o una sessione SSH rimasta come terminale Codex potrebbe essere terminata alla chiusura della TUI. Il valore può avere effetto su tutte le chat che usano la stessa configurazione/daemon; documentare e testare questo comportamento durante l'installazione.

Questo tocca direttamente il flusso delle build: se il modello avvia una compilazione come terminale `exec` ancora in esecuzione e poi conclude il turno in attesa della sveglia, la chiusura della TUI con unload immediato può terminare quel terminale. Per garantire che la compilazione sopravviva, il job deve essere avviato in modo indipendente dalla sessione Codex, sia in locale sia sull'host SSH. Un piccolo helper opzionale per job persistenti potrebbe quindi diventare parte utile della prima versione, pur mantenendo i checker generici.

### Arresto manuale dalla TUI

La TUI supporta comandi shell prefissati con `!`. Nella versione Codex esaminata, il comando eseguito in questo modo riceve `CODEX_THREAD_ID` della chat corrente: il client può quindi offrire `!codex-alarm stop-chat`, senza chiedere al modello di scegliere un destinatario o consumare token. L'installazione del plugin deve rendere disponibile il piccolo eseguibile `codex-alarm` sul `PATH` (oppure offrire un percorso stabile di fallback) e verificarlo in una TUI reale. Se `CODEX_THREAD_ID` manca, il comando deve fallire senza operare su altre chat.

`stop-chat` è sincrono: contrassegna la chat come sospesa dall'utente, cancella tutte le sveglie registrate per quel thread e i loro esiti non ancora consegnati, poi restituisce un riepilogo e solo allora termina. Il dispatcher deve serializzare consegna e arresto per thread, così dopo la risposta di successo non partono nuove consegne per quella chat. Mentre è sospesa, una pubblicazione esterna `require_available` o `allow_headless` riceve `AGENT_UNAVAILABLE` con motivo `CHAT_SUSPENDED`; una pubblicazione `defer_until_available` resta in attesa. La sospensione viene rimossa alla successiva apertura della chat, tramite `SessionStart` di ripresa (non quello di compaction), oppure con un comando esplicito `resume-chat`; le sveglie cancellate non tornano attive.

Se una sveglia ha già avviato un turno prima di `stop-chat`, la cancellazione non può annullare il lavoro già svolto. La risposta deve indicare `in_flight` e `safe_to_exit`; quest'ultimo è `true` solo quando non restano consegne future né turni di sveglia in corso. Il controller può interrompere un turno solo se riconosce con certezza che è stato avviato dalla sveglia, senza interrompere un turno ordinario dell'utente; se non può farlo, risponde `safe_to_exit: false`. Il comportamento preciso va provato. Questa funzione ferma le sveglie e la consegna degli eventi, non le build avviate come job indipendenti.

La [documentazione delle slash command](https://learn.chatgpt.com/docs/reference/slash-commands) descrive skill nel menu `/` e prompt personalizzati `/prompts:<nome>`, ma questi avviano un turno del modello; i prompt personalizzati sono inoltre deprecati. Non risulta un'interfaccia stabile per far aggiungere a un plugin un comando `/stop-alarms` nativo, immediato e senza modello nella TUI corrente. Una skill può essere offerta come scorciatoia opzionale, ma l'arresto affidabile usa `!codex-alarm stop-chat`.

## Plugin Codex e servizio riutilizzabile

Il pacchetto può contenere `plugin.json`, `mcp.json`, una skill che spiega il flusso degli allarmi e gli hook `PreToolUse`, `SessionStart`, `Interrupt` e, se utile, `SessionEnd`. La [documentazione plugin](https://developers.openai.com/plugins/build/plugins#plugin-structure) supporta questi componenti; gli hook del plugin richiedono revisione e fiducia e, nelle regole correnti, un plugin con hook non può essere pubblicato nella directory pubblica. Una distribuzione locale o tramite marketplace Git va verificata sul Codex CLI installato.

Il servizio persistente non deve dipendere dalla durata della connessione MCP. Può essere distribuito **dentro lo stesso plugin**: il primo uso avvia o verifica il processo locale e l'eventuale servizio utente, senza imporre un secondo pacchetto o un'installazione manuale dell'API. Il bootstrap resta un'operazione tecnica necessaria, da testare con installazione pulita, aggiornamento e disinstallazione. La prima versione dovrebbe dichiarare Linux come ambiente supportato.

Per mantenere il progetto integrabile senza modificare il nucleo, separare:

1. **Core**: registro SQLite, scheduler, checker, timer di backup, coda di eventi, deduplicazione tramite `event_id`, stato delle consegne e API locale versionata. Non contiene codice Telegram né bug bounty.
2. **Adattatore Codex**: plugin MCP, hook e client App Server per collegare `thread_id` agli allarmi e consegnare `toolOutput`.
3. **Adattatori esterni**: pacchetti indipendenti che traducono eventi di altri programmi in chiamate all'API locale. Possono vivere in altri repository.

La stessa API deve accettare un evento esterno diretto a un thread o a un alias registrato, per esempio `publish({event_id, recipient, kind, payload})`. `event_id` rende idempotente la pubblicazione. Il destinatario può registrare una sottoscrizione a un topic tramite tool MCP; un sistema bug bounty pubblica l'evento sul topic senza conoscere i dettagli di Codex. L'evento entra nella stessa coda di una sveglia prodotta da un checker e segue le stesse regole di consegna. I payload esterni restano output di tool, non istruzioni developer.

Il percorso di consegna interattivo è predefinito; una ripresa autonoma è configurabile separatamente:

- **Interattiva** (predefinita): consegna immediata a thread caricati; gli allarmi già registrati conservano l'esito se il thread è scaricato o tenuto in pausa da `Interrupt`. Non riapre Codex quando l'utente chiude la CLI.
- **Autonoma** (abilitazione esplicita per agente o sottoscrizione): il controller può usare `thread/resume` anche senza TUI. È adatta ad agenti bug bounty che devono lavorare in background, e rende intenzionale il costo di un risveglio headless.

### Contratto locale per eventi esterni

Il contratto proposto, con esempi e stati distinti di consegna e lavoro dell'agente, è in [API.md](docs/API.md). `delivery_policy` è esattamente la scelta richiesta per la chiamata esterna: `require_available` (predefinita, senza attesa), `defer_until_available` (conserva l'evento fino alla riapertura) o `allow_headless` (solo se abilitata esplicitamente per il destinatario). Gli eventi generati dagli allarmi usano la stessa coda e il medesimo dispatcher con la politica di attesa.

Un futuro adattatore Telegram potrebbe pubblicare i messaggi in ingresso sull'API e usare la skill esistente per quelli in uscita. Il bridge Telegram non è una dipendenza del core né del plugin Codex; l'invio e la ricezione seguono le autorizzazioni scelte dall'utente per il singolo task. Per eventi provenienti da un host remoto si può aggiungere un gateway autenticato o usare SSH verso l'API locale, senza modificare scheduler e adattatore Codex.
