# Contratto — sensore «Stato riscaldamento» per stanza (schema v1)

Stato: **approvato (C1) con le modifiche del §10**, 25/11/2026. Origine: `output/versatile_thermostat_room_conditions_2026-11-25/PIANO-CONDIZIONI-STANZA.md` §5–§7. Il §8 elencava le divergenze tra piano e codice; le decisioni prese sono nel §10. Ogni modifica successiva a questo testo va comunicata, perché la card verrà adattata su questo contratto e sugli esempi in `tests/fixtures/room_status/sample_*.json`.

Il sensore è una **proiezione in sola lettura** sugli stessi dati del collettore caldaia: non comanda nulla, non cambia l'uscita già pubblicata (`rooms`, `excluded_rooms`, `conditions`) e non introduce soglie nuove.

## 1. Entità

- Piattaforma `sensor`, uno per ogni stanza configurata nella sezione `heating_orchestration` (anche le stanze senza PDC), categoria diagnostica, nessun polling.
- `unique_id`: `<central_entry_id>_heating_orchestration_room_<room_key>`. Nome tradotto «Stato riscaldamento {room}» / «Heating status {room}». L'entity_id generato **non** è un dato del contratto: la card lo riceve in configurazione, come per il sensore caldaia.
- **Stato** = `heat_state` (§3), oppure `disabled` (modalità `disabled`, nessuna condizione), `starting` (primi ingressi non ancora validi) o `error`. Mai `unknown` come valore inventato: `unknown` esiste solo come `heat_state` quando i dati non bastano.
- Pubblicazione nello **stesso giro** della caldaia: stessi `evaluated_at` e `generation`, aggiornamento almeno ogni 15 s, deduplica con firma (nessuna scrittura se nulla è cambiato).
- Modalità: `disabled` → stato `disabled`; `observe` e `control` → valutazione completa. Calcolare e pubblicare non dipende da `control`.
- Freschezza lato UI come CONTRATTI §2: `schema_version == 1`, `mode` ∈ {`observe`,`control`}, `evaluated_at` fra −2 s e +45 s dall'orologio del client; altrimenti la card mostra tutto come `unknown`.

## 2. Attributi

| Attributo | Tipo | Note |
| --- | --- | --- |
| `schema_version` | int | `1` |
| `mode` | str | `disabled` / `observe` / `control` (modalità caldaia) |
| `evaluated_at` | float | Secondi Unix, uguale allo snapshot della caldaia |
| `generation` | int | Uguale allo snapshot della caldaia |
| `room_key`, `climate_entity_id` | str | Identificazione della stanza |
| `heat_state` | str | §3 (uguale allo stato) |
| `reason` | str | §4 |
| `detail_reasons` | list[str] | Motivi secondari (stessi codici di `reason`) |
| `source` | str | `radiator` / `pdc` / `none` |
| `current`, `target` | float/null | Temperatura e setpoint letti |
| `gap` | float/null | `target − current`, 3 decimali |
| `start_gap` | float | Soglia di nuova richiesta = `DEFAULT_THERMAL_GAP_C` (0,3), la stessa costante di M1 |
| `deadline` | float/null | Scadenza che spiega l'attesa corrente, secondi Unix |
| `stages` | list | `{key, status, passed, total}` per `ready`, `demand`, `boiler` e, per le stanze con PDC, `pump` |
| `blocker` | obj/null | `{id, stage, status, reason_code}` (§5) |
| `conditions` | list | `{id, stage, status, reason_code, actual, expected, deadline, unit}` |
| `pump` | obj/null | Solo stanze con PDC (§6) |
| `last_error` | str/null | Errore di valutazione, senza segreti |

- `status` di una condizione ∈ `pass` / `fail` / `wait` / `unknown` / `na` / `observe`.
- `unit` ∈ `°C`, `s`, `count`, `W` oppure `null` (`W` già usata dalle condizioni PDC; vedi §8, punto 4).
- Solo valori finiti: niente NaN/Infinity (tutto passa da `json_safe` del collettore). Attributi serializzati ≤ 8 KB per stanza.
- Recorder: sono esclusi `conditions`, `stages`, `blocker`, `detail_reasons`, `evaluated_at`, `generation`, `deadline`, `pump`. Restano registrati lo stato e `source`, `reason`, `gap` (storico «Andamento», CONTRATTI §8).

## 3. Valori di `heat_state`

