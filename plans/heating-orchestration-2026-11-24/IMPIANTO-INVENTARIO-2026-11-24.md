# Inventario impianto (sola lettura) - 24/11/2026

Lettura live via connettore HA, 16:00-16:15 Europe/Rome (CET, UTC+1). HA Container 2026.11.2, fuso Europe/Rome. Nessuna scrittura, comando o ricarica eseguiti. I timestamp `last_triggered` di HA sono UTC; qui convertiti in ora locale (+1 h).

Nota: il worker precedente non aveva salvato nulla (`live-2026-11-24/` non esisteva).

## 1. Hash baseline contro live

Tutte e 15 le automazioni hanno `config_hash` live **identico** alla baseline di stamattina (09:00). Nessuna e' cambiata, quindi non sono stati salvati duplicati in `live-2026-11-24/` (la baseline resta valida). `validation` = null per tutte.

| Automazione | Hash baseline = live | Stato | Ultimo trigger (Rome) |
| --- | --- | --- | --- |
| caldaia_controllo_stabile_delle_richieste | 7799f0f9d831df35 | on | 16:03:45 |
| camera_carlo_scelta_radiatore_o_pompa_di_calore | da603f22ae05f2b2 | on | 06:12:48 |
| camera_diego_luisa_scelta_radiatore_o_pompa_di_calore | 51fc60d208ead00f | on | 14:48:11 |
| camera_carlo_fine_override_condizionatore | 9d410804009fc0ac | on | 16:03:00 (ogni minuto) |
| camera_carlo_override_manuale_condizionatore | 660d958cc60c7ed2 | on | 15:56:44 |
| camera_carlo_protezioni_override_condizionatore | 77067e449cca9520 | on | 21/11 14:16 |
| camera_carlo_registro_comandi_condizionatore | 0410c360d37e0117 | on | 06:12:50 |
| camera_carlo_profilo_comfort_split_automatico | 24db2881ddfde1a5 | on | 06:12:48 |
| camera_carlo_registro_ciclo_persistente_pdc | b7a57f5aa8d84d4c | on | 06:12:51 |
| camera_diego_luisa_fine_override_condizionatore | 608b26d0878b8a84 | on | 16:03:00 |
| camera_diego_luisa_override_manuale_condizionatore | a551457f157c81a2 | on | 15:56:44 |
| camera_diego_luisa_protezioni_override_condizionatore | fc301c998d0aea52 | on | mai |
| camera_diego_luisa_registro_comandi_condizionatore | 86fc88d87bf81280 | on | 14:48:13 |
| camera_diego_luisa_profilo_comfort_split_automatico | a1746cc40c3d73f0 | on | 14:48:11 |
| camera_diego_luisa_registro_ciclo_persistente_pdc | 35d9ca818429c517 | on | 14:48:15 |

Il controller attuale ha `last_triggered` 16:03 ma ha trigger `time_pattern /15 s`: `last_triggered` si aggiorna solo se le condizioni passano (le tre automazioni legacy devono essere off: lo sono).

## 2. Automazioni di riscaldamento fuori baseline

| Entita' | Stato | Ultimo trigger | Configurazione | Hash |
| --- | --- | --- | --- | --- |
| caldaia_filtro_richiesta_vtherm (id 1793810460536) | **off** | 09:19:49 | YAML/package: la config API non la legge (CONFIG_NOT_FOUND) | n/d |
| caldaia_sincronizza_timer (id caldaia_sincronizza_timer) | **off** | 09:19:49 | YAML/package: non leggibile | n/d |
| caldaia_avvio_immediato_richiesta_manuale (id 1795167361926) | **off** | mai | letta, salvata | bf03420a863734c5 |
| caldaia_tracciamento_ciclo_plus (oikos_plus_ciclo_caldaia) | **unavailable** | mai | YAML/package: non leggibile | n/d |
| riscaldamento_registro_azioni_per_stanza (id 1795424772523) | on | 16:03:42 | letta, **non salvata** (solo log) | 113f5eb161deb3a5 |
| oikos_riprendi_programma_riscaldamento_* (8, id oikos_override_riscaldamento_*) | on | carlo 12:20:54, cucina 04/11, soggiorno 10/11, altre mai | YAML/package: non leggibili | n/d |

