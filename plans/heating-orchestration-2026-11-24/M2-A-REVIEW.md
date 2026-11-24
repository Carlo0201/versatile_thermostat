# M2-A - Review indipendente (collector e observe caldaia)

Data: 24/11/2026. Revisore: sessione Claude indipendente. Nessuna modifica a codice, test, baseline o piano esistenti; nessun accesso a Home Assistant, nessun test HA, nessun commit. Verifiche ad hoc solo nella scratchpad di sessione (`.../scratchpad/rev_m2a/`).

## Verdetto

M2-A e' **accettabile come sola osservazione dal punto di vista del codice di produzione**: nessun rilievo bloccante. Il collector e' in parita' con `raw` su tutto cio' che ho potuto provare, l'observe non ha alcun percorso di scrittura verso altri attuatori o helper, il lifecycle e' coerente. Restano **7 rilievi importanti** (tre riguardano i test HA scritti, che contengono errori certi, un'asserzione mutante non coperta, due rischi di robustezza/rumore e un insieme di requisiti da chiudere prima di M2-B) e **9 minori**. Prima di M2-B i test HA vanno eseguiti (richiede un'autorizzazione di Carlo, che oggi li ha vietati) e corretti.

Totale: 0 bloccanti, 7 importanti, 9 minori.

## Hash dei file revisionati (SHA-256, tutti coincidenti con M2-A-ESITO)

| File | SHA-256 |
| --- | --- |
| heating_boiler_policy.py (M1, congelato) | edf7e9007ac44166bfc56a3ad8c74dd6b167af6f4ed2760831d865443c27945b (invariato) |
| heating_boiler_collector.py | 1b2224c088ae67fef4272c3c713d9c9deeb1f387fd1ded335dc1db0b0bc42ed4 |
| feature_heating_orchestration_manager.py | ea534d32b50b8db42b0a54def8a3b5fd244142b3cede95ada621db337bf0fd19 |
| config_flow.py | ea146deaf405140fdbc23559e55e6f4192c0dfeb69cd2fb3a307a29ece9814ef |
| config_schema.py | b880e6fdad5c523d10591ab5d79d46cbda39caffc44072042ed961a4177c6625 |
| sensor.py | c73e5a9aaaf4d04a615d1328081c512d1fd4fb8a92058c2635a7cdd4bee0420b |
| __init__.py | 1adc8e4ca6978f08d1496b52d72641ccd94427033f914e649ab7291f60853248 |
| const.py | 0eeaf3763d1bab3aa3919dbd448afda18d2ec94337917ebb02cc958fa53e34b1 |
| vtherm_central_api.py | ae0f4663c0fdb5922395f2d359d300a6b1448f6b0a7c515a172ef57efbf9c93b |
| translations/en.json | fec362ec23d09e89bb87d9189edb1cb1b0e27738cbf9c712925edccd77444b7d |
| translations/it.json | ac59af8fb81cd8d7d957a53b3a9465eb5f2e11b91ce08aed372fece72eff339a |
| tests/test_heating_boiler_collector.py | 05f36e0dd75fd3ffc9f326fe97e7adecb36bd7ffeb73a0b464581e3f7b3f64df |
| tests/test_heating_orchestration.py | 55846e34a5f24568fa5c4836ff6ff7a8567340c5eb1f1c290a68479ee8ff8917 |
| baseline/caldaia_controllo_stabile_delle_richieste.json | 4ee0fedd33837cd227f0580c84468a452d16c56de84962bada6968363f4c298a (= index.json) |
| baseline/boiler-observation-cases.json | fe3f09524669d62ed6ea2acfcd7c6aed60169e046fe2abf9a9897949fd183c07 (= index.json) |

Tutti i file passano `ast.parse`.

## Test puri eseguiti

