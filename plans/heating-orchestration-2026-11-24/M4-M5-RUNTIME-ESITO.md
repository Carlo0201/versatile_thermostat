# M4-M5 e runtime PDC — Esito (W2)

Stato: consegna del Worker W2 sul codice locale del fork, **non committata**. Nessuna HA in esecuzione, nessun accesso all'impianto, nessun commit, push, stash, reset o clean. La prima versione installabile è **solo observe**: il controllo PDC esiste nel codice ma non è raggiungibile (§5).

## 1. Review di M3, M4 e M5

**M3 (`heating_pdc_policy.py`)**: nessuna modifica (SHA-256 invariato). Verificato che le due stanze differiscono solo per `setpoint_min_c` (16 / 18) e `manual_preset_bypass` (Carlo): sono parametri della configurazione di stanza. Soglie, priorità e baseline non sono state toccate. Anomalie della baseline (`M3-ESITO.md` §6): 1 (fronte di avvio unico), 4, 5, 6, 9 **preservate**; la 2 (eventi persi durante la sequenza) è **corretta nel runtime** (un worker per stanza rilegge ogni evento e le protezioni vengono valutate anche durante la transazione, §4); la 3 e la 7 sono gestite (override congela la transazione e poi la riporta indietro; `return_unconfirmed` ritenta ogni 120 s, `return_retry_s = 0` ripristina il comportamento baseline); la 8 è definita (rilascio solo se posseduta).

**M4 (`heating_pdc_transaction.py`)**, difetti trovati e corretti. Tutte sono modifiche in senso più prudente della baseline, da approvare:

