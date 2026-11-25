# Migrazione delle logiche di riscaldamento nel fork

**Mandato corrente: Claude assume l'intero progetto, inclusi coordinamento, review, Oikos e attività HA consentite. Punto di ingresso: `CLAUDE-TAKEOVER.md`; stato operativo: `CLAUDE-STATUS.md`. I passaggi a Codex e i limiti organizzativi dei precedenti incarichi sono superati. Politiche funzionali, verifiche e requisiti di ripristino rimangono validi.**

Piano preparato il 24 novembre 2026, orari Europe/Rome. Stato: **M1 approvato dopo le correzioni Claude; incarico M2-A pronto. Nessuna attivazione sull'impianto eseguita**.

Punto di ingresso corrente per l'esecutore: **`CLAUDE-TAKEOVER.md`**. Claude subentra a OpenCode per l'implementazione di tutti i lotti ancora aperti. I documenti OPENCODE restano lo storico; piano funzionale e confini operativi rimangono validi.

## Responsabilità

| Attività | Responsabile |
| --- | --- |
| Codice Python del fork, test, documentazione tecnica della modifica | Claude, subentrato a OpenCode |
| Inventario HA, specifiche funzionali, revisione e confronto con l'impianto | Claude con Carlo |
| Card Oikos e adattamento al nuovo contratto diagnostico | Claude |
| Installazione del fork, configurazione, passaggio di controllo e osservazione reale | Claude con Carlo, secondo il mandato corrente |

Claude non accede all'impianto e non esegue script di distribuzione. Nessun commit, push, release o installazione fa parte dell'incarico. Non deve risolvere la migrazione generando altre automazioni o pacchetti per HA.

## Base verificata

- Repository: `repos/versatile_thermostat`, branch `codex/avatto-trv-pilot`, HEAD `6a819dcc1437fb33ddf8bf0dc2e3aa09e0c80666`, versione componente 10.6.3.
- Prima del lavoro risultano non tracciati `plans/avatto-oikos-pilot.ps1` e `plans/boiler-timers.ps1`: appartengono al lavoro esistente, non modificarli o eseguirli.
- Nessun `AGENTS.md` interno trovato; si applicano le istruzioni del progetto padre e `CONTRIBUTING.md`, salvo i vincoli più specifici di questo incarico.
- `baseline/` contiene 15 configurazioni lette dalle API HA, tutte con `validation=null`: caldaia, due scelte di fonte, due registri di ciclo, due registri comandi, due rilevatori override, due fine override, due protezioni override e due profili comfort.
- `baseline/index.json` registra origine, hash della configurazione HA e hash SHA-256 dei file. Questi snapshot sono fonti di lettura, **non script da applicare**. Prima di una futura attivazione vanno confrontati di nuovo con HA.
- Le versioni in `plans/avatto-oikos/` documentano una politica precedente: non sono la specifica corrente. In particolare non ripristinare la regola “PDC solo se unica stanza in richiesta”, il vecchio ritardo manuale o lo spegnimento caldaia con una sola valvola.
- Le modifiche alla caldaia del 24/11 non hanno ancora una convalida prolungata su cicli naturali. La migrazione deve riprodurre la politica verificata dai test; non assumere dimostrato un risparmio o l'eliminazione dei cicli brevi.

L'ordine di lettura è: questo file, `SPECIFICA.md`, `DIAGNOSTICA.md`, `CLAUDE-HANDOFF.md`, poi le fonti specifiche citate. Le configurazioni complete prevalgono sulle descrizioni sintetiche per formule, confronti, timeout e ordine delle azioni. Le incongruenze vanno segnalate, non corrette silenziosamente.

## Architettura scelta

Una funzionalità opzionale del fork, con un coordinatore legato alla configurazione centrale e associazioni configurabili tra stanza, radiatore e PDC. Separare motori decisionali puri, raccolta degli ingressi HA ed esecuzione asincrona dei comandi. Non duplicare il controllo Avatto, l'inibizione o la gestione finestre già presenti.

La funzione nuova è disabilitata nelle configurazioni esistenti. Le future modalità runtime sono `disabled`, `observe`, `control`. In observe calcola proposte e diagnostica propria, senza chiamate a climate, inibizione, helper/timer esistenti o registri di comando. Non adotta automaticamente il controllo dopo un reload o la scomparsa del controller esterno.

Il consenso nativo, la proposta del coordinatore, il comando inviato e lo stato confermato del master devono essere distinti. Il nuovo controllo non deve usare come consenso la propria uscita, né avere lo stesso attuatore del gestore nativo. L'integrazione con `FeatureCentralBoilerManager` va verificata in M2: preferire riuso dei suoi dati e punti di estensione a un secondo ciclo concorrente. Incompatibilità di configurazione o proprietario concorrente impediscono il control.

