# M1 — Esito: motore decisionale caldaia

Stato corrente: **M1 e revisione R1/R2/E1 completati sul codice locale del fork, in attesa della review Codex.** Il motore **non è collegato** al runtime VTherm e **l'impianto non è stato modificato**. Nessun commit, push, tag, release o PR; nessuna modifica a YAML live, `.storage`, helper, entità, dashboard Oikos, fixture originali o file del reviewer. **M2 non è iniziato.**

Provenienza: la prima consegna è di OpenCode (sezione storica in fondo, testo invariato). La revisione R1/R2/E1 è di Claude, su incarico `CLAUDE-HANDOFF.md` e sulla base di `M1-REVIEW.md`. I risultati in `review/` restano del reviewer; le verifiche di Claude sono separate in `m1-r1-claude/`.

## Revisione Claude — R1, R2, E1 (24 novembre 2026)

### Stato di partenza verificato

- Branch `codex/avatto-trv-pilot`, HEAD `6a819dcc1437fb33ddf8bf0dc2e3aa09e0c80666`, nessun file tracciato modificato. Al termine branch e HEAD sono invariati; i file restano non tracciati.
- Hash di motore e test identici a quelli del passaggio (`25b47e7f…`, `2e8ac521…`): nessuna correzione parziale da preservare.
- Fixture `baseline/boiler-decision-cases.json` con SHA-256 `b2d758bd…f7e`, uguale a `index.json` e all'errata.
- Fallimento R2 riprodotto prima della correzione: comando puro senza stub → `1 error during collection` (import di `custom_components.versatile_thermostat.__init__`).
- Ripristino: le versioni OpenCode dei due file sono copiate in `m1-r1-claude/originali-opencode/`. Per tornare alla prima consegna basta ricopiarle nei percorsi originali.

### R1 — diagnostica coerente con il percorso

`_build_conditions` determina prima il **percorso** della decisione, con lo stesso ordine dei rami del template. Poi valuta soltanto i requisiti di quel percorso; quelli degli altri percorsi diventano `na`.

- master `heat` → mantenimento;
- master diverso da `off`/`heat` → master non disponibile;
- master `off` con manuale `on` → avvio manuale;
- master `off` con manuale `off` o ignoto → avvio automatico. Il ramo automatico del template richiede `manual == 'off'`, quindi un manuale ignoto resta un blocco `unknown` di quel percorso.

| Condizione | Manuale | Mantenimento | Automatico | Master non disponibile |
| --- | --- | --- | --- | --- |
| `master` | pass | pass | pass | unknown |
| `inputs`, `valve_count` | applicate | applicate | applicate | applicate |
| `thermal_gap` | na | na | pass / wait | na |
| `pilot` (prontezza globale `pilot_ready`) | na | na | pass / fail / unknown; na con pilota OFF | na |
| `single_room_pilot` (veto `pilot_ok` della stanza) | na | na | pass / fail solo con pilota ON, una valvola e una stanza qualificata; altrimenti na | na |
| `manual_request` | pass | na | na se OFF, unknown se ignoto | na |
| `path` | pass `manual` | na | pass `normal_wait`/`single_wait`, oppure wait con il motivo della decisione | unknown `master_unavailable` |
| `stability` | na | na | solo con percorso candidato | na |
| `cooldown` | na | na | pass / wait | na |
| `consent` | na | na | solo con candidato normale | na |

Differenze diagnostiche rispetto alla prima consegna:

- Manuale: gap, pilota, stabilità, pausa e consenso sono `na`, anche con ingressi non utilizzabili. Prima gap e pilota venivano valutati, e con ingressi non validi anche la pausa.
- Mantenimento: `pilot` è `na`. Prima poteva risultare `fail`/`unknown`.
- Master non disponibile: i requisiti di avvio sono `na` e `path` è `unknown/master_unavailable`. Prima risultava `path=wait/thermal_wait` con gap e pilota valutati.
- Percorso automatico senza candidato: `path.reason_code` (e quello di `cooldown` in attesa) riporta il motivo effettivo della decisione: `thermal_wait`, `data_missing`, `data_or_central` o `no_demand`. Prima era sempre `thermal_wait`.
- Nuova condizione `single_room_pilot`, con codice diagnostico `CONDITION_REASON_SINGLE_ROOM_PILOT_VETO = "single_room_pilot_veto"`. È distinta da `pilot`, che rappresenta il consenso globale `pilot_ready`. Nel percorso normale resta `na`, perché la baseline non applica il veto di stanza.

