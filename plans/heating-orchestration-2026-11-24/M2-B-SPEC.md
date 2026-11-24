# M2-B — Controllo caldaia: specifica del coordinatore

Versione 1, 24/11/2026. Si basa su M1 (approvato), su M2-A (consegnato, in review: vedi `M2-A-REVIEW.md`), sulla baseline `caldaia_controllo_stabile_delle_richieste.json` e su `IMPIANTO-INVENTARIO-2026-11-24.md`. Le politiche M1 non cambiano.

## Comportamento attuale da preservare (baseline)

- Automazione in `mode: single` con `max_exceeded: silent`. Trigger: cambi di stato degli ingressi, avvio di HA, ricarica delle automazioni e ogni 15 s.
- La sequenza è:
  1. raccolta `raw`;
  2. calcolo `plan`;
  3. scrittura del ciclo;
  4. **nuova raccolta e nuovo calcolo**;
  5. pubblicazione della diagnostica;
  6. comando.
- Il comando parte soltanto se sono `off` le tre automazioni legacy (`caldaia_filtro_richiesta_vtherm`, `caldaia_sincronizza_timer`, `caldaia_avvio_immediato_richiesta_manuale`), lette come condizioni sia all'inizio sia subito prima del comando.
- Il comando è `climate.set_hvac_mode` (`off`/`heat`) su `climate.termostato_ingresso`, seguito da una voce di logbook. **Non c'è attesa di conferma**: la conferma è implicita nella valutazione successiva, che rilegge il master.
- Unico proprietario automatico del master: questa automazione (inventario D). Il gestore caldaia nativo VTherm scrive solo `input_boolean.richiesta_caldaia_vtherm`, usato come consenso.

## Requisiti di M2-B

### Modalità e attivazione

- Modalità caldaia: `disabled` | `observe` | `control`, nella stessa sezione di M2-A. La PDC avrà una modalità **separata** (`pdc_mode`, punto 2 del piano); la modalità caldaia non attiva nulla sulle PDC.
- `control` è selezionabile solo dall'options flow della centrale, con un passo di conferma esplicito che elenca le condizioni di attivazione. Nessuna migrazione o default porta a `control`.
- Configurazione aggiuntiva obbligatoria per `control`: `external_owner_entity_ids`, la lista dei controller esterni che non devono essere attivi. Per l'impianto attuale sono `automation.caldaia_controllo_stabile_delle_richieste` e le tre legacy. Validazione: nessuna di queste può coincidere con master, consenso o altri ingressi.

### Interblocco di proprietà (controllato prima di ogni comando)

Il controller è **proprietario** solo se valgono tutte queste condizioni:

1. la modalità è `control` e la entry è caricata;
2. la generazione è corrente e non annullata;
3. ogni entità di `external_owner_entity_ids` ha stato esattamente `off`. Stati `on`, `unknown`, `unavailable` ed entità assente bloccano: la scomparsa del controller esterno non concede la proprietà;
4. il master ha un modo noto, `off` o `heat`.

Se la condizione 3 non è soddisfatta, il controller resta in osservazione con `owner = observer` e `control_status = blocked_external_owner`, senza comandi. Il controller **non spegne né accende** mai automazioni, helper o timer legacy: il passaggio di proprietà è un'operazione del runbook.

Se la proprietà si perde (controller esterno riattivato, modalità cambiata, unload), tutto ciò che è pendente si annulla: conferme, ritentativi, timer. Non si inviano comandi di compensazione.

### Comando e conferma

- Un comando parte solo se l'azione M1 è `heat` o `off` e il master osservato è diverso dal modo richiesto.
- Subito prima del comando: **nuova raccolta e nuova valutazione M1** con un clock nuovo (come la baseline). Si prosegue solo se l'azione è la stessa e l'interblocco vale ancora.
- Servizio: `climate.set_hvac_mode` con `blocking=True`, timeout 30 s.
- Al termine del servizio:
  - `commanded_mode`, `last_command_at` e `command_status = pending` si registrano subito;
  - il comando completato **non** è una conferma;
  - `last_confirmation_at` e `command_status = confirmed` si registrano solo quando lo stato osservato del master coincide con il modo comandato, entro 60 s (listener sul master con fallback al poll).
- Fallimento o timeout del servizio, oppure conferma non arrivata entro 60 s: `command_status = failed`, `last_error`, nuovo tentativo con attesa crescente 60 s → 120 s → 300 s, al massimo 3 tentativi per lo stesso modo richiesto. Dopo il terzo tentativo `control_status = command_failed` e nessun altro comando verso quel modo finché il master non cambia stato o la decisione M1 non cambia. Così un guasto di comunicazione non produce accensioni e spegnimenti ripetuti.
- Un comando `off` dovuto a `no_valid_radiator` ha la precedenza: non viene ritardato da errori di persistenza o di diagnostica.
- Un solo comando in volo alla volta: serializzazione con lock del manager, senza tenere il lock durante l'attesa della conferma.
- Logbook: una voce per comando inviato e una per conferma o fallimento (transizioni, non tick), con un prefisso compatibile `OIKOS_HEATING_V1|` se DIAGNOSTICA lo richiede.

### Persistenza

- Lo `Store` di M2-A aggiunge `command`: `{mode, at, attempts, status}`, con lo schema versionato e la migrazione da M2-A.
- Al riavvio un comando `pending` diventa `unconfirmed`. Non si considera mai confermato: lo stato si rilegge dal master.
- Le scritture di persistenza sono differite e non bloccano mai il comando.

### Riavvio, ricarica, unload

