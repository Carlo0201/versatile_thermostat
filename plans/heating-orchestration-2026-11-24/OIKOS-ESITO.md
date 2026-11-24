# Esito Worker C (Oikos) - 24 novembre 2026

Stato: frontend nativo completato nell'SDK, pacchetti preparati, **non installati**, nessun commit/push. Riferimento: `CONTRATTI.md` §2 e §3.

## Versioni e pacchetti

| Card | Prima | Dopo | Pacchetto |
| --- | --- | --- | --- |
| riscaldamento-stanza | 1.10.1 | 1.11.0 | `dist-cards/riscaldamento-stanza-1.11.0.zip` |
| riscaldamento-centrale | 1.6.0 | 1.7.0 | `dist-cards/riscaldamento-centrale-1.7.0.zip` |
| riepilogo-stanza | 2.5.17 | 2.6.0 | `dist-cards/riepilogo-stanza-2.6.0.zip` |
| salute-riscaldamento | 1.1.22 | 1.2.0 | `dist-cards/salute-riscaldamento-1.2.0.zip` |

Nota: `build-card.mjs` incrementa da solo la patch del manifest dopo ogni build (1.7.1, ecc.). Ho riportato i manifest alla versione effettivamente compilata e documentata nel CHANGELOG prima di impacchettare; le release precedenti (es. 1.10.1) restano in `dist-cards/`.

| Pacchetto | SHA-256 |
| --- | --- |
| `dist-cards/riscaldamento-stanza-1.11.0.zip` | `495e2171184ab46a6a8b09dbd677b1deb207cd7ae7adbe9105492d69ac48295c` |
| `dist-cards/riscaldamento-centrale-1.7.0.zip` | `a4d50919444f8f522e344215892ee9bdf2b3fc67c174f91c2d95a1c366a6b3ad` |
| `dist-cards/riepilogo-stanza-2.6.0.zip` | `34f1f815feddfb690f091eb0c71e192d4c0f5bc4d03519085f698eb19b9f35ed` |
| `dist-cards/salute-riscaldamento-1.2.0.zip` | `2feb5de21d1dd1888ec91a0e2092256293530be88e43bc23a656cb720e4c1b1f` |

## Fonte nativa: API dell'adattatore

