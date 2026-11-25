# Prima release per il controllo caldaia

25 novembre 2026, Europe/Rome. Carlo ha autorizzato di iniziare dalla caldaia e ha esplicitamente escluso un ambiente HA isolato. Nessuna fixture o istanza Home Assistant di test è stata avviata. **Release preparata, non installata; automazioni e configurazione live non modificate.**

## Pacchetto

`output/versatile_thermostat_boiler_control_2026-11-25/` nel progetto padre:

- cartella già estratta `custom_components/versatile_thermostat/`;
- `versatile_thermostat-10.6.3+oikos.2-boiler-control.zip`;
- istruzioni complete `INSTALLAZIONE-CALDAIA.md`, metadati `BUILD.json`, hash dei 89 file.

SHA-256 finale ZIP: `03a6d93418ea470fd010ad97908f0a1b3a7b20b5efc4abe329eae7565f56646b`.

Base stabile `c838def04506ca092a6479986ad14d14f775ddfd`. **Solo cinque file differiscono**: manifest, `heating_boiler_collector.py` (gate True), `heating_boiler_command.py`, `feature_heating_orchestration_manager.py`, `__init__.py` (ripristino publisher della sola caldaia). Flow, schema e traduzioni sono quelli della base stabile che già supporta il controllo caldaia con conferma. I moduli PDC sono byte-identici alla base, con `PDC_CONTROL_AVAILABLE = False`.

Il working tree conserva il lavoro PDC precedente. Il builder esporta il commit stabile e sovrappone solo i due moduli boiler indicati, applicando esplicitamente gate, riaggancio e versione. Per non trasformare il working tree misto in una release implicita, la sua versione/gate generali non vengono aperti: **l'artefatto da installare è quello in output, non una copia dell'intero working tree**. Nessun commit, push, tag o pubblicazione.

## Correzioni prima dell'apertura

1. Budget per modo persistito come chiave opzionale `command_budget` nello Store v2. I tentativi consumati e i backoff sopravvivono al riavvio, anche quando la richiesta era stata superseded; un invio pendente salvato prima della successiva valutazione viene ricostruito dal record. Shutdown annulla task e callback senza concedere nuovi tentativi. Persistenze malformate o clock futuro non vengono interpretati come un nuovo budget libero. La perdita di proprietà mantiene il proprio comportamento di annullamento.
2. Dopo unload fallito, il manager sostitutivo adotta la lista dei publisher e gli ID del manager fermato. I callback di detach originali continuano ad agire sui contenitori condivisi: il sensore viene aggiornato anche senza un reload riuscito.
3. Politica termica, collettore e soglie invariati; nessuna modifica a caldaia nativa, TRV, PDC o dashboard.

File sorgenti modificati per il boiler: `heating_boiler_command.py`, `feature_heating_orchestration_manager.py`, il solo aggancio boiler in `__init__.py`, nuovi test `tests/test_heating_boiler_release.py`, harness M2-B aggiornato e due scenari aggiunti. I diff PDC preesistenti sono preservati ed esclusi dal pacchetto.

## Verifiche

- **469 passed, 1 skipped** per policy, collector, comando e nuovi casi di release. Lo skip è il test che richiede l'import completo del componente con HA. Pytest del venv minimo emette due warning di configurazione asyncio.
- **25/25 scenari del manager sul candidato** (API HA simulate, zero accessi all'impianto): priorità OFF su HEAT pendente, servizio in errore, conferme, quattro invii, proprietà, persistenza bloccata/fallita, stato del master ignoto, unload, ripristino dei publisher e budget al riavvio.
- Il test di riavvio del fake usava un clock riportato indietro al valore iniziale; ora il riavvio usa il tempo raggiunto dalla precedente esecuzione. Il clock realmente futuro resta coperto da un test conservativo separato.
- Pacchetto: 89 file, ZIP integro, contenuti identici alla cartella estratta, compilazione e parsing JSON riusciti, gate caldaia True e gate PDC False. Confronto completo contro il commit stabile limita le differenze ai cinque file dichiarati.
- Runner offline già predisposto nella precedente consegna, senza socket: non è una prova del runtime HA. Non è stato creato né avviato un ambiente HA per questa attivazione. Una ricerca preliminare nella cache non ha prodotto alcun ambiente utilizzato; è stata interrotta su indicazione di Carlo.

Comandi riproducibili, dalla radice del fork, usando il Python del venv minimo OpenCode:

```powershell
$boilerPython = 'C:\Users\CarloTomaselli-SOFTCARE\AppData\Local\Temp\opencode\m1venv\Scripts\python.exe'
& $boilerPython plans/heating-orchestration-2026-11-24/pdc/offline_pytest.py -q --noconftest --import-mode=importlib -p no:cacheprovider tests/test_heating_boiler_release.py tests/test_heating_boiler_command.py tests/test_heating_boiler_collector.py tests/test_heating_boiler_policy.py
& $boilerPython plans/heating-orchestration-2026-11-24/boiler-release/verify_release.py
```

Per provare il manager del pacchetto, impostare `BOILER_COMPONENT_DIR` alla cartella `custom_components/versatile_thermostat` del candidato ed eseguire il runner offline con `--script plans/heating-orchestration-2026-11-24/m2b/fake_ha_scenarios.py`. Il test con il candidato verifica anche il gate aperto del suo collector; il caso di gate chiuso viene iniettato esplicitamente nel processo di test.

## Preflight live (sola lettura)

Alle 12:15 circa: master `climate.termostato_ingresso` HEAT, observe maintain con tre valvole valide, controller stabile ON. Legacy filtro, sincronizza timer e avvio manuale immediato OFF. Ricerca HA dei riferimenti al master: le stesse quattro automazioni, nessuno script/scena. Hash del controller stabile confermato `7799f0f9d831df35`.

La condivisione SMB non è accessibile dalla sessione (`Test-Path` False). Non si è potuta leggere/sostituire la cartella installata; non è stato dichiarato eseguito un backup del componente. Il primo passo operativo richiede la copia da Windows, con backup, e un riavvio lasciando le automazioni attive e la centrale in observe.

## Prossimo passo e ripristino

Seguire `INSTALLAZIONE-CALDAIA.md` nel pacchetto. Dopo caricamento verificato: salvare la configurazione corrente, configurare i quattro proprietari esterni, impostare e confermare control mantenendo il controller stabile ON e verificare il blocco. Solo dopo disattivare il controller stabile e attendere assestamento/valutazione. Gli altri tre restano OFF; il gestore caldaia nativo resta attivo come consenso. Nessuna temperatura viene modificata per provare una schermata.

Le card basate sulla diagnostica legacy devono passare alla fonte native o a una vista diretta del sensore prima di considerare conclusa la migrazione della UI; gli helper legacy smettono di essere aggiornati dal controller spento. Nessuna modifica alle card è stata eseguita in questa preparazione.

Ripristino del controllo: riattivare il controller stabile (che blocca il nuovo tramite interblocco), poi riportare la nuova orchestrazione a observe/disabled. Non attivare le altre tre legacy. Ripristino del codice: cartella del componente salvata prima dell'installazione e riavvio, senza rimozione delle entry o cancellazione degli store. Non c'è una prova live di funzionamento del control finché il pacchetto non è installato e il passaggio non è effettuato.