**Politica decisionale invariata.** Il confronto AST con l'originale mostra modifiche solo in `_build_conditions` e nella chiamata da `evaluate_boiler`, che non passa più `usable` e passa `reason`. Azione, motivo, scadenza e ciclo non dipendono dalle condizioni, e la parità è confermata sotto.

Regressioni aggiunte in `tests/test_heating_boiler_policy.py`:

| Test | Caso |
| --- | --- |
| `test_manual_start_diagnostics_ignore_gap_and_pilot` (5) | Manuale con gap 0,1 e pilota negato/ignoto/non valido → `heat/manual`, condizioni di avvio `na`, nessun blocco |
| `test_maintenance_diagnostics_ignore_pilot_and_start_requirements` (5) | Mantenimento con pilota negato/ignoto, manuale on/off/ignoto → `none/maintain`, nessun blocco |
| `test_maintenance_diagnostics_keep_inputs_and_valve_count` (2) | Mantenimento con zero valvole o ingressi non validi → `off/no_valid_radiator`, unico blocco `valve_count` o `inputs` |
| `test_single_room_pilot_veto_is_distinct_from_global_readiness` | Singolo con pilota pronto e `pilot_ok=false` → `none/thermal_wait`, `pilot=pass`, `single_room_pilot=fail/single_room_pilot_veto`; controprova con `pilot_ok=true` → `heat/single` |
| `test_normal_path_diagnostics_do_not_require_room_pilot_qualification` (2) | Normale con stanze senza qualificazione pilota, pilota ON pronto o OFF → `heat/normal`, `single_room_pilot=na`, nessun blocco |
| `test_master_unavailable_diagnostics_expose_only_the_master` | Master `unavailable` → blocchi solo `master` e `path` |
| `test_diagnostics_agree_with_the_decision_on_a_deterministic_grid` | 23.328 combinazioni (refuso 15.552 corretto il 24/11 su segnalazione della review) (master, ok, valvole, gap, `pilot_ok`, manuale, pilota, prontezza, consenso, 4 cicli). Una decisione `manual`/`normal`/`single`/`maintain` non ha blocchi; ogni altra ne espone almeno uno. Nei percorsi manuale e mantenimento le condizioni di avvio sono sempre `na` |

Il test esistente sull'insieme degli id diagnostici include ora `single_room_pilot`.

### R2 — suite pura portabile

- Il test non importa più `custom_components.versatile_thermostat`. Carica il solo file `heating_boiler_policy.py` con `importlib.util.spec_from_file_location`, con il nome privato `_vtherm_pure_heating_boiler_policy_under_test`. Il modulo viene registrato in `sys.modules` solo con quel nome, necessario ai dataclass. Nessun package viene sostituito o simulato, `__init__.py` del componente non è modificato e non esiste più alcuno stub.
- `test_pure_module_loads_in_an_isolated_interpreter_without_the_component_package` avvia `python -I` (senza PYTHONPATH né site utente), carica il modulo, verifica che non siano importati `homeassistant`, `custom_components` o `vtherm_api` e valuta un caso.
- `test_policy_under_test_is_loaded_from_the_component_source` verifica che il file caricato sia quello reale del componente e che non prenda il posto dell'attributo del package.
- Ambiente completo: il file resta eseguibile con il conftest del repository. `test_component_package_exposes_the_same_source` (con `importorskip("homeassistant")`) verifica che l'import normale del package risolva lo stesso sorgente. Nell'ambiente minimo il test risulta **skipped**, non passato.
- Il docstring del test riporta i comandi aggiornati. Nessun requisito, lockfile o configurazione pytest è stato modificato.

### E1 — rettifica autorizzata degli expected

