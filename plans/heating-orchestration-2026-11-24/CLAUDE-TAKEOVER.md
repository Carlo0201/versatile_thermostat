# Claude — presa in carico completa del progetto riscaldamento

## Mandato corrente di Carlo

Carlo sta usando Opus 5.5 e trasferisce a Claude **tutto il lavoro residuo**, compresi coordinamento, specifiche di dettaglio, implementazione, review, test, card Oikos, preparazione della distribuzione e attività Home Assistant consentite. Codex non è più un gate di review o un coordinatore disponibile. Sono autorizzati task più ampi e lavoro parallelo quando utile.

Questo documento sostituisce i precedenti vincoli organizzativi “Claude solo codice”, “attendi la review di Codex”, “non iniziare M2-B/M3…” e le assegnazioni a Codex presenti in README, handoff e incarichi precedenti. **Non sostituisce le politiche di riscaldamento approvate, le istruzioni del progetto né i permessi degli strumenti.** Usa i vecchi incarichi come specifiche funzionali e storico, non come motivo per interrompere il lavoro tra un lotto e l'altro.

Porta avanti il progetto autonomamente: analizza, implementa, fai verificare, correggi e prosegui. Non fermarti dopo il primo piano o dopo M2-A per chiedere un passaggio a Codex. Chiedi a Carlo soltanto informazioni indispensabili o decisioni con conseguenze fuori dall'ambito autorizzato. Non aspettarti che questa chat venga nuovamente usata.

## Workspace e istruzioni

Cartella principale:
`C:\Users\CarloTomaselli-SOFTCARE\OneDrive - SOFTCARE SRL\Documenti\Home Assistant`

- Fork: `repos/versatile_thermostat`.
- SDK Oikos: `oikos-card-starter-main`.
- Piano e prove: `repos/versatile_thermostat/plans/heating-orchestration-2026-11-24`.
- Evidenze operative: `output/Caldaia_stabilita_2026-11-24`, `output/PDC_cicli_override_2026-11-23`, `output/PDC_ritardo_finestre_2026-11-23`, `output/Registro_riscaldamento_2026-11-23`.

Leggi `AGENTS.md` del progetto; prima di lavorare sulle card leggi anche `AGENTS.md` e `SKILL.md` dell'SDK. Usa API supportate e istruzioni del connettore HA prima delle operazioni corrispondenti. Le possibilità di Codex non garantiscono che la tua sessione abbia gli stessi strumenti: verifica gli accessi senza cercare credenziali nei file temporanei.

Nessun commit, push, tag, PR o pubblicazione richiesti. Non eseguire gli script storici `plans/avatto-oikos-pilot.ps1` e `plans/boiler-timers.ps1`. Non scrivere direttamente nei file interni `.storage` di Home Assistant.

## Stato verificato al passaggio

M1 è **approvato**, compresa la revisione Claude R1/R2/E1. Vedi `M1-CONFERMA-CLAUDE.md`. Non ricominciare quel lavoro.

- Branch del fork: `codex/avatto-trv-pilot`, HEAD `6a819dcc1437fb33ddf8bf0dc2e3aa09e0c80666`.
- Motore `heating_boiler_policy.py`: SHA-256 `edf7e9007ac44166bfc56a3ad8c74dd6b167af6f4ed2760831d865443c27945b`.
- Test `test_heating_boiler_policy.py`: SHA-256 `43764243ad73e53740655b6690d0d74aaf071ba08d3dcff19d30d2e0d9c57708`.
- Verifica indipendente: 143 pass, 1 skip (ambiente HA completo mancante); parità con Jinja su 27 casi e altri 5.000, seed 104729. Griglia diagnostica 23.328 combinazioni.
- I file di M1 e la cartella dei piani sono **non tracciati**. Esistono altre modifiche nell'SDK Oikos, parte del lavoro corrente: non cancellarle, non resettarle o sovrascriverle.
- Alla lettura di questo handoff non esiste ancora `M2-A-ESITO.md` e non risultano modifiche runtime M2 nel Git status. Una sessione Claude può aver iniziato M2 nel frattempo: **controlla lo stato reale e riprendi eventuale lavoro in corso prima di assegnare nuovi worker**.
- Nessun nuovo codice M1/M2 è stato installato da Codex. Le automazioni attuali continuano a essere la fonte di controllo dell'impianto.

