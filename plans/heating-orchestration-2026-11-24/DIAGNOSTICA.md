# Contratto diagnostico e compatibilità Oikos

Obiettivo: checklist e spiegazione leggono la stessa decisione usata dal controller, senza ricostruire in JavaScript un secondo regolatore. Questo documento definisce il contratto da realizzare progressivamente; non dichiara già disponibili le nuove entità.

## Risultato decisionale

Il motore M1 restituisce almeno azione proposta (`none`, `heat`, `off`), motivo, scadenza UTC/Unix oppure null e contesto di ciclo successivo. Il modello deve consentire la successiva aggiunta delle condizioni diagnostiche senza cambiare la politica.

Motivi caldaia baseline da conservare: `no_demand`, `maintain`, `no_valid_radiator`, `manual`, `normal_wait`, `normal`, `awaiting_consent`, `single_wait`, `single`, `thermal_wait`, `data_or_central`, `data_missing`, `master_unavailable`.

La diagnostica runtime M2 aggiungerà: `schema_version`, `evaluated_at`, `mode` disabled/observe/control, `owner`, `proposed_action`, `reason`, `deadline`, `commanded_mode`, `confirmed_mode`, `last_command_at`, `last_confirmation_at`, `valid_valve_count`, stanze/valvole valide, condizioni e ultimo errore. Dichiarare separatamente se la conferma rappresenta soltanto il modo del master; non etichettarla come stato del bruciatore.

Condizione strutturata: `id` stabile, `status` in pass/fail/wait/unknown/na, `reason_code`, `actual`, `expected` e `deadline` facoltativa. Numeri con unità esplicita; campi assenti/null per valori ignoti, mai NaN/Infinity nel JSON. Valori e timestamp devono riferirsi allo stesso snapshot/numero di valutazione. Le etichette tradotte appartengono alla UI, i codici al controller.

Condizioni minime caldaia: controller/proprietario, validità ingressi, centrale, partecipazione stanza, termostato pronto/inizializzato, freschezza temperatura, finestra/bypass, inibizione, sicurezza, potenza, split spento, richiesta fisica, numero valvole, gap, percorso normale/singolo/manuale, stabilità, pausa, consenso previsto. Le condizioni di avvio non applicabili al mantenimento vanno indicate na, non fallite. Una stanza non valida deve esporre il blocco specifico, non soltanto “non idonea”.

Per M3–M5 aggiungere sorgente richiesta/effettiva, transazione e fase, provenienza override, stato prenotazione e condizioni PDC. Separare diagnostica di avvio, protezioni in marcia e ultimo fallimento storico.

## Fonti Oikos attuali da preservare durante la transizione

| Fonte | Uso attuale |
| --- | --- |
| `input_text.caldaia_controllo_diagnostica` | JSON compatto v1, aggiornato dal controller esterno |
| `input_text.caldaia_controllo_ciclo` | Ancore delle attese caldaia |
| `timer.caldaia_attesa_avvio`, `timer.caldaia_attesa_spegnimento` | Countdown nella centrale |
| `sensor.<stanza>_pdc_diagnostica` | `codici|limite_W|stabilita_secondi` |
| `input_text.<stanza>_pdc_ciclo` | HEAT/OFF confermati e target iniziale |
| Flag di gestione, fase e ultimo esito PDC | Stato della scelta della fonte e dei tentativi |
| Logbook con prefisso `OIKOS_HEATING_V1|` | Azione/motivo/autore registrati |

Il JSON compatto caldaia usa `v,t,m,n,e,r,a,d,ok,man,pi,pc,vc`; gli indici `e` seguono l'ordine Camera Carlo, Ingresso, Cucina, Soggiorno, Camera Laura–Livia, Bagno Verde, Bagno Blu, Camera Diego–Luisa. L'attuale reader considera valida la diagnostica per 45 s, con tolleranza futura 2 s e automazione esterna ON. Non basta pubblicare un nuovo JSON con la vecchia automazione OFF: serve un adattamento esplicito del frontend.

Riferimenti attuali nel progetto padre: `oikos-card-starter-main/cards/_shared/boilerControl.js`, `heatingChecklist.js`, `heatPump.js`, `roomExplanation.js`, e `docs/heating-checklist.md` nello stesso SDK. Sono input per la review; OpenCode non deve modificarli nel lotto M1.

M2 esporrà diagnostica nativa del componente con identità stabile. Codex adatterà Oikos affinché selezioni esplicitamente la fonte nuova quando attiva; la vecchia rimane durante observe. Non mantenere due scrittori sugli helper esistenti e non fingere l'automazione esterna ON. La forma e l'entità concrete saranno fissate nella review M2 prima del passaggio all'impianto.

Per il registro, un evento deve distinguere richiesta, comando e conferma e conservare il contesto originale quando presente. Un autore ignoto resta ignoto. Eventi di transizione e fallimento, non un evento ad ogni tick. Diagnostica dettagliata ad alta frequenza da escludere dal recorder con i meccanismi del componente, preservando uno storico utile di stato/motivo.
