# Review del fork Versatile Thermostat

25 novembre 2026, Europe/Rome · `codex/avatto-trv-pilot` · `a8b62ab54ec10636ff0a327d9acadc1162e073af`

## 1. Sintesi in 10 righe

1. Il commit è verificabile, ma **non è una build riservata a `observe`**. La guida disponibile descrive una build precedente.
2. La caldaia rifiuta una configurazione `control` salvata. La PDC accetta `control` dalla configurazione centrale se è confermato.
3. Con entrambi i manager esplicitamente in `observe`, il rischio diretto di comandi dell’orchestrazione è basso. Le prove con HA finto hanno registrato zero comandi.
4. In `observe` il rischio principale è operativo. Il sensore stanza può mostrare uno zero su dati ignoti e presentare come allineate due pubblicazioni di istanti diversi.
5. Il controllo PDC presenta due problemi di proprietà. Può rilasciare un’inibizione sostituita da altri e recuperare il diritto di rilascio su un dispositivo riconfigurato.
6. Il controllo caldaia ha un problema prima di un futuro rilascio. Dopo l’attesa dello store può inviare HEAT a un master diventato indisponibile.
7. M1 è invariato rispetto alla release observe precedente. L’estrazione di `active_valves()` preserva l’espressione originale del collettore.
8. Override del programma e registro stanza hanno motori separati, senza collegamento al ciclo di vita HA nel commit. Entrambi hanno un errore riprodotto.
9. I 16 file eseguibili offline hanno dato **1317 passati e 2 saltati**. I tre harness con HA finto hanno passato 25 scenari caldaia, 45 verifiche PDC e 33 verifiche stanza.
10. Giudizio. **Non distribuire questo commit come observe-only.** Per il controllo servono prima proprietà verificabile e recupero sicuro, poi validazione su HA 2026.11.2 in ambiente isolato.

## 2. Problemi

I riferimenti ai moduli Python e a `translations/` sono relativi a `custom_components/versatile_thermostat/`. `tests/` e `plans/` partono dalla radice del fork. Le prove usano soltanto ingressi sintetici e porte simulate.

### Bloccanti

#### B1. Il controllo PDC è raggiungibile nella build che si presume observe-only

- **Dove** · `const.py:291`, `feature_heating_pdc_manager.py:71,87–106,143–163,223,396–449`, `config_flow.py:968–986,1035–1054,1740–1743`, `__init__.py:112–125,196–212`, `manifest.json:25`.
- **Scenario e prova** · `PDC_CONTROL_AVAILABLE` e `HO_PDC_CONTROL_ENABLED` sono entrambi `True`. Una centrale con caldaia in `observe` e PDC salvata come `control`, con conferma esplicita e proprietari configurati, avvia il runtime PDC di controllo. L'harness PDC a riga 453 prova proprio che il manager parte in `control` e acquisisce la proprietà quando il proprietario esterno passa da `on` a `off`. Lo scenario a riga 422 verifica soltanto il rifiuto della configurazione senza conferma. Anche l'azione amministrativa di ripresa automatica viene registrata nella build corrente.
- **Correzione proposta** · Per una futura distribuzione observe-only, allineare il vincolo di rilascio tra options flow, creazione del manager, porte degli effetti e servizi. Testare una configurazione persistita già confermata, anche dopo reload. Etichettare in modo distinto la build che consente il controllo PDC. **Coinvolge i cancelli. M1 escluso.**

#### B2. Il runtime PDC può rilasciare un’inibizione sostituita da un altro proprietario

- **Dove** · `heating_pdc_runtime.py:1111–1146,1169–1198`, `heating_pdc_transaction.py:274–300,458–492`, `inhibition_manager.py:215–237,254–259`.
- **Scenario e prova** · La PDC entra in una sessione con `inhibition_owned=True`. Un altro attore rimuove e reimposta l’inibizione prima del ritorno al radiatore. Nella prova con split e VTherm PDC entrambi `off`, l’inibizione corrente aveva il marcatore della nuova transizione esterna e il runtime ha comunque chiamato `unset_inhibition`. Il codice confronta i marcatori durante l’acquisizione iniziale, poi per rilasciare si fida del booleano nella transazione. I test `tests/test_heating_pdc_runtime.py:415–458,475–485` coprono l’inibizione preesistente e la transazione che già sa di non possederla. Il caso della proprietà sostituita dopo l’acquisizione sfugge a entrambi.
- **Correzione proposta** · Dare a set/unset un’identità del proprietario e un identificatore di acquisizione persistenti. Confrontare e rilasciare sotto il lock del gestore dell’inibizione. Se la proprietà non è dimostrabile, lasciare l’inibizione e pubblicare il motivo. Provare sostituzione concorrente, set idempotente e riavvio nel mezzo dell’acquisizione. **M1 e cancelli esclusi.**

