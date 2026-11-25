# Prima versione installabile in observe — 10.6.3+oikos.1

Data: 25/11/2026, Europe/Rome. Coordinatore: Claude.

**Stato: sviluppo locale completato per la sola osservazione. Non validato in Home Assistant, non installato, non attivato sull'impianto.** Nessun test che richieda il runtime di HA è stato eseguito: il primo avvio reale sarà anche la prima prova vera.

## 1. Versione e pacchetto

| Voce | Valore |
| --- | --- |
| Repository | `Carlo0201/versatile_thermostat`, branch `codex/avatto-trv-pilot` (pubblico, pushato) |
| Commit | `99b8994` — «Close boiler control for the observe-only release and set version 10.6.3+oikos.1» |
| Versione manifest | `10.6.3+oikos.1`; l'impianto ha `10.6.3`, probabilmente la build `6e44449` |
| Working tree | Pulito, salvo i due script storici non tracciati `plans/avatto-oikos-pilot.ps1` e `plans/boiler-timers.ps1`, che non fanno parte del pacchetto |
| Pacchetto | `output/versatile_thermostat_observe_2026-11-25/versatile_thermostat-10.6.3+oikos.1-99b8994.zip` nel progetto padre |
| SHA-256 pacchetto | `57c6febb008fe7407dde832c7bb158d0f95f1bb79841cddba77fd70e7cbe78aa` (723.276 byte) |
| Contenuto | `custom_components/versatile_thermostat/`, 89 file, generati con `git archive 99b8994`. Nessun `__pycache__` e nessun file non tracciato. Gli hash dei singoli file sono in `SHA256SUMS-contenuto.txt` accanto allo zip |
| Dipendenze | Invariate rispetto a 10.6.3: `numpy`, `scipy`, `vtherm_api>=0.7.0`. La review finale ha verificato che le funzioni usate esistono già in `vtherm_api` 0.7.0 |