1. **Proprietà dell'inibizione assunta sempre.** `request_start` impostava `inhibition_owned=True` a prescindere (un test lo affermava con `radiator_inhibited_before=True`). Ora `request_start` richiede esplicitamente `radiator_inhibited_before`: `True` rifiuta (`radiator_inhibited_foreign`), `None` rifiuta (`radiator_inhibition_unknown`), solo `False` genera il possesso.
2. **La macchina si fidava di `conditions_ok`.** Si poteva passare al setpoint con arresto del radiatore non confermato e inviare HEAT senza setpoint confermato o con radiatore non fermo, e arrivare a `running` senza arresto confermato. Ora ogni passaggio richiede insieme `radiator_stopped`, `target_confirmed` (per HEAT), `conditions_ok` e possesso.
3. **Ritorno chiuso dal solo split OFF.** Si poteva rilasciare il radiatore con il VTherm PDC ancora in `heat` (che avrebbe rimandato HEAT allo split). Ora servono split **e** VTherm PDC `off`. Lo `unknown` non è mai conferma.
4. **Rilascio "fire and forget".** La transizione a `idle` avveniva alla emissione del comando: un `unset_inhibition` fallito perdeva il possesso e lasciava il radiatore inibito per sempre. Nuova fase `releasing`: conferma con `confirm_release`, ritenta ogni 30 s, si rivalida a ogni tentativo (se la PDC è tornata attiva si torna al ritorno), si chiude se il VTherm dichiara di non essere inibito.
5. **HEAT in volo.** Un OFF visto subito dopo l'invio di HEAT (latenza cloud) chiudeva la sessione e un HEAT tardivo poteva arrivare a radiatore aperto. Il comando OFF conta solo dopo `heat_settle_s` (20 s) dall'invio di HEAT non ancora confermato (`next_wakeup`).
6. **`return_unconfirmed` terminale.** Una conferma OFF tardiva era ignorata. Ora completa il ritorno; senza conferma si ritenta.
7. **Impossibile esprimere le protezioni sotto override.** `request_return` richiedeva override `off`, mentre la baseline le esegue con override `on` (protezioni, ripresa automatico, split spento dall'utente). Nuovo `override_exempt`, persistito nella transazione.
8. **Sessione manuale assente** (`override_sincronizza` mette `in_gestione` ON): nuovo `adopt_manual`.
9. **Persistenza assente**: `transaction_to/from_dict` e `reservation_to/from_dict` con validazione stretta (un dato non valido non viene mai interpretato).
10. `record_inhibition_result`: dopo la chiamata il runtime verifica che l'inibizione sia stata creata da lui (flag e marcatore `last_transition_at` cambiato); se la trova già presente declassa il possesso e abbandona l'avvio.

**M5 (`heating_pdc_provenance.py`)**: ricontrollato contro `registro_comandi`, `override_manuale` e `fine_override`: 75 s con scadenza intera, scadenza `<` stretta, campi `m/t/f/p/s`, effetti Quiet→fan auto e OFF→preset none, classificazione dei cambi, utente autenticato solo con `user_id`, dispositivo ritrovato attivo. **Nessun difetto**, file invariato. Limiti dichiarati: SmartThings non identifica chi usa il telecomando (autore `unknown`, mai promosso a `user`); una chiamata da automazione ha `user_id` assente; il baseline registra gli effetti attesi solo per i comandi sullo split, non sul VTherm PDC, e il runtime fa lo stesso.

## 2. Decisione sull'inibizione

`InhibitionManager.async_set` è idempotente: una chiamata su un VTherm già inibito non cambia nulla e non dice di chi è. Regola adottata:

- il possesso (`inhibition_owned`) vale vero **solo** se, immediatamente prima della richiesta, il VTherm riporta esplicitamente `inhibited: false` (o nessuna sezione di inibizione con `specific_states` presente, cioè mai usata) e dopo la chiamata riporta `inhibited: true` con un `last_transition_at` nuovo;
- **inibizione già presente o di proprietario ignoto all'avvio PDC: l'avvio è bloccato** con motivo diagnostico (`radiator_inhibited_foreign`, `radiator_inhibition_unknown`). Proseguire senza possesso non è sicuro: il proprietario estraneo può rilasciare in qualsiasi momento riaprendo il radiatore mentre la PDC funziona (doppia fonte). È coerente con la baseline: un radiatore inibito ha `radiator_mode = off`, quindi la policy non porta mai a `start` (codice `room_off`);
- il possesso è scritto sullo store **prima** della chiamata (write-ahead); senza scrittura durevole non parte alcuna sessione. Il rilascio avviene solo se posseduta, dopo split e VTherm PDC `off` riletti subito prima della chiamata e mai durante la sospensione generale (con `hold_radiator_inhibition`);
- limite residuo: se un altro attore inibisce nella finestra di attesa del lock del core tra la nostra lettura e la nostra chiamata, il marcatore cambia e non distinguibile da una nostra transizione. Nell'impianto l'unico meccanismo che inibisce è questo manager (il legacy è escluso dall'interlock);
- anche il percorso di controllo, non raggiungibile in questa versione, non acquisisce né rilascia mai un'inibizione non posseduta (test e mutazioni).

## 3. Architettura

File nuovi (nessuna dipendenza da HA nei primi tre):

- `heating_pdc_collector.py` (puro): configurazione (`parse_pdc_config`, `allow_control` falso di default), lettura degli stati in `PdcSnapshot` (stessa semantica `float(none)`, `last_reported`, `last_changed` delle automazioni), `build_observation`, protezioni durante l'avvio, diagnostica.
- `heating_pdc_runtime.py` (puro, asyncio, **file aggiunto rispetto all'elenco assegnato**, necessario per testare senza HA la logica di sequenza): un worker per stanza, observe, control, interblocco, prenotazione condivisa, write-ahead, recupero, provenienza M5, override e sospensione generale. Porte iniettate (`PdcPorts`).
- `feature_heating_pdc_manager.py` (glue HA): listener, `Store` versionato, API VTherm di inibizione, publisher, servizi. In observe la porta `_HassPorts` rifiuta comunque servizi, inibizione e scritture (seconda barriera).

Conferme: si usano solo gli stati osservati. HEAT dello split non è attività del compressore: `hvac_action` dello split è mostrato in `split_action`, mai usato per decidere. Unknown e unavailable non valgono OFF né richiesta nulla. Rivalutazione dopo ogni attesa: i comandi hanno token e vengono scartati se non più correnti; HEAT è rivalidato con una lettura fresca subito prima della chiamata; l'interlock è riletto prima di ogni comando.

Protezioni durante la transazione (aggiunta, in senso prudente): potenza assente/vecchia/≥2700 W, modo centrale, automatico o programma non più compatibili, finestra; rilevate a ogni evento invece che al prossimo punto di controllo. Avviata la PDC, un VTherm che dichiara il radiatore non inibito mentre noi lo possediamo ferma la PDC (seconda fonte).

Classificazione della provenienza: i cambi dello split sono classificati dopo 1 s (come la baseline) e le decisioni sono sospese durante l'attesa, per non fermare per errore una PDC che l'utente ha appena cambiato. La copia manuale split→VTherm PDC (script `sincronizza`, letto solo come riassunto) copia sempre se diverso, con ripetizione limitata a 60 s: la dipendenza dai comandi attesi del baseline non è verificabile.

## 4. Stato observe e control

| | observe (consegnato) | control (codice presente, non raggiungibile) |
| --- | --- | --- |
| Comandi, inibizione, store | nessuno (verificato) | sì, solo con `allow_control` |
| Decisione e diagnostica per stanza (CONTRATTI §4) | sì | sì |
| Transazione, prenotazione, override, sospensione | no | sì |
| Cicli e helper legacy | letti (`in_gestione`, override, ciclo) | locali, con specchio facoltativo degli helper |

Barriere che rendono `control` irraggiungibile: (1) il flow offre solo `disabled`/`observe` (`HO_PDC_SELECTABLE_MODES`); (2) `parse_pdc_config` rifiuta `control` con `control_not_available` salvo `allow_control`; (3) il manager passa `allow_control=PDC_CONTROL_AVAILABLE` (costante `False`) e uno stato salvato `control` diventa errore esplicito per ogni stanza, senza costruire il runtime; (4) `PdcRuntime` solleva `PdcControlUnavailableError` se costruito in control senza `allow_control`; (5) le porte del manager rifiutano comandi, inibizione e scritture se non in control. Un test dimostra ciascuna barriera e la mutazione di ognuna è uccisa.

Controllo non terminato (non attivare): interblocco `external_owner_entity_ids` ancora da cablare nel flow, lista concreta dei proprietari da decidere, collegamento sospensione generale alla card, specchio di `in_gestione` per la sessione caldaia. Le chiavi esistono già nel parser.

## 5. Integrazione nei file condivisi

Configurazione, nella sezione `heating_orchestration` della centrale (entry.data), separata dalla caldaia:

```
heating_orchestration:
  mode: <caldaia, invariato>
  pdc_mode: disabled | observe          # `control` rifiutato
  pdc_runtime:
    power_entity_id, central_mode_entity_id
    rooms: [ {room_key, radiator_entity_id, radiator_valve_entity_id, temperature_entity_id, window_entity_id,
              split_entity_id, pdc_entity_id, automatic_entity_id, schedule_stop_entity_id,
              managed_entity_id, override_entity_id,            # obbligatori in observe (letti)
              cycle_entity_id, override_timer_entity_id, schedule_switch_entity_ids,   # facoltativi
              setpoint_min_c (16), manual_preset_bypass (false)} ]
```

- Chiavi assenti: nessun manager, nessun sensore, entry identica a oggi. Il passo del flow caldaia riporta `pdc_*` senza toccarle.
- `const.py`: `CONF_HO_PDC_MODE`, `CONF_HO_PDC_RUNTIME`, `CONF_HO_PDC_ROOMS_TO_REMOVE`, `HO_PDC_SELECTABLE_MODES`, `HO_PDC_SENSOR_SUFFIX`, `HO_PDC_STORAGE_KEY_PREFIX`.
- `config_schema.py` / `config_flow.py`: voce di menu `heating_pdc` (config e options) con i passi `heating_pdc`, `heating_pdc_rooms`, `heating_pdc_add_room`, `heating_pdc_remove_room`; validazione con `parse_pdc_config` come observe e controllo dell'esistenza delle entità.
- `vtherm_central_api.py`: `heating_pdc_manager`, `async_create_heating_pdc_manager`, `async_remove_heating_pdc_manager`.
- `__init__.py`: creazione prima del forward delle piattaforme, avvio dopo, `async_on_unload`, rimozione prima dello scarico delle piattaforme con ripristino se lo scarico fallisce, rimozione dello store in `async_remove_entry`.
- `sensor.py`: `HeatingOrchestrationPdcSensor`, uno per stanza, `unique_id = <entry>_heating_orchestration_pdc_<room_key>`, `_unrecorded_attributes` per i dati volatili; un'entry con la sola sezione PDC mostra anche il sensore caldaia in `disabled` (effetto collaterale accettato).
- Traduzioni it/en: nuove chiavi con parità (menu, 4 passi, 13 errori `pdc_*`, selettore, nome del sensore).

Storage key `versatile_thermostat.heating_orchestration_pdc.<entry>`, versione 1; in observe non viene scritto nulla.

## 6. File e SHA-256

Percorsi dalla radice del fork. `heating_pdc_policy.py` e `heating_pdc_provenance.py` sono invariati rispetto a M3.

| File | SHA-256 |
| --- | --- |
| custom_components/versatile_thermostat/heating_pdc_policy.py (invariato) | `67f866c2c09a3eb9896aa131f853191e8c16993f86c3a4eb24e95296f0bc975e` |
| custom_components/versatile_thermostat/heating_pdc_provenance.py (invariato) | `1b5bd8ae814eaab4792012716f417c44435e7cb37836b298a951f61965fe7203` |
| custom_components/versatile_thermostat/heating_pdc_transaction.py (rivisto) | `05213b9c11ef157367057fb5f6f84a3f35b6846c8f627180d134092d9a34e9fb` |
| custom_components/versatile_thermostat/heating_pdc_collector.py (nuovo) | `066aa4055edb4bdc76d1ac9f7583aedb9dffe54e9d78ff28123d778b690483b3` |
| custom_components/versatile_thermostat/heating_pdc_runtime.py (nuovo) | `c9aa9e382b502c0d25d200d30408fa4f5b92817a48dfcc4d8880699e9735bc17` |
| custom_components/versatile_thermostat/feature_heating_pdc_manager.py (nuovo) | `a23ee3cced032c95ecf05d30152f5d97db39714c81fe361ae76c1ea5d446cea0` |
| custom_components/versatile_thermostat/__init__.py (condiviso) | `aeba9b358cf4d53a384e29cc9e379f7aedd7a2f4ed964e393c5584f49e691fa2` |
| custom_components/versatile_thermostat/vtherm_central_api.py (condiviso) | `e0aad46c4261ccdc8f65268e6b5829040b7181dc3db8d712e13308b13b880ed6` |
| custom_components/versatile_thermostat/config_flow.py (condiviso) | `e50818a7c87b087a51566b0d49ece0d0e2bb39d91b6f8340f07bce85da8c57b1` |
| custom_components/versatile_thermostat/config_schema.py (condiviso) | `f1570e565960c5b0828b81193e15fdcb8fccbd25e1da763679d7996e70ea93a1` |
| custom_components/versatile_thermostat/const.py (condiviso) | `8b9dca4063f42549f8ec2c5cc048c55bd70be32321d9dc65f51521866d0a214e` |
| custom_components/versatile_thermostat/sensor.py (condiviso) | `bd84bb1223a0d9645d7e80ae5cbc9652120b9fbe27601f14be5ff0ebe09ce738` |
| custom_components/versatile_thermostat/translations/en.json | `f8cbcb209fce50c4bfc73244bcc097a7ac19d94cc00385f12ef7593586d1da0e` |
| custom_components/versatile_thermostat/translations/it.json | `69b4ec69f43e66f2981dc137460b85bdde863398448bbb2cda91b0133e934beb` |
| tests/test_heating_pdc_transaction.py | `352124f731fb0cb3a317db8bc3fd3b64e6d02069ba9edaa840516e06f08a55bd` |
| tests/test_heating_pdc_collector.py | `9f45bb31810883f03a499169c2b31a1cf3b966c4a3f8ace4eaa15263f6c93883` |
| tests/test_heating_pdc_runtime.py | `178e9993434832aecc770816872cb4f766dd5491f6cac216e6b075dff17ee9b3` |
| tests/test_heating_pdc_integration.py | `06215253133e97ab028523e14b3cabde8b4a607e88550b9c34ddfc3daf4661a4` |
| tests/test_heating_pdc_fake_ha.py | `064c6e45ce0e59c02494474c193cb350e987932fd0c7eadd73e8c3f86449e394` |
| tests/test_heating_pdc_manager.py (HA, non eseguito) | `b0da4b4d14a4ffa1fc41dd8a0bcbeea932b5b5205c6ed62193192f59e3376208` |
| tests/test_heating_pdc_ha_integration.py (HA, non eseguito) | `a438c394359b97709998299ee34258e8f38f58a8abbb7585b1d817a8373713fb` |
| plans/.../pdc/verify_collector.py | `a418f6708f37a89d6b0626b6b2663c5e95383389fb5d6f751cd85ea9289333c1` |
| plans/.../pdc/fake_ha_pdc.py | `67d1a31688b1c2085b8ec5a47f9ff4dfa5509fc40bf7477f6f7b76c26f440c48` |
| plans/.../pdc/mutation_check_runtime.py | `a8ddb66f11dcab912071a9c4cc6cbf3f9b709ca8b21bf8d4c9f675ad8ada79cd` |
| plans/.../pdc/M4-M5-RUNTIME-mutazioni.json | `3fe50cfff783df7fe3b2663fc6a10b5e6fcf1690e8272fe88538847ae8106237` |
| plans/.../pdc/M4-M5-RUNTIME-parita-collector.json | `5e51953eb16df90bf74d332616986ecf22f8ebff6b44bc10f18617b1b96ebeef` |

## 7. Verifiche

Interprete: `C:\Users\CarloTomaselli-SOFTCARE\AppData\Local\Temp\opencode\m1venv\Scripts\python.exe` (3.15.2). Dalla radice del fork, 25/11/2026.

- **Test puri PDC** (policy, transaction, provenance, collector), `python -m pytest -q --noconftest --import-mode=importlib -p no:cacheprovider ...`: **536 passed** (riferimento prima del lavoro: 438 sui tre file più il collector assente).
- **Suite pura intera** (caldaia collector/command/policy, PDC policy/transaction/provenance/collector/runtime/integration/fake_ha): **1063 passed, 1 skipped** (riferimento: 933 passed, 1 skipped prima del lavoro).
- **Parità M3** `pdc/verify_m3.py --random 6000 --seed 20261124`: uscita 0, nessuna divergenza (motore invariato).
- **Parità del collector** `pdc/verify_collector.py --random 6000` contro le parti Jinja reali: uscita 0; per stanza 116 casi di confine, 486 di griglia (arresto radiatore, rilettura, setpoint) e 6000 casuali; 6602 snapshot uguali campo per campo al riferimento del harness, 19806 confronti di `conditions_ok` per fase con i `if` della sequenza, `radiator_stopped` e `target_confirmed` contro i template.
- **Mutazioni** `pdc/mutation_check_runtime.py`: **56 su 56 uccise** (proprietà dell'inibizione, mai PDC senza arresto, mai radiatore senza PDC OFF, interlock, write-ahead, recupero, sospensione, lettura del collector, observe che invia/persiste/tocca l'inibizione, barriere di control, flow, wiring e `_unrecorded_attributes`). Un primo giro ne lasciò vivi 7 e poi 3: sono stati aggiunti i test corrispondenti.
- **Finto HA** `pdc/fake_ha_pdc.py` (come `m2b/fake_ha_scenarios.py`, nessun codice HA eseguito): 39 controlli, 0 falliti. In observe, per 20 minuti simulati con picco di potenza, finestra, comando utente, split avviato dal telecomando e sensori persi: **0 chiamate di servizio, 0 chiamate di inibizione, 0 scritture sullo store, 0 notifiche**, diagnostica pubblicata per entrambe le stanze; `control` rifiutato con stato `error` e nulla costruito; stop e reload senza listener, timer o task residui.
- `py_compile`/parsing AST con Python 3.15.2 e 3.14.6 dei file nuovi e dei sei condivisi; nomi dei file condivisi risolti; JSON delle traduzioni validi, formato invariato, parità it/en delle chiavi PDC (test).
- **Black 24.10.0** `--check --line-length 180` pulito su tutti i file PDC (moduli, test, script). I sei file condivisi non erano puliti in HEAD e non sono stati riformattati.
- Confronto con la baseline: `git diff --stat` limitato ai file elencati, più `translations` (solo le nuove chiavi, più due virgole).

## 8. Test Home Assistant non eseguiti

`tests/test_heating_pdc_manager.py` (9 test: lifecycle, store, servizi, adattatore VTherm, sospensione) e `tests/test_heating_pdc_ha_integration.py` (9 test: setup, unload e reload, sensori, entry senza chiavi, rifiuto di control, options flow, chiavi riportate dal passo caldaia). **Scritti, mai eseguiti** (divieto di Carlo): possono contenere errori e vanno lanciati una volta in ambiente isolato prima di installare. Non eseguiti nemmeno `tests/test_heating_orchestration.py` e la suite completa.

## 9. Limiti e richieste

- Nessuna prova su HA reale: il finto HA copre la logica asyncio del manager, non i servizi veri. Compatibilità con HA 2026.11.2 verificata solo staticamente (sorgenti 2026.10.4: `async_listen` con filtro `@callback`, `async_call_later`, `async_create_task(eager_start=False)`, `persistent_notification`).
- Black 24 pulito sui file PDC; i file condivisi non erano puliti già in HEAD e non sono stati riformattati.
- Python 3.14 (CI): solo parsing AST dei file; i test sono stati eseguiti con 3.15.2.
- Traduzioni: la parità it/en già presente in HEAD ha lacune non mie (it senza `humidity`, `entity.select/sensor` varie; en senza `lock.data_description`), da sistemare a parte.
- Incidente: eseguendo uno script con nome generico (`tr.py`) rimasto nella cartella temporanea condivisa, ho riscritto `translations/en.json` e `it.json` con una versione vecchia. Le ho ripristinate da HEAD (`git checkout HEAD -- <i due file>`, nessun altro lavoro era non committato in quei file) e poi applicate le sole chiavi PDC.
- `control` PDC: fuori ambito in questa versione; prima di abilitarlo servono review indipendente della sequenza, interblocco nel flow e prova su banco con una stanza.