#### B3. Una transazione PDC salvata sopravvive al cambio del dispositivo a cui si applica

- **Dove** · `heating_pdc_runtime.py:483–503,525–560,592–600,911–923`, `heating_pdc_transaction.py:601–625`, `vtherm_central_api.py:267–272`, `config_flow.py:1013–1033,1095–1112`.
- **Scenario e prova** · Ho salvato una transazione `releasing` con proprietà, poi ho cambiato il radiatore del binding conservando la chiave stanza. Il runtime ricreato ha accettato la proprietà per il nuovo binding senza errori di caricamento. Lo store identifica la transazione per `room_key` e omette i binding degli attuatori. Il recupero applica il record ai dispositivi correnti. Se si rimuove una stanza durante una transazione, la transazione non viene caricata e la relativa prenotazione viene scartata, mentre lo stop esplicitamente non compensa gli effetti.
- **Correzione proposta** · Legare la transazione e l’override ai dispositivi originari. Richiedere una conclusione verificata della sessione prima di cambiare o rimuovere un binding o passare a `observe`/disabled. Preservare i record di transazioni orfane fino a riconciliazione esplicita. Migrare lo store e bloccare il recupero automatico dei vecchi record con proprietà indimostrabile. **M1 e cancelli esclusi.**

### Importanti

#### I1. La caldaia può inviare HEAT sulla base di uno stato del master precedente al salvataggio

- **Dove** · `feature_heating_orchestration_manager.py:772–791,1059–1079,1101–1121,1151–1157`.
- **Scenario e prova** · Con la barriera aperta soltanto nell’harness, ho portato il master da `off` a `unavailable` al ritorno dell’attesa di persistenza, prima che la rivalutazione accodata girasse. Il servizio finto ha ricevuto un HEAT. Il controllo finale della proprietà riceve il vecchio `result.master_mode`. La verifica delle modalità supportate legge gli attributi, che possono essere ancora presenti su un’entità indisponibile.
- **Correzione proposta** · Dopo l’ultimo `await` prima dell’invio, rifare lettura del master, valutazione, scelta del comando e verifica della proprietà. Invalidare e contabilizzare coerentemente un invio abbandonato. Aggiungere regressioni con perdita del master, finestra aperta, perdita di richiesta e riattivazione del proprietario durante il salvataggio. **M1 e cancelli esclusi.**

#### I2. L’interblocco PDC consente OFF con il proprietario esterno ignoto

- **Dove** · `heating_pdc_runtime.py:159,663–712,748–787,834–839,1063–1070`, `CONTRATTI.md` §6.
- **Scenario e prova statica** · Con una transazione attiva, tutti i proprietari che bloccano risultano mancanti, `unknown` o `unavailable` per almeno 30 s. Lo stato pubblicato è `blocked`, ma `_safety_off()` può inviare OFF sia al VTherm PDC sia allo split. Se un proprietario è esplicitamente `on`, questa eccezione è inibita. Il contratto richiede un `off` esplicito per gli effetti. Un proprietario temporaneamente non leggibile può continuare ad agire.
- **Correzione proposta** · Allineare il runtime al contratto o approvare e documentare esplicitamente l’eccezione. In quest’ultimo caso legarla a una proprietà verificata della sessione, limitare i tentativi e testare riconnessioni intermittenti e proprietario che torna `on` tra i due invii. **M1 e cancelli esclusi.**

#### I3. Stato stanza e caldaia condividono la generazione anche quando pubblicano istanti diversi

