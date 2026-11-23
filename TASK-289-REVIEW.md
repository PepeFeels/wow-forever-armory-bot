# TASK 289 – Granskning av recoverykompatibel releasekandidat

## Beslut

**GO för fortsatt granskning av DEV-kandidaten. NO-GO för PROD.** Ingen
PROD-installation, databasändring, tjänstestart, auditarkivsåtgärd eller
GuildOS-åtkomst har utförts.

## Utgångsläge och arbetsområde

Baslinjen är den i TASK 288 verifierade committen
`fef091ebc35cff1ac385f2971421f3bc9649599a`, Git tree
`d34715664e7e9b1356b241636f64803d57388175`. Den ursprungliga delade
arbetskatalogen hade HEAD `b341c30…` och många ändrade och ospårade filer; den
lämnades orörd. Utvecklingen utfördes i den separata rena klonen
`/tmp/wow-armory-task-289`.

## Korrigeringar

- Lade in den granskade validatorn med SHA-256
  `9201ffc0c36c0ed5119eb0af94a9bd2dadeb49b33525d7ca8162752ec317304e`.
- Samordnade validatorpinnen i TASK 287-adminrutinen, produktions-preflighten,
  deployment-wrappern och systemd-startup-preflighten.
- Uppdaterade preflightens hashpinne för den ändrade
  `armory-db-preflight.py`-filen.
- Bevarade TASK 287-adminrutinen byte för byte med SHA-256
  `dd8c2b03dd921517b1a2504d819699793f7de33a02688c5fe2ceb60e1fbb2fd8`.
  Därmed behålls bland annat `O_NOATIME`-läsning, kontroller av filtyp/ägare/
  hårdlänkar, skydd mot samtidiga processer och återanvändning av det redan
  låsta auditkataloghandtaget.
- Validatorn stöder journal- och reviewhashbundna filnamn och den arkiverade
  TASK 267-kedjan med Recovery-ACK version 2 och recovery-addendum. Den aktiva
  journalen behöver inte och får inte återskapas.
- Lägger till regressionsprov för TASK 267-kedjan, TASK 287:s låsbeteende och
  TASK 289:s kompatibilitetspinnar och filnamn.

Detaljer finns i [kompatibilitetsmatrisen](TASK-289-COMPATIBILITY-MATRIX.md).

## Baslinje och ursprungliga PROD-referenser

TASK 288 angav recovery-validator `9201ffc0…`, adminrutin `dd8c2b03…`,
Recovery-ACK `a5ceaa2a…` och tidigare kandidat `fef091eb…`. Adminfilen och
validatorn kunde återfinnas som hashverifierade payloads i befintligt
TASK 287/TASK 278-material. Recovery-ACK-hashen är en beställarreferens; DEV
tester använder syntetiska fixtures och läser inte PROD-arkivet.

## Testning

Hela testsamlingen passerade: **473 PASS, 0 FAIL** med Python 3.14.4,
`discord.py 2.7.1` och Pillow 13.0.0. Den innehåller syntetiska tester för
installation, recovery, rollback, startup-preflight, arkiverad journal,
Recovery-ACK, addendum, TASK 287-lås och hashbundet reviewfilnamn.

Körningen gav befintliga `ResourceWarning`-varningar för öppna SQLite-objekt,
en HTTP 429-stängningsvarning och discord.py:s `DeprecationWarning`. Testerna
kördes endast i den tillfälliga DEV-miljön. De verifierar inte PROD:s
filmetadata, systemd-version, databas eller aktuella service-/processläge.

## Release och gränser

Ny releasecommit, ren checkout och artefakter ska identifieras med fullständiga
SHA-256-värden i leveransrapporten. Releaseidentiteten ersätter den tidigare
kandidaten först efter granskning. PROD kräver ny separat preflight och separata
godkännanden för installation, finalisering och aktivering. Inget i denna
rapport ger sådant godkännande.
