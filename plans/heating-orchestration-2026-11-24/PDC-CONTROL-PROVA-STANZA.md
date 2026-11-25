# Futura prova del controllo PDC su una stanza

Preparato il 25/11/2026, Europe/Rome. Documento operativo per review: **non eseguire con la release attuale**. `PDC_CONTROL_AVAILABLE` e `HO_PDC_CONTROL_ENABLED` restano false. Nessuna installazione o azione sull'impianto è stata fatta in questa consegna.

## Condizioni prima di aprire il controllo

1. Eseguire i test Home Assistant del manager, del flow e dell'integrazione in ambiente isolato con la versione compatibile con l'impianto (inventario: HA 2026.11.2 / Python 3.14). Eseguire anche regressioni centrale, Avatto e inibizione. I test offline, anche quando verdi, non sostituiscono questi controlli.
2. Review del diff e dei limiti in `PDC-CONTROL-CODEX-ESITO.md`, inclusi possesso dell'inibizione, perdita di interblocco e semantica degli script esterni. Solo dopo preparare una release separata con i gate aperti, versione e pacchetto propri.
3. Rileggere configurazioni e dipendenze attuali: l'inventario del 24/11 è storico, non garantisce i proprietari di oggi. Confermare che nessun altro attore possa acquisire/rilasciare l'inibizione del radiatore durante la prova.
4. Acquisire un backup supportato di configurazioni/registri e una copia della cartella del componente installato; annotare versione, hash, modalità della centrale e stati dei controller che verranno sospesi. Non modificare `.storage` a mano.
5. Iniziare con Camera Carlo soltanto, con split e VTherm PDC OFF confermati, nessuna transazione o override attivo e nessuna inibizione preesistente. Non provocare accensioni per una prova UI. Se una condizione è ignota, rinviare il passaggio.

## Collegamenti di Camera Carlo (da riconfermare)

| Funzione | Riferimento noto |
| --- | --- |
| Radiatore | `climate.trv_vtherm_camera_carlo` |
| Split | `climate.camera_carlo_condizionatore` |
| VTherm PDC | `climate.camera_carlo_pompa_di_calore` |
| Sessione condivisa con caldaia | `input_boolean.camera_carlo_pdc_in_gestione` |
| Override | `input_boolean.camera_carlo_pdc_override` |
| Minimo setpoint split / bypass preset | 16 °C / attivo |

Valvola fisica, temperatura, potenza, finestra, automatico, prossimo stop e programmi vanno riletti dalla configurazione attuale. Non ricostruirli dai nomi. Le soglie M3 restano invariate.

## Proprietari e ingressi esterni da migrare

Elenco candidato tratto da `baseline/index.json` e inventario: prima della prova leggere e verificare ogni elemento e tutti i suoi chiamanti.

| Entità candidata | Trattamento prima del passaggio |
| --- | --- |
| `automation.camera_carlo_scelta_radiatore_o_pompa_di_calore` | Disattivare dopo aver annotato lo stato; includere nell'interblocco |
| `automation.camera_carlo_protezioni_override_condizionatore` | Disattivare soltanto quando il fork sostituisce le protezioni; includere nell'interblocco |
| `automation.camera_carlo_override_manuale_condizionatore` | Disattivare per evitare il vecchio avvio/sync dell'override |
| `automation.camera_carlo_fine_override_condizionatore` | Disattivare per evitare il vecchio ritorno automatico |
| `script.camera_carlo_pdc_override_avvia` | Migrare o scollegare i chiamanti legacy; dichiarare nell'interblocco se scrittore concorrente |
| `script.camera_carlo_pdc_override_sincronizza` | Migrare o scollegare i chiamanti legacy; includere nell'interblocco |
| `script.camera_carlo_pdc_riprendi_automatico` | Migrare o scollegare i chiamanti legacy; includere nell'interblocco |

**Uno script `off` è inattivo in quell'istante, non disabilitato.** `script.turn_off` non impedisce nuove invocazioni: non basta per migrare i pulsanti Oikos o altri chiamanti. Questa consegna non modifica tali pulsanti. Se non è possibile escludere i vecchi ingressi senza perdere i comandi manuali necessari, la prova è bloccata finché l'adattamento è pronto.

Profilo comfort e registri ciclo/comandi possono restare esterni soltanto dopo aver confermato che non scrivano modo HVAC, temperatura PDC, sessione o inibizione. La caldaia mantiene il proprio controller attuale.

La prima prova non deve ignorare Camera Diego Luisa: configurare anche i suoi ingressi e mantenerne il controllo nel fork **bloccato dall'effettivo proprietario legacy**, così Camera Carlo ne legge split e sessione esterni. Non usare un'entità fittizia per bloccarla. Il codice ora usa il managed legacy per le stanze bloccate, anziché assumere che la propria transazione locale idle significhi assenza di sessione.

