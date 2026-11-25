# M2-B - Review indipendente (controllo attivo della caldaia)

Data: 24/11/2026. Revisore: sessione Claude indipendente. Commit revisionato `18b72f0` (diff contro `c16fd0a`). Nessuna modifica al repository, nessun commit, nessun accesso a Home Assistant reale, nessun test HA eseguito. Gli scenari miei stanno nella scratchpad di sessione (`.../scratchpad/rev_m2b/`) e usano il finto HA del worker (`m2b/fake_ha_harness.py`), che non avvia HA.

## Verdetto

L'interblocco di proprietà è corretto: non ho trovato nessun percorso che invii un comando con l'interblocco non valido, né due comandi realmente concorrenti sotto il lock. La politica M1 è intatta e il collector è in parità. I rilievi sono sulla **robustezza dopo i fallimenti**, sui **test HA scritti** e su un **rischio di coordinamento** sul working tree.

Totale: **0 bloccanti, 5 importanti, 8 minori** (separati sotto in certi / plausibili).

I 5 importanti da chiudere prima di attivare `control`: I-1 (richiesta esaurita che non si sblocca mai), I-2 (il budget dei 4 invii si azzera con il flapping), I-3 (ciclo a ritardo zero), I-4 (test HA che falliranno di sicuro), I-5 (traduzioni nel working tree regredite).

## Hash dei file revisionati (SHA-256)

