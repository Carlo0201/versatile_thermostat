# Esito — condizioni e stato per stanza calcolati dall'integrazione

Data: 25/11/2026 (Europe/Rome). Piano: `output/versatile_thermostat_room_conditions_2026-11-25/PIANO-CONDIZIONI-STANZA.md`. Contratto: `ROOM-CONDITIONS-CONTRATTO.md` (approvato in C1, precisato in C2 al §10).

**Stato:** lavoro locale nel fork, **non committato**. Nessun test su Home Assistant reale, nessun pacchetto o ZIP, nessuna modifica a card, SDK, `heating_boiler_policy.py`, cancelli di controllo, `CONTRATTI.md` e `CLAUDE-STATUS.md`. Il sensore per stanza è scritto e provato **solo con HA finto e test puri**: la parte HA reale (entità, registro, traduzioni a video) non è stata eseguita.

## 1. Cosa fa ora l'integrazione

Per ogni stanza configurata nella sezione `heating_orchestration` pubblica un sensore diagnostico `<central_entry_id>_heating_orchestration_room_<room_key>` con stato (`heat_state`), motivo, motivi secondari, fonte, scarto, scadenza, fasi (`ready`, `demand`, `boiler`, `pump`), prima condizione bloccante e l'elenco delle condizioni. È una **proiezione in sola lettura** sugli stessi dati del collettore e della decisione M1 dello stesso giro: stesso orologio e stessa `generation` della caldaia, nessun comando, nessuna soglia nuova (`start_gap` = `DEFAULT_THERMAL_GAP_C`).

## 2. File

### Nuovi

