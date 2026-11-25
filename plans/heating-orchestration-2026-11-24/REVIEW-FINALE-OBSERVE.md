# Review finale: prima versione installabile in observe (commit c709cf8)

Revisore: Claude (Sonnet 5.5), 25/11/2026. Solo lettura: nessuna modifica al repository, nessun commit, nessun avvio di Home Assistant.
Diff esaminato: `git diff 6a819dc c709cf8 -- custom_components tests` (31 file). Le review M2-A e M2-B non sono state ripetute.

## 1. Verdetto

**Installabile in observe: SI, con le condizioni sotto. Nessun rilievo bloccante.**

Condizioni (tutte operative, nessuna modifica di codice obbligatoria):

1. Pacchetto pulito (vedi punto 8 / R-3) e `manifest.json` con una versione distinguibile (R-2).
2. Prima dell'installazione: copia della cartella `custom_components/versatile_thermostat` installata e backup di `.storage/core.config_entries` (punto 7 / sezione 5).
3. Dopo l'installazione **non selezionare `control` nello step "Orchestrazione caldaia"** delle opzioni (R-1) e non toccare la configurazione finché il primo avvio non e' pulito nei log.
4. Il codice non e' mai girato su HA 2026.11.2 (i test girano sul 2026.10.4). Il controllo statico non ha trovato incompatibilita', ma il primo avvio reale e' la prima prova: tenere pronto il rollback.

## 2. Risposte alle domande 1-7 (con evidenza)

### 1. Disabled uguale al comportamento preesistente: confermato

Centrale **senza** la chiave `heating_orchestration`:

- `__init__.py:168` e `:177-189`: manager creati solo se `CONF_HEATING_ORCHESTRATION in entry.data` / `_has_pdc_runtime(entry)`; nessun `async_on_unload` extra. Il `try/except BaseException` (`:191-204`) attorno al forward fa solo `raise` di nuovo; per le entry non centrali e' trasparente.
- `__init__.py:247-252, 261, 267`: in unload `had_*` e' falso (manager `None`), nessun ramo nuovo.
- `sensor.py:93`: entita' create solo con la chiave presente. Nessuna entita' senza configurazione.
- Nessuno `Store` creato senza configurazione: lo store si istanzia solo in `async_start` (`feature_heating_orchestration_manager.py:483`, `feature_heating_pdc_manager.py:374`), che esce subito se disabled/invalido.
- Unica nuova attivita' senza chiave: `async_remove_entry` (`__init__.py:275`), eseguita solo alla **rimozione** della entry centrale; `Store(...).async_remove()` e' idempotente.
- Differenze visibili: il menu del flusso centrale ha due voci in piu' (`config_flow.py:650-652`: "heating_orchestration", "heating_pdc"), con traduzioni en/it presenti in `config` e `options`.
- `vtherm_central_api.py`: solo attributi e metodi aggiunti; `central_boiler_manager`, `central_power_manager` e il resto invariati.
- `const.py`/`config_schema.py`: aggiunte; `CONFIG_VERSION=2` / `CONFIG_MINOR_VERSION=4` invariati, quindi nessuna migrazione e nessun `async_migrate_entry` toccato.
- Import: i moduli `heating_*` sono puri (nessun import `homeassistant`/`vtherm_api`, verificato), nessun ciclo con `const.py`. Un errore di import romperebbe tutta l'integrazione anche da disabilitata, ma i test lo escludono.
- Gestore caldaia nativo, Avatto, inibizione e finestre: file non toccati (il diff non contiene `feature_central_boiler_manager.py`, `inhibition_manager.py`, `base_thermostat.py`, feature finestra).
- Con la chiave presente e `mode: disabled`: manager creato in stato `disabled`, sensore creato, nessuno store, nessun listener (`async_start` esce alla riga 480).
- Traduzioni: confronto chiave per chiave con `6a819dc`: 0 chiavi rimosse, 0 modificate, 241 aggiunte per lingua, JSON valido, tutti i codici d'errore dei flussi (`ho_*`, `pdc_*`, `unknown_entity`) presenti in `config.error` e `options.error`. L'incidente `tr.py` non ha lasciato danni.

### 2. Observe senza effetti

Servizi/comandi/inibizioni (grep su `feature_heating_*`, `heating_*`):