- **Dove** · `feature_heating_orchestration_manager.py:151–157,532–572,596–614,648,1212–1214`, `heating_room_status.py:973–988`, `ROOM-CONDITIONS-CONTRATTO.md` §1 e §10.6.
- **Scenario e prova** · Su snapshot invariato, a `t+25 s` il sensore stanza mostra ancora `t+0`. Il tick a `t+30 s` aggiorna entrambi, perché la soglia di 25 s viene verificata con un timer di valutazione di 15 s. In una seconda prova ho cambiato solo il feed PDC a `t+1 s`. La stanza pubblica `t+1 s` con la generazione di caldaia ancora a `t+0`. L’harness confronta stanza e caldaia al primo giro e osserva il rinnovo solo entro 35 s, quindi passa in entrambi i casi.
- **Correzione proposta** · Usare un heartbeat a scadenza esplicita e un identificativo di snapshot coerente. Se si vuole conservare aggiornamenti indipendenti, rendere esplicito nel contratto che la generazione stanza è separata e fornire un riferimento allo snapshot M1 usato. Includere la fase nella firma delle condizioni come richiede la chiave composta. Stabilire quali scadenze o valori numerici debbano aggiornarsi subito. **M1 e cancelli esclusi.**

#### I4. Il sensore stanza trasforma ingressi mancanti in zero o esclusioni definitive

- **Dove** · `heating_boiler_collector.py:642–658,744–760`, `heating_room_status.py:502–517,596–616,638–649`, `ROOM-CONDITIONS-CONTRATTO.md` §10.1 e §10.5.
- **Scenario e prova** · Con conteggio centrale `unknown` e attributo `active_device_ids=[]` ancora presente, M1 riceve ingressi non validi, ma la stanza dice `valve_active=fail`, `actual=0`. Provato con lo stesso snapshot e una stringa malformata al posto della lista, la riga diventa correttamente `unknown`. Inoltre, se manca il flag di partecipazione alla caldaia, la fase `boiler` diventa `na` come quando la stanza è esplicitamente esclusa. Anche questo è stato riprodotto.
- **Correzione proposta** · Per il sensore distinguere conteggio attendibile, lista effettivamente leggibile, dato mantenuto da un’entità indisponibile e zero confermato. Usare `na` solo con esclusione esplicita. Tenere la qualificazione del collettore per M1 separata dalla spiegazione dei dati mancanti. Aggiungere casi con lista vecchia vuota/piena, conteggio incoerente e flag di partecipazione assente. **M1 e cancelli esclusi.**

#### I5. L’override del programma riattiva scheduler che il fork non ha sospeso

- **Dove** · `room_override_runtime.py:180–211,234–267,279–349,415–432`.
- **Scenario e prova** · Due programmi partono rispettivamente da `on` e `off`. L’override spegne il primo. Al termine, con il controller legacy di nuovo `on` e il primo programma `unavailable`, il runtime invia comunque due `turn_on`, uno a ciascun programma, e può proseguire con `run_action`. Riprodotto con porte finte. L’interblocco viene applicato durante la pausa ma manca nel percorso di fine. Lo store non distingue quali programmi siano stati modificati dal fork.
- **Correzione proposta** · Persistire lo stato di partenza e le modifiche attribuibili all’override. Ripristinare solo i programmi gestiti, rivalutando proprietario e disponibilità prima di ogni effetto. Tracciare gli esiti per impedire la ripetizione di `run_action` già riuscite dopo un fallimento parziale. **M1 e cancelli esclusi. Oggi il motore non è collegato a HA.**

#### I6. L’ultimo lotto del registro può perdersi durante uno stop ordinato

- **Dove** · `room_journal_runtime.py:144–156,232–257`; `tests/test_room_journal.py:364–396`.
- **Scenario e prova** · Ho bloccato il primo `save` su una porta finta e invocato `stop()`. Il runtime aveva già impostato `_dirty=False`. La cancellazione del task di scrittura non ripristina il flag, quindi lo stop non riprova il flush. Il contatore ha registrato una sola scrittura iniziata, poi cancellata. I test esistenti coprono stop con scrittura ancora accodata e salvataggio che solleva un errore, senza coprire questa cancellazione.
- **Correzione proposta** · Completare il flush in volo oppure mantenere un contatore di revisione e marcare come persistita la revisione solo dopo conferma del salvataggio. Provare anche eventi ricevuti mentre uno snapshot è in scrittura. **M1 e cancelli esclusi. Oggi il motore non è collegato a HA.**

