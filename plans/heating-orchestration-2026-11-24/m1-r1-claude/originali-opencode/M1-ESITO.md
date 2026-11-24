# M1 — Esito: motore decisionale caldaia

Stato: **M1 completato sul codice locale del fork**. Il motore **non è collegato** al runtime VTherm e **l'impianto non è stato modificato**. Nessun commit, push, tag, release o PR; nessuna modifica a YAML live, `.storage`, helper, entità, dashboard Oikos o fixture originali.

Baseline verificata all'inizio del lavoro: branch `codex/avatto-trv-pilot`, HEAD `6a819dcc1437fb33ddf8bf0dc2e3aa09e0c80666`, coerente con l'incarico. Al termine il branch e l'HEAD sono invariati; l'albero contiene solo file nuovi non tracciati (motore, test, cartella `plans/` già presente).

## File creati

| File | Contenuto | SHA-256 |
| --- | --- | --- |
| `custom_components/versatile_thermostat/heating_boiler_policy.py` | Motore puro, modelli tipizzati, parsing del contesto, validazione, condizioni diagnostiche | `25B47E7FF87F865B4C0523937A2DCD193E3C7E370AD779E7A3CC77FBF8CD2A35` |
| `tests/test_heating_boiler_policy.py` | Parità fixture, matrice M1, sequenze, restore, immutabilità, validazione, purezza | `2E8AC52101D445A0BDAA1130B81E4DF64853F226E38AFA41FB8EC774B88A9FE3` |

Nessun file tracciato è stato modificato. Nessun file esistente del repository è stato adattato per far passare i test.

## Scelte tecniche

### Fonte normativa

La fonte usata è il **template `plan` attualmente presente** in `baseline/caldaia_controllo_stabile_delle_richieste.json`, non una sua ricostruzione. Il collector `raw` resta fuori da M1: il motore riceve uno snapshot già normalizzato. Di conseguenza appartengono al futuro collector (M2) qualificazione delle stanze, conteggio delle valvole fisiche, freschezza, finestre, inibizioni, PDC e disponibilità della centrale.

### Modulo puro

`heating_boiler_policy.py` non importa Home Assistant, non fa I/O, non legge clock globale, non usa asyncio e non chiama servizi. Il tempo è sempre un parametro esplicito (`now`, secondi Unix assoluti). Il test `test_engine_has_no_home_assistant_or_io_imports` verifica l'assenza di import vietati.

API pubblica:

- `evaluate_boiler(snapshot: BoilerSnapshot, cycle: BoilerCycle | None, now: float, config: BoilerPolicyConfig | None = None) -> BoilerDecision`
- `parse_cycle_context(value: Any) -> BoilerCycle`
- modelli frozen con `slots`: `BoilerPolicyConfig`, `QualifiedBoilerRoom`, `BoilerSnapshot`, `BoilerSingleWait`, `BoilerCycle`, `BoilerCondition`, `BoilerDecision`
- costanti per azioni, motivi, stati delle condizioni e default.

### Parametri

Default identici alla baseline: gap `0.3 °C`, due valvole ordinarie, stabilità normale `120 s`, attesa singola `900 s`, pausa OFF `600 s`. `BoilerPolicyConfig` valida tipi, finitezza e minimi; `normal_valve_count` deve essere un intero `>= 2`, perché il percorso normale è definito da almeno due valvole. Le associazioni stanza/valvola non sono nel motore.

### Semantica preservata

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

### Robustezza del contesto

`parse_cycle_context` accetta mapping, stringhe numeriche (come `float(none)` di Jinja), contesti mancanti/non mapping/troncati e campi invalidi. Un'ancora non finita o non interpretabile diventa `None` invece di propagare `NaN`/`Infinity`: è l'unico irrigidimento rispetto alla lettera del template, motivato dal contratto diagnostico che vieta valori non JSON-safe. Non cambia alcun caso baseline valido.

### Contratto diagnostico

`BoilerDecision` include `evaluated_at` e una tupla `conditions` con `id`, `status` (`pass`/`fail`/`wait`/`unknown`/`na`), `reason_code`, `actual`, `expected` e `deadline` facoltativa. Le condizioni sono **informative**: la politica non le legge e non ne dipende. Sono una proposta per M2, non nuove entità né sensori.

## Parità con la baseline