Controlli eseguiti sul pacchetto:
- estratto in una cartella separata;
- 89 file identici al commit (l'unica differenza col working tree Windows è il fine riga CRLF di `heating_pdc_collector.py`; il pacchetto ha LF, come il commit);
- `compileall` riuscito su tutti i `.py`;
- `heating_boiler_policy.py` invariato, con hash `edf7e900…945b`.

Ricostruzione riproducibile: `git archive --format=zip -o <file>.zip 99b8994 custom_components/versatile_thermostat`.

## 2. Cosa offre questa versione

| Funzione | Disabled (predefinito, nessuna configurazione) | Observe |
| --- | --- | --- |
| Termostati, programmi, preset, Avatto, finestre, inibizione, gestore caldaia nativo | Invariati rispetto a `6a819dc` (review finale §1) | Invariati |
| Nuove entità | Nessuna | Sensore `…_heating_orchestration_boiler`; un sensore `…_heating_orchestration_pdc_<stanza>` per ogni stanza PDC configurata |
| Store | Nessuno | Caldaia: solo il proprio ciclo di osservazione (`versatile_thermostat.heating_orchestration…`). PDC: nessuna scrittura |
| Comandi a dispositivi, helper, timer, automazioni, inibizioni | Nessuno | **Nessuno**: verificato con analisi statica e con gli scenari ad HA finto |
| Menu della centrale | Due voci in più: «Orchestrazione caldaia» e «Orchestrazione PDC» | — |

### Controllo: stato reale

- **Caldaia, `control`:** il codice è completo e ha avuto due review, ma è **reso inaccessibile** in questa release (`BOILER_CONTROL_AVAILABLE = False`). Il flusso opzioni non lo offre. Una sezione `control` salvata viene rifiutata con errore esplicito (`config_control_not_available`) e la caldaia resta senza orchestrazione.
- **PDC, `control`:** il codice delle transazioni esiste ma è **incompleto e irraggiungibile**, protetto da 5 barriere con test. Le modifiche prudenti a M4 vanno approvate prima di abilitarlo (`M4-M5-RUNTIME-ESITO.md`).
- **Sospensione generale «Spegni tutto il riscaldamento»:** **non implementata**. Esiste solo il contratto (`CONTRATTI.md` §7).
- **Storico ed eventi di attività per le popup:** **non implementati**. Esiste solo il contratto (`CONTRATTI.md` §8).
- Le automazioni attuali restano le uniche proprietarie dell'impianto.

## 3. Verifiche eseguite e mancanti

Eseguite il 24–25/11 su Windows, con Python 3.15.2 e il solo pytest, senza HA:
- **Suite pura caldaia e PDC: 1063 passed, 1 skipped.** Lo skip è il test dell'import del package completo, che richiede HA.
- Parità con i template Jinja reali:
  - motore caldaia: 27 casi e 5.000 casuali;
  - collector caldaia: 17 fixture e 20.000 casi;
  - PDC M3: 116 casi di confine e 6.000 casuali per stanza;
  - collector PDC: 116 + 486 + 6.000 casi per stanza.
- Mutazioni (errori introdotti di proposito, tutti scoperti dai test):
  - modulo comando caldaia: 67/67;
  - M3: 67/67;
  - runtime PDC: 56/56.
- Scenari con HA finto (non è Home Assistant):
  - caldaia: 23/23, compresa la barriera di rilascio;
  - PDC: 39 controlli, 0 falliti, con zero chiamate di servizio in observe.
- Black 24 sui file nuovi, `py_compile` e JSON delle traduzioni con parità it/en delle chiavi nuove.
- Review indipendenti: M2-A, M2-B e review finale observe (`REVIEW-FINALE-OBSERVE.md`). Esito finale: installabile in observe, nessun bloccante.
- Compatibilità con HA 2026.11.2: solo **analisi statica**, sui sorgenti della wheel scaricata e non eseguita.

**Non eseguiti:**
- `tests/test_heating_orchestration.py`, `test_heating_pdc_manager.py`, `test_heating_pdc_ha_integration.py`;
- regressioni HA (`test_central_boiler.py`, `test_avatto_room_control.py`, config flow);
- intera suite del fork;
- CI GitHub;
- hassfest;
- avvio su HA 2026.11.2.

## 4. Prima dell'installazione: backup

1. **Versione installata.** Copia integrale della cartella `/config/custom_components/versatile_thermostat` del container HA. È l'unico rollback affidabile del codice: la versione installata non è determinabile in sola lettura, quindi non va dato per scontato che sia `6e44449`.
   - L'installazione precedente era stata fatta tramite Portainer (`tmp/avatto-install-6e44449`, con la copia `before/`). Gli script lì presenti non vanno rieseguiti senza averli rivisti.
2. **Configurazione.** Backup di HA (Impostazioni → Sistema → Backup) che comprenda `.storage/core.config_entries`, `core.entity_registry` e `core.device_registry`. Non modificare quei file a mano.
3. Annotare l'ora e lo stato di `automation.caldaia_controllo_stabile_delle_richieste` e delle due `*_scelta_radiatore_o_pompa_di_calore`, che devono restare ON.

## 5. Aggiornamento senza eliminare le integrazioni

1. Verificare l'hash dello zip con `SHA256SUMS-pacchetto.txt`.
2. Sostituire il **contenuto** della cartella `/config/custom_components/versatile_thermostat` con quello dello zip:
   - eliminare prima i vecchi `.py`, per non lasciare moduli orfani;
   - non toccare le configurazioni;
   - non rimuovere l'integrazione dalla UI: le 11 entry, i termostati, i programmi e le dashboard restano.
3. Riavviare Home Assistant (richiede il permesso di Carlo).
4. Controllare in Impostazioni → Dispositivi e servizi che le 11 entry Versatile Thermostat siano `loaded` e che la versione mostrata sia `10.6.3+oikos.1`.

Dopo il riavvio la nuova funzione è **disabled**: non deve cambiare nulla.

## 6. Attivare solo observe

Procedere solo se dopo il riavvio i controlli del §7 punti 1–3 sono a posto. **Iniziare dalla sola caldaia**; la PDC in observe è un secondo passo facoltativo.

**Caldaia.** Configurazione centrale → Configura → «Orchestrazione caldaia» → modalità **observe**. Collegamenti, dalla baseline:
- master `climate.termostato_ingresso`;
- modo centrale `select.central_mode`;
- conteggio `sensor.nb_device_active_for_boiler`;
- manuale `binary_sensor.caldaia_richiesta_manuale_oikos`;
- filtro pilota `input_boolean.caldaia_filtro_pilota_avatto`;
- pilota pronto `binary_sensor.caldaia_avvio_automatico_avatto`;
- consensi `input_boolean.richiesta_caldaia_vtherm` e `binary_sensor.central_boiler`;
- ciclo legacy (seed, sola lettura) `input_text.caldaia_controllo_ciclo`;
- poi le 8 stanze `climate.trv_vtherm_*`:
  - filtro pilota per `camera_carlo` e `bagno_blu`;
  - per `camera_carlo` e `camera_diego_luisa` lo split e la sessione `input_boolean.<stanza>_pdc_in_gestione`.

L'esempio completo è in `M2-A-ESITO.md` e `m2/collector_cases.py` (`BASELINE_SECTION`). La modalità `control` non compare.

**PDC (facoltativo, dopo qualche giorno di caldaia in observe).** «Orchestrazione PDC» → `pdc_mode` **observe**. Si aggiungono le due stanze con i campi elencati in `M4-M5-RUNTIME-ESITO.md`:
- potenza;
- modo centrale;
- radiatore e valvola;
- temperatura;
- finestra;
- split e VTherm PDC;
- automatico;
- stop programma;
- in gestione;
- override.

Parametri: `setpoint_min_c` 16 per Camera Carlo e 18 per Camera Diego Luisa; `manual_preset_bypass` vero solo per Camera Carlo. Le entità vanno lette dalle automazioni chooser attuali.

Il salvataggio delle opzioni ricarica le VTherm, come già prima. Farlo in un momento senza richieste di calore.

## 7. Segnali da controllare dopo installazione e attivazione

1. Nessun errore `versatile_thermostat` nel registro di sistema al riavvio. Le 11 entry sono `loaded`. I termostati rispondono come prima.
2. Le automazioni attuali sono ancora ON e continuano a comandare: il controllo caldaia esterno riscrive la sua diagnostica legacy, che Oikos continua a leggere.
3. Con la sola disabled non compaiono entità nuove oltre alle voci di menu.
4. In observe il sensore caldaia passa da `starting` a un motivo M1 (`no_demand`, `normal_wait`, …). Gli attributi da controllare sono:
   - `mode = observe` e `owner = observer`;
   - `control_status = inactive`;
   - `commanded_mode` e `last_command_at` sempre nulli;
   - `evaluated_at` aggiornato almeno ogni 25–30 s;
   - `cycle_origin` = `legacy_seed` oppure `new_observation`.
5. Confronto con il controller attuale: in condizioni stabili `reason` e `proposed_action` del sensore devono coincidere con `r` e `a` di `input_text.caldaia_controllo_diagnostica`. Differenze nei primi minuti (pausa iniziale, seed) o legate all'istante di campionamento non sono differenze di politica. Una differenza persistente va registrata con orario e stati.
6. **Nessun effetto**: il logbook di `climate.termostato_ingresso`, degli split e dei VTherm non mostra azioni attribuite all'integrazione; nessuna inibizione nuova.

## 8. Rollback

**Disattivazione rapida, senza reinstallare.** Riportare «Orchestrazione caldaia» e «Orchestrazione PDC» a **disabled**. Il manager si ferma e i sensori non vengono più creati.

**Ritorno alla versione precedente.**
1. Rimettere la cartella salvata al §4.1 e riavviare.
2. Il codice vecchio ignora le chiavi nuove (`heating_orchestration`, `pdc_*`) e le conserva quando salva le opzioni (review finale, punto 4). Restano due store e i sensori nuovi orfani: sono innocui, ma le card che li leggessero li vedrebbero `unavailable`.
3. Pulizia facoltativa, dopo il rollback:
   - rimuovere dal registro entità i sensori `…_heating_orchestration_*` rimasti senza integrazione;
   - gli store orfani si possono lasciare.

   Non serve rimuovere l'integrazione.
4. Se in futuro una entry avesse `control` confermato, reinstallare il codice nuovo lo riattiverebbe. In questa release non è possibile, perché `control` è chiuso.

## 9. Problemi aperti noti di questa release

Dai report di review:
- R-4: in observe il PDC non azzera `last_error` di stanza dopo un errore risolto, e alcune eccezioni non pubblicano `error`.
- R-6: se lo scarico della centrale fallisce, i sensori restano agganciati al vecchio manager.
- R-8: riportando la caldaia a disabled dal flusso, i collegamenti vengono cancellati e vanno reinseriti.
- R-9: rimuovendo una stanza PDC il suo sensore resta orfano.
- R-10: ogni salvataggio delle opzioni ricarica tutte le VTherm (comportamento preesistente).
- La memoria dei tentativi caldaia non è persistente. Riguarda solo il control, oggi chiuso.
- Un'entry con la sola sezione PDC mostra anche il sensore caldaia in `disabled`.
- Lacune di traduzione già presenti in HEAD (it senza `humidity`, en senza `lock.data_description`): non legate a questo lavoro.

## 10. Lavoro residuo, in ordine di priorità

1. **Validazione in HA in un ambiente isolato, non l'impianto**, quando Carlo lo autorizza:
   - eseguire i tre file di test HA scritti e le regressioni caldaia, Avatto e config flow, su HA 2026.11.2;
   - hassfest.
   L'ambiente Windows è già preparato (`AppData\Local\vtherm-ha-test`, con HA 2026.10.4).
2. Installazione in observe (§4–7) e confronto su cicli naturali per alcuni giorni.
3. Correzione dei minori R-4, R-6, R-8 e R-9.
4. Apertura del control caldaia:
   - review dei test HA eseguiti;
   - runbook di passaggio di proprietà (spegnere il controller esterno solo dopo la verifica in observe);
   - memoria persistente dei tentativi;
   - `BOILER_CONTROL_AVAILABLE = True` in una release distinta.
5. Control PDC:
   - approvazione delle modifiche M4;
   - review indipendente della sequenza;
   - interblocco nel flusso;
   - prova su una sola stanza.
6. Sospensione generale (`CONTRATTI.md` §7), dopo il control di caldaia e PDC.
7. Eventi di attività e storico (`CONTRATTI.md` §8), poi adattamento delle card Oikos, che oggi non leggono i nuovi sensori. Il lavoro Oikos della fonte nativa è nell'SDK, non installato (`OIKOS-ESITO.md`).
8. Verificare l'anomalia `input_text.camera_diego_luisa_pdc_transizione = return_unconfirmed` del 20/11, prima del control PDC.