- `corrected_result_by_name()` legge `baseline/boiler-decision-errata.json` e verifica `schema_version`, `source_file` e lo SHA-256 dei byte della fixture. Controlla anche che le rettifiche siano esattamente quelle autorizzate (`AUTHORIZED_ERRATA = {("master_unknown", ("cycle", "r"))}`) e che il valore originale sia `1800`. Solo allora applica `null` a una **copia profonda in memoria**.
- `test_baseline_decision_matches_saved_result` confronta ora **tutti i 27 casi** (azione, motivo, deadline, `m/off/r/s`) senza esclusioni per nome.
- `test_errata_targets_the_indexed_fixture_and_leaves_it_untouched` verifica che fixture, indice ed errata abbiano lo stesso hash e che l'originale in memoria conservi `r=1800` anche dopo la rettifica.
- `test_master_unknown_errata_is_the_only_authorized_correction` sostituisce il precedente test di divergenza e ne conserva la spiegazione verificata. Con master `unavailable`, `usable` e `normal` sono falsi e il template imposta `ready = none`, quindi `r` viene azzerato. Il test controlla anche che la rettifica cambi solo quel campo e solo quel caso.
- Il punto M2 della prima consegna “correggere o rigenerare la fixture `master_unknown`” è chiuso dall'errata del reviewer; la fixture originale non è stata toccata.

### File cambiati e hash

| File | Prima (OpenCode) | Dopo (Claude) |
| --- | --- | --- |
| `custom_components/versatile_thermostat/heating_boiler_policy.py` | `25b47e7ff87f865b4c0523937a2dcd193e3c7e370ad779e7a3cc77fbf8cd2a35` | `edf7e9007ac44166bfc56a3ad8c74dd6b167af6f4ed2760831d865443c27945b` |
| `tests/test_heating_boiler_policy.py` | `2e8ac52101d445a0bdaa1130b81e4df64853f226e38afa41fb8ec774b88a9fe3` | `43764243ad73e53740655b6690d0d74aaf071ba08d3dcff19d30d2e0d9c57708` |
| `plans/heating-orchestration-2026-11-24/M1-ESITO.md` | prima consegna | questa revisione aggiunta in testa |

Nuovi file, tutti sotto `plans/heating-orchestration-2026-11-24/m1-r1-claude/`:

| File | Contenuto | SHA-256 |
| --- | --- | --- |
| `originali-opencode/heating_boiler_policy.py` | Copia di ripristino della prima consegna | `25b47e7f…a35` |
| `originali-opencode/test_heating_boiler_policy.py` | Copia di ripristino della prima consegna | `2e8ac521…fe3` |
| `originali-opencode/M1-ESITO.md` | Esito della prima consegna prima di questo aggiornamento | `63050f9c28f81a0aa357926924be72a6ab5af935159566eb8e16c5a00e874bbd` |
| `verify_m1-risultato-claude.json` | Output JSON di `review/verify_m1.py` eseguito da Claude | `45075be806d0d43a38832b1673b34e7a2196738662553a32641c5abec9728c51` |
| `verify_m1-stdout.txt` | Hash del motore verificato, versioni, comando e stdout | `6b6ef29edfa4a36e558a8175bbddeabc92c1113898cd4733ec770b79ac433b82` |

Invariati e controllati: `review/verify_m1.py` (`71ba7f79…6f4`), `review/M1-verifica.json` (`a1a7e611…939`), tutte le 17 fixture baseline (verificate dallo script del reviewer) e `__init__.py` del componente.

### Comandi ed esiti di Claude

Ambienti, tutti Windows con Python 3.15.2, PYTHONPATH vuoto e nessun Home Assistant:

- **A — solo pytest:** venv nuovo nella scratchpad della sessione Claude con `pip install pytest` (pytest 9.2.0 e sue dipendenze: colorama, iniconfig, packaging, pluggy, Pygments). Ricreabile con `python -m venv <dir>` seguito da `<dir>\Scripts\python -m pip install pytest`.
- **B — venv OpenCode** `…\Temp\opencode\m1venv` (pytest 9.2.0, black 26.10.1, Jinja2 3.1.6), usato **senza** lo stub. Verificato: `include-system-site-packages = false` e nessun file `.pth`.
- **C — Black della CI:** venv nella scratchpad con `pip install "black>=24.0,<25.0"`, versione installata 24.10.0, lo stesso vincolo di `.github/workflows/pull.yml`.

Dalla radice del repository, con PYTHONPATH non impostato (`-p no:cacheprovider` evita di scrivere `.pytest_cache` nel fork):

