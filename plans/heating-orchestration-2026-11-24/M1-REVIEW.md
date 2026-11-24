# Review Codex di M1 — 24 novembre 2026

Esito: **motore decisionale conforme al template corrente; M1 da chiudere con una revisione mirata prima di M2**. Nessuna modifica ai due file consegnati da OpenCode o all'impianto effettuata durante la review.

## Evidenze verificate

- HEAD ancora `6a819dcc1437fb33ddf8bf0dc2e3aa09e0c80666`; nessuna modifica a file tracciati. Hash di motore e test identici a `M1-ESITO.md`.
- Tutti i 17 file della baseline conservano il loro SHA-256 originale.
- Rieseguiti i test del motore con il venv/stub di OpenCode: **122 passed**, due warning sulle opzioni pytest-asyncio non disponibili nell'ambiente minimo.
- Verifica indipendente con il template Jinja realmente catturato: **27/27 casi e 5.000/5.000 casi aggiuntivi**, seed 104729. Confrontati action, reason, deadline e contesto successivo. Script riproducibile in `review/verify_m1.py`, risultato in `review/M1-verifica.json`. Non è un test contro il runtime completo HA.
- Riprodotto senza stub il comando puro documentato nel test: fallisce in collection perché importa l'`__init__` del componente e richiede `vtherm_api`. Confermata dipendenza dal package stub esterno alla repository.
- Test caldaia/Avatto con Home Assistant e Python 3.14 non eseguiti: l'ambiente locale disponibile non li comprende. Il motore non è importato dal runtime, quindi questa lacuna non dimostra una regressione, ma resta una verifica da completare nell'ambiente compatibile prima dell'integrazione M2.
- Il ricontrollo Black locale non è terminato, anche con un solo worker, ed è stato interrotto. La formattazione dichiarata da OpenCode non è stata convalidata indipendentemente in questa review; rieseguirla nella revisione richiesta.

## R1 — condizioni diagnostiche incoerenti con il percorso effettivo

Priorità: media, da correggere prima di riutilizzare il risultato nelle card. Riferimento: `_build_conditions` in `heating_boiler_policy.py`, righe 507–530.

Riproduzione 1: ingressi validi, master OFF, una valvola, manuale ON, gap 0,1 °C, pilota ON con `pilot_ready=false`. Il motore propone correttamente `heat/manual`; la diagnostica però espone `thermal_gap=wait` e `pilot=fail`. Questo percorso non richiede né il gap di avvio automatico né il pilota: quelle condizioni devono essere `na`, come cooldown, stabilità e consenso.

Riproduzione 2: stessi dati con master HEAT e manuale OFF. Il motore restituisce correttamente `none/maintain`, ma lascia `pilot=fail`. Anche in mantenimento il pilota di avvio è non applicabile.

Conseguenza: la futura checklist dichiarerebbe un blocco che non impedisce la decisione mostrata. Determinare prima il percorso e rendere le condizioni coerenti con i requisiti realmente applicati. Non cambiare la politica decisionale per farla corrispondere alla diagnostica.

Copertura aggiuntiva richiesta: stanza singola con pilota ON pronto e `pilot_ok=false`. Non confondere il consenso globale pilota con la qualificazione della stanza singola: rendere individuabile quest'ultimo veto nella diagnostica senza applicarlo al percorso normale, che nella baseline non lo richiede.

## R2 — esecuzione dei test puri dipendente da file temporanei esterni

Priorità: media, da correggere per una consegna riproducibile. Riferimento: import a riga 25 di `tests/test_heating_boiler_policy.py` e comando nel docstring.

Il comando documentato con `--noconftest` non basta: importa `custom_components.versatile_thermostat.__init__`, che richiede dipendenze HA. I 122 test passano soltanto aggiungendo a PYTHONPATH uno stub con percorso assoluto sotto Temp, assente dal repository e non ricreabile dal solo comando documentato.

Rendere la suite pura eseguibile con il solo pytest, senza percorsi personali e senza alterare il runtime. Possibile caricamento isolato del singolo modulo via importlib, oppure un runner mantenuto nel repository. Non sostituire globalmente il package VTherm con stub che possano contaminare gli altri test HA. Il comando per l'ambiente completo HA deve continuare a essere supportato.

## E1 — errata nella fixture originale, confermata dalla review

OpenCode ha correttamente segnalato `master_unknown`: l'esito storico conserva `cycle.r=1800`, mentre il template attuale e il motore lo azzerano. Non va modificata la logica per adeguarla a quel risultato storico.

Ho preservato l'originale e aggiunto `baseline/boiler-decision-errata.json`, con hash di origine, vecchio e nuovo valore. È autorizzato applicare questa sola rettifica agli expected nel test. Tutti i 27 casi devono tornare nel confronto integrale, senza esclusione per nome: validare precondizione/hash della rettifica, applicarla a una copia in memoria e confrontare anche tutti gli altri campi.

## Politiche confermate per la migrazione

Le differenze fra percorso singolo, normale e manuale elencate come “anomalie” da OpenCode sono in gran parte scelte della baseline: il singolo dopo 900 s non può richiedere il consenso nativo a due valvole, il manuale scavalca attese/consenso di avvio, il mantenimento non riapplica i requisiti di avvio. Restano invariati anche la qualificazione pilota specifica del solo singolo e il troncamento delle ancore già documentato. Qualunque revisione di queste politiche va separata dalla migrazione.

## Consegna successiva

Incarico `M1-R1-OPENCODE.md`: correggere R1/R2, usare E1, aggiungere test e aggiornare l'esito. Nessun collegamento runtime e nessuna M2 in questa revisione. Il prompt M2 verrà preparato dopo la verifica delle correzioni.
