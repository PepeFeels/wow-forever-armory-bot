# Administratörsguide

Den här guiden är avsedd för den som sköter den körande WoW Forever Armory-botten.
Den beskriver kontroller och säkra arbetsflöden; produktionsändringar ska göras
från en granskad release enligt `OPERATIONS.md`.

## Tjänster och data

- Discord-botten kör som systemanvändaren `wow-armory` via
  `wow-forever-armory-bot.service`.
- Den dagliga säkerhetskopieringen styrs av
  `wow-forever-armory-backup.timer` och kör
  `wow-forever-armory-backup.service`.
- Databasen finns normalt i
  `/var/lib/wow-forever-armory-bot/guild-armory.sqlite3`.
- Säkerhetskopior sparas i `backups/active/`. De innehåller medlems- och
  karaktärsdata och ska behålla ägare `wow-armory` och rättighet `0600`.
- Discord-token läses som en systemd-credential från
  `/etc/wow-forever-armory-bot/discord-token`. Visa eller kopiera aldrig dess
  innehåll.

## Vanliga kontroller

Kontrollera tjänster utan att ändra dem:

```bash
sudo systemctl status wow-forever-armory-bot.service
sudo systemctl status wow-forever-armory-backup.timer
sudo systemctl show wow-forever-armory-backup.service -p Result -p ExecMainStatus
sudo journalctl -u wow-forever-armory-bot.service --since today
```

En schemalagd `oneshot`-backupservice är normalt `inactive (dead)` efter jobbet;
kontrollera att senaste `Result` är `success` och `ExecMainStatus` är `0`.
Timern ska vara `active (waiting)` och `enabled`.

## Discord-funktioner

Serveradministratören aktiverar panelen med `/armory activate` i vald kanal.
Medlemmar använder panelens **Add & Edit**-kontroller för att lägga till,
ersätta eller ta bort karaktärer. Gränsen är tre karaktärer per medlem och
Discord-server. Profilen anges som en stödd HTTPS-länk till den officiella WoW
Forever Armoryn. Uppdateringsarbetaren kontrollerar profiler ungefär var femte
minut; panelbilden ändras när den hämtade karaktärsdatan förändras.

Global Discord-kommandosynkronisering görs separat. Normal botstart skapar eller
ändrar inte globala kommandon. Följ gransknings- och godkännandestegen i
`OPERATIONS.md` innan sådan synkronisering.

## Backup och återställning

Backupservicen skapar en SQLite-snapshot medan botten kör, kontrollerar
integriteten och använder en låsning för att undvika samtidiga backuper.
Retentioninställningen är 14; ta inte manuellt bort eller skriv över äldre
backuper. Kontrollera filens integritet och antal rader med dokumenterade
kommandon i `OPERATIONS.md`.

Återställning är ett separat underhållsarbete: stoppa först enligt godkänd
återställningsprocedur, kontrollera backupens hash och integritet, återställ till
den avsedda databassökvägen via projektets `restore`-funktion och verifiera
journal/attest innan tjänster återaktiveras. Återställ aldrig en gammal snapshot
för att hantera ett kodfel. Ändra inte direkt i SQLite och radera inte
återställningsspärrar eller revisionsbevis.

## Release och fortsatt utveckling

Utveckla på en separat gren och kör hela testsviten i projektets isolerade
virtuella miljö. Tester använder temporära databaser och mockade Discord- och
Armory-anrop. Testa aldrig registrering, borttagning eller återställning genom
att skriva i produktion.

Granska sedan ändringar, tester, commit och releasepaket. Använd den
administrativa staging-, preflight-, backup-, audit- och aktiveringsproceduren i
`OPERATIONS.md`. Byt inte `current` manuellt, hoppa inte över en underkänd eller
okänd preflight och starta inte om botten bara för att testa kod. En körande
release ska bara bytas efter verifierad backup och godkänd kontrollkedja.

## Kända driftbegränsningar

- Armoryns egen tidsstämpel kan släpa efter en lyckad kontroll.
- Om den externa Armoryn inte svarar behåller botten sparad data och fortsätter
  senare försök; senaste information kan då vara gammal.
- Produktionskommandon för global Discord-synkronisering kräver separat
  granskning och godkännande.

Detaljerade installationer, konfigurationer, auditbevis, uppgraderingar,
återställning och felsökning finns i `OPERATIONS.md` och `TROUBLESHOOTING.md`.