Gli hash sono una fotografia, non un comando per ripristinare file che nel frattempo siano stati modificati. Conserva i lavori successivi e verifica le differenze.

## Documenti e fonti

1. `README.md`, `SPECIFICA.md`, `DIAGNOSTICA.md`: piano generale, regole e contratto iniziale.
2. `M1-CONFERMA-CLAUDE.md`, `M1-ESITO.md`: motore approvato e limiti delle verifiche.
3. `M2-A-CLAUDE.md`: specifica dettagliata collector/observe, già pronta. Mantieni quei criteri tecnici, ma non il vincolo organizzativo di fermarti per Codex.
4. `baseline/index.json`: 15 automazioni lette via API più due file di casi (27 decisione, 17 osservazione), hash originali verificati. `boiler-decision-errata.json` autorizza la sola rettifica di `master_unknown` agli expected in memoria.
5. `review/verify_m1.py`: confronto offline col vero template catturato; nessuna HA live necessaria. Evidenze precedenti in `review/` e `m1-r1-claude/`.

Le baseline sono una fotografia del 24/11, non prova che la configurazione live sia rimasta identica. Prima della distribuzione rileggi HA e confronta i consumatori. I pacchetti storici in `plans/avatto-oikos/` descrivono politiche precedenti e non vanno reinstallati come se fossero attuali.

## Obiettivo finale e regole da preservare

Portare nel fork la gestione caldaia e il coordinamento radiatore/PDC, riducendo le automazioni di controllo duplicate e fornendo a Oikos diagnostica autorevole. Il cambiamento di sede della logica non autorizza a cambiare anche le politiche senza evidenza e motivazione esplicita.

### Caldaia

- Ordinario: almeno due valvole fisiche valide, una stanza con gap >=0,3 °C, stabilità 120 s, consenso nativo e pilota se abilitato.
- Singolo: una valvola, stessa stanza e stesso target con gap >=0,3 °C per 900 s. Non richiede consenso nativo a due valvole; conserva il veto pilota specifico se abilitato.
- Mantenimento: basta una valvola valida. Zero valvole o ingressi/centrale invalidi richiedono OFF; nessun minimo ON contro valvole chiuse.
- Pausa automatica: 600 s da OFF confermato. Manuale Oikos valido immediato, senza attese automatiche, mantenendo le protezioni.
- Master indisponibile non è spento; nessun comando nuovo senza stato valido. Ciclo persistente e ingressi riletti prima dei comandi.

### PDC

- Avvio: gap >=0,4 °C, stabilità 120 s, finestra chiusa, automatico/programma/centrale compatibili, niente override AC, disponibilità split, pausa 600 s da OFF confermato.
- Potenza: riserva 1500 W entro 2600 W (avvio casa <=1100 W); protezione stop a 2700 W secondo il confronto esatto degli snapshot.
- Stop ordinario a target +0,2 °C dopo 600 s HEAT confermato; stop anticipato a +0,5 °C o target abbassato già raggiunto. Protezioni/manuale hanno precedenza.
- Non avviare se entro 600 s il programma elimina la richiesta; setpoint split `ceil(target+1)` limitato 16–30 °C, conservando sequenze e verifiche correnti.
- Finestra aperta in marcia: 120 s continuativi, richiusura azzera; dati mancanti non sono finestra chiusa. Conservare i dettagli della protezione override, compresa la tolleranza precedente di 30 s per dati ignoti.
- Una sola PDC automatica contemporanea; non introdurre nuove priorità o il vecchio requisito “unica stanza in richiesta”. SmartThings resta l'integrazione.
- Passaggio PDC: prenotazione della capacità, inibizione radiatore e arresto fisico confermato, target verificato, nuove condizioni, HEAT e conferma split. Ritorno: split OFF confermato prima di rilasciare l'inibizione posseduta. Timeout/unknown non autorizzano doppia fonte.
- Manuale AC 120 minuti; registro effetti attesi per campo/valore/scadenza. Quiet può portare fan ad auto, OFF può azzerare preset: finestra attesa 75 s; vero evento utente autenticato prioritario. Autore ignoto resta ignoto.

Per formule, timeout e casi limite prevalgono gli snapshot e le specifiche. Programmazioni, preset e profili Samsung possono restare esterni quando non serve migrarli: individua chiaramente i proprietari dei comandi. Non riscrivere Avatto, inibizione o finestre già presenti.

## Consegne ampie e parallele

