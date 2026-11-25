# Incarico Claude — migrazione delle logiche di riscaldamento

Carlo ha trasferito a Claude il lavoro di implementazione precedentemente assegnato a OpenCode, per esaurimento del limite di utilizzo. Questo è il punto di ingresso corrente. I file con suffisso OPENCODE restano come storico delle specifiche e delle consegne; non indicano un secondo esecutore attivo.

## Ruoli e stato da cui riprendere

- **Claude:** codice Python, test e documentazione tecnica locale del fork.
- **Codex:** coordinamento, specifiche, review, card Oikos e successive attività sull'impianto con Carlo.
- Directory di lavoro: il repository `repos/versatile_thermostat` del progetto Home Assistant.
- M1 è già implementato da OpenCode ed è stato revisionato da Codex. Riprendi i due file esistenti: non riscrivere M1 da zero.
- Il motore riproduce il template corrente sui 27 casi di baseline e su 5.000 casi aggiuntivi verificati dal reviewer. I 122 test esistenti passano usando lo stub esterno, che è proprio una delle dipendenze da eliminare.
- Restano **R1, R2 ed E1** descritti sotto. L'incarico immediato è chiudere questa revisione di M1, senza iniziare M2.
- Verifica del passaggio: branch `codex/avatto-trv-pilot`, HEAD `6a819dcc1437fb33ddf8bf0dc2e3aa09e0c80666`. I due file hanno ancora gli stessi hash della prima consegna: non risultano correzioni parziali di OpenCode nei file di implementazione/test.

| File | SHA-256 al passaggio |
| --- | --- |
| `custom_components/versatile_thermostat/heating_boiler_policy.py` | `25b47e7ff87f865b4c0523937a2dcd193e3c7e370ad779e7a3cc77fbf8cd2a35` |
| `tests/test_heating_boiler_policy.py` | `2e8ac52101d445a0bdaa1130b81e4df64853f226e38afa41fb8ec774b88a9fe3` |

Ricontrolla Git e contenuti prima di scrivere: eventuali cambiamenti successivi vanno preservati. I file nuovi sono non tracciati; non cancellarli, non fare reset/clean. I due script PowerShell già presenti in `plans/` non appartengono all'incarico e non vanno eseguiti.

## Letture

Leggi le istruzioni applicabili del progetto e del repository, quindi, in questa cartella:

1. `README.md`, `SPECIFICA.md`, `DIAGNOSTICA.md` per piano e comportamenti da conservare.
2. `M1-ESITO.md` per la consegna precedente.
3. `M1-REVIEW.md` per le evidenze indipendenti e le correzioni richieste.
4. `baseline/boiler-decision-errata.json`, il relativo originale e `baseline/index.json`.

Per i vincoli originari di M1 puoi consultare `M1-OPENCODE.md`: si applicano anche a Claude. Questo handoff sostituisce soltanto l'assegnazione dell'esecutore e indica la revisione corrente.

## Lavoro da completare ora

### R1 — diagnostica coerente con il percorso

Determina il percorso prima di assegnare gli stati delle condizioni. Nell'avvio manuale, gap, pilota, stabilità, cooldown e consenso automatici sono `na`. Nel mantenimento pilota e requisiti di avvio sono `na`. Restano applicabili ingressi necessari e conteggio minimo di mantenimento.

Rendi diagnosticabile il veto `pilot_ok` della stanza singola, distinto dal consenso globale `pilot_ready`. Non introdurre quel veto nel percorso normale: la baseline non lo richiede.

Aggiungi regressioni per manuale con gap insufficiente e pilota negato/ignoto, mantenimento con pilota negato/ignoto, singolo con pilota pronto ma qualificazione stanza negata, normale senza qualificazione pilota della stanza. Verifica azione/motivo e condizioni diagnostiche. **Non cambiare la politica decisionale per adeguarla alla diagnostica.**

### R2 — suite pura portabile

Elimina la dipendenza da package stub personali sotto Temp. Il comando `python -m pytest -q --noconftest --import-mode=importlib tests/test_heating_boiler_policy.py` deve funzionare nell'ambiente minimo con pytest, senza HA e senza PYTHONPATH speciale.

Carica il singolo modulo puro in modo isolato oppure fornisci un runner mantenuto nel repository. Non modificare l'`__init__.py` del componente e non contaminare gli altri test HA con sostituzioni globali dei package. Conserva il supporto all'esecuzione nell'ambiente completo del repository.

La CI usa Python 3.14. L'ambiente minimo della consegna precedente usa Python 3.15.2 e non ha le dipendenze complete HA: non presentare i test di integrazione come eseguiti. Non aggiornare indiscriminatamente i requisiti per aggirare la limitazione.

### E1 — rettifica autorizzata degli expected

Usa la sola errata autorizzata in `baseline/boiler-decision-errata.json`. Verifica hash e valore originale; applicala a una copia in memoria degli expected. Non modificare le fixture originali. Elimina l'esclusione di `master_unknown`: tutti i 27 risultati devono essere confrontati integralmente. Mantieni una spiegazione verificata dell'errata.

### Verifiche e consegna

Riesegui test puri e formattazione, conserva la parità di azione, motivo, scadenza e contesto. Puoi eseguire `review/verify_m1.py` con Jinja2, senza adattarlo per mascherare divergenze. I risultati sotto `review/` sono del reviewer: nella consegna registra separatamente le verifiche di Claude.

Black non è terminato nella sessione di review anche con un worker: se il problema si ripete, riportalo e non dichiarare un controllo passato. Nessuno sleep reale per simulare attese di minuti.

Aggiorna `M1-ESITO.md` aggiungendo la revisione di Claude e preservando la provenienza della prima consegna OpenCode: R1/R2/E1, file cambiati e hash, comandi esatti, risultati e controlli non eseguiti. Consegna a Codex per la review.

## Lotti successivi trasferiti a Claude

Claude subentra anche come esecutore dei futuri M2–M6 del piano: integrazione caldaia, decisioni PDC, transazioni radiatore/PDC, override/provenienza e consolidamento. Saranno assegnati uno alla volta da Codex dopo la review del lotto precedente. Questa consegna non autorizza a iniziarli tutti insieme.

## Confini

Solo codice, test e documentazione locale del fork. Nessun accesso all'impianto HA, deploy, riavvio, modifica a Oikos o configurazioni live, scrittura diretta a `.storage`, script di installazione, commit o push. Non cambiare soglie, parametri o politica della baseline. Nessuna ricreazione del lavoro già completato.