- **Caldaia**: un solo `services.async_call` in tutto il codice, `feature_heating_orchestration_manager.py:971` (`climate.set_hvac_mode` sul master), raggiungibile solo da `_async_send` <- `_launch_send` <- `step.send`. In observe `_control_plane` esce alla riga 827-830 (`mode != control` -> `None`), quindi `step` e' `None` e `_launch_send` non parte (riga 820). Nessun riferimento a `input_*`, `timer.`, `automation.`, inibizione (il collector solo *legge* `inhibited`, riga 755-757). Gli eventi logbook (`bus.async_fire`, riga 1081) sono eventi, non servizi, e in observe non vengono emessi (`_startup_events` vuoti senza record attivo).
- **PDC**: `call_service`/`set_inhibition`/`unset_inhibition`/`save` passano da `_HassPorts._guard()` (`feature_heating_pdc_manager.py:137-140`), che solleva in observe. I mirror su `input_boolean` (`heating_pdc_runtime.py:972-1003`) sono raggiungibili solo da percorsi `control` (`_recover`, `_on_tx_changed`, override) e anche se fossero chiamati cadrebbero sulla guardia (eccezione intercettata, riga 988). `persistent_notification` (`:757, :763`) solo su cambio di transazione (control). Il listener `EVENT_CALL_SERVICE` e' registrato solo in control (`feature_heating_pdc_manager.py:422`).
- **Dove si scrive lo store**:
  - Caldaia (observe): `async_delay_save` (`:610`, dal `_schedule_save` della valutazione, riga 799) e `async_save` in stop (`:553`). Scrive solo il **proprio** file `.storage/versatile_thermostat.heating_orchestration.<entry_id>` (contesto del ciclo); innocuo. Il load puo' migrare e riscrivere lo stesso file (v1->v2).
  - PDC (observe): nessuna scrittura. Lo `Store` e' solo letto (`async_load`); `_ports.save` e' bloccato dalla guardia.
- **Primo avvio**: `waiting_for_home_assistant` / `waiting_for_entities` (boiler `:772-783`; PDC `:623-641`) pubblicano `starting` senza leggere nulla di rischioso.
- **Errore del collector**: boiler, ogni fase isolata (`:786-818`), pubblica `error`, riarma gli ascolti. PDC: `async_run_pass` intercetta `TypeError/ValueError/KeyError` (`:654`); altre eccezioni arrivano a `_work` (`:410`) che logga e prosegue (vedi R-4).
- **Restore/seed**: boiler `_seed_once` legge il legacy cycle (read-only, `:652`); PDC `_seed_cycle` solo in control (`:579-580`).
- **Reload/unload/shutdown**: `async_stop` annulla listener, timer, pending, task; non invia compensazioni. `EVENT_HOMEASSISTANT_STOP` chiama lo stop del manager (`:501-504`, PDC `:412-415`), con rimozione del proprio `once` dalla mappa prima di fare `unsub` (nessuna doppia rimozione).

### 3. Control non attivabile per errore

