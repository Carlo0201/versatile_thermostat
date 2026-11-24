# Specifica funzionale e verifiche

## Caldaia — comportamento da preservare

Fonte normativa: `baseline/caldaia_controllo_stabile_delle_richieste.json`. Le prime variabili raccolgono gli ingressi (`raw`, `cycle`, `now_ts`); le seconde calcolano `plan`. Il calcolo è ripetuto dopo la scrittura del contesto e prima dell'esecuzione: una sola valutazione remota non autorizza un comando con ingressi ormai cambiati.

| Condizione | Comportamento |
| --- | --- |
| Master OFF, almeno 2 valvole fisiche valide, almeno una stanza con gap >=0,3 °C | Stabilità 120 s e pausa da OFF 600 s; alla scadenza serve consenso nativo e l'eventuale pilota |
| Master OFF, esattamente 1 valvola valida, stanza con gap >=0,3 °C | Stessa stanza e stesso target per 900 s, oltre alla pausa da OFF di 600 s; non richiede soglia nativa a due valvole; conserva veto e qualificazione pilota se abilitato |
| Master HEAT, almeno 1 valvola valida e ingressi/centrale validi | Mantiene; non ripropone soglia termica o numero minimo di avvio |
| Master HEAT, zero valvole valide oppure ingressi/centrale invalidi | Richiede OFF subito; nessun minimo ON può forzare funzionamento contro valvole chiuse |
| Richiesta manuale Oikos riconosciuta e almeno 1 valvola valida, master OFF | HEAT senza stabilità/pausa automatica; non scavalca validità impianto, finestra o disponibilità del master |
| Master unknown/unavailable o modalità diversa da OFF/HEAT | Nessun comando; non trattare come OFF confermato |
| Contesto OFF assente, incoerente o futuro e master OFF confermato | Ancora conservativa all'istante di valutazione; pausa iniziale 600 s |

Sono valvole fisiche, non stanze: il soggiorno ha due valvole. Il conteggio grezzo deve essere intero non negativo e coerente con gli ID fisici unici; quello valido viene poi filtrato. Una stanza partecipa solo se VTherm pronto e inizializzato, HVAC heat/auto, partecipante alla centrale, finestra chiusa confermata dal contatto e dal manager oppure bypass esplicito, nessuna inibizione/sicurezza/sovrapotenza, temperatura e target numerici, campione non futuro e non più vecchio di 3600 s, domanda Avatto non esplicitamente falsa. La valvola deve appartenere agli ID attivi, essere heat/auto e riportare heating. Per le camere con PDC serve inoltre sessione automatica OFF e split fisico OFF, entrambi confermati.

Nella baseline il filtro specifico `pilot_ok` riguarda Camera Carlo e Bagno Blu; non estenderlo in base a documenti storici. Esprimerlo come input/configurazione, senza nomi di stanza nel codice generico. Un dato mancante non diventa zero o “nessuna richiesta”. Quando la baseline applica un default, descriverlo e verificarlo invece di introdurre una nuova semantica implicitamente.

Persistenza logica iniziale: `m` ultimo modo osservato, `off` istante OFF confermato, `r` inizio stabilità normale, `s=[identificativo_stanza,target,inizio]` o lista vuota. La stabilità normale dipende dalla validità del percorso, non dall'identità immutabile delle stanze; quella singola richiede identità e target uguali. Perdita requisiti, cambio candidato o target azzerano l'attesa singola. Niente tempo di avvio generato dall'apertura della dashboard. In M2 usare storage/restore HA supportato, versionato, senza scritture dirette a `.storage`.

Distinguere `none/heat/off` proposti da comando inviato e conferma osservata: chiamata di servizio completata non prova HEAT/OFF né l'accensione del bruciatore.

## PDC — migrazione dopo la caldaia

Fonti: i due `*_scelta_radiatore_o_pompa_di_calore.json` e le rispettive automazioni companion. Lo stato HEAT SmartThings non misura il funzionamento del compressore.