`pump_transition`, `pump_heating`, `pump_selected`, `radiator_heating`, `boiler_waiting`, `heat_waiting`, `continuing_heat`, `at_target`, `below_start_threshold`, `regulating`, `window`, `central_off`, `safety`, `power`, `auto_pause`, `off`, `other_mode`, `unavailable`, `initializing`, `unknown`.

Più i tre stati di servizio `disabled`, `starting`, `error` (§1). Un'attività non si deduce mai da una differenza di temperatura.

## 4. Valori di `reason`

`boiler_diagnostic`, `boiler_single`, `boiler_cooldown`, `boiler_stability`, `window_pending`, `window_closing`, `manual_override`, `schedule_change`, `waiting_trv`, `waiting_target`, `waiting_split`, `returning_radiator`, `return_unconfirmed`, `pump_serving`, `pump_measured`, `pump_selected`, `radiator_heating`, `radiator_reported`, `boiler_timer`, `boiler_consent`, `boiler_off`, `boiler_threshold`, `trv_request`, `room_demand`, `continuing_demand`, `at_target`, `small_gap`, `demand_standby`, `window`, `central_off`, `safety`, `power`, `auto_pause`, `off`, `other_mode`, `unavailable`, `initializing`, `unknown`, `unknown_reason`, `idle`, `below_target`, `heating`, `pump_hold`, `automatic_off`, `central_mode`, `preset`, `pump_window`, `power_high`, `power_missing`, `temperature_missing`, `other_pdc`, `cooldown`, `split_busy`, `data_missing`, `diagnostics_missing`, `trv_unconfirmed`, `target_unconfirmed`, `split_unconfirmed`, `conditions_changed`, `radiator_reenabled`.

Un codice ridondante o mancante emerso in implementazione sarà documentato nell'esito, senza rimozioni silenziose.

## 5. Fasi e bloccante

| `stage` | Significato | Condizioni (`id`) |
| --- | --- | --- |
| `ready` | La stanza può scaldare | `room_available`, `hvac_enabled`, `room_ready`, `initialized`, `central_mode`, `window`, `inhibition`, `safety`, `overpowering`, `temperature`, `pdc_released` (solo stanze PDC) |
| `demand` | La stanza chiede calore | `room_demand`, `valve_active` |
| `boiler` | La caldaia può partire per la stanza | `boiler_participation`, `eligible_radiator`, `controller`, e le condizioni M1 copiate dal sensore caldaia: `master`, `inputs`, `valve_count`, `thermal_gap`, `pilot`, `single_room_pilot`, `manual_request`, `path`, `stability`, `cooldown`, `consent` |
| `pump` | Percorso pompa di calore (solo stanze PDC) | condizioni inoltrate dal sensore PDC della stanza, `stage = pump` |

- Stato della fase: `fail` se c'è almeno un `fail`; altrimenti `wait`; altrimenti `unknown`; altrimenti `pass`; `na` se tutte `na`. Le `na` non entrano in `total` e `passed`; `observe` conta come `pass` per la fase ma resta `observe` nella condizione.
- **Bloccante** = prima condizione `fail` o `wait` lungo `ready → demand → boiler` (ordine delle fasi e, dentro la fase, ordine della tabella); se non c'è, la prima `unknown`; altrimenti `null`. **La fase `pump` non nomina mai il bloccante di un avvio a radiatore.**
- Le condizioni dell'elenco sono sempre presenti, tranne `pdc_released` e la fase `pump` nelle stanze senza PDC (omesse, non `na`).

## 6. Oggetto `pump`

Solo con PDC: `{phase, failure, started_at, deadline}` dall'ultima pubblicazione PDC della stanza.

- `phase`: `idle`, `waiting_radiator`, `waiting_target`, `waiting_heat`, `running`, `returning`, `returning_direct`, `return_unconfirmed`, `releasing`, `override_hold`.
- `failure`: **stringa o `null`** (codice di fallimento della transazione, es. `split_unconfirmed`). Differisce dal piano (`{code, at, restored}`): nel codice `tx.failure` è una stringa e «ripristinato» non esiste come campo (vedi §8, punto 3).
- Se la pubblicazione PDC non è disponibile: `pump = null` e una sola condizione `pump_controller` con `status = unknown`, `reason_code = pdc_unavailable`.

## 7. Comportamento di stato (`heat_state`/`reason`)