#### I7. Il recupero da unload fallito dipende dal successo di ogni callback dei sensori

- **Dove** · `__init__.py:260–289`, `feature_heating_orchestration_manager.py:444–455,479–504,509–515`, `feature_heating_pdc_manager.py:312–327,371–383`.
- **Scenario e prova statica** · Se lo scarico delle piattaforme restituisce `False`, vengono ricreati i manager e i publisher superstiti sono chiamati prima di `async_start`. Basta che un callback stanza sollevi un errore durante `adopt_publishers` per interrompere il riavvio della caldaia e saltare il ramo di ripristino PDC. Nella pubblicazione normale della stanza lo stesso errore è già isolato. Inoltre `adopt_publishers` condivide anche lo stato stanza precedente: può inviare il vecchio payload prima della prima valutazione nuova.
- **Correzione proposta** · Trasferire soltanto le sottoscrizioni valide, inizializzare lo snapshot come `starting` e isolare gli errori per publisher e per manager. Tenere un contesto di proprietà indipendente dai callback diagnostici. Provare fallimento parziale di una piattaforma, eccezione in un callback, detach dopo adozione e successivo reload riuscito. **M1 e cancelli esclusi.**

#### I8. La proiezione stanza accetta dati PDC di un altro binding con la stessa chiave

- **Dove** · `feature_heating_orchestration_manager.py:537–545`, `vtherm_central_api.py:267–277`, `heating_room_status.py:404–445`, `config_flow.py:948–961`.
- **Scenario e prova statica** · Le due liste di stanze sono configurate e validate separatamente. Se nella sezione caldaia un determinato slug punta allo split A e nella sezione PDC lo stesso slug punta allo split B, il provider cerca solo per slug. La stanza può mostrare `pump_heating` sulla base dello split B mentre `pdc_released` controlla A. Con slug diversi per la stessa stanza il feed PDC risulta invece non disponibile.
- **Correzione proposta** · Collegare le sezioni tramite un binding esplicito e validarne la coerenza nel flusso e al caricamento. Se il binding è incoerente, pubblicare un errore leggibile per la sola stanza. **M1 e cancelli esclusi.**

### Minori

#### Minore 1. La parità può essere verde con un riferimento obsoleto o con un bloccante della fase sbagliata

- **Dove** · `tests/test_heating_room_status_parity.py:111–115,153–159,173–188,194–219`, `tests/tools/export_card_reference.mjs:85`, `tests/tools/room_status_scenarios.py:418`.
- **Scenario e prova statica** · Il test sugli hash verifica nomi e formato SHA-256. Un hash di 64 zeri supera quella verifica. Il confronto del bloccante usa solo `id`, quindi un bloccante con lo stesso ID nella fase sbagliata può passare. Una fase assente da uno dei due output viene saltata. La mappa indicizzata per `(stage,id)` preserva i duplicati leciti tra fasi ma non rileva un doppione nella stessa fase.
- **Correzione proposta** · Testare presenza delle fasi attese, unicità interna e chiave composta del bloccante. Rinominare il test degli hash secondo ciò che verifica e prevedere un controllo riproducibile degli hash del sorgente quando l’SDK è disponibile. Tenere la rigenerazione fuori dalla normale invocazione di pytest: oggi `PARITY_UPDATE` attiva una scrittura nel fork. **M1 e cancelli esclusi.**

#### Minore 2. Alcuni payload del sensore non rispettano lo schema pubblicato

- **Dove** · `heating_room_status.py:911–967`, `sensor.py:1292–1307,1335–1343`, `ROOM-CONDITIONS-CONTRATTO.md` §1–2.
- **Scenario e prova** · Ho ottenuto `heat_state=null` da `empty_room_attributes()` per una stanza con stato `disabled`. Il contratto indica uguaglianza tra lo stato pubblicato e l’attributo `heat_state`. Nel percorso `manager_missing` il sensore pubblica solo quattro attributi. Un consumer che verifica lo schema riceve pertanto forme diverse sotto la stessa versione.
- **Correzione proposta** · Usare un unico costruttore per gli stati di servizio, valorizzare `heat_state` e decidere con un test di schema come rappresentare `reason` e `evaluated_at` prima della prima valutazione. **M1 e cancelli esclusi.**

