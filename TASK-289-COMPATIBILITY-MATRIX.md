# TASK 289 – Kompatibilitetsmatris

## Omfattning

Matrisen beskriver DEV-korrigeringen från baslinjen
`fef091ebc35cff1ac385f2971421f3bc9649599a`. Production-referenserna nedan är
de värden TASK 289-beställningen anger; de har inte kontrollerats mot PROD.

| Del | TASK 288 / PROD-referens | TASK 289-kandidat | Kontroll |
|---|---|---|---|
| Recovery-validator | `9201ffc0c36c0ed5119eb0af94a9bd2dadeb49b33525d7ca8162752ec317304e` | Samma bytekod och SHA-256 | Administrationskoden, startup-preflighten och wrappern använder samma pinne |
| TASK 287-administrationskod | `dd8c2b03dd921517b1a2504d819699793f7de33a02688c5fe2ceb60e1fbb2fd8` | Samma bytekod och SHA-256 | Behåller `O_NOATIME`, skyddad filöppning, återanvändning av redan låst auditkatalog och fail-closed-låsning |
| Hashbunden recovery-review | `recovery-review-<journal-sha256>-<review-sha256>.json` | Validatorn och adminrutinen accepterar och verifierar journal- och reviewhash | Testar korrekt format och avvisar felaktigt format |
| Historisk review | TASK 267:s historiska journal och review finns kvar | Bevaras oförändrade när en ny hashbunden review används | Validatorn kontrollerar historisk reviewhash mot pinnen |
| Arkiverad incidentjournal | Journalen ska vara arkiverad; aktiv `deployment-state.json` saknas | Validatorn avvisar aktiv eller ofullständig journal och verifierar arkivhash | Recovery-ACK måste matcha journal, manifest och addendum |
| Recovery-ACK | `a5ceaa2a5a3fc8ffd93092d6f7e8126b6cc631dbedcf818d1404d53cb6b1d74e` | TASK 267:s version 2-ACK med addendumstöd | Befintlig kedja valideras utan att återaktivera journalen |
| Startup-preflight | Delad validator och preflight måste vara samma granskade version | Hashpinne för `armory-db-preflight.py` uppdaterad | Testerna kontrollerar konsistens mellan källa och pinne |
| Installation | Shared validator och startup-preflight kopieras från releasekällan | Installeraren fortsätter att byta komponenterna atomiskt | Syntetisk installation och rollback täcks av regressionstester |

## Säkerhetsgränser

Validatorn kontrollerar auditkatalogens metadata, lås, journaler, manifest,
ACK-filer, recovery-addendum, reviewfiler, installationsresultat och
aktiveringshändelser. Den historiska Recovery-ACK-kedjan är bevismaterial och
ska inte skrivas om. TASK 287-adminskydden är bevarade byte för byte.

Matrisen bekräftar endast kod- och testkompatibilitet i DEV. Den styrker inte
att PROD:s aktuella filer, journal, ACK, tjänster eller databas fortfarande
har de beställda värdena.
