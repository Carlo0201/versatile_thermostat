# M2-B - Esito (Worker W1, caldaia): controllo `control`, correzioni della review M2-A

Data: 24/11/2026. Fork `repos/versatile_thermostat`, branch `codex/avatto-trv-pilot`, base `c16fd0a`. Nessun commit, nessun push, nessun accesso a Home Assistant reale, **nessuna esecuzione di Home Assistant né dei suoi fixture di test**. Nessuna modifica a `heating_boiler_policy.py` (SHA-256 invariato), `heating_pdc_*`, `feature_heating_pdc_manager.py`, SDK Oikos, `baseline/`, `review/`, `live-2026-11-24/`, `CLAUDE-STATUS.md`, `CONTRATTI.md`, `README.md`, manifest, requisiti, versione.

**Stato onesto in una riga:** logica del comando completa e provata da 106 test puri (67 mutanti su 67 uccisi) e da 19 scenari sul manager reale con un finto Home Assistant scritto a mano; config flow, lifecycle HA, traduzioni caricate, sensore, recorder e `Store` reali **non verificati** (test HA scritti, mai eseguiti).

## 1. Correzioni dei rilievi di `M2-A-REVIEW.md`

| Rilievo | Esito | Dove / come |
| --- | --- | --- |
| **I-1** `last_error` sticky e mascherante | Chiuso | Gli errori di avvio (`store_load_failed`, `store_invalid`, `legacy_seed_invalid`) stanno in `_startup_error`, mostrati al massimo 60 s (`STARTUP_ERROR_TTL_S`) dalla prima valutazione e poi rilasciati. `last_error` si ricalcola a ogni valutazione con priorità esplicita: unità non supportata > errore del comando fallito > errore di persistenza > errore di avvio (config e `evaluation_error` hanno lo stato `error` proprio). Test: `test_unit_error_is_reported_even_after_a_startup_error`, `test_startup_error_is_released_after_a_minute`, scenario 13. |
| **I-2** asserzioni HA certe sbagliate | Chiuso (non eseguito) | `world()` ora contiene il ciclo legacy valido (niente `legacy_seed_invalid` spurio); `cycle["off"] == int(clock.now)` (M1 riparte la pausa se il modo salvato non era `off`); il caso `store_invalid` usa una sezione senza legacy; rimossa l'asserzione su `MASTER == unknown` contraddetta da `put_states`. |
| **I-3** mutante `count <= len(unique)` | Chiuso | Righe `("2", [a,b,c]) -> invalido` e `("4", [a,b,c]) -> invalido` in `test_raw_count_validity`; il generatore `m2/collector_cases.py` produce anche il conteggio inferiore agli id unici; harness rieseguito: **17 fixture + 20.000 casi (seed 777) identici al template Jinja**, risultato rigenerato in `m2/verify_m2_collector-result.json` (hash collector corrente). |
| **I-4** rumore di pubblicazione | Chiuso | Si pubblica solo se cambia il contenuto significativo (`publication_signature`, puro e testato: stato M1, decisione, campi di comando/controllo, ingressi, stanze idonee e valvole, codici di esclusione, stato delle condizioni, ancore del ciclo, proprietari esterni) oppure al **heartbeat di 25 s**. Restano fuori `evaluated_at`, `generation`, `gap`/`target` e gli `actual` delle condizioni, che cambiano a ogni campione. Motivo del heartbeat: la freschezza lato Oikos è -2..+45 s e il poll è ogni 15 s; con soglia 25 s si pubblica a ogni secondo tick, quindi età massima ~30 s e circa 2.880 scritture/giorno a regime (prima: tutte le valutazioni, anche quelle scatenate dai campioni dei climate). Non si può scendere sotto senza perdere la freschezza: HA scrive una riga di stato anche se cambiano solo attributi non registrati. `generation` ora conta le **pubblicazioni** (come dice il contratto). |
| **I-5** eccezioni troppo strette | Chiuso | Ogni fase di `_evaluate` è isolata (`_compute`, piano di controllo, pubblicazione, risottoscrizione/timer); un'eccezione qualunque nel calcolo pubblica `error` con `evaluation_error:<tipo>` e ripristina l'ascolto; ogni publisher è protetto singolarmente; un errore di pubblicazione non impedisce il comando. Scenario 16. |
| **I-6** test HA con tempi fragili | Chiuso (non eseguito) | `Clock.advance` usa istanti cumulativi dal primo `advance` (preso subito dopo il setup), documentato nella classe; lista dei servizi chiamati dal setup filtrata per `persistent_notification` come prima ma l'osservazione senza servizi usa lo spy esplicito; `entity_id` accetta solo i due suffissi tradotti; nessuna lambda su `call_service` non `@callback` rimasta. |
| **I-7** requisiti prima del control | Chiuso | Calcolo separato da pubblicazione e comando (`EvaluationResult` immutabile); salvataggio del record prima del servizio (OFF non atteso, HEAT al massimo 5 s); assestamento dopo setup/reload (`settling`); interblocco di proprietà. Vedi §2. |
| M-1 inesattezze nell'esito M2-A | Corretto qui | Numeri reali dei test e hash (§5, §6). |
| **M-2** persistenza | Chiuso | `Store` v2 con `_async_migrate_func` (sottoclasse `_OrchestrationStore`, migrazione v1->v2 pura in `migrate_store_data`); una versione futura solleva `NotImplementedError`, intercettato (`store_load_failed`, nessun setup fallito); `async_remove_entry` cancella il file; `first_origin` resta quello di caricamento ma `_last_saved_key` si aggiorna solo quando i dati vengono davvero prelevati dallo `Store` e dopo un salvataggio immediato fallito si riarma. Limite: un salvataggio *differito* che fallisce dentro lo `Store` non è ritentato finché i dati non cambiano (lo registra HA). |
| **M-3** race in `_on_deadline` | Chiuso | `_OneShotTimer` con token: un callback già in coda non azzera un timer rimpiazzato. Usato anche per il timer dei comandi. |
| M-4 default `manual`/`pilot` | Non modificato | Scelta documentata; la diagnostica continua a pubblicare `off`. Proposta di contratto in §9. |
| M-5 fuso dei timestamp naive | Non modificato | Nessun cambio; resta da provare sull'impianto. |
| **M-6** doppia fonte dei modi | Chiuso | `const.py` importa i nomi dei modi dal collector puro (`MODE_*`, `CONFIG_FLOW_MODES`, `SELECTABLE_MODES`). |
| **M-7** rifiniture del flow | Parziale | (a) l'esistenza delle entità resta richiesta al salvataggio, ora anche per i proprietari esterni; (b)(c) invariati. |
| M-8 `entity_id` dipendente dalla lingua | Non modificato | Il test accetta i due suffissi; Oikos deve risolvere via registro. |
| **M-9** ordine di unload | Chiuso | Se `async_unload_platforms` fallisce, `async_unload_entry` ricrea e riavvia il manager (il sensore resta sullo stato precedente fino al prossimo reload: limite dichiarato). In più `async_setup_entry` ferma il manager se il setup solleva. Le callback di unload sono state verificate: vedi §3.8. |

