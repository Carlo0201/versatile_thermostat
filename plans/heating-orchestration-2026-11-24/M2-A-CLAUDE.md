# Claude — M2-A: dati reali e osservazione della caldaia

M1 è approvato: leggi `M1-CONFERMA-CLAUDE.md`, il piano, `SPECIFICA.md`, `DIAGNOSTICA.md` e le istruzioni applicabili. Il tuo incarico rimane solo codice, test e documentazione del fork. Codex gestisce review, Oikos e successiva installazione. **Non accedere all'impianto.**

M2 è ora diviso in due consegne: **M2-A qui autorizzato**, collector e sola osservazione; **M2-B non ancora assegnato**, comandi, conferme e proprietà degli attuatori. Non implementare control in questa consegna. Nessuna modifica alla politica M1, nessuna PDC, nessun deploy, commit o push.

## Risultato richiesto

Il fork può essere configurato per leggere lo snapshot HA, qualificare le valvole e valutare M1, pubblicando una diagnostica nativa. In observe non invia alcun comando a master, termostati, split, inibizione, helper/timer o automazioni esistenti. La normale gestione VTherm continua identica. Con funzione assente/disabled, configurazioni e comportamento esistenti restano invariati.

## Ricognizione e punti di integrazione

Controlla stato Git e conserva tutti i file non tracciati: contengono M1, baseline e lavoro precedente. Leggi almeno:

- `vtherm_central_api.py`: API locale derivata da `VThermAPI`, configurazione centrale, `init_vtherm_links`, manager caldaia/potenza. Evita nuove dipendenze o modifiche al package esterno `vtherm_api`.
- `__init__.py`: setup, update listener, reload globale e unload delle entry. La centrale può essere ricollegata più volte: non moltiplicare observer/listener.
- `feature_central_boiler_manager.py`, `base_manager.py`, `sensor.py` (setup entry centrale e sensori boiler).
- `config_flow.py`, `config_schema.py`, `const.py`, traduzioni e test dei flow. Adotta i percorsi di configurazione già usati nel fork, senza introdurre installazione YAML.
- `heating_boiler_policy.py`, `baseline/caldaia_controllo_stabile_delle_richieste.json` (collector `raw`), `baseline/boiler-observation-cases.json` e `baseline/index.json`.

Puoi scegliere nomi di moduli coerenti col repository; struttura suggerita: collector separato e `FeatureHeatingOrchestrationManager` della centrale. Il collector deve essere testabile su un mapping di stati senza HA attivo. Il manager traduce un singolo snapshot HA coerente nel modello M1, gestisce il proprio ciclo e pubblica il risultato. Non aggiungere un secondo percorso di chiamate ai servizi del gestore caldaia nativo.

## Configurazione esplicita

1. Modalità ammesse in M2-A: `disabled` (default per chiavi assenti) e `observe`. `control` non deve essere selezionabile né attivabile tramite dati configurazione; un valore non supportato deve essere rifiutato o produrre uno stato inattivo con errore esplicito, mai controllo implicito.
2. Associazioni configurabili: VTherm stanza, eventuale sensore finestra, appartenenza al pilota della stanza singola, eventuali split/sessione PDC; master osservato, centrale, fonte conteggio/ID attivi, manuale, pilota e due consensi nativi. Nessun ID di casa o nome di stanza nel codice runtime.
3. Gli underlying delle valvole possono essere ricavati dagli attributi VTherm, con validazione. Scarta binding duplicati/incoerenti e riferimenti mancanti senza comandare nulla. Resta distinta la stanza dalla singola valvola.
4. Esponi UI/config flow e opzioni sufficienti per configurare observe senza file editati a mano; traduzioni italiano/inglese. Le associazioni ripetute possono usare passi configurazione dedicati o una struttura validata già supportata: evita un campo JSON opaco come unica interfaccia.
5. Leggi i valori tramite le convenzioni effettive del fork (`data`/`options`): non assumere che siano uguali. Mantieni compatibilità con entry esistenti e testala. Nessuna selezione automatica del master che potrebbe indicare il consenso stesso.

## Collector e semantica

La specifica normativa è il collector Jinja `raw`. Porta esattamente filtri, confronti e default, conservando la distinzione fra dato invalido e domanda assente:

- Conteggio grezzo finito, intero non negativo e coerente con gli ID fisici unici. La baseline deduplica gli ID: una lista ripetuta è valida solo se il conteggio corrisponde agli ID unici; non irrigidire implicitamente questa semantica.
- Qualificazione VTherm: heat/auto, pronto, inizializzato, partecipante alla caldaia, finestra contatto/manager o bypass esplicito, assenza inibizione/sicurezza/potenza, domanda Avatto non falsa, temperatura e target validi, misura non futura e vecchia al massimo 3600 s. Preserva i default della baseline dove sono espliciti e documentali.
- Valvole: appartenenza alla lista attiva, modo heat/auto e `hvac_action=heating`. Conta ID fisici unici; supporta due valvole nella stessa stanza. Il gap della stanza è arrotondato a tre decimali prima di M1.
- Stanze associate a PDC: radiatore idoneo solo con sessione OFF e split fisico OFF confermati. Missing/unknown/unavailable non equivalgono a OFF. L'observer legge soltanto queste associazioni: non implementa la scelta della fonte PDC.
- Pilota: qualifica la stanza singola secondo binding, senza allargare il veto al percorso normale. Distinguere pilota ON/OFF/ignoto, prontezza globale e consensi nativi. Mai usare come consenso la proposta del nuovo observer.
- Centrale valida solo nei modi che permettono il riscaldamento. Master ignoto non diventa OFF; timestamp con offset vengono convertiti correttamente. Chiarisci e valida l'unità termica prevista da M1 (°C); unità assenti/incompatibili vanno gestite esplicitamente, senza applicare soglie Celsius a valori Fahrenheit.
- Per dati necessari assenti/incoerenti, fornisci `ok=false` e diagnostica specifica, senza eccezioni che fermino la valutazione. Se il modello richiede un conteggio intero anche in errore, l'eventuale zero strutturale deve restare accompagnato da invalidità e non essere presentato come conteggio osservato valido.