```powershell
python -m pytest -q --noconftest --import-mode=importlib -p no:cacheprovider tests/test_heating_boiler_policy.py
```

| Esecuzione | Esito |
| --- | --- |
| A, comando sopra | **143 passed, 1 skipped** (`test_component_package_exposes_the_same_source`: `homeassistant` assente), 2 `PytestConfigWarning` sulle opzioni `asyncio_*` |
| A, stesso comando senza `--import-mode=importlib` | 143 passed, 1 skipped |
| A, da `tests/` con `test_heating_boiler_policy.py` | 143 passed, 1 skipped |
| B, comando sopra | 143 passed, 1 skipped |
| A, con il conftest del repository (`python -m pytest -q tests/test_heating_boiler_policy.py`) | Errore atteso nel caricamento del conftest: `ModuleNotFoundError: pytest_socket`. Dimostra solo che l'ambiente completo HA manca qui; non è un'esecuzione dell'ambiente completo |

Conteggio: 122 test della prima consegna, meno 26+1 sostituiti, più 27 di parità, 2 di errata, 17 di R1 e 3 di R2: 144 raccolti.

Formattazione, limitata ai due file:

```powershell
python -m black --check --diff --line-length 180 custom_components/versatile_thermostat/heating_boiler_policy.py tests/test_heating_boiler_policy.py
```

- C (black 24.10.0): **2 files would be left unchanged**, exit 0.
- B (black 26.10.1): **2 files would be left unchanged**, exit 0.
- In questa sessione Black è terminato in circa 5 s, eseguito con `timeout 120`: il blocco osservato in review non si è ripetuto. Un primo passaggio con black 26 aveva segnalato lo stile di una stringa multilinea nel nuovo test; lo snippet è stato riscritto come lista di righe, una forma neutra rispetto alle due versioni.

Parità indipendente con lo script del reviewer, non modificato (ambiente B, Jinja2 3.1.6):

```powershell
python -I plans/heating-orchestration-2026-11-24/review/verify_m1.py
```

- Esito: `hashes: 17`, `baseline_parity: 27`, `random_parity: 5000` (seed 104729), exit 0, sul motore `edf7e900…945b`. Ogni caso confronta azione, motivo, deadline e contesto successivo con il template Jinja catturato.
- `fixture_differences: ["master_unknown"]` è atteso: lo script confronta con la fixture originale, non con l'errata. La differenza è quella autorizzata in E1.
- Le riproduzioni diagnostiche del reviewer ora danno: manuale con gap 0,1 e pilota negato → `thermal_gap`, `pilot`, `single_room_pilot`, `stability`, `cooldown`, `consent` tutti `na`; mantenimento → `pilot` e i requisiti di avvio `na`. In entrambi i casi non ci sono blocchi.
- Lo script scrive `review/M1-verifica.json`. Per non alterare l'evidenza del reviewer il file è stato copiato prima dell'esecuzione e poi ripristinato; l'hash `a1a7e611…939` risulta invariato. L'output di Claude è in `m1-r1-claude/`.

### Controlli non eseguiti

- `tests/test_central_boiler.py`, `tests/test_avatto_room_control.py` e la suite pura con il conftest HA **non sono stati eseguiti**. Manca l'ambiente Home Assistant (`requirements_test.txt`, Python 3.14 della CI), e l'installazione non è stata ritentata dopo il fallimento documentato nella prima consegna. Il motore resta non importato dal runtime.
- `test_component_package_exposes_the_same_source` è **skipped** in tutti gli ambienti usati.
- Python 3.14 non è disponibile localmente: la suite pura è stata eseguita soltanto con 3.15.2.
- Black è stato controllato solo sui due file, non con `black .` sull'intero repository. Ruff, pylint e mypy non sono stati eseguiti.
- Nessuna esecuzione con xdist, nessun accesso all'impianto, alle entità, a MQTT o a Oikos.

### Punti per la review Codex