## 2. Architettura del controllo

### Moduli

- `heating_boiler_command.py` (nuovo, **puro**: nessun import di HA né di moduli del pacchetto, nessun clock globale, modelli frozen). Decide, a partire da azione M1, modo osservato del master, interblocco di proprietà, record del comando e clock: inviare, confermare, ritentare, rinunciare, annullare, sostituire. Espone `evaluate_ownership`, `update_settling`, `step`, `start_send`, `on_service_completed`, `on_service_failed`, `restart_record`, `command_diagnostics`, `record_to_dict`/`record_from_dict`, `migrate_store_data`.
- `feature_heating_orchestration_manager.py`: esegue i servizi, programma i timer, persiste, pubblica. **Non esiste un secondo percorso di servizi**: l'unica chiamata è `climate.set_hvac_mode` (`off`/`heat`) con `blocking=True` e timeout 30 s sul master; non scrive helper, timer, automazioni né il ciclo legacy, non accende né spegne controller esterni. Il gestore caldaia nativo non viene toccato.
- `heating_boiler_collector.py`: configurazione (`external_owner_entity_ids`, `control_confirmed`), diagnostica 1.1, `publication_signature`.

### Interblocco di proprietà (prima di ogni invio e a ogni valutazione)

`owner = controller` solo se: modalità `control` e manager attivo (epoca corrente, non fermato); **ogni** entità di `external_owner_entity_ids` ha stato esattamente `off` (`on`, `unknown`, `unavailable`, assente, `OFF`, `off ` bloccano; la lista vuota blocca); il master ha un modo `off`/`heat`; c'è stata almeno una valutazione completa (conteggio valido, unità supportata, master noto). Altrimenti `owner = observer` e `control_status` = `blocked_external_owner` / `blocked_master_unknown` / `starting`. Perdita della proprietà: record pendente -> `cancelled`, ritentativi e timer annullati, task di invio in volo cancellato, nessun comando di compensazione.

### Sequenza di una valutazione (`_evaluate`)

1. Clock unico; ingressi centrali presenti e HA in esecuzione, altrimenti `starting`.
2. `_compute(now)`: collector + M1 -> `EvaluationResult` immutabile.
3. Piano di controllo: assestamento, interblocco, `step()` -> record e transizioni (logbook), cancellazione dell'invio in volo se non più valido.
4. Pubblicazione (se significativa o heartbeat), risottoscrizione (inclusi i proprietari esterni), timer M1 e timer del prossimo scadere del comando (timeout di conferma o ritentativo).
5. Se `step` chiede un invio, nasce un task separato `vtherm boiler command <modo>`; un errore delle fasi 3-4 non impedisce il comando.