Comando richiesto (radice del fork, venv M1):
`python -m pytest -q --noconftest --import-mode=importlib -p no:cacheprovider tests/test_heating_boiler_collector.py tests/test_heating_boiler_policy.py`
Esito: **342 passed, 1 skipped** (343 raccolti; lo skip e' quello di M1). Nota: M2-A-ESITO scrive "343 passed" e "collector 200": in realta' collector 199 + policy 143 con 1 skip (rilievo M-1).

## Evidenze di parita' del collector con `raw` (punto 1)

Fatti verificati da me:

1. Lettura riga per riga di `raw` (baseline) contro `heating_boiler_collector.py:603-804`. Corrispondono: validita' del conteggio (`:603-619`, float finito, >=0, intero, `ids` sequenza non stringa/mapping, `count == len(unique(ids))` con `unique` case-insensitive come Jinja, `:512-527`); qualificazione stanza (`:706-743`: heat/auto, `is_ready == True`, `is_initialized == True`, `is_used_by_central_boiler == True`, finestra `bypass or (window_state == off and sensor str and is_state(sensor, off))` a `:622-631`, PDC session e split entrambi `off` a `:634-645`, `inhibited` default False confrontato con `== False` a `:724`, `safety_state`/`overpowering_state` default `off` `:727-731`, `avatto_room_demand != False` a `:732`, temperature `float(none)` `:734-735`, eta' `0 <= age <= 3600` con default epoch 0 `:739-743`); valvole (`:750-752`, `in ids`, heat/auto, `hvac_action == heating`); `gap = round(target-current, 3)` `:756`; `pilot_ok` `:757`; `count = len(unique(valves))` `:780`; `ok = valid_count and central in (Auto, Heat only)` `:778`; consenso `all(on)` `:774`; master/manual/pilot come `states()` stringa, `unknown` se assente `:437-442`.
2. Ho **rieseguito** l'harness `m2/verify_m2_collector.py` su una copia in scratchpad (stesso codice, solo percorsi/uscita ridiretti): hash collector attuale `1b2224c0...` (quello salvato in `verify_m2_collector-result.json`, `06319855...`, e' di prima del reformat Black, quindi stale), 17 fixture ok (reso e salvato), **20.000 casi generati con seed 777 ok** (il worker ne aveva 5.000 con seed 20261124).
3. 40 mutazioni mirate di uno snapshot sano confrontate con il motore Jinja2 reale (stringhe numeriche con spazi, bool come temperatura, timestamp naive/con spazio/solo data/epoch intero, bypass `1.0`, conteggio `1e1`/` 9 `/`9_0`, id attivi con maiuscole, valvole duplicate in `underlying_entities`, `underlying_entities` stringa/mancante, sezioni malformate, arrotondamento 21.0005, eta' esatta 3600 / 3600.0004 / futuro di 1 ms, valvola condivisa tra stanze, sessione PDC mancante, manuale mancante): **37 identici, 3 differenze, tutte dichiarate come irrigidimenti** (conteggio `inf` -> template solleva `OverflowError`; sezione `specific_states` stringa -> template `UndefinedError`; temperatura NaN -> output non letterale). Nessuna divergenza non dichiarata.
4. Mutation testing dell'harness: 17 mutanti del collector (eta' `>` -> `>=`, futuro `<` -> `<=`, `pilot_ok` truthy, bypass ignorato, `unique` case-sensitive, solo `heat`, inibizione default True, consenso `any`, `avatto` `== True`, `hvac_action != idle`, finestra solo sensore, `Stopped` ammesso, conteggio senza `unique`, sessione PDC ignota accettata, ...): **14 uccisi, 3 sopravvissuti**. Sopravvissuti: `count == len` -> `count <= len` (sopravvive anche ai 199 test del collector: vedi I-3), `round(..., 2)` (ucciso dai test puri), `is_ready` truthy (ucciso dai test puri). L'harness e' quindi discriminante sulle soglie e sui filtri principali.

Giudizio di indipendenza: **parziale ma sufficiente per observe**. Il motore e' il Jinja2 vero e il template e' quello normativo (con controllo che i due `raw` della baseline siano identici, `collector_cases.py:raw_template_source`), ma gli helper HA (`is_state`, `state_attr`, `has_value`, `as_timestamp`, `states`, `now`) sono emulati dallo stesso autore del collector; `float`, `round`, `as_timestamp` di HA non vengono esercitati (la parita' col motore template di HA non e' stata testata, `test_heating_orchestration_parity.py` non esiste). Il confronto copre solo `raw`; codici di esclusione, `rooms[].valves` e diagnostica sono provati solo dai test puri.

## Evidenze observe senza effetti (punto 2)

- `grep` su `heating_boiler_collector.py` e `feature_heating_orchestration_manager.py` per `async_call`, `.services`, `async_set`, `async_fire`, `async_remove`, `create_task`, `hass.data`: nessuna occorrenza. Gli unici accessi a HA sono `hass.states.get`/lettura dello stato come mapping, `hass.bus.async_listen_once` (STARTED/STOP, `manager:269-270`), i tracker di eventi/tempo, `Store` con chiave propria, e la scrittura dello stato della propria entita' sensore (`sensor.py:1178-1182`).
- Il seed legacy e' solo `hass.states.get(legacy)` + `json.loads` (`manager:354-360`); nessuna scrittura degli helper legacy.
- La diagnostica non viene ascoltata e un'eventuale configurazione che la usa come ingresso viene rifiutata a runtime (`manager:444-448`).

