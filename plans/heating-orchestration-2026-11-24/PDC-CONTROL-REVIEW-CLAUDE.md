# Review Claude del controllo PDC (working tree)

Data: 25 novembre 2026, Europe/Rome. Revisione **in sola lettura**: nessuna modifica a sorgenti, impianto, installazione, riavvii, commit o push. L'unico file scritto è questo. Base: HEAD `c838def` + working tree non committato (21 file tracciati modificati, più `tests/test_heating_pdc_control_completion.py`, `tests/test_heating_pdc_flow_unit.py`, `pdc/verify_control_completion.py`, `pdc/offline_pytest.py` non tracciati).

Letti: `AGENTS.md`, `PDC-CONTROL-OPENCODE.md`, `PDC-CONTROL-CODEX-ESITO.md`, `PDC-CONTROL-PROVA-STANZA.md`, il diff di `heating_pdc_runtime.py`, `feature_heating_pdc_manager.py`, `config_flow.py`, `config_schema.py`, `sensor.py`, `__init__.py`, `heating_pdc_collector.py`, `heating_boiler_command.py`, `feature_heating_orchestration_manager.py`, e per intero la macchina `heating_pdc_transaction.py` e le parti collegate di runtime/collector/policy. In `repos/versatile_thermostat` non esiste un `AGENTS.md` o `CLAUDE.md` (solo `.github/agents/*.agent.md`, non applicabili a questa review).

## Esito in breve

Il disegno è solido sui punti chiave del passaggio radiatore ↔ split: l'inibizione si prende e si conferma prima di HEAT; il ritorno richiede split **e** VTherm PDC `off` con attesa del HEAT in volo (`heat_settle_s`); il radiatore si rilascia solo se l'inibizione è posseduta e dopo OFF confermato; il recupero dopo riavvio non rilascia mai sulla base del solo stato salvato; `unknown`/`unavailable` non valgono `off` nell'interblocco. I gate restano chiusi (`PDC_CONTROL_AVAILABLE = False`, `HO_PDC_CONTROL_ENABLED = False`, il manager passa `allow_control=PDC_CONTROL_AVAILABLE`).

Non è pronto per la prova su stanza. Ho trovato **3 problemi da risolvere prima**, 4 di media gravità e alcune note. Nessun problema richiede di cambiare le soglie M3.

