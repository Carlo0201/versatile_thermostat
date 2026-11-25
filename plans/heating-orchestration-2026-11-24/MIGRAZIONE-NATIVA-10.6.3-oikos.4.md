# Migrazione del riscaldamento alla versione 10.6.3+oikos.4

Questa build è un candidato per il passaggio di controllo a tappe. Caldaia e PDC sono configurabili separatamente. L'aggiornamento richiede una nuova conferma per ogni controllo già configurato. Abilitare esplicitamente `observe` durante il primo avvio di confronto, con i proprietari precedenti operativi.

## Prima dell'aggiornamento

1. Salvare la directory installata `custom_components/versatile_thermostat` e un backup HA che includa ConfigEntry, registro entità, Store del fork e configurazioni YAML. Annotare quali automazioni, script e programmi sono attivi. Verificare che il backup sia ripristinabile.
2. Chiudere le sessioni PDC e gli override stanza con il proprietario attuale. Verificare split e VTherm PDC `off` e il radiatore restituito. Un'eventuale transazione attiva salvata da una build precedente senza proprietario verificabile richiede riconciliazione con quella build prima del passaggio.
3. Verificare gli utilizzatori delle automazioni e degli helper, includendo dashboard Oikos, ConfigEntry, YAML, script, scene e flussi Node-RED. Partire da [IMPIANTO-INVENTARIO-2026-11-24.md](IMPIANTO-INVENTARIO-2026-11-24.md) e ricontrollare l'elenco sull'impianto. Nomi simili o stato `off` da soli non sono una prova che una voce si possa rimuovere.

## Installazione e confronto

1. Estrarre il pacchetto in una directory temporanea e verificare l'impronta SHA-256. Sostituire per intero la sola directory dell'integrazione con quella contenuta nel pacchetto. Riavviare Home Assistant preservando le ConfigEntry.
2. Verificare la versione `10.6.3+oikos.4`, tutte le entità caricate e i log. Configurare `observe` con gli ingressi legacy per il confronto. Controllare disponibilità, motivi decisionali e aggiornamenti dei sensori di stanza, anche a finestra aperta e dopo la riconnessione di un dispositivo.
3. In caso di errore fermare il passaggio e ripristinare la directory precedente. Ripristinare anche la configurazione e gli Store dal backup se nel frattempo sono stati modificati. Evitare di copiare un vecchio Store su un impianto che ha ancora una sessione nativa in corso.

## Passaggio di proprietà

| Passo | Configurazione | Prova per poter proseguire |
| --- | --- | --- |
| Caldaia | Opzioni della centrale, configurare `control`, proprietari esterni, associarvi le stanze e confermare. Iniziare con gli ingressi noti. | Con un proprietario on, mancante o non disponibile, verificare il blocco. Spegnere il controller legacy in modo reversibile. Verificare il primo ciclo naturale, la richiesta manuale, la pausa automatica e il ritorno a OFF. |
| Ingressi caldaia | Abilitare il consenso nativo quando il consenso legacy è stato sostituito. Si possono omettere i consensi esterni e il contesto legacy. Il gestore centrale conserva la diagnostica di richiesta e l'orchestratore assume l'invio dei comandi. | Verificare che l'azione legacy non scriva più sull'helper di consenso. L'eventuale filtro pilota e i consensi già indicati continuano ad avere effetto fino alla rimozione dalle opzioni. |
| Programma stanza | Associare gli switch di Scheduler e gli eventuali proprietari legacy del programma. Configurare il calendario lavorativo se usato. | Dopo aver disattivato il vecchio script di ripresa, provare una stanza. Verificare durata, scadenza, annullamento, riavvio e che un programma precedentemente spento rimanga spento. |
| PDC | Configurare e confermare `control` con tutti i proprietari della stanza. Per usare un'autorizzazione nativa, indicare lo stato dell'opzione automatica e togliere il corrispondente helper. Configurare entrambe le stanze se usano la stessa riserva di potenza. Ritirare il proprietario legacy di una stanza alla volta. | Provare presa in carico, arresto, riconnessione e conferma di OFF di entrambi i dispositivi prima del rilascio del radiatore. Verificare che un'inibizione di un altro proprietario resti al suo posto. |
| Eliminazione riferimenti | Rimuovere dalle opzioni i mirror e le sorgenti legacy sostituiti. Il sensore stanza PDC richiede chiave stanza, VTherm radiatore e split coerenti nelle due configurazioni. | Verificare di nuovo con i proprietari legacy disabilitati. Svuotare la lista dei proprietari solo dopo aver verificato che tutti siano stati effettivamente ritirati. |

