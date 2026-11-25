# PDC control — esito dei task OpenCode

**Aggiornamento Codex, 25/11/2026:** Carlo ha chiesto di completare la consegna dopo il limite di utilizzo di OpenCode. Il dettaglio del subentro, delle correzioni e dei limiti è in [PDC-CONTROL-CODEX-ESITO.md](PDC-CONTROL-CODEX-ESITO.md). Il testo di OpenCode sotto resta come storico; il registro seguente è aggiornato alla consegna di Codex. Nessun controllo reale è stato attivato.

Mandato: `PDC-CONTROL-OPENCODE.md` del 25 novembre 2026. Questo file registra, task per task, file toccati, comandi, risultati e limiti. Non autorizza installazione, attivazione sull'impianto, commit, push o modifiche Oikos.

## 0. Stato di partenza verificato

- Data: 25 novembre 2026.
- Repository: `repos/versatile_thermostat`; branch `codex/avatto-trv-pilot`; HEAD `c838def`.
- Git: tre soli file non tracciati, tutti preservati: `plans/avatto-oikos-pilot.ps1`, `plans/boiler-timers.ps1`, `plans/heating-orchestration-2026-11-24/PDC-CONTROL-OPENCODE.md`. Nessun reset, clean, stash o cambio branch.
- Impianto: non consultato e non modificato. Nessuna credenziale usata.
- Ambiente test puro: Python 3.15.2, pytest 9.2.0, venv `C:\Users\CarloTomaselli-SOFTCARE\AppData\Local\Temp\opencode\m1venv`. Il venv non contiene `homeassistant`, `pytest_homeassistant_custom_component`, `pytest_socket` né `vtherm_api`: i test HA restano un punto separato (Task 9).
- Baseline PDC pura, rieseguita all'inizio di questa consegna:

```
python -m pytest -q --noconftest --import-mode=importlib -p no:cacheprovider ^
  tests/test_heating_pdc_policy.py tests/test_heating_pdc_transaction.py ^
  tests/test_heating_pdc_provenance.py tests/test_heating_pdc_collector.py ^
  tests/test_heating_pdc_runtime.py tests/test_heating_pdc_integration.py ^
  tests/test_heating_pdc_fake_ha.py
```

Risultato: **604 passed**, 2 warning, 13.12 s.

- Barriera del control invariata: `PDC_CONTROL_AVAILABLE = False` in `feature_heating_pdc_manager.py:70`; il flow offre solo `disabled` e `observe` (`const.py:291`); il parser rifiuta `control` senza `allow_control` (`heating_pdc_collector.py:319-320`); il runtime solleva `PdcControlUnavailableError` senza opt-in; le porte del manager rifiutano comandi, inibizione e scritture fuori dal control.

## Registro dei task

| Task | Stato | Sintesi |
| --- | --- | --- |
| 1 — inventario lacune | completato (solo analisi) | checklist sotto |
| 2 — diagnostica dopo errori (R-4) | completato | reset per passaggio, cattura `Exception`, due test + mutazione |
| 3 - interblocco nel flow | completato localmente da Codex | lista esplicita, conferma, gate chiuso e rilettura prima di ogni effetto |
| 4 - specchio sessione caldaia | completato localmente da Codex | serializzazione, conferme, retry e valori obsoleti |
| 5 - possesso dell'inibizione | completato localmente da Codex | unknown non acquisito; rilascio confermato dallo stato |
| 6 - riavvio e ripresa transazione | completato localmente da Codex | tutte le fasi non idle, store e clock incoerenti |
| 7 - manuale, override e protezioni | completato localmente da Codex | casi per fase, protezioni, programma e risposta tardiva |
| 8 - ciclo di vita entità PDC (R-6/R-9) | completato localmente da Codex | publisher dopo unload fallito e pulizia dei soli sensori rimossi |
| 9 - validazione isolata e regressioni | parziale, blocco ambiente HA | 1122 passed, 1 skipped offline; test HA non eseguiti |
| 10 - consegna per review | completato da Codex | report separato e runbook; nessuna attivazione |

---

## Task 1 — inventario delle lacune (solo analisi)

### 1.1 Metodo

