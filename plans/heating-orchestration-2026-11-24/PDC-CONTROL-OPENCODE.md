# OpenCode — completamento controllo PDC, task sequenziali

Mandato di Carlo del 25 novembre 2026: proseguire l'implementazione locale del controllo PDC attraverso piccoli task eseguiti uno dopo l'altro. Questa consegna aggiorna i precedenti incarichi limitati a M1/observe soltanto per questo ambito. Non autorizza installazione o attivazione sull'impianto, commit, push, pubblicazioni o modifiche Oikos.

## Metodo obbligatorio

Lavora in `repos/versatile_thermostat`. Leggi le istruzioni applicabili del progetto e del repository. Controlla branch, HEAD e stato Git; preserva tutto il lavoro esistente, inclusi i due script storici non tracciati. Non usare reset, clean, stash o cambi di branch. Non avviare altri agenti: un solo esecutore, un task alla volta.

Leggi `CONTRATTI.md`, `SPECIFICA.md`, `M3-ESITO.md`, `M4-M5-RUNTIME-ESITO.md`, `REVIEW-FINALE-OBSERVE.md`, `INSTALLAZIONE-OBSERVE.md` e l'inventario pertinente. Distingui stato corrente e cronologia: alcuni report descrivono codice allora non committato o precedenti divieti. Verifica il codice effettivo prima di decidere cosa manca.

Per ogni task: implementazione circoscritta, test pertinenti, revisione del diff, aggiornamento di `PDC-CONTROL-OPENCODE-ESITO.md` con file, comandi, risultati e limiti. Poi prosegui autonomamente al task successivo se i controlli passano. Se emerge una scelta di comportamento non coperta dai contratti, documentala e fermati su quel punto senza inventare una politica. Un test non eseguito non equivale a superato. Evita riformattazioni estese e aggiornamenti indiscriminati delle dipendenze.

## Stato di partenza

Il 25/11 Carlo ha installato `10.6.3+oikos.1` e attivato l'osservazione della sola caldaia con otto stanze. PDC observe non attivata nella procedura di questa chat. Il controllo reale resta alle automazioni esistenti. Nel confronto 09:57–10:16 Europe/Rome, caldaia observe e legacy concordavano su attesa, scadenza di avvio 10:07:04 e mantenimento. Non costituisce validazione del controllo PDC.

Esistono già policy, transazione, provenienza, collector, runtime, manager e test PDC. Non riscriverli da zero. Il report M4-M5 indica control incompleto e protetto da cinque barriere. Mantieni `PDC_CONTROL_AVAILABLE = False` e il rifiuto del control nella release normale per tutta la consegna. Nei test usa i punti di iniezione espliciti; non rimuovere le protezioni per farli passare. Il controllo caldaia e la relativa barriera non appartengono al lavoro.

## Task 1 — inventario delle lacune

Solo analisi: verifica le lacune del report contro il codice e i test. Produci una checklist con posizione del codice, comportamento richiesto, verifica mancante e proprietari esterni di ciascun attuatore/helper. Usa inventario e baseline già presenti; non consultare o modificare l'impianto. Distingui conferme di stato, esito delle chiamate ai servizi e attività effettiva del compressore. Consegna: una lista concreta che permetta di eseguire i task successivi senza cambiare le soglie di M3.

## Task 2 — diagnostica coerente dopo errori

Correggi R-4 del report finale: azzeramento di `last_error` dopo recupero riuscito e pubblicazione coerente degli errori del percorso PDC. Non cancellare un errore ancora attivo e non nascondere eccezioni. Testa errore, permanenza e recupero, inclusa observe senza effetti su servizi/inibizione/store.

## Task 3 — interblocco nel flusso di configurazione

Completa il cablaggio di `external_owner_entity_ids` nel parser/schema/flow e nelle traduzioni it/en, riusando il contratto esistente. Il percorso control deve richiedere una lista esplicita e conferma; un proprietario on, unknown, unavailable o non trovato impedisce il controllo. Rivalida prima di ogni effetto, non soltanto all'avvio. Conserva disabled/observe e i collegamenti caldaia. Il control deve restare non selezionabile nella release normale; testa il percorso con feature flag iniettato. Non disabilitare mai automaticamente i proprietari esterni.

## Task 4 — specchio della sessione per la caldaia