Stessa priorità di `explainRoom` della card, riassunta nel piano §7 (1 PDC in transizione → 2 non disponibile/non inizializzata/finestra/centrale spenta/sicurezza/potenza/pausa auto → 3 split in riscaldamento → 4 PDC selezionata → 5 valvola in richiesta con caldaia accesa → 6 in attesa della caldaia → 7 richiesta attiva → 8 a temperatura/sotto soglia/regolazione). In caso di dubbio vince la card, salvo differenza documentata; sulle regole di qualificazione (finestra, età temperatura, richiesta mantenuta) vince l'integrazione, come da piano.

## 8. Punti in cui il piano e il codice non coincidono (da approvare)

1. **`valve_active` e stanze già escluse.** Il collettore calcola `no_active_valve` **solo se nessun altro codice** è presente (`continue` prima del controllo valvole, `heating_boiler_collector.py:784-794`). Per una stanza esclusa per altri motivi l'esito della valvola non è disponibile dai codici. Proposta: in quel caso `valve_active` = `unknown` con `reason_code = not_evaluated`, **senza riscrivere la regola** e senza toccare il collettore. Conseguenza sulla parità: la card oggi mostra la valvola anche con la stanza bloccata da altro. Alternativa: un helper pubblico, additivo, che calcola le valvole attive a prescindere dagli altri codici. Scelta di Carlo.
2. **Codici `count_invalid` e `unit_unsupported`** (aggiunti dal collettore a ogni stanza) non hanno una riga nel piano. Proposta: `count_invalid` → riflesso dalla condizione M1 `inputs` (fase `boiler`), non come riga di stanza; `unit_unsupported` → condizione `temperature` con `reason_code = unit_unsupported`.
3. **`pump.failure`**: stringa invece di `{code, at, restored}`, perché il dato PDC esistente è una stringa (§6).
4. **`W` come unità** e condizione PDC **`setpoint`**: il collettore PDC usa l'unità `W` e pubblica anche `setpoint` (`heating_pdc_policy.py`), assenti dall'elenco del piano. Propongo di inoltrarle con `stage = pump` insieme alle altre.
5. **Tolleranza finestra**: il collettore non modella il ritardo prima della sospensione (limite del piano §11.2). Finché VTherm non espone il ritardo, `window_pending` non è prodotto e il limite sarà dichiarato nell'esito.

## 9. Esempi

Tre esempi completi (stato più attributi), con valori illustrativi (le condizioni M1 sono copiate dal sensore caldaia, non ricalcolate):

- `tests/fixtures/room_status/sample_radiator_heating.json` — radiatore che scalda con caldaia accesa;
- `tests/fixtures/room_status/sample_boiler_waiting.json` — richiesta attiva, in attesa della caldaia (stabilità);
- `tests/fixtures/room_status/sample_pump_transition.json` — stanza con PDC in transizione.

Nessun entity_id reale: solo segnaposto (`climate.example_*`).

## 10. Decisioni prese dopo il confronto con la card (C2)

Il contratto del §1–§7 resta valido; queste voci lo precisano.

1. **`valve_active` è sempre calcolata**, anche per le stanze escluse da altri motivi (approvato da Carlo). Usa `active_valves()`, la stessa espressione del collettore resa pubblica; con `active_device_ids` illeggibile resta `unknown` con `reason_code = count_invalid`. Il valore `not_evaluated` esiste solo per stanza non disponibile.
2. **Chiave di una condizione = (`stage`, `id`).** Alcuni `id` esistono in due fasi (`central_mode`, `temperature`, `thermal_gap`, `cooldown`, `stability`): la card deve cercarli per fase.
3. **Finestra in tolleranza.** Il ritardo di VTherm si ricava confrontando il sensore grezzo (acceso) con lo stato confermato (spento) senza bypass: la condizione `window` è `wait` con `reason_code = window_pending`. Il limite del §8.5 cade.
4. **`pump.failure`** è una stringa o `null`, come nel dato PDC esistente.
5. **Stanza fuori dalla caldaia per configurazione:** tutta la fase `boiler` è `na` e il bloccante non nomina la caldaia.
6. **Heartbeat:** le stanze si ripubblicano almeno ogni 25 s, come la caldaia (`PUBLISH_HEARTBEAT_S`), non ogni 15 s.
7. **Segnali legacy della card** (timer di avvio e consenso della caldaia): il motore usa la scadenza e il motivo `awaiting_consent` della decisione M1 dello stesso giro. Se la scadenza M1 è attiva una stanza che chiede calore con la caldaia spenta risulta `boiler_waiting`, anche se la sua valvola è ferma (come oggi con il timer).