Tutti coincidono con M2-B-ESITO §6, tranne le traduzioni del working tree (vedi I-5: il blob di `18b72f0` coincide con l'esito).

| File | SHA-256 |
| --- | --- |
| heating_boiler_policy.py (M1) | edf7e9007ac44166bfc56a3ad8c74dd6b167af6f4ed2760831d865443c27945b (**invariato, come richiesto**) |
| heating_boiler_command.py | 2380d82fe376389eecf8ac0f51948724ae72bef9a609ec0db57188e2f0441314 |
| heating_boiler_collector.py | 40f35aa5030788300d89dc822d203e9122bb1e0c77aa4449cef5658f2a2fffb8 |
| feature_heating_orchestration_manager.py | f2de9ff9b27004a1475d1de9ce760cfd3fa969be1562aaaa235f688a5f6df033 |
| __init__.py | 81d99068942cdae525fa75426b43796de776bbeae05c4640aff1c56493456e59 |
| config_flow.py | bb20aa3edcf90bf769e52827ff29e4017819c7e867ce5d02acafc79f46f95a98 |
| config_schema.py | 3a59277c8352682023fea531ae6691679009d7332910d9553cfdec72683d0308 |
| const.py | 3be3793ae06d5202ea4a0f495b76d388d2cda15e797d85aa980625a5f07ba0e2 |
| sensor.py | 1fbcf2be61e9d7f5bf1bc68fb7cab514e5f502f41090d9facc38d03593e53121 |
| translations/en.json (blob 18b72f0) | 5b4febb47b608406fcdc832dab41d4ab8ca56fcb20de52718c01b98934365eb1 |
| translations/it.json (blob 18b72f0) | 2162e5bb23093a1d0b8db51d01af97f9fc5a22503d9c21cc0a8cad156987d9cf |
| translations/en.json (working tree, 24/11 sera) | 72c3ceb74ef5792148a8f4453dd7e510b350b32e5d7d87025ddfd187dcf2cf81 (diverso) |
| translations/it.json (working tree, 24/11 sera) | 16e647150d2502e76f6d9478055176adb8fb42d6b13df6fdea2cdb0e0cc80208 (diverso) |
| tests/test_heating_boiler_command.py | 03dcea1b9b9941f75b39e86c1798d4883341b5a16edb1d75ab1d546e9cdebf75 |
| tests/test_heating_boiler_collector.py | 5b1675cae7124e860e462f96c43678b34421dfc546314e7404cefddd21566182 |
| tests/test_heating_orchestration.py | 4c649524588b22cd0775a23526238492f0c05d9a60793281720f3e19f01547df |
| m2b/fake_ha_harness.py | ea0bb5f59669aef5e8d0ce92914bfa8562b5095f551e37560497409ce4587ddf |
| m2b/fake_ha_scenarios.py | 96d7c7af304935e45402f1ca200aeadd49570c63bf6a9cf3aeeb5fbe4921318b |
| m2b/mutate_command.py | 8e0d4b21734ed877319cfb301866170b611eff53bf537474e22cc516a4df469b |

## Verifiche eseguite

- `pytest ... tests/test_heating_boiler_command.py tests/test_heating_boiler_collector.py tests/test_heating_boiler_policy.py` (comando richiesto): **456 passed, 1 skipped**, come atteso.
- `python -I plans/.../m2b/fake_ha_scenarios.py`: **19/19 scenari ok**.
- Parità del collector: il diff di `heating_boiler_collector.py` tocca solo configurazione, diagnostica 1.1 e firma di pubblicazione; **nessuna riga della logica `raw`** cambia. La parità del Jinja non l'ho rieseguita perché lo script scrive un JSON nel repository (vietato dal mio incarico): vale la lettura del diff più i 456 test.
- Scenari miei sul manager reale con il finto HA (A, B, C, D, E, F, vedi sotto).
- Lettura statica dei sorgenti HA 2026.10.4 nel venv `vtherm-ha-test` (core.py, helpers/storage.py, exceptions.py).

## Sicurezza dell'attuatore: esito dell'analisi

Un comando può partire solo da `_async_send_locked` (manager:890), lanciato solo da `_evaluate` (manager:816). Prima della chiamata al servizio passano tre controlli indipendenti:

1. `_can_send(epoch)`: epoca corrente, `_running`, non `_stopped`, non `hass.is_stopping` (manager:735, 892, 938, 962).
2. Ricalcolo con clock nuovo e `command_step` con interblocco riletto dagli stati (manager:896-915). Un invio che non coincide più con la decisione è abbandonato senza consumare tentativi.
3. Ultima rilettura della proprietà dopo l'attesa del salvataggio e controllo che il record sia ancora `sending` con lo stesso `send_id` (manager:941-949).

Interblocco, caso per caso (letto nel codice e provato con scenari):

- Controller esterno `on`, `unknown`, `unavailable`, assente, lista vuota: `evaluate_ownership` blocca (command.py:297-306). Provato dagli scenari s05 e s06.
- Modalità diversa da `control`, `control_confirmed` falso: `parse_orchestration_config` (collector.py:398-401) fa fallire la config; il manager resta `disabled` con `state=error`. `control` non è raggiungibile da nessun default o migrazione.
- Generazione vecchia, unload in corso: guardia di epoca su ogni callback e task (manager:487-500, 735).
- Master `unknown`/`unavailable`/non `off|heat`: `BLOCK_MASTER_UNKNOWN`, nessun invio.
- Shutdown di HA: `step` con `shutting_down` annulla tutto (command.py:476-483) e `_can_send` blocca.
- Un solo comando in volo: `asyncio.Lock`; un `task.cancel()` sul precedente non libera il lock finché il precedente non è uscito (verificato in s08: `heat` annullato prima che parta `off`).
- Nessun secondo percorso di servizi: l'unica `async_call` è manager:955, `climate.set_hvac_mode`.

I rischi veri riguardano **cosa succede dopo un fallimento** e sono in I-1, I-2, I-3, M-1.

---

## Rilievi importanti

### Difetti certi

**I-1. Una richiesta esaurita non si sblocca mai, nemmeno dopo un'indisponibilità del master. La caldaia può restare in `heat` senza richiesta.**

- Dove: `heating_boiler_command.py:476-483` (ramo "non proprietario" tocca solo i record `is_active`, non quelli esauriti), `:485-487` e `:445-457` (lo sblocco esiste solo per «master uguale al modo richiesto» o «azione M1 diversa»); manager:742-746.
- Scenario provato (F): master in `heat`, azione `off` per `no_valid_radiator`, 4 `off` falliti (servizio in errore o master che non conferma) -> `command_failed`. Poi il master va `unavailable` per 40 s (`blocked_master_unknown`) e torna `heat`. Dopo altri 10 minuti: `control_status = command_failed`, 4 invii totali, azione ancora `off`. Nessun nuovo tentativo, finché non cambia l'azione M1 o non c'è un riavvio/reload.
- Perché è grave: la specifica dice «finché il master non cambia stato osservato». Un'indisponibilità del master è proprio il caso in cui gli `off` sono falliti, e il ritorno da `unavailable` a `heat` è un cambio di stato osservato che la deviazione §8.6 considera «ramo morto» perché pensa a soli due modi. In una avaria di comunicazione di 10 minuti la caldaia resta accesa senza domanda per ore. La baseline invece riprovava ogni 15 s.
- Correzione: (a) nel ramo non proprietario di `step`, o nel manager quando il master esce dai modi noti, trasformare una richiesta esaurita in `cancelled`/`superseded`, così il ritorno del master riparte con budget nuovo (con il rate limit di I-2); (b) per l'`off`, aggiungere una sonda lenta (un invio ogni 30 min mentre `command_failed` e l'azione è `off`) oppure un avviso persistente; (c) test puro e scenario che coprano «esaurito, master unavailable, master di nuovo heat».

