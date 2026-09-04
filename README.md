# Frühstück-Vorbestellung – Badesee Ummendorf

Kleine Web-App für Gäste eines Festival-Campings, um Frühstück vorzubestellen
und direkt online zu bezahlen (Stripe Checkout). Der Besitzer sieht bezahlte
Bestellungen in einer nach Zeitfenster gruppierten Küchenübersicht.

## 1. Menü & Zeitfenster anpassen

Am einfachsten direkt in der App unter **/kueche/angebot** (verlinkt von der
Küchenübersicht, PIN-geschützt) – Artikel, Preise und Zeitfenster bearbeiten,
hinzufügen oder entfernen, ohne Code oder Server-Neustart. Änderungen gelten
sofort für neue Bestellungen, bereits aufgegebene bleiben unverändert.

Alternativ lässt sich `menu.json` auch direkt bearbeiten (Preise in Cent).

## 2. Stripe einrichten (einmalig, ca. 15 Minuten)

1. Konto auf https://dashboard.stripe.com erstellen (falls noch nicht vorhanden)
   und die Geschäftsdaten hinterlegen (nötig, damit echte Zahlungen ausgezahlt werden).
2. Im Dashboard unter **Entwickler → API-Schlüssel**:
   - `STRIPE_SECRET_KEY` (beginnt mit `sk_live_...` bzw. `sk_test_...` zum Testen)
3. Unter **Entwickler → Webhooks** einen Endpoint anlegen:
   - URL: `https://DEINE-DOMAIN/webhook/stripe`
   - Event: `checkout.session.completed`
   - Den angezeigten **Signing Secret** als `STRIPE_WEBHOOK_SECRET` übernehmen
4. Zum risikofreien Testen zuerst die `sk_test_...`-Schlüssel und Stripes
   Test-Kartennummer `4242 4242 4242 4242` (beliebiges künftiges Datum, beliebige CVC) verwenden.

## 3. Bestätigungsmail einrichten (optional, aber empfohlen)

Gäste geben beim Bestellen Name und E-Mail-Adresse an. Nach erfolgreicher
Zahlung verschickt die App automatisch eine Bestätigungsmail mit Bestellung,
Summe und Abholzeit. Dafür wird ein SMTP-Konto benötigt – am einfachsten:

1. Ein Google-Konto mit aktivierter 2-Faktor-Authentifizierung verwenden und
   unter https://myaccount.google.com/apppasswords ein **App-Passwort**
   erstellen (kein normales Passwort verwenden).
2. Umgebungsvariablen setzen:
   - `SMTP_HOST=smtp.gmail.com`
   - `SMTP_PORT=587`
   - `SMTP_USER=deine-adresse@gmail.com`
   - `SMTP_PASSWORD=` das erstellte App-Passwort
   - `SMTP_FROM_NAME=Frühstück Badesee Ummendorf` (optional, Absendername)

Jeder andere SMTP-Anbieter (z. B. vom eigenen Hoster, oder ein Dienst wie
Brevo/SendGrid) funktioniert genauso über dieselben Variablen. Ohne SMTP-
Konfiguration läuft die App normal weiter, es wird dann nur keine Mail
verschickt (steht als Warnung im Server-Log).

## 4. Lokal starten (zum Testen)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

export STRIPE_SECRET_KEY="sk_test_..."
export STRIPE_WEBHOOK_SECRET="whsec_..."
export KUECHEN_PIN="1234"          # frei wählbarer PIN für die Küchenübersicht
export DOMAIN="http://localhost:5000"
export FLASK_SECRET_KEY="ein-zufaelliger-string"

# optional, für Bestätigungsmails – siehe Abschnitt 3
export SMTP_HOST="smtp.gmail.com"
export SMTP_PORT="587"
export SMTP_USER="deine-adresse@gmail.com"
export SMTP_PASSWORD="app-passwort"