Verifica riproducibile: con il runner offline di Codex, `tests/test_heating_pdc_control_completion.py` + `tests/test_heating_pdc_runtime.py` → **112 passed** (non ho rieseguito l'intera suite da 1122). I test HA reali restano non eseguiti, come già dichiarato.

## Problemi da risolvere prima della prova (alta gravità)

### A1 — Store non interpretabile: nessun attore protegge un PDC eventualmente acceso e non c'è via d'uscita

Evidenze:
- `heating_pdc_runtime.py` `async_run_pass` (~698-700): con `_load_errors` non vuoto, ogni stanza pubblica `error` e **ritorna senza `_control_pass`**; vale per entrambe le stanze anche se la transazione di una è stata caricata correttamente.
- `_save` (~493-495) rifiuta di scrivere finché `_load_errors` esiste, anche allo stop; `_load_errors` non viene mai svuotato nella vita del processo.
- Il requisito del control è che tutti i proprietari legacy siano `off`: quindi, con store corrotto dopo un riavvio, un VTherm PDC in HEAT e un radiatore inibito dal fork restano **senza nessun controller** (il legacy è spento, il fork è fermo). Nessuna notifica persistente (solo `return_unconfirmed` ne ha una).
- Il runbook vieta di cancellare lo store per forzare un ciclo, ma non offre alternative.
- Rischio aggiuntivo di falso positivo: `_load_room` (~539-541) tratta come corrotto `started_at`/`phase_started_at`/`heat_sent_at` > `now + 2 s`. Un HA che si avvia con orologio indietro rispetto all'ultimo salvataggio (NTP non ancora sincronizzato) blocca il control fino al riavvio successivo, senza possibilità di recupero a orologio corretto.

Prove mancanti: nessun test copre "store corrotto con PDC in HEAT" né "orologio che si riallinea dopo l'avvio" (il test `test_future_transaction_clock_blocks_recovery...` verifica solo il blocco).

Proposta: vedi task T1, T2.

### A2 — L'interblocco blocca anche gli OFF di sicurezza, con proprietario solo "transitorio"

Evidenze:
- `_blocked_by` (~646) considera bloccante qualsiasi stato diverso da `off`, incluso `unavailable` e **assente** (`lookup(...)` senza stato).
- `_command` (~915), `_execute` (~934), `_emergency_off` (~1244), `_do_release` (~1025) ricontrollano l'interblocco: quindi anche `set_hvac_mode off` di PDC/split, il ritorno per finestra aperta, protezioni, override e sospensione generale sono inviati **solo** se tutti i proprietari sono `off` in quell'istante.
- `async_run_pass` (~701-705) in stato bloccato chiama `_cancel_wake` e pubblica `blocked`: nessun timer di ripresa; riparte solo con un `state_changed` di un proprietario (i proprietari sono tra le entità osservate: confermato in `configured_entity_ids`).
- Casi realistici: ricarica delle automazioni (entità rimossa/ricreata per qualche istante), script del legacy in esecuzione dai pulsanti Oikos (`on` per tutta la durata), avvio di HA con entità non ancora create. A sessione in corso il fork resta congelato con PDC in HEAT e radiatore inibito; nessuna notifica, nessun limite di tempo. La semantica "il proprietario esterno è responsabile" (Codex esito, limite 2) vale se `on`, ma non se il proprietario è solo `unavailable`/assente.

Decisione di Carlo del 25/11/2026: tolleranza breve e poi solo OFF di sicurezza per proprietario non disponibile; congelamento totale se un proprietario è `on` (dettagli in T3). **Implementata il 25/11/2026** (vedi "Implementazione T3" in fondo). Come minimo serve una notifica quando una transazione non idle resta bloccata oltre una soglia.

### A3 — Una lista proprietari **vuota** passa da observe a control senza dichiarazione esplicita

Evidenze:
- `config_flow.py` `async_step_heating_pdc_add_room`: fuori da `control` (`mode != HO_MODE_CONTROL`) scrive comunque `room["external_owner_entity_ids"] = list(user_input.get(...) or [])`, cioè **`[]` esplicito** in ogni stanza creata in observe (il filtro iniziale scarta i vuoti, poi li riaggiunge).
- `heating_pdc_collector.py` (~271-275): `owners_declared` è vero per `[]`; il parser non distingue una lista vuota messa dal flow da una dichiarata dall'operatore.
- Conseguenza: passando da observe a control (stessa stanza, stessi dati) il percorso "richiede lista esplicita" è aggirato. Con `[]` `_blocked_by` è sempre vuoto e l'interblocco non protegge nulla; il passo di conferma mostra solo `stanza: -` come placeholder.
- Nella release attuale è innocuo (gate chiusi), ma è esattamente il difetto che l'incarico del task 3 voleva escludere.

Test mancante: observe-con-stanze → control con stesse stanze deve rifiutare.

## Problemi di media gravità

### M1 — `_do_inhibit` lascia valere il possesso quando la lettura iniziale è ignota e nulla è stato inviato
`heating_pdc_runtime.py` ~960-985: con `inhibited_now` `None` non viene chiamato `set_inhibition`, `transitioned` diventa `False`; poi `record_inhibition_result` (transaction ~490) con `inhibited_after` ancora `None` risponde "il possesso dichiarato prima resta" (`PdcStep(tx, reservation)`). Il possesso `inhibition_owned=True` resta senza che il fork abbia posto nulla. Se più tardi un altro attore inibisce e la valvola è chiusa, `radiator_stopped` diventa vero, la sequenza prosegue fino a HEAT e il rilascio finale chiama `unset_inhibition` su un'inibizione non sua. La finestra è stretta (`_start` richiede lettura `False`, ma c'è un `await _save()` tra lettura e `_do_inhibit`), per questo media e non alta. Correzione piccola: lettura iniziale ignota → rinuncia con motivo, senza possesso.

### M2 — Rilascio del radiatore senza tetto, senza notifica e con diagnostica che scompare
- `observe` fase `releasing` (transaction ~724-733) ritenta ogni `release_retry_s` = 30 s senza limite; non esiste notifica equivalente a `return_unconfirmed`. Un radiatore che non si libera resta spento per sempre (la stanza non scalda).
- `async_run_pass` (~672) azzera `last_error` a ogni passata, mentre i rilasci avvengono ogni 30 s e le passate ogni 15 s: `release_not_confirmed`/`release_failed` compaiono solo nella passata che li produce e spariscono in quella successiva.
- `_do_release` (~1027-1030) legge `inhibited` **subito dopo** `await unset_inhibition`; se il VTherm aggiorna l'attributo in modo asincrono si ottiene un falso `release_not_confirmed` (innocuo ma rumoroso, +30 s). Una breve attesa dello stato con tetto sarebbe più corretta.

### M3 — Errori degli specchi: pubblicati come `error` mentre la sessione è sana, e cancellati alla passata dopo
`_mirror._failed` (~1071-1076) imposta `last_error` e **pubblica `STATE_ERROR` come stato della stanza** anche con sessione in corso; la passata successiva azzera l'errore, ritenta e lo ripubblica: il sensore diagnostico passa da `error` a stato normale in modo ciclico finché l'helper è `unavailable`. Un ritorno silenzioso resta nei rami iniziali di `_call` (~1079-1080: epoca superata/valore non più desiderato/interblocco perso) e nelle eccezioni (~1089), che non richiedono una nuova valutazione: la riconciliazione dipende dalla prossima passata (tick 15 s).