#### Minore 3. Pulizia del registro e creazione delle entità usano validazioni diverse

- **Dove** · `sensor.py:89–108,1151–1199,1300–1308` e `heating_boiler_collector.py:118`, `heating_pdc_collector.py:335–340`.
- **Scenario e prova statica** · Due righe con la stessa chiave nella configurazione centrale producono due istanze stanza con lo stesso `unique_id`. Il manager rifiuta invece la configurazione duplicata. Se sparisce l’intera sezione, la pulizia PDC considera tutte le sue stanze rimosse, mentre quella stanza ritorna prima di consultare il registro. Si possono lasciare entità stanza orfane dopo un ripristino di configurazione.
- **Correzione proposta** · Condividere la lettura dei binding con il manager. Distinguere sezione assente, vuota o malformata, e creare al massimo un sensore per chiave valida. Verificare conservazione dei nomi personalizzati per stanze rimaste, rimozione delle sole entità obsolete della centrale e rollback tra versioni. **M1 e cancelli esclusi.**

#### Minore 4. La guida di installazione e rollback è applicabile a un altro commit

- **Dove** · `INSTALLAZIONE-OBSERVE.md` §1–3, §8–10, `ROOM-CONDITIONS-ESITO.md` §7, `manifest.json:25`, `feature_heating_orchestration_manager.py:716–745`.
- **Scenario e prova statica** · La guida identifica `99b8994` / `10.6.3+oikos.1` e la sua impronta del pacchetto. Il commit revisionato dichiara `10.6.3+oikos.3`, contiene sensori stanza e apre la PDC. Il ripristino dei tentativi della caldaia ora legge `command_budget` dallo store. Il caso di riavvio con budget già consumato passa nell’harness caldaia. L’esito stanza descrive ancora file locali non committati e un rollback basato sulla loro rimozione.
- **Correzione proposta** · Versionare le istruzioni col commit, elencare runtime e servizi effettivamente disponibili e distinguere rollback di codice, ConfigEntry, store, proprietà attiva e registro entità. Per PDC con sessione attiva, descrivere come verificare che split e VTherm PDC siano OFF e che le inibizioni possedute siano riconciliate prima del passaggio al legacy. **M1 e cancelli esclusi per la documentazione.**

## 3. Cosa manca per un controllo sicuro, in ordine di dipendenza

1. **Definire il perimetro della distribuzione.** Chiudere la discrepanza B1. Scrivere un test di rilascio che provi options flow, configurazione persistita e servizi con la stessa combinazione di cancelli. Tenere i manager in `observe` durante le verifiche di confronto.
2. **Rendere verificabile la proprietà degli effetti.** Risolvere B2, B3, I1, I2 e I7. Provare acquisizione e rilascio in concorrenza, riavvio a ogni fase di hand-over, cambio di binding, perdita di rete e riconnessione in ordine diverso. Usare conferme esplicite di OFF e limiti di ritentativo verificati dopo riavvio.
3. **Chiudere il rapporto con il legacy.** Definire per attuatore chi comanda, chi può inibire, chi si occupa dei programmi e quando trasferire la proprietà. Collegare il motore di override solo dopo I5 e con stato e servizi effettivamente verificabili. Una ricerca delle istanziazioni di `RoomOverrideRuntime` e `RoomJournalRuntime` nel codice di produzione restituisce zero riscontri. Il collettore PDC riusa costanti dell’override senza collegare il suo runtime. Prima di esporre il registro stanza, risolvere I6 e definire provenienza, autorizzazione e deduplica degli eventi di reconnect.
4. **Validare su HA 2026.11.2 isolato.** Provare ConfigEntry e options flow, registry con ID personalizzati, reload parziale, traduzioni, Recorder, store guasto e restart. Verificare entrambi i percorsi PDC con e senza proprietario esterno. Separare le regressioni per caldaia, Avatto e split. M1 mantiene la pausa automatica e il bypass della richiesta manuale documentato in `SPECIFICA.md:13`. Prima di una prova di controllo, verificare la protezione effettiva dei dispositivi anche nei cicli manuali ravvicinati, mantenendo il comportamento manuale approvato.
5. **Preparare passaggio e ritorno operativo.** Prima confronto passivo di proposte, deadline e stati su cicli naturali. Poi prove a impatto limitato per un attuatore alla volta, con criteri di arresto e ritorno esplicito al proprietario precedente. Per la sospensione e lo storico già previsti dal contratto, completare il collegamento tra caldaia, PDC, override e registro prima di dichiararli disponibili. Esiste già un’implementazione parziale della sospensione nel runtime PDC a `heating_pdc_runtime.py:1440–1513`.

