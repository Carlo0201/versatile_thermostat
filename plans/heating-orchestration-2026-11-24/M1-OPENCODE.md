# Incarico OpenCode — M1: motore decisionale caldaia

Lavora nella cartella del fork `repos/versatile_thermostat` del progetto Home Assistant. Carlo ti affida **solo codice, test e documentazione tecnica locale del fork**. Codex coordina specifiche, review e futura migrazione dell'impianto.

Leggi prima `plans/heating-orchestration-2026-11-24/README.md`, `SPECIFICA.md`, `DIAGNOSTICA.md` e le istruzioni applicabili del repository e del progetto padre. Implementa soltanto M1 descritto qui. M2–M6 sono contesto per non chiudere male l'architettura; non fanno parte di questa consegna.

## Obiettivo concreto

Spostare la decisione caldaia oggi contenuta nelle variabili Jinja dell'automazione attiva in un motore Python puro, deterministico e testabile, mantenendo esattamente avvio ordinario, stanza singola, mantenimento, manuale, consensi e ancore temporali. **Non collegare ancora il motore al runtime VTherm.**

## Prima di scrivere

1. Controlla branch, HEAD e stato Git. La baseline osservata è `codex/avatto-trv-pilot` a `6a819dcc1437fb33ddf8bf0dc2e3aa09e0c80666`. Non cambiare branch, resettare o sovrascrivere modifiche esistenti. Se il codice di base è cambiato, descrivi l'impatto e preservalo.
2. Leggi `baseline/index.json` e `baseline/caldaia_controllo_stabile_delle_richieste.json`, in particolare il template `plan` e il collector `raw`. I due blocchi sono separati: in M1 porti il decisore; in M2 si porterà il collector.
3. Leggi `feature_central_boiler_manager.py`, `base_manager.py`, `avatto.py`, i punti rilevanti di `thermostat_climate.py` e i test caldaia/Avatto esistenti. Evita modifiche a questi moduli in M1: il nuovo motore non deve essere importato automaticamente dal runtime.

## Implementazione richiesta

- Nuovo modulo suggerito: `custom_components/versatile_thermostat/heating_boiler_policy.py`. Nome diverso ammesso se coerente e motivato. Niente import Home Assistant, I/O, clock globale, asyncio, letture di helper o chiamate a servizi nel motore.
- Modelli tipizzati per parametri, snapshot normalizzato, stanza qualificata, ciclo e risultato. Valuta una funzione della forma `evaluate_boiler(snapshot, cycle, now, config) -> decision`. Parametri iniziali: gap 0,3 °C, due valvole ordinarie, stabilità 120 s, attesa singola 900 s, pausa OFF 600 s. Le associazioni della casa non appartengono al motore.
- Input clock esplicito; stesso input produce stesso risultato, senza mutare snapshot o contesto. Istanti assoluti ripristinabili con UTC/Unix; conversioni di orologio e scheduler saranno responsabilità M2.
- Preserva `action`, `reason`, `deadline` e ciclo successivo, incluso azzeramento/riavvio delle attese e caso di master ignoto. Un comando proposto non modifica direttamente il master confermato.
- Non trasformare assenza di manuale, consenso o dati in approvazione implicita. Conserva e documenta i default della baseline ove esistono. Segnala separatamente anomalie o miglioramenti desiderabili: la migrazione non autorizza a correggere silenziosamente la politica.
- Considera fin d'ora l'interfaccia del contratto diagnostico, ma non creare nuove entità, helper o sensori in M1.

## Test e parità

- Nuovo file suggerito: `tests/test_heating_boiler_policy.py`.
- Carica i casi/esiti salvati in `baseline/boiler-decision-cases.json`, associandoli per nome. Confronta l'intero risultato atteso disponibile, non soltanto l'azione. Queste fixture derivano da valutazioni eseguite nel motore Jinja HA; non richiamare HA per generare gli expected.
- Aggiungi i casi della matrice M1 di `SPECIFICA.md`, in particolare confini, cambio stanza/target, mantenimento con una valvola, consenso nativo negato dopo la stabilità ordinaria, pilota ON con veto e stanza singola non qualificata, richiesta manuale con dati invalidi e master non disponibile.
- Testa sequenze: OFF iniziale → richiesta → scadenza → HEAT confermato → riduzione a una valvola → zero → OFF confermato → nuovo avvio. Verifica che duplicate valutazioni non spostino le ancore e che la proposta HEAT non faccia partire il tempo ON senza conferma.
- Testa contesto mancante/non mapping, ancore negative/future, null, timestamp limite, immutabilità degli input e parametri non validi. Niente sleep reale e nessun collegamento a dispositivi.
- La CI attuale usa Python 3.14 e `requirements_test.txt` (include `requirements_dev.txt` e fixture HA). Usa ambiente isolato o quello esistente; non aggiornare indiscriminatamente dipendenze, lockfile o requisiti per far passare il lavoro. Se non puoi eseguire un controllo, riportalo con il motivo.
- Esegui i test nuovi e quelli pertinenti: `python -m pytest -q tests/test_heating_boiler_policy.py tests/test_central_boiler.py tests/test_avatto_room_control.py`. Il file caldaia segnala instabilità in parallelo: esegui questo sottoinsieme senza xdist. Formattazione secondo il repository (`black`, riga 180); limita il controllo ai file modificati.

## Confini espliciti

Non usare strumenti HA, SSH, Docker o script PowerShell di distribuzione. Non eseguire `plans/avatto-oikos-pilot.ps1` o `plans/boiler-timers.ps1`. Non scrivere YAML per installazione, non cambiare configurazione live o `.storage`, non installare il componente, non riavviare HA, non toccare le card Oikos. Non modificare pacchetti storici, fixture originali o questa specifica per adattarli al risultato del codice. Non creare commit, push, tag, release o PR.

## Consegna

Aggiorna un file `M1-ESITO.md` in questa cartella con: scelte tecniche, file creati/modificati, tabella dei casi coperti, comandi e risultati dei test, dipendenze o controlli mancanti, discrepanze rilevate, punti utili alla review M2. Riporta chiaramente che il motore non è ancora collegato e l'impianto non è stato modificato. Termina la consegna dopo M1: Codex farà la review prima del lotto successivo.
