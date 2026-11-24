# Idee e decisioni per l'implementazione del riscaldamento

Data: 24 novembre 2026, Europe/Rome.

Documento di raccolta per chi proseguirà il lavoro sul fork e sulle card Oikos. Distingue le decisioni concordate con Carlo dai punti tecnici ancora da definire. Non certifica funzionalità implementate e non modifica le specifiche esistenti: queste decisioni dovranno essere integrate nelle consegne pertinenti.

## 1. Sospensione generale del riscaldamento — concordata

### Comando e posizione nella card

- Nome: **«Spegni tutto il riscaldamento»**.
- Posizione: card **riscaldamento-centrale**, in una sezione poco esposta, proposta **«Altre azioni»**. Il nome del contenitore è una proposta grafica, non un vincolo tecnico.
- Prima dell'esecuzione chiedere conferma. Testo proposto: «Spegnere il riscaldamento in tutte le stanze, comprese le PDC?».
- Durante la sospensione, mostrare chiaramente **«Riscaldamento sospeso manualmente»** e il pulsante **«Riprendi riscaldamento»**, senza nasconderli nella sezione delle azioni.

### Effetto richiesto

- Spegnere caldaia e PDC e sospendere le richieste di tutte le stanze/radiatori.
- Bloccare nuove accensioni automatiche, anche a seguito di cambi di programmazione.
- Bloccare anche le accensioni manuali delle singole stanze: **serve prima la riattivazione generale**.
- Conservare programmi, temperature e impostazioni; annullare gli override temporanei di accensione.
- Nessuna funzione antigelo da aggiungere per questa sospensione, come richiesto da Carlo. Le eventuali protezioni interne dei dispositivi vanno individuate e descritte senza promettere di disabilitarle.
- Mantenere la sospensione dopo riavvio o ricarica di Home Assistant, fino a un'esplicita riattivazione generale. Nessuna scadenza automatica.

### Conferma dello spegnimento

- Mostrare **«Spegnimento in corso»** finché i dispositivi non hanno confermato lo stato atteso.
- Un servizio completato non dimostra che il dispositivo sia spento.
- Se un dispositivo non risponde, indicare quale; non mostrare l'intero impianto come spento.
- `unknown`, `unavailable` ed entità mancanti non equivalgono a OFF.
- Distinguere lo stato della sospensione dal completamento dei comandi: il blocco delle nuove richieste resta attivo anche se lo spegnimento di un dispositivo non è ancora confermato.

### Ripresa

- Il comando **«Riprendi riscaldamento»** rimuove la sospensione generale.
- Rivalutare ingressi, programmazione e obiettivi validi in quel momento.
- Non ripristinare gli override temporanei annullati né riaccendere automaticamente tutti i dispositivi che erano accesi prima.
- Restano applicabili le normali protezioni, condizioni di avvio e regole di proprietà degli attuatori.

### Observe e passaggio di controllo

- In modalità observe questo comando **non è operativo** e non deve inviare comandi all'impianto o modificare helper/controllori esistenti.
- La futura funzione richiede un percorso autorizzato che copra tutti i controllori attivi e tutti gli attuatori interessati. Il solo OFF al master non impedisce nuove accensioni né spegne le PDC.
- Non presentare il comando globale come disponibile con il solo controllo caldaia attivo, se le PDC o altri percorsi di accensione restano fuori dal suo controllo.
- Definire il comportamento in caso di perdita della proprietà senza introdurre comandi concorrenti o disattivazioni automatiche dei controllori esterni.

### Punti tecnici ancora da definire

- Come rappresentare e conservare la sospensione centrale, senza alterare programmi e setpoint salvati.
- Quali override di accensione annullare esattamente e tramite quali API; distinguere questi dagli altri override.
- Come garantire il blocco in ogni percorso: scheduler, comandi Oikos, servizi HA, controllo nativo, script/automazioni e percorsi esterni eventualmente presenti.
- Come trattare accensioni dal telecomando fisico, dal dispositivo o da applicazioni esterne. L'intento è mantenere la sospensione, ma la copertura reale va verificata: non presumere che tutti questi ingressi siano intercettabili o bloccabili.
- Ordine degli arresti e gestione delle transazioni PDC/radiatore già in corso, senza rilasciare inibizioni altrui o creare riaccensioni durante l'arresto.
- Timeout, tentativi e segnalazioni per spegnimenti non confermati; gestione dei dispositivi che tornano disponibili mentre la sospensione è attiva.
- Contratto diagnostico per stato generale, stato di ogni dispositivo, motivi, origine del comando e ultimo esito.

## 2. Chiarimenti sul controllo caldaia M2-B — da risolvere

Questi sono rilievi emersi nella discussione della specifica, non nuove regole già approvate nei dettagli. Riferimenti: `M2-B-SPEC.md` e `M2-A-REVIEW.md`.

1. **Numero dei tentativi:** chiarire se «3 tentativi» significhi tre invii totali oppure primo invio più tre ritentativi. Rendere coerenti il limite e le attese di 1, 2 e 5 minuti.
2. **Priorità OFF e salvataggio:** specificare come evitare che il salvataggio preliminare, con attesa massima di 5 secondi, ritardi uno spegnimento prioritario.
3. **OFF durante un'accensione pendente:** annullare i ritentativi HEAT divenuti obsoleti e definire la gestione di un'eventuale conferma HEAT tardiva.

## 3. Verifiche future proposte

Da tradurre in verifiche del codice ed eventualmente in test appropriati, rispettando il divieto corrente di test HA e interventi sull'impianto.

- Sospensione con caldaia, radiatori e PDC attivi; conferme parziali o mancanti.
- Cambio di programma e tentativo di accensione di una stanza durante la sospensione.
- Riavvio/ricarica mentre la sospensione è attiva o lo spegnimento è ancora in corso.
- Ripresa con programma diverso da quello attivo al momento della sospensione.
- Dispositivo indisponibile che torna online, oppure conferma tardiva di un precedente comando HEAT.
- Modalità observe o controllo parziale: nessun comando globale eseguibile.
- Transazione di cambio fonte già in corso e perdita di proprietà di un attuatore.

## Ambito di questa raccolta

La creazione di questo documento è stata richiesta da Carlo. Non autorizza installazioni, modifiche all'impianto, esecuzione di test HA, commit o pubblicazioni. Non introduce una pagina di amministrazione Oikos: quella proposta resta rinviata. L'intervento attuale aggiunge solo questo file, senza aggiornare gli altri piani o i log.