Ho confrontato le lacune di `REVIEW-FINALE-OBSERVE.md` (R-4, R-6, R-9 e limiti §4) e di `M4-M5-RUNTIME-ESITO.md` §4/§9 con il codice effettivo a HEAD `c838def`, rileggendo i punti citati e non solo i report. Ho usato l'inventario `IMPIANTO-INVENTARIO-2026-11-24.md` §5 per i proprietari esterni. Nessuna modifica al codice in questo task.

### 1.2 Lacune confermate

| ID | Posizione nel codice | Comportamento richiesto | Verifica mancante | Task |
| --- | --- | --- | --- | --- |
| L1 (R-4) | `heating_pdc_runtime.py:402-412` (`_work`), `:616-656` (`async_run_pass`), `:647` (azzeramento), `:654` (catch) | `last_error` deve riflettere l'ultimo esito: azzerato a ogni passaggio riuscito (anche observe), impostato e pubblicato a ogni errore. Un errore attivo non va cancellato da un percorso che non lo ha risolto. Nessuna eccezione nascosta. | Osservare un errore, la sua permanenza finché la causa resta, il recupero con azzeramento; observe senza effetti su servizi, inibizione e store. Oggi `:644-646` esce prima di `:647`, quindi in observe l'errore resta appiccicato; `:654` intercetta solo `TypeError/ValueError/KeyError`, le altre eccezioni arrivano a `_work` che imposta `pass_error` ma non pubblica `error`. | 2 |
| L2 | `config_schema.py:313-344`, `config_flow.py:941-951`, `:958-995`, `const.py:291` | Il percorso control deve offrire la modalità, richiedere `external_owner_entity_ids` esplicito per ogni stanza e una conferma; observe e disabled restano invariati. Nella release normale `control` non è selezionabile. | Non esiste alcun campo proprietari nello schema; `_pdc_check` valida sempre con `HO_MODE_OBSERVE`; `HO_PDC_SELECTABLE_MODES` ha due sole voci. Nessun test del percorso control con flag iniettato. | 3 |
| L3 | `config_flow.py:958-995`, `:1617-1621`, `feature_heating_pdc_manager.py:86-98`, `heating_pdc_collector.py:307-320` | La conferma va persistita e verificata a ogni lettura: un `pdc_mode = control` senza conferma non deve costruire un runtime di controllo. La conferma non si eredita quando la configurazione PDC cambia. | `async_finalize` declassa solo la caldaia; `extract_pdc_section` non mappa alcuna conferma; il parser PDC non la richiede. La caldaia ha già questo comportamento (`heating_boiler_collector.py:406-408`), quindi il modello esiste e non va inventato. | 3 |
| L4 | `heating_pdc_runtime.py:806` (setpoint in `_running_step`), `:1116` (`_sync_pdc`), `:1126-1128` (`_emergency_off`), `:1202-1214` (`_suspend_step`), `:972-997` (`_mirror`) | L'interblocco va rivalidato prima di ogni effetto, non solo all'inizio del passaggio. `_execute` lo fa già (`:869-871`), ma i comandi elencati lo aggirano. | Test che facciano comparire un proprietario esterno tra la decisione e ciascun effetto, verificando che nessun servizio parta e che lo stato diagnostico diventi `blocked`. | 3 |
| L5 | `heating_pdc_runtime.py:972-997` (`_mirror`, `_mirror_managed`), `:749-754` (`_on_tx_changed`) | Lo specchio di `in_gestione` deve seguire la sessione: ON quando il possesso è persistito e la sessione inizia, OFF solo dopo lo spegnimento confermato di split e VTherm PDC. Una scrittura fallita va ritentata; una scrittura tardiva non deve lasciare il valore sbagliato. Un helper non disponibile non è OFF. | `_mirror` imposta `mirrored_managed` **prima** della scrittura e non ritenta; la closure controlla solo l'epoch, non il valore desiderato corrente, quindi un ON tardivo può arrivare dopo l'OFF. Nessun test di scrittura fallita, helper unavailable, avvio/ritorno. | 4 |
| L6 (R-6) | `__init__.py:267-270`, `sensor.py:1210-1224` | Dopo un unload fallito il manager ripristinato deve tornare a pubblicare sui sensori esistenti, oppure i sensori devono dichiarare esplicitamente che il manager è nuovo. Nessun listener/timer/task deve restare sul manager morto. | Il manager ripristinato nasce senza sensori agganciati (i sensori hanno `_detach` sul vecchio); resta l'ultimo stato fino al reload. | 8 |
| L7 (R-9) | `sensor.py:96-101`, `:1175-1224` | Rimuovendo una stanza PDC, il suo sensore diagnostico non deve restare orfano nel registro. | Il sensore è creato solo per le stanze presenti all'installazione della piattaforma; la rimozione non cancella l'entità già registrata. | 8 |
| L8 | `config_flow.py:809` | Il passaggio caldaia non deve invalidare una conferma PDC già data: la chiave di conferma PDC va trasportata insieme a `pdc_mode`/`pdc_runtime`. | Oggi il carry-over copia solo le due chiavi PDC; una chiave di conferma a livello di sezione andrebbe persa a ogni modifica caldaia. | 3 |
| L9 | `heating_pdc_runtime.py:796-807`, `:1100-1129`, `:1202-1214` | Durante una sessione, un comando utente, un cambio di modo centrale, uno stop programma, un HEAT tardivo dal cloud o un OFF non confermato devono essere trattati secondo il contratto già presente (fasi, timeout, `return_unconfirmed`), senza nuove politiche. | La policy copre potenza, finestra, modo centrale, programma, temperatura; mancano test runtime per comando utente in ogni fase persistita, cambio modo centrale in corsa, stop programma in corsa, HEAT tardivo, OFF non confermato. | 7 |

