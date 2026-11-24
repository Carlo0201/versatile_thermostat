# M3 — Esito: motore decisionale PDC (con M4 e M5 puri)

Stato: **consegna del Worker B sul codice locale del fork, non revisionata dal coordinatore.** I moduli non sono importati dal runtime e l'impianto non è stato toccato. Nessun commit, push, stash, reset o clean; nessun file di A, C o congelato modificato; nessuna HA reale usata.

Provenienza: il motore e la prima parità sono di un worker precedente (interrotto); qui sono stati ripresi, verificati con mutazioni, corretti nei punti scoperti, completati con test, M4 e M5. Il motore `heating_pdc_policy.py` non ha subìto modifiche di logica in questa ripresa: solo la formattazione Black (CI, 24.10.0, `--line-length 180`).

## 1. Regole estratte e riferimenti alla baseline

Fonti (immutabili, hash verificati da `baseline/index.json`): `baseline/camera_carlo_*` e `baseline/camera_diego_luisa_*`. Il sensore `*_pdc_diagnostica` non è in `baseline/`: fonte secondaria `pdc/fonti/pdc_diagnostica_2026-11-23.json` (SHA-256 `20fbc983…f3`, origine dichiarata nel file, **da riconfrontare con HA live**).

| Regola | Valore | Dove nella baseline |
| --- | --- | --- |
| Trigger avvio | diagnostica `ready\|1100\|120` per 120 s (un solo fronte) | `scelta_radiatore…` `triggers[0]` (`id: start`); condizioni: override `off` e (`in_gestione` on oppure trigger `start`) |
| Altri trigger | potenza >2700, potenza unknown/unavailable, cambi di helper/finestra/centrale/split/PDC/timer/programmi/ciclo, attributi `requested_state`/`temperature`/`preset_mode` del VTherm, avvio HA, ogni minuto | `triggers[1..8]` |
| Ritardo iniziale | 2 s | `actions[0].delay` |
| Override | con override diverso da `off` il selettore non fa nulla, nemmeno uno stop | `actions[1]` |
| Ramo stop | `keep_running_ok` falso oppure stop termico | `actions[2].choose[0]`, motivo in `journal_reason` |
| Ramo setpoint | setpoint PDC diverso da `ceil(target+1)` limitato | `choose[1]` |
| Ramo avvio | condizioni comuni + guardie raw + orizzonte programma + pausa | `choose[2]`, `conditions` |
| Gap di avvio | `target - temperatura >= 0,4` con tolleranza 1e-6 | template condizioni e diagnostica |
| Capacità | `potenza + 1500 <= 2600` (cioè <= 1100 W); potenza non negativa e campione <= 45 s | idem; diagnostica `power_high` > 1100 |
| Dati | temperatura <= 3600 s; potenza <= 45 s; sensori unknown mai equiparati a zero | idem |
| Altra PDC | `in_gestione` altra `off` **e** split altra `off` | `other_managed`/`other_split`; nessuna priorità, nessuna regola "unica stanza" |
| Programma | `sensor.*_pdc_prossimo_stop_programma` > ora + 600 s (`none` = nessuno) | diagnostica `schedule_change`, `float(0)` nei template |
| Pausa | `cycle.m == off` e `0 <= off <= ora - 600` | diagnostica `cooldown` |
| Guardie raw | `last_changed` di split e `in_gestione` >= 600 s, fuori dalla diagnostica | `choose[2].conditions` |
| Stop ordinario / anticipato | target +0,2 dopo 600 s da HEAT / target +0,5 | `choose[0].conditions`, registro `cycle.on` |
| Target abbassato | `target < cycle.target` e già raggiunto | idem |
| Finestra in marcia | aperta per più di 120 s (strict, come `for` di HA); unknown ferma subito | `choose[0].conditions` |
| Protezioni override manuale | potenza >=2700/assente/stantia o centrale `Stopped`; finestra aperta >120 s; finestra ignota >=30 s | `protezioni_override_condizionatore` |
| Setpoint | `[[ceil(target+1),min]\|max,30]\|min` | `choose[1]`, `choose[2]` |
| Registro ciclo | `m`, `on`, `off`, `target` iniziale (troncamento a secondo intero) | `registro_ciclo_persistente_pdc` |
| Sequenza avvio | `in_gestione` on, attesa TRV 25 s, setpoint, HEAT, attesa HEAT 20 s, rilettura | `choose[2].sequence` |
| Sequenza ritorno | OFF al VTherm PDC, attesa split OFF 20 s, OFF diretto con attesa 15 s | `choose[0].sequence` e coda di `choose[2]` |