**I-2. Il budget dei 4 invii si azzera con il flapping dell'azione verso `none` (limite dichiarato in §9, qui quantificato). Il backoff 60/120/300 s non protegge.**

- Dove: `command.py:442-444` e `:448-449` (supersede per azione diversa, anche `none`), `:385-388`/`:494-496` (nuova richiesta con tentativo 1 immediato), `:354-363`.
- Scenario provato (B): master `off`, richiesta manuale che commuta `on`/`off` ogni 2 s con servizio sempre in errore: **11 invii di `heat` in 20 s** (invece di 4 in ~8 minuti). Scenario E: master `heat`, conteggio valvole che oscilla 0/3 con servizio in errore: **9 invii di `off` in 16 s**. Il ritmo è quello dell'ingresso che oscilla; per `off` non c'è pausa M1 che lo limiti.
- Correzione: ricordare per ogni modo `failures`/`next_allowed_at` anche dopo un supersede da `none`; azzerare solo su conferma, su comando opposto realmente inviato, o dopo una finestra (per esempio 15 min senza richieste). Test puro: `heat` fallito, azione `none`, azione `heat` di nuovo -> nessun invio prima di `retry_at`. È la decisione che §9 chiede al coordinatore; la mia raccomandazione è **sì, serve la memoria**.

**I-3. Ciclo di valutazione a ritardo zero nel percorso «invio abbandonato».**

- Dove: `feature_heating_orchestration_manager.py:912-915` (`async_request_evaluation()` dopo ogni abbandono); analogo ai rami `:943` e `:990`.
- Scenario provato (A): se il ricalcolo dentro il task dà sempre un'azione diversa da quella della valutazione che l'ha lanciato, `evaluate -> launch -> abandon -> request -> evaluate` gira a ogni giro del loop: **400 chiamate a `evaluate_boiler` in 200 giri**, senza invii. È esattamente il caso del test HA `test_control_revalidates_with_a_new_evaluation_before_the_send` (tests/test_heating_orchestration.py:1017) e dello scenario s11 (il worker lo chiude con soli 8 giri di loop e non lo vede). In produzione serve un disaccordo persistente tra due letture degli stessi stati, improbabile ma non escluso (clock diverso, ciclo M1 che avanza), e il costo è un loop caldo del processo HA.
- Correzione: nel ramo di abbandono non chiamare `async_request_evaluation()`; il prossimo evento, il timer M1 o il poll a 15 s ridecidono (la funzione `_evaluate` ha già tutto). In alternativa un limitatore (una sola richiesta per secondo). Aggiungere uno scenario che conti le valutazioni.

**I-4. Test HA scritti che falliranno di sicuro (o non provano nulla).** Non eseguiti, solo lettura statica.

