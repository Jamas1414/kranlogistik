# Kranlogistik Baustelle

Web-App zur Reservation von Kran-Zeitslots auf der Baustelle. Externe Firmen können sich registrieren,
nach Freischaltung durch einen Administrator Zeitslots buchen, eigene Buchungen einsehen/stornieren
und eine Auswertung zur Kran-Nutzung abrufen (inkl. CSV-Export für die Verrechnung).

## Funktionen

- **Login / Registrierung**: Externe Firmen registrieren sich selbst. Der erste registrierte
  Nutzer wird automatisch Administrator. Alle weiteren Konten müssen vom Admin freigeschaltet werden.
- **Kalender / Zeitslot-Buchung**: Tagesansicht mit stündlichen Slots (06:00–18:00 Uhr, anpassbar),
  freie Slots können reserviert werden, eigene Buchungen können storniert werden.
- **Admin-Bereich**: Nutzer freischalten/sperren, Admin-Rechte vergeben, Konten löschen,
  alle Buchungen verwalten.
- **Auswertung**: Pro Firma/Nutzer Anzahl Buchungen und Gesamtstunden im gewählten Zeitraum,
  CSV-Export für die Abrechnung.
- **PWA**: Die App enthält ein Web-App-Manifest und einen Service Worker, sodass sie auf
  Android und iOS über den Browser ("Zum Startbildschirm hinzufügen") wie eine App installiert
  werden kann.

## Lokal starten

```bash
cd kran-app
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Die App ist danach unter http://localhost:5000 erreichbar. Beim ersten Start wird automatisch
die SQLite-Datenbank `kranlogistik.db` angelegt.

1. Über "Registrieren" das erste Konto anlegen – dieses wird automatisch Administrator.
2. Weitere Firmen registrieren sich selbst und müssen anschliessend im Admin-Bereich
   freigeschaltet werden.

## Konfiguration anpassen

In `app.py` (oben im Code):

- `SLOT_START_HOUR` / `SLOT_END_HOUR`: Betriebszeiten des Krans (Standard 6–18 Uhr)
- `SLOT_LENGTH_MIN`: Länge der angezeigten Slot-Vorschläge (Standard 60 Minuten).
  Nutzer können beim Buchen aber auch andere Zeitfenster wählen.
- `SECRET_KEY`: Vor dem Produktiveinsatz unbedingt auf einen sicheren, zufälligen Wert setzen
  (z.B. als Umgebungsvariable `SECRET_KEY`).

## Online betreiben (Web)

Für den produktiven Einsatz empfiehlt sich ein Hosting-Anbieter mit Python-Unterstützung
(z.B. Render, Railway, PythonAnywhere, ein eigener Server mit `gunicorn` + `nginx`).
Produktionsstart z.B.:

```bash
gunicorn -w 4 -b 0.0.0.0:8000 app:app
```

Für eine produktive Nutzung mit mehreren gleichzeitigen Zugriffen wird empfohlen, statt
SQLite eine PostgreSQL-Datenbank zu verwenden (in `app.config['SQLALCHEMY_DATABASE_URI']` anpassen).

## Nutzung auf Android / iOS

Die App ist als **Progressive Web App (PWA)** umgesetzt – eine native App-Entwicklung ist dafür
nicht nötig:

- **Android (Chrome)**: Seite öffnen → Menü → "Zum Startbildschirm hinzufügen" / "App installieren".
- **iOS (Safari)**: Seite öffnen → Teilen-Symbol → "Zum Home-Bildschirm".

Die App erscheint danach als eigenes Icon auf dem Homescreen und startet im Vollbildmodus,
ohne Browser-Adressleiste.

## Hinweise / nächste Schritte

- Diese Version verwendet SQLite – ideal für einen Prototyp oder kleine Baustellen.
  Für mehrere Baustellen gleichzeitig oder grössere Nutzerzahlen sollte auf PostgreSQL/MySQL
  umgestellt werden.
- Passwort-Reset per E-Mail ist noch nicht implementiert (aktuell muss ein Admin im Zweifel
  ein neues Konto anlegen bzw. Konten verwalten).
- Die Slot-Länge ist aktuell frei wählbar innerhalb der Betriebszeiten; falls feste Slots
  (z.B. nur 1-Stunden-Blöcke) gewünscht sind, kann das Buchungsformular entsprechend
  eingeschränkt werden.