| Regola | Baseline |
| --- | --- |
| Avvio | Automatico abilitato, nessun override climatizzatore, stanza/programmi e centrale compatibili, finestra chiusa, dati validi, gap >=0,4 °C, stabilità 120 s |
| Capacità elettrica | Casa + riserva 1500 W entro 2600 W, quindi limite di avvio 1100 W; arresto per limite a 2700 W secondo il confronto esatto dello snapshot |
| Disponibilità | Split locale OFF e altro split libero; una sola PDC automatica alla volta; nessuna nuova regola “unica stanza in richiesta” |
| Orizzonte programma | Non avviare se entro 600 s un cambio programma elimina la richiesta/spegne; verifica anche prima del comando finale |
| Nuovo ciclo | Almeno 600 s da OFF confermato; conservare guardie conservative già presenti su raw/sessione |
| Stop ordinario | Target +0,2 °C, dopo almeno 600 s da HEAT confermato |
| Stop anticipato | Target +0,5 °C, oppure target abbassato rispetto all'avvio e già raggiunto |
| Protezioni | Potenza, dati mancanti/stantii, programma, centrale e comandi manuali hanno precedenza sul minimo ordinario |
| Finestra in marcia | ON continuo 120 s; richiusa prima della soglia azzera l'attesa. Avvio nuovo richiede chiusa. Unknown/unavailable conserva la protezione attuale, inclusa tolleranza di 30 s nelle protezioni override: non equiparare a chiusa |
| Setpoint PDC | `ceil(target_stanza + 1)` limitato a 16–30 °C; preservare verifica dei limiti e sequenza degli snapshot |
| Controllo manuale AC | Override 120 minuti; interrompe l'automatismo prima dei comandi. OFF utente e protezioni restano prioritari |

Transazione verso PDC: prenotare la capacità comune, inibire il radiatore tramite API esistente, attendere conferma di arresto radiatore/valvola, impostare target e verificarne l'esito, rileggere condizioni, inviare HEAT al VTherm PDC, attendere HEAT dello split fisico. Non riscrivere il requested state del radiatore come surrogato di inibizione.

Ritorno: OFF al VTherm PDC, attendere split fisico OFF; eventuale fallback OFF diretto e ulteriore attesa come nella baseline. Solo dopo conferma rilasciare l'inibizione posseduta. Timeout, indisponibilità o cancellazione non autorizzano riapertura del radiatore. Baseline Carlo: attesa arresto radiatore 25 s, conferma HEAT 20 s, OFF 20 s e fallback 15 s; confrontare anche Diego Luisa prima di generalizzare.

Una chiamata concorrente non può acquisire lo stesso split o superare la mutua esclusione. Rileggere override e protezioni dopo ogni await. Serializzare decisione e prenotazione; evitare di tenere un lock globale per l'intera attesa cloud. Token/generazioni impediscono che una risposta o un timer vecchio completi una nuova transazione. Un lease scaduto non prova che il dispositivo sia spento.

Registro effetti attesi: corrispondenza campo/valore/scadenza, non soppressione generale dei cambi. Quiet può portare fan_mode ad auto e OFF può azzerare il preset: effetti previsti per 75 s. Un evento utente HA autenticato mantiene precedenza; SmartThings non identifica chi usa il telecomando. Se la provenienza manca deve restare sconosciuta.

## Matrice di accettazione

| Area | Casi obbligatori |
| --- | --- |
| M1: parità | Tutti i casi e gli esiti salvati in `baseline/boiler-decision-cases.json`; action, reason, deadline e contesto, non solo azione |
| Soglie caldaia | 0/1/2 valvole, due valvole nella stessa stanza, gap 0,299/0,300, 119/120 s, 899/900 s, 599/600 s |
| Temporizzazioni | Perdita/ritorno requisiti; candidato o target diverso; continuità del normale con stanze diverse; deadline = massimo tra stabilità e cooldown |
| Manuale | ON/OFF/unknown; immediato solo con ingressi utilizzabili; non altera le protezioni; mantenimento indipendente dal consenso di avvio |
| Restore | Contesto assente, non mapping, numeri assenti/negativi/futuri; master non disponibile; valutazioni duplicate allo stesso istante e sequenze con cambio modo |
| Raccolta M2 | Fixture `boiler-observation-cases.json`; ID duplicati, mismatch conteggio, valvole mancanti, dati vecchi/futuri, PDC attiva o ignota, finestra e bypass |
| Lifecycle M2 | Disabled invariato, observe zero comandi/effetti, control con un solo proprietario, perdita proprietà durante timer, reload/unload cancella tutte le callback, errore persistenza non impedisce stop |
| PDC M3–M4 | Soglie elettriche e termiche esatte; potenza assente; prossima fascia; 599/600 s; target variabile; finestra 119/120 s e chiusura/riapertura; override mentre si attende |
| Transazioni M4 | Due richieste simultanee, timeout TRV/target/HEAT/OFF, conferma già vera prima dell'attesa, evento duplicato/tardivo, cancellazione e riconnessione; nessuna doppia fonte |
| Provenienza M5 | Quiet/fan auto atteso, OFF/preset none atteso, scadenza 75 s, variazione diversa, vero utente, autore assente; scadenza override e protezioni |
| Compatibilità | Configurazioni esistenti senza nuove chiavi; nessuna rinomina entità; Avatto, finestre, inibizione, caldaia nativa e requested state invariati quando disabled |

Le prove temporali usano clock controllato: nessuno sleep reale di minuti. Integrazione HA su ambiente di test, mai entità dell'impianto. Discrepanze tra comportamento attuale e miglioramenti desiderabili sono problemi separati da portare alla review.