Modulo `cards/_shared/nativeBoilerControl.js` (sola lettura, non comanda HA, non legge mai l'helper legacy).

- `boilerSourceConfig(config)` (in `boilerControl.js`): `{boilerSource:'legacy'|'native', boilerDiagnosticEntityId}`. Predefinita `legacy`; `native` solo se esplicita. Nessun rilevamento automatico.
- `readBoilerControl(snapshot, now, connected, source)`: con `legacy` restituisce esattamente la lettura precedente (golden `boilerSource.legacy-golden.json`); con `native` delega a `readNativeBoilerControl`.
- `readNativeBoilerControl(snapshot, entityId, now, connected)`: stessa forma della lettura legacy più `mode, owner, ownership, proposedAction, generation, inputsOk, conditions[], rooms[], excludedRooms[], cooldownUntil`.
  - `error`: `unconfigured | missing | offline | unavailable | starting | error | schema | owner | disabled | stale | future | null`.
  - Freschezza: `evaluated_at` tra -2 s e +45 s; `schema_version` deve essere 1; modo in `disabled|observe|control`; in `observe` il proprietario deve essere `observer`.
  - `ownership`: `external` (observe, il comando resta al controller esterno), `native` (control e `owner == controller`), `none` (control, altro proprietario).
- `nativeSchemaError(attrs)`, `nativeEntityId(value)` (solo `sensor.*`), `nativeRoomStatus(control, climateEntityId)`: stanza idonea, esclusa con codici, non configurata o sconosciuta.

## Mappatura condizioni nativo -> righe checklist

Regole: `na` resta `na`; dato assente, scaduto, di altro schema o stato non valido dà `unknown`, mai `pass`.

| Origine | Riga |
| --- | --- |
| modo/proprietario | `nativeController`: `observe` = stato distinto "in osservazione"; `control` = `pass` solo con proprietario `controller` e dato fresco, altrimenti `fail`; `starting` = attesa; `error`/`disabled` = non soddisfatta; assente/scaduto/schema = non verificabile |
| `conditions[].id` | `master`->boilerMaster, `inputs`->validInputs, `valve_count`->boilerValveCount, `thermal_gap`->boilerThermalGap, `pilot`->boilerPilot, `single_room_pilot`->boilerSingleRoomPilot, `manual_request`->boilerManual, `path`->boilerRoute, `stability`->boilerHold, `cooldown`->boilerCooldown, `consent`->boilerConsent; id sconosciuti -> boilerOtherCondition |
| `excluded_rooms[].reasons` | `not_boiler_participant`->boilerParticipation; `entity_unavailable, hvac_not_heating`->boilerRoomState; `not_ready, not_initialized`->boilerRoomReady; `window_open, window_unknown`->boilerWindow; `inhibited`->boilerInhibition; `safety`; `overpowering`; `avatto_no_demand`->boilerRoomDemand; `temperature_missing/stale/future`->boilerTemperature; `pdc_session_active/unknown`->boilerPumpReleased; `split_not_off/unknown`->radiatorSplitOff; `no_active_valve, unit_unsupported`->boilerValve |
| stanza in `rooms[]` / in nessun elenco | idonea / non configurata |

## Controlli eseguiti (dal 24/11/2026, Node v26.13.0)

| Comando | Esito |
| --- | --- |
| `node --test` su tutti i `*.test.mjs` di `cards/` (esclusi `tmp*` e `screensaver-casa`) | 267 test, 267 passati, 0 falliti. Include non regressione `legacy` (golden), adattatore nativo, checklist nativa, render dei componenti principali delle quattro card (anche con `connected` assente/offline: nessun `connected is not defined`) |
| `node tools/build-card.mjs cards/<id>` per le quattro card | OK |
| `node tools/pack-card.mjs cards/<id>` per le quattro card | OK, zip sopra |
| `node --test preview/heating-checklist.test.mjs` (Chrome headless, dopo la build) | 12 scenari passati: legacy 4 (2 temi x 2 larghezze) e nativo 8 (osservazione/controllo x temi x 390/1280 px), nessun overflow, nessuna eccezione |
| `node --test preview/room-journal.test.mjs` | **fallisce**: il mock di `useHass` viene assegnato dopo il caricamento del bundle, che destruttura l'SDK all'import; nel pacchetto 1.10.1 la struttura è identica. Non legato alla fonte nativa; non corretto |
| `node --test preview/ac-override.test.mjs` | **fallisce**: il test naviga a `http://localhost:5180` (server esterno non avviato qui). Non eseguito il controllo |

Sempre in parallelo i test di anteprima non funzionano: usano la porta 5193; vanno lanciati uno alla volta.

## Controllo visivo

Eseguito in modo automatico con il test di anteprima (Chrome headless); le schermate sono in `tmp/heating-checklist-preview/` (nomi `<tema>-<larghezza>.png` e varianti native). Ho verificato testo, stati e assenza di overflow, **non** ho ispezionato a occhio le immagini nella cartella. Le fixture native sono `cards/_shared/nativeBoiler.fixture.js`; l'anteprima si apre con `preview/heating-checklist.html?source=native&mode=observe|control`.

## Passaggio a native

Vedi `docs/heating-checklist.md`, sezione "Fonte nativa della caldaia". In sintesi: verificare il sensore (schema 1, `evaluated_at` fresco), impostare in ogni card "Fonte delle decisioni caldaia" = Nativa con l'`entity_id` del sensore; iniziare in `observe` (riga "in osservazione", il comando resta al controller esterno), passare a `control` nel backend; ripristino = tornare a Legacy.

## Dipendenza aperta

L'`entity_id` reale del sensore non è fissato nelle card (campo `boilerDiagnosticEntityId`, deve iniziare con `sensor.`). Lo fisserà il Worker A; va poi inserito nelle impostazioni delle quattro card. Fino ad allora la fonte resta `legacy`.

## File modificati

Elenco con SHA-256 allo stato finale. Il lavoro del Worker C/precedente è distinguibile soltanto per data (modifiche dopo le 11:50 del 24/11/2026); le card di riscaldamento e `_shared` contenevano già modifiche non committate di Carlo/Codex, preservate e non separabili con certezza file per file, perché git non ha un commit di base per i file nuovi.

### Toccati da questo lavoro (sorgenti, test, versioni, anteprima, documentazione)

| File | SHA-256 |
| --- | --- |
| `cards/_shared/boilerControl.js` | `9799acb9b64bb2a8573f1fe0bd95315db8255d2db3efdd228aa5ae53590ea550` |
| `cards/_shared/boilerSource.legacy-golden.json` | `e115720839a366a9c38265905ef4633c1a463062dcb0d74b9e31ec24a3b3952a` |
| `cards/_shared/boilerSourceCards.render.test.mjs` | `e95b1634f7858332630e55a899052d58e39b878c3d76cf65be91029440c220e2` |
| `cards/_shared/BoilerSourceFields.jsx` | `92b18e52063c1da51fe145c711e740f626ff609fc5336e0366313877f7ed2452` |
| `cards/_shared/boilerSourceLegacy.test.mjs` | `498b5b9f68945f610b741dd45d466b1869406a2a67b1440ec4d247e4829f4af0` |
| `cards/_shared/cardRender.testkit.mjs` | `ab51afb7b7a03c1e4fa29b3f401fa29ebaafe0e50fcdefa6e5b20145d33b9c8a` |
| `cards/_shared/heatingChecklist.js` | `b6582b50cecd8be356db717461f22d6a1af0132e7f61429eca532ad88cbadfcf` |
| `cards/_shared/HeatingChecklist.jsx` | `b23daca1e95b4ceef5686e970bccb00454a75e29b23094cb775ae920110b4475` |
| `cards/_shared/HeatingChecklist.native.render.test.mjs` | `c81fb708d628d74c1ab04a1396a8c73cfc37736a75b84776f32bfd8de1efca76` |
| `cards/_shared/heatingChecklistTranslations.js` | `0e0c97811ebe40d3577269456133fec2bf7c8ef61bf212bbdf018992da8a6db6` |
| `cards/_shared/heatingStatus.js` | `4da424318c01ff267b43c354730a2b9e16610f8564dc258c6181b4fcf40f5824` |
| `cards/_shared/nativeBoiler.fixture.js` | `c4003d0af4249c032b8ef8c6b829c0fbac663df5a70ec3f90529d47b67a96b01` |
| `cards/_shared/nativeBoilerControl.js` | `0655a3e083dfe747e155f52caab7b2a1b7a063be62d9501b3b2d2f8d5933a9d1` |
| `cards/_shared/nativeBoilerControl.test.mjs` | `388529fcd1e081a61c6d1fda4cf4abba2d664be62acea2b610eee06a296b2618` |
| `cards/_shared/nativeChecklistTranslations.js` | `68c1ed45e0e19362f52cfc52267905b3a84acd8c0468452121f7e7d35bbaad24` |
| `cards/_shared/roomExplanation.js` | `8951184ae29e95e14bc7a1dbc17c7efe82eaebdf6b13659ce92862a7a2354dcf` |
| `cards/riepilogo-stanza/CHANGELOG.md` | `2a1efb5580847501683296096a7f75581457549ea26f097ca20a1241a2aa809c` |
| `cards/riepilogo-stanza/manifest.json` | `8fe2e85fbabd50ea11c4d3a8457b6c329eb7bb30eb75a642074ebd9bb7f251ab` |
| `cards/riepilogo-stanza/src/Card.jsx` | `9317f54b216ff0ea84819458f8808b24b1b1a13a5a83f62a8f51a54a999c1b28` |
| `cards/riepilogo-stanza/src/i18n/en.json` | `49c967f66b6257cd2ea88b171be80b71d1737caab4b207fc43a1c028b7cc878e` |
| `cards/riepilogo-stanza/src/i18n/it.json` | `289325de134fb7a82648b4c46c574794f68b6e70c2b07a28f29d088548cb5dae` |
| `cards/riepilogo-stanza/src/Settings.jsx` | `82830f8a6ac7a2921eaf4fa52cf95a03c587aafe9a5b301bcac92db3bf190ce4` |
| `cards/riscaldamento-centrale/CHANGELOG.md` | `bfd8bd1b6f0b7e6e632758ac4c5a96f671a60b3dbfd2dbc233ec9326efe75604` |
| `cards/riscaldamento-centrale/manifest.json` | `bc8704e63c5e5fa5179e69047c7195d640efe3f3ed7c789fc50b7d389078655a` |
| `cards/riscaldamento-centrale/src/Card.jsx` | `538c710f87b77090b1a202036bd39cf02f039fe15d3884251c1861f64ec31599` |
| `cards/riscaldamento-centrale/src/i18n/en.json` | `ca5c2df90546c79295d5bad41f613fe338450251e55a090aa466bd056bde218e` |
| `cards/riscaldamento-centrale/src/i18n/it.json` | `f8f92554f559e570ac66a6ce763ad32350d0eec4325d128e661ce2300af0998c` |
| `cards/riscaldamento-centrale/src/Settings.jsx` | `718b9b54854b68393fe0e1c902bc037dd8a2b59be876606fee8e93abd434a064` |
| `cards/riscaldamento-stanza/CHANGELOG.md` | `1574783f50038dc591b18561cd6a8fb31d1fa0ed7b1f7fe700efc832ead47cbd` |
| `cards/riscaldamento-stanza/manifest.json` | `b2f095c7eab03b878d125e4e45319bb162f66d1bbe459f02a4ec66b1f4f6bb1d` |
| `cards/riscaldamento-stanza/src/Card.jsx` | `a5c293cd7d8aa70fd539a4511fb555dcb72b95214925a6767e63208991095574` |
| `cards/riscaldamento-stanza/src/i18n/en.json` | `09a6db0db6e38d6e28e0040c892e485bbc065f5aa1f21f1b8204a55ceb5cbc95` |
| `cards/riscaldamento-stanza/src/i18n/it.json` | `9eda3adccc61c710615865bb00461ea900b461279a55523f8a511af6c60040fc` |
| `cards/riscaldamento-stanza/src/Settings.jsx` | `d2bda9b4aabfe3e28eae1baaacd5e64fd7bcbcb728016458d9a504893be22879` |
| `cards/salute-riscaldamento/CHANGELOG.md` | `f77743ce47a5eb038cd20c1d427a9e9646b61b556e0f50681a65a7519c3e89dc` |
| `cards/salute-riscaldamento/manifest.json` | `4047b9efe9a6616d01743b886ca76d82ae959224ab77c3ce46836909d3dcd013` |
| `cards/salute-riscaldamento/src/Card.jsx` | `ed1bb3efbafbc32d0fa5eb0168d999f2df319071679c3ca49145af6a49c3f763` |
| `cards/salute-riscaldamento/src/i18n/en.json` | `778cba07e0999ff0b4f44fe40affd225ca8e0095518e6bf69eee7e006ee33a30` |
| `cards/salute-riscaldamento/src/i18n/it.json` | `2a19b57aa056c1ac907051aba98b7b1a45372e94f38ba3f4826ec6c72ce8aa94` |
| `cards/salute-riscaldamento/src/Settings.jsx` | `93ddb3950107f3b6ef0a4188aa0201b3959b8407e7dc098c7cc95170af07d27a` |
| `docs/heating-checklist.md` | `b679ce31c366506658c2c996855ea41716d9678ebc0175e1412dd4c5d6e272d7` |
| `preview/heating-checklist.mjs` | `70303440dffba6eab9a7ed84189706c153ec2c233157f6feb48f0c46d58fc35b` |
| `preview/heating-checklist.test.mjs` | `ceca982303acec23752cb765c992afd4d99709445e881c88c2661c811f616299` |

### Preesistenti di Carlo/Codex, non toccati (stato git, SHA-256 attuale)

| File | SHA-256 |
| --- | --- |
| `cards/_shared/ProgramControl.jsx` | `f5a04e7c071b0f2efb7f4a301ec41c3f8ffd7f3e3c0cf4387cf048549db25b9a` |
| `cards/_shared/climate.js` | `af8c61f0e394e97fac67323bc6b7070dc54509e224fdfe7dc1edb4c26b997fc1` |
| `cards/_shared/climate.test.mjs` | `1da1a28a43d0a403c1ec02746a3949b05ec2dd9c0c558e09c325e4c2788714f1` |
| `cards/_shared/heatingProgram.js` | `cc2cdec98ed14472dff6bbd5d87d016c5cbe9c027aa122ba81d38c5bda804a11` |
| `cards/_shared/useLiveDashboard.js` | `fe4df206e15ae142f32d1d082d2f04cbcb10172870484fa70beae6f4ccf3412b` |
| `cards/salute-riscaldamento/src/anomalies.js` | `049642b9248635c1780b30f21f564b7fa63f2be2f9adf9a47c26d3a788b23cb3` |
| `cards/salute-riscaldamento/src/anomalies.test.mjs` | `068450fed3f132706506ce9247be37305bdd709c1ff8934b0bee93879a3e0214` |
| `preview/fixtures.js` | `00c9e62381da79e09bec519f045215aea611d8b72f90b84574f8fd27a293337b` |
| `preview/sdk.js` | `490b774bc36fbadbb18bf3b05de5dad32152944f7907be63e1753f892cbd1d26` |
| `cards/_shared/RoomHeatingPanel.jsx` | `817e8f8fae1d9e188cd749edae7dc0cb86ba3817ad3ddb5a9ef8cb9f3cd9e0b2` |
| `cards/_shared/HeatingChecklist.render.test.mjs` | `10a188b7b75352e0d3d294c67aac685efb8a1e3dfc494de4dc9492900f2b5860` |
| `cards/_shared/heatingChecklist.test.mjs` | `af058666e0340fe5f86f582527349daea053b4ca1f18b7f19981f4139a57160e` |
| `cards/riscaldamento-stanza/src/Card.render.test.mjs` | `dc1f20140829710d24d775e694bb7000cd12951a41e6ff6bede0804fc0d00195` |
| `preview/heating-checklist.html` | `ff0949f9b2f81973e707f4fc3115217822c11a4bc00bb3e290a23ec79753ca52` |

Altri file preesistenti non toccati (`ProgramControl.jsx`, `AcOverrideStatus.jsx`, `useAcControl.js`, ecc.) non sono elencati. Le directory `dist/` delle quattro card sono prodotti di build.