Se sono disponibili subagent/agent teams di Claude Code, usa un coordinatore con **2–3 worker mirati**, più review indipendente quando serve. Non attivare molti agenti solo per accorciare ogni singola fase: consumano la stessa quota del modello e aggiungono coordinamento.

| Filone | Responsabilità e output | Dipendenze |
| --- | --- | --- |
| A — Caldaia completa | M2-A collector/observe/config/diagnostica e M2-B executor, ownership, conferme, persistenza e lifecycle; test HA e compatibilità | Riusa M1; osservazione revisionata prima di predisporre controllo attivo |
| B — PDC completa | M3/M4/M5: decisioni, transazioni con inibizione, mutua esclusione, timeout, manuale/provenienza e test | Può iniziare con modello puro e fixture in parallelo ad A; runtime dopo accordo sui contratti e ownership |
| C — Oikos | Checklist e stati basati sul contratto nativo, registro, compatibilità legacy, build e pacchetti nell'SDK | Analisi/fixture UI subito; implementazione dopo schema condiviso stabile |
| D — Review/test | Parità con baseline, input invalidi, timing, concurrency, reload, config flow, rendering e assenza doppio controllo | Lavora su snapshot identificabile dei file; revisione dopo ciascun blocco integrato |

Il coordinatore fissa presto interfacce, schema diagnostico e mappa dei file posseduti. Un unico autore alla volta per `__init__.py`, `vtherm_central_api.py`, `config_flow.py`, `config_schema.py`, `const.py`, `sensor.py`, traduzioni e test condivisi del fork. Gli altri worker consegnano moduli separati e indicazioni di integrazione; il coordinatore integra i punti comuni. Nell'SDK un solo autore dei moduli condivisi.

I test che dipendono da nuove API si possono preparare in parallelo sul contratto concordato, ma l'esecuzione integrata viene dopo l'unione dei cambiamenti. Review e test sul codice ancora in mutazione non certificano il risultato finale: registra hash/versione e riesegui i controlli pertinenti dopo l'integrazione.

Se usi worktree o checkout separati, ricorda che il codice approvato è ancora non tracciato: un worktree dal solo HEAD **non lo contiene**. Predisponi un manifest e trasferisci soltanto i sorgenti/test/baseline necessari, verificandone gli hash; non copiare la cartella HA intera con materiali sensibili. Non fare commit, stash, reset o clean per creare isolamento. In alternativa usa la stessa working directory con proprietà dei file esclusiva.

Se la sessione non offre strumenti multiagente, completa gli stessi filoni in sequenza, senza bloccare il progetto né chiedere a Carlo di aprire chat multiple. Puoi suggerire sessioni separate solo se utili, con cartelle/file assegnati e un solo integratore.

## Verifiche e limiti dell'ambiente

- Ambiente puro precedente: venv con Python 3.15.2, pytest/Jinja2/Black. Non deve essere una dipendenza personale del progetto: documenta comandi ricreabili e niente stub esterni.
- CI del fork: Python 3.14 e `requirements_test.txt`. I test caldaia/Avatto e HA completi non sono stati eseguiti da Codex; l'ambiente Windows precedente non installava le dipendenze HA. Puoi predisporre un ambiente di test isolato adatto, ma non usare la HA di casa come test runner e non dichiarare riuscito un test non eseguito.
- Caldaia nativa segnala instabilità dei test in parallelo: quel sottoinsieme va eseguito in singolo processo. Usa clock controllato per attese di minuti.
- Black rimaneva bloccato nelle sessioni Codex; Claude lo ha eseguito con successo con 24.10.0 e 26.10.1. Non modificare le dipendenze globali per aggirare differenze ambientali.
- Ingressi unknown/unavailable, campioni vecchi/futuri, duplicati e riconnessioni devono essere parte dei test, non casi impliciti assimilati a OFF/zero.
- Un servizio completato non prova HEAT/OFF fisico né attività bruciatore/compressore. Proposta, comando e conferma sono stati distinti.

## Oikos: contesto e lavoro residuo

Ultimi pacchetti preparati: stanza 1.10.1, centrale 1.6.0, riepilogo 2.5.17, salute 1.1.22. Verifica quelli realmente installati prima di intervenire. La 1.10.0 stanza aveva `connected is not defined`, corretto nella 1.10.1 con test di rendering del componente principale: non reintrodurlo.