Dedotto da `spook__find_usages` (non dalla config): `oikos_riprendi_programma_riscaldamento_camera_carlo` referenzia `switch.schedule_camera_carlo_vtherm_feriale`. Non referenzia `climate.trv_vtherm_camera_carlo` ne' altro attuatore rilevato dall'indice. Per le altre 7 non ho verificato.

Altre automazioni "plus" (asciugatrice, forno, frigorifero, lavatrice, luci, raccolta) sono unavailable e non riguardano il riscaldamento.

Scritture lette nelle 15 baseline (estratte dai file locali):
- controllo_stabile: `climate.termostato_ingresso` set_hvac_mode; `input_text.caldaia_controllo_ciclo` e `_diagnostica`; `timer.caldaia_attesa_avvio` (start/cancel), `timer.caldaia_attesa_spegnimento` (cancel); logbook.
- scelta Carlo/Diego: `climate.<stanza>_pompa_di_calore` (hvac_mode e temperatura), `climate.<split>` (hvac_mode off), `input_boolean.*_pdc_in_gestione`, `input_text.*_pdc_transizione`.
- protezioni override: stessi attuatori PDC/split OFF e `in_gestione` OFF.
- override manuale / fine override: chiamano gli script `*_pdc_override_avvia|sincronizza|riprendi_automatico`.
- profilo comfort: solo `preset/fan/swing` dello split e switch display/beep (non modo ne' temperatura).
- registri ciclo/comandi: solo `input_text.*_pdc_ciclo` e `*_pdc_comandi_attesi`.

Script (6): `camera_{carlo,diego_luisa}_pdc_override_{avvia,sincronizza,riprendi_automatico}`, tutti off. Letti e salvati solo i tre di Carlo; quelli di Diego Luisa sono omologhi per nome e dipendenze (find_usages coerente) ma **non letti**.
- `override_avvia`: `input_boolean.*_pdc_override` ON, `input_datetime.*_override_fine` +2 h, `timer.override_condizionatore_*` 2 h.
- `override_sincronizza`: scrive `climate.*_pompa_di_calore` (hvac_mode, temperatura) copiando dallo split, e `input_boolean.*_pdc_in_gestione` ON/OFF.
- `riprendi_automatico`: PDC e split OFF (attende conferma 20 s), poi `in_gestione` OFF, annulla timer, override OFF.

## 3. Centrale VTherm (entry X94W)

Letture 16:06:
- `sensor.nb_device_active_for_boiler` = **1**, `active_device_ids` = [`climate.trv_camera_diego_luisa`], cambiato 15:56:49 (nota: elenca l'entita' sottostante del TRV, non il VTherm).
- `binary_sensor.central_boiler` = **off** dal 12:51:00; `central_boiler_manager`: soglia dispositivi 2, ritardo attivazione 120 s, keep-alive 0, soglia potenza 0.
- `select.central_mode` = **Heat only** (dal 18/11 15:15). Opzioni: Auto, Stopped, Heat only, Cool only, Frost protection.

**Il gestore caldaia nativo non comanda alcun attuatore fisico.** `service_activate/deactivate` del manager puntano a `input_boolean.richiesta_caldaia_vtherm` turn_on/turn_off. Tale booleano e' letto solo da `caldaia_controllo_stabile` (come "consenso") e dal filtro legacy off. Nessun VTherm o integrazione trasforma quel booleano in HEAT/OFF su `climate.termostato_ingresso`: lo fa soltanto il controller esterno. Quindi oggi il passaggio di proprieta' richiede di dare al fork un `service_activate/deactivate` verso `climate.termostato_ingresso` (o un'azione equivalente) e di spegnere il controller.

Stato del termostato: `climate.termostato_ingresso` = off dalle 13:07:13, hvac_modes off/heat, preset `hold`, setpoint 26 (nota: `preset_mode` hold/program indica un programma interno del termostato non verificabile da qui).
Controller: `r`=`single_wait`, azione none, scadenza 16:11:49, attesa su Camera Diego Luisa (target 14,5; temperatura 14,2) con una sola valvola. `timer.caldaia_attesa_avvio` active, `attesa_spegnimento` idle.

## 4. Versione del fork installata

- Read-only non consente di leggere il `manifest.json` installato su HA. Non determinata con certezza.
- Evidenza live che il fork e' attivo: i VTherm espongono `inhibition` (schema_version 1, `command_gate`), `avatto_room_demand`, `underlying_temperature_tracking`; `get_config_entries` mostra 11 entry loaded (centrale X94W, 8 TRV, 2 PDC).
- Evidenza locale di installazione: `tmp/avatto-install-6e44449/` contiene `before/` (upstream 10.6.4) e `fork/` (10.6.3) con commit **6e44449** "Version Oikos boiler bypass, Avatto filter and native timers". Probabile versione installata: 6e44449, ma non provata.
- Fork locale: branch `codex/avatto-trv-pilot`, HEAD **6a819dc** (manifest 10.6.3), 2 commit avanti a 6e44449 (cc96642 Laura Livia nel filtro; 6a819dc priorita' PDC Carlo) e **14 file modificati non committati** (`__init__.py`, `config_flow.py`, `config_schema.py`, `const.py`, `sensor.py`, ...). Il manifest locale e' uguale (10.6.3), quindi la versione non distingue le build. Prima del deploy serve un confronto per hash dei file di `custom_components/versatile_thermostat` (non possibile in sola lettura).
- Logica HA: `ha_config_get_automation` sulle YAML non e' possibile, quindi `caldaia_filtro` ecc. restano da leggere dai file YAML su disco/Portainer.

## 5. Mappa dei proprietari per attuatore

| Attuatore | Scrittori attivi oggi | Scrittori dormienti (off/YAML) | Conflitti / note |
| --- | --- | --- | --- |
| `climate.termostato_ingresso` (HEAT/OFF) | `caldaia_controllo_stabile` (unico automatico). Oikos card (comando manuale utente), UI HA | `caldaia_filtro_richiesta_vtherm`, `caldaia_avvio_immediato_richiesta_manuale` (set_hvac_mode); `caldaia_sincronizza_timer` (timer) | Controller con 3 condizioni "legacy off" contro doppi scrittori. Rischio doppio scrittore se qualcuno riattiva una legacy: il controller ci si blocca da solo. Fork nativo: nessuno. Possibili scrittori esterni (termostato fisico, schedule, Node-RED) non verificabili |
| VTherm TRV (hvac_mode, preset, target) | `switch.schedule_*_vtherm_*` (16 switch, tutti on, presumibilmente Scheduler/Schedule integration), Oikos card, UI. `oikos_riprendi_*` (YAML, solo carlo parzialmente noto) | - | Nessuna delle 15 baseline scrive i TRV. L'inibizione VTherm (`inhibition.command_gate`) e' gestita nel fork: non ho trovato chi la imposta nelle automazioni; nessuno scrive `versatile_thermostat.*` |
| `climate.*_pompa_di_calore` (VTherm PDC) | scelta radiatore/PDC (Carlo e Diego), protezioni override, script `override_sincronizza`/`riprendi_automatico` | - | **Doppio scrittore sullo stesso attuatore**: scelta, protezioni e script scrivono tutti hvac_mode PDC. Coordinati da `in_gestione` e `override`, ma non da un proprietario unico |
| Split `climate.camera_*_condizionatore*` | scelta (OFF), protezioni (OFF), `riprendi_automatico` (OFF), profilo comfort (preset/fan/swing), Oikos card/SmartThings utente | - | Profilo comfort e scelta scrivono lo stesso device (campi diversi). L'override manuale legge `call_service` di tutti i climate e `comandi_attesi` per distinguere |
| `input_boolean.*_pdc_in_gestione` | scelta, protezioni, `override_sincronizza`, `riprendi_automatico` | - | 4 scrittori per stanza; letto da controllo_stabile, profilo, fine/override, template `*_pdc_diagnostica`, `avatto_*_pre_fork` |
| `input_boolean.*_pdc_override` | `override_avvia`, `riprendi_automatico` | - | Letto da profilo, protezioni, fine override |
| `input_boolean.richiesta_caldaia_vtherm` | **gestore nativo centrale (unico)** | - | Letto da controllo_stabile e filtro legacy |
| `input_boolean.caldaia_filtro_pilota_avatto` | nessuna automazione (manuale/UI) | - | Letto da controllo_stabile, filtro, sincronizza_timer |
| `input_text.caldaia_controllo_ciclo` | controllo_stabile | - | Letto solo da controllo_stabile. Valore live `{"m":"off","off":1795522033,"r":null,"s":["camera_diego_luisa",14.5,...]}` (OFF dalle 13:07:13) |
| `input_text.caldaia_controllo_diagnostica` | controllo_stabile | - | Letto solo da controllo_stabile e (fuori da HA) card Oikos `boilerControl.js` (JSON v1 entro 45 s) |
| `timer.caldaia_attesa_avvio/_spegnimento` | controllo_stabile | `caldaia_sincronizza_timer` (off) | Solo osservazione per la UI |
| `input_text.*_pdc_ciclo` | registri ciclo (Carlo, Diego) | - | Letto da scelta |
| `input_text.*_pdc_comandi_attesi` | registri comandi | - | Letto da override manuale e script |
| `input_text.*_pdc_transizione` | scelta | - | Diego Luisa vale ora `idle|return_unconfirmed|1795202325|restored` = 20/11/2026 20:18:45: stato residuo anomalo da verificare |
| `switch.schedule_*_vtherm_*` | utente / Schedule | - | Letti da `oikos_riprendi_*`, scelta, template manuale Oikos |

Template che leggono o dipendono dagli attuatori: `sensor.tempo_accensione_termostato_continuo` (legge `climate.termostato_ingresso`), `binary_sensor.caldaia_consenso_termico_avvio`, `binary_sensor.caldaia_richiesta_manuale_oikos` (e `_pre_fork`), `binary_sensor/sensor.avatto_domanda|comando_*_pre_fork`, `sensor.*_pdc_diagnostica`, `sensor.*_pdc_prossimo_stop_programma`. Dashboard `lovelace` e `dashboard-dashboard` referenziano termostato e TRV. Nessuno risulta scrivere gli attuatori.

Note: i `climate.trv_*` fisici sottostanti (Zigbee2MQTT/MQTT) sono comandati dai VTherm. `climate.camera_*_pompa_di_calore` e' un VTherm over_climate che pilota lo split SmartThings.

## 6. Helper legacy

- `input_text.caldaia_controllo_diagnostica`: scrive `caldaia_controllo_stabile`; legge la card Oikos (fuori HA, non verificato).
- `input_text.caldaia_controllo_ciclo`: scrive e legge solo `caldaia_controllo_stabile`.
- `timer.caldaia_attesa_*`: scrive `controllo_stabile` (e legacy `sincronizza_timer`, off); lettori: UI. Il timer `attesa_avvio` e' attivo (duration 0:15).
- `input_boolean.*pdc*` e `sensor.*_pdc_diagnostica`: vedi mappa; `sensor.*_pdc_diagnostica` e' un template (leggono in_gestione, split, PDC, schedule, central_mode, TRV) con valori `at_target,small_gap|1100|120`.
- `input_boolean.richiesta_caldaia_vtherm`: scrive il nativo; legge controllo_stabile (consenso) + filtro legacy.

## 7. Cosa disattivare/adattare al passaggio di proprieta'

1. **Controllo caldaia**: disattivare `caldaia_controllo_stabile_delle_richieste` solo dopo che il fork abbia un `service_activate/deactivate` su `climate.termostato_ingresso` (o equivalente) e quando non sono in corso transizioni. Prima annullare timer pendenti (`caldaia_attesa_avvio` e' attivo).
2. Le tre legacy (`filtro`, `sincronizza_timer`, `avvio_immediato`) devono restare off: non riattivarle durante il test. Le loro YAML sono da leggere sul disco.
3. `input_text.caldaia_controllo_ciclo/diagnostica` e `timer.caldaia_attesa_*`: la card Oikos dipende dalla diagnostica v1 (45 s). Quando il controller si ferma smettono di aggiornarsi: la card deve selezionare esplicitamente il nuovo contratto. Non rimuovere gli helper prima.
4. PDC: scelta, protezioni override e script condividono scrittura su VTherm PDC/split e `in_gestione`. In un eventuale M3 il proprietario unico deve assorbire o escludere esplicitamente queste automazioni; il profilo comfort split (preset/fan/swing) e i registri (ciclo/comandi) possono restare esterni.
5. `caldaia_tracciamento_ciclo_plus`: unavailable, non riguarda il controllo.
6. `oikos_riprendi_programma_riscaldamento_*`: restano esterne (riprendono la programmazione), da non toccare.
7. Il filtro `input_boolean.caldaia_filtro_pilota_avatto` oggi off: decidere se resta feature del fork.

Resta esterno: card Oikos, switch Schedule, SmartThings (split), Zigbee2MQTT/MQTT, template `*_pre_fork`, dashboard.

## 8. File salvati (SHA-256)

Cartella `repos/versatile_thermostat/plans/heating-orchestration-2026-11-24/live-2026-11-24/`:

| File | SHA-256 | Nota |
| --- | --- | --- |
| caldaia_avvio_immediato_richiesta_manuale.json | a0ca7c514c50c5ed20308e8a5dcc524bf095d271a9786bcf46d6e3e9ed6fc0eb | description abbreviata |
| script_camera_carlo_pdc_override_avvia.json | 861fe8c0ef704ab1f659ed4612379c2aa4e4f6536a2a04f610eaa12d2d44fa7a | integrale |
| script_camera_carlo_pdc_override_sincronizza.json | 9d4c588de7c94f4ea4be64292f3d687e6b9aaf32bf3712bf35bac74850453826 | solo riassunto strutturale |
| script_camera_carlo_pdc_riprendi_automatico.json | ffa9741711c56651f469be0793fbcc2680f396051ad75cbb4747cf89425dbd05 | messaggio notifica abbreviato |

I file sono trascrizioni (non byte-identici all'output API); gli hash HA sono nelle note.

## 9. Limiti

- Config non leggibili (YAML/package, CONFIG_NOT_FOUND): filtro, sincronizza_timer, tracciamento_ciclo_plus, 8 `oikos_riprendi_*`. Il relativo contenuto e' dedotto solo da `find_usages`.
- Non letti: script di Diego Luisa; `riscaldamento_registro_azioni_per_stanza` non salvata (log-only, scrive solo logbook).
- Node-RED: nessuna config entry `nodered`; Container senza supervisor, `ha_get_app` non provato. Esistenza o assenza di flussi Node-RED che comandano `climate.termostato_ingresso` o i TRV: **non verificabile**. Idem Z2M/MQTT diretto e chiamate API di terzi (Oikos card, REST).
- Lo storico `history__get_history` non e' stato usato; nessuna analisi dei trigger oltre a `last_triggered`.
- Versione installata del fork non provata (vedi sezione 4).
- `find_usages` non vede scritture via `call_service` dall'esterno ne' da JS delle card.