python app.py
```

Bestellformular: http://localhost:5000
Küchenübersicht: http://localhost:5000/kueche
Angebot bearbeiten: http://localhost:5000/kueche/angebot (verlinkt von der Küchenübersicht)

Für Webhook-Tests lokal die Stripe CLI verwenden: `stripe listen --forward-to localhost:5000/webhook/stripe`

## 5. Deployment auf Render.com (kostenloser Plan)

1. Code auf GitHub bringen (einmalig):
   ```bash
   git init
   git add .
   git commit -m "Erste Version"
   ```
   Dann auf https://github.com/new ein neues, leeres Repository anlegen
   (kein README/`.gitignore` mit anlegen lassen) und die dort angezeigten
   Befehle ausführen, z. B.:
   ```bash
   git remote add origin https://github.com/DEIN-NUTZERNAME/DEIN-REPO.git
   git branch -M main
   git push -u origin main
   ```
2. Auf https://dashboard.render.com → **New +** → **Web Service** → das eben
   erstellte GitHub-Repo auswählen (ggf. GitHub-Zugriff für Render autorisieren).
3. Einstellungen beim Anlegen:
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app`
   - **Instance Type:** Free
4. Unter **Environment** die Umgebungsvariablen eintragen (siehe Abschnitt 2–3):
   `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `KUECHEN_PIN`, `FLASK_SECRET_KEY`,
   optional `SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`SMTP_PASSWORD`/`SMTP_FROM_NAME`.
   `DOMAIN` erst **nach** dem ersten Deploy setzen, sobald die von Render
   vergebene URL bekannt ist (z. B. `https://dein-projekt.onrender.com`) –
   danach den Stripe-Webhook (Abschnitt 2, Schritt 3) auf diese URL zeigen lassen.
5. **Create Web Service** klicken – der erste Deploy dauert ein paar Minuten.

### Wichtig beim kostenlosen Plan

- Der Dienst schläft nach ca. 15 Minuten ohne Anfragen ein und braucht beim
  nächsten Aufruf ca. 30–50 Sekunden zum Aufwachen – der erste Gast nach einer
  Pause wartet also kurz auf das Laden der Seite.
- Die Festplatte ist **nicht dauerhaft**: Bei jedem neuen Deploy (z. B. wenn du
  danach nochmal Code-Änderungen pushst) wird die SQLite-Datenbank
  zurückgesetzt und bisherige Bestellungen gehen verloren. Deshalb während des
  Events nicht mehr neu deployen, und die Küchenübersicht nicht als einzige
  Quelle für die Abrechnung verwenden, falls das wichtig ist.
- Falls sich das im Nachhinein als zu riskant erweist, lässt sich jederzeit
  auf den kostenpflichtigen "Starter"-Plan mit persistenter Festplatte
  upgraden (im Render-Dashboard unter dem Service → Settings).

Wichtig: Stripe Checkout benötigt zwingend eine **HTTPS**-Adresse im Live-Modus –
Render liefert das automatisch mit.

## 6. QR-Code für Gäste

Nach dem Deployment einen QR-Code auf die Startseiten-URL erstellen (z. B. über
einen beliebigen kostenlosen QR-Generator) und am Camping/Eingang aushängen.

## 7. Am Veranstaltungstag

- Küchenübersicht (`/kueche`, PIN-geschützt) auf einem Tablet/Laptop in der
  Küche offen lassen, Seite bei Bedarf neu laden.
- Bestellungen erscheinen erst nach erfolgreicher Zahlung (Stripe-Webhook)
  automatisch in der Übersicht, gruppiert nach Zeitfenster inkl. Summenzeile
  je Artikel (z. B. "12x Croissant") – praktisch für die Mengenplanung.
- "Als abgeholt markieren" blendet erledigte Bestellungen aus, ohne sie zu löschen.
- Gäste erhalten nach erfolgreicher Zahlung automatisch eine Bestätigungsmail
  (falls SMTP eingerichtet, siehe Abschnitt 3).

## Grenzen dieser einfachen Lösung

- Keine Bestandsverwaltung (z. B. "nur 50 Rühreier verfügbar") – bei Bedarf
  ergänzbar.
- Küchenübersicht aktualisiert sich nicht automatisch live, sondern per
  Neuladen (für ein Wochenende ausreichend, ließe sich aber leicht nachrüsten).
- Keine Stornofunktion für Gäste eingebaut.

Bei Bedarf lässt sich das alles nachrüsten – für 100–150 Frühstücke an einem
Wochenende ist dieser Umfang aber realistisch und schnell aufsetzbar.