- Con modalità `control` configurata, dopo setup o reload il controller riparte in osservazione. Diventa proprietario solo quando l'interblocco vale con ingressi validi, cioè dopo almeno una valutazione completa con master noto e conteggio valido.
- Su unload/reload si annullano tutti i task, i listener e le conferme pendenti, e le callback tardive vengono ignorate tramite generazione.
- Nessun comando durante lo shutdown di HA.

### Decisioni del coordinatore sulle ambiguità (IDEE-IMPLEMENTAZIONE §2) — prevalgono sul testo sopra

1. **Tentativi.** Al massimo **4 invii totali** per lo stesso modo richiesto: 1 primo invio + 3 ritentativi. Ogni ritentativo parte dopo un fallimento (servizio in errore o conferma assente entro 60 s), con attese di 60 s, 120 s e 300 s. Dopo il quarto fallimento: `control_status = command_failed`, finché il master non cambia stato osservato o l'azione M1 non cambia.
2. **OFF prioritario senza attesa.** Per un comando `off` il record del comando viene accodato in persistenza senza attenderlo: il servizio parte subito. Solo il comando `heat` attende il salvataggio, al massimo 5 s, e prosegue anche se fallisce.
3. **OFF durante un HEAT pendente.**
   - Un'azione M1 `off` **sostituisce** immediatamente un `heat` pendente o in attesa di ritentativo: annulla ritentativi e attese di conferma dell'heat (`superseded`) e invia l'`off` senza aspettare l'esito dell'heat.
   - Una conferma HEAT tardiva, cioè il master osservato in `heat` dopo che è stato richiesto `off`, non viene registrata come conferma del comando `off`. È uno stato osservato normale: la valutazione successiva lo tratta con M1 e, se l'`off` è ancora necessario, il record `off` pendente prosegue con conferme e ritentativi propri.
   - Lo stesso vale in senso inverso per un `heat` che segue un `off` (con le attese M1 di pausa).
4. **Ricalcolo prima del comando.** Subito prima di ogni invio, compresi i ritentativi, si rifanno raccolta, M1 e interblocco con un clock nuovo. Se l'azione non coincide più, il comando viene abbandonato con stato `superseded`.
5. **Tre stati distinti, sempre esposti.** `service_completed_at` (il servizio è tornato senza errori), `command_status = pending` (in attesa di conferma) e `confirmed` (stato del master osservato uguale al comando). Un comando pendente al riavvio diventa `unconfirmed` e il riavvio non lo conferma mai.

### Aggiunte dalla review M2-A (I-7)

- **Assestamento dopo setup/reload.** Dopo setup, reload della centrale o `reload_all_vtherm` nessun comando, compreso `off`, finché non ci sono state almeno 2 valutazioni consecutive valide a distanza di almeno 30 s con la stessa azione M1. Durante il reload le stanze sono transitoriamente non idonee: un OFF proposto in quella fase non è attendibile. Diagnostica: `control_status = settling`.
- **Calcolo separato da pubblicazione e comando.** La valutazione produce un risultato immutabile. Pubblicazione e comando lo consumano, e un errore di pubblicazione non impedisce il comando né la riprogrammazione.
- **Salvataggio prima del comando.** Il record del comando si salva subito prima del servizio, con attesa massima di 5 s. Se il salvataggio fallisce si prosegue lo stesso, registrando `last_error`.

### Diagnostica (CONTRATTI v1.1, aggiunte compatibili)

- `owner` vale `controller` solo con l'interblocco soddisfatto.
- Nuovi attributi:
  - `control_status`: `inactive` | `blocked_external_owner` | `ready` | `pending` | `confirmed` | `command_failed`;
  - `command_status`, `commanded_mode`, `last_command_at`, `last_confirmation_at`;
  - `command_attempts`, `external_owners: [{entity_id, state}]`.
- Lo stato dell'entità resta il motivo M1.

### Fuori ambito di M2-B

- PDC e `pdc_mode` (punto 2 del piano).
- Modifiche Oikos (punto 3).
- Disattivazione di automazioni o helper sull'impianto.
- Rimozione degli helper legacy.
- Timer legacy `timer.caldaia_attesa_*`: in `control` non vengono scritti. I countdown li fornisce la diagnostica nativa (`deadline`); l'adattamento è del punto 3.

## Architettura richiesta

- Nuovo modulo **puro** `heating_boiler_command.py`. Decide se inviare un comando, ritentare, confermare, bloccare o annullare, a partire da:
  - decisione M1;
  - master osservato;
  - stato dell'interblocco;
  - record del comando;
  - clock.

  Usa modelli frozen e nessun import di HA, come M1. Si testa con pytest puro.
- Il manager di M2-A chiama questo modulo ed esegue i servizi. Non deve esistere un secondo percorso di servizi parallelo al gestore caldaia nativo.

## Verifiche

- Test puri del modulo comando:
  - ogni transizione;
  - timeout esattamente al limite (59/60 s);
  - backoff;
  - limite di 3 tentativi;
  - conferma già vera prima del comando;
  - master unknown;
  - perdita della proprietà durante l'attesa;
  - evento tardivo di una generazione vecchia;
  - riavvio con comando pending;
  - priorità di `off`.
- Test HA (scritti, **non eseguiti** per divieto di Carlo):
  - observe senza servizi;
  - control con interblocco;
  - un solo comando;
  - conferma;
  - errore del servizio;
  - unload durante l'attesa;
  - riattivazione del controller esterno durante il control.
- Compatibilità statica con HA 2026.11.2 delle API usate.
- Black 24 sui file nuovi o toccati, tutti i test puri e la review del coordinatore.
