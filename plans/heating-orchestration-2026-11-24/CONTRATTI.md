# Contratti condivisi — orchestrazione riscaldamento

Coordinatore: Claude. Versione 1, 24 novembre 2026. Questo file è di proprietà del coordinatore. I worker propongono modifiche nel proprio esito e non lo modificano direttamente. Ogni cambiamento incompatibile incrementa `schema_version` e viene registrato in fondo.

Il file fissa interfacce e nomi condivisi tra il fork (filoni A e B) e Oikos (filone C). Le politiche restano quelle di `SPECIFICA.md` e degli snapshot in `baseline/`.

## 1. Configurazione nel fork

- La funzione appartiene alla **configurazione centrale** VTherm (entry con `thermostat_type` centrale) ed è memorizzata in una sezione dedicata, per esempio `heating_orchestration`. Per stabilire se il valore vive in `data` o in `options` si seguono le convenzioni effettive del fork. Se la chiave è assente la modalità vale `disabled` e il comportamento resta identico a oggi.
- `mode`: `disabled` | `observe` | `control`. `control` resta **non selezionabile** finché M2-B non è completo e revisionato. Un valore sconosciuto produce lo stato inattivo con errore esplicito, mai un controllo implicito.
- Ingressi centrali, tutti entity_id configurabili e nessuno nel codice:
  - `master_entity_id` (climate osservato e comandato dal coordinatore);
  - `central_mode_entity_id` (select);
  - `active_count_entity_id` (sensore con attributo `active_device_ids`);
  - `manual_request_entity_id`;
  - `pilot_filter_entity_id`, `pilot_ready_entity_id`;
  - `consent_entity_ids` (lista; il consenso vale vero solo se tutti sono `on`);
  - `legacy_cycle_entity_id` (facoltativo, seed di sola lettura).
- Stanze: una lista validata di elementi `{room_key, climate_entity_id, pilot_filter, pdc}`.
  - `room_key` è uno slug stabile scelto in configurazione.
  - `pilot_filter` (booleano) indica se la stanza è soggetta al veto `pilot_ok` della stanza singola.
  - `pdc` è assente oppure `{split_entity_id, session_entity_id}`.
  - Gli underlying delle valvole e il sensore finestra si ricavano dagli attributi VTherm, come fa la baseline.
- Le stanze sono identificate verso Oikos **dal `climate_entity_id`**, non da indici posizionali come nel JSON v1 legacy.

## 2. Entità diagnostica caldaia (schema v1)

- Piattaforma `sensor`, appartenente alla configurazione centrale. `unique_id` = `<central_entry_id>_heating_orchestration_boiler`, nome tradotto “Orchestrazione caldaia” / “Boiler orchestration”. Oikos non deve dipendere dall'entity_id generato: lo riceve in configurazione e propone come predefinito `sensor.<nome_generato>`, documentato nell'esito M2-A.
- **Stato**: motivo decisionale M1 (`no_demand`, `maintain`, …), oppure `disabled`, `starting` (in attesa dei primi ingressi validi) o `error`. Non viene mai usato `unknown` come motivo inventato.
- **Attributi**: JSON finito, senza NaN/Infinity, dimensione limitata. Tempi in secondi Unix (float) con UTC implicito.