## Rilievi

### Difetti certi

#### Importanti

**I-1 - `last_error` sticky e mascherante (manager).** `manager.py:366` (`legacy_seed_invalid`), `:311`/`:316` (`store_load_failed`, `store_invalid`) assegnano `self._last_error` e nessun codice lo azzera; `:477-479` usa `self._last_error` e **scarta** `temperature_unit_unsupported` se `_last_error` e' gia' valorizzato.
Scenario: legacy configurato ma entita' assente o `unknown` al primo avvio -> `last_error = "legacy_seed_invalid"` per tutta la vita del manager anche se l'osservazione e' sana, e un'unita' non Celsius successiva non viene mai segnalata. Anche il significato di "ultimo errore" del contratto (§2) viene meno.
Correzione: separare errori di avvio (con scadenza o azzerati dopo la prima valutazione valida) da errori per-valutazione; calcolare `last_error` ad ogni valutazione con priorita' esplicita (config > unita' > seed/store), o accodare piu' codici.

**I-2 - Il test HA ha asserzioni che falliranno con certezza (`tests/test_heating_orchestration.py`).** Verificato per ragionamento sul codice e, per il punto 2, con M1 reale:
1. righe 235 e 300 (`last_error is None`): `base_snapshot` non contiene `LEGACY` (`tests/test_heating_boiler_collector.py:93-110`) e `SECTION` lo configura -> `legacy_seed_invalid` (I-1).
2. riga 317 (`last_error == "temperature_unit_unsupported"`): stesso motivo, `_last_error` e' gia' `legacy_seed_invalid`.
3. riga 484 (`cycle["off"] == clock.now - 600`): il ciclo seed ha `m="normal"` e il master e' `off`; M1 azzera l'ancora (`saved != 'off'` -> `stopped = n`) e restituisce secondi interi. Provato: `off` = `int(now)`, non `now-600`.
4. riga 543 (`last_error == "store_invalid"`): `_seed_once` (`manager:353-366`) sovrascrive con `legacy_seed_invalid` perche' `LEGACY` non esiste; l'origine sarebbe comunque `new_observation` ma l'errore no.
5. riga 287 (`MASTER.state == "unknown"`): `put_states(hass, world(...))` a riga 278 riscrive tutte le entita' del mondo, compreso `MASTER = "off"`, dopo il ciclo che lo porta a `unknown`.
Correzione: aggiungere `LEGACY` al `world()` dei test che non lo testano (o toglierlo da `SECTION`), usare `int(clock.now)` e il valore atteso reale per `off`, non riscrivere `MASTER` o spostare l'asserzione.
Altre asserzioni non verificabili a freddo (ordine non garantito) restano in I-6.

**I-3 - Parita' non protetta contro `count <= len(unique)` (harness e test).** `heating_boiler_collector.py:619` richiede `count == len(unique_ids)` come la baseline; il mutante `<=` sopravvive sia all'harness (3.000 casi) sia a tutti i 199 test del collector. Il generatore non produce mai "conteggio inferiore agli ID unici" (es. sensore `2` con tre ID).
Scenario: una regressione che accetti conteggi piu' bassi non verrebbe vista.
Correzione: aggiungere ai test puri e al generatore `count_state = len(unique) - 1`; chiudere con un caso nominale sulle stanze (deve produrre `count_invalid`).

#### Importanti (plausibili, non provati)

**I-4 - Rumore di pubblicazione e righe recorder.** `manager.py:492` pubblica ad ogni valutazione; ad ogni pubblicazione `generation` cambia e `evaluated_at` cambia, quindi lo stato HA e' sempre diverso (`sensor.py:1178-1182`). Le valutazioni sono scatenate da **ogni cambio anche di soli attributi** di ciascun climate osservato (temperatura corrente, `last_temperature_datetime`, ecc.), piu' il poll ogni 15 s. `_unrecorded_attributes` esclude gli attributi ma, a quanto ricordo del recorder, una riga di stato viene comunque scritta quando l'evento `state_changed` scatta; non l'ho potuto verificare in HA. Con 8 stanze e aggiornamenti VTherm frequenti il numero di eventi/righe puo' essere ben oltre i 5.760 al giorno del solo poll, contro "senza eventi ad ogni tick" della specifica.
Correzione proposta: coalescere con una finestra minima (es. >=1-2 s tra pubblicazioni dovute a eventi), oppure non pubblicare se decisione, ingressi e condizioni sono invariati e l'ultima pubblicazione ha meno di ~10 s (la freschezza UI e' -2..+45 s); mantenere il poll da 15 s come heartbeat.