- `path.actual` conserva i valori della prima consegna (`normal_wait`, `single_wait`, `manual`) mentre `expected` è `manual|normal|single`. È un'incoerenza ereditata, da sistemare insieme allo schema definitivo M2.
- `single_room_pilot.actual` è il booleano `pilot_ok`. Lo slug della stanza non è esposto perché `BoilerCondition` non ha un campo stanza; le condizioni per stanza saranno parte del contratto M2.
- Con pilota ignoto e un solo candidato, `single_room_pilot` è `na`: il blocco è già esposto da `pilot=unknown`.
- Con `ok=false` nel percorso automatico, gap e pilota sono ancora calcolati dallo snapshot. Il collector M2 dovrà stabilire se le stanze sono significative quando gli ingressi non sono validi.
- `review/verify_m1.py` scrive dentro `review/`. Se Codex preferisce, può rendere il percorso di output un parametro; Claude non lo ha modificato.
- `m1-r1-claude/originali-opencode/` serve solo al ripristino e al diff della review: può essere rimosso dopo l'approvazione.

---

## Prima consegna — OpenCode (storico)

Testo originale della prima consegna, invariato salvo i titoli abbassati di un livello. Alcuni dati sono superati dalla revisione Claude: 26/27 confronti integrali, 122 test, comandi con stub, test dedicato alla divergenza `master_unknown`.

Stato: **M1 completato sul codice locale del fork**. Il motore **non è collegato** al runtime VTherm e **l'impianto non è stato modificato**. Nessun commit, push, tag, release o PR; nessuna modifica a YAML live, `.storage`, helper, entità, dashboard Oikos o fixture originali.

Baseline verificata all'inizio del lavoro: branch `codex/avatto-trv-pilot`, HEAD `6a819dcc1437fb33ddf8bf0dc2e3aa09e0c80666`, coerente con l'incarico. Al termine il branch e l'HEAD sono invariati; l'albero contiene solo file nuovi non tracciati (motore, test, cartella `plans/` già presente).

### File creati

| File | Contenuto | SHA-256 |
| --- | --- | --- |
| `custom_components/versatile_thermostat/heating_boiler_policy.py` | Motore puro, modelli tipizzati, parsing del contesto, validazione, condizioni diagnostiche | `25B47E7FF87F865B4C0523937A2DCD193E3C7E370AD779E7A3CC77FBF8CD2A35` |
| `tests/test_heating_boiler_policy.py` | Parità fixture, matrice M1, sequenze, restore, immutabilità, validazione, purezza | `2E8AC52101D445A0BDAA1130B81E4DF64853F226E38AFA41FB8EC774B88A9FE3` |

Nessun file tracciato è stato modificato. Nessun file esistente del repository è stato adattato per far passare i test.

### Scelte tecniche

#### Fonte normativa

La fonte usata è il **template `plan` attualmente presente** in `baseline/caldaia_controllo_stabile_delle_richieste.json`, non una sua ricostruzione. Il collector `raw` resta fuori da M1: il motore riceve uno snapshot già normalizzato. Di conseguenza appartengono al futuro collector (M2) qualificazione delle stanze, conteggio delle valvole fisiche, freschezza, finestre, inibizioni, PDC e disponibilità della centrale.

#### Modulo puro

`heating_boiler_policy.py` non importa Home Assistant, non fa I/O, non legge clock globale, non usa asyncio e non chiama servizi. Il tempo è sempre un parametro esplicito (`now`, secondi Unix assoluti). Il test `test_engine_has_no_home_assistant_or_io_imports` verifica l'assenza di import vietati.

API pubblica:

- `evaluate_boiler(snapshot: BoilerSnapshot, cycle: BoilerCycle | None, now: float, config: BoilerPolicyConfig | None = None) -> BoilerDecision`
- `parse_cycle_context(value: Any) -> BoilerCycle`
- modelli frozen con `slots`: `BoilerPolicyConfig`, `QualifiedBoilerRoom`, `BoilerSnapshot`, `BoilerSingleWait`, `BoilerCycle`, `BoilerCondition`, `BoilerDecision`
- costanti per azioni, motivi, stati delle condizioni e default.

#### Parametri

Default identici alla baseline: gap `0.3 °C`, due valvole ordinarie, stabilità normale `120 s`, attesa singola `900 s`, pausa OFF `600 s`. `BoilerPolicyConfig` valida tipi, finitezza e minimi; `normal_valve_count` deve essere un intero `>= 2`, perché il percorso normale è definito da almeno due valvole. Le associazioni stanza/valvola non sono nel motore.