**Caldaia** (`control` e' selezionabile solo dalle opzioni, vedi R-1):

- `parse_orchestration_config`: `control` richiede `external_owner_entity_ids` non vuoto (`heating_boiler_collector.py:392`) e `control_confirmed is True` (`:399-401`); altrimenti errore e modo effettivo `disabled`.
- `control_confirmed` e' scritto solo dallo step di conferma (`config_flow.py:849-865`), disponibile solo nel flusso opzioni (`_ho_allow_control`, `config_flow.py:68, 1659`). Nel flusso di creazione `control` e' rifiutato (`:813`) e non e' nelle opzioni del selettore (`config_schema.py:264`). La conferma non si trascina: `candidate` e' ricostruito dal solo `user_input` (`:805-808`). `async_finalize` declassa a `observe` un `control` non confermato (`:1617-1621`).
- Dopo installazione/migrazione/reload/riavvio: la entry **non ha la chiave** (installazione) e nessuna migrazione la crea; un `control` gia' confermato resta tale al riavvio (voluto), ma dietro assestamento e interlock di proprieta' (M2-B).

**PDC** (5 barriere, in serie, verificate):

1. UI: `HO_PDC_SELECTABLE_MODES = [disabled, observe]` (`const.py`) e rifiuto in `config_flow.py:966-970`.
2. `PDC_CONTROL_AVAILABLE = False` (`feature_heating_pdc_manager.py:70`), passato al parser...
3. ... `parse_pdc_config(allow_control=False)` rifiuta `control` con `control_not_available` (`heating_pdc_collector.py:319-320`): config invalida, nessun runtime costruito, sensori in `error`.
4. `PdcRuntime.__init__` solleva `PdcControlUnavailableError` (`heating_pdc_runtime.py:283-285`).
5. `_HassPorts._commands_allowed` falso -> `_guard()` su servizi, inibizione e scrittura (`feature_heating_pdc_manager.py:135-140`).

Piu' i gate di modalita' interni (`_local_inputs`, `_control_pass`, `on_call_service` solo in control). Limite: le barriere 2, 3, 4 e 5 dipendono tutte dalla stessa costante `PDC_CONTROL_AVAILABLE` (R-5, minore).

### 4. Rollback
Vedi sezione 3 piu' sotto.

### 5. Lifecycle

- **Ordine in `__init__`**: creazione manager *prima* del forward (`:168-189`), `async_start` *dopo* (`:193-197`), `except BaseException` che li rimuove e rilancia (`:198-204`). In unload: stop *prima* di `async_unload_platforms` (`:247-253`), ripristino con nuovo manager avviato se lo scarico fallisce (`:261-270`).
- **`async_on_unload`**: HA 2026.11.2 esegue `self._on_unload.pop()()` e awaita il coroutine come task della entry (`config_entries.py:1380-1393`); esecuzione in ordine inverso, quindi `_stop_*` (registrati dopo l'update listener) girano per primi; vengono chiamati solo se lo scarico riesce o se il setup fallisce (`:1179`, `:1045`). Sono chiavati sull'`entry_id` (`vtherm_central_api.py` `async_remove_*`), quindi non toccano un manager piu' nuovo.
- **Job/timer/listener**: boiler `_unsubs`, `_once_unsubs`, `_state_unsub`, `_deadline_timer`, `_command_timer` (token), `_pending` (`call_soon`), `_send_task`, `_save_tasks`: tutti annullati in `async_stop` (`:528-557`). PDC: `_unsubs`, `_once_unsubs`, e nel runtime `_cancel_wake` + cancel dei worker (`:381-385`). Le callback tardive sono neutralizzate da `epoch` (boiler `:491-504`, PDC `:392-415`, wake `:438-442`) e dal token del timer.
- **Reload della centrale con TRV/PDC ricollegati**: i manager leggono solo `hass.states` (nessun riferimento a oggetti VTherm), quindi nessun riferimento stantio. Gli `unavailable` transitori durante il reload producono solo decisioni/diagnostici, mai effetti in observe. Il sensore si riaggancia al manager nuovo in `async_added_to_hass` (`sensor.py`), con stato `error/manager_missing` se manca.
- **Sensori**: `async_will_remove_from_hass` fa il detach; `_async_publish` controlla `hass` ed `entity_id`.
- Limite noto e documentato: dopo uno scarico fallito il manager e' ripristinato ma i sensori restano agganciati al vecchio (R-6).

### 6. Inibizione
Nessun percorso, anche inaccessibile, acquisisce o rilascia un'inibizione altrui senza essere proprietario:

- La caldaia non usa l'inibizione.
- PDC: `set_inhibition`/`unset_inhibition` solo tramite `_HassPorts` (guardia). Nel codice di control (non raggiungibile) l'acquisizione e' saltata se `inhibited_now is True` (`heating_pdc_runtime.py:899`), la proprieta' si accerta con il marcatore di transizione (`:909-912`), e il rilascio avviene solo con `tx.inhibition_owned` e fase `RELEASING` (`:946`). Nota: se l'attributo `inhibited` non e' leggibile (`inhibited_now is None`) la chiamata parte comunque, ma la proprieta' resta non dichiarata (`transitioned=None`). E' codice morto in questa release; da riesaminare quando si apre il control.

### 7. Compatibilita' statica con HA 2026.11.2
Download riuscito (`homeassistant-2026.11.2-py3-none-any.whl`, 68,8 MB, scratchpad `hawheel`), letto come zip, mai eseguito ne' installato. Metadata: richiede Python >= 3.14.2.

| API usata dal codice nuovo | Esito sui sorgenti 2026.11.2 |
|---|---|
| `ConfigEntry.async_on_unload` con coroutine function | OK (`config_entries.py:1372-1385`) |
| `async_remove_entry(hass, entry)` | OK (`:1237-1240`) |
| `runtime_data` | non usato dal codice nuovo |
| `Store(hass, version, key)`, `async_load/async_save/async_delay_save/async_remove`, `_async_migrate_func` a 3 parametri | OK (`storage.py:295-308, 502-513, 533, 664`). Nuovo: `max_readable_version` e `UnsupportedStorageVersionError(HomeAssistantError)` (`exceptions.py:496`), gia' catturata da `HomeAssistantError` |
| `async_track_state_change_event`, `async_track_time_interval`, `async_track_point_in_utc_time`, `async_call_later` | OK, firme invariate (`event.py`) |
| `bus.async_listen(event_filter=)` richiede callback | OK, `_climate_only` e' `@callback` (solo control) |
| `hass.async_create_task(target, name, eager_start)`, `hass.is_stopping` | OK (`core.py:515, 854`); default `eager_start=True` gestito dal boiler, PDC passa `False` |
| `EntityCategory`, `EVENT_LOGBOOK_ENTRY`, `ATTR_DOMAIN/NAME`, `EVENT_CALL_SERVICE`, `EVENT_HOMEASSISTANT_STARTED/STOP` | OK (`const.py`) |
| `_unrecorded_attributes` | OK (`helpers/entity.py:596, 642`) |
| Logbook: chiavi `message/name/domain/entity_id` | OK (`logbook/const.py:36-43`) |
| `persistent_notification.async_create(hass, message, title, notification_id)`, `async_dismiss(hass, id)` | OK (`helpers.py`) |
| Selettori: `EntitySelectorConfig(domain, multiple)`, `SelectSelectorConfig(options, multiple, mode, translation_key)`, `NumberSelectorMode.BOX`, `BooleanSelector`, `TextSelector` | OK (`selector.py`) |
| `dt_util.utcnow`, `utc_from_timestamp`, `get_default_time_zone`, `hass.config.units.temperature_unit` | OK |
| Config flow `async_show_menu` / `add_suggested_values_to_schema` | invariati rispetto al codice preesistente |

Punto di attenzione: HA 2026.11.2 **non dipende piu' da voluptuous**, ma da `probatio==0.13.0`, e in `homeassistant/__init__.py` fa `install_as_voluptuous`; quindi `import voluptuous as vol` (usato anche da tutto il codice preesistente) continua a funzionare. `probatio.Schema.schema` e `Marker.schema` esistono (usati da `{key.schema for key in schema.schema}` in `config_flow.py`). Non e' una novita' del commit.

### 8. Manifest e pacchetto: vedi sezione 4.

## 3. Rollback (domanda 4)

Con `6a819dc` o `6e44449` rimesso su una entry gia' modificata:

- **Setup e config flow vecchi non si rompono.** `git show 6a819dc:__init__.py`, `config_flow.py`: nessun accesso a `heating_orchestration`/`pdc_*`, nessuna validazione stretta dei dati della entry (`extra=vol.ALLOW_EXTRA` solo su `CONFIG_SCHEMA` YAML), il flusso opzioni parte da `config_entry.data.copy()` (`:~1680`) e in `async_step_finalize` riscrive `data=self._infos`: le chiavi sconosciute **sopravvivono** intatte (shallow copy, il vecchio codice non le tocca).
- `CONFIG_VERSION/MINOR` identici (2.4): HA non vede un downgrade di versione della entry.
- **Store nuovi**: `.storage/versatile_thermostat.heating_orchestration.<entry_id>` (v2) e `.storage/versatile_thermostat.heating_orchestration_pdc.<entry_id>` (v1). Il codice vecchio non li legge: innocui. Il vecchio codice non ha `async_remove_entry`, quindi cancellando la entry restano orfani (pochi kB).
- **Entita' orfane**: `sensor.*heating_orchestration_boiler` e `sensor.*heating_orchestration_pdc_<room>` restano nel registro e dopo il riavvio diventano "non piu' fornite dall'integrazione" (`restored`, `unavailable`). Innocue, ma **le card/automazioni Oikos che le leggono vedranno `unavailable`**: da tenere presente.
- **Un solo rischio in avanti**: se la entry contiene `mode: control` + `control_confirmed: true`, reinstallando il codice nuovo il control si riattiva (con assestamento e interlock). Prima del rollback, o in observe, non lasciare `control` confermato.

**Procedura di pulizia piu' sicura** (nessun comando fisico, tutto reversibile):

1. Con il codice **nuovo** ancora attivo: opzioni -> Orchestrazione caldaia = `disabled` e Pompa di calore = `disabled`, salva (ferma i manager; i sensori restano in stato `disabled`). Verificare nei log che non ci siano errori.
2. Fermare HA (o fare un backup completo). Ripristinare la cartella dell'integrazione precedente (copia fatta prima dell'installazione). Avviare HA e controllare che le entry carichino.
3. Impostazioni -> Dispositivi e servizi -> Entita': eliminare i sensori orfani `*_heating_orchestration_*` ("non disponibile").
4. Solo se serve pulizia completa, a HA fermo: cancellare i due file `.storage` sopra e, facoltativamente, la chiave `heating_orchestration` dalla entry in `core.config_entries` (backup prima). Lasciarla e' innocuo e permette di riattivare l'observe con la stessa configurazione.
5. Non usare `git restore`/checkout sul repository di lavoro per questo: il rollback riguarda la cartella installata, non il repository.

## 4. Manifest e pacchetto (domanda 8)

**Versione proposta**: `10.6.3+oikos.1` (successiva: `10.6.3+oikos.2`...).

- Verifica sui sorgenti: HA valida la versione dei custom con `AwesomeVersion(..., ensure_strategy=[CALVER, SEMVER, SIMPLEVER, BUILDVER, PEP440])` (`loader.py:848-868`); una versione non valida **non** blocca il caricamento (solo avviso), ma meglio evitarla. La regex SemVer di AwesomeVersion 25.8.0 (`utils/regex.py`) accetta sia `10.6.3-oikos.1` sia `10.6.3+oikos.1`; `CALVER` non le cattura (il `+`/`-` non e' ammesso), quindi si cade su SEMVER.
- Perche' `+` e non `-`: in SemVer il suffisso `-oikos.1` e' una *pre-release* e ordina **prima** di `10.6.3`; il `+` (build metadata) non cambia la precedenza. Se HACS segue il fork originale, con `-` mostrerebbe "aggiornamento disponibile a 10.6.3" e potrebbe sovrascrivere. Non ho eseguito AwesomeVersion per confrontare l'ordinamento: verificare in HACS dopo la prima installazione. Se l'installazione resta manuale (come per 6e44449) il punto e' irrilevante.
- `hacs.json`: `zip_release: true`, `filename: versatile_thermostat.zip`, `homeassistant: 2026.3.1` (minimo); il workflow di rilascio imposta la versione dal tag e zippa **dal contenuto** della cartella (zip senza cartella radice). Per un'installazione manuale basta copiare la cartella.

**Il pacchetto deve contenere** `custom_components/versatile_thermostat/` con: tutti i `*.py` (compresi i nuovi `heating_*.py` e `feature_heating_*_manager.py`), `manifest.json`, `services.yaml`, `icons.json`, `translations/` (11 file), `brand/` (4), `llm/` (3).

**Da escludere**: `__pycache__/` (17 file presenti nell'albero di lavoro), `*.pyc`, la cartella vuota `custom_components/homeassistant/`, `tests/`, `plans/`, `.git*`, `.devcontainer`, `.github`.

**Dipendenze del manifest** (invariate, `requirements`): `numpy`, `scipy`, `vtherm_api>=0.7.0`; `dependencies: []`, `after_dependencies: [recorder, http]`. Verificato sulle wheel: `vtherm_api` 0.7.0 e 0.13.1 espongono entrambe `get_vtherm_logger`, `set_inhibition`, `unset_inhibition`, `is_inhibition_started`, quindi il minimo `>=0.7.0` basta anche per il codice nuovo (la parte inibizione e' comunque importata tardi e solo in control). Nessuna dipendenza nuova da aggiungere: `persistent_notification` e' un componente core caricato sempre; il logbook non e' importato (la chiave `message` e' ridefinita localmente, riga 151 del manager).

## 5. Rilievi numerati

**Bloccanti: nessuno.**

**Importanti**

- **R-1. `control` della caldaia selezionabile in una release "observe".** `config_schema.py:264` (`HO_OPTIONS_MODES`), `config_flow.py:798`, `const.py` (`HO_OPTIONS_MODES`). Scenario: nel flusso opzioni, scegliendo `control` e spuntando la conferma con almeno un `external_owner_entity_ids`, la caldaia viene comandata (`climate.set_hvac_mode` sul master) dopo l'assestamento. Non e' attivabile "per errore" (servono selezione, lista proprietari e casella di conferma), ma a differenza del PDC non c'e' una costante di release che lo spenga. Correzione (a scelta): introdurre `HO_BOILER_CONTROL_AVAILABLE = False` che tolga `control` da `HO_OPTIONS_MODES` e lo faccia rifiutare dal manager come per il PDC; oppure accettarlo e documentare nel runbook "non selezionare control". Decisione di Carlo.
- **R-2. Versione non distinguibile.** `manifest.json:25` e' `10.6.3`, uguale alla versione probabilmente installata (6e44449): dopo l'installazione non si potrebbe dire quale codice gira. Correzione: `10.6.3+oikos.1` (sezione 4).
- **R-3. Pacchetto: albero di lavoro sporco.** 17 file `__pycache__` in `custom_components/versatile_thermostat/` e cartella vuota `custom_components/homeassistant/`. Costruire il pacchetto da `git archive c709cf8 custom_components/versatile_thermostat` (o escludere esplicitamente) e controllare che manifest, `translations/*.json` e i nuovi `.py` siano presenti.
- **R-7. Mai provato su HA 2026.11.2.** I test (1063) usano HA 2026.10.4; il venv 2026.11.2 non e' stato lanciato. Controllo statico: nessuna incompatibilita' (sezione 2.7). Mitigazione: installare con rollback pronto, controllare i log al primo avvio (errori di import, `Error setting up entry`, `Error unloading`), poi il sensore diagnostico `disabled`/`starting`.

**Minori**

- **R-4. Errore PDC "appiccicoso" in observe.** `heating_pdc_runtime.py:412` imposta `room.last_error = "pass_error:..."` e in observe nessuno lo azzera (solo `:647`, nel ramo control). Inoltre le eccezioni diverse da `TypeError/ValueError/KeyError` (`:654`) non pubblicano `error`: il sensore resta all'ultimo valore, con `evaluated_at` che invecchia. Correzione: azzerare `last_error` all'inizio di `_observe_pass`, e catturare `Exception` in `async_run_pass` pubblicando `error`.
- **R-5. Barriere PDC con un'unica costante.** `feature_heating_pdc_manager.py:70, 135, 215, 386`: una modifica a `PDC_CONTROL_AVAILABLE` apre insieme parser, motore e porte. Accettabile (barriere in serie, UI separata); suggerito un test che fissi `PDC_CONTROL_AVAILABLE is False` e `pdc_mode: control` -> errore.
- **R-6. Sensori non riagganciati dopo scarico fallito.** `__init__.py:261-270`: il manager ripristinato non ha sensori attaccati (documentato nell'ESITO). In observe e' solo un diagnostico fermo fino al prossimo reload.
- **R-8. Spegnere la caldaia dal flusso cancella i legami.** `config_flow.py:805-808`: con `mode: disabled` si salva solo `mode` (+ stanze + PDC); entita' master/consensi/proprietari vanno reinseriti. Voluto per sicurezza (azzera la conferma), ma sorprendente.
- **R-9. Stanze PDC rimosse lasciano il sensore orfano** nel registro (`sensor.py:98-101`): da cancellare a mano.
- **R-10. Ogni salvataggio delle opzioni ricarica tutte le entry VTherm** (`__init__.py:230-231`, preesistente). In observe e' innocuo, ma i diagnostici passano da `starting` ad ogni modifica.

## 6. Cio' che non ho potuto verificare

- Esecuzione reale su HA 2026.11.2 (vietata): compatibilita' solo statica; non ho verificato `async_show_form`/serializzazione dei selettori sul frontend 2026.11 con `probatio`.
- Non ho rilanciato i 1063 test: l'ambiente `m1venv` non ha `pytest_socket` (i test con `tests/conftest.py` richiedono l'ambiente HA di test). Mi fido del dato del coordinatore.
- Contenuto reale della `core.config_entries` dell'impianto (se la entry centrale ha gia' la chiave `heating_orchestration`), versione effettiva di `vtherm_api` installata, e se l'impianto usa HACS per questo fork.
- Ordinamento di `10.6.3+oikos.1` rispetto a `10.6.3` in HACS: verificata solo l'accettazione della regex, non il confronto (non ho eseguito AwesomeVersion).
- Comportamento sotto carico (numero di valutazioni PDC/boiler per ogni aggiornamento dei climate) e scritture recorder degli attributi: non misurati; gli attributi voluminosi sono in `_unrecorded_attributes`.
- Hassfest/validazione HACS del manifest.