### M4 — Lavoro non PDC nel working tree, con formato di store modificato
`heating_boiler_command.py` (persistenza del budget invii, `shutdown` non azzera più il budget) e `feature_heating_orchestration_manager.py` (nuova chiave `command_budget` nello store caldaia, `adopt_publishers`) non fanno parte del controllo PDC e toccano la caldaia in produzione (observe). `PDC-CONTROL-CODEX-ESITO.md` dichiara "nessuna modifica alla barriera caldaia" e cita regressioni caldaia (459 passed), ma non spiega questa modifica né la sua provenienza; la modifica è additiva ma cambia cosa viene salvato e come ripartono i tentativi dopo un riavvio. Va riletta e confermata o separata dal lavoro PDC prima di costruire una release; non l'ho rivalutata nel merito.

## Note e punti minori

1. `_other_view` (~601-610): per un'altra stanza bloccata si legge l'helper legacy, ma se la stanza altra ha una transazione fork non idle e il suo specchio non è ancora scritto (bloccato dall'interlock), questa stanza può vedere "libero" finché lo split dell'altra non è osservato attivo. La prenotazione condivisa non è atomica con il legacy (già dichiarato). Da coprire con un test.
2. `_cleanup_removed_pdc_sensors` (`sensor.py`) rimuove **tutti** i sensori diagnostici PDC dell'entry quando la sezione PDC è assente o senza stanze (anche con modo `disabled` temporaneo). È coerente con "non più configurati", ma viene eseguita a ogni setup; un cambio temporaneo a disabled cancella nome/storico dell'entità. Valutare se limitarlo a stanze effettivamente rimosse.
3. `_classify_later` (preesistente) incrementa `classify_pending` per **ogni** `state_changed` dello split, anche per soli attributi; `_control_step` ritorna presto finché `classify_pending > 0`. Con `CLASSIFY_DELAY_S = 1 s` basta un flusso di aggiornamenti più fitto di 1 s per sospendere ritorno, scadenze e protezioni. Poco probabile con SmartThings; da misurare nella prova osservata.
4. `adopt_publishers` (PDC e caldaia): corretto e protetto da `previous.is_running`; l'estrazione dei contenitori `_publishers` condivisi è sensata. Nessun problema trovato.
5. `PDC-CONTROL-PROVA-STANZA.md`, tabella dei proprietari: l'automazione protezioni è da "disattivare soltanto quando il fork sostituisce le protezioni" **e** da includere nell'interblocco. Un proprietario nell'interblocco `on` blocca l'intero fork: se resta attiva, il control non parte mai; se si spegne, le protezioni dipendono interamente dal fork, congelabile come in A2. Va chiarito nel runbook.

## Punti verificati e senza difetto trovato

- Avvio: `request_start` rifiuta inibizione presente/ignota, altra PDC occupata/ignota, override attivo; il possesso viene salvato (`_begin_session`) prima di toccare il radiatore; `in_gestione` ON è programmato solo dopo il salvataggio (`_finish_pass`/`_begin_session`).
- HEAT: `_do_heat` rivalida radiatore fermo (valvola OFF, VTherm OFF, inibito), setpoint confermato, condizioni, assenza di sospensione; HEAT tardivo gestito da `heat_settled`.
- Finestra/potenza: `unsafe_during_start` e `window_running_ok` trattano finestra non `off`, potenza mancante, vecchia o ≥ soglia come motivo di ritorno; finestra `unknown` durante la marcia ferma la sessione (prudente).
- Ritorno: `off_confirmed` richiede split e PDC `off` + HEAT assestato; `releasing` ritorna al ritorno se split/PDC tornano attivi; nessun rilascio senza OFF confermato.
- Riavvio: `recover` fa passare ogni fase non idle per la via sicura con token nuovo; il recupero attende dispositivi leggibili al massimo 120 s, poi tenta OFF e non rilascia mai alla cieca.
- Override: la copia split → PDC (`_sync_pdc`) rilegge la sorgente prima di ogni servizio e non copia un HEAT vecchio su un nuovo OFF; i comandi fork non partono con `user_id`, quindi non avviano un override come utente.
- Osservazione: `_mirror` e `_command` ritornano se la modalità non è `control`; nessuna scrittura in observe (coerente con i test citati).

## Prossime piccole task (ordine consigliato, ognuna indipendente)

| # | Task | Dimensione | Chiude |
| --- | --- | --- | --- |
| T1 | Store non interpretabile: notifica persistente e diagnostica chiara; definire il comando/procedura di sblocco (es. servizio di "adozione" dello store con conferma, o ripristino da backup) e correggerne il runbook | piccola-media | A1 |
| T2 | Controllo orologio: confrontare con l'ora corrente solo quando l'orologio è sincronizzato, oppure tollerare e ricontrollare dopo qualche minuto invece del blocco permanente; test con orologio che avanza | piccola | A1 |
| T3 | **Decisione di Carlo (25/11/2026): tolleranza e OFF.** Proprietario `on` → resta il congelamento totale. Proprietario `unavailable`/`unknown`/assente (mai `on`) → tolleranza breve (proposta 30 s, da fissare nel codice come costante); scaduta, sono ammessi **solo** gli OFF di sicurezza di VTherm PDC e split, con il limite di frequenza già esistente. Restano vietati HEAT, inibizione, rilascio del radiatore, setpoint, specchi helper e nuove sessioni. Notifica persistente se la stanza resta bloccata con transazione non idle oltre la tolleranza. Test: flapping di un proprietario durante `returning` e `running`, proprietario `on` che resta vietato, nessun comando non-OFF nella finestra di tolleranza scaduta | piccola | A2 |
| T4 | Non scrivere `[]` automatico nelle stanze observe; in control rifiutare l'assenza e (se confermata la scelta) anche la lista vuota; test observe → control | piccola | A3 |
| T5 | `_do_inhibit`: lettura iniziale ignota → nessun possesso e ritorno motivato; test | piccola | M1 |
| T6 | Rilascio: attesa limitata dello stato dopo `unset_inhibition`, notifica dopo N tentativi, `last_error` persistente finché la causa c'è; test | piccola | M2, M3 |
| T7 | Chiarire in repository la provenienza delle modifiche a `heating_boiler_command.py` / store caldaia (separarle o validarle con le regressioni caldaia) | piccola | M4 |
| T8 | Test HA reali (manager, flow, integrazione) in ambiente compatibile e regressioni centrale/Avatto/inibizione — prerequisito già indicato da Codex | media | limite aperto |
| T9 | Correggere le tabelle del runbook (protezioni nell'interblocco, nessun cancellamento dello store) dopo T1 e T3 | piccola | nota 5 |

## Limiti di questa review

- Review di codice e test letti, senza esecuzione su Home Assistant, senza accesso all'impianto e senza consultare lo storico live. I rischi A2, M1 e M3 sono dedotti dal flusso del codice; non ho scritto test che li riproducano (vincolo di sola lettura sul repository).
- Eseguito solo il runner offline su due moduli (112 passed); non è una prova del loop HA né di Python 3.14 usato dall'impianto.
- Non ho riletto le entità reali: i nomi di Camera Carlo nel runbook restano da riconfermare come già indicato.
- Nessuna modifica all'impianto, nessuna installazione, nessun riavvio, nessun commit o push.

## Implementazione T3 (25/11/2026, solo locale)

Su richiesta di Carlo (tolleranza 30 s). Nessuna installazione, nessun gate aperto, nessun commit o push; impianto non modificato.

- `heating_pdc_runtime.py`: costante `OWNER_GRACE_S = 30.0`; `_owner_blockers` separa proprietari "hard" (stato diverso da off/unknown/unavailable/assente, in pratica `on`) da "soft"; l'orologio di tolleranza parte solo se **tutti** i bloccanti sono soft e riparte se un proprietario torna `off`; scaduto il tempo, e solo con una transazione non idle della stanza, `_command` lascia passare esclusivamente `climate.set_hvac_mode off` verso VTherm PDC e split (`_safety_off`, limitato da `PROTECTION_EMERGENCY_GAP_S`). HEAT, setpoint, inibizione, rilascio, specchi helper e nuove sessioni restano bloccati; con un proprietario `on` il congelamento è totale. La stanza bloccata ora programma un risveglio alla scadenza (prima cancellava ogni timer); notifica persistente `<stanza>_pdc_owner_unavailable` a tolleranza scaduta, rimossa al ritorno del proprietario.
- Test aggiunti in `tests/test_heating_pdc_control_completion.py` (8 casi): nessun comando entro 29 s e solo OFF dopo, per `unavailable`/`unknown`/assente; radiatore mai rilasciato mentre bloccata; ritorno sicuro e notifica rimossa al rientro; `on` mai scavalcato; `unavailable` → `on` resta congelato; flapping azzera l'orologio; nessun OFF senza sessione della stanza; solo OFF verso PDC/split passano l'interblocco dopo la tolleranza.
- Risultato: suite PDC + caldaia offline **1130 passed, 1 skipped** (prima 1122); `py_compile` del runtime riuscito. Restano non eseguiti i test HA reali.
- Limiti: la tolleranza non copre il caso "store illeggibile" (A1) né le stanze senza transazione (da progetto); dopo gli OFF di sicurezza la transazione resta ferma finché il proprietario non è di nuovo `off`, con radiatore ancora inibito.