#### Semantica preservata

- `count` è il numero di **valvole fisiche valide**, non di stanze: due valvole nella stessa stanza attivano il percorso normale.
- Il percorso normale richiede `count >= 2`, almeno una stanza qualificata, master OFF/HEAT e consenso di automatismo (pilota OFF, oppure pilota ON con `pilot_ready` vero).
- Il percorso singolo richiede esattamente una valvola, esattamente una stanza qualificata, identità `slug` e `target` uguali all'ancora precedente e, con pilota ON, `pilot_ok` della stanza.
- Il percorso manuale è immediato con master OFF e ingressi utilizzabili; non richiede stabilità, pausa o consenso nativo.
- Il mantenimento (master HEAT) non riapplica soglia termica, numero minimo o consenso; con zero valvole o ingressi invalidi propone OFF.
- Master diverso da OFF/HEAT (inclusi `unknown`, `unavailable`, `None`) non produce comandi e non è trattato come OFF confermato.
- Le ancore sono conservate e ripristinate come secondi interi, con la stessa troncatura di `|int`; un'ancora negativa o futura viene riavviata all'istante di valutazione.
- Un contesto OFF assente/incoerente avvia una nuova pausa da `now`; il passaggio a OFF confermato riavvia la pausa.
- La proposta HEAT non modifica il master osservato né fa partire un tempo ON: `cycle.mode` resta il modo campionato nello snapshot.
- `deadline` è il massimo tra scadenza di stabilità e fine pausa. Nei casi di consenso mancante la baseline azzera la deadline: comportamento replicato.

#### Robustezza del contesto

`parse_cycle_context` accetta mapping, stringhe numeriche (come `float(none)` di Jinja), contesti mancanti/non mapping/troncati e campi invalidi. Un'ancora non finita o non interpretabile diventa `None` invece di propagare `NaN`/`Infinity`: è l'unico irrigidimento rispetto alla lettera del template, motivato dal contratto diagnostico che vieta valori non JSON-safe. Non cambia alcun caso baseline valido.

#### Contratto diagnostico

`BoilerDecision` include `evaluated_at` e una tupla `conditions` con `id`, `status` (`pass`/`fail`/`wait`/`unknown`/`na`), `reason_code`, `actual`, `expected` e `deadline` facoltativa. Le condizioni sono **informative**: la politica non le legge e non ne dipende. Sono una proposta per M2, non nuove entità né sensori.

### Parità con la baseline

- Fixture caricate per nome da `baseline/boiler-decision-cases.json`: 27 casi e 27 esiti.
- Confronto completo di `action`, `reason`, `deadline` e ciclo successivo (`m`, `off`, `r`, `s`), non della sola azione.
- **26 casi su 27** coincidono integralmente con l'esito salvato.
- **1 discrepanza nella fixture**, non nel motore: `master_unknown`. L'input salvato ha `cycle = {"m":"off","off":1000,"r":1800,...}` e `raw.master = "unavailable"`. Il template corrente e il motore restituiscono `cycle.r = null`; l'esito salvato riporta `r = 1800`. Tutti gli altri campi coincidono. La fixture non è stata modificata; il caso è escluso dal confronto integrale e coperto da `test_master_unknown_saved_result_disagrees_with_the_baseline_template`, che documenta la divergenza. Va corretto in sede di review, non nel codice M1.

Verifica aggiuntiva eseguita con un harness Jinja2 locale (non versionato): il motore è stato confrontato con il template `plan` su tutti i 27 casi e su **20.000 casi casuali** (master, conteggi, stanze, gap, ancore, consensi, manuale, pilota): **27/27 e 20.000/20.000** con uguaglianza esatta di azione, motivo, deadline e ciclo successivo.

### Copertura della matrice M1