Il motore non contiene la sequenza dei comandi, ma espone i predicati che la sequenza rilegge (`recheck_after_radiator_stop`, `recheck_after_setpoint`, `keep_running_ok`, `radiator_stopped`, `classify_start_failure`).

## 2. Differenze tra Camera Carlo e Camera Diego Luisa

Confronto strutturale dei sette file di ciascuna stanza, normalizzando identificativi e nomi di entità. Oltre agli entity_id (`climate.*_condizionatore` contro `*_condizionatore_aria_stanza`, `finestra_interna` contro `finestre_interne`), agli id delle automazioni e all'id di notifica, **restano due differenze di comportamento**, ora parametri espliciti di `PdcPolicyConfig`:

| Parametro | Camera Carlo | Camera Diego Luisa | Dove |
| --- | --- | --- | --- |
| `setpoint_min_c` | 16 | 18 | formula del setpoint (`choose[1]`, `choose[2]`, rilettura) |
| `manual_preset_bypass` | vero | falso | condizione di preset: Carlo accetta anche la regolazione manuale Oikos (timer `active`, oppure `idle` con i due `switch.schedule_*` off) quando il preset non è tra `none/eco/comfort/boost` |

Timeout, soglie, limiti di potenza e orizzonti sono identici (verificato sui valori `timeout`, `delay`, `for` dei due file). Il profilo comfort dello split non è migrato: non influenza le decisioni PDC (resta esterno).

## 3. API del motore (`heating_pdc_policy.py`)

- `evaluate_pdc(snapshot, context, now, config) -> PdcDecision`: azione `none|start|stop|set_setpoint`, motivo, scadenza, codici diagnostici legacy, stringa `codici|1100|120`, setpoint, motivo di stop, condizioni strutturate (forma di `CONTRATTI.md` §2; solo informative, la decisione non le legge).
- Modelli congelati: `PdcSnapshot` (stati grezzi come stringhe, `None` = entità mancante; istanti `last_changed/last_reported`), `PdcCycle`, `PdcStartContext` (`ready_since`, `edge_consumed`, effimero), `PdcPolicyConfig`, `PdcCondition`, `PdcDecision`.
- Predicati riusabili da M4: `preset_ok`, `automatic_ok`, `start_core_ok`, `raw_guards_ok`, `schedule_ok`, `cooldown_ok`, `radiator_stopped`, `recheck_after_radiator_stop`, `recheck_after_setpoint`, `window_running_ok`, `keep_running_ok`, `minimum_on_done`, `thermal_stop`, `split_setpoint`, `stop_reason`, `classify_start_failure`, `override_protection_stop`, `diagnostic_codes`, `diagnostic_state`.
- `parse_pdc_cycle` e `observe_split_mode` riproducono il registro del ciclo.
- Irrobustimenti deliberati (non sono nella baseline): numeri non finiti rifiutati nello snapshot; booleani e non finiti nel ciclo trattati come mancanti; orizzonte programma non finito = non valido.

## 4. File e SHA-256

| File | SHA-256 |
| --- | --- |
| `custom_components/versatile_thermostat/heating_pdc_policy.py` | `67f866c2c09a3eb9896aa131f853191e8c16993f86c3a4eb24e95296f0bc975e` |
| `custom_components/versatile_thermostat/heating_pdc_transaction.py` (M4) | `62c515eb0f07835ecd09e764dc7d90c35f3b41c42072598650d8f10fb7abd744` |
| `custom_components/versatile_thermostat/heating_pdc_provenance.py` (M5) | `1b5bd8ae814eaab4792012716f417c44435e7cb37836b298a951f61965fe7203` |
| `tests/test_heating_pdc_policy.py` | `c5f6f549d3f3d7ed7c8c3e2160d7ee70032f5359088df38ce5454f2e8be466f5` |
| `tests/test_heating_pdc_transaction.py` | `ece4f2fc3bfe5a151c5aad0612e541bcf07f165a32298052fdbd761d0fd4bef3` |
| `tests/test_heating_pdc_provenance.py` | `4301a5f0f1fa6b57fa9d44d9aa28abfaa674464c0d50c4f66b3fdc279ec9c284` |
| `plans/…/pdc/verify_m3.py` | `09d1b2c2169ac6612d00a2900aee4102ded0490031433854907dbaab97a1dae8` |
| `plans/…/pdc/mutation_check.py` | `dfd67c47b64c1be40c03c97bf7e8966d950e999c80515c4296bbbd42bc59c98f` |
| `plans/…/pdc/M3-parita.json` | `7407aa4edbcf65dc8e6c089c30b617a83e2ad82c7f2fc7b85681e54c2f26174c` |