1. `tests/test_heating_orchestration.py:111-121`, fixture `service_calls`: `patch.object(hass.services, "async_call", spy)`. In HA 2026.10.4 `ServiceRegistry` ha `__slots__ = ("_hass", "_services")` (`homeassistant/core.py:2625`): `setattr` su un'istanza senza `__dict__` solleva `AttributeError: ... attribute 'async_call' is read-only`. Fallisce in setup **ogni test che usa la fixture**: righe 208, 217, 254 (6 parametri), 320, 1115 (9 test in totale). La fixture è ereditata da M2-A e la review M2-A non l'aveva notata. Correzione: `patch("homeassistant.core.ServiceRegistry.async_call", new=spy_unbound)` a livello di classe (con `self`), oppure `async_mock_service` e un listener sull'evento `call_service`.
2. `:1183-1189`, `test_options_flow_validates_the_external_controllers`: per `[MASTER]` si aspetta `ho_duplicate_binding`, ma `MASTER` è `climate.*`, dominio non ammesso per i proprietari: `_check_entity_id` (collector.py:274) fallisce prima con `ho_invalid_domain`, prima del controllo duplicati (collector.py:396-408). L'asserzione `error in result["errors"].values()` fallisce. Per provare il duplicato usare un consenso (`input_boolean.consent_a`) come proprietario.
3. Asserzione debole `:324` e `:340`: `hass.bus.async_listen("call_service", lambda ...)` con una lambda non `@callback` gira nell'executor; `seen == []` è quasi sempre vera anche se un servizio fosse chiamato. Usare `async_capture_events(hass, "call_service")`.
4. `:1017` `test_control_revalidates...`: vedi I-3. Con la correzione di I-3 va aggiunta l'asserzione sul numero di valutazioni.
5. Tempi (non verificabile staticamente): `Clock.advance` fa avanzare l'orologio del manager di `seconds` e fa scattare `async_fire_time_changed(fire_base + elapsed)`. I timer one-shot sono armati in istanti dell'orologio del manager che parte da `real0` della fixture; l'ipotesi che siano sempre scaduti per `fire_base + elapsed` regge solo se `fire_base >= real0`, cosa vera per costruzione, ma i test sulle scadenze a 59/60/61 s sono fragili. Vanno eseguiti prima di fidarsene.

**I-5. Working tree: le traduzioni sono state riscritte da un altro worker e hanno perso parti di M2-B.**

- Il blob di `18b72f0` è corretto e completo (selettore `heating_orchestration_mode` con `control` in `en` e `it`, passo di conferma, errori, niente `ho_control_not_available`; hash uguali a quelli dell'esito). Il working tree, durante la mia review, è cambiato (`git status` mostra `M translations/en.json` e `it.json`, non per mano di M2-B): il selettore **non ha più** l'opzione `control`, ricompare `ho_control_not_available`, `ho_required` torna a «observe mode» e i passi `heating_orchestration_control_confirm` cambiano ordine. `git diff 18b72f0 -- custom_components/versatile_thermostat/translations/` mostra 53 righe cambiate per lingua.
- Effetto: se quel working tree viene committato, in options flow l'opzione `control` compare con l'etichetta grezza «control» e l'errore ricompare.
- Correzione: il coordinatore chiede al worker PDC di rifare le sue aggiunte sopra il blob di `18b72f0` (o di fare un merge a mano). Nessuna azione mia: non tocco quei file.

---

## Rilievi minori

### Difetti certi

**M-1. Annullare l'`await` del servizio per un supersede verso `none` è inutile e a mezzo servizio.** (`manager:864`, scenario C)

`_apply_step` cancella il task di invio anche quando la richiesta è sostituita da `none` (non solo dal comando opposto). Scenario C: `off` in volo, il conteggio valvole torna valido, azione `none/maintain` -> record `superseded` e **servizio annullato a metà** (`CancelledError` dentro l'handler del master). Il motivo per cui si cancella è l'ordinamento verso un comando opposto, che con `none` non esiste. Correzione: cancellare solo per sostituzione con il modo opposto e per perdita di proprietà (come chiede la spec); con `none` lasciare finire il servizio e ignorare l'esito per `send_id`.

Nota sul rischio di cancellare un `await` a metà (richiesto): l'handler di `climate.set_hvac_mode` riceve `CancelledError` in un punto qualsiasi. Gli handler HA sono pensati per questo (HA li cancella allo shutdown), ma se l'integrazione del master implementa `set_hvac_mode` sincrono (l'implementazione di default di `ClimateEntity.async_set_hvac_mode` lo esegue nell'executor) la cancellazione scollega solo il future e il thread **continua**: un `heat` annullato può completare **dopo** l'`off` successivo. Non è un comando concorrente sotto il lock del manager, ma lo è sul dispositivo. Il danno si autocorregge (il master osservato `heat` non conferma l'`off`, che scade a 60 s e riprova), quindi lo classifico minore. Non so di che integrazione sia `climate.termostato_ingresso` (inventario: preset `hold/program`).

**M-2. Nessuna tolleranza dopo che i proprietari esterni diventano tutti `off`.** (command.py:297-309, manager:738-746, scenario D)

Scenario D: proprietario `on` per 60 s, l'ultimo passa a `off`: il comando parte nella prima valutazione successiva, senza attesa. L'assestamento di 30 s è già `latched` perché è stato soddisfatto mentre il controller era bloccato (lo scenario s05 lo assume). Un'istanza della vecchia automazione già in corso (`mode: single`, in un'azione lunga) è fermata da `automation.turn_off` di default (`stop_actions: true`), quindi il rischio è basso, ma una pausa di una valutazione (>= 15 s) dopo l'ultima transizione verso `off` dei proprietari elimina il caso. Va nel runbook comunque.

