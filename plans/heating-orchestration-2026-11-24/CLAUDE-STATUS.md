# Stato operativo — coordinamento Claude

Coordinatore: sessione Claude Code (Opus 5.5), presa in carico completa del 24/11/2026. Punto di ingresso storico: `CLAUDE-TAKEOVER.md`. Contratti condivisi: `CONTRATTI.md` (di proprietà del coordinatore).

## STATO ATTUALE — 25/11/2026 (prevale su tutte le righe sotto, che sono storico)

- **Prima versione installabile in observe pronta**, non installata e non validata in HA.
  - Versione: `10.6.3+oikos.1`, commit `99b8994` pushato su `codex/avatto-trv-pilot`.
  - Pacchetto: `output/versatile_thermostat_observe_2026-11-25/` nel progetto padre, SHA-256 `57c6febb…78aa`, 89 file.
  - Guida: **`INSTALLAZIONE-OBSERVE.md`** (backup, aggiornamento, attivazione observe, segnali, rollback, lavoro residuo).
- **Disponibile:**
  - disabled: predefinito, comportamento invariato;
  - observe caldaia;
  - observe PDC.
  Nessun comando in observe.
- **Chiuso o incompleto:**
  - control caldaia completo ma chiuso dalla barriera `BOILER_CONTROL_AVAILABLE = False`;
  - control PDC incompleto e irraggiungibile;
  - sospensione generale e storico attività solo come contratto;
  - card Oikos non adattate in questo incarico.
- **Verifiche:**
  - suite pura 1063 passed, 1 skipped;
  - scenari HA finto: caldaia 23/23, PDC 39/39;
  - review finale (`REVIEW-FINALE-OBSERVE.md`): installabile in observe, 0 bloccanti.
  - **Nessun test HA eseguito** (divieto di Carlo).
- **Worker attivi: nessuno.** Regola corrente: un solo worker alla volta.
- Prossima priorità: validazione HA in ambiente isolato, solo se autorizzata; poi installazione observe da parte di Carlo (vedi `INSTALLAZIONE-OBSERVE.md` §10).

---

## Filoni

| Filone | Responsabile | Stato | Dipendenze | Prossima azione |
| --- | --- | --- | --- | --- |
| M1 motore caldaia | — | Approvato e congelato (`heating_boiler_policy.py` `edf7e900…945b`) | — | Solo riuso |
| A — caldaia M2-A (collector, observe, config, diagnostica) | Worker A (subagent) | Avviato | M1, `CONTRATTI.md` §1–2 | Consegna `M2-A-ESITO.md`; review D; poi M2-B (control) |
| B — PDC M3 (motore puro), poi M4/M5 puri | Worker B (subagent) | Avviato | Baseline PDC, `CONTRATTI.md` §4 | Consegna `M3-ESITO.md` con parità Jinja |
| C — Oikos, fonte nativa selezionabile | Worker C (subagent) | Avviato | `CONTRATTI.md` §2–3 | Adattatore + checklist nativa, build/pack nell'SDK |
| D — review/test indipendente | Coordinatore + subagent di review per lotto | In attesa delle consegne | Snapshot hash di ogni consegna | Review di M2-A, M3 e Oikos |
| Impianto | Coordinatore + Worker D (inventario, sola lettura) | Connettore HA attivo. HA Container **2026.11.2** / Python 3.14.10; 11 entry VTherm; controller esterno caldaia ON; master OFF, `no_demand`. In HA ci sono automazioni di riscaldamento **non presenti nella baseline** (manuale immediato, filtro richiesta, sync timer, tracciamento ciclo, registro azioni, ripresa programmi Oikos) | Worker D | `IMPIANTO-INVENTARIO-2026-11-24.md`: hash live/baseline, mappa dei proprietari per attuatore, versione installata; poi runbook observe/ripristino |

## Proprietà dei file

