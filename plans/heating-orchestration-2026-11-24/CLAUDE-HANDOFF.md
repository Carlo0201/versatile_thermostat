# Handoff precedente Claude — M2-A

**Mandato aggiornato di Carlo: presa in carico completa, anche coordinamento, review, Oikos e impianto. Leggi ora `CLAUDE-TAKEOVER.md` e `CLAUDE-STATUS.md`.** Le limitazioni organizzative sotto (“solo M2-A”, “Codex gestisce review”, “nessuna attività impianto”) sono superate dal nuovo mandato; rimangono lo storico dell'incarico precedente, non gate correnti.

M1 e la revisione R1/R2/E1 sono approvati da Codex. Il nuovo incarico è **M2-A: collector e sola osservazione della caldaia**.

Leggi `M1-CONFERMA-CLAUDE.md` e implementa `M2-A-CLAUDE.md`, oltre alle istruzioni applicabili del progetto e del fork. Il piano generale e i contratti rimangono in `README.md`, `SPECIFICA.md` e `DIAGNOSTICA.md`.

## Ruoli

- Claude: codice, test e documentazione tecnica locale del fork.
- Codex: coordinamento, review, Oikos e successiva attività sull'impianto con Carlo.

M2 è diviso in **M2-A (osservazione)** e **M2-B (comandi e ownership)**. Questa consegna autorizza solo M2-A. Non iniziare M2-B o PDC.

## Stato verificato

- Branch `codex/avatto-trv-pilot`, HEAD `6a819dcc1437fb33ddf8bf0dc2e3aa09e0c80666`.
- Motore M1 approvato: `custom_components/versatile_thermostat/heating_boiler_policy.py`, SHA-256 `edf7e9007ac44166bfc56a3ad8c74dd6b167af6f4ed2760831d865443c27945b`.
- Test: `tests/test_heating_boiler_policy.py`, SHA-256 `43764243ad73e53740655b6690d0d74aaf071ba08d3dcff19d30d2e0d9c57708`.
- Review indipendente: 143 pass, 1 skip senza ambiente HA completo; 27 casi e 5.000 aggiuntivi in parità con Jinja. R1, R2 ed E1 sono chiusi.
- Codice e test restano non tracciati: ricontrolla Git e preserva questi file e ogni modifica successiva. Nessun commit, push o installazione effettuati.
- Primo handoff M1 preservato in `archive/CLAUDE-HANDOFF-M1.md`; incarichi OPENCODE, prima review ed esiti restano storico, non task ancora da eseguire.

## Consegna

Prepara `M2-A-ESITO.md` con file, schema configurazione/diagnostico, test eseguiti e limiti. La funzione nuova deve restare disabled per default; observe pubblica soltanto proposte, senza comandi agli attuatori o helper legacy. Installazione, configurazione live e passaggio del controllo rimangono fuori dal tuo incarico.

Nessun accesso HA, deploy, riavvio, modifica Oikos, script d'installazione, commit o push. Dopo M2-A consegna a Codex per la review.
