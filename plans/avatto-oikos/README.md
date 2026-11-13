# Pilota Avatto per Oikos

Il fork contiene sia il controllo delle valvole in `custom_components/versatile_thermostat` sia il pacchetto Home Assistant di questa casa. Il pacchetto usa gli identificativi già presenti nell'impianto e si installa separatamente dal componente VTherm.

| Funzione | Sorgente |
| --- | --- |
| Controllo del setpoint Avatto con isteresi a 0,3 °C | `custom_components/versatile_thermostat/avatto.py` e `thermostat_climate.py` |
| Domanda ambiente di Camera Carlo, Bagno Blu e Camera Laura Livia e consenso automatico pilota | `package.yaml`, sezione `template` |
| Avvio manuale Oikos con una testina confermata per 120 secondi | `package.yaml`, automazione `Caldaia - Filtro richiesta vTherm` |
| Timer nativi di avvio e spegnimento | `package.yaml`, sezione `timer` e automazione `Caldaia sincronizza timer` |

Il bypass riconosce un timer di override Oikos attivo o la modalità Manuale, nella quale entrambi i programmi della stanza sono spenti. La testina deve appartenere alla stessa stanza e segnalare riscaldamento. Restano validi i blocchi per finestra, modalità centrale, pompa di calore e assenza di testine attive.

`input_boolean.caldaia_filtro_pilota_avatto` parte spento, anche dopo un riavvio. In questa modalità i sensori Avatto permettono il confronto con lo storico. Accendendolo, la logica automatica esclude dal conteggio di avvio le testine di Camera Carlo, Bagno Blu e Camera Laura Livia quando la rispettiva stanza ha smesso di chiedere calore. Per Camera Laura Livia, il sensore legge la richiesta calcolata da VTherm. L'avvio manuale segue la sua regola anche quando il filtro Avatto è acceso.

I timer `timer.caldaia_attesa_avvio` e `timer.caldaia_attesa_spegnimento` espongono le attese del filtro. L'automazione li sincronizza con le scadenze Home Assistant. I loro eventi non comandano il termostato master. Si possono aggiungere a una scheda Entità di Lovelace.

## Installazione e prova

1. Conservare una copia del filtro caldaia e degli helper attuali. Il pacchetto **sostituisce** l'automazione e gli helper omonimi già creati via UI. Caricare entrambe le copie produrrebbe automazioni concorrenti o entità con suffisso. Il consenso termico, i programmi Oikos, le testine e il master rimangono dipendenze dell'impianto.
2. Inserire `package.yaml` tra i pacchetti Home Assistant, verificare la configurazione e verificare gli ID dopo il caricamento. Controllare che ci sia un solo filtro caldaia e che entrambi i timer risultino `idle` senza richieste.
3. Installare il codice VTherm del fork e abilitare **Controllo Avatto da temperatura ambiente** sul solo radiatore di Camera Carlo. Verificare il passaggio dalla pompa di calore al radiatore sopra il target, la riapertura sotto il target e il ritorno al setpoint minimo una volta raggiunto.
4. Provare in Oikos un override con una sola TRV attiva. Verificare che il timer di avvio parta, che la caldaia attenda due minuti e che alla fine dell'override il filtro torni alle regole automatiche.
5. Confrontare lo storico del consenso Avatto con gli avvii reali prima di abilitare il filtro pilota sulla caldaia. Per interrompere la prova, spegnere il filtro Avatto e deselezionare l'opzione sul termostato.