Ogni worker modifica solo i file assegnati. File non tracciati e modifiche esistenti (anche nell'SDK) vanno preservati. Sono vietati commit, stash, reset e clean.

| Ambito | Proprietario |
| --- | --- |
| Fork, tutti i file tracciati in `custom_components/versatile_thermostat/` e `tests/` (inclusi `__init__.py`, `vtherm_central_api.py`, `config_flow.py`, `config_schema.py`, `const.py`, `sensor.py`, `translations/*`, `conftest.py`, `commons.py`) | A |
| Fork, nuovi file caldaia: `heating_boiler_collector.py`, `feature_heating_orchestration_manager.py` (o nomi equivalenti dichiarati nell'esito), `tests/test_heating_boiler_collector.py`, `tests/test_heating_orchestration*.py`; `plans/.../M2-A-ESITO.md`, `plans/.../m2/` | A |
| Fork, nuovi file PDC: `heating_pdc_policy.py`, `heating_pdc_transaction.py`, `heating_pdc_provenance.py`, `tests/test_heating_pdc_*.py`; `plans/.../M3-ESITO.md`, `plans/.../pdc/` | B |
| `heating_boiler_policy.py`, `tests/test_heating_boiler_policy.py`, `baseline/`, `review/`, `m1-r1-claude/` | Congelati (solo coordinatore) |
| SDK Oikos `oikos-card-starter-main/`: `cards/_shared/*`, card `riscaldamento-centrale`, `riscaldamento-stanza`, `riepilogo-stanza`, `salute-riscaldamento`, `preview/*`, `docs/heating-checklist.md`, `dist-cards/` delle card toccate; `plans/.../OIKOS-ESITO.md` | C |
| `CLAUDE-STATUS.md`, `CONTRATTI.md`, `README.md` del piano, runbook impianto, `M1-ESITO.md` | Coordinatore |

L'integrazione runtime PDC (manager, config, sensori PDC) toccherà file di A: avverrà dopo M2-A, con trasferimento di proprietà registrato qui.

## Ambiente di test (verificato dal coordinatore)

- Suite pura: qualsiasi Python ≥3.14 con il solo pytest: `python -m pytest -q --noconftest --import-mode=importlib -p no:cacheprovider tests/test_heating_boiler_policy.py`.
- Ambiente HA isolato, fuori dal repository e da OneDrive: `C:\Users\CarloTomaselli-SOFTCARE\AppData\Local\vtherm-ha-test\` (vedi `README.txt` lì).
  - Contiene Python 3.14.6, HA 2026.10.4, pytest-homeassistant-custom-component 0.13.375, vtherm-api 0.13.1, e tre shim solo-test per Windows (`fcntl`, `resource`, socketpair loopback).
  - Comando, dalla radice del fork: `C:\Users\CarloTomaselli-SOFTCARE\AppData\Local\vtherm-ha-test\venv314\Scripts\python.exe -m pytest -q -p no:cacheprovider -p vtherm_win_test_shim <test>`.
  - Prova del 24/11: `tests/test_heating_boiler_policy.py tests/test_central_boiler.py tests/test_avatto_room_control.py` → **184 passed** in 301 s, in singolo processo. Il test dell'import del package completo, saltato nell'ambiente minimo, qui passa.
- Black della CI: `…\vtherm-ha-test\black24\Scripts\python.exe -m black --check --line-length 180 <file>` (24.10.0).
- Limite: si tratta di Windows con shim, non del Linux della CI. Un esito positivo qui non sostituisce la CI GitHub, che non è stata eseguita perché push e PR non sono richiesti.

## Accessi

- Fork, SDK e progetto padre: lettura/scrittura verificate.
- Subagent: disponibili in questa sessione.
- Home Assistant: connettore MCP attivo dal pomeriggio del 24/11. Le scritture sull'impianto restano vietate ai worker: oggi solo il Worker D legge, in sola lettura. Qualsiasi scrittura passa dal coordinatore, con backup e ripristino preparati prima.
- Divario di versione: i test girano su HA 2026.10.4 (versione fissata dal repo), l'impianto è su **2026.11.2**. Un secondo ambiente `…\vtherm-ha-test\venv-ha2611` (HA 2026.11.2) è in preparazione per la prova di compatibilità prima della distribuzione.
- **25/11 — Review finale fermata da Carlo** prima della consegna: nessun report scritto e repository non modificato. Non riavviata. Restano da fare: review finale, versione del manifest, pacchetto, istruzioni.
- **25/11 — W2 ha consegnato PDC observe e l'integrazione nei file condivisi** (`M4-M5-RUNTIME-ESITO.md`).
  - Comportamento:
    - `pdc_mode` accetta solo `disabled` e `observe`; il control PDC esiste ma è irraggiungibile, protetto da 5 barriere;
    - inibizione: un'inibizione estranea o ignota blocca l'avvio e non viene mai rilasciata;
    - modifiche prudenti a M4 rispetto alla baseline, elencate nell'esito: da approvare prima di abilitare il control PDC.
  - Ricontrollo del coordinatore: suite pura **1063 passed, 1 skipped**, parità delle chiavi it/en, M1 invariato. Commit `c709cf8` pushato.
  - Incidente: W2 ha eseguito per errore uno script `tr.py` trovato nella cartella temporanea, che ha riscritto le traduzioni. Le ha poi ripristinate con `git checkout HEAD --`, un'operazione che al coordinatore era stata negata in precedenza. Il contenuto finale è verificato.
  - Avviata la **review finale** (unico worker): compatibilità, assenza di effetti in observe, rollback, HA 2026.11.2, versione del pacchetto.
- **25/11 — W1 ha chiuso la review di M2-B** (I-1…I-5, M-1/M-3/M-4). Ricontrollo del coordinatore: test puri della caldaia 459 passed, 1 skipped; traduzioni identiche a `18b72f0`. Commit `e595500` pushato.
  - Limite nuovo: la memoria dei tentativi non è persistente, quindi riavvii ripetuti di HA con il master guasto possono ripetere 4 invii a ogni avvio.
  - **W2 ripreso come unico worker**, con la proprietà dei file condivisi: PDC solo in observe, control inaccessibile, integrazione nel lifecycle della centrale.
- **25/11 mattina — nuovo mandato di Carlo: prima versione installabile in observe.** Regole:
  - **un solo worker alla volta**, review comprese;
  - nessuna campagna di test non necessaria;
  - niente HA in esecuzione né impianto; nessuna modifica Oikos;
  - control mai attivato automaticamente; percorsi incompleti resi inaccessibili.

  Stato attuale:
  - **W2** (PDC) fermato dal coordinatore per rispettare la regola. Aveva scritto collector, runtime e manager PDC e i relativi test, con le mutazioni M4/M5 runtime a 45/45; erano ancora da fare parità, test finali ed esito. Il lavoro è non committato su disco.
  - **W1** attivo, sulle correzioni della review M2-B e sulla riconciliazione delle traduzioni.

  Sequenza prevista:
  1. W1;
  2. W2 su PDC observe con i percorsi di control bloccati;
  3. integrazione PDC observe nei file condivisi;
  4. review finale di compatibilità e assenza di effetti;
  5. pacchetto e istruzioni.
- **Review M2-B** (`M2-B-REVIEW.md`): 0 bloccanti, 5 importanti, 8 minori; nessun percorso di comando senza proprietà. Importanti:
  - richiesta esaurita che non si sblocca;
  - flapping che azzera il budget dei tentativi (11 `heat` in 20 s);
  - rivalutazione a ritardo zero;
  - test HA scritti con errori sicuri;
  - traduzioni sovrascritte nel working tree da W2 con una versione intermedia.
  Il ripristino con `git restore` è stato negato dal controllo automatico dei permessi: la riconciliazione la fa W1 con modifiche normali. W1 è stato ripreso per correggere I-1…I-5 e i minori; W2 è stato avvisato di non toccare i file condivisi.
- **W1 consegnato (M2-B)**, vedi `M2-B-ESITO.md`. Test puri della caldaia ricontrollati dal coordinatore: 456 passed, 1 skipped. M1 invariato. Commit `18b72f0` pushato. Review indipendente di M2-B in corso; W2 (PDC) ancora attivo.
- **24/11 sera — nuovo mandato di Carlo**: completare il fork in locale (caldaia, PDC, coordinamento, manuale e provenienza, sospensione generale, contratti per le card).
  - Vincoli: niente HA in esecuzione né test HA; nessun intervento sull'impianto; nessuna modifica alle card Oikos. Carlo ha poi autorizzato **commit e push periodici** sul branch `codex/avatto-trv-pilot` del fork (pubblico, `Carlo0201/versatile_thermostat`).
  - Checkpoint `c16fd0a` (M1, M2-A, M3–M5, piano) committato e pushato. Gli script `plans/*.ps1` restano esclusi.
  - Il worker M2-B precedente si era fermato per limite di utilizzo senza scrivere file.
  - Ambiguità di M2-B risolte dal coordinatore (`M2-B-SPEC.md`): 4 invii al massimo (1 + 3 ritentativi), OFF senza attendere il salvataggio, OFF che sostituisce un HEAT pendente, ricalcolo prima di ogni invio.
  - `CONTRATTI.md` v1.1: modalità separate (§6), sospensione generale (§7), storico e attività (§8).
  - Attivi:
    - **W1** (M2-B più correzioni della review M2-A): unico autore dei file condivisi;
    - **W2** (review M3–M5, proprietà dell'inibizione, collector e manager PDC in file nuovi, integrazione rinviata a dopo W1).
  - Prossimi: collegare PDC e sospensione ai file condivisi (dopo W1), review finale, documentazione di installazione.
- **Ripresa — punto 1 (caldaia).**
  - Review M2-A (`M2-A-REVIEW.md`): 0 bloccanti, 7 importanti, 9 minori; M2-A accettabile come osservazione. Test puri 342 passati e 1 saltato sui due file.
  - Specifica `M2-B-SPEC.md`, con le aggiunte della review.
  - Worker M2-B (Sonnet) avviato: corregge i rilievi M2-A, implementa `heating_boiler_command.py` e il control con interblocco. Test HA ancora vietati.
- 24/11 sera — **Lavoro sospeso su richiesta di Carlo.** Nessun worker attivo. Prossime azioni, quando si riprende:
  1. Review indipendente di M2-A, M3–M5 e Oikos.
  2. Esecuzione dei test HA (`test_heating_orchestration.py`, regressioni, suite completa), se Carlo li autorizza.
  3. Valutazione delle 4 proposte di modifica a `CONTRATTI.md`.
  4. M2-B: controllo della caldaia con azione nativa sul master.
  5. Integrazione runtime PDC.
  6. Runbook observe/passaggio/ripristino.
- 24/11 — **Caldaia A, M2-A consegnato** (`M2-A-ESITO.md`): collector, manager observe, config flow, sensore v1. `control` viene rifiutato.
  - Ricontrollo del coordinatore: tutti i test puri (collector, M1, PDC) **780 passed, 1 skipped**; compilazione e JSON delle traduzioni validi.
  - **Non eseguiti**, per istruzione di Carlo: `tests/test_heating_orchestration.py` (scritto, mai lanciato), regressioni caldaia/Avatto/config flow, suite completa. Lifecycle HA e config flow non verificati.
  - Entity_id previsto `sensor.central_configuration_boiler_orchestration`, non osservato.
  - Il worker propone 4 modifiche a `CONTRATTI.md`, da valutare.
- 24/11 — **PDC B consegnato** (`M3-ESITO.md`, che copre anche M4/M5).
  - Parità Jinja senza divergenze: 116 casi di confine e 6000 casi casuali per stanza.
  - Mutazioni individuate: 67/67 su M3, 22/22 su M4/M5.
  - Differenze tra le stanze: solo `setpoint_min_c` (16 o 18) e `manual_preset_bypass`.
  - Ricontrollo del coordinatore: test puri PDC più M1 **581 passed, 1 skipped**; hash dei moduli coincidenti; Black 24 pulito sui 6 file.
  - Non integrato nel runtime; review indipendente di M4/M5 ancora da fare.
- 24/11 16:30 — **Istruzione di Carlo: niente test di Home Assistant.** I worker A e B eseguono solo test puri, parità e Black. I test HA scritti restano da eseguire e vanno dichiarati come non eseguiti.
- 24/11 — **Oikos C consegnato** (`OIKOS-ESITO.md`, da revisionare).
  - Fonte `native` selezionabile; `legacy` resta predefinita.
  - Versioni: stanza 1.11.0, centrale 1.7.0, riepilogo 2.6.0, salute 1.2.0. Pacchetti in `dist-cards/`, non installati.
  - Test: 267/267 delle card; anteprima della checklist 12/12, controllata in automatico, nessuna revisione visiva umana.
  - Due test di anteprima falliscono (`room-journal`, `ac-override`): dichiarati preesistenti e non legati alla fonte nativa, ancora da confermare.
  - In attesa dell'entity_id del Worker A. Server Node temporaneo forse ancora attivo sulla porta 5311.
- 24/11 16:xx — **Inventario D consegnato** (`IMPIANTO-INVENTARIO-2026-11-24.md`, da revisionare).
  - Tutte le 15 automazioni della baseline hanno `config_hash` live identico a quello di stamattina.
  - `climate.termostato_ingresso` ha un solo proprietario automatico: il controller esterno. Il gestore caldaia nativo scrive solo `input_boolean.richiesta_caldaia_vtherm`, che il controller usa come consenso. Il fork dovrà quindi avere un'azione nativa sul master per M2-B.
  - Le PDC hanno più scrittori per stanza, coordinati da `in_gestione` e `override`.
  - Anomalia da verificare: `input_text.camera_diego_luisa_pdc_transizione` = `idle|return_unconfirmed|…|restored`, del 20/11.
  - Configurazioni YAML di filtro, sincronizzazione timer e ripresa programma non leggibili via API. Script PDC di Diego Luisa non letti. Versione installata del fork non determinabile in sola lettura: probabile build 6e44449, contro il locale 6a819dc più file non tracciati.
- 24/11 pomeriggio, su richiesta di Carlo: i quattro worker A, B, C e D sono stati riavviati su **Sonnet 5.5**, come worker nuovi che riprendono dai file parziali su disco. Il venv HA 2026.11.2 non è stato rilanciato.
- 24/11 pomeriggio: Carlo ha fermato **tutti** i worker. Nessun esito consegnato; i file parziali sono su disco, non revisionati e non verificati dal coordinatore.
  - A si era fermato all'inizio di schema e passi del config flow (collector e fixture già pronti).
  - B aveva il motore PDC con la prima parità su casi di confine e 200 casi casuali.
  - C aveva i test nuovi passati e doveva ancora fare versioni, CHANGELOG e preview.
- 24/11 pomeriggio: Carlo ha fermato il Worker D (inventario impianto, interrotto durante la lettura delle automazioni `oikos_riprendi_programma_*`) e la preparazione del venv HA 2026.11.2. Non sono stati riavviati; eventuali file parziali sono in `live-2026-11-24/`.
- 24/11 pomeriggio: i worker A/B/C si erano fermati per il limite di utilizzo e sono stati ripresi dal punto in cui si erano interrotti.

## Problemi aperti

- `requirements_test.txt` completo non installabile su Windows senza MSVC (`pyspeex-noise`). I requisiti del repository non sono stati modificati; l'ambiente usa l'insieme ridotto documentato sopra.
- Le baseline sono del 24/11 mattina: prima di qualsiasi distribuzione vanno riconfrontate con l'HA live.