### Un invio (`_async_send`, un solo comando in volo, `asyncio.Lock`)

Lock tenuto solo per il tempo dell'invio, **non** durante l'attesa della conferma.

1. Ricalcolo con **clock nuovo** di raccolta, M1 (con il ciclo già aggiornato, come la baseline) e interblocco; `step()` su dati freschi. Se non chiede più lo stesso invio (azione cambiata, master cambiato, proprietà persa) l'invio è abbandonato (`superseded`/`cancelled`) e **nessun tentativo è consumato**. Vale anche per i ritentativi.
2. `start_send`: record `sending` (tentativo n), evento `sent` nel logbook.
3. Persistenza immediata: **OFF** accoda il salvataggio senza attenderlo; **HEAT** attende al massimo 5 s (`asyncio.shield` + `asyncio.timeout`) e prosegue comunque, con `last_error = command_save_failed|command_save_timeout`.
4. Ultimo controllo di proprietà e che il record sia ancora quello; verifica che il master esponga il modo in `hvac_modes` (altrimenti fallimento `hvac_mode_unsupported`, senza chiamata).
5. `climate.set_hvac_mode`, `blocking=True`, `asyncio.timeout(30)`.
6. Esito: servizio tornato senza errori -> `pending` con `service_completed_at` (**non è una conferma**); eccezione o timeout -> `failed` con ritentativo. Un esito che arriva dopo unload/epoca vecchia o per un invio sostituito (`send_id` diverso) è ignorato.

### Stati distinti, sempre esposti

`service_completed_at` (servizio completato) / `command_status = pending` (in attesa) / `confirmed` (stato osservato del master uguale al comando, `last_confirmation_at`). `unknown`/`unavailable`/modo non noto non confermano mai.

### Ritentativi (decisione 1)

4 invii al massimo per lo stesso modo richiesto: primo invio + 3 ritentativi. Un ritentativo segue un fallimento (servizio in errore/timeout, oppure master non nel modo comandato entro **60 s dal completamento del servizio**, confronto inclusivo: a 60,0 s la conferma ancora vale, a 59,999 s non scatta il timeout). Attese 60 -> 120 -> 300 s dal momento del fallimento. Dopo il quarto fallimento `control_status = command_failed` e nessun altro comando verso quel modo finché il master non cambia stato osservato (= conferma tardiva, con soli due modi noti) o l'azione M1 non cambia (`superseded`, poi una richiesta nuova con budget nuovo).

### OFF prioritario e sostituzione (decisioni 2 e 3)

- Il record dell'OFF è accodato in persistenza senza attesa; solo HEAT attende (max 5 s).
- Un'azione M1 `off` **sostituisce subito** un heat pendente, in invio o in attesa di ritentativo: record `superseded`, il task del servizio heat in volo viene **cancellato**, l'OFF parte senza attendere l'esito dell'heat. Una conferma heat tardiva (master osservato `heat` dopo la richiesta di `off`) non conferma l'OFF: è uno stato osservato normale; il record OFF prosegue con conferme e ritentativi propri. Stesso schema per un heat dopo un off. (Scenario 8: con il servizio heat bloccato in volo, l'OFF parte e il log mostra `sent:heat, superseded:heat, sent:off`.)

### Assestamento dopo setup/reload (I-7)