**M-3. `control_status = pending` pubblicato prima che l'invio sia lanciato o abbandonato.** (manager:806 e 816, command.py:500-509)

`_publish_result` consuma `step.control_status`, che vale `pending` quando `send` è valorizzato, anche se il record ancora non esiste (`command_status = null`) e anche se poi l'invio sarà abbandonato. Se l'abbandono non cambia nulla, il prossimo valore uguale non è ripubblicato (stessa firma): per fino a ~25 s Oikos vede `pending` senza comando. Cosmetico.

**M-4. `SEND_STALE_AFTER_S = 35` coincide con il massimo reale 5 s (salvataggio HEAT) + 30 s (servizio).** (command.py:66)

Con un servizio che termina al limite, la valutazione dichiara `send_lost` (record `failed`, ritentativo tra 60 s) proprio quando il servizio sta per rientrare con successo; l'esito tardivo è ignorato (`on_service_completed` su `failed`). Si autocorregge quando il master viene osservato nel modo giusto (conferma tardiva dal ramo `failed`, command.py:445-447). Soglia consigliata: 40 s.

**M-5. Dopo un riavvio una richiesta `unconfirmed` resta `unconfirmed` anche se il master è già nel modo richiesto.** (command.py:423-431)

È la scelta voluta («il riavvio non conferma mai»), ma lo stato resta per sempre `unconfirmed` e non diventa `confirmed` finché non si invia un nuovo comando. Cosmetico per Oikos; documentarlo.

### Plausibili

**M-6. Assestamento che non si riarma per il reload di un singolo VTherm (deviazione §8.9).** (command.py:336-337, manager:828-831)

Valutato: il caso concreto richiede `ok = False` (select centrale o conteggio `unavailable`) o conteggio 0 mentre il master è `heat`. Il reload di un VTherm non cambia il conteggio se altre valvole sono aperte, quindi il rischio è più basso di quanto l'esito suggerisca, ed è lo stesso che aveva la baseline. Accettabile. Se si vuole chiuderlo: riarmare l'assestamento (solo per `off`) quando una valutazione con `ok=False` segue una valida.

**M-7. Rollback.** Un file `Store` v2 letto dal codice M2-A (versione 1) solleva `UnsupportedStorageVersionError` (sottoclasse di `HomeAssistantError`, `exceptions.py:84`) e M2-A lo gestisce come `store_load_failed`: ripartenza da nuova osservazione con perdita delle ancore del ciclo (riparte la pausa di 600 s). Benigno; da scrivere nel runbook.

**M-8. Sorgente HA 2026.10.4 invece di 2026.11.2.** Non ho potuto leggere i sorgenti 2026.11.2. Nulla nelle API usate sembra a rischio (vedi sotto), ma resta una lacuna.

---

## Semantica dei comandi (richiesta 2)