### 1.3 Altre verifiche fatte (nessuna nuova lacuna di codice)

- `_blocked_by` (`:609-612`) considera bloccante qualunque stato diverso da `off`, quindi `on`, `unknown`, `unavailable` ed entità assente bloccano già. Non serve una nuova regola.
- `parse_pdc_config` accetta una lista proprietari **dichiarata vuota** in control (`:269-273`), mentre la caldaia richiede almeno un proprietario (`heating_boiler_collector.py:399-400`). È una differenza già documentata nel parser PDC, non una svista: il flow deve comunque scrivere la chiave (lista vuota inclusa), altrimenti `owners_declared` è falso e il parser rifiuta. Da rispettare nel Task 3.
- `OWNER_DOMAINS = ("automation", "script")` (`heating_pdc_collector.py:125`): l'interblocco non può nominare direttamente un `input_boolean` o un `climate`, ma solo le automazioni/script che li scrivono. È coerente con l'inventario §5.
- `extract_pdc_section` sovrappone `pdc_mode` al sotto-dizionario (`feature_heating_pdc_manager.py:97-98`): la conferma PDC deve essere mappata lì, non dentro `pdc_runtime`, per restare separata dalla configurazione runtime.
- Il sensore PDC non ha oggi un attributo `control_status` come lo schema caldaia 1.1: espone lo stato `blocked` e `blocked_by`. Il contratto PDC §4 non richiede `control_status`; la divergenza va registrata, non colmata inventando un campo (Task 10).
- In observe il runtime non chiama `_mirror_*`; aggiungere comunque una guardia esplicita in `_mirror` rende la garanzia "zero scritture in observe" testabile e non dipendente dal chiamante (Task 4).

### 1.4 Proprietari esterni per attuatore/helper (PDC)

Fonte: `IMPIANTO-INVENTARIO-2026-11-24.md` §5. Gli stati sono quelli dell'inventario del 24/11, non una lettura live di oggi.