### Comandi stanza

- `versatile_thermostat.heating_room_override` accetta `room_key` e `minutes`. Omettere `minutes` per annullare una pausa e riprendere gli scheduler sospesi dall'integrazione. Se risponde `pending`, verificare gli attributi di errore e avanzamento del sensore stanza.
- `versatile_thermostat.heating_room_history` accetta `room_key` e i filtri opzionali `since`, `until`, `limit` e `offset`. Restituisce le righe del registro nativo. Serve una chiamata di servizio con risposta. Le date numeriche sono timestamp Unix.
- Il nuovo registro osserva il VTherm radiatore e l'eventuale split associato. Le modifiche rilevate riportano l'origine disponibile nel contesto di Home Assistant. Per un telecomando fisico il contesto può restare ignoto.
- La richiesta manuale nativa è legata all'avvio di un override confermato e a una stanza idonea con richiesta termica. Nei due minuti dall'avvio può usare il percorso manuale già previsto da M1. Il resto della durata continua come override del programma.

## Pulizia dopo la prova

| Gruppo | Prerequisito per disattivare e poi rimuovere |
| --- | --- |
| Controller caldaia, filtri e timer di avvio | Le decisioni native riproducono i cicli approvati. La UI legge i sensori nativi e invia i comandi corretti. |
| Chooser, override, timer e registri PDC | Ogni stanza ha completato partenza, ritorno al radiatore e riavvio con controllo nativo. Nessun uso residuo di mirror o helper. |
| Ripresa programmi e timer override stanza | L'azione nativa viene usata dalla UI e gli switch Scheduler risultano associati correttamente. |
| Registro azioni legacy | Il registro e la UI nativi sono sufficienti per l'uso quotidiano e si è deciso come conservare lo storico già raccolto. |
| Profilo comfort split, previsione dello stop programma e integrazioni esterne | Confermare la sostituzione effettiva prima di ritirare automazioni di ventola/display/volume, sensori che prevedono il prossimo OFF e consumer prodotti da Oikos. Se i programmi sono collegati nelle opzioni PDC, la guardia nativa legge il prossimo cambio esposto da Scheduler. La regola nativa è più conservativa: sospende una nuova partenza vicino a ogni cambio di programma, inclusi i cambi di preset. Verificare la parità d'uso prima di togliere la sorgente legacy. |

Eliminare solo dopo la fase reversibile, dopo aver rieseguito la ricerca dei riferimenti e in piccoli gruppi verificabili. Sensori, termostati, split e gli switch creati da Scheduler restano fonti e dispositivi del sistema.

## Perimetro della build

La build sposta nell'integrazione l'arbitraggio caldaia/PDC, l'override dei programmi esistenti e il registro stanza. I programmi settimanali restano in Scheduler. Il profilo accessorio dello split e la sospensione coordinata dell'intero impianto richiedono ancora lavoro. Le card Oikos esistenti vanno riconfigurate o adattate se referenziano gli helper o gli script da ritirare. La prova offline verifica le logiche senza contattare l'impianto e non certifica il passaggio operativo su Home Assistant.

## Controlli del pacchetto

- Archivio compilato dal fork locale con base `a8b62ab` e patch di questa versione. Contiene 95 file sotto `custom_components/versatile_thermostat/`, con integrità ZIP e corrispondenza byte per byte al codice provato. Sintassi Python e JSON verificata. Politica M1 identica a `a8b62ab`.
- SHA-256 `50fb55eb76924382018c343b628225fbc47df8abc78c5383dc5541737f807475`.
- Suite offline con Python 3.15.2 e pytest 9.0.3. **1404 passati, 2 saltati**. I due skip riguardano l'import di HA assente nell'ambiente e la rigenerazione volontaria delle fixture Oikos.
- Home Assistant simulato. Caldaia **26/26 scenari**, PDC **45 verifiche**, sensori stanza **12 scenari / 35 verifiche**. Verificato anche l'adattatore dei servizi stanza con porte finte, dall'avvio a override, annullamento e interrogazione del registro.