Completa lo specchio di `managed_entity_id` / `in_gestione` secondo il contratto. Verifica i momenti esatti in cui diventa on/off e il rapporto con inibizione e spegnimento confermato di split e VTherm PDC. Una chiamata riuscita non è conferma di stato. Gestisci helper non disponibile e scrittura fallita senza doppia fonte o perdita del possesso. Testa avvio, fallimento intermedio e ritorno, confermando zero scritture in observe.

## Task 5 — possesso dell'inibizione

Verifica e completa acquisizione, persistenza prima dell'effetto, conferma del possesso e rilascio. Non acquisire o rilasciare inibizioni estranee; unknown non significa false. Testa la concorrenza possibile, il fallimento della persistenza, la fase `releasing`, i retry e la riattivazione tardiva della PDC. Non alterare genericamente l'API di inibizione senza una necessità dimostrata; segnala eventuali limiti che richiedono un contratto nuovo.

## Task 6 — riavvio e ripresa di una transazione

Esegui casi di recupero in ogni fase persistita, con store mancante/corrotto/incompatibile e clock incoerente. Rileggi gli stati prima degli effetti. Verifica prenotazioni condivise fra due stanze, task vecchi, eventi duplicati e token invalidati. Nessun nuovo HEAT o rilascio può derivare soltanto dallo stato salvato. Limita tentativi e attese come da contratto e pubblica il motivo dei blocchi.

## Task 7 — manuale, override e protezioni

Completa i test e correggi i difetti comprovati per comandi utente durante ciascuna fase, provenienza dei comandi, override, finestre, potenza mancante/vecchia/eccessiva, stop programma e cambio modo centrale. Includi HEAT cloud tardivo e OFF non confermato. Preserva le differenze di configurazione fra Carlo e Diego Luisa e la politica M3 approvata. Non implementare una nuova sospensione generale o una nuova UI: usa soltanto il contratto già presente; se il collegamento mancante impedisce control, registralo come blocco esplicito.

## Task 8 — ciclo di vita delle entità PDC

Correggi R-6 e R-9 per il percorso PDC: unload fallito, reload, stanza rimossa e sensore orfano. Nessun listener, timer, task o sensore deve restare associato a un manager morto. Preserva il comportamento caldaia; se la correzione tocca codice condiviso, esegui anche le regressioni pertinenti della caldaia. Non eliminare entità dall'impianto.

## Task 9 — validazione isolata e regressioni

Esegui i test puri PDC e le regressioni observe/disabled pertinenti. Esegui i test HA già scritti del manager, dell'integrazione e del flow soltanto in un ambiente locale isolato, senza credenziali, dati live, servizi di rete o accesso all'impianto. Non avviare il server HA reale. Se un divieto corrente applicabile impedisce i test HA o l'ambiente non è utilizzabile, documenta il blocco senza superarlo e non dichiarare pronto il controllo. Distingui HA finto, HA sotto pytest e compatibilità con la versione installata; gli shim Windows non sostituiscono una prova Linux. Niente CI remota/push. Formattazione e parsing JSON solo sui file modificati. Ogni bug corretto deve avere una verifica comportamentale significativa.

## Task 10 — consegna per review, senza attivazione

Riassumi nel report: task completati/bloccati, diff, risultati riproducibili, limiti e condizioni ancora da soddisfare per aprire il control. Prepara un runbook di futura migrazione per una sola stanza, con inventario dei proprietari da spegnere, conferme, criteri di arresto e ripristino esatti. Il runbook non è autorizzazione a eseguirlo. Niente pacchetto installabile con control aperto, version bump o rimozione delle barriere. Non modificare i pacchetti observe archiviati. Termina indicando che il lavoro è locale e l'impianto non è stato modificato.

## Messaggio di avvio da usare in OpenCode

Leggi `plans/heating-orchestration-2026-11-24/PDC-CONTROL-OPENCODE.md` ed esegui i task nell'ordine indicato, uno alla volta. Inizia dal task 1, aggiorna il report dopo ogni task e prosegui soltanto se le verifiche pertinenti passano. Mantieni chiuso il controllo reale e rispetta i confini dell'incarico. Non fermarti alla proposta di un piano: completa il lavoro locale autorizzato fino alla consegna o a un blocco concreto.