Anche `pdc/M3-mutazioni.json` (risultato per mutazione) e `pdc/M3-parita-stdout.txt`. Il `policy_sha256` registrato in entrambi i JSON è quello del motore sopra.

## 5. Comandi ed esiti (24/11/2026, Python 3.15.2 con Jinja2 e pytest; ambiente fuori da OneDrive)

Interprete: `C:\Users\CarloTomaselli-SOFTCARE\AppData\Local\Temp\opencode\m1venv\Scripts\python.exe`. Dalla radice del fork:

```
python plans/heating-orchestration-2026-11-24/pdc/verify_m3.py --random 6000 --seed 20261124
python plans/heating-orchestration-2026-11-24/pdc/mutation_check.py --random 0 --out <file>
python -m pytest -q --noconftest --import-mode=importlib -p no:cacheprovider tests/test_heating_pdc_policy.py tests/test_heating_pdc_transaction.py tests/test_heating_pdc_provenance.py
black --check --line-length 180 <file>   (black 24.10.0 della CI)
```

- **Parità con Jinja reale**: uscita 0, nessuna divergenza. Per ciascuna stanza 116 scenari di confine, più 6000 casi casuali con seed fisso (**20261124 Camera Carlo, 20261125 Camera Diego Luisa**). Confrontati a ogni caso: stringa diagnostica legacy, ramo scelto (`stop/set_setpoint/start/none`), motivo del giornale di stop, formula del setpoint, attesa "radiatore fermo", i tre controlli di rilettura della sequenza, classificazione dei fallimenti, protezioni dell'override, scrittura del registro del ciclo. Rami casuali (Carlo): none 4123, stop 1278, start 487, set_setpoint 112; (Diego Luisa): none 4181, stop 1238, start 474, set_setpoint 107. Scritture del registro: 1798 e 1780. Protezioni override che fermano: 14 e 18.
- **L'harness discrimina**: 67 mutazioni deliberate del motore (soglie, confronti inclusivi/stretti, tempi, clamp, ceil/floor, ordine dei motivi, ecc.), **67 su 67 individuate**. 64 dalla sola parità sui casi di confine (`--random 0`), 3 solo dai test (mutazioni equivalenti per l'harness: due predicati ridondanti col gate diagnostico, e il default 16 di `setpoint_min_c` che l'harness sovrascrive con i parametri delle stanze). Al primo giro 58/67; i 9 sopravvissuti hanno portato ad aggiungere scenari: setpoint su target frazionari e agli estremi, `gap` bit-esatto alla soglia (target 0,5), potenza alta con finestra aperta/ignota, registro con `on`/`off` mancanti e orario frazionario.
- **Test pytest**: 346 (motore, compreso un campione di parità Jinja con 150 casi per stanza), 43 (M4), 49 (M5): **438 passed**. Si eseguono col solo pytest (`--noconftest --import-mode=importlib`), modulo caricato per percorso; `jinja2` è opzionale (`importorskip`). Il test di parità del motore richiama l'harness con `-I`.
- **Mutazioni M4/M5**: 22 mutazioni ad hoc (rilascio senza OFF, token ignorato, timeout 25/15 s, ownership, prenotazione, restart, finestra 75 s e 120 min, provenienza promossa a `user`, ecc.), **22 su 22 individuate**. Lo script non è conservato; l'elenco è riproducibile dai nomi nei test.
- **Black 24.10.0**: 8 file invariati dopo la formattazione.
- Copertura debole nota: nei casi casuali il predicato "radiatore fermo" è quasi sempre falso (1 e 0 casi veri su 6000), quindi `recheck_after_radiator_stop` è coperto soprattutto dagli scenari di confine e dai test; non è una divergenza.

