"""
Frühstück-Vorbestellung für das Festival-Camping am Badesee Ummendorf
=======================================================================

Ein schlanker Flask-Server für:
- Bestellformular für Gäste (mehrere Zeitfenster, mehrere Artikel)
- Bezahlung direkt über Stripe Checkout (Kreditkarte, Apple/Google Pay, ggf. auch SEPA/Sofort
  je nach Stripe-Kontoeinstellungen)
- Küchen-/Adminübersicht mit PIN-Schutz, gruppiert nach Zeitfenster
- Bestellungen werden erst nach erfolgreicher Zahlung (Stripe-Webhook) als "bezahlt" markiert

Siehe README.md für Einrichtung und Deployment.
"""

import json
import os
import re
import smtplib
import sqlite3
import uuid
from datetime import datetime
from email.mime.text import MIMEText
from functools import wraps

import stripe
from flask import Flask, g, jsonify, redirect, render_template, request, session, url_for

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "bestellungen.db")
MENU_PATH = os.path.join(BASE_DIR, "menu.json")

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "bitte-in-produktion-aendern")

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
KUECHEN_PIN = os.environ.get("KUECHEN_PIN", "1234")
DOMAIN = os.environ.get("DOMAIN", "http://localhost:5000")

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER)
SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "Frühstück Badesee Ummendorf")

with open(MENU_PATH, encoding="utf-8") as f:
    MENU = json.load(f)

ITEMS_BY_ID = {item["id"]: item for item in MENU["items"]}
SLOTS_BY_ID = {slot["id"]: slot for slot in MENU["zeitfenster"]}