Questo non crea una prenotazione atomica condivisa con le automazioni legacy. Durante la prima prova sospendere, con modalità approvata e ripristinabile, le nuove partenze automatiche PDC di Diego Luisa; preservare la gestione del suo radiatore. Annotare i relativi helper/stati prima di modificarli e verificare che lo split sia OFF. Se non si può garantire questa esclusione, la prova su una sola stanza è bloccata fino a una strategia di migrazione condivisa. Le chiamate manuali esterne restano possibili e devono essere osservate; non considerare la prenotazione interna del fork un blocco di SmartThings o dei telecomandi.

## Sequenza della prova (solo dopo autorizzazione al passaggio reale)

1. Sospendere gli attori legacy identificati e i relativi chiamanti. Verificare che ogni proprietario configurato risulti esattamente `off`; unknown/unavailable/assente bloccano.
2. Configurare la stanza completa, la lista esplicita dei proprietari e confermare il control nella release dedicata. Mantenere il controllo caldaia nel suo modo corrente.
3. Lasciare avvenire una richiesta naturale. Verificare: prenotazione unica, possesso persistito, sessione helper ON confermata, radiatore inibito e fermo, setpoint PDC confermato, poi HEAT. Non interpretare HEAT come prova di compressore attivo.
4. Sul ritorno verificare prima VTherm PDC e split OFF confermati, poi rilascio dell'inibizione posseduta, conferma del rilascio e sessione OFF. Un timeout non permette di riaprire il radiatore.
5. Registrare orari, transazione, decisioni, conferme e autore disponibile. In questa consegna lo storico eventi generale e i servizi globali di sospensione non sono completati; non considerarli strumenti di arresto già disponibili.

## Criteri di arresto e ripristino

Arrestare la prova in caso di doppia fonte, HEAT senza arresto radiatore confermato, perdita del possesso, helper persistente errato, store illeggibile o differenza di politica non spiegata.

Se il fork è raggiungibile e conserva l'interblocco, completare il ritorno sicuro prima di disabilitarlo. Se occorre fermare i dispositivi manualmente, richiedere OFF a VTherm PDC e split e verificarlo su entrambi; un esito positivo del servizio non basta. Se non confermano OFF, mantenere il radiatore inibito e sospendere il ripristino automatico: serve risolvere la comunicazione, non liberare il radiatore alla cieca.

Solo con PDC OFF confermate: riportare l'orchestrazione PDC a disabled, verificare che il manager non invii più comandi e rilasciare esclusivamente l'inibizione di cui è documentato il possesso. Ripristinare i chiamanti UI e i controller legacy negli stati annotati prima del test. Se il nuovo codice non si carica, ripristinare la cartella del componente salvata e riavviare con il piano di rollback approvato; non riattivare due controller contemporaneamente e non cancellare il file di stato per forzare un nuovo ciclo.

## Aggiornamento del 25/11/2026 (dopo la review e le correzioni T1–T7)

Solo codice e test locali; gate ancora chiusi, nessuna installazione.

- **Proprietari legacy e protezioni.** Un proprietario dell'interblocco `on` blocca l'intero fork (congelamento totale). Un proprietario `unavailable`/`unknown`/assente è tollerato 30 s; poi, e solo con una sessione in corso della stanza, passano **solo** gli OFF di sicurezza a VTherm PDC e split. Se l'automazione «protezioni override» viene lasciata attiva e nell'elenco proprietari, il control non parte mai finché è `on`; se viene spenta, le protezioni dipendono dal fork. Decidere prima della prova e annotarlo.
- **Stato salvato non utilizzabile.** Il fork si ferma, pubblica `error` e crea la notifica persistente `pdc_store_unreadable`. Non cancellare il file di stato per forzare un ciclo. Procedura: verificare a mano che split e VTherm PDC siano OFF e che il radiatore non sia inibito, poi ripristinare il file di stato da backup. Un timestamp salvato nel futuro (orologio non ancora sincronizzato all'avvio) si ricontrolla ogni 60 s e si risolve da solo quando l'orologio è corretto.
- **Lista proprietari.** In observe il flow non scrive più `[]`: passando a control la dichiarazione dei proprietari va fatta esplicitamente. Le stanze create in observe con la vecchia versione del flow possono avere `[]` salvato: rileggere e rifare la dichiarazione prima di confermare il control.
- **Inibizione.** Se la lettura iniziale del flag è ignota e nessuna chiamata è stata inviata, il fork non rivendica il possesso e non rilascerà mai un'inibizione che non ha posto.
- **Rilascio del radiatore.** I tentativi falliti restano visibili in `last_error` finché la fase dura; dopo 5 tentativi arriva la notifica `<stanza>_pdc_release_stuck`. Non è stato introdotto un tetto automatico: un radiatore non rilasciato resta inibito finché non risponde, per non riaprirlo con uno split ancora attivo.
- **Non coperto.** Test con Home Assistant reale: vedi esito in `PDC-CONTROL-CODEX-ESITO.md` e nel report di sessione. Atomicità della prenotazione con i controller legacy e `_other_view` con helper non ancora scritto: restano i limiti già dichiarati.