| Attributo | Tipo | Note |
| --- | --- | --- |
| `schema_version` | int | `1` |
| `mode` | str | `disabled` / `observe` / `control` |
| `owner` | str/null | `observer` in observe; `controller` solo in control attivo; `null` se disabled |
| `evaluated_at` | float | Istante della valutazione (clock unico dello snapshot) |
| `generation` | int | Incrementato a ogni valutazione pubblicata |
| `proposed_action` | str | `none` / `heat` / `off` (proposta M1) |
| `reason` | str | Motivo M1 |
| `deadline` | float/null | Scadenza M1 |
| `master_mode` | str/null | Modo osservato del master nello snapshot |
| `count_raw` | int/null | Conteggio letto; `null` se non numerico |
| `count_raw_valid` | bool | Conteggio intero, non negativo e coerente con gli ID unici |
| `valid_valve_count` | int | Valvole fisiche valide; significativo solo se `inputs_ok` |
| `inputs_ok` | bool | `ok` passato a M1 |
| `inputs` | obj | `{central_mode, manual, pilot, pilot_ready, consent}` come osservati (stringhe/booleani/null) |
| `rooms` | list | Stanze idonee: `{climate_entity_id, room_key, target, gap, pilot_ok, valves: [entity_id…]}` |
| `excluded_rooms` | list | `{climate_entity_id, room_key, reasons: [codice…]}` per ogni stanza configurata non idonea |
| `conditions` | list | Condizioni M1: `{id, status, reason_code, actual, expected, deadline, unit}`; `unit` è `°C`, `s`, `count` oppure `null` |
| `cycle` | obj | `{m, off, r, s}` come M1 |
| `cycle_origin` | str | `legacy_seed` / `local_restore` / `new_observation` |
| `commanded_mode`, `last_command_at`, `last_confirmation_at` | null in observe | Valorizzati solo dal controller M2-B |
| `last_error` | str/null | Ultimo errore del collector/manager, senza segreti |

Codici di esclusione stanza, stabili e senza testo tradotto:

- stato e prontezza del VTherm: `entity_unavailable`, `hvac_not_heating`, `not_ready`, `not_initialized`, `not_boiler_participant`;
- finestra: `window_open`, `window_unknown`;
- protezioni: `inhibited`, `safety`, `overpowering`, `avatto_no_demand`;
- temperatura: `temperature_missing`, `temperature_stale`, `temperature_future`;
- PDC: `pdc_session_active`, `pdc_session_unknown`, `split_not_off`, `split_unknown`;
- valvole: `no_active_valve`, `unit_unsupported`;
- dati della centrale: `count_invalid` (le stanze non vengono valutate perché i dati centrali sono invalidi, come nella baseline).

Una stanza può avere più codici contemporaneamente: il collector li raccoglie tutti per la diagnostica, ma la qualificazione resta quella della baseline.

Recorder: `conditions`, `rooms`, `excluded_rooms`, `evaluated_at`, `generation`, `cycle` e `inputs` vanno esclusi con `_unrecorded_attributes`. Lo stato (motivo) e gli attributi brevi restano registrati.

Freschezza lato UI: il dato è valido se `schema_version == 1`, `mode` vale `observe` o `control` e `evaluated_at` cade tra −2 s e +45 s rispetto all'orologio del client. Il manager rivaluta almeno ogni 15 s.

## 3. Oikos — selezione esplicita della fonte

- Nuova impostazione delle card che usano `boilerControl.js` / `heatingChecklist.js`: `boilerSource` = `legacy` (predefinita, comportamento attuale invariato) | `native`, con `boilerDiagnosticEntityId` configurabile.
- `native` legge **solo** l'entità del punto 2 e non mescola i dati con il JSON legacy. In `observe` la card mostra chiaramente che si tratta di una proposta di osservazione: la proprietà resta del controller esterno e la riga del controller lo dice. In `control` la riga del controller è `pass` solo con `owner == controller` e dato fresco.
- Dato assente, scaduto o con schema diverso → righe `unknown`, mai `pass`. Le condizioni `na` del contratto restano `na`.
- Il passaggio da `legacy` a `native` è una modifica di configurazione delle card fatta da Carlo o dal coordinatore al momento del passaggio di proprietà, non un rilevamento automatico.

## 4. PDC (filone B) — contratto preliminare

Il contratto definitivo verrà fissato all'integrazione runtime M3/M4.

