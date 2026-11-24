# TASK 289 – PROD-runbook för framtida separat godkännande

## Aktuell status: NO-GO

Denna runbook är ett planeringsunderlag och inte en körorder. TASK 289 har inte
anslutit till PROD och har inte ändrat databas, tjänster, auditarkiv eller
releasepekare. Den blir aktuell först efter oberoende granskning av den nya
releasecommitten och en färsk skrivskyddad PROD-preflight.

## Nya hashvärden

Fylls i efter releasecommit och artefaktbygge från ren checkout. Hashvärden
från TASK 288 får inte återanvändas som nya installationspinnar.

| Objekt | SHA-256 |
|---|---|
| Releasecommit | redovisas i leveransrapporten |
| Git tree | redovisas i leveransrapporten |
| Releasebundle | redovisas i leveransrapporten |
| Källarkiv | redovisas i leveransrapporten |
| Wheel | redovisas i leveransrapporten |
| `deploy/production_admin.py` | `dd8c2b03dd921517b1a2504d819699793f7de33a02688c5fe2ceb60e1fbb2fd8` |
| `deploy/recovery_audit.py` | `9201ffc0c36c0ed5119eb0af94a9bd2dadeb49b33525d7ca8162752ec317304e` |
| `deploy/production_preflight.py` | `4d525f3c61cb28467c738b2fc985fe3b242c8366b248a3f47167916fc9407bac` |
| `deploy/production_wrapper.py` | `29ef05e6d6a1fd3eca3a0b9692159485d7aa45e0c0954f86acd6511b3162be23` |
| `deploy/systemd/armory-db-preflight.py` | `9567f17e24247470a3e12a5fb8a3d7e617f743ae707de533ff2f9d5ec233b4ec` |
| Recovery-ACK (historisk referens) | `a5ceaa2a5a3fc8ffd93092d6f7e8126b6cc631dbedcf818d1404d53cb6b1d74e` |

## Steg 1 – Färsk skrivskyddad preflight

Efter separat beslut ska en administratör köra den godkända preflighten från en
rootägd, skrivskyddad checkout och en rootägd scriptkopia. Kör inte rootkod från
en utvecklarägd katalog. Preflighten ska kontrollera minst:

1. Full releasecommit, bundle/source/wheel och alla komponenthashar.
2. Att aktiv `deployment-state.json` saknas och att arkiverad journal,
   manifest, hashbunden recovery-review, recovery-addendum och Recovery-ACK
   matchar exakt granskade hashar. Läs inte om eller ändra auditbevisen.
3. Att releasepekaren fortfarande är den uttryckligen godkända tidigare
   releasen, tjänsterna är stoppade/avaktiverade och inga Armory-processer
   eller öppna DB-handtag finns.
4. Att databashash, metadata, snapshotintegritet och befintlig
   backupinventering är aktuella. SQLite öppnar inte originalfilen direkt.
5. Att konfigurationens credential-fil har säker ägare/läge utan att visa
   hemlighetsvärdet samt att server- och kanalrättigheter kontrolleras separat.

Varje FAIL eller UNKNOWN stoppar förloppet. Ingen automatisk retry, recovery,
restore eller rollback får utföras.

## Steg 2 – Separata beslut

Efter godkänd preflight krävs separata beslut för:

1. **Installation:** exakt releasecommit, hashmanifest, installer, backup och
   rollbackansvarig godkänns.
2. **Finalisering:** deploymentens completion-arkiv och ACK godkänns efter att
   installationen kontrollerats. Historisk incidentjournal, Recovery-ACK,
   addendum och review lämnas orörda.
3. **Aktivering:** bot och timer får startas först efter separat beslut och ny
   startupkontroll.

En code-only-uppgradering använder inte `production_migration.py` och gör ingen
SQLite-skrivning. Databasändring eller restore kräver ett separat uppdrag och
beslut.

## Rollback och avbrott

Före releasepekarskifte: stoppa och bevara staging. Efter avbrott: följ den
hashbundna journalen och kontrollera observerat tjänsteläge; kör inte om
wrappern på antagande. Efter lyckad installation men före aktivering behålls
databas och backup orörda. Ändra inte pekare manuellt och återställ inte en
äldre databasbackup över senare data. Korrigerad rollbackprocedur måste vara
testad mot syntetiska releaser innan eventuell PROD-installation.

**Beslut nu: GO endast för DEV-arbete och granskning; NO-GO för PROD.**
