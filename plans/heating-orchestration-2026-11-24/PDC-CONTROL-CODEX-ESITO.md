# Completamento della consegna PDC di OpenCode

Data: 25 novembre 2026, Europe/Rome. Codex subentra su richiesta di Carlo dopo il limite di utilizzo di OpenCode. **Solo codice, test e documentazione locali; nessuna modifica all'impianto, installazione, commit o push.** Manifest ancora `10.6.3+oikos.1`; nessun pacchetto control generato.

## Punto di partenza e preservazione del lavoro

OpenCode aveva completato i task 1 e 2 nel proprio report e lasciato modifiche parziali ai task 3 e 4: flow/schema, parser, traduzioni, manager, runtime e test. Sono stati letti e preservati quei diff, i due script storici non tracciati e le fixture originali. Non sono stati cambiati branch o HEAD, né usati reset, clean o stash.

La prima verifica disponibile di runtime/collector/wiring ha dato 132 passed. La successiva suite PDC ha dato 654 passed, poi 661 passed dopo il completamento dei test del flow e altri casi. Sono risultati intermedi; quello finale è registrato in fondo.

## Stato dei task

| Task | Esito |
| --- | --- |
| 1 — inventario | Completato da OpenCode; riconfrontato con i diff al subentro |
| 2 — diagnostica | Completato da OpenCode; esteso agli errori asincroni dello specchio helper |
| 3 — interblocco/flow | Completato localmente, gate chiuso; verifiche parser e flow leggero superate; test del vero flow HA ancora da eseguire |
| 4 — sessione condivisa | Completato: scritture serializzate, ritentate e confermate dallo stato; niente ON obsoleto dopo la fine sessione |
| 5 — inibizione | Completate correzioni e verifiche locali: nessuna acquisizione su dato ignoto, nessun rilascio confermato solo dall'esito del servizio |
| 6 — recupero | Completati casi per ogni fase non idle, store corrotto/incompatibile e clock futuro; blocco esplicito e preservazione dello store non interpretabile |
| 7 — override/protezioni | Completati casi locali per tutte le fasi, protezioni in marcia, programma OFF e copia manuale con un nuovo OFF durante l'attesa |
| 8 — ciclo di vita PDC | Completato: riaggancio publisher dopo unload fallito, detach valido anche sul manager sostitutivo, pulizia dei soli sensori diagnostici rimossi, cancellazione task e timer |
| 9 — validazione | Suite pura, manager con HA finto e controlli statici eseguiti; **test con Home Assistant reale sotto pytest bloccati dall'ambiente** |
| 10 — consegna | Questo report e `PDC-CONTROL-PROVA-STANZA.md`; controllo reale ancora chiuso, nessuna installazione |

## Correzioni principali

### Interblocco e conferma

- Conservate le modifiche di OpenCode a modalità PDC indipendente, lista dei proprietari e conferma nel flow. A ogni modifica PDC la conferma viene invalidata; le modifiche alla sola caldaia la conservano.
- Campo proprietari obbligatorio nel percorso control: omissione diversa da una lista vuota esplicitamente dichiarata. La lista vuota resta consentita dal contratto del parser, non è un default implicito di control.
- La conferma valida vive al livello `pdc_control_confirmed` della sezione centrale. Una chiave `control_confirmed` infilata nel sotto-dizionario runtime non la sostituisce.
- `_command` rivalida modo, arresto del runtime e proprietari prima di ogni singolo servizio, anche tra due comandi della stessa sequenza. Specchio helper e inibizione rivalidano a loro volta l'interblocco.
- Tutti i gate restano chiusi: `PDC_CONTROL_AVAILABLE = False`, `HO_PDC_CONTROL_ENABLED = False`, parser/runtime/porte protetti. Nessuna modifica alla barriera caldaia.

### Sessione e risposte tardive

- `in_gestione` ON viene programmato solo dopo che il possesso è stato scritto durevolmente. OFF segue la fine della transazione, che richiede lo spegnimento confermato e il rilascio confermato.
- Una sola scrittura in volo per helper. Prima dell'invio vengono rilette intenzione e proprietà; dopo il ritorno viene verificato lo stato, non soltanto l'assenza di eccezioni.
- Scrittura fallita, non confermata o helper ignoto sono diagnosticati e ritentati ai passaggi successivi. Una scrittura già partita che termina con un'intenzione superata richiede una nuova riconciliazione.
- Un cambiamento esterno di helper viene riconciliato anche se il precedente valore era stato memorizzato in `mirrored_*`.
- La copia manuale split → VTherm PDC rilegge la sorgente dopo ogni await: niente vecchio HEAT copiato sopra un nuovo OFF utente.