- Motori puri in moduli separati: `heating_pdc_policy.py` (decisioni avvio/mantenimento/arresto), `heating_pdc_transaction.py` (macchina a stati del passaggio radiatore↔PDC), `heating_pdc_provenance.py` (override manuale ed effetti attesi). Nessun import di HA, nessun I/O, clock esplicito, modelli frozen, come M1.
- Ogni decisione PDC espone condizioni con la stessa forma del punto 2 e codici compatibili con l'insieme legacy di `sensor.<stanza>_pdc_diagnostica`: `ready`, `managed`, `room_off`, `at_target`, `small_gap`, `schedule_change`, `data_missing`, `manual_override`, `window`, `automatic_off`, `central_mode`, `preset`, `temperature_missing`, `power_missing`, `power_high`, `other_pdc`, `split_busy`, `cooldown`, più eventuali nuovi codici documentati.
- La diagnostica runtime prevista è un sensore per stanza PDC della configurazione centrale, con `unique_id` `<central_entry_id>_heating_orchestration_pdc_<room_key>` e attributi `schema_version`, `mode`, `owner`, `proposed_action`, `reason`, `conditions`, `power_limit_w`, `stability_s`, `transaction {id, phase, started_at, deadline}`, `reservation`, `override {active, until, source}`, `last_error`.

## 5. Proprietà dei file (vedi `CLAUDE-STATUS.md`)

Un solo autore per ciascun file. I file condivisi del fork (`__init__.py`, `vtherm_central_api.py`, `config_flow.py`, `config_schema.py`, `const.py`, `sensor.py`, `translations/*`, `tests/conftest.py`, `tests/commons.py`) appartengono al filone A. Il filone B crea solo moduli e test nuovi. Il filone C lavora solo nell'SDK Oikos. `heating_boiler_policy.py` è approvato e congelato: nessun worker lo modifica senza decisione del coordinatore.

## 6. Modalità separate caldaia / PDC (v1.1)

- La caldaia usa `mode` (`disabled` / `observe` / `control`), le PDC usano `pdc_mode`, sempre nella sezione `heating_orchestration` della centrale.
- Ogni modalità è indipendente. `control` si attiva solo dall'options flow, con un passo di conferma e un `external_owner_entity_ids` proprio (caldaia, e per ogni stanza PDC).
- Il fork non accende né spegne mai un controller esterno. Finché un proprietario esterno configurato non è esattamente `off`, l'attuatore resta in osservazione con `control_status = blocked_external_owner`.

## 7. Sospensione generale «Spegni tutto il riscaldamento» (v1.1)

Decisioni di Carlo in `IDEE-IMPLEMENTAZIONE.md` §1. Qui si definisce il contratto dell'integrazione; la card non fa parte di questo incarico.

### Comandi

- Servizi `versatile_thermostat.suspend_heating` e `versatile_thermostat.resume_heating`, rivolti alla configurazione centrale. Sono pensati per la card Oikos e la richiesta di conferma è a carico della card.
- **Disponibilità**: solo se la caldaia è in `control`, **tutte** le stanze PDC configurate sono in `pdc_mode = control` e l'interblocco di proprietà è soddisfatto per ogni attuatore. Altrimenti il servizio rifiuta con l'errore `suspension_unavailable` e indica gli attuatori non controllati. In observe, o con controllo parziale, il comando non è operativo e non tocca nulla.

### Effetto

1. Si persiste subito `suspended = true` nello `Store` della centrale, con `requested_at`, `requested_by` (`user_id` HA se presente, altrimenti `unknown`) e `origin`. La sospensione resta attiva dopo riavvio e reload, senza scadenza.
2. Si bloccano le nuove accensioni:
   - la decisione caldaia diventa `off`/`none` con motivo `suspended`;
   - le PDC non avviano;
   - per ogni stanza configurata il radiatore viene sospeso tramite l'inibizione VTherm esistente. Se l'inibizione è già presente ed è di altri o di proprietà ignota, non viene acquisita: si registra `owned = false` e alla ripresa non viene rilasciata.
   Il gate dell'inibizione blocca i comandi di scheduler e manuali verso le valvole.
