# M2-A — Esito (Worker A, caldaia): collector e observe

Data: 24/11/2026. Fork `repos/versatile_thermostat`, branch `codex/avatto-trv-pilot`. Nessun commit, nessun accesso a Home Assistant reale, nessuna modifica a manifest, requisiti, versione, `heating_boiler_policy.py`, Oikos, `baseline/`.

**Stato onesto in una riga:** codice completo; test puri collector+policy eseguiti e verdi (343); test Home Assistant scritti ma **NON ESEGUITI** (istruzione di Carlo: niente più HA); config flow e lifecycle HA quindi **non verificati** da test.

## Architettura

- `heating_boiler_collector.py` (puro, nessun import HA, nessun clock globale): configurazione validata (`parse_orchestration_config`), collector che riproduce il `raw` Jinja (`collect_boiler_inputs(states, config, now, temperature_unit, local_tz)`), codici di esclusione stanza, payload diagnostico v1 (`build_boiler_diagnostics`, `empty_diagnostics`), serializzazione ciclo `m/off/r/s`.
- `feature_heating_orchestration_manager.py`: un manager per entry centrale. Listener sugli ingressi (inclusi attributi) con risottoscrizione su finestre e valvole scoperte; poll ogni 15 s; coalescenza (un solo `call_soon` per più eventi); token `_epoch` che invalida le callback dopo `async_stop`; un solo `now` per valutazione, collector e M1 sullo stesso snapshot; timer one-shot solo per scadenze M1 future (le scadute si recuperano col poll, nessun loop a ritardo zero); `Store` versionato (v1, chiave `versatile_thermostat.heating_orchestration.<entry_id>`, `async_delay_save` 30 s, flush su stop); seed legacy una tantum (`legacy_seeded`), `cycle_origin` = `legacy_seed`/`local_restore`/`new_observation`. Nessuna chiamata di servizio. Non ascolta la propria diagnostica. Attende gli ingressi centrali (`starting` + `last_error=waiting_for_entities:...`).
- `vtherm_central_api.py`: `heating_orchestration_manager`, `async_create_heating_orchestration_manager` (ferma l'eventuale precedente), `async_remove_heating_orchestration_manager`.
- `__init__.py`: il manager nasce solo se `heating_orchestration` è presente nei `data` dell'entry centrale; `async_start` dopo `async_forward_entry_setups`; stop su unload (prima della rimozione del sensore) e via `async_on_unload`.
- `sensor.py`: `HeatingOrchestrationBoilerSensor` (solo specchio del manager, diagnostico, `_unrecorded_attributes`).
- `config_schema.py`, `config_flow.py`, `translations/en.json`, `translations/it.json`: UI.

## Configurazione (schema effettivo)

Vive in `entry.data["heating_orchestration"]` (il flow opzioni riscrive `data`, come il resto del fork); `options` non è usata. Chiave assente = disabled, entry invariate.

```
heating_orchestration:
  mode: disabled | observe            # control: rifiutato (control_not_available), stato error, nessun controllo
  master_entity_id            climate.*                     (obbligatorio in observe)
  central_mode_entity_id      select.* | input_select.*     (obbligatorio; ok solo con "Auto"/"Heat only")
  active_count_entity_id      sensor.* con attributo active_device_ids (obbligatorio)
  manual_request_entity_id    binary_sensor|input_boolean|switch (facoltativo)
  pilot_filter_entity_id      idem (facoltativo)
  pilot_ready_entity_id       idem (facoltativo)
  consent_entity_ids          lista 1..8 idem (obbligatoria; vero se tutti on)
  legacy_cycle_entity_id      input_text.* (facoltativo, solo seed)
  rooms: [ {room_key (^[a-z0-9_]{1,64}$), climate_entity_id climate.*, pilot_filter bool,
            pdc: {split_entity_id climate.*, session_entity_id (binary dom.)} } ]   # max 32
```
Validazioni: dominio, formato, duplicati tra tutti i binding (centrali e stanza), `room_key` duplicata, PDC incompleta (split e sessione insieme), `pdc.split == climate`. Un `disabled` accetta qualunque altra chiave senza leggerla. Il flow richiede inoltre che ogni entità esista (`unknown_entity`).

Flow (solo entry centrale): voce di menu `heating_orchestration` (dopo `central_boiler`) -> modo+entità centrali -> se observe, menu stanze (`add_room`, `remove_room`, `done`). Una stanza si modifica rimuovendola e riaggiungendola. Errori con chiavi `ho_<codice>`.

Nota: le liste `menu_options` dei test esistenti del config flow centrale sono cambiate (nuova voce `heating_orchestration`); `tests/test_config_flow.py` risulta eseguito una volta, prima dell'istruzione, con 13 passed 1 skipped, ma sul codice del flow non ancora ricontrollato: da riverificare.

## Entità diagnostica (schema v1, come CONTRATTI.md §2)

- Piattaforma `sensor`, `unique_id` = `<entry_id>_heating_orchestration_boiler`, `translation_key` `heating_orchestration_boiler`, categoria diagnostica, stesso device della centrale. Nome: "Orchestrazione caldaia" / "Boiler orchestration".
- **entity_id che HA genererà (previsto, non verificato in esecuzione):** `sensor.<nome device centrale slug>_<nome tradotto slug>`; con la centrale predefinita e lingua inglese atteso `sensor.central_configuration_boiler_orchestration` (it: `..._orchestrazione_caldaia`). Il nome device è `CENTRAL_CONFIG_NAME`: Oikos deve riceverlo in configurazione e verificarlo dal registro.
- Stato: motivo M1, oppure `disabled`/`starting`/`error`.
- Attributi: tutti quelli di CONTRATTI §2 (`schema_version, mode, owner, evaluated_at, generation, proposed_action, reason, deadline, master_mode, count_raw, count_raw_valid, valid_valve_count, inputs_ok, inputs{central_mode,manual,pilot,pilot_ready,consent}, rooms, excluded_rooms, conditions, cycle, cycle_origin, commanded_mode/last_command_at/last_confirmation_at = null, last_error`). JSON finito (`json_safe`), liste limitate a 64, errore a 255 caratteri.
- `_unrecorded_attributes`: `conditions, rooms, excluded_rooms, evaluated_at, generation, cycle, inputs`.
- `unit` condizioni: `thermal_gap` °C, `valve_count` count, `stability`/`cooldown` s, altrimenti null.

## Semantica del collector e irrigidimenti dichiarati

Port esatto del `raw` (17 fixture immutabili + 5000 casi generati confrontati con Jinja2 offline dal worker precedente: `m2/verify_m2_collector.py`, esito in `m2/verify_m2_collector-result.json`, hash collector di quella esecuzione antecedente al reformat Black). Parità col motore template reale di HA **non testata** (il file `test_heating_orchestration_parity.py` citato nei docstring non è stato scritto).

Scelte esplicite: `unique` Jinja confronta le stringhe senza distinguere maiuscole; timestamp mancante/illeggibile = epoch 0; naive letto nel fuso locale HA; unità diversa da °C -> `ok=false` e `unit_unsupported`; `now` non finito -> ValueError; irrigidimenti rispetto al template (dove solleverebbe o produrrebbe non-JSON): numeri non finiti invalidi, sezioni attributo malformate lette come vuote, protezioni malformate trattate come attive, id attivi non hashable invalidano il conteggio. Con conteggio invalido le stanze non sono valutate (`count_invalid`), `count` strutturale 0 accompagnato da `ok=false`/`count_raw_valid=false`.

## File (SHA-256)

| File | SHA-256 |
| --- | --- |
| custom_components/versatile_thermostat/heating_boiler_collector.py (nuovo) | 1b2224c088ae67fef4272c3c713d9c9deeb1f387fd1ded335dc1db0b0bc42ed4 |
| custom_components/versatile_thermostat/feature_heating_orchestration_manager.py (nuovo) | ea534d32b50b8db42b0a54def8a3b5fd244142b3cede95ada621db337bf0fd19 |
| custom_components/versatile_thermostat/config_flow.py | ea146deaf405140fdbc23559e55e6f4192c0dfeb69cd2fb3a307a29ece9814ef |
| custom_components/versatile_thermostat/config_schema.py | b880e6fdad5c523d10591ab5d79d46cbda39caffc44072042ed961a4177c6625 |
| custom_components/versatile_thermostat/sensor.py | c73e5a9aaaf4d04a615d1328081c512d1fd4fb8a92058c2635a7cdd4bee0420b |
| custom_components/versatile_thermostat/__init__.py | 1adc8e4ca6978f08d1496b52d72641ccd94427033f914e649ab7291f60853248 |
| custom_components/versatile_thermostat/const.py | 0eeaf3763d1bab3aa3919dbd448afda18d2ec94337917ebb02cc958fa53e34b1 |
| custom_components/versatile_thermostat/vtherm_central_api.py | ae0f4663c0fdb5922395f2d359d300a6b1448f6b0a7c515a172ef57efbf9c93b |
| custom_components/versatile_thermostat/translations/en.json | fec362ec23d09e89bb87d9189edb1cb1b0e27738cbf9c712925edccd77444b7d |
| custom_components/versatile_thermostat/translations/it.json | ac59af8fb81cd8d7d957a53b3a9465eb5f2e11b91ce08aed372fece72eff339a |
| tests/test_heating_boiler_collector.py (nuovo, puro) | 05f36e0dd75fd3ffc9f326fe97e7adecb36bd7ffeb73a0b464581e3f7b3f64df |
| tests/test_heating_orchestration.py (nuovo, HA, non eseguito) | 55846e34a5f24568fa5c4836ff6ff7a8567340c5eb1f1c290a68479ee8ff8917 |
| heating_boiler_policy.py (congelato, non toccato) | edf7e9007ac44166bfc56a3ad8c74dd6b167af6f4ed2760831d865443c27945b |

Le traduzioni sono state riscritte con json (formato identico, solo virgole finali aggiunte); altre lingue senza le nuove chiavi (fallback inglese).

## Test: comandi ed esiti

Eseguiti:
- `python -m pytest -q -p no:cacheprovider -p vtherm_win_test_shim --noconftest --import-mode=importlib tests/test_heating_boiler_collector.py tests/test_heating_boiler_policy.py` (interprete venv314, nessuna istanza HA) -> **343 passed** (collector 200: 17 fixture, boundary età 3599/3600/3601, futuro, finestra aperta prima della sospensione, bypass, PDC non disponibile, due valvole per stanza, duplicati, condizioni simultanee, unità, validazione config, schema diagnostico; policy M1 143).
- Black 24 `--line-length 180`: applicato ai nuovi file (collector, due test); manager e policy già conformi. I file tracciati (`__init__`, `const`, `config_flow`, `config_schema`, `sensor`, `vtherm_central_api`) risultano non conformi a Black già prima per codice non mio: non riformattati per non produrre diff estranei.
- Una esecuzione di `tests/test_config_flow.py` (HA, prima dell'istruzione): 13 passed, 1 skipped.

**NON eseguiti** (scritti in `tests/test_heating_orchestration.py`, mai lanciati, possono contenere errori): vecchie entry/disabled/config invalida, observe senza chiamate di servizio, starting e ingressi, coalescenza, attributi, poll 15 s, un solo clock, deadline passata/futura, seed/restore/storage invalido, unload/reload/callback tardive, duplicazione osservatori, unique_id/categoria. **Non scritti**: test del config flow con binding invalidi, test di parità col motore template HA. Non eseguite: suite completa, `test_central_boiler.py`, `test_avatto_room_control.py`, altri config flow.

## Limiti

- Lifecycle HA, config flow e UI mai verificati in esecuzione; entity_id previsto, non osservato.
- Observe senza seed parte con ciclo vuoto (pausa iniziale possibile): non è divergenza della politica.
- `control` non selezionabile; nessun comando, nessun campo "comando inviato" valorizzato.
- Modifica stanza = rimuovi e riaggiungi. Esistenza entità richiesta già in configurazione.
- Se il flow opzioni salva con `disabled` le altre chiavi restano conservate (non lette).

## Disattivazione

Impostare `mode: disabled` dal menu "Orchestrazione caldaia" (resta solo il sensore `disabled`), oppure rimuovere la chiave `heating_orchestration` dai `data` dell'entry: nessun manager, nessun sensore. Dati residui: solo `.storage/versatile_thermostat.heating_orchestration.<entry_id>` (cancellabile, contiene solo il ciclo). Le sorgenti legacy non sono state toccate.

## Proposte al contratto

1. `CONTRATTI.md` §1: precisare che il valore vive in `entry.data` (non `options`).
2. §2: aggiungere `starting` con `last_error=waiting_for_entities:<id,...>` e gli errori di configurazione `config_<codice>[:<chiave>]` in `last_error`.
3. Aggiungere codice `unit_unsupported` come motivo di `inputs_ok=false`; documentare `valid_valve_count=0` non significativo se `inputs_ok=false`.
4. `evaluated_at` in `starting`/`error` può essere valorizzato ma `generation` resta fermo.

## Punti per M2-B

- Il manager ha già epoch, coalescenza, `Store` e `owner`; il controller dovrebbe riusare `_evaluate` e riempire `commanded_mode/last_command_at/last_confirmation_at`, senza secondo percorso di servizi del gestore caldaia nativo.
- Sbloccare `control` in `SELECTABLE_MODES` (collector e `const.HO_SELECTABLE_MODES`) solo dopo review.
- Il seed legacy è già "una volta"; in control definire il passaggio di proprietà dei timer/helper legacy.
- Il master `unknown` è riportato come tale: M2-B deve confermare lo stato dopo il comando prima di dichiarare `owner=controller`.
- Prima di M2-B: eseguire i test HA scritti qui e correggere gli eventuali errori.