- **4 invii, attese 60/120/300 s dal fallimento**: corretto (command.py:61-63, 350-363). Ritentativo solo se `attempts == attempt - 1` (command.py:390). Provato da s03.
- **Timeout 59/60 s**: la conferma ha precedenza sul timeout; a 60,0 s vale ancora, a 59,999 s non scatta (command.py:445-452). La conferma è misurata dal **completamento del servizio**, non dall'invio: la lettera della spec («al termine del servizio... entro 60 s») lo consente.
- **OFF senza attesa del salvataggio, HEAT con attesa massima 5 s e `shield`**: corretto (manager:619-638). Scenari s09 e s10.
- **OFF che sostituisce un HEAT**: corretto per l'ordine (s08: `sent:heat, superseded:heat, sent:off`). Il rischio di cancellare il servizio a metà è nel M-1 e nella nota sull'executor.
- **Conferma HEAT tardiva** non conferma l'`off`: corretto (`_advance` confronta con `record.mode`, command.py:445). Il caso «master già `heat` nella stessa valutazione dell'`off`»: il heat non si conferma e il log mostra `superseded:heat` per un heat in realtà riuscito. Accettabile.
- **Ricalcolo prima di ogni invio, ritentativi compresi**: sì (manager:896-915). Con il rischio di I-3.
- **Tre stati distinti** (`service_completed_at`, `pending`, `confirmed`): sì (command.py:405-413, 517-529).
- **`unconfirmed` dopo riavvio**: sì (command.py:423-431, manager:575-578); scenario s07.
- **Assestamento 30 s**: sì, inclusivo (command.py:342). Con la nota M-2 e M-6.
- **Loop di accensioni/spegnimenti**: l'oscillazione fisica `heat`/`off` è limitata dalla pausa M1 di 600 s dopo un `off`. Il rischio di I-2 è un'oscillazione dei **comandi** (non dei risultati) con il servizio in errore.

## Lifecycle (richiesta 3)

- `async_stop` cancella: listener di stato, intervallo, one-shot HA STARTED/STOP, timer di scadenza e di comando, valutazione in coda, task di invio (cancellato e atteso al massimo 5 s) e salvataggi in corso, poi flush finale (manager:512-553). Le callback tardive sono ignorate per epoca (487-500, 675-679, 703-707). Provato da s19 e s17.
- Le callback di unload sono registrate con `entry.async_on_unload(_stop_orchestration)` (`__init__.py`, modifica del commit): una coroutine function è accettata e viene eseguita a ogni unload; lo stop è idempotente e per `entry_id` (`vtherm_central_api.py:258-264`), quindi dopo un reload non tocca il manager nuovo.
- `async_unload_entry` ferma il manager prima di scaricare le piattaforme; se lo scaricamento fallisce ne ricrea uno e lo riavvia (limite dichiarato: sensore non riagganciato).
- `async_setup_entry` ferma il manager se `async_forward_entry_setups` o `async_start` sollevano (anche `CancelledError`).
- Nessun task non tracciato: i task di salvataggio sono in `_save_tasks` e attesi allo stop; il task di invio è in `_send_task`. Unica scorciatoia: `_launch_send` sovrascrive `_send_task` con `None` se il nuovo task finisce in modo eager mentre ne esiste uno vecchio ancora in uscita dalla cancellazione (manager:878); innocuo perché il vecchio è già cancellato.
- Migrazione `Store` v1->v2: `_OrchestrationStore._async_migrate_func(old_major, old_minor, old_data)` con la firma a tre argomenti usata da HA 2026.10.4 (`storage.py:449-455`); HA salva dopo la migrazione. Versione maggiore: `UnsupportedStorageVersionError` (HomeAssistantError) è intercettata dal manager (manager:561). Corretto.

## Politica M1 e compatibilità (richieste 4 e 5)