# ---------------------------------------------------------------------------
# Datenbank
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS bestellungen (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL DEFAULT '',
            zeitfenster_id TEXT NOT NULL,
            positionen_json TEXT NOT NULL,
            summe_cent INTEGER NOT NULL,
            bezahlt INTEGER NOT NULL DEFAULT 0,
            abgeholt INTEGER NOT NULL DEFAULT 0,
            stripe_session_id TEXT,
            erstellt_am TEXT NOT NULL
        )
        """
    )
    spalten = [row[1] for row in db.execute("PRAGMA table_info(bestellungen)")]
    if "email" not in spalten:
        db.execute("ALTER TABLE bestellungen ADD COLUMN email TEXT NOT NULL DEFAULT ''")
    db.commit()
    db.close()


init_db()


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def kueche_login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("kueche_ok"):
            return redirect(url_for("kueche_login"))
        return view(*args, **kwargs)
    return wrapped


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")
    return slug or "artikel"


def eindeutige_id(basis, vergebene_ids):
    kandidat = basis
    zaehler = 2
    while kandidat in vergebene_ids:
        kandidat = f"{basis}_{zaehler}"
        zaehler += 1
    return kandidat


def sende_bestaetigungsmail(name, email, zeitfenster_label, positionen, summe_cent):
    if not (SMTP_HOST and SMTP_USER and SMTP_PASSWORD):
        app.logger.warning("SMTP nicht konfiguriert – keine Bestätigungsmail an %s gesendet.", email)
        return

    zeilen = "\n".join(f"- {p['anzahl']}x {p['name']}" for p in positionen)
    text = (
        f"Hallo {name},\n\n"
        f"vielen Dank für deine Frühstücksbestellung am Badesee Ummendorf!\n\n"
        f"Abholzeit: {zeitfenster_label}\n\n"
        f"Deine Bestellung:\n{zeilen}\n\n"
        f"Gesamt: {summe_cent / 100:.2f} €\n\n"
        f"Bitte komm einfach zur gewählten Zeit zur Frühstücksausgabe.\n\n"
        f"Wir freuen uns auf dich!"
    )

    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = "Bestätigung: Deine Frühstücksbestellung"
    msg["From"] = f"{SMTP_FROM_NAME} <{SMTP_FROM}>"
    msg["To"] = email

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, [email], msg.as_string())
    except Exception:
        app.logger.exception("Bestätigungsmail an %s konnte nicht gesendet werden.", email)


# ---------------------------------------------------------------------------
# Gäste-Routen
# ---------------------------------------------------------------------------

@app.route("/")
def bestellformular():
    return render_template("bestellen.html", items=MENU["items"], slots=MENU["zeitfenster"])


@app.route("/bestellen", methods=["POST"])
def bestellung_erstellen():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    zeitfenster_id = data.get("zeitfenster_id")
    mengen = data.get("mengen", {})  # { item_id: anzahl }

    if not name:
        return jsonify({"error": "Bitte einen Namen angeben."}), 400
    if not email or not EMAIL_REGEX.match(email):
        return jsonify({"error": "Bitte eine gültige E-Mail-Adresse angeben."}), 400
    if zeitfenster_id not in SLOTS_BY_ID:
        return jsonify({"error": "Ungültiges Zeitfenster."}), 400

    positionen = []
    summe_cent = 0
    for item_id, anzahl in mengen.items():
        anzahl = int(anzahl)
        if anzahl <= 0:
            continue
        item = ITEMS_BY_ID.get(item_id)
        if not item:
            continue
        positionen.append({"id": item_id, "name": item["name"], "anzahl": anzahl, "preis_cent": item["preis_cent"]})
        summe_cent += anzahl * item["preis_cent"]

    if not positionen:
        return jsonify({"error": "Bitte mindestens einen Artikel auswählen."}), 400

    bestell_id = str(uuid.uuid4())
    db = get_db()
    db.execute(
        "INSERT INTO bestellungen (id, name, email, zeitfenster_id, positionen_json, summe_cent, erstellt_am) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (bestell_id, name, email, zeitfenster_id, json.dumps(positionen), summe_cent, datetime.utcnow().isoformat()),
    )
    db.commit()

    # Stripe Checkout Session erstellen
    line_items = [
        {
            "price_data": {
                "currency": MENU["waehrung"],
                "product_data": {"name": p["name"]},
                "unit_amount": p["preis_cent"],
            },
            "quantity": p["anzahl"],
        }
        for p in positionen
    ]

    checkout_session = stripe.checkout.Session.create(
        mode="payment",
        line_items=line_items,
        customer_email=email,
        success_url=f"{DOMAIN}/danke?bestellung={bestell_id}",
        cancel_url=f"{DOMAIN}/?abgebrochen=1",
        metadata={"bestell_id": bestell_id},
    )

    db.execute(
        "UPDATE bestellungen SET stripe_session_id = ? WHERE id = ?",
        (checkout_session.id, bestell_id),
    )
    db.commit()

    return jsonify({"checkout_url": checkout_session.url})


@app.route("/danke")
def danke():
    bestell_id = request.args.get("bestellung")
    return render_template("danke.html", bestell_id=bestell_id)


@app.route("/webhook/stripe", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        return "", 400

    if event["type"] == "checkout.session.completed":
        session_obj = event["data"]["object"]
        bestell_id = session_obj.get("metadata", {}).get("bestell_id")
        if bestell_id:
            db = get_db()
            db.execute("UPDATE bestellungen SET bezahlt = 1 WHERE id = ?", (bestell_id,))
            db.commit()

            row = db.execute("SELECT * FROM bestellungen WHERE id = ?", (bestell_id,)).fetchone()
            if row:
                slot = SLOTS_BY_ID.get(row["zeitfenster_id"])
                slot_label = slot["label"] if slot else row["zeitfenster_id"]
                sende_bestaetigungsmail(
                    row["name"],
                    row["email"],
                    slot_label,
                    json.loads(row["positionen_json"]),
                    row["summe_cent"],
                )

    return "", 200


# ---------------------------------------------------------------------------
# Küchen-/Admin-Routen
# ---------------------------------------------------------------------------

@app.route("/kueche/login", methods=["GET", "POST"])
def kueche_login():
    fehler = None
    if request.method == "POST":
        if request.form.get("pin") == KUECHEN_PIN:
            session["kueche_ok"] = True
            return redirect(url_for("kueche_uebersicht"))
        fehler = "Falscher PIN."
    return render_template("kueche_login.html", fehler=fehler)


@app.route("/kueche/logout")
def kueche_logout():
    session.pop("kueche_ok", None)
    return redirect(url_for("kueche_login"))


@app.route("/kueche")
@kueche_login_required
def kueche_uebersicht():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM bestellungen WHERE bezahlt = 1 ORDER BY zeitfenster_id, erstellt_am"
    ).fetchall()

    # Nach Zeitfenster gruppieren, inkl. Summenzeile pro Artikel
    gruppiert = {slot["id"]: {"label": slot["label"], "bestellungen": [], "artikel_summe": {}} for slot in MENU["zeitfenster"]}
    for row in rows:
        positionen = json.loads(row["positionen_json"])
        gruppe = gruppiert.setdefault(row["zeitfenster_id"], {"label": row["zeitfenster_id"], "bestellungen": [], "artikel_summe": {}})
        gruppe["bestellungen"].append(
            {
                "id": row["id"],
                "name": row["name"],
                "positionen": positionen,
                "summe_cent": row["summe_cent"],
                "abgeholt": bool(row["abgeholt"]),
            }
        )
        for p in positionen:
            gruppe["artikel_summe"][p["name"]] = gruppe["artikel_summe"].get(p["name"], 0) + p["anzahl"]

    return render_template("kueche.html", gruppiert=gruppiert)


@app.route("/kueche/abgeholt/<bestell_id>", methods=["POST"])
@kueche_login_required
def markiere_abgeholt(bestell_id):
    db = get_db()
    db.execute("UPDATE bestellungen SET abgeholt = 1 WHERE id = ?", (bestell_id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/kueche/angebot")
@kueche_login_required
def angebot_bearbeiten():
    return render_template("angebot.html", menu=MENU)


@app.route("/kueche/angebot", methods=["POST"])
@kueche_login_required
def angebot_speichern():
    global MENU, ITEMS_BY_ID, SLOTS_BY_ID

    data = request.get_json(force=True)
    roh_items = data.get("items", [])
    roh_slots = data.get("zeitfenster", [])

    neue_items = []
    vergebene_item_ids = set()
    for roh in roh_items:
        name = (roh.get("name") or "").strip()
        if not name:
            continue
        try:
            preis_cent = round(float(str(roh.get("preis_euro", "0")).replace(",", ".")) * 100)
        except (TypeError, ValueError):
            continue
        if preis_cent <= 0:
            continue
        item_id = eindeutige_id(slugify(roh.get("id") or name), vergebene_item_ids)
        vergebene_item_ids.add(item_id)
        neue_items.append({"id": item_id, "name": name, "preis_cent": preis_cent})

    neue_slots = []
    vergebene_slot_ids = set()
    for roh in roh_slots:
        label = (roh.get("label") or "").strip()
        if not label:
            continue
        slot_id = eindeutige_id(slugify(roh.get("id") or label), vergebene_slot_ids)
        vergebene_slot_ids.add(slot_id)
        neue_slots.append({"id": slot_id, "label": label})

    if not neue_items:
        return jsonify({"error": "Mindestens ein Artikel erforderlich."}), 400
    if not neue_slots:
        return jsonify({"error": "Mindestens ein Zeitfenster erforderlich."}), 400

    MENU["items"] = neue_items
    MENU["zeitfenster"] = neue_slots
    with open(MENU_PATH, "w", encoding="utf-8") as f:
        json.dump(MENU, f, ensure_ascii=False, indent=2)

    ITEMS_BY_ID = {item["id"]: item for item in MENU["items"]}
    SLOTS_BY_ID = {slot["id"]: slot for slot in MENU["zeitfenster"]}

    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