| Attuatore / helper | Scrittori esterni oggi | Entità dichiarabili nell'interblocco | Note |
| --- | --- | --- | --- |
| `climate.<stanza>_pompa_di_calore` (VTherm PDC) | scelta radiatore/PDC (Carlo e Diego), protezioni override, script `override_sincronizza` / `riprendi_automatico` | `automation.*` della scelta e delle protezioni, `script.override_sincronizza`, `script.riprendi_automatico` | Doppio scrittore storico sullo stesso attuatore; l'interblocco deve nominare tutti gli automation/script attivi, non il `climate` |
| Split `climate.camera_*_condizionatore*` | scelta (OFF), protezioni (OFF), `riprendi_automatico` (OFF), profilo comfort (preset/fan/swing), card Oikos/SmartThings | stesse automazioni/script della riga sopra | Il profilo comfort scrive campi diversi (preset/fan/swing) e può restare esterno (inventario §7.4); non è un proprietario di `hvac_mode` |
| `input_boolean.<stanza>_pdc_in_gestione` | scelta, protezioni, `override_sincronizza`, `riprendi_automatico` | le stesse automazioni/script | 4 scrittori per stanza; coperti dalle stesse entità |
| `input_boolean.<stanza>_pdc_override` | `override_avvia`, `riprendi_automatico` | `automation.*` dell'avvio override, `script.riprendi_automatico` | Letto da profilo, protezioni e fine override |
| `input_text.<stanza>_pdc_ciclo` | registri ciclo (Carlo, Diego) | — (può restare esterno) | Il runtime lo legge soltanto; non lo scrive |
| `input_text.<stanza>_pdc_comandi_attesi` | registri comandi | — (può restare esterno) | Letto dall'override manuale esterno |
| `input_text.<stanza>_pdc_transizione` | scelta | — (può restare esterno) | Il runtime usa il proprio store, non questo helper |
| Inibizione del VTherm radiatore | nessun scrittore trovato nelle automazioni (ipotesi dell'inventario) | — | Da verificare prima di aprire il control: se un'automazione inibisce il radiatore, va dichiarata come proprietario |
| `climate.termostato_ingresso` (caldaia) | `caldaia_controllo_stabile` | — | Fuori ambito: la barriera caldaia resta |

Regola operativa: finché una qualunque entità dichiarata non è **esattamente** `off`, la stanza resta in osservazione/bloccata e non parte nessun comando. Il fork non accende né spegne mai un proprietario esterno.

### 1.5 Tre livelli di prova da non confondere

| Livello | Cosa dimostra | Come si osserva nel codice | Cosa non dimostra |
| --- | --- | --- | --- |
| Esito della chiamata al servizio | Che HA ha accettato la chiamata senza eccezioni | `_command` restituisce `True` (`:851-861`) | Che il dispositivo abbia eseguito: nessuna conferma di stato |
| Conferma di stato | Che l'entità rilegge lo stato atteso | `lookup(states, ...)`, `read_inhibition`, `SPLIT_ACTIVE_MODES`, `STATE_OFF` | Che il dispositivo fisico sia acceso/spento davvero; `unknown`/`unavailable`/assente non sono OFF |
| Attività del compressore | Che la PDC sta comprimendo | Nessuna entità o telemetria nel repository | Non è osservabile localmente; richiederebbe dati d'impianto, fuori ambito |

Conseguenza per i test: un `call_service` riuscito non può essere usato come prova di "acceso" o "spento"; ogni verifica di effetto deve rileggere lo stato, e i test devono distinguere i tre livelli.

### 1.6 Cosa non è una lacuna (vincoli da preservare)

- Soglie M3 approvate: 1100 W di avvio, 2700 W di arresto in corsa, 0,4 °C di gap, 120 s di stabilità, 600 s di minimo acceso e di guardie, 45 s di età potenza, 3600 s di età temperatura. Non si cambiano.
- `heating_pdc_policy.py` e `heating_pdc_provenance.py` sono congelati/invarianti.
- Il controllo caldaia e la sua barriera sono fuori ambito.
- `PDC_CONTROL_AVAILABLE = False` resta per tutta la consegna; i test usano i punti di iniezione espliciti.
- Le differenze di configurazione Carlo / Diego Luisa (soglia 16 / 18 °C, bypass preset) restano quelle approvate.

### 1.7 Punti che richiedono una decisione o un contratto nuovo

1. **Lista concreta dei proprietari per stanza.** È una decisione dell'operatore, non del codice: il flow richiede la lista esplicita, ma quali automazioni/script spegnere va inventariato e confermato prima di un test su banco. Va nel runbook del Task 10; non blocca i Task 2-9.
2. **Perdita dell'interblocco durante una protezione.** Il mandato chiede di rivalidare prima di ogni effetto, quindi anche `_emergency_off` si ferma. Conseguenza: se un proprietario esterno torna attivo mentre scatta una protezione (es. picco di potenza), il fork non spegne; l'azione passa al proprietario esterno. È coerente con "il fork non scrive quando è bloccato", ma è un effetto safety-relevant da documentare esplicitamente nel Task 3 e da riportare nel Task 10, non da reinterpretare in silenzio.
3. **Rimozione del sensore orfano (R-9).** Va rimossa solo l'entità diagnostica PDC del fork quando la stanza non è più configurata, mai entità dell'impianto. Se l'API del registro non è disponibile, il Task 8 deve segnalare il limite invece di cancellare altro.
4. **`control_status` nello schema PDC.** Il contratto PDC §4 non lo prevede; la caldaia lo ha nello schema 1.1. Non lo aggiungo senza un aggiornamento di contratto: resta una divergenza documentata.

Nessuno di questi punti impedisce di iniziare il Task 2.

---

## Task 2 — diagnostica coerente dopo errori (R-4)

### 2.1 Modifica

File: `custom_components/versatile_thermostat/heating_pdc_runtime.py`.

- `async_run_pass` (`:619-667`): `last_error` e `blocked_by` vengono azzerati all'inizio di **ogni** passaggio, prima del ramo observe. Un passaggio che arriva in fondo risolve l'errore del precedente; una causa ancora presente lo ripropone nel passaggio che la incontra.
- Il `try` ora comprende anche il controllo degli input e il ramo observe, quindi un errore del collector o della pubblicazione viene gestito allo stesso modo in observe e in control.
- La cattura passa da `(TypeError, ValueError, KeyError)` a `Exception`, con `_LOGGER.exception` (traccia completa, nessuna eccezione nascosta). `asyncio.CancelledError` non è intercettata perché deriva da `BaseException`.
- L'errore viene salvato in `room.last_error` e pubblicato con `_publish_empty(room, STATE_ERROR, ...)`, usando l'istante corrente.
- `_work` (`:410-416`) resta come ultima rete di sicurezza e ora tenta anche la pubblicazione, senza poter morire se la diagnostica stessa fallisce.
- Gli errori persistenti restano nei loro campi separati: `_last_save_error` (azzerato solo da un salvataggio riuscito), `_load_errors` (errori di lettura dello store) e `last_denied` (motivo di un avvio negato, azzerato quando un avvio viene accettato). Il reset per passaggio non li tocca.

### 2.2 Test aggiunti

File: `tests/test_heating_pdc_runtime.py`.

- `test_observe_publishes_an_error_and_clears_it_when_a_later_pass_succeeds`: errore iniettato con `_collect` che solleva `RuntimeError`; verifica stato `error`, `last_error = evaluation_error:RuntimeError`, nessun servizio, nessuna inibizione, nessun salvataggio, store ancora `None`; secondo passaggio con la causa ancora presente (errore ripubblicato, `evaluated_at` avanzato); poi ripristino e passaggio normale con `last_error` tornato `None` e ancora zero effetti.
- `test_a_control_pass_that_fails_is_published_and_sends_nothing`: stesso errore in control; verifica `error` pubblicato, nessun comando, nessuna inibizione, nessun salvataggio; al ripristino il motore torna a valutare e `last_error` è `None`.

### 2.3 Comandi e risultati

```
python -m pytest -q --noconftest --import-mode=importlib -p no:cacheprovider ^
  tests/test_heating_pdc_runtime.py -k "observe_publishes_an_error or control_pass_that_fails"
```

`2 passed`, 57 deselected, 0.75 s.

Suite PDC pura completa: **606 passed**, 2 warning, 18.15 s (604 di baseline + 2 nuovi).

Verifica di mutazione: ripristinata temporaneamente la vecchia cattura `(TypeError, ValueError, KeyError)`, i due nuovi test **falliscono entrambi**; il file è stato poi ripristinato dal backup e i due test tornano verdi. I test quindi coprono davvero il difetto e non solo il percorso felice.

Black 24.10.0 `--check --line-length 180` sui due file modificati: nessuna modifica necessaria.

### 2.4 Limiti

- La prova è sul runtime puro con HA finto; non c'è stata esecuzione su Home Assistant (Task 9).
- Il reset per passaggio è deliberato: `last_error` descrive l'ultimo passaggio, non è un latch di guasto. Un guasto persistente resta visibile tramite la fase della transazione (`return_unconfirmed`), `_last_save_error`, `_load_errors` e `last_denied`.
- In observe un errore del collector non interrompe i passaggi successivi: il timer di rivalutazione continua a riprovare, come prima.