Le 17 fixture di osservazione esistenti devono rimanere immutate. Confronta il risultato raccolto completo previsto, non solo l'azione del motore. Aggiungi casi per condizioni simultanee, duplicati, due valvole in una stanza, PDC non disponibile, finestra aperta prima della sospensione confermata, bypass, età 3599/3600/3601 e timestamp futuri.

## Observe, ciclo e lifecycle

- Un observer per configurazione centrale; ascolta solo gli ingressi pertinenti, inclusi cambi attributi, e usa una rivalutazione di recupero ogni 15 s. Non ascoltare la propria diagnostica creando loop.
- Scheduling senza sleep bloccanti, con scadenze M1 e ricalcolo degli ingressi. Deadline già passate, assenza consenso e aggiornamenti duplicati non devono creare loop a ritardo zero. Coalescenza/serializzazione per eventi contemporanei e token generazione per callback vecchie.
- Un solo clock catturato per valutazione. Esegui collector e M1 sullo stesso snapshot; diagnostica e ciclo devono appartenere alla stessa generazione. Una proposta HEAT non conferma lo stato master.
- Conserva il contesto observer separato dai vecchi helper: restore/storage HA supportato, schema versionato, senza scritture dirette a `.storage`. Scritture differite solo quando serve; niente persistenza sincrona o scrittura disco ad ogni secondo.
- Se utile, consenti un **seed opzionale, di sola lettura e una sola volta** dal ciclo legacy configurato; valida lo schema con M1 e registra `cycle_origin` (legacy_seed/local_restore/new_observation). Non sovrascrivere continuamente il ciclo con quello esterno. Un'avvio observe senza seed può introdurre una pausa iniziale: dichiaralo, non presentarlo come divergenza della politica.
- Non ripristinare automaticamente observe/control dopo una disabilitazione intenzionale. Se è configurato observe può ripartire come observer al caricamento della relativa entry. Aspetta gli ingressi realmente disponibili prima di dichiarare osservazioni valide.
- Su unload, reload, modifica configurazione o disabled annulla timer, listener e task posseduti; niente pubblicazioni tardive da generazioni annullate. Non interferire con timer/listener nativi o inibizioni che non appartengono all'observer.

## Diagnostica nativa

Entità diagnostica della configurazione centrale, unique_id stabile derivato dall'entry e stato breve (motivo decisionale o unavailable). Non riutilizzare gli ID legacy né scrivere i loro helper.

Attributi versione 1: modalità observe, istante valutazione, numero generazione, azione proposta, motivo, scadenza, modo master osservato, conteggio grezzo/valido e loro validità, stanze/valvole idonee, condizioni M1 e condizioni locali di esclusione, origine ciclo e ultimo errore. Nessun campo “comando inviato” o “confermato dal coordinatore” valorizzato in observe: non esiste un comando dell'observer.

Per ogni condizione: ID stabile, pass/fail/wait/unknown/na, motivo, valore/atteso con unità e scadenza eventuale. Le condizioni non applicabili seguono le correzioni di M1. Dati ignoti restano null/unknown, JSON finito e dimensione limitata. Definisci chiaramente quali elementi appartengono alla stanza e quali alla centrale. Escludi attributi ad alta frequenza dal recorder con i meccanismi HA del componente; conserva uno storico utile di motivo/stato, senza log/eventi ad ogni tick.

Documenta il nome e lo schema effettivo per il futuro adattamento Oikos. Non modificare Oikos in questa consegna. Le sorgenti legacy rimangono necessarie fino al passaggio esplicito del frontend.

## Verifiche obbligatorie e consegna

- Riesegui M1: 143 pass e uno skip nell'ambiente minimo attuale; nessuna regressione. Collector testabile offline con clock esplicito e fixture originali.
- Test HA dedicati: setup vecchie entry/disabled senza nuovi effetti; config flow completo e binding invalidi; observe non chiama servizi di attuazione, inibizione o helper legacy; seed/restore, concorrenza, deadline passata, polling, reload/unload e callback tardive; unique_id e aggiornamenti diagnostici.
- Non usare una sostituzione globale dei package per simulare test di integrazione. Esegui i test HA nell'ambiente Python 3.14 e dipendenze del repository se disponibile; altrimenti scrivili ma dichiara chiaramente che non sono stati eseguiti. Stub di unit test non certificano il lifecycle HA.
- Regessioni pertinenti: caldaia nativa, Avatto, configurazione e nuove entità. Non parallelizzare il sottoinsieme caldaia noto instabile; usa clock controllato, nessun comando fisico.
- Limita formattazione/check ai file coinvolti. Non aggiornare requisiti per aggirare limiti locali. Correggi il refuso nel testo corrente dell'esito M1 sulle combinazioni diagnostiche: il prodotto del test è 23.328; preserva la consegna storica di OpenCode.
- Consegna `M2-A-ESITO.md`: architettura, elenco file/hash, schema config e diagnostico, test e limiti, esempi sintetici di configurazione per revisione (non da installare), compatibilità e disattivazione locale. Non aggiornare manifest/versioni pubbliche o creare release in questa fase.

Termina con la consegna M2-A. Il collegamento ai comandi e le verifiche di proprietà saranno oggetto di M2-B dopo review di Codex.