Nessun comando, OFF compreso, finché non ci sono due valutazioni consecutive **valide** a distanza di almeno 30 s con la **stessa** azione M1 (`update_settling`: una valutazione non valida, un'azione diversa o un orologio che torna indietro riavviano la finestra). `control_status = settling`. È un gate del solo comando: la proprietà è già valutata. Una volta raggiunto resta acquisito per la vita del manager (nuovo manager a ogni reload: si riassesta).

### Persistenza

`Store` v2: `{cycle, legacy_seeded, first_origin, saved_at, command}`, `command = {mode, at, attempts, status, send_id, service_completed_at, last_confirmation_at, base_master, error}` (`retry_at` non è salvato). Al riavvio/ricarica un record `sending`/`pending`/`failed` diventa `unconfirmed` (evento `unconfirmed` nel logbook, mai confermato dal riavvio); un ritentativo non sopravvive. Le scritture sono differite (ciclo 30 s, transizioni 2 s) e non bloccano mai un comando; a ogni invio il ciclo è salvato insieme al record.

### Lifecycle

Unload/reload/stop: epoca incrementata, listener, timer, task di invio (cancellato e atteso 5 s al massimo), salvataggi in corso e flush finale; le callback tardive sono ignorate per epoca. Nessun comando con `hass.is_stopping`. Dopo setup/reload la modalità `control` riparte in osservazione e in assestamento.

## 3. Dettagli verificati

### 3.1 Configurazione (nuove chiavi di `entry.data["heating_orchestration"]`)

```
mode: disabled | observe | control
external_owner_entity_ids   lista 1..16 di automation.* | input_boolean.* | switch.*   (obbligatoria per control)
control_confirmed           true  (scritta SOLO dal passo di conferma dell'options flow; obbligatoria per control)
```
Il resto come M2-A. Validazioni nuove: nessun proprietario può coincidere con master, consensi, altri ingressi, stanze, split o sessioni PDC (`duplicate_binding`); `control` senza proprietari = `required`; `control` senza `control_confirmed is True` = `control_not_confirmed` (il manager resta `disabled` con `state=error`, mai controllo implicito). Nessuna migrazione né default porta a `control`.

### 3.2 Options flow

`control` è offerto **solo** dall'options flow di una centrale esistente (`_ho_allow_control`), mai dalla creazione. Passo `heating_orchestration` (modo + entità + proprietari esterni, esistenza verificata) -> passo `heating_orchestration_control_confirm` che elenca le condizioni di attivazione e lo stato corrente dei proprietari esterni (possono essere ancora attivi: finché non sono tutti `off` il controller resta bloccato e non invia nulla) -> stanze. Ogni modifica richiede una nuova conferma (il flag non è riportato). La finalizzazione non salva mai un `control` non confermato: lo degrada a `observe` con avviso. Uscendo da `control` il flag è rimosso.

### 3.3 Diagnostica v1.1 (compatibile; `schema_version` resta 1)

Nuovi attributi: `schema_minor` (1), `control_status`, `command_status`, `service_completed_at`, `command_attempts`, `command_retry_at`, `external_owners: [{entity_id, state}]` (`state` null = entità assente). `commanded_mode`, `last_command_at`, `last_confirmation_at` ora valorizzati. `owner = controller` solo con l'interblocco soddisfatto, altrimenti `observer`; `mode` pubblicato = `control`; lo stato dell'entità resta il motivo M1.

- `control_status`: `inactive`, `starting`, `settling`, `blocked_external_owner`, `blocked_master_unknown`, `ready`, `pending`, `confirmed`, `command_failed` (la specifica elencava `inactive`, `blocked_external_owner`, `ready`, `pending`, `confirmed`, `command_failed` e, nella sezione I-7, `settling`; aggiunti `starting` e `blocked_master_unknown`).
- `command_status`: `sending`, `pending`, `confirmed`, `failed`, `unconfirmed`, `superseded`, `cancelled`, oppure null.
- `external_owners` è mostrato anche in `observe` se configurato (controllo preventivo prima dell'attivazione).
- Recorder: invariato `_unrecorded_attributes`; i campi nuovi cambiano raramente e restano registrati.

### 3.4 Logbook

Una voce per transizione (`sent`, `confirmed`, `failed`, `exhausted`, `superseded`, `cancelled`, `unconfirmed`), mai una per tick, nome `Caldaia`, dominio `climate`, entità = master, evento `logbook_entry` (stessa costante e chiavi di `logbook.log`, senza dipendere dal componente). Messaggio: `OIKOS_HEATING_V1|boiler|<evento>|<modo>|attempt=<n>/4|reason=<motivo M1>|valves=<n>[|detail=<causa>]`. **Il formato è una proposta**: se Oikos richiede un altro tracciato si cambia in un punto (`_log_event`).

### 3.5 Traduzioni

`en.json` e `it.json` (config e options): descrizione aggiornata, campo `external_owner_entity_ids`, passo `heating_orchestration_control_confirm`, errori `ho_control_not_confirmed` e `ho_too_many_external_owners`, opzione `control` del selettore; rimosso `ho_control_not_available`. Altre lingue: fallback inglese. JSON validi (`json.tool`).

### 3.6 Modalità caldaia separata dalla futura `pdc_mode`

`mode` della sezione caldaia non attiva nulla sulle PDC e non legge nulla di PDC oltre allo split/sessione già usati dal collector per la qualificazione delle stanze. Nessuna interazione con `pdc_mode`.

### 3.7 Proprietà dei file condivisi

Toccati: `__init__.py`, `config_flow.py`, `config_schema.py`, `const.py`, `sensor.py` (solo docstring), traduzioni `en`/`it`, `tests/test_heating_orchestration.py`. **Non toccati** (verificato con hash): `vtherm_central_api.py`, `tests/conftest.py`, `tests/commons.py`.

### 3.8 Callback di unload (richiesta esplicita)

- `ConfigEntry.async_on_unload(func)` accetta una funzione coroutine (`Callable[[], Coroutine | None]`, verificato sul sorgente 2026.10.4) e le esegue in ordine inverso: `_stop_orchestration` (registrata dopo l'update listener) gira per prima.
- Lo stop è idempotente e per `entry_id`: dopo un reload non può fermare il manager nuovo (creato dopo l'unload).
- `async_unload_entry` ferma il manager **prima** di scaricare le piattaforme (nessun comando durante lo scaricamento); se lo scaricamento fallisce ricrea e riavvia il manager (M-9).
- `async_setup_entry` ferma il manager se `async_forward_entry_setups` o `async_start` sollevano (`BaseException`, poi `raise`).
- `async_stop` cancella task e timer; i listener one-shot HA STARTED/STOP si ripuliscono da soli e sono comunque annullati; test HA `test_the_unload_callbacks_are_registered_once_and_stop_the_manager` (non eseguito).

## 4. Test e verifiche eseguiti (comandi esatti, dalla radice del fork)

Interprete: `C:\Users\CarloTomaselli-SOFTCARE\AppData\Local\Temp\opencode\m1venv\Scripts\python.exe` (Python 3.15 alpha del venv M1), nessun HA.

1. Suite pura richiesta:
   `python -m pytest -q --noconftest --import-mode=importlib -p no:cacheprovider tests/test_heating_boiler_command.py tests/test_heating_boiler_collector.py tests/test_heating_boiler_policy.py tests/test_heating_pdc_policy.py tests/test_heating_pdc_transaction.py tests/test_heating_pdc_provenance.py`
   -> **928 passed, 1 skipped** (riferimento senza il file nuovo: 780 + 1 skipped). Scomposizione: command 106 (nuovo), collector 205 (+6 sui 199 precedenti e +40 sulle nuove aree: configurazione `control`, proprietari, diagnostica 1.1, firma di pubblicazione), policy 143 + 1 skip, PDC invariati. Nessuna regressione PDC osservata (i file di W2 al momento dell'esecuzione passano).
2. Mutazione del modulo comando, `python plans/heating-orchestration-2026-11-24/m2b/mutate_command.py` (67 mutanti testuali: costanti 60/120/300/30/5/4 invece di 3, `>=`/`>` ai limiti 59/60 s e ai 30 s, interblocco, supersede, conferma invertita, esaurimento, ritentativo senza incremento, gate di assestamento, annullamento, restart, `send_id`, persistenza): **67 applicati, 67 uccisi, 0 sopravvissuti** (al primo giro 64/67: tre mutanti sui `wake_at` e sul campo `external_owners` hanno portato a due test aggiuntivi).
3. Scenari con **finto Home Assistant** (`plans/heating-orchestration-2026-11-24/m2b/fake_ha_scenarios.py`, `python -I ...`): carica il manager **reale** con stub scritti a mano dei soli API HA che usa (core, helper di eventi/timer, `Store`, bus, servizi) e li pilota con un orologio simulato: **19/19 scenari** (assestamento e un solo OFF, conferma e logbook, quattro invii e `command_failed`, errore di servizio, proprietario esterno e riattivazione, unload a metà attesa e riavvio -> `unconfirmed`, OFF che sostituisce un HEAT in volo, OFF non atteso dal salvataggio e HEAT oltre i 5 s, salvataggio fallito, ricalcolo che abbandona un'azione, pubblicazione significativa/heartbeat, TTL dell'errore di avvio e priorità, migrazione store v1 e versione futura, `hvac_modes` senza il modo, publisher/calcolo difettosi, shutdown e reload). **Non è Home Assistant**: non esegue config flow, registri, `Store` reale, recorder, traduzioni.
4. Parità del collector: `python -I plans/heating-orchestration-2026-11-24/m2/verify_m2_collector.py --cases 20000 --seed 777` -> 17 fixture e 20.000 casi identici al template Jinja.
5. `py_compile` di tutti i file Python toccati (inclusi i tre di test): ok. `json.tool` su `en.json` e `it.json`: ok.
6. Black 24.10.0 (`black24`), `--check --line-length 180`: `heating_boiler_command.py`, `heating_boiler_collector.py`, `feature_heating_orchestration_manager.py` (riformattato: contiene mio codice nuovo), `tests/test_heating_boiler_command.py`, `tests/test_heating_boiler_collector.py`, `tests/test_heating_orchestration.py`, più i tre script `m2b/`: tutti conformi. I file condivisi preesistenti (`__init__`, `config_flow`, `config_schema`, `const`, `sensor`) non sono riformattati.

## 5. Test HA scritti e NON ESEGUITI (`tests/test_heating_orchestration.py`)

Possono contenere errori; vanno eseguiti nell'ambiente isolato prima di qualunque attivazione. Sezioni:

- Esistenti, riscritte sui rilievi: entry senza sezione, `disabled`, sezioni non valide (ora con gli errori di `control` senza proprietari/conferma), pubblicazione schema 1.1, `unique_id`/categoria, recorder, observe senza servizi, attesa degli ingressi, ingressi non validi, unità, coalescenza, polling e heartbeat, pubblicazione immediata dei cambi significativi e non dei soli campioni, un clock per valutazione, scadenze, nessun loop, duplicati, auto-riferimento, publisher difettoso, seed/restore/migrazione v1/versione futura/TTL errori, scrittura differita non rimandata, flush e rimozione dello store, unload/reload/callback tardive, callback di unload.
- Nuovi `control`: assestamento poi un solo OFF; conferma dal master e logbook; quattro invii con attese crescenti e `command_failed`; errore del servizio e ritentativo; salvataggio lento/fallito (OFF non atteso, HEAT prosegue); blocco per proprietario `on`/`unknown`/`unavailable`/assente; master sconosciuto; riattivazione del controller esterno (annullamento, nessuna compensazione, nuova richiesta al ritorno); ricalcolo prima dell'invio che abbandona un'azione; unload durante l'attesa e riavvio (`unconfirmed`); reload che riassesta; record `pending` salvato -> `unconfirmed`; campi di controllo e nessun altro servizio; options flow (modi offerti, conferma esplicita, validazione dei proprietari, uscita da `control`, creazione senza `control`).

## 6. File (SHA-256)

| File | SHA-256 |
| --- | --- |
| custom_components/versatile_thermostat/heating_boiler_policy.py (M1, **non toccato**) | edf7e9007ac44166bfc56a3ad8c74dd6b167af6f4ed2760831d865443c27945b |
| custom_components/versatile_thermostat/heating_boiler_command.py (nuovo) | 2380d82fe376389eecf8ac0f51948724ae72bef9a609ec0db57188e2f0441314 |
| custom_components/versatile_thermostat/heating_boiler_collector.py | 40f35aa5030788300d89dc822d203e9122bb1e0c77aa4449cef5658f2a2fffb8 |
| custom_components/versatile_thermostat/feature_heating_orchestration_manager.py | f2de9ff9b27004a1475d1de9ce760cfd3fa969be1562aaaa235f688a5f6df033 |
| custom_components/versatile_thermostat/__init__.py | 81d99068942cdae525fa75426b43796de776bbeae05c4640aff1c56493456e59 |
| custom_components/versatile_thermostat/config_flow.py | bb20aa3edcf90bf769e52827ff29e4017819c7e867ce5d02acafc79f46f95a98 |
| custom_components/versatile_thermostat/config_schema.py | 3a59277c8352682023fea531ae6691679009d7332910d9553cfdec72683d0308 |
| custom_components/versatile_thermostat/const.py | 3be3793ae06d5202ea4a0f495b76d388d2cda15e797d85aa980625a5f07ba0e2 |
| custom_components/versatile_thermostat/sensor.py (solo docstring) | 1fbcf2be61e9d7f5bf1bc68fb7cab514e5f502f41090d9facc38d03593e53121 |
| custom_components/versatile_thermostat/vtherm_central_api.py (non toccato) | ae0f4663c0fdb5922395f2d359d300a6b1448f6b0a7c515a172ef57efbf9c93b |
| custom_components/versatile_thermostat/translations/en.json | 5b4febb47b608406fcdc832dab41d4ab8ca56fcb20de52718c01b98934365eb1 |
| custom_components/versatile_thermostat/translations/it.json | 2162e5bb23093a1d0b8db51d01af97f9fc5a22503d9c21cc0a8cad156987d9cf |
| tests/test_heating_boiler_command.py (nuovo, puro) | 03dcea1b9b9941f75b39e86c1798d4883341b5a16edb1d75ab1d546e9cdebf75 |
| tests/test_heating_boiler_collector.py | 5b1675cae7124e860e462f96c43678b34421dfc546314e7404cefddd21566182 |
| tests/test_heating_orchestration.py (HA, non eseguito) | 4c649524588b22cd0775a23526238492f0c05d9a60793281720f3e19f01547df |
| plans/heating-orchestration-2026-11-24/m2b/fake_ha_harness.py (nuovo) | ea0bb5f59669aef5e8d0ce92914bfa8562b5095f551e37560497409ce4587ddf |
| plans/heating-orchestration-2026-11-24/m2b/fake_ha_scenarios.py (nuovo) | 96d7c7af304935e45402f1ca200aeadd49570c63bf6a9cf3aeeb5fbe4921318b |
| plans/heating-orchestration-2026-11-24/m2b/mutate_command.py (nuovo) | 8e0d4b21734ed877319cfb301866170b611eff53bf537474e22cc516a4df469b |
| plans/heating-orchestration-2026-11-24/m2/collector_cases.py (generatore I-3) | d6cc9fdfd049c4537518b1e953e7de65ef64bcc96b10baf0827d6744fe397a8d |
| plans/heating-orchestration-2026-11-24/m2/verify_m2_collector-result.json (rigenerato) | ff4e7b671fb2add42ae6665495da38b0758afaff9df58e2e6128501d8531d65b |

(Nota: la cartella `m2b/` e le due modifiche in `m2/` sono fuori dall'elenco dei file del fork ma non nell'elenco dei vietati.)

## 7. Compatibilità statica con Home Assistant 2026.11.2

Non ho potuto leggere le note di rilascio 2026.11. Ho verificato **leggendo** (senza eseguire nulla) i sorgenti di HA **2026.10.4** presenti nell'ambiente isolato `AppData\Local\vtherm-ha-test\venv314`; vale come proxy, non come garanzia per 2026.11.2.

| API usata | Verifica sui sorgenti 2026.10.4 | Rischio |
| --- | --- | --- |
| `Store(hass, version, key)` + sottoclasse con `_async_migrate_func(old_major, old_minor, old_data)` | Metodo presente; il chiamante usa la firma a 3 argomenti e salva il risultato; `NotImplementedError` se la versione non coincide | Basso. Dal 2026 esiste anche `max_readable_version` (non usato). |
| `Store.async_save`, `async_delay_save(data_func, delay)`, `async_remove` | Presenti. Attenzione: `async_delay_save` **non** ri-arma un timer già più vicino | Basso |
| `async_track_point_in_utc_time`, `async_track_state_change_event`, `async_track_time_interval` | Firme confermate (`point_in_time: datetime`) | Basso |
| `hass.async_create_task(coro, name=..., eager_start=True)` | Confermato. Con l'avvio eager un task può terminare prima che il chiamante riceva l'handle: gestito (`task.done()`) | Medio: il flusso dipende dall'avvio eager; ho coperto il caso ma solo con lo stub |
| `hass.is_stopping` | `cached_property` invalidata da `set_state` | Basso |
| `hass.loop.call_soon`, `hass.bus.async_listen_once` (coroutine), `async_fire`, `EVENT_LOGBOOK_ENTRY`, `ATTR_NAME/DOMAIN/ENTITY_ID` | Costanti in `homeassistant.const`; chiavi come `logbook.async_log_entry` (`message` letterale) | Medio: il messaggio usa la chiave `"message"` copiata dal componente `logbook`, che non si importa |
| `hass.services.async_call(domain, service, data, blocking=True)` + `asyncio.timeout` | Standard | Basso |
| `ConfigEntry.async_on_unload` con coroutine | `Callable[[], Coroutine | None]`, in ordine inverso | Basso |
| `selector.SelectSelectorConfig(translation_key=...)`, `EntitySelectorConfig(domain=list, multiple=...)`, `BooleanSelector` | Come M2-A | Basso |
| `OptionsFlow.config_entry` nel ramo `finalize` | Preesistente nel fork (`#713`) | Rischio noto del fork, non introdotto qui |
| `dt_util.get_default_time_zone`, `utc_from_timestamp`, `utcnow` | Presenti | Basso |
| Python | `dataclass(slots=True)`, `asyncio.timeout`, `X | None`: Python >= 3.12 | Basso |

## 8. Deviazioni dalla specifica e scelte da rivedere

1. **Sostituzione ed eventuale conferma heat.** Un'azione `off` sostituisce l'heat (`superseded`) anche se nella stessa valutazione il master è già `heat`: la conferma di quel heat **non** è registrata (decisione 3 presa alla lettera). Il log mostra `sent:heat, superseded:heat, sent:off`.
2. **Riferimento del timeout di conferma**: 60 s dal **completamento del servizio**, non dall'invio (la spec dice "al termine del servizio... entro 60 s").
3. **Flag `control_confirmed`** persistente (non previsto dalla spec): rende verificabile che `control` sia stato attivato solo dal passo di conferma e impedisce l'attivazione implicita da dati scritti a mano.
4. **Stati aggiuntivi**: `control_status` `starting`, `settling`, `blocked_master_unknown`; `command_status` `sending`, `unconfirmed`, `superseded`, `cancelled`; attributo `schema_minor`.
5. **Richiesta esaurita e riavvio**: dopo un riavvio anche un record esaurito diventa `unconfirmed` e non blocca più (il blocco vale finché il manager vive).
6. **"Master cambia stato osservato"** per sbloccare un esaurito: con due soli modi noti equivale alla conferma tardiva; il ramo "cambio senza conferma" non esiste (ramo morto rimosso).
7. **Heartbeat 25 s** e `generation` = numero di pubblicazioni (non di valutazioni).
8. **Logbook** via evento `logbook_entry` (non via servizio `logbook.log`): stesso effetto, nessuna dipendenza dal componente.
9. **Assestamento latched**: non si riarma per il reload di un solo VTherm (la spec parla di setup/reload della centrale). Un rischio residuo è l'OFF `no_valid_radiator` durante il reload di un singolo VTherm a controller già assestato: il ricalcolo prima dell'invio lo attenua (una stanza `entity_unavailable` per l'attimo del reload non basta se le altre sono valide) ma non lo esclude.
10. **`owner = controller` anche durante `settling`** (la proprietà è già verificata; i comandi no). Oikos deve distinguere via `control_status`.
11. Aggiunte fuori dal perimetro stretto: `async_remove_entry` (cancella lo store), ripristino del manager se lo scaricamento fallisce, controllo `hvac_modes` del master, estensione del generatore di parità (I-3).

## 9. Limiti noti

- **Nulla di quanto tocca HA è stato eseguito**: config flow (passi, schema `selector`, traduzioni), sensore, registro entità, recorder, `Store` reale, `async_fire_time_changed`, timing dei test. Il finto HA verifica la logica asincrona del manager, non l'integrazione.
- **Budget dei 4 invii**: una richiesta `superseded` perché l'azione M1 è passata a `none` e poi torna a `heat/off` riparte con 4 invii nuovi. Un flapping dell'azione M1 combinato con un guasto di comunicazione può quindi superare 4 invii complessivi; l'assestamento e le pause M1 (600 s dopo uno spegnimento) lo limitano per `heat`, non per `off`. Va deciso se serve una memoria del budget oltre la richiesta.
- `owner`/`control_status` sono letti a ogni valutazione: una riattivazione del controller esterno è vista subito (listener sui proprietari) ma un'automazione che cambia stato senza evento (es. entità sostituita) è vista al prossimo poll (<= 15 s).
- Il comando non è mai garantito: `climate.set_hvac_mode` sul master può essere sovrascritto da chiunque scriva il master (card Oikos, UI); la conferma lo osserva ma non lo impedisce.
- Se `async_unload_platforms` fallisce il sensore non è ri-agganciato al nuovo manager fino al prossimo reload.
- La rimozione della voce lascia il file di store solo se `async_remove_entry` non viene chiamato (non verificato in HA).
- Un salvataggio differito fallito dentro lo `Store` non è ritentato finché i dati non cambiano.
- Entity id del sensore dipendente dalla lingua (M-8) e default `manual`/`pilot` = `off` non distinguibili da non configurato (M-4): invariati.
- Piano di passaggio (non eseguito, non è compito del controller): Carlo imposta `observe` con i proprietari esterni, confronta la diagnostica, **spegne** le quattro automazioni, poi attiva `control` dall'options flow e conferma; per tornare indietro basta riaccendere il controller esterno (blocco immediato) o scegliere `observe`/`disabled`. Non sono state scritte né modificate automazioni, helper o timer legacy.

## 10. Proposta di contratto (per il coordinatore, non applicata a `CONTRATTI.md`)

1. **§1 Configurazione**: aggiungere `external_owner_entity_ids` (1..16, domini `automation`/`input_boolean`/`switch`, obbligatoria per `control`) e `control_confirmed`; `control` selezionabile solo dall'options flow con conferma; `control` senza questi valori = stato `error` e modo effettivo `disabled`.
2. **§2 Diagnostica 1.1**: `schema_version` resta 1; aggiungere `schema_minor = 1` e i campi `control_status`, `command_status`, `service_completed_at`, `command_attempts`, `command_retry_at`, `external_owners` con i valori elencati in §3.3. `generation` conta le pubblicazioni; **cadenza di pubblicazione**: sul cambio significativo e almeno ogni ~30 s (ogni secondo tick di poll), quindi `evaluated_at` non cambia a ogni valutazione.
3. **§3 Oikos, riga del controller**: `pass` solo con `owner == controller` **e** `control_status` in {`ready`, `pending`, `confirmed`}, dato fresco e schema 1; `settling` = attesa (con indicazione), `blocked_external_owner` / `blocked_master_unknown` / `starting` = non `pass`, `command_failed` = `fail`. Distinguere in UI: richiesta (azione proposta) / comando inviato (`last_command_at`, `commanded_mode`) / servizio completato (`service_completed_at`) / confermato (`last_confirmation_at`); un comando `pending` non è uno stato dell'impianto. `external_owners[].state == null` significa entità assente.
4. **Logbook**: `OIKOS_HEATING_V1|boiler|<evento>|<modo>|attempt=<n>/4|reason=<motivo>|valves=<n>[|detail=<causa>]`, eventi `sent|confirmed|failed|exhausted|superseded|cancelled|unconfirmed` (da confermare con Oikos).
5. **M-4**: documentare che `inputs.manual` e `inputs.pilot` valgono `off` anche se non configurati (o pubblicare `null`).
6. **Registro modifiche** contratto: v1.1 (M2-B) con le aggiunte sopra.