**I-5 - Gestione eccezioni troppo stretta (`manager.py:228-233`, `:468`, `:437-494`).** Solo `(TypeError, ValueError, KeyError)` sono intercettati nel blocco di raccolta+M1; `_publish` itera i publisher senza protezione e `_resubscribe`/`_schedule_deadline` stanno dopo la pubblicazione. Un'eccezione di altro tipo (es. `AttributeError`, `AssertionError`, o un publisher che solleva) esce da `call_soon` (viene loggata dal loop, il poll successivo riprova) ma salta `_resubscribe`, quindi finestre e valvole appena scoperte non vengono ascoltate fino a un'altra valutazione riuscita, e lo stato resta con `evaluated_at` vecchio.
Correzione: intercettare `Exception` nell'intero corpo di `_evaluate` (pubblicando `error` con `evaluation_error:<tipo>`), proteggere ogni publisher separatamente, eseguire `_resubscribe`/`_schedule_deadline` anche dopo un errore del publisher.

**I-6 - Test HA con temporizzazione fragile e punti non provabili senza esecuzione.** `Clock.advance` (`tests:71-76`) usa un istante assoluto `real0 + elapsed`, ma `async_fire_time_changed` confronta con il tempo reale: i timer di intervallo vengono registrati a `real0 + delta` (delta = durata reale del setup). `test_recovery_polling_every_15_seconds` (361-374) richiede `delta <= 1 s` per far scattare il poll a 16 s, e `test_reload_never_duplicates_observers` (631-646) accumula tre reload prima di `advance(16)`: probabile flakiness. Inoltre `service_calls` filtra solo `persistent_notification` (162, 186, 208) senza sapere se il setup della centrale con caldaia nativa chiama altri servizi; `assert entity_id == ... or entity_id.endswith("_boiler_orchestration")` (253) e' debole; il listener `call_service` e' una lambda non `@callback` (269).
Correzione: far avanzare con `dt_util.utcnow() + timedelta(seconds=...)` (o `freezer`), e verificare la lista dei servizi chiamati dal setup della centrale prima di dichiararla "nessuna chiamata".

