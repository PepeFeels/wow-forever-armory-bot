# TASK-302 – Återställning och driftsättning

Det här dokumentet kompletterar och ersätter den historiska rekommendationen i
`TASK-301-RECOVERY.md`. Den filen beskriver läget före TASK-302 och ska finnas
kvar som historik.

## Felsökning och korrigeringar

1. Den hashbundna återställningen hade redan publicerat den arkiverade journalen
   `deployment-state-b957b1735570ad821ddbc5f4f8bcaa82ca635e1393394a439879bef531a7f4ca.json`
   och dess ack. Auditvalidatorn avvisade ändå historiken eftersom den krävde en
   installationsresultatfil för varje journal med `deployment_id`. Journalen
   avbröts vid `backup_verified`, innan en sådan fil kan finnas. Regeln godkänner
   nu enbart den uttryckligt pinnade pre-shutdown-klassen utan resultatfil och
   fortsätter att avvisa resultatfiler som motsäger den.
2. Samma validator krävde att den historiska DB-preflightfilen var körbar
   (`0700`), trots att den bevarade filen är avsiktligt skrivskyddad (`0444`).
   Den kontrollerar nu rätt metadata och fortfarande exakt SHA-256.
3. Produktionsdatabasen har en tom WAL och en privat SHM-fil. Återställnings-
   kontrollen tillåter bara stabila sidofiler med rätt ägare och läge, tom WAL,
   inga processhandtag och oförändrad identitet. En WAL med data, journalfil,
   restore-markör, osäker metadata eller pågående process stoppar kontrollen.
4. Produktionspinnen godkände endast föregående kandidat. Ett nytt explicit
   `rotate-pin`-flöde sparar först de exakta gamla pinbytesen i en hash-namngiven
   läs-only historikfil och skriver en separat läs-only godkännandepost. Den
   kontrollerar tidigare pinhash, release, föregående PROD-release och oförändrad
   installerarhash innan en atomisk ersättning. Befintlig pin och historik skrivs
   inte över i tysthet.

## Verifierade PROD-data

- Aktiv release: `dcceeb85464e09e34b112e41b16a833226c72270`.
- Journalens ursprungliga SHA-256: `b957b1735570ad821ddbc5f4f8bcaa82ca635e1393394a439879bef531a7f4ca`.
- Auditvalidatorn på den korrigerade kandidaten accepterar de två bevarade
  journal/ack-kedjorna. Journalen och historiska poster har inte raderats.
- Databasen: SHA-256 `4b7e10b846c31fd080f21449478475f783cba00964f0b532b62606c356e1a998`,
  SQLite-integritet `ok`, 1 guild, 4 medlemmar och 4 karaktärer.
- Registrerad backup: SHA-256
  `697b031ced2bf45818eafeb8b4d2556784da04873b1565ce6e1678007a112b5c`,
  matchar journalen, SQLite-integritet `ok`, samma tabellantal.
- En färsk, isolerad snapshot-kontroll bekräftade databasen och sidofilerna utan
  att skriva i PROD-databasen. Backupkopian före driftstart måste fortfarande
  skapas och kontrolleras enligt deployment-runbooken.
- Bot och backup-timer är för närvarande inaktiva/disabled; backup-tjänsten är
  inactive/static.

## Testresultat före driftsättning

Kör hela testsviten från repositoryt med:

```sh
PYTHONPATH=.:src ../.venv/bin/pytest -q
```

Slutliga fulla körningen före releasepaketering: 529 tester och 142
deltester passerade. Endast upstream-varningar om
`asyncio.iscoroutinefunction` rapporterades.

## Deploymentordning

1. Använd den slutliga committen och bundle-hashen som rapporteras efter den
   sista testkörningen. Kontrollera staged `candidate` med production preflight.
2. Bevara installerad `recovery_audit.py`, `production_admin.py`, wrapper och
   hjälpfiler som hash-namngivna read-only kopior innan verktyg byts. Installera
   de granskade verktygen från root-skyddad staging och verifiera varje SHA-256.
3. Kör den godkända idempotenta återställningskommandot med exakt befintlig
   godkännandereferens, eller verifiera den redan publicerade arkiv/ack-kedjan.
   Kör auditvalidator och preflight igen innan pinnen ändras.
4. Rotera deployment-pinnen från den befintliga godkända fa15-pinnen. Aktuell
   gamla pin-SHA-256 är
   `eb50aae67cc35bf7913a4b868ed794555c65df4f46a94e8eef82e79e2584dd69`.
   Kör med `rotate-pin`, den slutliga release-committen, föregående release
   `dcceeb85464e09e34b112e41b16a833226c72270`, oförändrad installerhash
   `a6b04dea109cb916e910b19fb926bc255c42c7cd961e7b8da3b7b352573d8641`,
   godkännare `nolo-admin` och referens `TASK-302`.
5. Byt deployment-checkout med no-overwrite-arkivering av den nuvarande
   kandidatcheckouten. Kontrollera rename-resultat och Git-identitet efter varje
   steg. Kör inte wrappern under en pågående namnväxling.
6. Kör root-owned deployment-wrapper exakt en gång. Om den misslyckas, stoppa
   och inspektera journal och tjänstetillstånd innan nästa försök.
7. Verifiera ny backup, manifest och installation-resultat. Slutför journalen
   med `finalize-deployment` och aktivera tjänster med `activate-services`.
8. Kontrollera en botprocess, Discord-anslutning, kommandorespons, fyra
   registrerade karaktärer, Armory-uppdatering och aktiv/aktiverad backup-timer.

Driftsättning är inte godkänd för omstart om någon kontroll returnerar FAIL eller
UNKNOWN, om WAL inte är tom, om journalkedjan inte validerar, om pin-historiken
inte kan verifieras eller om backupen inte är färsk och kontrollerad.