## 4. Miglioramenti consigliati

- Rendere esplicito nel collettore quali protezioni sono disabilitate e quali non sono leggibili. `heating_boiler_collector.py:507–516,761–770` tratta sezioni assenti o vuote come oggetti senza protezioni. Preservare la compatibilità legacy richiede di distinguerle da sezioni impreviste o parziali. Aggiungere test per `None`, attributi non pubblicati ancora e configurazione senza feature opzionale.
- Ridurre l’accoppiamento tra moduli. `heating_room_status.py:27–62` importa sette helper privati del collettore. Esporre un contratto di lettura per gli ingressi diagnostici condivisi e lasciare M1 isolato. Separare valutazione, ciclo di vita e adattamento HA nei manager grandi quando si affrontano i rilievi funzionali.
- Eliminare il ramo identico in `heating_room_status.py:839–847` e il parametro `eligible_collected` non letto da `_explain()` alle righe 718–728. Uniformare docstring e commenti al §10, ad esempio `_controller_condition()` dichiara che `observe` non conta come pass mentre `build_stages()` lo conta correttamente, come da contratto.
- Per il Recorder mantenere i campi diagnostici voluminosi esclusi come oggi. Misurare quante righe producono `current`, `target` e `gap` ancora registrati a fronte di sensori rumorosi. Aggiungere una prova end-to-end del tetto di 8192 byte con valori massimi dei binding e motivi PDC, verificando che l’errore si limiti alla stanza.

## 5. Verifiche eseguite e non eseguite

### Eseguite

| Verifica | Comando o metodo | Esito |
| --- | --- | --- |
| Identità del codice e M1 | `git rev-parse HEAD`, `git status`, `git diff --exit-code 99b8994 a8b62ab -- custom_components/versatile_thermostat/heating_boiler_policy.py` | Branch e commit corrispondono. Blob M1 identico, `40762eb307b78cd0b70374e61ceb9b3c14411a23`. |
| Estrazione di `active_valves()` | `git diff -U16 99b8994 a8b62ab -- custom_components/versatile_thermostat/heating_boiler_collector.py` e test collettore | Identiche espressione, ordine dei predicati, traversata delle valvole e deduplica successiva. Il rilievo I4 riguarda il nuovo chiamante del sensore. |
| Suite offline | `python -IB <runner> -q --noconftest --import-mode=importlib -p no:cacheprovider <16 file>` | **1317 passati, 2 saltati, 2 warning** in 65,11 s. Gli skip riguardano import del package HA e rigenerazione delle differenze di parità. I warning sono opzioni pytest-asyncio presenti nella configurazione, con plugin non caricati. |
| Caldaia, HA finto | `python -IB <runner> --script <piano>/m2b/fake_ha_scenarios.py` | **25/25**. Include rifiuto del control caldaia, zero servizi in observe e budget ripristinato. |
| PDC, HA finto | `python -IB <runner> --script <piano>/pdc/fake_ha_pdc.py` | **45 verifiche, 0 fallite**. In observe zero chiamate di servizio, inibizioni e scritture nello store PDC. Lo scenario con conferma esplicita dimostra l’avvio in control. |
| Stato stanza, HA finto | `python -IB <runner> --script <piano>/room-status/fake_ha_room_status.py` | **11 scenari, 33 verifiche, 0 fallite**. Include pubblicazione iniziale nello stesso giro e isolamento dell’errore di una stanza. |
| Prove mirate | `python -IB <runner> --stdin`, fixture sintetiche e sostituzione delle porte | Riprodotti rilascio di inibizione con marcatore estraneo, riuso proprietà dopo cambio di binding, HEAT con master diventato indisponibile, heartbeat a 30 s, generazione stanza/caldaia condivisa con due timestamp, zero diagnostico su conteggio ignoto, partecipazione assente resa `na`, due riattivazioni scheduler e perdita del flush. |
| Sintassi, traduzioni e API HA | `compile()` in memoria, `git show <ref>:<json>` e confronto ricorsivo, lettura della wheel HA 2026.11.2 | 76 file Python compilati. Le 20 chiavi foglia nuove per lingua coincidono, con `{room}` in entrambe. Restano invariati i 36 scostamenti di chiavi solo en e i 2 solo it presenti a `99b8994`. |