| File | Contenuto |
| --- | --- |
| `custom_components/versatile_thermostat/heating_room_status.py` | Motore puro: `evaluate_room_status`, `room_attributes`, `empty_room_attributes`, `room_publication_signature`. Nessun import di HA. |
| `tests/test_heating_room_status.py` | Test puri del motore (tabella delle condizioni, fasi, bloccante, stati, attributi, non regressione del collettore sulle 17 fixture di baseline). |
| `tests/test_heating_room_status_parity.py` | Parità con la card (77 scenari) e tabella delle differenze documentate. |
| `tests/test_heating_room_status_manager.py` | Esegue gli scenari del manager con HA finto. |
| `tests/tools/room_status_scenarios.py` | Generatore degli ingressi dei 77 scenari (segnaposto, nessun entity_id reale). |
| `tests/tools/export_card_reference.mjs` | Esegue il codice **attuale** della card sugli stessi ingressi (sola lettura sull'SDK). Gli id fissi della card sono letti dal suo sorgente a run time, non scritti qui. |
| `tests/fixtures/room_status/` | `scenarios.json`, `reference_card.json` (con SHA-256 dei file della card usati), `parity_differences.json`, tre `sample_*.json` del contratto. |
| `plans/heating-orchestration-2026-11-24/room-status/fake_ha_room_status.py` | 11 scenari del manager reale con HA finto (33 verifiche). |
| `plans/heating-orchestration-2026-11-24/ROOM-CONDITIONS-CONTRATTO.md` | Contratto. |

### Modificati (file già a lavoro di altri: solo aggiunte, nessuna riformattazione)

| File | Aggiunta |
| --- | --- |
| `heating_boiler_collector.py` | Funzione pubblica `active_valves()` **estratta dall'espressione esistente** e usata dal collettore al suo posto (+9/−3 righe). Comportamento e uscita invariati: i test del collettore sono immutati e verdi. |
| `feature_heating_orchestration_manager.py` | `room_keys`, `room_state`, `attach_room`, `_publish_room(s)`, `_publish_rooms_special`, `pdc_state_provider`; chiamate in `_publish_result` e `_publish_special` (in `try`, non bloccano mai la caldaia); `adopt_publishers` copia anche i publisher delle stanze. |
| `sensor.py` | Classe `HeatingOrchestrationRoomSensor`, creazione di un sensore per ogni stanza configurata, pulizia dei sensori di stanze rimosse (solo se la sezione è leggibile in modo non ambiguo). |
| `const.py` | `HO_ROOM_SENSOR_SUFFIX = "_heating_orchestration_room_"`. |
| `vtherm_central_api.py` | Il manager riceve `pdc_state_provider` (lettura dell'ultima pubblicazione PDC). |
| `translations/en.json`, `it.json` | Chiave `heating_orchestration_room` («Heating status {room}» / «Stato riscaldamento {room}»). |

`sensor.py`, `const.py` e `vtherm_central_api.py` **non erano già conformi a Black 24** prima delle mie modifiche (verificato su `HEAD`): non li ho riformattati.

## 3. Test, prima e dopo

Comando (da `CLAUDE-STATUS.md`): `python -m pytest -q --noconftest --import-mode=importlib -p no:cacheprovider tests/<file>.py`. **Nota:** nel venv `vtherm-ha-test` il plugin `pytest-homeassistant-custom-component` blocca i socket e il comando da solo dà errori in setup (1165 errori su 14 file); funziona aggiungendo `-p vtherm_win_test_shim`. Con un Python pulito con il solo pytest non serve.

| Misura | Prima | Dopo |
| --- | --- | --- |
| Suite pura, 14 file (collettore, M1, comando, release, PDC) | 1165 passati | 1165 passati (stessi 14 file, invariati) |
| Nuovi test (motore, parità, manager) | — | +163 passati (motore 81, parità 81, manager 1; 1 saltato = aggiornamento delle differenze, solo con `PARITY_UPDATE`); totale 17 file: 1328 passati, 1 saltato |
| Scenari HA finto caldaia (`m2b/fake_ha_scenarios.py`) | 25/25 | 25/25 |
| Scenari HA finto PDC (`pdc/fake_ha_pdc.py`) | 45 verifiche, 0 fallite | 45 verifiche, 0 fallite |
| Scenari HA finto stanze (nuovo) | — | 11 scenari, 33 verifiche, 0 fallite |
| Black 24 (`--line-length 180`) sui file nuovi | — | pulito |

Il numero «1063 passati, 1 saltato» di `CLAUDE-STATUS.md` riguarda un insieme di file diverso da quello che ho eseguito; il confronto vale tra «prima» e «dopo» sugli stessi file. Non ho eseguito: `tests/test_heating_orchestration.py`, `tests/test_heating_pdc_ha_integration.py`, `tests/test_heating_failure_detection.py`, `tests/test_room_override.py`, `tests/test_room_journal.py` (richiedono HA o fixture HA) e la suite completa.

## 4. Tabella delle differenze di parità con la card (77 scenari, codice della card al momento dell'esecuzione)

Esito: **stato, motivo, motivi secondari e scadenza coincidono con la card in tutti i 77 scenari.** Restano 16 scenari con differenze di righe, fasi o bloccante; sono tutte registrate in `parity_differences.json` e il test fallisce se ne compare una nuova o ne sparisce una. Dove la card ha due righe in contraddizione, l'integrazione ne pubblica una sola (vince l'integrazione, come da piano).

| Scenario | Card | Integrazione | Motivo |
| --- | --- | --- | --- |
| finestra aperta ma in tolleranza | riga radiatore `pass`, riga caldaia `fail`, fase ready `fail` | `window` = `wait` (`window_pending`) | La card si contraddice; ora è un solo valore |
| finestra aperta / finestra ignota / transizione PDC con finestra | fase richiesta `wait` | `pass` | La card mette il gap termico M1 nella fase «richiesta», il contratto lo mette in «caldaia» |
| unità non in °C | temperatura `pass`, valvola `fail` | temperatura `fail` (`unit_unsupported`), valvola `pass` | Il collettore attribuisce il blocco alla temperatura |
| data della temperatura assente | `unknown` | `fail` (`temperature_stale`) | Regola del collettore (età enorme) |
| centrale non valida (`count_invalid`) | tutte le righe stanza `unknown` | righe `pass`, `inputs` fallisce nella fase caldaia | Deciso in C1 |
| richiesta di calore con attributo assente e valvola ferma / stanza spenta senza attributo | riga caldaia `pass`, riga radiatore `unknown` | `unknown` | La card ha due righe che si contraddicono |
| a temperatura | riga caldaia «richiesta» `pass` | `unknown` | Idem |
| valvole illeggibili | `unknown` | `fail` (`no_active_valve`) | Regola del collettore |
| PDC in observe | rilascio radiatore `unknown` | `pass` | La card usa «gestita» (solo in control); l'integrazione legge la sessione PDC e lo split |
| stanza fuori dalla caldaia per configurazione | fase caldaia `fail`, bloccante `boilerParticipation` | fase caldaia `na`, nessun bloccante | Piano §11.3 |

Differenze che **cambiano ciò che l'utente vede** e approvate: (a) valvola sempre calcolata (era `unknown`, approvata in C2 la versione calcolata); (b) finestra in tolleranza `wait` (contratto §10); (c) stanza fuori dalla caldaia: fase `na` e nessun bloccante invece di «non partecipa».

## 5. Testo proposto per `CONTRATTI.md` §9

> ## 9. Stato per stanza (schema v1)
>
> Sensore diagnostico per ogni stanza configurata, `unique_id` `<central_entry_id>_heating_orchestration_room_<room_key>`, nome «Stato riscaldamento {room}». È una proiezione in sola lettura sui dati del collettore e della decisione M1 dello stesso giro: stesso `evaluated_at` e `generation` della caldaia, nessun comando. Stato: `heat_state` (`pump_transition`, `pump_heating`, `pump_selected`, `radiator_heating`, `boiler_waiting`, `heat_waiting`, `continuing_heat`, `at_target`, `below_start_threshold`, `regulating`, `window`, `central_off`, `safety`, `power`, `auto_pause`, `off`, `other_mode`, `unavailable`, `initializing`, `unknown`) oppure `disabled`, `starting`, `error`.
>
> Attributi: `schema_version`, `mode`, `evaluated_at`, `generation`, `room_key`, `climate_entity_id`, `heat_state`, `reason`, `detail_reasons`, `source` (`radiator`/`pdc`/`none`), `current`, `target`, `gap`, `start_gap`, `deadline`, `stages` (`{key,status,passed,total}` per `ready`, `demand`, `boiler`, `pump`), `blocker` (`{id,stage,status,reason_code}` o `null`), `conditions` (`{id,stage,status,reason_code,actual,expected,deadline,unit}`; `status` ∈ `pass`/`fail`/`wait`/`unknown`/`na`/`observe`), `pump` (`{phase,failure,started_at,deadline}` o `null`), `last_error`. La chiave unica di una condizione è (`stage`,`id`). Il bloccante è la prima condizione `fail`/`wait` lungo `ready → demand → boiler`, altrimenti la prima `unknown`; la fase `pump` non nomina mai il bloccante di un avvio a radiatore.
>
> Recorder: `conditions`, `stages`, `blocker`, `detail_reasons`, `evaluated_at`, `generation`, `deadline`, `pump` esclusi; restano stato, `source`, `reason`, `gap`. Attributi ≤ 8 KB per stanza. Freschezza lato UI come §2.

## 6. Limiti non coperti

1. **Nessuna prova in Home Assistant reale:** entità, registro entità (creazione e rimozione dei sensori), traduzioni, Recorder e dimensione reale degli attributi non sono verificati. La classe `HeatingOrchestrationRoomSensor` e la pulizia del registro sono scritte sul modello del sensore PDC ma mai eseguite.
2. **Parità solo con scenari sintetici**, non con dati dell'impianto. Il riferimento è il codice della card al momento dell'esecuzione (`reference_card.json` registra gli SHA-256 dei file letti): se la card cambia, va rigenerato con `node tests/tools/export_card_reference.mjs`.
3. **Segnali legacy della card** (timer di avvio e sensore di consenso della caldaia): nell'integrazione sono sostituiti dalla scadenza e dal motivo `awaiting_consent` della decisione M1; sull'impianto potrebbero non coincidere in ogni istante.
4. **Dati PDC:** `managed` e la fase di transazione sono letti solo con PDC in `control` (come la card); in observe una stanza PDC non è mai `pump_transition`.
5. **Controllo (`control`):** il motore legge `owner`/`mode` ma non ho provato la riga `controller` con proprietà reale, perché il controllo è chiuso dai cancelli.
6. **Reattività:** le stanze si ripubblicano quando cambia la decisione della caldaia o ogni 25 s; non c'è un listener dedicato per stanza.
7. **Anomalie valvola/VTherm** della card salute: fuori da questo incarico (piano §11.1).
8. **Id duplicati tra fasi** (`central_mode`, `temperature`, `thermal_gap`, `cooldown`, `stability`): la card deve cercare per (fase, id).
9. **Stanza spenta con attributo di richiesta ancora vero:** la card (e quindi l'integrazione, per parità) la dà `boiler_waiting`/`heat_waiting`; non l'ho corretta.

## 7. Ripristino

Tutto è locale e non committato. Per tornare indietro: eliminare i file nuovi della §2 e ripristinare i sei file modificati (`git diff` mostra solo righe aggiunte, tranne l'estrazione di `active_valves()` nel collettore). Nessuna installazione è stata fatta, quindi nessun passo da annullare sull'impianto.
