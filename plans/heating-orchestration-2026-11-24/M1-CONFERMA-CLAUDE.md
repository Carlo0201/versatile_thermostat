# Conferma Codex della revisione Claude — 24 novembre 2026

**M1 approvato per proseguire con M2-A.** R1, R2 ed E1 sono risolti. Il motore resta scollegato dal runtime e non è stato installato sull'impianto.

## Verifiche indipendenti

- Rieseguito il comando puro senza PYTHONPATH/stub: **143 passed, 1 skipped**, due warning di configurazione pytest-asyncio. Il test saltato riguarda l'import nel package completo HA: le dipendenze di quell'ambiente non sono disponibili.
- `review/verify_m1.py` invariato: **27/27 casi** e **5.000/5.000 casi aggiuntivi** corrispondono al template Jinja corrente per azione, motivo, scadenza e ciclo. Tutti i 17 file originali della baseline conservano il proprio hash.
- Confermati nelle riproduzioni indipendenti: gap e pilota `na` durante il manuale; pilota `na` nel mantenimento. Letti i nuovi test per veto della singola stanza, percorso normale, ingresso isolato e controllo dell'errata.
- Tutti i 27 expected sono nuovamente confrontati integralmente nei test, applicando solo l'errata autorizzata a una copia in memoria e verificando l'hash di origine. `master_unknown` resta l'unica differenza con l'originale storico: è correttamente risolta dall'errata, non cambiando la politica.
- Nessun import del package HA nella suite pura: caricamento del singolo modulo con nome privato, senza sostituzioni dei package globali; prova aggiuntiva in interprete `python -I`.
- Nessuna modifica a file tracciati o all'HEAD; i due file nuovi conservano gli hash dichiarati da Claude: motore `edf7e9007ac44166bfc56a3ad8c74dd6b167af6f4ed2760831d865443c27945b`, test `43764243ad73e53740655b6690d0d74aaf071ba08d3dcff19d30d2e0d9c57708`.

Il JSON della prima review è preservato in `review/M1-verifica-prima-Claude.json`; `review/M1-verifica.json` contiene il ricontrollo dopo le correzioni. Le evidenze di Claude restano separate in `m1-r1-claude/`.

Precisazione documentale: la griglia del test `test_diagnostics_agree_with_the_decision_on_a_deterministic_grid` contiene **23.328** combinazioni (prodotto dei parametri nel codice), non 15.552 come indicato nell'esito di Claude. Il test passa sul prodotto effettivo; il refuso non è un difetto del motore e non blocca M1.

## Limiti

Suite completa HA/Python 3.14, integrazione caldaia/Avatto e prove fisiche non eseguite in questa review. Il motore puro è approvato; questa approvazione non certifica ancora il collector o l'esecuzione dei comandi, che appartengono a M2.

Anche in questa sessione il ricontrollo Black del reviewer non è terminato ed è stato interrotto: la formattazione con Black 24/26 resta verificata da Claude come descritto in `M1-ESITO.md`, non indipendentemente dal reviewer. Questa limitazione non modifica gli esiti dei test e della parità rieseguiti.

## Prossimo incarico

`M2-A-CLAUDE.md`: collector, integrazione disabilitata per default e sola osservazione. M2 viene diviso in M2-A (osservazione) e M2-B (esecuzione comandi/ownership, dopo review). La politica di M1 rimane invariata.