Tutti gli ID delle stanze e degli helper appartengono alla configurazione o alle fixture, non al motore. Parametri iniziali corrispondenti alla baseline, espliciti e validati. Nessuna nuova priorità tra le due PDC nella prima migrazione.

## Lotti e consegne

| Lotto | Consegna Claude | Condizione per chiuderlo |
| --- | --- | --- |
| M1 — caldaia: motore puro | Modelli, valutazione, contesto di ciclo, motivi e condizioni; test di parità e sequenze temporali | Review di Codex, casi baseline e nuovi edge case passati; zero collegamenti runtime |
| M2 — caldaia: integrazione | Raccolta ingressi, persistenza versionata, scheduling cancellabile, diagnostica, config flow, observe e control esplicito | Test HA di lifecycle/ownership, compatibilità disabled; nessuna installazione da Claude |
| M3 — PDC: decisioni | Motore puro per scelta fonte, avvio, mantenimento e arresto; persistenza HEAT/OFF e target iniziale | Parità con le due configurazioni attuali, soglie e finestre; nessun cambio di priorità |
| M4 — PDC: transazioni | Prenotazione globale dello split, inibizione, conferme e ripristino, timeout e annullamenti | Nessun avvio senza radiatore arrestato; nessun rilascio senza OFF split confermato |
| M5 — manuale e provenienza | Rilevamento override, registro degli effetti attesi, scadenza, protezioni, eventi per Oikos | Quiet/OFF non creano falso override; un vero intervento utente mantiene precedenza |
| M6 — consolidamento | Documentazione configurazione, contratti, regressioni e note di distribuzione | Review integrata e matrice di copertura completa |

**L'incarico corrente autorizza solo M2-A (collector e observe); M1 è approvato.** Al termine Claude consegna file cambiati, risultati dei test e dubbi; Codex effettua la review e prepara il lotto successivo. Questo è un confine di incarico concordato per organizzare le consegne, non un requisito di approvazione di uno strumento.

Programmazioni settimanali, temperature dei preset, override Oikos della programmazione e rappresentazione grafica restano esterni. Le politiche leggono la richiesta risultante. Quiet, display, ventola e dettagli Samsung restano inizialmente nelle automazioni comfort: in M5 si coordina la provenienza dei loro comandi; una loro eventuale migrazione è una consegna separata. Nessuna variazione a SmartThings.

## Passaggio all'impianto: attività futura di Codex

1. Nuova lettura configurazioni, stato Git/versione realmente installata, riferimenti di automazioni/script/helper/card e servizi di attivazione nativi. Verificare anche eventuali percorsi Node-RED prima di dichiarare esclusività del controllo. Inventario attuale non dimostra l'assenza di tutti i consumatori.
2. Installazione della versione revisionata in modalità disabled; poi observe, lasciando il controller attuale come unico proprietario. Confrontare decisioni su richieste normali, stanza singola, manuale, perdita dati e finestra. Observe non invia comandi di prova.
3. Preparare l'adattamento Oikos al contratto nativo prima di togliere le vecchie fonti. Verificare tutte le condizioni della checklist e la distinzione tra richiesta e attività confermata.
4. Per ciascun attuatore: portare a termine o annullare le transazioni pendenti, spegnere il relativo vecchio controller, verificarne lo stato, attivare esplicitamente il nuovo. Per le PDC trattare insieme chooser, registri, override e protezioni che comandano lo stesso split; il lotto caldaia può precedere quello PDC.
5. Conservare un'interdizione runtime contro controller esterni attivi o dati del proprietario non disponibili. Perdita di proprietà: annullare gli incarichi pendenti senza competere inviando comandi di compensazione arbitrari. Non rimuovere un'inibizione che non si possiede.
6. Osservare cicli naturali e verificare reload/riavvio su ambiente di test prima dell'impianto. Rimuovere helper e automazioni soltanto dopo la verifica dei consumatori, in un intervento distinto.

Ripristino operativo: disabilitare il nuovo controllo e verificare cancellazione di timer/transazioni; riconciliare stati reali e inibizioni; ripristinare il controller precedente specifico e riattivarlo solo quando non ci sono due proprietari. Non fare un semplice doppio toggle durante una transazione PDC e non rilasciare il radiatore se lo split non è confermato spento.

## Stato delle consegne

| Lotto | Stato |
| --- | --- |
| M1 | Approvato: R1/R2/E1 chiusi; vedi `M1-CONFERMA-CLAUDE.md` |
| M2 | M2-A assegnato a Claude con `M2-A-CLAUDE.md`; M2-B (comandi/ownership) dopo review |
| M3-M6 | Specificati a livello di obiettivo; implementazione dopo review dei lotti precedenti |
| Impianto e card | Nessuna modifica effettuata per preparare questo piano |