**I-7 - Requisiti da chiudere prima di M2-B (vedi punto 9).** Non sono difetti di M2-A ma oggi ostacolano control: `_evaluate` sincrono e monolitico (`manager:437-494`) senza fase di comando/riconferma; salvataggio ciclo ritardato di 30 s (`:339`) incompatibile con un comando che richiede l'ancora `off` gia' durevole; nessun controllo di proprieta' esclusiva rispetto all'automazione legacy; i reload della centrale (`__init__.py:129-143`, `update_listener:203-204`) ricaricano tutte le entry e rendono le stanze transitoriamente non idonee, con rischio di OFF spurio in control (la baseline lo ha solo all'avvio HA). Dettaglio nella sezione dedicata.

#### Minori (certi)

**M-1 - Inesattezze nell'esito (`M2-A-ESITO.md`).** (a) "343 passed" e "collector 200": reali 342 passed + 1 skipped, collector 199. (b) La nota "le liste `menu_options` dei test esistenti del config flow centrale sono cambiate" e' errata: `tests/test_config_flow.py` non e' modificato (nessuna differenza git) e nessun test esistente asserisce il menu della centrale (il test centrale a `test_config_flow.py:99` non controlla `menu_options`; tutti i `menu_options ==` riguardano flow non centrali). Quindi nessun test esistente si rompe per la nuova voce; manca per contro un test della nuova voce. (c) `verify_m2_collector-result.json` riporta l'hash del collector precedente al reformat: non corrisponde al file consegnato. Ho verificato io che la parita' regge sull'hash attuale.

**M-2 - Persistenza.** `Store(self._hass, HO_STORAGE_VERSION, key)` (`manager:244`) senza `async_migrate_func`: un futuro aumento di versione fara' sollevare `NotImplementedError` in `async_load`, non intercettato dalla tupla a `:309`, e l'`async_setup_entry` fallirebbe. Nessun `async_remove_entry` rimuove il file `.storage/versatile_thermostat.heating_orchestration.<entry_id>`. `first_origin` (`:327`) salva in realta' l'origine corrente. `_last_saved_cycle` (`:337`) viene aggiornato prima che la scrittura riesca: un errore di scrittura non viene ritentato finche' il ciclo non cambia.

**M-3 - Race stretta in `_on_deadline` (`manager:425-430`).** Il callback azzera `_deadline_unsub`/`_deadline_at` condivisi senza verificare di appartenere al timer corrente: se un vecchio callback e' gia' in coda quando viene ricreato il timer, il nuovo timer perde il riferimento (non puo' piu' essere cancellato e puo' accumularsi un duplicato). Gli effetti sono limitati alle richieste di valutazione (che sono protette dall'epoch).

**M-4 - Default per ingressi non configurati (`collector:771-773`).** `manual`/`pilot` non configurati valgono `off`, quindi l'automatico e' ammesso senza alcun filtro pilota; `inputs.manual`/`inputs.pilot` pubblicano `off` come per un ingresso reale. La scelta e' ragionevole (documentata come "facoltativo") ma la diagnostica non distingue "non configurato" da `off`. Proposta: pubblicare `null` o un marcatore, o dichiararlo nel contratto.

**M-5 - Fuso dei timestamp naive (`collector:490-509`).** I timestamp senza offset sono letti nel fuso HA configurato; il template li legge nel fuso del processo (come nota il docstring). Se il container non ha `TZ=Europe/Rome` la parita' puo' differire di un'ora sui soli timestamp naive. VTherm normalmente espone valori con offset: nessun effetto atteso, ma non e' stato provato.

**M-6 - Duplicazione di costanti.** `SELECTABLE_MODES` in `heating_boiler_collector.py:88` e `HO_SELECTABLE_MODES`/`HO_MODE_*` in `const.py:262-266` devono restare allineate a mano; l'ESITO stesso lo ricorda per M2-B. Preferibile una sola fonte (il collector e' puro, `const` lo puo' importare, non il contrario se si vuole mantenere il collector senza dipendenze).

**M-7 - Config flow, rifiniture.** (a) `_ho_check` (`config_flow.py:774-783`) richiede l'esistenza di tutte le entita' configurate al momento del salvataggio: una entita' che torna dopo il riavvio non e' configurabile; un errore su un binding centrale compare come `base` anche mentre si aggiunge una stanza (`:841`). (b) Salvare `mode=disabled` crea comunque la chiave e quindi il sensore `disabled` (nuova entita' per entry che solo apre il menu); e' dichiarato nell'esito ma cambia l'inventario entita'. (c) Una stanza si modifica solo rimuovendola e riaggiungendola. (d) La voce di menu compare per tutte le centrali esistenti (`:645-646`), cosi' come richiesto dallo scopo.

**M-8 - `entity_id` dipendente dalla lingua (`sensor.py:1139-1143`).** Con `has_entity_name` e nome tradotto, l'entity_id generato al primo inserimento dipende dalla lingua di HA (`..._boiler_orchestration` in en, `..._orchestrazione_caldaia` in it). L'ESITO lo dichiara; Oikos deve risolvere via registro entita'/`unique_id` o ricevere l'id configurato. Il test (`tests:253`) accetta due forme.

**M-9 - Ordine di unload (`__init__.py:218-224`).** Il manager viene fermato e rimosso dall'API prima di `async_unload_platforms`; se lo scaricamento delle piattaforme restituisce `False`, l'entry resta caricata senza manager, con il sensore fermo sull'ultimo stato. Rara. In piu' le `assert` nel collector (`:289`, `:749`) vengono rimosse con `-O` (nessun effetto pratico).

### Punti plausibili (non provati) gia' riportati
I-4, I-5, I-6, M-2 (parte `NotImplementedError`), M-3, M-5.

## Risposte puntuali ai punti richiesti

### 3. Lifecycle
- **Listener registrati una volta e ripuliti.** `async_start` (`manager:237-272`) registra intervallo, STARTED (solo se HA non e' running), STOP e stato; `_resubscribe` (`:387-405`) rimuove la sottoscrizione precedente prima di crearne una nuova e ignora la propria entita' (`:389`). `async_stop` (`:274-301`) annulla tutto, incrementa `_epoch`, annulla il pending `call_soon` e il deadline, salva se `_dirty`. Reload: `async_create_heating_orchestration_manager` (`vtherm_central_api.py:251-257`) ferma sempre il precedente; in `__init__.py:163-181` il manager nasce prima delle piattaforme e parte dopo `async_forward_entry_setups` (il sensore e' gia' collegato); in unload viene fermato prima del sensore (`:218-220`). Doppia chiamata di stop e' idempotente (`_stopped`).
- **Poll e coalescenza.** `async_track_time_interval(..., 15 s)` + un solo `loop.call_soon` per piu' eventi (`:372-378`), esecuzione solo se `epoch` coincide e `_running` (`:380-385`). Non ci sono sleep bloccanti.
- **Un solo clock.** `now = self._clock()` una volta (`:442`), passato a collector e M1; `build_boiler_diagnostics` rifiuta clock diversi (`collector:942-943`); `_store_data` rilegge il clock solo per `saved_at`.
- **Nessun loop a ritardo zero.** `_schedule_deadline` crea timer solo per scadenze strettamente future (`:417-419`); deadline passata o `None` (compreso `awaiting_consent`, dove M1 azzera la scadenza) non pianificano nulla, il recupero e' il poll. Duplicati con stato identico non generano eventi.
- **Reload della centrale.** Ogni modifica della centrale ricarica tutte le entry (`update_listener`/`reload_all_vtherm`): il manager viene ricreato una volta per reload e il precedente fermato; lo stato `Store` e' riletto. Nessun accumulo di listener tranne la race minore M-3.
- **Eccezioni.** Parziale, vedi I-5. Un errore di `Store` (load/save) non blocca start/stop per le eccezioni elencate (`:299-301`, `:309-312`), con il limite M-2.

### 4. Persistenza
`Store` versionato (v1, chiave per entry), `async_delay_save(30 s)` solo se il ciclo cambia (`:332-339`), flush su stop, nessuna scrittura diretta di `.storage`, seed legacy una sola volta e in sola lettura (`:346-368`), `cycle_origin` = `legacy_seed`/`local_restore`/`new_observation`. Restano I-1 (errore sticky), M-2 e l'osservazione che un seed non ripetibile dopo un primo avvio senza legacy e' voluto ma va dichiarato in sede d'uso.

### 5. Compatibilita'
- Entry senza chiave: nessun manager, nessun sensore, nessuna lettura (`__init__.py:166`, `sensor.py:90`); unico effetto visibile e' la nuova voce di menu in UI per le centrali (`config_flow.py:645-646`).
- `disabled` e' il predefinito (`collector:329-337`) e accetta qualunque altra chiave senza leggerla.
- `control` e valori sconosciuti sollevano `OrchestrationConfigError` (`:334-339`): il manager entra in `error`, `mode` effettivo `disabled`, `owner` `null`, non possiede nulla (`manager:140-148`, `:241`). Il flow non lo offre (`HO_SELECTABLE_MODES`).
- `menu_options`: nessun test esistente verifica il menu della centrale (vedi M-1b).
- Traduzioni: 111 chiavi nuove (`heating_orchestration*`, `ho_*`, `selector.heating_orchestration_mode`, `entity.sensor.heating_orchestration_boiler`) presenti in en e it, parita' completa, JSON valido, diff limitato alle sole aggiunte (virgole finali comprese).

### 6. Diagnostica
Attributi e codici conformi a CONTRATTI §2 (tutti i 23 campi presenti, `commanded_mode`/`last_command_at`/`last_confirmation_at` sempre `null`), `unique_id = <entry_id>_heating_orchestration_boiler` (`sensor.py:1167`), categoria diagnostica, stesso device della centrale (stessi `identifiers` degli altri sensori centrali), `_unrecorded_attributes` = conditions, rooms, excluded_rooms, evaluated_at, generation, cycle, inputs (`:1147-1157`), `json_safe` rimuove NaN/Infinity e limita liste a 64 e profondita' a 6 (`collector:843-857`), errore a 255 caratteri. Le unita' delle condizioni corrispondono agli id di M1 (`thermal_gap`, `valve_count`, `stability`, `cooldown`). L'entity_id e' derivato (M-8). Il solo scarto semantico: in errore di configurazione il `mode` pubblicato e' `disabled` (contratto: `observe` in observe), accettabile ma da dichiarare a Oikos.

### 7. Compatibilita' statica con HA 2026.11.2 (nessuna esecuzione)
API HA usate dal codice nuovo e valutazione:

| API | Dove | Rischio |
| --- | --- | --- |
| `homeassistant.helpers.event.async_track_time_interval`, `async_track_point_in_utc_time`, `async_track_state_change_event` | manager | Basso: API stabili. `async_track_state_change_event` e' ancora l'API consigliata; `async_track_point_in_utc_time` e' ancora presente. |
| `homeassistant.helpers.storage.Store` (`async_load`, `async_save`, `async_delay_save`) | manager | Basso; senza `async_migrate_func` (M-2). |
| `hass.loop.call_soon`, `hass.bus.async_listen_once` con coroutine, `CoreState`, `EVENT_HOMEASSISTANT_STARTED/STOP`, `callback`, `CALLBACK_TYPE` | manager | Basso/medio: `loop.call_soon` diretto e' tollerato ma e' il tipo di uso che HA scoraggia fuori dal core; alternativa `hass.async_run_hass_job`/`async_call_later(0)`. |
| `dt_util.utcnow`, `utc_from_timestamp`, `get_default_time_zone()` | manager | Basso (`get_default_time_zone` esiste da 2024.6). |
| `hass.config.units.temperature_unit` | manager | Basso. |
| `SensorEntity`, `EntityCategory` (`homeassistant.const`), `DeviceInfo`, `_attr_translation_key`, `_attr_has_entity_name`, `_unrecorded_attributes` | sensor | Basso. |
| `ConfigEntry.async_on_unload` con coroutine, `add_update_listener`, `async_forward_entry_setups`, `async_unload_platforms`, `async_update_entry` | `__init__.py` | Basso (gia' usati dal fork). |
| `selector.EntitySelector(EntitySelectorConfig(domain=list, multiple=...))`, `SelectSelector(...translation_key...)`, `TextSelector`, `BooleanSelector` | config_schema | Basso. |
| `FlowHandler.async_show_menu`, `async_show_form`, `OptionsFlow` con `self.config_entry` | config_flow | Basso/medio: il fork usa gia' `OptionsFlowHandler(config_entry)` e `self.config_entry` in `async_step_finalize` (preesistente, non introdotto qui); le versioni recenti di HA stanno restringendo la gestione di `OptionsFlow.config_entry`, ma e' un rischio pre-esistente del fork. |
| `entry.runtime_data` | - | Non usato. Nessun rischio. |
| Python | tutto | `dataclass(slots=True)`, sintassi `X | None`, `from __future__ import annotations`: richiesto Python >= 3.12 (ok per HA 2026.11). |

Non ho potuto leggere le note di versione 2026.x (post dataset), quindi nessuna deprecazione specifica del 2026.11 e' esclusa: l'unica verifica possibile e' lo smoke test sull'istanza di test prima dell'installazione.

### 8. Test HA scritti e mai eseguiti
Import e fixture verificati: `init_vtherm_api` esiste in `tests/conftest.py:170`, `hass_storage`, `MockConfigEntry`, `async_fire_time_changed` esistono nel pacchetto `pytest_homeassistant_custom_component`, `FULL_CENTRAL_CONFIG` in `tests/commons.py:155`, `asyncio_mode = auto` in `setup.cfg`/`pyproject.toml`, `VersatileThermostatAPI.reset_vtherm_api` e' usato altrove in `conftest.py:166`, `CONF_HEATING_ORCHESTRATION` arriva da `const` tramite `commons`. Errori certi: elenco I-2. Fragilita': I-6. Il file importa dal test puro (`from .test_heating_boiler_collector import ...`): corretto, il modulo non esegue i test e carica il collector con un nome privato.
Asserzioni che non provano quanto dichiarano: `test_observe_never_calls...` (264-287) non cattura servizi chiamati da altri componenti prima del setup perche' li azzera (`service_calls.clear()` a 271) e la prova "nessun comando" dipende dal filtro `persistent_notification` altrove; `test_unique_id_entity_id_and_category` (253) accetta due forme; `test_recorder_exclusions...` (257-261) controlla solo la costante, non il comportamento del recorder.
Copertura assente (dichiarata nell'ESITO): config flow con binding non validi, parita' col template HA, e una prova che il numero di righe recorder resti limitato (I-4).

### 9. Prontezza per M2-B (control)
Gia' pronti: epoch/coalescenza, `Store`, campi `owner`/`commanded_mode`/`last_*` nel contratto, `master_entity_id` unico e ascoltato, `_own_entity_ids` per evitare auto-riferimento, `control` rifiutato in un solo punto del parser.
Ostacoli o decisioni da prendere:
1. **Struttura di `_evaluate`.** E' sincrona e fa raccolta, M1, aggiornamento di `_cycle`, `generation`, pubblicazione e risottoscrizione in un colpo. M2-B deve distinguere calcolo puro (`_compute() -> decision`) da pubblicazione e da una fase di comando asincrona con token, e ripetere il calcolo dopo la scrittura del contesto e prima dell'azione (SPECIFICA, prima nota).
2. **Persistenza.** `async_delay_save(30 s)` e' corretta per observe ma in control l'ancora `off`/`r`/`s` deve essere durevole (o riportata con `async_save`) prima di un comando che ne dipende; altrimenti un crash o reload entro 30 s ricalcola `stopped = now` e riapre la pausa di 600 s (o la accorcia).
3. **Conferme.** Nessuna macchina a stati per "comando inviato / atteso / confermato / scaduto". Serve: ID comando, timeout, nessun retry a raffica dopo fallimento, conferma solo da stato osservato di `climate.termostato_ingresso` (il master e' gia' ascoltato), `unknown`/`unavailable` mai conferma, gestione di scrittori esterni (la card Oikos e la UI scrivono ancora il master).
4. **Proprieta' esclusiva.** Non esiste alcun controllo che `automation.caldaia_controllo_stabile_delle_richieste` (e le tre legacy) siano `off` quando si passa a control; l'inventario mostra che oggi il controllore esterno e' attivo. Il passaggio richiede un handshake esplicito (lettura dello stato delle automazioni legacy, rifiuto di `control` se attive, `owner` che diventa `controller` solo dopo conferma) e una decisione sui timer `timer.caldaia_attesa_*` e sul `service_activate/deactivate` nativo (oggi puntano a `input_boolean.richiesta_caldaia_vtherm`, che resta il consenso).
5. **Reload e transitori.** `reload_all_vtherm` ricarica tutte le entry; durante il reload le stanze risultano `entity_unavailable`, il conteggio valido scende, e con master `heat` M1 propone `off` (`no_valid_radiator`). In observe e' innocuo; in control servono un periodo di assestamento/isteresi dopo reload e riavvio (oggi `_inputs_ready` attende solo gli ingressi centrali, `manager:450-456`, non le stanze) e la regola che l'OFF per assenza di dati richieda stabilita' di qualche valutazione. Stessa cautela per i reload di un solo VTherm.
6. **Modalita' e configurazione.** `SELECTABLE_MODES` va sbloccato in due punti (M-6); `parse_orchestration_config` oggi tratta `control` come errore e ritorna `mode=disabled`: serve un percorso di errore diverso per "control non consentito perche' proprieta' non ottenuta".
7. **Percorso di chiamate.** Una sola implementazione di `climate.set_hvac_mode` verso `master_entity_id`, con verifica che il master esponga `heat`/`off` in `hvac_modes`; nessun passaggio dal gestore caldaia nativo (spec).
8. **Diagnostica.** Aggiungere contatori/ultimo esito del comando; l'UI Oikos in control deve poter distinguere "proposto" da "inviato" da "confermato".

## Non verificato

- Nessun test HA eseguito (divieto): config flow, setup/unload/reload, `Store`, timer, registrazione entita', `entity_id` generato, traduzioni caricate da HA, comportamento del recorder, lifecycle reale, parita' col motore template di HA.
- Compatibilita' con HA 2026.11.2: solo analisi statica; nessuna lettura delle note di rilascio 2026.x.
- Parita' del collector contro dati reali dell'impianto (nessun accesso a HA); fuso del container dell'impianto (M-5); formato reale di `last_temperature_datetime` (assunto ISO con offset).
- Il test della suite esistente `tests/test_config_flow.py` e le altre suite caldaia/Avatto non sono state eseguite da me (richiedono HA); la sola evidenza e' lo statico (nessun test esistente asserisce il menu della centrale).
- Il numero di righe recorder generate dal sensore (I-4) e' un'inferenza.
- Non ho riletto per intero i 199 test del collector oltre al mutation testing mirato (17+4 mutanti) e ai nomi/struttura; la qualita' di ogni singola asserzione non e' stata verificata.
- Italiano delle traduzioni: letti solo 5 messaggi a campione.