| Area richiesta | Copertura nei test |
| --- | --- |
| Parità fixture | 27 casi caricati; 26 confronti integrali; `master_unknown` con test dedicato alla divergenza |
| 0/1/2 valvole | `test_normal_path_boundaries`, `test_single_path_boundaries`, `test_two_valves_in_the_same_room_use_the_normal_path` |
| Due valvole nella stessa stanza | `test_two_valves_in_the_same_room_use_the_normal_path` |
| Gap 0,299 / 0,300 | `test_normal_path_boundaries` |
| 119 / 120 s | `test_normal_path_boundaries` (ancora 1881/1880 a `now=2000`) |
| 899 / 900 s | `test_single_path_boundaries` (ancora 1101/1100) |
| 599 / 600 s | `test_normal_path_boundaries` (pausa 1401/1400) |
| Deadline = massimo tra stabilità e cooldown | `test_normal_path_boundaries` (pausa 1950 → deadline 2550) |
| Perdita/ritorno requisiti, candidato o target diverso | `test_single_path_boundaries`, `test_losing_and_regaining_requirements_restarts_normal_stability` |
| Continuità del normale con stanze diverse | `test_normal_stability_survives_a_change_of_room` |
| Manuale ON/OFF/unknown, solo con ingressi utilizzabili | `test_manual_and_missing_values` |
| Manuale non altera le protezioni | `test_manual_and_missing_values` (master HEAT, zero valvole, ingressi invalidi) |
| Mantenimento indipendente dal consenso | `test_maintenance_does_not_reapply_start_conditions` |
| Contesto assente/non mapping/numeri assenti/negativi/futuri | `test_non_mapping_context_is_empty`, `test_context_with_invalid_entries_is_partially_ignored`, `test_numeric_strings_are_accepted_like_the_jinja_float_filter`, `test_non_finite_anchors_are_treated_as_missing`, `test_single_anchor_with_non_numeric_target_is_dropped`, `test_truncated_off_anchor_reproduces_the_one_second_effect`, `test_timestamp_limits_are_deterministic` |
| Master non disponibile | `test_master_unavailable_never_commands` (auto, cool, unavailable, unknown, None) |
| Valutazioni duplicate e sequenze con cambio modo | `test_duplicate_evaluation_does_not_move_the_anchors`, `test_heat_proposal_does_not_confirm_the_master_or_start_on_time`, `test_full_sequence_off_request_expiry_heat_single_zero_off_restart`, `test_transition_to_off_starts_a_new_cooldown` |
| Pilota ON con veto / stanza singola non qualificata | `test_pilot_veto_is_ignored_by_the_normal_path`, `test_pilot_on_without_readiness_does_not_authorize_automatic_start`, `test_single_path_boundaries` |
| Consenso nativo negato dopo la stabilità | `test_normal_path_boundaries` (`awaiting_consent` con `consent=False/None`) |
| Immutabilità e determinismo | `test_inputs_are_not_mutated_and_results_are_new_objects`, `test_duplicate_evaluation_does_not_move_the_anchors` |
| Parametri e input non validi | `test_invalid_config_is_rejected`, `test_invalid_snapshot_is_rejected`, `test_invalid_now_is_rejected`, `test_evaluate_rejects_wrong_model_types`, `test_config_parameters_are_applied` |
| Contratto diagnostico | `test_diagnostic_conditions_are_structured_and_json_safe`, `test_single_path_diagnostic_marks_consent_not_applicable` |
| Purezza del motore | `test_engine_has_no_home_assistant_or_io_imports` |
| Raccolta, lifecycle, PDC, transazioni, provenienza | Fuori da M1: non implementati, come da incarico |

### Comandi e risultati

Ambiente: Windows, Python 3.15.2, venv isolato `C:\Users\CarloTomaselli-SOFTCARE\AppData\Local\Temp\opencode\m1venv` con `pytest`, `black`, `Jinja2`. La suite è stata eseguita con `--noconftest --import-mode=importlib` e un package stub locale, perché `tests/conftest.py` importa Home Assistant e l'ambiente HA non è installabile su questa macchina (vedi sotto).

Test del motore:

```powershell
$env:PYTHONPATH="C:\Users\CarloTomaselli-SOFTCARE\AppData\Local\Temp\opencode\stub"
& "C:\Users\CarloTomaselli-SOFTCARE\AppData\Local\Temp\opencode\m1venv\Scripts\python.exe" -m pytest -q --noconftest --import-mode=importlib `
  "C:\Users\CarloTomaselli-SOFTCARE\OneDrive - SOFTCARE SRL\Documenti\Home Assistant\repos\versatile_thermostat\tests\test_heating_boiler_policy.py"