### Inibizione e recupero

- L'inibizione viene richiesta solo con `inhibited` esplicitamente false alla rilettura finale. True o ignoto non conferiscono possesso.
- L'esito positivo di `unset_inhibition` non basta: viene richiesto `inhibited: false` osservato. In caso contrario la transazione resta `releasing`, conserva il possesso e ritenta.
- Lo store incompatibile o non interpretabile blocca gli effetti e non viene sovrascritto da default, neppure durante lo stop. Validati anche tipo della versione, sezioni rooms/sospensione/override e timestamp futuri della transazione.
- Ogni fase persistita non idle viene provata con dispositivi non disponibili: nessun HEAT nuovo e nessun rilascio senza OFF confermato. I test preesistenti coprono anche recupero di running valido, prenotazioni, timeout, ritorno tardivo e persistenza fallita.

### Ciclo di vita e altre stanze

- Dopo un unload delle piattaforme fallito, il manager PDC ripristinato adotta i contenitori dei publisher del precedente manager già fermato. I vecchi callback di detach agiscono sugli stessi contenitori: non restano riferimenti staccati dal nuovo publisher.
- La pulizia di registro seleziona soltanto sensori di questa entry, piattaforma VTherm e prefisso diagnostico PDC, non più configurati. Se la sezione è malformata non interpreta l'errore come una richiesta di rimozione. Nessuna entità dell'impianto è stata rimossa.
- Lo stop cancella e attende worker/specchi e cancella anche i callback di classificazione in attesa.
- Una stanza ancora bloccata dal proprietario legacy usa il proprio helper legacy per la vista dell'altra PDC, anziché una transazione locale idle non autorevole. Un test conferma che una sessione legacy in avvio impedisca l'avvio della stanza controllata.

## File interessati

In aggiunta ai diff iniziali di OpenCode:

- `heating_pdc_runtime.py`, `feature_heating_pdc_manager.py`, `config_flow.py`: completamento e correzioni sopra;
- `__init__.py`, `sensor.py`: ciclo di vita PDC e registro;
- `tests/test_heating_pdc_control_completion.py`, `tests/test_heating_pdc_flow_unit.py`: regressioni aggiunte;
- `tests/test_heating_pdc_manager.py`, `tests/test_heating_pdc_ha_integration.py`: test HA aggiunti; gate abilitato solo nella fixture del modulo di test manager;
- `tests/test_heating_pdc_fake_ha.py`, `pdc/fake_ha_pdc.py`: scenario publisher con ripristino e avvio offline opzionale;
- `pdc/offline_pytest.py`, `pdc/verify_control_completion.py`: strumenti locali riproducibili, mai importati dal componente.

I motori congelati `heating_pdc_policy.py`, `heating_pdc_provenance.py` e `heating_boiler_policy.py` non sono stati modificati. Nessuna soglia, programma o comportamento termico M3 è stato cambiato.

## Verifiche e riproduzione

Dalla radice del repository, interprete effettivo:

```powershell
$pdcPython = 'C:\Users\CarloTomaselli-SOFTCARE\AppData\Local\Temp\opencode\m1venv\Scripts\python.exe'
& $pdcPython plans/heating-orchestration-2026-11-24/pdc/offline_pytest.py -q --noconftest --import-mode=importlib -p no:cacheprovider tests/test_heating_pdc_control_completion.py tests/test_heating_pdc_flow_unit.py tests/test_heating_pdc_runtime.py tests/test_heating_pdc_policy.py tests/test_heating_pdc_transaction.py tests/test_heating_pdc_provenance.py tests/test_heating_pdc_collector.py tests/test_heating_pdc_integration.py tests/test_heating_pdc_fake_ha.py tests/test_heating_boiler_policy.py tests/test_heating_boiler_collector.py tests/test_heating_boiler_command.py
& $pdcPython plans/heating-orchestration-2026-11-24/pdc/verify_control_completion.py
```