## 6. Anomalie della baseline (segnalate, non corrette)

1. **Fronte di avvio unico** (`start_edge_one_shot`): il trigger `ready` per 120 s scatta una volta. Se in quell'istante falliscono le guardie raw o l'override non è `off`, non c'è nuovo tentativo finché la diagnostica non lascia `ready`. Le guardie raw (600 s su `last_changed` di split e `in_gestione`) sono fuori dalla diagnostica, che dunque può dire `ready` mentre l'avvio è bloccato.
2. **`mode: single`, `max_exceeded: silent`** nel selettore: un evento (per esempio potenza >2700) che arriva durante una sequenza in corso è scartato senza traccia; resta solo il controllo al minuto. La sequenza di avvio dura fino a circa 45 s di attese (25 + 20), più i comandi.
3. **Override durante la sequenza**: le condizioni `override == off` interrompono lo script in silenzio lasciando `in_gestione` ON; con override diverso da `off` il selettore non esegue nemmeno uno stop di protezione (compete alle protezioni dell'override, che agiscono solo con override `on`, non con unknown).
4. **Campioni dal futuro**: il mantenimento accetta temperatura e potenza con età negativa e potenza negativa, l'avvio rifiuta la potenza negativa, lo stop termico richiede età >= 0. Preservato.
5. **Motivo di stop `window`** quando la finestra non è `off`, anche se lo stop è termico. Preservato.
6. **Setpoint con target mancante**: `waiting_target` con target `None` genera un template non renderizzabile e il valore dell'helper non cambia; il motore restituisce `None`.
7. **Ritorno non confermato** (split non OFF dopo 20 + 15 s): la baseline lascia `in_gestione` ON (corretto per non riaprire il radiatore) ma non ritenta; non c'è un evento di riprova.
8. Nella baseline il rilascio del radiatore avviene altrove (spegnimento di `in_gestione`); non esiste una chiamata esplicita di rilascio dell'inibizione: va definita nell'integrazione (§7).
9. `numeric_state` della soglia 2700 W fa scattare l'avvio solo all'attraversamento; l'osservazione periodica la copre.

## 7. Proposta di contratto runtime per l'integrazione

Da fissare nel `CONTRATTI.md` §4 (proprietà del coordinatore).

**Ingressi** (un solo lettore per stanza, snapshot coerente a istante unico; campi da `PdcSnapshot`): helper `*_pdc_override`, `*_pdc_in_gestione`, `*_pdc_automatica`, `*_pdc_ciclo` (restore); `select.central_mode`; VTherm radiatore (modo, `requested_state`, `preset_mode`, `temperature`, `specific_states.inhibition.inhibited`); valvola fisica; sensore temperatura con `last_reported`; sensore finestra con `last_changed`; potenza di casa con `last_reported`; split SmartThings (`state`, `last_changed`) e VTherm PDC (modo, setpoint); altra stanza (`in_gestione`, split); sensore prossimo stop programma; timer e switch programma (solo Carlo). Tutti configurabili per `room_key`, nessun entity_id nel codice.

**Eventi che rivalutano**: ogni cambio di stato degli ingressi, avvio HA, tick di 60 s (come baseline) e **scadenza `decision.deadline`** (nuovo: evita di aspettare il minuto). Le rivalutazioni sono serializzate per stanza.

**Contesto** (`PdcStartContext`): effimero, non persistito; dopo un riavvio riparte da `ready_since = None`. Il ciclo `m/on/off/target` è l'unico stato persistente (storage supportato, versionato, mai `.storage` diretto); seed iniziale di sola lettura dagli helper legacy.

**Decisione → comandi** (eseguiti solo in `control`; in `observe` solo diagnostica): `start` → `request_start` (M4); `stop` → `request_return`; `set_setpoint` → `climate.set_temperature` sul VTherm PDC con `decision.setpoint`. Con override non `off` il proprietario dei comandi è il modulo di provenienza (M5), non il selettore.

**Conferme**: una chiamata di servizio completata non prova nulla. Per avvio/ritorno valgono solo gli stati osservati: radiatore fermo (`radiator_stopped`), setpoint corretto, split `heat`, split `off`; `None`/unknown non sono mai conferme. Il rilascio dell'inibizione si esegue solo dopo split `off` confermato e solo se posseduta (`inhibition_owned`).

**Preemption**: durante una transazione gli eventi di protezione (potenza >= 2700, finestra, override, centrale `Stopped`, dati mancanti) non vanno persi come nella baseline (§6.2): il manager li accoda e, dopo ogni attesa, rilegge le condizioni e può chiamare `request_return`.

**Diagnostica per stanza** (`CONTRATTI.md` §4): `proposed_action` (`none/heat/off` della PDC), `reason`, `conditions` (da `PdcDecision.conditions`), `power_limit_w` (1100), `stability_s` (120), `transaction {id, phase, started_at, deadline}` (da `PdcTransaction`), `reservation`, `override {active, until, source, provenance}`, `last_error`. Codici di condizione: quelli legacy più `raw_guard` e `edge_consumed`.

**Configurazione per stanza**: `PdcPolicyConfig` (le due differenze del §2), `PdcTransactionConfig` (timeout), nessun nome di stanza nel codice generico.

## 8. M4 e M5 (moduli puri, test inclusi)

**M4 — `heating_pdc_transaction.py`**: `PdcTransaction` (fasi: `idle`, `waiting_radiator`, `waiting_target`, `waiting_heat`, `running`, `returning`, `returning_direct`, `return_unconfirmed`, `override_hold`), `PdcReservation` (un solo titolare, rilascio solo con stanza e token coincidenti), funzioni `request_start`, `observe`, `request_return`, `cancel`, `recover`. Garanzie coperte dai test: prenotazione serializzata (due richieste, una sola accettata), inibizione posseduta e rilasciata **solo** dopo split `off` confermato (verificato su una griglia esaustiva di osservazioni non-off in ogni fase), timeout della baseline (25/20/20/15 s; setpoint senza attesa, parametrizzabile), conferma già vera prima dell'attesa, token che scartano eventi vecchi o tardivi, eventi duplicati senza comandi doppi, override non confermato `off` che congela senza comandi né rilasci, cancellazione e riavvio sempre attraverso il ritorno con nuovo token, `return_unconfirmed` senza rilascio (la riprova è esplicita).
Decisioni di modello da rivedere: la rilettura di fase (`conditions_ok`) è calcolata dal chiamante con i predicati del motore e passata come booleano; il setpoint della baseline non ha attesa, quindi `target_timeout_s = 0` (un evento duplicato nella stessa istante avvierebbe il ritorno sicuro, mai un'apertura del radiatore).

**M5 — `heating_pdc_provenance.py`**: registro degli effetti attesi per campo (modo, temperatura, ventola, preset, swing) con scadenza a 75 s e valore esatto, effetti collaterali SmartThings (Quiet → ventola `auto`; OFF → preset `none`), classificazione dei cambi del dispositivo (atteso / manuale), comando utente HA autenticato, dispositivo ritrovato attivo, override di 120 minuti con rinnovo, scadenza (`fine <= ora`) e ripristino. Un cambio non atteso è manuale ma con **autore `unknown`**: non viene mai promosso a `user`, e un evento ignoto non cancella un utente noto.

## 9. Limiti, ripristino e cosa resta

- Nessun collegamento al runtime né a HA: tutto ciò che qui è "verificato" è offline (Python 3.15, Windows). Non è stata eseguita la CI GitHub né l'ambiente HA completo.
- La parità vale per le entità e gli snapshot catturati il 24/11; le automazioni live possono essere cambiate (`CLAUDE-STATUS.md`): riconfrontare gli hash prima di distribuire.
- Il sensore `*_pdc_diagnostica` proviene da una fonte secondaria del 23/11.
- M4 e M5 non hanno un riferimento Jinja eseguibile: sono verificati da test e mutazioni, non da parità. La loro integrazione richiede revisione indipendente.
- Resta: manager runtime PDC (osservazione, poi controllo), traduzione degli stati osservati in `PdcObservation`/`PdcSnapshot`, sensore diagnostico per stanza, restore del ciclo, integrazione dei file di A dopo M2-A, review D.
- Ripristino: i file sono nuovi e non tracciati; per tornare indietro basta cancellarli (nessun file preesistente è stato sovrascritto, tranne `pdc/verify_m3.py`, esteso con `PDC_POLICY_PATH`, `--no-report` e nuovi scenari, e `pdc/M3-parita.json`, rigenerato).