- `heating_boiler_policy.py`: SHA-256 uguale a quello atteso. Il manager importa solo `evaluate_boiler`, `parse_cycle_context` e i modelli; nessuna modifica.
- Entry senza la sezione: `async_setup_entry` continua a creare il manager solo se `CONF_HEATING_ORCHESTRATION in entry.data` (`__init__.py`). `async_remove_entry` è nuovo e fa `Store.async_remove()` su un file che può non esistere: `suppress(FileNotFoundError)` in HA (`storage.py`), quindi sicuro per le entry esistenti.
- Entry M2-A in `observe`: il parser non porta mai a `control`; `external_owner_entity_ids` assente = `()`, `control_confirmed` assente = falso. Resta `observe`, con la stessa diagnostica più i campi 1.1.
- `control` senza il passo di conferma: impossibile dal flow (`config_flow.py`: `_ho_allow_control` solo nell'options flow, flag scritto solo da `async_step_heating_orchestration_control_confirm`, finalize degrada a `observe` se manca). Dai dati scritti a mano il parser rifiuta (`config_control_not_confirmed`). Il flag è comunque un booleano nei dati della entry: chi scrive i dati a mano può impostarlo (limite accettabile, deviazione §8.3 approvata).
- Traduzioni: complete nel blob `18b72f0` (en e it, config e options: passo di conferma, campo, errori, selettore). Regredite nel working tree (I-5).

## Valutazione delle deviazioni del §8 dell'esito

| # | Deviazione | Valutazione |
| --- | --- | --- |
| 1 | Un `off` sostituisce un heat anche se nella stessa valutazione il master è già `heat` | Accettabile: coerente con la decisione 3. Il log mostra `superseded:heat` per un heat riuscito, da documentare. |
| 2 | Timeout di conferma dal completamento del servizio | Accettabile: segue la lettera della spec. Il massimo osservabile prima del ritentativo diventa ~95 s (5+30+60). |
| 3 | Flag `control_confirmed` | Accettabile, utile; limite: forgiabile da chi scrive i dati. |
| 4 | Stati aggiuntivi | Accettabili, ma vanno nel contratto v1.1 (CONTRATTI §3: `blocked_master_unknown`, `starting`, `settling`). |
| 5 | Richiesta esaurita e riavvio -> `unconfirmed` | Accettabile in sé, ma vedi I-1: il riavvio è oggi l'unico modo di sbloccare. |
| 6 | «Master cambia stato» = conferma tardiva, ramo morto rimosso | **Non accettabile**: ignora l'escursione `unavailable`/`unknown` (I-1). |
| 7 | Heartbeat 25 s, `generation` = pubblicazioni | Accettabile; età massima ~30 s, entro la finestra Oikos -2..+45 s. |
| 8 | Logbook via evento `logbook_entry` | Accettabile: chiavi `name`, `message`, `domain`, `entity_id` uguali a `logbook.async_log_entry`; compare solo se il componente logbook è caricato (come con il servizio `logbook.log` della baseline). |
| 9 | Assestamento latched | Accettabile con la nota M-6 (rischio più basso di quanto dichiarato). |
| 10 | `owner = controller` durante `settling` | Accettabile; Oikos deve leggere `control_status` (CONTRATTI §3 proposto). |
| 11 | Aggiunte fuori perimetro (`async_remove_entry`, ripristino manager, controllo `hvac_modes`, I-3 del collector) | Accettabili. Il controllo `hvac_modes` blocca solo se la lista è non vuota e non contiene il modo (manager:975-981): sicuro. |

## API di HA e rischi con 2026.11.2 (analisi statica, richiesta 7)

Verificato su 2026.10.4: `Store.__init__`/`_async_migrate_func`/`async_delay_save`/`async_save`/`async_remove` (`storage.py`), `UnsupportedStorageVersionError` come `HomeAssistantError`, `hass.is_stopping` come `cached_property` (`core.py:453`), `async_create_task(..., eager_start=True)` (`core.py:788`), `ServiceRegistry.__slots__` (`core.py:2625`, causa di I-4.1), `async_track_point_in_utc_time`/`async_track_state_change_event`/`async_track_time_interval`, `async_listen_once` con coroutine, `EVENT_LOGBOOK_ENTRY`.

Rischi: (1) l'avvio eager di `async_create_task` condiziona la logica di `_launch_send` (task già finito o già sospeso): gestito, ma solo provato con lo stub; (2) il formato dell'evento `logbook_entry` è copiato dal componente `logbook` e può cambiare; (3) `OptionsFlow.config_entry` preesistente nel fork; (4) Python 3.12+ per `asyncio.timeout` e `dataclass(slots=True)`; (5) `Store.async_delay_save` non riarma un timer più vicino già programmato (dichiarato): un salvataggio di transizione a 2 s può slittare fino a 30 s; il salvataggio prima dell'invio resta immediato.

## Cosa non ho potuto verificare

- Qualunque comportamento di HA reale: config flow (schema selector, passi, placeholder), registro entità, recorder, `Store` reale, `async_fire_time_changed`, traduzioni caricate, sensore.
- Il comportamento di `climate.termostato_ingresso` sotto `CancelledError` e se la sua integrazione è sincrona (executor) o asincrona.
- Che i test HA scritti (1.207 righe) falliscano per davvero: i punti I-4.1 e I-4.2 sono certi dalla lettura dei sorgenti; gli altri sono stime. Nessuno è stato eseguito.
- La parità del collector con il template Jinja (non rieseguita: lo script scrive un JSON nel repository).
- HA 2026.11.2: sorgenti non disponibili, solo 2026.10.4.
- I test dei file `heating_pdc_*` (esclusi dall'incarico).