La checklist è espandibile e il layout esistente va mantenuto. Verde/rosso/attesa/ignoto/non applicabile con icona e testo. I dati mancanti non sono condizioni rispettate; le condizioni iniziali PDC in marcia e quelle caldaia non richieste dal mantenimento sono non applicabili.

Attualmente `boilerControl.js` richiede automazione esterna ON e JSON helper v1 recente (45 s). Quando si disabilita il controller esterno, **serve una selezione esplicita del nuovo contratto nel frontend**: pubblicare soltanto un altro JSON non basta. Evita due scrittori sugli helper legacy. Vedi `DIAGNOSTICA.md` e SDK `docs/heating-checklist.md`.

Build/pack di tutte le card che consumano moduli condivisi; sorgenti e pacchetti dentro l'SDK. Carlo preferisce i pacchetti lì, senza nuovi ZIP da scaricare in chat. Testare componenti completi oltre ai modelli. L'anteprima browser non partiva nelle sessioni Codex; verifica se il tuo ambiente può eseguirla. Se non può, dichiara che il controllo visivo non è stato eseguito.

## Impianto: passaggio progressivo e ripristino

Hai in carico anche queste attività se disponi delle connessioni e dei permessi. Non sono autorizzati comandi fisici gratuiti per provare la UI. Non cercare token nei file: usa connettori e API supportate. Se manca un accesso, completa tutto il lavoro indipendente e riporta il blocco preciso a Carlo.

1. Ricontrolla configurazione reale, versione installata, stati e riferimenti. Verifica controller nativo, automazioni, helper, script, card ed eventuali percorsi Node-RED, senza assumere attuale l'architettura storica. Conserva backup mirati prima delle scritture.
2. Requisito per distribuire: test pertinenti passati, review indipendente dei punti critici, configurazione concreta e ripristino preparati. Prima disabled/observe, controller esterno unico proprietario, nessuna modifica agli attuatori nell'observer.
3. Confronta le decisioni su richieste naturali e valida UI/diagnostica. Differenze per seed del ciclo o istanti di campionamento vanno distinte da differenze di politica. Dichiarare cosa non è stato possibile osservare.
4. Per il control, assicurati di aver annullato transazioni pendenti e disattivato il vecchio proprietario pertinente prima di attivare il nuovo. Mai due controller sullo stesso attuatore. Per PDC includi chooser, override, protezioni e registri che possono comandare lo split.
5. Controllo runtime della proprietà; perdita proprietà o callback vecchia non deve generare compensazioni concorrenti. Un'inibizione non posseduta non si cancella. Il fallimento di una scrittura diagnostica/persistenza non deve impedire uno stop necessario.
6. Rimuovi helper e automazioni solo dopo verifica dei consumatori e osservazione riuscita; non cancellare rollback per semplice pulizia.

Le istruzioni AGENTS governano interventi rilevanti o interruzioni: prepara prima il risultato concreto e il ripristino, poi chiedi solo le eventuali decisioni indispensabili a Carlo. Il trasferimento di coordinamento non è un permesso per operazioni distruttive o per aggirare approvazioni degli strumenti. Non introdurre una richiesta di conferma per ogni normale modifica reversibile del codice.

Ripristino: disabilitare il nuovo proprietario, annullarne i task, riconciliare stati e inibizioni; riattivare il controller precedente solo dopo verifica che non vi siano concorrenti. Non rilasciare radiatore con split non confermato OFF. Conserva le configurazioni esatte prima/dopo e il confronto degli hash.

## Continuità, output e definizione di completamento

Mantieni qui `CLAUDE-STATUS.md`: filone, responsabile, file assegnati, stato, dipendenze, test, problemi e prossima azione. Aggiornalo alle consegne e prima di compattazione/cambio sessione. Gli esiti per lotto rimangono utili, ma non richiedono un nuovo prompt di Codex.

Il lavoro è completo quando codice e configurazione sono revisionati/testati, le card leggono il contratto nuovo, observe e passaggio di proprietà sono verificati dove accessibili e Carlo riceve un resoconto che distingue file locali, pacchetti, versione installata e comportamento reale osservato. Se la parte operativa è bloccata, consegna comunque implementazione completa, pacchetti e runbook concreti, dichiarando ciò che resta da applicare.

Prossima azione: ricognizione del lavoro Claude già in corso, assegnazione di A/B/C con proprietà file e contratti condivisi; completare il progetto, senza un ulteriore passaggio a Codex.