`<piano>` indica `plans/heating-orchestration-2026-11-24`. Il venv richiesto non era presente al percorso indicato. Ho usato Python 3.15.2, pytest 9.0.3 e Jinja2 3.1.6 dalla cache locale, con un venv esterno al fork. Il runner temporaneo configura un event loop senza socket di risveglio per le sole prove simulate, perché il socketpair locale si bloccava nel sandbox. Il codice dei test e dei tre harness è quello del commit. La suite è stata eseguita su una copia estratta in temporaneo perché diversi test avviano sottoprocessi con `-I`, che ignorano le variabili d'ambiente per la bytecode cache.

Il gruppo offline corrisponde ai file `tests/test_heating*.py` meno `test_heating_orchestration.py`, `test_heating_failure_detection.py`, `test_heating_pdc_ha_integration.py` e `test_heating_pdc_manager.py`. La prima raccolta ha identificato quest’ultimo come dipendente da `homeassistant` e dalle fixture `hass`/store. Nessun test di quel file è stato eseguito. I numeri non sono direttamente confrontabili con il riferimento 1328/1, che include un ambiente o un insieme di test diverso.

L'ispezione della wheel HA 2026.11.2 conferma le API che il sensore usa per ID stabile, placeholder e attributi esclusi dal Recorder in `helpers/entity.py:593–645,739–744,989–994`, il filtro per ConfigEntry in `helpers/entity_registry.py:2840–2844` e il supporto alle coroutine di unload in `config_entries.py:1372–1385`. Il sensore usa il suffisso previsto dal contratto e i campi voluminosi elencati al §2 sono tutti esclusi in `sensor.py:1298`. [Classe Entity](https://contributors.home-assistant.io/core/entities/entity-class/) · [Nomi tradotti delle entità](https://contributors.home-assistant.io/core/quality-scale/entity-translations/)

In entrambi i modi `observe` il percorso di comando viene interrotto nel manager caldaia a `feature_heating_orchestration_manager.py:974–979` e nel runtime PDC a `heating_pdc_runtime.py:824–826`, con un’ulteriore guardia alle porte PDC. La caldaia può scrivere il proprio contesto e, se proviene da un precedente control con un comando in volo, registrare un evento di ripristino nel logbook (`feature_heating_orchestration_manager.py:653–655,720–725,1235`). Il perimetro è la nuova orchestrazione. Le funzionalità VTherm preesistenti hanno un proprio ciclo di vita.

### Non eseguite

- Runtime Home Assistant reale o isolato. Non eseguiti `tests/test_heating_orchestration.py`, `tests/test_heating_pdc_ha_integration.py`, `tests/test_heating_failure_detection.py`, `tests/test_heating_pdc_manager.py` e `tests/test_room_*.py`. Le riproduzioni override e registro importano direttamente i runtime e usano porte finte.
- Installazione, comandi fisici, hassfest, CI e suite integrale di VTherm.
- Rigenerazione della parità via Node. Il test ha confrontato i 77 scenari con il riferimento salvato nel fork. Card e SDK non sono stati consultati o modificati.

## 6. Punti aperti

- La lettura della wheel prova la disponibilità delle API HA 2026.11.2. La compatibilità del ciclo completo con registro, Recorder, traduzioni visualizzate, reload parziale e dipendenze resta da verificare in HA isolato.
- Le riproduzioni PDC e caldaia provano richieste emesse dalle porte. Conferme dei dispositivi, timeout effettivi, provenienza SmartThings e riordino degli eventi dipendono dall’integrazione e dall’impianto.
- Non ho verificato la revisione della card rispetto agli hash delle fixture, né la versione effettivamente installata. Il risultato riguarda il commit indicato.
- Ho verificato le protezioni della nuova orchestrazione e le sue interfacce con VTherm. Le parti upstream estranee al diff sono state lette dove definiscono un comportamento da cui dipende l’orchestrazione.