- Regressioni caldaia pure eseguite separatamente: **459 passed, 1 skipped**; lo skip è l'import completo che richiede HA.
- Quattro mutazioni in copie temporanee: rimozione dell'interblocco per servizio, falsa conferma del rilascio, invio di uno specchio obsoleto, mancato blocco dello store corrotto. **4/4 rilevate da assertion fallite**, non da errori di import. Il sorgente attivo non è stato mutato. Un primo esperimento sulla riconciliazione finale era ridondante rispetto agli eventi del fake e non veniva rilevato: è stato sostituito con il caso significativo del comando schedulato diventato obsoleto.
- Parsing di **86 file Python e 2 JSON** riuscito. JSON it/en e parità delle nuove traduzioni coperti dai test di integrazione pura.
- Formattazione dei moduli PDC e test interessati con Black **26.10.1**, linea 180, tramite API sincrona perché anche il launcher parallelo di Black richiede socket locali. La CI storica cita Black 24: non viene dichiarata eseguita con quella versione. I file condivisi non sono stati riformattati integralmente.
- `git diff --check` senza errori; nessun cambiamento dei motori congelati, manifest o pacchetti archiviati.

### Limite dell'ambiente di test

Python 3.15.2 / pytest 9.2.0 del venv di OpenCode. L'avvio ordinario di asyncio si blocca nel socketpair locale: una prova TCP loopback esplicita restituisce `WinError 10013`. Il runner offline usa un event loop senza socket per i test puri a clock controllato, rifiuta registrazioni I/O e non è adatto a servizi reali, rete o risvegli da thread. Non è una prova del loop Windows/HA di produzione.

La raccolta dei test HA fallisce in `tests/conftest.py` con **`ModuleNotFoundError: pytest_socket`**; nel venv mancano anche Home Assistant e pytest-homeassistant-custom-component. Il percorso storico `AppData/Local/vtherm-ha-test` non esiste in questa sessione. Non sono stati scaricati aggiornamenti o alterate dipendenze per aggirare il limite. I nuovi test HA sono scritti e compilati, **non eseguiti**.

## Limiti prima dell'attivazione

1. Eseguire test HA e regressioni Avatto/inibizione/centrale in ambiente adeguato; poi review della sequenza e preparazione di una release distinta. Questa consegna non dichiara pronto il controllo sull'impianto.
2. Rileggere proprietari e chiamanti UI; uno script `off` è soltanto inattivo, non incapace di essere invocato. La perdita di interblocco blocca anche gli OFF di protezione del fork: la responsabilità passa al proprietario esterno, non vengono inviati comandi concorrenti.
3. L'API di inibizione non espone un possesso atomico fra lettura e acquisizione: il marcatore di transizione non distingue due attori che acquisiscono nella stessa finestra. Occorre confermare l'esclusività degli scrittori prima del control; se non dimostrabile serve un contratto di possesso nell'API, non una deduzione nello specchio.
4. La prenotazione del fork non è atomica con i controller legacy. Per una prima prova su Carlo, mantenere visibili gli ingressi di Diego e sospenderne le nuove partenze automatiche con una procedura concordata. Non omettere semplicemente la seconda PDC dalla configurazione.
5. Servizi/card della sospensione generale e storico eventi rimangono fuori da questa consegna. Lo stato `blocked` PDC rimane quello del contratto corrente, senza inventare `control_status`. Il problema storico di riaggancio del sensore **caldaia** dopo unload fallito non è stato ampliato a questo incarico PDC.

Runbook: `PDC-CONTROL-PROVA-STANZA.md`. Ripristino locale: il diff distingue le modifiche preesistenti e quelle descritte qui; non usare reset globale su un working tree condiviso. Ripristino d'impianto non necessario perché non è stato modificato.

## Risultato finale della suite

**1122 passed, 1 skipped, 2 warning in 29,96 s**, esecuzione combinata PDC + caldaia dopo le modifiche finali. Lo skip richiede l'import completo con Home Assistant; i due warning pytest riguardano le opzioni asyncio non riconosciute nel venv minimo. Il runner emette inoltre avvisi di deprecazione della policy asyncio su Python 3.15. Non sono errori del componente.

Il risultato è una validazione offline di logica, sequenze e adattatori con fake, non la validazione dell'impianto o del runtime HA. La parte implementativa locale è consegnata; il task 9 resta esplicitamente parziale per i test HA mancanti e l'apertura del controllo rimane bloccata.