3. Si annullano gli override temporanei di accensione **posseduti dal fork**: override AC/PDC di M5 e transazioni PDC in corso, con la sequenza di ritorno sicura (split OFF confermato prima di qualunque rilascio). Gli override esterni, come le riprese di programma Oikos, sono **fuori copertura** e vanno documentati come tali.
4. Spegnimento:
   - caldaia `off`;
   - PDC OFF con conferma, rispettando l'ordine: prima PDC ferme, poi master;
   - conferme e ritentativi come in M2-B, con al massimo 4 invii per dispositivo.
5. Stato per dispositivo: `pending`, `confirmed`, `unreachable` (unknown, unavailable o entità mancante: **mai OFF**), `external_activation` (riacceso da telecomando, app o servizio esterno durante la sospensione).
   - Per `external_activation` si rimanda OFF con le stesse regole di tentativi, senza un ciclo infinito; dopo il limite resta `external_activation`.
   - I telecomandi fisici e le app del produttore non sono bloccabili: si possono solo rilevare e contrastare in modo limitato.
6. Stato generale (sensore `<central_entry_id>_heating_suspension`): `inactive` | `suspending` | `suspended` (tutto confermato) | `suspended_partial` (blocco attivo, ma qualche dispositivo non confermato o non raggiungibile) | `unavailable` (comando non disponibile, con il motivo).
7. La perdita di proprietà durante la sospensione (per esempio un controller esterno riattivato) produce `suspended_partial` con il motivo. Non ci sono comandi concorrenti né disattivazioni automatiche.

### Ripresa

`resume_heating`:
- rilascia **solo** le inibizioni possedute dalla sospensione;
- azzera lo stato `suspended`;
- rivaluta subito ingressi, programmi e condizioni correnti, con pause e stabilità normali;
- non ripristina gli override annullati né accende in blocco ciò che era acceso prima.

## 8. Storico e attività per le popup Andamento, Condizioni e Attività (v1.1)

Obiettivo: poter analizzare a posteriori una decisione con le condizioni **di quel momento**, senza ricostruirle dallo stato attuale.

- **Andamento** (Recorder, stati numerici). Sensori diagnostici per stanza con stato breve, registrati normalmente: fonte proposta (`radiator` / `pdc` / `none`), motivo, gap arrotondato. Gli attributi voluminosi stanno in `_unrecorded_attributes`.
- **Condizioni** (stato corrente). Attributi `conditions` dei sensori, non registrati dal Recorder: servono solo alla UI dal vivo.
- **Attività** (storico puntuale). Un'entità `event` per la centrale (`<central_entry_id>_heating_activity`) emette un evento solo alle **transizioni**: proposta che cambia, comando inviato, conferma o fallimento, inizio o fine di transazione PDC, override, sospensione o ripresa, perdita di proprietà.
  - Contenuto: `event_type`, `actuator`, `action`, `reason`, `author` (`user` con `user_id` solo se HA lo fornisce; altrimenti `automation` / `controller` / `unknown`), `mode`, `owner`, `generation`, e un **estratto compatto delle condizioni** di quel momento (id → stato e motivo, con valori numerici arrotondati).
  - Limiti: al massimo 4 KB per evento e nessun evento ad ogni tick.
  - Le entità `event` sono registrate dal Recorder: lo storico segue `purge_keep_days` (default 10 giorni). La conservazione è quindi limitata e configurabile da Carlo, senza crescita illimitata.
- **Logbook**: una voce per comando, conferma o fallimento, con prefisso `OIKOS_HEATING_V1|`, compatibile con il registro attuale.
- **Limiti dichiarati**: SmartThings non identifica chi usa il telecomando, quindi l'autore resta `unknown`. Il modo HEAT dello split non dimostra che il compressore stia funzionando. Le condizioni anteriori all'installazione non esistono.

## Registro modifiche

- v1 (24/11/2026): prima stesura del coordinatore.
- v1.1 (24/11/2026 sera): modalità separate per caldaia e PDC (§6), sospensione generale (§7), storico e attività (§8). Le aggiunte sono compatibili con v1. Gli attributi di controllo della caldaia sono definiti in `M2-B-SPEC.md`.