```

Risultato: **122 passed**, 0 failed (due warning `PytestConfigWarning` per opzioni `asyncio_*` del `pyproject.toml` non note al pytest del venv, senza impatto).

Formattazione:

```powershell
& "...\m1venv\Scripts\python.exe" -m black --check --line-length 180 `
  "custom_components\versatile_thermostat\heating_boiler_policy.py" `
  "tests\test_heating_boiler_policy.py"
```

Risultato: **2 file would be left unchanged**.

Parità Jinja2 (harness locale non versionato): `fixtures: 27 ok`, `random: 20000 ok`.

### Controlli non eseguiti

- `tests/test_central_boiler.py` e `tests/test_avatto_room_control.py` **non sono stati eseguiti**. Richiedono l'ambiente di test Home Assistant (`requirements_test.txt`). Il tentativo di installazione isolata di `homeassistant==2026.10.4` + `pytest-homeassistant-custom-component` su Python 3.15.2 fallisce in fase di metadata di `numpy` 2.3.2: nessun compilatore disponibile (`Unknown compiler(s)`: `cl`, `gcc`, `clang` e gli altri non trovati). La CI del repository usa Python 3.14 e le fixture HA: è la sede corretta per questi due file. Non sono state modificate dipendenze, lockfile o requisiti per aggirare il problema.
- Nessuna esecuzione con xdist: il sottoinsieme richiesto è stato eseguito in singolo processo, come indicato per l'instabilità nota di `test_central_boiler.py`.
- Nessuna verifica su impianto reale, entità, servizi, MQTT o dashboard: fuori ambito M1 e non autorizzata.

### Anomalie della baseline preservate (da valutare in review)

Queste non sono correzioni di M1: il motore le riproduce intenzionalmente. Sono elencate perché la migrazione non le ha corrette in silenzio.

1. Il percorso normale ignora `pilot_ok`: con pilota ON pronto, può avviare anche se tutte le stanze qualificate hanno veto pilota. Il percorso singolo invece applica il veto.
2. Il percorso singolo non richiede il consenso nativo (`consent`), quindi può avviare senza consenso dopo la stabilità di 900 s.
3. Il percorso manuale ignora pausa OFF e consenso nativo; resta limitato da validità degli ingressi e disponibilità del master.
4. Con master HEAT, `r` viene azzerato se il percorso normale non è applicabile, anche se il riscaldamento è già in mantenimento.
5. Le ancore vengono troncate a secondi interi con `|int`; un'ancora frazionaria può anticipare di quasi un secondo la scadenza.
6. `pilot_rooms` nel template è codice morto: il motore non lo espone.
7. Master HEAT con `ok = false` produce `off / no_valid_radiator`, non `data_or_central`: la baseline privilegia l'assenza di valvole valide.
8. La fixture `master_unknown` non è riproducibile dal template corrente (vedi sopra).

### Punti per la review M2

- Definire il contratto del collector: arrotondamento del gap a tre decimali, conteggio delle **valvole fisiche uniche** e coerenza con gli ID, qualificazione delle stanze, freschezza e provenienza dei dati. Il motore si fida dello snapshot per definizione.
- Decidere se le anomalie 1–4 sono da conservare nella migrazione o da correggere con una modifica esplicita e testata, separata da M1.
- Fissare lo schema definitivo delle condizioni diagnostiche (nomi stabili, unità, `unknown` vs `na`) e la loro serializzazione JSON; in M1 sono solo una proposta informativa.
- Persistenza: usare storage/restore HA supportato e versionato; nessuna scrittura diretta a `.storage`. Il ciclo è già serializzabile in `m/off/r/s`.
- Chiarire il tipo di `deadline` nel contratto: il motore restituisce `float`, le fixture salvano `int`; i confronti numerici coincidono, ma la serializzazione dovrà scegliere un tipo stabile.
- Correggere o rigenerare la fixture `master_unknown` in sede di review, senza toccare il motore.
- `evaluated_at` è l'istante di valutazione in secondi Unix; le ancore del ciclo sono intere. La diagnostica M2 dovrà dichiarare esplicitamente questa distinzione e non usare `evaluated_at` come prova di comando o conferma.