- Fixture caricate per nome da `baseline/boiler-decision-cases.json`: 27 casi e 27 esiti.
- Confronto completo di `action`, `reason`, `deadline` e ciclo successivo (`m`, `off`, `r`, `s`), non della sola azione.
- **26 casi su 27** coincidono integralmente con l'esito salvato.
- **1 discrepanza nella fixture**, non nel motore: `master_unknown`. L'input salvato ha `cycle = {"m":"off","off":1000,"r":1800,...}` e `raw.master = "unavailable"`. Il template corrente e il motore restituiscono `cycle.r = null`; l'esito salvato riporta `r = 1800`. Tutti gli altri campi coincidono. La fixture non è stata modificata; il caso è escluso dal confronto integrale e coperto da `test_master_unknown_saved_result_disagrees_with_the_baseline_template`, che documenta la divergenza. Va corretto in sede di review, non nel codice M1.

Verifica aggiuntiva eseguita con un harness Jinja2 locale (non versionato): il motore è stato confrontato con il template `plan` su tutti i 27 casi e su **20.000 casi casuali** (master, conteggi, stanze, gap, ancore, consensi, manuale, pilota): **27/27 e 20.000/20.000** con uguaglianza esatta di azione, motivo, deadline e ciclo successivo.

## Copertura della matrice M1

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

## Comandi e risultati

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

## Controlli non eseguiti

- `tests/test_central_boiler.py` e `tests/test_avatto_room_control.py` **non sono stati eseguiti**. Richiedono l'ambiente di test Home Assistant (`requirements_test.txt`). Il tentativo di installazione isolata di `homeassistant==2026.10.4` + `pytest-homeassistant-custom-component` su Python 3.15.2 fallisce in fase di metadata di `numpy` 2.3.2: nessun compilatore disponibile (`Unknown compiler(s)`: `cl`, `gcc`, `clang` e gli altri non trovati). La CI del repository usa Python 3.14 e le fixture HA: è la sede corretta per questi due file. Non sono state modificate dipendenze, lockfile o requisiti per aggirare il problema.
- Nessuna esecuzione con xdist: il sottoinsieme richiesto è stato eseguito in singolo processo, come indicato per l'instabilità nota di `test_central_boiler.py`.
- Nessuna verifica su impianto reale, entità, servizi, MQTT o dashboard: fuori ambito M1 e non autorizzata.

## Anomalie della baseline preservate (da valutare in review)

Queste non sono correzioni di M1: il motore le riproduce intenzionalmente. Sono elencate perché la migrazione non le ha corrette in silenzio.

1. Il percorso normale ignora `pilot_ok`: con pilota ON pronto, può avviare anche se tutte le stanze qualificate hanno veto pilota. Il percorso singolo invece applica il veto.
2. Il percorso singolo non richiede il consenso nativo (`consent`), quindi può avviare senza consenso dopo la stabilità di 900 s.
3. Il percorso manuale ignora pausa OFF e consenso nativo; resta limitato da validità degli ingressi e disponibilità del master.
4. Con master HEAT, `r` viene azzerato se il percorso normale non è applicabile, anche se il riscaldamento è già in mantenimento.
5. Le ancore vengono troncate a secondi interi con `|int`; un'ancora frazionaria può anticipare di quasi un secondo la scadenza.
6. `pilot_rooms` nel template è codice morto: il motore non lo espone.
7. Master HEAT con `ok = false` produce `off / no_valid_radiator`, non `data_or_central`: la baseline privilegia l'assenza di valvole valide.
8. La fixture `master_unknown` non è riproducibile dal template corrente (vedi sopra).

## Punti per la review M2

- Definire il contratto del collector: arrotondamento del gap a tre decimali, conteggio delle **valvole fisiche uniche** e coerenza con gli ID, qualificazione delle stanze, freschezza e provenienza dei dati. Il motore si fida dello snapshot per definizione.
- Decidere se le anomalie 1–4 sono da conservare nella migrazione o da correggere con una modifica esplicita e testata, separata da M1.
- Fissare lo schema definitivo delle condizioni diagnostiche (nomi stabili, unità, `unknown` vs `na`) e la loro serializzazione JSON; in M1 sono solo una proposta informativa.
- Persistenza: usare storage/restore HA supportato e versionato; nessuna scrittura diretta a `.storage`. Il ciclo è già serializzabile in `m/off/r/s`.
- Chiarire il tipo di `deadline` nel contratto: il motore restituisce `float`, le fixture salvano `int`; i confronti numerici coincidono, ma la serializzazione dovrà scegliere un tipo stabile.
- Correggere o rigenerare la fixture `master_unknown` in sede di review, senza toccare il motore.
- `evaluated_at` è l'istante di valutazione in secondi Unix; le ancore del ciclo sono intere. La diagnostica M2 dovrà dichiarare esplicitamente questa distinzione e non usare `evaluated_at` come prova di comando o conferma.
