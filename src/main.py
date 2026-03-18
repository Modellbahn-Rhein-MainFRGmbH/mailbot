"""
Modellbahn-Rhein-Main Mail Assistent v5
Fabian Rauch - Brevo API + IMAP + Telegram
Neu: Feedback-Loop, bessere Kategorien, robuster WooCommerce-Abruf
"""

import imaplib
import email as email_lib
import os
import json
import time
import logging
import requests
import hashlib
from datetime import datetime
from email.header import decode_header
from anthropic import Anthropic

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Mail empfangen (IMAP - deine echte Mailadresse)
MAIL_HOST  = os.environ["MAIL_HOST"]
MAIL_USER  = os.environ["MAIL_USER"]
MAIL_PASS  = os.environ["MAIL_PASS"]

# Mail senden (Brevo HTTP API - kein SMTP noetig!)
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")

# Shop APIs
WC_URL     = os.environ.get("WC_URL", "")
WC_KEY     = os.environ.get("WC_KEY", "")
WC_SECRET  = os.environ.get("WC_SECRET", "")
SC_KEY     = os.environ.get("SC_KEY", "")
SC_SECRET  = os.environ.get("SC_SECRET", "")

# WordPress Application Password (fuer Rechnungs-PDF Download)
WP_USER     = os.environ.get("WP_USER", "")
WP_APP_PASS = os.environ.get("WP_APP_PASS", "")

# Telegram
TG_TOKEN   = os.environ["TG_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]

# Claude
ANTHROPIC_KEY = os.environ["ANTHROPIC_KEY"]

# Feedback-Datei (Railway Volume oder lokaler Pfad)
FEEDBACK_DIR  = os.environ.get("FEEDBACK_DIR", "/data")
FEEDBACK_FILE = os.path.join(FEEDBACK_DIR, "feedback_history.json")

client  = Anthropic(api_key=ANTHROPIC_KEY)
pending = {}


# ============================================================
# FEEDBACK-LOOP: Korrekturen speichern und beim Prompten nutzen
# ============================================================

def load_feedback():
    """Lade bisherige Korrekturen aus JSON-Datei."""
    try:
        os.makedirs(FEEDBACK_DIR, exist_ok=True)
        if os.path.exists(FEEDBACK_FILE):
            with open(FEEDBACK_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        log.warning(f"Feedback laden: {e}")
    return []


def save_feedback(entry):
    """Speichere eine Korrektur (Original + Aenderung + Kontext)."""
    try:
        history = load_feedback()
        history.append(entry)
        # Maximal 50 Korrekturen behalten (die neuesten)
        if len(history) > 50:
            history = history[-50:]
        os.makedirs(FEEDBACK_DIR, exist_ok=True)
        with open(FEEDBACK_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        log.info(f"Feedback gespeichert ({len(history)} Eintraege)")
    except Exception as e:
        log.warning(f"Feedback speichern: {e}")


def build_feedback_prompt():
    """Erstelle einen Prompt-Abschnitt aus den letzten Korrekturen."""
    history = load_feedback()
    if not history:
        return ""
    # Die letzten 10 Korrekturen als Lernbeispiele
    recent = history[-10:]
    lines = ["\nLERNBEISPIELE AUS BISHERIGEN KORREKTUREN (Fabians echte Aenderungen):"]
    for i, fb in enumerate(recent, 1):
        lines.append(f"\nKorrektur {i}:")
        lines.append(f"  Kategorie: {fb.get('category', 'unbekannt')}")
        lines.append(f"  Kundenanfrage: {fb.get('customer_query', '')[:100]}")
        lines.append(f"  Mein Vorschlag war: {fb.get('original_draft', '')[:150]}")
        lines.append(f"  Fabian wollte: {fb.get('edit_instruction', '')}")
        lines.append(f"  Korrigierte Version: {fb.get('corrected_draft', '')[:150]}")
    lines.append("\nNutze diese Korrekturen um Fabians Stil und Vorlieben besser zu treffen.")
    return "\n".join(lines)


SIGNATURE = """Beste Grüße,

Fabian Rauch
Geschäftsführer
Modellbahn-Rhein-Main FR GmbH

Tel: 0160 3833340
E-Mail: info@modellbahn-rhein-main.de
Web: www.modellbahn-rhein-main.de
Adresse: Max-Planck-Str. 18, 63322 Rödermark

Handelsregister: Amtsgericht Offenbach, HRB 58191
Umsatzsteuer-ID gemäß 27a UStG: DE456540670

Hinweis: Diese E-Mail enthält vertrauliche Informationen. Wenn Sie nicht der
beabsichtigte Empfänger sind, informieren Sie bitte den Absender und löschen
Sie die Nachricht."""

SYSTEM_PROMPT = """
Du bist der KI-Assistent von Fabian Rauch, Inhaber von Modellbahn-Rhein-Main.
Dein Ziel: Kundenkommunikation so verfassen, dass Fabian sie mit 0% Nachbearbeitung uebernehmen kann.

IDENTITAET UND SPRACHPROFIL:
- Schreib wie ein Experte mit echter Leidenschaft fuer Modellbahn. Begriffe wie seidenweicher Lauf, Bastelobjekt, Traumstueck sind erwuenscht.
- Kein Corporate Talk. Nicht: Wir bedauern die Unanehmlichkeiten. Sondern: Das ist natuerlich aergerlich, wir loesen das aber sofort und unkompliziert.
- Pragmatisch: Wenn eine Teil-Erstattung schneller ist als Hin-und-Her-Versand, schlag sie direkt vor.
- Kurze Saetze, klare Aussagen, keine unnoetige Buerokratie.
- Deutsch fuer deutsche Kunden, Englisch fuer internationale Kunden.
- Du/Sie je nach Kontext: eBay-Kunden oft du (informell), Shop-Kunden Sie (formell).

GESCHAEFTSREGELN:
- Artikel unter 15 EUR: Nicht zurueckfordern. Ersatz oder Geld zurueck. Kunde behaelt Teil als Ersatzteilspender.
- Fehlende/falsche Teile ohne Ersatz: Wahl zwischen Teilrueckzahlung oder Rueckgabe.
- Ruecksendelabels: NUR bei berechtigten Beschwerden. NIEMALS bei einfacher Stornierung!
- eBay-Retouren: https://modellbahnrheinmain.shipping-portal.com/rp
- Shop-Retouren: https://modellbahnrheinmainshop.shipping-portal.com/rp/
- Tax-Free: Wir verkaufen nach Paragraph 25a UStG. Keine MwSt. ausgewiesen, kein Export-Refund.
- Kombiversand: Kunden duerfen 14 Tage Auktionen sammeln bevor Zahlung faellig wird.
- Rabatte: Kaum Rabatte, hoechstens bei schon laenger eingestellten Artikeln.

WICHTIG - LAGER UND SORTIMENT:
- Wir verkaufen ausschliesslich gebrauchte Modellbahn-Ware (Sammlerstuecke, Gebrauchtware).
- Jeden Artikel haben wir nur einmal. Wenn ausverkauft, dann wirklich weg.
- Nachbestellen ist NICHT moeglich. Wir kaufen Sammlungen auf - ob ein Artikel wiederkommt, ist Zufall.
- Wenn ein Artikel als "Ausverkauft" angezeigt wird: Dem Kunden ehrlich sagen, dass der Artikel leider schon verkauft wurde und wir ihn nicht nachbestellen koennen.
- NIEMALS sagen "Wir bestellen nach" oder "Wir koennen den Artikel fuer Sie reservieren/bestellen".
- Stattdessen auf kommende Sammlungen verweisen: "Wir veroeffentlichen regelmaessig neue Sammlungen auf unserer Website. Schauen Sie gerne mal in unsere Ankuendigungen rein - vielleicht ist beim naechsten Mal genau das Richtige dabei."
- Wenn ein Artikel "Auf Lager" ist: Darauf hinweisen dass wir ihn nur einmal haben und schnelles Zugreifen empfehlen.

EBAY vs. SHOP - PREISSTRUKTUR:
- Unsere Artikel sind im eigenen Online-Shop GUENSTIGER als auf eBay.
- Grund: eBay erhebt ca. 13% Gebuehren. Wir schlagen daher 8% auf den Shop-Preis drauf fuer eBay.
- Artikel kommen ZUERST im Shop online, danach erst auf eBay. Die besten Stuecke sind oft im Shop schon weg bevor sie auf eBay erscheinen.
- Bei eBay-Anfragen VORSICHTIG auf den Shop hinweisen (eBay-Richtlinien verbieten direkte Links!):
  "Die Artikel finden Sie auch guenstiger in unserem gleichnamigen Online-Shop. Schauen Sie dort mal rein."
- NIEMALS einen direkten Link zur Website in eBay-Nachrichten schreiben!
- NIEMALS die URL www.modellbahn-rhein-main.de in eBay-Antworten nennen!
- Nur den Hinweis auf den "gleichnamigen Online-Shop" geben, der Kunde kann selbst suchen.

LADEN UND ABHOLUNG:
- Wir haben KEINE festen Oeffnungszeiten. Besuch nur nach Terminvereinbarung.
- Samstags haben wir NICHT geoeffnet.
- Abholung ist kein Problem: Beim Bestellvorgang kann man "Abholung" als Versandart waehlen.
- Dann Termin vereinbaren und vorbeikommen.
- Bezahlung vor Ort mit Karte ist moeglich.
- Adresse: Max-Planck-Str. 18, 63322 Roedermark.

ZAHLUNGSARTEN:
- PayPal, Visa, Mastercard, American Express, Kauf auf Rechnung, SEPA Lastschrift, PayPal Ratenzahlung, Vorkasse/Bankueberweisung.
- OHNE Kundenkonto geht nur Bankueberweisung.
- MIT Kundenkonto hat man freie Wahl aller Zahlungsarten.
- Kundenkonto ist nicht noetig - Gastbestellung ist moeglich.
- Vorteil Kundenkonto: Bestellungen einsehen/verwalten + alle Zahlungsarten verfuegbar.

FUNKTIONSPRUEFUNG UND ZUSTAND:
- Alle Modelle werden auf einer Teststrecke geprueft (ebenes Oval mit ordentlich Auslauf).
- Getestet wird: Fahrbetrieb (vorwaerts/rueckwaerts), Licht, Digital- und Soundfunktionen.
- NICHT getestet: Steigungen, verschiedene Radien, Weichen.
- Detaillierte Produktfotos in Katalogqualitaet - man erhaelt exakt das abgebildete Modell.
- Bei gebrauchten Modellen spielt der Zustand eine wichtige Rolle, daher werden alle Modelle von allen Seiten fotografiert.

LIEFERUMFANG:
- Wir verkaufen gebrauchte Ware, da kann es vorkommen dass Teile fehlen (Zuruestteile, Bedienungsanleitungen).
- Alles was im Lieferumfang enthalten ist, ist auf den Bildern zu sehen.
- Ist etwas NICHT abgebildet: Davon ausgehen dass es fehlt.
- Im Zweifel koennen Kunden nachfragen, wir schauen gerne nochmal im Lager nach.

SAMMLUNG VERKAUFEN / ANKAUF:
- Wir kaufen Sammlungen, egal welche Spurgroesse, Hersteller oder Epoche.
- Interessant fuer uns: Gepflegte Sammlungen ab 20 Lokomotiven und 100 Wagen aufwaerts (grober Richtwert in Spur H0).
- Auch Sammlerstuecke und seltene Handarbeitsmodelle sind interessant.
- Ueber das Ankaufformular auf der Website kann man ein unverbindliches Angebot einholen.
- Faires Angebot auf Basis topaktueller Marktpreise dank eigener Datenbank.

KOMMENDE SAMMLUNGEN:
- Auf unserer Website stehen unten auf jeder Seite die naechsten Sammlungen im Zulauf.
- Wenn ein Artikel nicht mehr verfuegbar ist, auf die Ankuendigungen verweisen: "Schauen Sie gerne auf unserer Website unter den Ankuendigungen - dort sehen Sie welche Sammlungen als naechstes reinkommen. Vielleicht ist beim naechsten Mal genau das Richtige dabei."

KATEGORIE-SPEZIFISCHE ANWEISUNGEN:

Bei LIEFERSTATUS:
- Nutze die Sendcloud-Tracking-Daten falls vorhanden.
- Gib dem Kunden die Trackingnummer und den aktuellen Status.
- Wenn kein Tracking vorhanden: Bestelldatum pruefen, Bearbeitungszeit erwaehnen (1-3 Werktage).

Bei RETOURE:
- Unterscheide: eBay oder Shop? Jeweils anderen Retourenlink senden.
- Frag nach dem Grund. Bei Widerruf kein Ruecksendelabel.
- Bei berechtigter Beschwerde: Ruecksendelabel anbieten.

Bei BESCHWERDE:
- Erst Verstaendnis zeigen, dann Loesung anbieten.
- Unter 15 EUR: Sofort Geld zurueck oder Ersatz, Artikel behalten.
- Ueber 15 EUR: Optionen anbieten (Teilerstattung oder Rueckgabe).

Bei PRODUKTFRAGE:
- Fachkundig antworten mit Modellbahn-Wissen.
- Artikelnummer (SKU) wird automatisch nachgeschlagen. Nutze die ARTIKEL-INFORMATIONEN aus den Bestelldaten.
- Wenn Artikel "Auf Lager": Verfuegbarkeit bestaetigen, Preis nennen, ggf. Shop-Link mitgeben. Hinweis: Nur einmal vorhanden, schnell zugreifen.
- Wenn Artikel "Ausverkauft": Ehrlich sagen, dass er leider schon verkauft wurde. NICHT "nachbestellen" anbieten. Auf kommende Sammlungen/Ankuendigungen verweisen.
- Wenn du die Antwort nicht weisst: Ehrlich sagen und Rueckruf/Mail anbieten.
- WICHTIG bei eBay-Anfragen: Der Preis in den ARTIKEL-INFORMATIONEN ist der SHOP-Preis. Der eBay-Preis ist ca. 8% hoeher. Bei eBay-Nachrichten NICHT den Shop-Preis nennen, sondern nur auf Verfuegbarkeit eingehen. Den Kunden auf den guenstigeren gleichnamigen Online-Shop hinweisen.

Bei STORNIERUNG:
- Pruefen ob Bestellung schon versendet wurde (Sendcloud-Daten).
- Wenn schon versendet: Kunde informieren, Retoure anbieten.
- Wenn noch nicht versendet: Stornierung bestaetigen.

Bei RECHNUNG_STEUER:
- Immer auf Paragraph 25a UStG Differenzbesteuerung hinweisen.
- Keine MwSt. ausweisbar, kein Export-Refund moeglich.
- Wenn Kunde eine Rechnung anfordert: Einfach bestaetigen dass die Rechnung per Mail zugesendet wird.
- WICHTIG: NIEMALS schreiben "[Rechnung als PDF-Anhang beifuegen]" oder aehnliche Platzhalter! Die Rechnung wird AUTOMATISCH vom System als PDF angehaengt. Im Text einfach schreiben: "Die Rechnung zu Ihrer Bestellung finden Sie im Anhang dieser E-Mail."

Bei KOMBIVERSAND:
- 14 Tage Sammelzeit bestaetigen.
- Erklaeren wie der Ablauf funktioniert.

Bei RABATTANFRAGE:
- Hoeflich aber bestimmt: Kaum Rabatte moeglich.
- Hoechstens bei laenger eingestellten Artikeln.

Bei KONTAKTFORMULAR:
- Kunde hat ueber das Website-Formular geschrieben.
- Anrede: Sie (formell), da es ein Shop-Kunde ist.
- Inhalt der Nachricht sorgfaeltig lesen und passend antworten.
- Falls eine Artikelnummer genannt wird: Nutze die ARTIKEL-INFORMATIONEN um Verfuegbarkeit und Preis zu nennen.
- Beachte: Jeden Artikel haben wir nur einmal. "Auf Lager" = sofort bestellbar, schnell zugreifen. "Ausverkauft" = leider weg, auf Ankuendigungen verweisen.

ECHTE BEISPIELE VON FABIAN (so schreibt er wirklich):

BEISPIEL 1 - Falsche Achsen (eBay, informell):
Kunde: "Raeder des Roco FS Personenwagen (4237B) waren AC nicht DC wie beschrieben."
Antwort: Hallo Karl, es tut mir sehr leid, dass die Achsen des Roco-Wagens faelschlicherweise als DC beschrieben wurden. Ich habe im Lager nachgesehen, aber leider haben wir keine passenden Austauschachsen vorraetig. Ich kann dir eine Teilrueckerstattung anbieten, wenn du den Wagen behalten moechtest, oder die komplette Rueckgabe.

BEISPIEL 2 - Transportschaden / Fehlteil (Shop, formell):
Kunde: "Bei der Lok fehlt eine Haltestange. Haben Sie Ersatz?"
Antwort: Hallo Herr Baierl, auf unseren Artikelfotos war die Haltestange noch vorhanden, sie muss also beim Transport abgefallen sein. Da ich das Teil nicht vorraetig habe, biete ich Ihnen einen 10,00 EUR Gutschein fuer den naechsten Einkauf oder die komplette Rueckgabe an.

BEISPIEL 3 - Technische Rueckfrage international (Englisch):
Kunde: "Lack-Overspray auf dem Dach? Gehaeuse beschaedigt? Versand in die USA moeglich?"
Antwort: Hallo Eric, ja, das Modell wurde nachlackiert, was man am Uebergang zum Dach sieht. Das Gehaeuse hat keine Spruenge. Wir versenden regelmaessig und gerne in die USA!

BEISPIEL 4 - Nachverhandlung / Kulanz:
Kunde: "Wieder da nach Krankheit. Vielleicht 10 EUR Nachlass fuer Decals?"
Antwort: Guten Tag Herr Pegel, schoen, dass Sie wieder wohlauf sind! Ich bin einverstanden. Die 10,00 EUR habe ich Ihnen soeben als Preisnachlass fuer die Decals erstattet. Gute Besserung weiterhin!

BEISPIEL 5 - System-Kompatibilitaet:
Kunde: "Original oder Umbau auf AC? Maerklin-kompatibel?"
Antwort: Hallo, das Modell ist im Originalzustand als AC-Wechselstrommodell gefertigt worden (kein Umbau). Damit ist es voll Maerklin-kompatibel.

BEISPIEL 6 - Humorvolle Technik-Frage (locker, mit Emoji):
Kunde: "Faellt die Kohle in der Kurve runter?"
Antwort: Hallo Robert, keine Sorge, so schlimm ist es nicht! Die Abdeckung rastet wegen des Decoders nicht fest ein, haelt aber im normalen Fahrbetrieb absolut sicher.

BEISPIEL 7 - Kaufabbruch ablehnen (eBay, kurz und klar):
Kunde: "Moechte gern das der Abbruch abgelehnt wird."
Antwort: Hallo Klaus, alles klar, ich habe deinen Wunsch beruecksichtigt und die Anfrage zum Kaufabbruch soeben abgelehnt. Der Kauf bleibt bestehen.

BEISPIEL 8 - Lob / Positives Feedback (kurz, herzlich):
Kunde: "Extra Versandmeldungen sind sehr gut. Hat nicht jeder Haendler."
Antwort: Vielen Dank fuer das nette Feedback! Es freut mich sehr, dass dieser Extra-Service mit den Zustellbenachrichtigungen bei Ihnen so positiv ankommt.

BEISPIEL 9 - Stornierung (eBay, unkompliziert):
Kunde: "Bitte stornieren, habe 'Nur Abholung' uebersehen."
Antwort: Kein Problem, das kann mal passieren. Ich habe den Kaufabbruch soeben fuer dich im System durchgefuehrt.

BEISPIEL 10 - Kombiversand (eBay, kurz):
Kunde: "Bitte Autos kombinieren fuer Sammelzahlung."
Antwort: Das ist bereits erledigt. Ich habe die Kaeufe zusammengefasst, sodass du die Zahlung nun gesammelt vornehmen kannst.

BEISPIEL 11 - Technischer Defekt (Shop, formell):
Antwort: Sehr geehrter Herr Schmeller, das Pulsieren deutet auf einen undefinierten Decoder-Zustand hin. Gerne koennen Sie die Lok zur Ueberpruefung einsenden. Bitte Fehlerbeschreibung und Kontaktdaten beilegen.

BEISPIEL 12 - Verschmutzung (Shop, formell, unter 15 EUR):
Antwort: Sehr geehrter Herr Schminke, die Verschmutzung haette uns auffallen muessen, das tut mir leid. 1. Sie behalten den Wagen, ich erstatte 5 EUR. 2. Ruecksendung gegen vollen Kaufpreis.

STIL-ZUSAMMENFASSUNG AUS DIESEN BEISPIELEN:
- Fabian ist DIREKT und LOESUNGSORIENTIERT - keine langen Einleitungen.
- Bei eBay: Du, locker, kurze Saetze. Bei Shop: Sie, formell aber trotzdem persoenlich.
- Immer konkrete Optionen nennen (1. ... oder 2. ...), nicht vage bleiben.
- Humor ist erlaubt wenn der Kunde locker schreibt.
- Kurze Antworten bevorzugen - lieber 3 praezise Saetze als 10 Fuellsaetze.
- NIEMALS: "Wir entschuldigen uns fuer die Unannehmlichkeiten" oder "Zoegern Sie nicht uns zu kontaktieren".
- STATTDESSEN: "Das tut mir leid" / "Das kann mal passieren" / "Das loesen wir".

FORMAT:
- Erste Zeile: BETREFF: Re: Originalbetreff
- Dann Leerzeile, dann Mail mit Anrede
- Unbekannte Werte markieren: **bitte ergaenzen**
- Signatur wird automatisch angehaengt, nicht selbst schreiben
"""


def decode_str(s):
    parts = decode_header(s)
    result = []
    for part, enc in parts:
        if isinstance(part, bytes):
            result.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result)


def get_mail_body_and_images(msg):
    body      = ""
    html_body = ""
    images    = []
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd and not body:
                try:
                    body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                except:
                    pass
            elif ct == "text/html" and "attachment" not in cd and not html_body:
                try:
                    html_body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                except:
                    pass
            elif ct.startswith("image/") and len(images) < 3:
                try:
                    img_data = part.get_payload(decode=True)
                    if img_data:
                        images.append(img_data)
                except:
                    pass
    else:
        ct = msg.get_content_type()
        try:
            raw = msg.get_payload(decode=True).decode("utf-8", errors="replace")
            if ct == "text/html":
                html_body = raw
            else:
                body = raw
        except:
            body = ""

    # Fallback: Wenn kein Plain-Text, HTML in lesbaren Text umwandeln
    if not body.strip() and html_body:
        body = html_to_text(html_body)
        log.info("Kein Plain-Text, HTML-Body verwendet")

    return body, images


def html_to_text(html):
    """Einfache HTML-zu-Text Konvertierung ohne externe Libraries."""
    import re
    # Script und Style Bloecke entfernen
    text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    # <br> und <p> in Zeilenumbrueche
    text = re.sub(r'<br\s*/?\s*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</p>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</div>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</tr>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</td>', ' | ', text, flags=re.IGNORECASE)
    text = re.sub(r'</th>', ' | ', text, flags=re.IGNORECASE)
    # Alle HTML-Tags entfernen
    text = re.sub(r'<[^>]+>', '', text)
    # HTML-Entities
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&nbsp;', ' ').replace('&quot;', '"').replace('&#39;', "'")
    # Mehrfache Leerzeilen reduzieren
    text = re.sub(r'\n\s*\n', '\n\n', text)
    return text.strip()


# ============================================================
# VERBESSERTE KLASSIFIZIERUNG: Feine Kategorien statt nur question/ignore
# ============================================================

CATEGORIES = [
    "lieferstatus",      # Wo ist mein Paket? Versandbestaetigung?
    "retoure",           # Rueckgabe, Widerruf, Umtausch
    "beschwerde",        # Defekt, Beschaedigung, falscher Artikel, Verschmutzung
    "produktfrage",      # Technische Fragen, Verfuegbarkeit, Kompatibilitaet
    "stornierung",       # Bestellung stornieren
    "rechnung_steuer",   # Rechnung, MwSt., Tax-Free
    "kombiversand",      # Sammelbestellung, 14-Tage-Regel
    "rabattanfrage",     # Preisnachlass, Mengenrabatt
    "kontaktformular",   # Anfrage ueber Website-Kontaktformular
    "ignore"             # Newsletter, Spam, automatische Mails OHNE Kundenfrage
]

def classify_mail(subject, body):
    """Klassifiziere Mail in feine Kategorien fuer bessere Antworten."""
    resp = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=30,
        messages=[{"role": "user", "content": (
            "Klassifiziere diese E-Mail fuer einen Modellbahn-Haendler.\n"
            "Antworte NUR mit einer der folgenden Kategorien:\n"
            "lieferstatus = Wo ist mein Paket, Versandbestaetigung\n"
            "retoure = Rueckgabe, Widerruf, Umtausch\n"
            "beschwerde = Defekt, Beschaedigung, falscher Artikel, Verschmutzung\n"
            "produktfrage = Technische Frage, Verfuegbarkeit, Kompatibilitaet\n"
            "stornierung = Bestellung stornieren\n"
            "rechnung_steuer = Rechnung, MwSt., Tax-Free Anfrage\n"
            "kombiversand = Sammelbestellung, 14-Tage-Regel\n"
            "rabattanfrage = Preisnachlass, Mengenrabatt\n"
            "kontaktformular = Anfrage ueber Website-Kontaktformular (Betreff enthaelt 'Kontakt', 'Neuer Eintrag', 'Formular')\n"
            "ignore = NUR Newsletter, Spam, rein automatische System-Mails OHNE Kundenfrage\n\n"
            "WICHTIG: Wenn eine Mail eine Kundenfrage enthaelt (egal ob per Formular oder direkt), ist es NIEMALS ignore!\n"
            "Mails mit Betreff 'Neuer Eintrag: Kontakt' sind Kontaktformular-Anfragen, NICHT ignore.\n\n"
            f"Betreff: {subject}\nInhalt: {body[:500]}"
        )}]
    )
    result = resp.content[0].text.strip().lower()
    for cat in CATEGORIES:
        if cat in result:
            return cat
    return "ignore"


# ============================================================
# VERBESSERTER WOOCOMMERCE-ABRUF: Hoehere Timeouts, mehr Daten, Retry
# ============================================================

def extract_order_number(subject, body):
    """Bestellnummer aus Betreff oder Mailtext extrahieren.
    Ignoriert eBay-Artikelnummern (12+ Ziffern) und eBay-spezifische Nummern."""
    import re

    # Bei eBay-Nachrichten: Nur nach explizit genannten Bestellnummern suchen, nicht nach Artikel-IDs
    is_ebay = "[eBay]" in subject

    text = f"{subject} {body}"
    patterns = [
        r'[Bb]estell(?:ung|nummer)[:\s#]*(\d{4,8})',  # Bestellnummer 1540592 (max 8 Ziffern)
        r'[Oo]rder[:\s#]*(\d{4,8})',                   # Order 1540592
        r'[Aa]uftrag[:\s#]*(\d{4,8})',                 # Auftrag 1540592
    ]

    # #-Muster nur bei Nicht-eBay-Mails (sonst matcht es eBay Artikelnummern)
    if not is_ebay:
        patterns.insert(0, r'#\s*(\d{4,8})')           # #1540592
        patterns.append(r'(?:Nr|Nummer)[.:\s]*(\d{4,8})')  # Nr. 1540592

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            num = match.group(1)
            # eBay-Artikelnummern haben 12+ Ziffern - die ignorieren
            if len(num) >= 10:
                continue
            return num
    return None


def extract_invoice_number(subject, body):
    """Rechnungsnummer aus Betreff oder Mailtext extrahieren."""
    import re
    text = f"{subject} {body}"
    patterns = [
        r'[Rr]echnung(?:snummer)?[:\s#]*(\d{4,})',   # Rechnung 20261814, Rechnungsnummer 20261814
        r'[Ii]nvoice[:\s#]*(\d{4,})',                  # Invoice 20261814
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def find_order_by_invoice_number(invoice_number):
    """Bestellung ueber Rechnungsnummer in WooCommerce finden."""
    if not WC_KEY or not invoice_number:
        return None
    try:
        # WooCommerce durchsuchen - Rechnungsnummer ist oft in den Meta-Daten
        r = requests.get(
            f"{WC_URL}/wp-json/wc/v3/orders",
            auth=(WC_KEY, WC_SECRET),
            params={"search": invoice_number, "per_page": 3},
            timeout=30
        )
        orders = r.json()
        if orders and isinstance(orders, list):
            log.info(f"Bestellung ueber Rechnungsnummer {invoice_number} gefunden: #{orders[0].get('id')}")
            return parse_order_data(orders[0], order_count=len(orders))
    except Exception as e:
        log.warning(f"WooCommerce Rechnungssuche: {e}")
    return None


def extract_sku_codes(subject, body):
    """Artikelnummern (SKUs) aus Betreff oder Mailtext extrahieren.
    Format: Buchstaben + Zahlen ohne Leerzeichen, z.B. KAD0007, SRT37, JB051
    Typische Praefixe: KAD, SRT, JB, THE, SAL, THX etc."""
    import re
    text = f"{subject} {body}"

    # Bekannte SKU-Praefixe von Modellbahn-Rhein-Main (2-3 Buchstaben + Ziffern)
    # Zuerst nach bekannten Praefixen suchen (zuverlaessiger)
    known_prefixes = r'(?:KAD|SRT|JB|THE|SAL|THX|SCT|KU|1GW|FAE|DBA)'
    priority_matches = re.findall(rf'\b({known_prefixes}\d{{1,6}})\b', text, re.IGNORECASE)

    # Dann allgemeinere Suche (2-3 Grossbuchstaben + Ziffern, aber strenger)
    general_matches = re.findall(r'\b([A-Z]{2,3}\d{2,6})\b', text)

    all_matches = priority_matches + general_matches

    # Duplikate entfernen, Reihenfolge beibehalten
    seen = set()
    skus = []
    for m in all_matches:
        upper = m.upper()
        if upper not in seen:
            seen.add(upper)
            skus.append(upper)

    # Nicht-SKUs herausfiltern (System-Begriffe, eBay-IDs, etc.)
    ignore = {"HTML", "HTTP", "HTTPS", "UTF8", "EUR", "USD", "IMAP", "SMTP", "PDF",
              "CSS", "API", "XML", "OVP", "DB", "AC", "DC", "BR", "ICE", "TGV",
              "RE", "IC", "EC", "HO", "TT", "EBAY", "DHL", "GLS", "DPD", "UPS"}
    # Auch zu lange oder zu kurze Codes filtern
    skus = [s for s in skus if s not in ignore and 3 <= len(s) <= 10]
    return skus[:5]  # Maximal 5 SKUs


def fetch_product_by_sku(sku):
    """Produkt ueber Artikelnummer (SKU) aus WooCommerce abrufen."""
    if not WC_KEY or not sku:
        return None
    try:
        r = requests.get(
            f"{WC_URL}/wp-json/wc/v3/products",
            auth=(WC_KEY, WC_SECRET),
            params={"sku": sku, "per_page": 1},
            timeout=30
        )
        products = r.json()
        if products and isinstance(products, list) and len(products) > 0:
            p = products[0]
            stock_status_map = {
                "instock": "Auf Lager",
                "outofstock": "Ausverkauft",
                "onbackorder": "Auf Nachbestellung"
            }
            result = {
                "sku": p.get("sku", sku),
                "name": p.get("name", ""),
                "price": p.get("price", ""),
                "regular_price": p.get("regular_price", ""),
                "stock_status": stock_status_map.get(p.get("stock_status", ""), p.get("stock_status", "")),
                "stock_quantity": p.get("stock_quantity"),
                "permalink": p.get("permalink", ""),
                "short_description": p.get("short_description", "")[:100]
            }
            log.info(f"Produkt gefunden: {sku} = {result['name']} ({result['stock_status']}, {result['price']} EUR)")
            return result
        else:
            log.info(f"Kein Produkt gefunden fuer SKU: {sku}")
    except Exception as e:
        log.warning(f"WooCommerce Produkt {sku}: {e}")
    return None


def fetch_order_by_id(order_id):
    """Eine spezifische Bestellung direkt per ID abrufen."""
    if not WC_KEY or not order_id:
        return None
    for attempt in range(2):
        try:
            r = requests.get(
                f"{WC_URL}/wp-json/wc/v3/orders/{order_id}",
                auth=(WC_KEY, WC_SECRET),
                timeout=30
            )
            if r.status_code == 200:
                return parse_order_data(r.json(), order_count=1)
            elif r.status_code == 404:
                log.info(f"Bestellung #{order_id} nicht gefunden")
                return None
            else:
                log.warning(f"WooCommerce Order #{order_id}: Status {r.status_code}")
        except requests.exceptions.Timeout:
            log.warning(f"WooCommerce Timeout fuer #{order_id} (Versuch {attempt + 1}/2)")
            if attempt == 0:
                time.sleep(3)
                continue
        except Exception as e:
            log.warning(f"WooCommerce: {e}")
            break
    return None


def parse_order_data(o, order_count=1):
    """Order-JSON in unser internes Format umwandeln."""
    items = ", ".join(
        f"{i['name']} (x{i['quantity']}, {i.get('total', '?')} EUR)"
        for i in o.get("line_items", [])
    )
    shipping = o.get("shipping", {})
    ship_addr = f"{shipping.get('city', '')}, {shipping.get('country', '')}" if shipping else ""
    customer_note = o.get("customer_note", "")
    payment = o.get("payment_method_title", "")

    status_map = {
        "processing": "In Bearbeitung",
        "completed": "Abgeschlossen",
        "on-hold": "Wartend",
        "pending": "Ausstehend",
        "cancelled": "Storniert",
        "refunded": "Erstattet",
        "failed": "Fehlgeschlagen"
    }
    status = status_map.get(o.get("status", ""), o.get("status", ""))

    result = {
        "order_id": o.get("id"),
        "status": status,
        "total": o.get("total"),
        "date": o.get("date_created", "")[:10],
        "items": items,
        "shipping_city": ship_addr,
        "payment_method": payment,
        "customer_note": customer_note,
        "order_count": order_count
    }
    coupons = o.get("coupon_lines", [])
    if coupons:
        result["coupons"] = ", ".join(c.get("code", "") for c in coupons)
    return result


def fetch_woocommerce_order(sender_email):
    """WooCommerce-Bestellungen per E-Mail suchen (Fallback wenn keine Bestellnummer)."""
    if not WC_KEY:
        return None

    for attempt in range(2):  # 2 Versuche
        try:
            r = requests.get(
                f"{WC_URL}/wp-json/wc/v3/orders",
                auth=(WC_KEY, WC_SECRET),
                params={"search": sender_email, "per_page": 3, "orderby": "date", "order": "desc"},
                timeout=30
            )
            orders = r.json()
            if orders and isinstance(orders, list):
                return parse_order_data(orders[0], order_count=len(orders))

        except requests.exceptions.Timeout:
            log.warning(f"WooCommerce Timeout (Versuch {attempt + 1}/2)")
            if attempt == 0:
                time.sleep(3)
                continue
        except Exception as e:
            log.warning(f"WooCommerce: {e}")
            break

    return None


def fetch_sendcloud_tracking(order_data, extra_search=None):
    """Sendcloud Tracking abrufen. Sucht nach order_id, oder extra_search als Fallback."""
    if not SC_KEY:
        return None

    search_terms = []
    if order_data and order_data.get("order_id"):
        search_terms.append(str(order_data["order_id"]))
    if extra_search:
        search_terms.append(str(extra_search))

    if not search_terms:
        return None

    for search in search_terms:
        try:
            r = requests.get(
                "https://panel.sendcloud.sc/api/v2/parcels",
                auth=(SC_KEY, SC_SECRET),
                params={"search": search},
                timeout=15
            )
            parcels = r.json().get("parcels", [])
            if parcels:
                p = parcels[0]
                result = {
                    "tracking_number": p.get("tracking_number"),
                    "status": p.get("status", {}).get("message", ""),
                    "carrier": p.get("carrier", {}).get("code", ""),
                    "tracking_url": p.get("tracking_url", "")
                }
                log.info(f"Sendcloud Tracking gefunden (Suche: {search}): {result['carrier']} {result['tracking_number']} - {result['status']}")
                return result
        except Exception as e:
            log.warning(f"Sendcloud: {e}")

    return None


def build_context(sender_email, order_data, tracking_data, product_data=None):
    lines = []
    if order_data:
        lines.append(f"Bestellung #{order_data['order_id']}: {order_data['items']}")
        lines.append(f"Status: {order_data['status']} | Datum: {order_data['date']} | Betrag: {order_data['total']} EUR")
        if order_data.get("payment_method"):
            lines.append(f"Bezahlung: {order_data['payment_method']}")
        if order_data.get("shipping_city"):
            lines.append(f"Versand nach: {order_data['shipping_city']}")
        if order_data.get("customer_note"):
            lines.append(f"Kundennotiz: {order_data['customer_note']}")
        if order_data.get("order_count", 0) > 1:
            lines.append(f"Stammkunde: {order_data['order_count']} Bestellungen gefunden")
        if order_data.get("coupons"):
            lines.append(f"Gutscheine: {order_data['coupons']}")
    if tracking_data:
        lines.append(f"Sendung: {tracking_data['carrier']} {tracking_data['tracking_number']}")
        lines.append(f"Paketstatus: {tracking_data['status']}")
        if tracking_data.get("tracking_url"):
            lines.append(f"Tracking: {tracking_data['tracking_url']}")
    if product_data:
        lines.append("\nARTIKEL-INFORMATIONEN:")
        for prod in product_data:
            lines.append(f"  Artikelnr: {prod['sku']} | {prod['name']}")
            lines.append(f"  Preis: {prod['price']} EUR | Verfuegbarkeit: {prod['stock_status']}")
            if prod.get("stock_quantity") is not None:
                lines.append(f"  Lagerbestand: {prod['stock_quantity']} Stueck")
            if prod.get("permalink"):
                lines.append(f"  Shop-Link: {prod['permalink']}")
    return "\n".join(lines) if lines else "Keine Bestelldaten gefunden."


def generate_draft(subject, body, sender, channel, context, category):
    """Antwort generieren mit Kategorie und Feedback-Kontext.
    Der VOLLSTAENDIGE Mail-Body wird uebergeben, inkl. Konversations-Historie."""
    channel_hint = "eBay-Nachricht" if channel == "ebay" else "Shop-Mail"
    feedback_section = build_feedback_prompt()
    full_system = SYSTEM_PROMPT + feedback_section

    resp = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1500,
        system=full_system,
        messages=[{"role": "user", "content": (
            f"Kanal: {channel_hint}\n"
            f"Kategorie: {category}\n"
            f"Absender: {sender}\n"
            f"Betreff: {subject}\n\n"
            f"BESTELLDATEN:\n{context}\n\n"
            f"KUNDEN-NACHRICHT (inkl. evtl. vorheriger Mail-Verlauf):\n{body}\n\n"
            f"WICHTIG: Falls die Nachricht einen Mail-Verlauf enthaelt (zitierte fruehere Nachrichten), "
            f"beruecksichtige den GESAMTEN Kontext der bisherigen Konversation fuer deine Antwort. "
            f"Antworte nur auf die NEUESTE Nachricht des Kunden, aber mit Wissen ueber den gesamten Verlauf.\n\n"
            f"Erstelle die fertige Antwort. Beachte die kategorie-spezifischen Anweisungen fuer '{category}'.\n"
            f"Erste Zeile: BETREFF: Re: {subject}"
        )}]
    )
    return resp.content[0].text.strip()


def send_telegram_text(text, reply_markup=None):
    url     = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            return r.json().get("result", {}).get("message_id")
    except Exception as e:
        log.error(f"Telegram: {e}")
    return None


def send_telegram_photo(image_data, caption=""):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto"
    try:
        r = requests.post(url, files={"photo": ("image.jpg", image_data, "image/jpeg")},
                      data={"chat_id": TG_CHAT_ID, "caption": caption}, timeout=15)
        if r.status_code == 200:
            return r.json().get("result", {}).get("message_id")
    except Exception as e:
        log.error(f"Telegram Foto: {e}")
    return None


def delete_telegram_messages(message_ids):
    """Loesche mehrere Telegram-Nachrichten."""
    for msg_id in message_ids:
        if msg_id:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{TG_TOKEN}/deleteMessage",
                    json={"chat_id": TG_CHAT_ID, "message_id": msg_id},
                    timeout=5
                )
            except:
                pass


def detect_language(text):
    """Sprache eines Textes erkennen (einfache Heuristik)."""
    # Deutsche Indikatoren
    german_words = ["sehr", "geehrte", "guten", "bitte", "danke", "liebe", "grüße", "gruesse",
                    "bestellung", "lieferung", "rechnung", "frage", "artikel", "haben", "können",
                    "moechte", "wäre", "würde", "freundlichen", "melden", "vielen", "dank"]
    text_lower = text.lower()
    german_count = sum(1 for w in german_words if w in text_lower)
    # Wenn wenig deutsche Woerter und genuegend Text -> vermutlich fremdsprachig
    if len(text) > 50 and german_count < 2:
        return "foreign"
    return "german"


def translate_to_german(text, label="Text"):
    """Uebersetze einen Text ins Deutsche via Claude."""
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            messages=[{"role": "user", "content": (
                f"Uebersetze den folgenden Text ins Deutsche. "
                f"Antworte NUR mit der deutschen Uebersetzung, keine Erklaerungen.\n\n{text}"
            )}]
        )
        return resp.content[0].text.strip()
    except Exception as e:
        log.warning(f"Uebersetzung fehlgeschlagen: {e}")
        return None


def send_long_telegram_text(text, reply_markup=None):
    """Telegram-Nachricht senden, bei Bedarf in mehrere Teile aufgeteilt. Gibt Liste der Message-IDs zurueck."""
    MAX_LEN = 4000
    msg_ids = []
    if len(text) <= MAX_LEN:
        mid = send_telegram_text(text, reply_markup)
        if mid:
            msg_ids.append(mid)
        return msg_ids

    parts = []
    while text:
        if len(text) <= MAX_LEN:
            parts.append(text)
            break
        split_pos = text.rfind("\n", 0, MAX_LEN)
        if split_pos < MAX_LEN // 2:
            split_pos = MAX_LEN
        parts.append(text[:split_pos])
        text = text[split_pos:].lstrip("\n")

    for i, part in enumerate(parts):
        if i == len(parts) - 1 and reply_markup:
            mid = send_telegram_text(part, reply_markup)
        else:
            mid = send_telegram_text(part)
        if mid:
            msg_ids.append(mid)
    return msg_ids


def send_approval_request(token, sender, subject, body, draft, channel, order_context, images, category, translation_customer=None, translation_draft=None, ebay_item_id=None):
    lines        = draft.split("\n")
    mail_body    = "\n".join(l for l in lines if not l.startswith("BETREFF:")).strip()
    kanal        = "🏪 eBay" if channel == "ebay" else "🛒 Shop"

    # Kategorie-Emoji fuer Telegram
    cat_emoji = {
        "lieferstatus": "📦", "retoure": "↩️", "beschwerde": "⚠️",
        "produktfrage": "❓", "stornierung": "❌", "rechnung_steuer": "🧾",
        "kombiversand": "📮", "rabattanfrage": "💰", "kontaktformular": "📋"
    }
    cat_icon = cat_emoji.get(category, "📧")

    # VOLLSTAENDIGER Body - kein Kuerzen mehr!
    full_body = body.strip()
    ctx_display = order_context

    # Nachricht zusammenbauen
    msg = (
        f"{kanal} {cat_icon} <b>{category.upper()}</b>\n"
        f"Von: <code>{sender}</code>\n"
        f"Betreff: {subject}\n"
    )

    # eBay Artikel-Link anzeigen falls vorhanden
    if ebay_item_id and channel == "ebay":
        msg += f"🔗 <a href=\"https://www.ebay.de/itm/{ebay_item_id}\">eBay Angebot #{ebay_item_id}</a>\n"

    msg += (
        f"\n<b>Kunden-Nachricht:</b>\n"
        f"--------------------\n"
        f"{full_body}\n"
        f"--------------------"
    )

    # Bei fremdsprachigen Mails: Deutsche Uebersetzung anhaengen
    if translation_customer:
        msg += (
            f"\n\n🌐 <b>Deutsche Übersetzung (Kunden-Nachricht):</b>\n"
            f"--------------------\n"
            f"{translation_customer}\n"
            f"--------------------"
        )

    msg += (
        f"\n\n<b>Bestelldaten:</b>\n<code>{ctx_display}</code>\n\n"
        f"<b>Mein Vorschlag:</b>\n"
        f"--------------------\n"
        f"{mail_body}\n"
        f"--------------------"
    )

    # Bei fremdsprachigen Antworten: Deutsche Uebersetzung anhaengen
    if translation_draft:
        msg += (
            f"\n\n🌐 <b>Deutsche Übersetzung (Antwort):</b>\n"
            f"--------------------\n"
            f"{translation_draft}\n"
            f"--------------------"
        )

    keyboard = {"inline_keyboard": [
        [{"text": "✅ Senden",      "callback_data": f"approve:{token}"},
         {"text": "✏️ Aendern",    "callback_data": f"edit:{token}"}],
        [{"text": "🗑️ Ignorieren", "callback_data": f"ignore:{token}"}]
    ]}
    msg_ids = send_long_telegram_text(msg, keyboard)
    for i, img in enumerate(images):
        mid = send_telegram_photo(img, f"📷 Bild {i+1} von {len(images)}")
        if mid:
            msg_ids.append(mid)
    return msg_ids


def fetch_invoice_pdf(order_id):
    """Rechnungs-PDF von WordPress/German Market herunterladen."""
    if not WP_USER or not WP_APP_PASS or not order_id:
        return None
    try:
        import base64
        # WordPress Application Password Auth
        credentials = base64.b64encode(f"{WP_USER}:{WP_APP_PASS}".encode()).decode()

        # Zuerst brauchen wir einen gueltigen Nonce
        # Wir nutzen den wp-admin AJAX Endpoint mit Basic Auth
        session = requests.Session()
        session.headers.update({"Authorization": f"Basic {credentials}"})

        # Direkt den Invoice-Download Endpoint aufrufen
        r = session.get(
            f"{WC_URL}/wp-admin/admin-ajax.php",
            params={
                "action": "woocommerce_wp_wc_invoice_pdf_invoice_download",
                "order_id": str(order_id)
            },
            timeout=30
        )
        if r.status_code == 200 and r.headers.get("Content-Type", "").startswith("application/pdf"):
            log.info(f"Rechnungs-PDF heruntergeladen fuer Bestellung #{order_id} ({len(r.content)} Bytes)")
            return r.content
        else:
            log.warning(f"Rechnungs-PDF nicht verfuegbar fuer #{order_id} (Status: {r.status_code})")
    except Exception as e:
        log.warning(f"Rechnungs-PDF Fehler: {e}")
    return None


def send_mail(to_addr, subject, body, pdf_attachment=None, pdf_filename=None, in_reply_to=None, references=None):
    """Mail senden ueber Brevo HTTP API, optional mit PDF-Anhang und Threading-Headers."""
    import base64
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    from email.mime.application import MIMEApplication

    full_body = body.strip() + "\n\n-- \n" + SIGNATURE
    payload = {
        "sender": {"name": "Modellbahn-Rhein-Main", "email": MAIL_USER},
        "to": [{"email": to_addr}],
        "subject": subject,
        "textContent": full_body,
        "replyTo": {"email": MAIL_USER}
    }

    # Threading-Headers fuer Mail-Verlauf (In-Reply-To / References)
    if in_reply_to:
        # Brevo unterstuetzt custom headers
        mail_headers = {}
        mail_headers["In-Reply-To"] = in_reply_to
        if references:
            mail_headers["References"] = f"{references} {in_reply_to}"
        else:
            mail_headers["References"] = in_reply_to
        payload["headers"] = mail_headers
        log.info(f"Mail-Threading: In-Reply-To {in_reply_to[:50]}...")

    # PDF-Anhang hinzufuegen falls vorhanden
    if pdf_attachment and pdf_filename:
        pdf_b64 = base64.b64encode(pdf_attachment).decode()
        payload["attachment"] = [{
            "content": pdf_b64,
            "name": pdf_filename
        }]
        log.info(f"PDF-Anhang: {pdf_filename} ({len(pdf_attachment)} Bytes)")

    headers = {
        "api-key": BREVO_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    try:
        r = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers=headers,
            json=payload,
            timeout=15
        )
        if r.status_code in (200, 201):
            log.info(f"Mail gesendet an {to_addr}: {subject}")

            # Kopie in Gesendet-Ordner ablegen (mit Threading)
            save_to_sent_folder(to_addr, subject, full_body, pdf_attachment, pdf_filename, in_reply_to, references)

            return True
        else:
            log.error(f"Brevo Fehler {r.status_code}: {r.text}")
            return False
    except Exception as e:
        log.error(f"Brevo Fehler: {e}")
        return False


def save_to_sent_folder(to_addr, subject, full_body, pdf_attachment=None, pdf_filename=None, in_reply_to=None, references=None):
    """Gesendete Mail per IMAP im Gesendet-Ordner ablegen (mit Threading-Headers)."""
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    from email.mime.application import MIMEApplication
    from email.utils import formatdate

    try:
        # E-Mail zusammenbauen
        if pdf_attachment and pdf_filename:
            msg = MIMEMultipart()
            msg.attach(MIMEText(full_body, "plain", "utf-8"))
            pdf_part = MIMEApplication(pdf_attachment, _subtype="pdf")
            pdf_part.add_header("Content-Disposition", "attachment", filename=pdf_filename)
            msg.attach(pdf_part)
        else:
            msg = MIMEText(full_body, "plain", "utf-8")

        msg["Subject"] = subject
        msg["From"]    = f"Modellbahn-Rhein-Main <{MAIL_USER}>"
        msg["To"]      = to_addr
        msg["Date"]    = formatdate(localtime=True)

        # Threading-Headers fuer Mail-Verlauf
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            if references:
                msg["References"] = f"{references} {in_reply_to}"
            else:
                msg["References"] = in_reply_to

        # Per IMAP in Gesendet-Ordner ablegen
        with imaplib.IMAP4_SSL(MAIL_HOST) as imap:
            imap.login(MAIL_USER, MAIL_PASS)

            # Gaengige Namen fuer den Gesendet-Ordner probieren
            sent_folders = ["Sent", "INBOX.Sent", "Gesendet", "INBOX.Gesendet",
                           "Sent Messages", "Sent Items", "INBOX.Sent Messages"]
            sent_folder = None

            # Ordnerliste vom Server holen
            _, folder_list = imap.list()
            available = []
            for f in folder_list:
                if f:
                    decoded = f.decode() if isinstance(f, bytes) else f
                    available.append(decoded)

            for folder in sent_folders:
                try:
                    status, _ = imap.select(f'"{folder}"')
                    if status == "OK":
                        sent_folder = folder
                        break
                except:
                    continue

            if sent_folder:
                imap.append(
                    f'"{sent_folder}"',
                    "\\Seen",
                    imaplib.Time2Internaldate(time.time()),
                    msg.as_bytes()
                )
                log.info(f"Mail in '{sent_folder}' abgelegt fuer {to_addr}")
            else:
                log.warning(f"Gesendet-Ordner nicht gefunden. Verfuegbar: {available[:5]}")

    except Exception as e:
        log.warning(f"Gesendet-Ordner: {e} (Mail wurde trotzdem gesendet)")


def process_mail(subject, sender, body, channel="shop", ebay_thread_id=None, images=None, ebay_item_id=None, ebay_recipient=None, original_message_id=None, original_references=None, imap_uid=None):
    token = hashlib.md5(f"{sender}{subject}{body[:50]}".encode()).hexdigest()[:8]
    if token in pending:
        return

    # Verfeinerte Klassifizierung
    category = classify_mail(subject, body)
    if category == "ignore":
        log.info(f"Ignoriert: {subject}")
        return

    log.info(f"Kategorie: {category} | {subject}")
    sender_email = sender.split("<")[-1].replace(">", "").strip()

    # === INTELLIGENTE BESTELLSUCHE ===
    order_data = None
    extra_sendcloud_search = None

    # Schritt 1: Bestellnummer aus der Mail extrahieren und direkt suchen
    order_number = extract_order_number(subject, body)
    if order_number:
        log.info(f"Bestellnummer aus Mail extrahiert: #{order_number}")
        order_data = fetch_order_by_id(order_number)
        extra_sendcloud_search = order_number

    # Schritt 2: Rechnungsnummer extrahieren und darüber Bestellung finden
    if not order_data:
        invoice_number = extract_invoice_number(subject, body)
        if invoice_number:
            log.info(f"Rechnungsnummer aus Mail extrahiert: {invoice_number}")
            order_data = find_order_by_invoice_number(invoice_number)
            if not extra_sendcloud_search:
                extra_sendcloud_search = invoice_number

    # Schritt 3: Fallback - nach E-Mail-Adresse suchen
    if not order_data:
        order_data = fetch_woocommerce_order(sender_email)

    # Schritt 4: Artikelnummern (SKUs) aus der Mail extrahieren und Produkte nachschlagen
    skus = extract_sku_codes(subject, body)
    product_data = []
    if skus:
        log.info(f"Artikelnummern aus Mail extrahiert: {skus}")
        for sku in skus:
            prod = fetch_product_by_sku(sku)
            if prod:
                product_data.append(prod)

    # Schritt 5: Sendcloud Tracking - mit order_data UND extra Suchbegriff
    tracking = fetch_sendcloud_tracking(order_data, extra_sendcloud_search)

    context  = build_context(sender_email, order_data, tracking, product_data)

    # VOLLSTAENDIGEN Body an Claude uebergeben (inkl. Konversations-Historie)
    draft = generate_draft(subject, body, sender, channel, context, category)

    # Sprache erkennen und ggf. uebersetzen
    translation_customer = None
    translation_draft = None
    lang = detect_language(body)
    if lang == "foreign":
        log.info("Fremdsprachige Mail erkannt - uebersetze fuer Fabian")
        translation_customer = translate_to_german(body, "Kunden-Nachricht")
        # Auch den Antwort-Entwurf uebersetzen
        draft_body = "\n".join(l for l in draft.split("\n") if not l.startswith("BETREFF:")).strip()
        translation_draft = translate_to_german(draft_body, "Antwort-Entwurf")

    pending[token] = {
        "sender": sender_email, "subject": subject, "body": body,
        "draft": draft, "channel": channel, "category": category,
        "ebay_thread_id": ebay_thread_id,
        "ebay_item_id": ebay_item_id,
        "ebay_recipient": ebay_recipient,
        "order_id": order_data.get("order_id") if order_data else None,
        "order_context": context, "images": images or [],
        "translation_customer": translation_customer,
        "translation_draft": translation_draft,
        "original_message_id": original_message_id,
        "original_references": original_references,
        "imap_uid": imap_uid,
        "telegram_msg_ids": []
    }
    tg_msg_ids = send_approval_request(
        token, sender_email, subject, body, draft, channel, context,
        images or [], category, translation_customer, translation_draft,
        ebay_item_id=ebay_item_id
    )
    pending[token]["telegram_msg_ids"] = tg_msg_ids or []
    log.info(f"Entwurf gesendet fuer {sender_email} (Token: {token}, Kategorie: {category})")


def is_ebay_notification(sender):
    """Pruefe ob die Mail eine eBay-Benachrichtigung ist (ignorieren)."""
    sender_lower = sender.lower()
    ebay_domains = ["@members.ebay.de", "@members.ebay.com", "@ebay.de", "@ebay.com",
                    "@reply.ebay.de", "@reply.ebay.com"]
    return any(domain in sender_lower for domain in ebay_domains)


def check_inbox():
    try:
        with imaplib.IMAP4_SSL(MAIL_HOST) as imap:
            imap.login(MAIL_USER, MAIL_PASS)
            imap.select("INBOX")
            _, data = imap.search(None, "UNSEEN")
            ids     = data[0].split()
            log.info(f"{len(ids)} ungelesene Mail(s)")
            for mid in ids:
                _, msg_data = imap.fetch(mid, "(RFC822)")
                msg     = email_lib.message_from_bytes(msg_data[0][1])
                subject = decode_str(msg.get("Subject", ""))
                sender  = msg.get("From", "")

                # eBay-Mails ignorieren (werden ueber eBay API abgerufen)
                if is_ebay_notification(sender):
                    log.info(f"eBay-Mail ignoriert: {subject}")
                    # eBay-Mails trotzdem als gelesen markieren damit sie nicht nochmal kommen
                    imap.store(mid, '+FLAGS', '\\Seen')
                    continue

                # Message-ID und References fuer Threading speichern
                original_message_id = msg.get("Message-ID", "")
                original_references = msg.get("References", "")

                body, images = get_mail_body_and_images(msg)
                process_mail(subject, sender, body, channel="shop", images=images,
                            original_message_id=original_message_id,
                            original_references=original_references,
                            imap_uid=mid.decode() if isinstance(mid, bytes) else str(mid))
    except Exception as e:
        log.error(f"IMAP: {e}")


def get_telegram_updates(offset=0):
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
            params={"offset": offset, "timeout": 30},
            timeout=35
        )
        return r.json().get("result", [])
    except:
        return []


def handle_telegram_update(update):
    if "callback_query" in update:
        cq     = update["callback_query"]
        data   = cq.get("data", "")
        action, token = data.split(":", 1) if ":" in data else (data, "")
        if token not in pending:
            send_telegram_text("⚠️ Vorgang nicht mehr gefunden.")
            return
        p = pending[token]
        if action == "approve":
            lines = p["draft"].split("\n")
            subj  = next((l.replace("BETREFF:", "").strip() for l in lines if l.startswith("BETREFF:")), f"Re: {p['subject']}")
            body  = "\n".join(l for l in lines if not l.startswith("BETREFF:")).strip()

            # eBay-Nachrichten ueber eBay API beantworten, Shop-Mails per Brevo
            if p.get("channel") == "ebay" and p.get("ebay_thread_id") and EBAY_ENABLED:
                ok = ebay_send_reply(
                    p["ebay_thread_id"], body,
                    recipient=p.get("ebay_recipient"),
                    item_id=p.get("ebay_item_id")
                )
                channel_label = "eBay"
            else:
                # Bei Rechnungsanfragen: PDF automatisch anhaengen
                pdf_data = None
                pdf_name = None
                order_id = p.get("order_id")
                category = p.get("category", "")

                if category == "rechnung_steuer" and order_id:
                    send_telegram_text(f"📄 Lade Rechnungs-PDF für Bestellung #{order_id}...")
                    pdf_data = fetch_invoice_pdf(order_id)
                    if pdf_data:
                        pdf_name = f"Rechnung_{order_id}.pdf"
                        send_telegram_text(f"✅ Rechnung gefunden, wird angehängt!")
                    else:
                        send_telegram_text(f"⚠️ Keine Rechnung für #{order_id} gefunden. Mail wird ohne Anhang gesendet.")

                ok = send_mail(p["sender"], subj, body,
                              pdf_attachment=pdf_data, pdf_filename=pdf_name,
                              in_reply_to=p.get("original_message_id"),
                              references=p.get("original_references"))
                channel_label = "Mail" + (" + Rechnung" if pdf_data else "")

            del pending[token]
            if ok:
                send_telegram_text(f"✅ {channel_label} an <code>{p['sender']}</code> gesendet!")
                # Mail als gelesen + beantwortet markieren (Antwort-Pfeil in Apple Mail)
                mark_mail_as_seen(p.get("imap_uid"))
                mark_mail_as_answered(p.get("imap_uid"))
                # Statistik
                reset_daily_stats_if_needed()
                daily_stats["answered"] += 1
                if p.get("channel") == "ebay":
                    daily_stats["ebay_answered"] += 1
                else:
                    daily_stats["shop_answered"] += 1
            else:
                send_telegram_text(f"⚠️ Fehler! Bitte manuell antworten an {p['sender']}")
            # Alte Nachrichten aus Telegram loeschen
            delete_telegram_messages(p.get("telegram_msg_ids", []))
        elif action == "edit":
            pending[token]["awaiting_edit"] = True
            # Statistik: Korrektur zaehlen
            reset_daily_stats_if_needed()
            daily_stats["edited"] += 1
            send_telegram_text(
                f"✏️ Was soll ich aendern?\n\n"
                f"Beispiele:\n"
                f"- 15 EUR Erstattung anbieten\n"
                f"- Freundlicher formulieren\n"
                f"- Retourenlink Shop einfuegen\n"
                f"- Auf Englisch schreiben\n\n"
                f"Token: <code>{token}</code>"
            )
        elif action == "ignore":
            tg_ids = p.get("telegram_msg_ids", [])
            # Mail als gelesen markieren
            mark_mail_as_seen(p.get("imap_uid"))
            # Statistik
            reset_daily_stats_if_needed()
            daily_stats["ignored"] += 1
            del pending[token]
            send_telegram_text("🗑️ Vorgang ignoriert.")
            # Alte Nachrichten aus Telegram loeschen
            delete_telegram_messages(tg_ids)

    elif "message" in update:
        text = update["message"].get("text", "").strip()
        msg_id = update["message"].get("message_id")

        if not text:
            return

        # Befehle verarbeiten
        if text == "/clean" or text == "/clean@ModellbahnAssistentBot":
            clean_telegram_chat(msg_id)
            return

        if text == "/status" or text == "/status@ModellbahnAssistentBot":
            fb_count = len(load_feedback())
            pending_count = len(pending)
            ebay_status = "aktiv" if EBAY_ENABLED else "nicht konfiguriert"
            status_msg = (
                f"📊 <b>Bot-Status</b>\n"
                f"Offene Vorgaenge: {pending_count}\n"
                f"Korrekturen im Archiv: {fb_count}\n"
                f"eBay API: {ebay_status}\n"
                f"eBay verarbeitet: {len(ebay_processed_ids)} Nachrichten"
            )
            send_telegram_text(status_msg)
            return

        if text == "/stats" or text == "/stats@ModellbahnAssistentBot":
            reset_daily_stats_if_needed()
            send_daily_summary()
            return

        if text.startswith("/"):
            return

        for token, p in list(pending.items()):
            if p.get("awaiting_edit"):
                lines    = p["draft"].split("\n")
                old_body = "\n".join(l for l in lines if not l.startswith("BETREFF:")).strip()

                # Feedback-Kontext auch beim Aendern nutzen
                feedback_section = build_feedback_prompt()
                full_system = SYSTEM_PROMPT + feedback_section

                resp = client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=1000,
                    system=full_system,
                    messages=[
                        {"role": "user",      "content": f"Bisheriger Entwurf:\n\n{old_body}"},
                        {"role": "assistant", "content": old_body},
                        {"role": "user",      "content": f"Bitte aendere: {text}\n\nVollstaendige ueberarbeitete Mail, erste Zeile: BETREFF: ..."}
                    ]
                )
                new_draft = resp.content[0].text.strip()

                # === FEEDBACK-LOOP: Korrektur speichern ===
                save_feedback({
                    "timestamp": datetime.now().isoformat(),
                    "category": p.get("category", "unbekannt"),
                    "customer_query": p.get("body", "")[:200],
                    "original_draft": old_body[:300],
                    "edit_instruction": text,
                    "corrected_draft": new_draft[:300]
                })

                pending[token]["draft"]         = new_draft
                pending[token]["awaiting_edit"] = False

                # Bei fremdsprachiger Korrektur: neue Uebersetzung
                translation_draft = None
                if p.get("translation_customer"):
                    draft_text = "\n".join(l for l in new_draft.split("\n") if not l.startswith("BETREFF:")).strip()
                    translation_draft = translate_to_german(draft_text, "Antwort-Entwurf")
                    pending[token]["translation_draft"] = translation_draft

                # Alte Telegram-Nachrichten loeschen
                delete_telegram_messages(p.get("telegram_msg_ids", []))

                new_tg_ids = send_approval_request(
                    token, p["sender"], p["subject"], p["body"],
                    new_draft, p["channel"], p["order_context"], p["images"],
                    p.get("category", "unbekannt"),
                    p.get("translation_customer"),
                    translation_draft or p.get("translation_draft"),
                    ebay_item_id=p.get("ebay_item_id")
                )
                pending[token]["telegram_msg_ids"] = new_tg_ids or []
                break


def clean_telegram_chat(command_msg_id=None):
    """Loesche alle Bot-Nachrichten aus dem Telegram-Chat (Inbox Zero)."""
    try:
        # Zuerst das /clean Kommando selbst loeschen
        if command_msg_id:
            delete_telegram_messages([command_msg_id])

        # Sonde senden um aktuelle Message-ID zu bekommen
        probe = send_telegram_text("🧹 Räume auf...")
        if probe:
            # Loesche von probe-ID rueckwaerts (max 300 Nachrichten, ca. 48h)
            for mid in range(probe, max(probe - 300, 0), -1):
                try:
                    requests.post(
                        f"https://api.telegram.org/bot{TG_TOKEN}/deleteMessage",
                        json={"chat_id": TG_CHAT_ID, "message_id": mid},
                        timeout=2
                    )
                except:
                    pass

        send_telegram_text("✨ Chat aufgeräumt!")
        log.info("Telegram Chat aufgeraeumt via /clean")

    except Exception as e:
        log.warning(f"Chat aufraeumen: {e}")
        send_telegram_text("⚠️ Konnte nicht alle Nachrichten löschen.")


def mark_mail_as_seen(imap_uid):
    """Markiere eine Mail als gelesen per IMAP."""
    if not imap_uid:
        return
    try:
        with imaplib.IMAP4_SSL(MAIL_HOST) as imap:
            imap.login(MAIL_USER, MAIL_PASS)
            imap.select("INBOX")
            imap.store(imap_uid.encode() if isinstance(imap_uid, str) else imap_uid, '+FLAGS', '\\Seen')
            log.info(f"Mail als gelesen markiert (UID: {imap_uid})")
    except Exception as e:
        log.warning(f"Mail als gelesen markieren: {e}")


def mark_mail_as_answered(imap_uid):
    """Markiere eine Mail als beantwortet per IMAP (zeigt Antwort-Pfeil in Apple Mail)."""
    if not imap_uid:
        return
    try:
        with imaplib.IMAP4_SSL(MAIL_HOST) as imap:
            imap.login(MAIL_USER, MAIL_PASS)
            imap.select("INBOX")
            imap.store(imap_uid.encode() if isinstance(imap_uid, str) else imap_uid, '+FLAGS', '\\Answered')
            log.info(f"Mail als beantwortet markiert (UID: {imap_uid})")
    except Exception as e:
        log.warning(f"Mail als beantwortet markieren: {e}")


# ============================================================
# TAGES-STATISTIK
# ============================================================

daily_stats = {
    "date": "",
    "answered": 0,
    "ignored": 0,
    "edited": 0,
    "ebay_answered": 0,
    "shop_answered": 0
}


def reset_daily_stats_if_needed():
    """Setze Statistik zurueck wenn ein neuer Tag beginnt."""
    today = datetime.now().strftime("%Y-%m-%d")
    if daily_stats["date"] != today:
        daily_stats["date"] = today
        daily_stats["answered"] = 0
        daily_stats["ignored"] = 0
        daily_stats["edited"] = 0
        daily_stats["ebay_answered"] = 0
        daily_stats["shop_answered"] = 0


def send_daily_summary():
    """Sende Tages-Zusammenfassung per Telegram (abends um 20:00)."""
    total = daily_stats["answered"] + daily_stats["ignored"]
    if total == 0:
        return  # Nichts zu berichten

    msg = (
        f"📊 <b>Tages-Zusammenfassung ({daily_stats['date']})</b>\n\n"
        f"✅ Beantwortet: {daily_stats['answered']}\n"
        f"   🛒 Shop: {daily_stats['shop_answered']}\n"
        f"   🏪 eBay: {daily_stats['ebay_answered']}\n"
        f"✏️ Davon korrigiert: {daily_stats['edited']}\n"
        f"🗑️ Ignoriert: {daily_stats['ignored']}\n"
        f"📬 Gesamt verarbeitet: {total}"
    )
    send_telegram_text(msg)


# ============================================================
# eBAY API: Nachrichten direkt aus dem eBay-Portal abrufen
# Aktivieren sobald Developer Account freigeschaltet ist
# ============================================================

EBAY_CLIENT_ID     = os.environ.get("EBAY_CLIENT_ID", "")
EBAY_CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET", "")
EBAY_REFRESH_TOKEN = os.environ.get("EBAY_REFRESH_TOKEN", "")
EBAY_ENABLED       = bool(EBAY_CLIENT_ID and EBAY_CLIENT_SECRET and EBAY_REFRESH_TOKEN)


def ebay_get_access_token():
    """Hole einen neuen Access Token ueber den Refresh Token."""
    if not EBAY_ENABLED:
        return None
    try:
        import base64
        credentials = base64.b64encode(f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode()).decode()
        r = requests.post(
            "https://api.ebay.com/identity/v1/oauth2/token",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {credentials}"
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": EBAY_REFRESH_TOKEN,
                "scope": "https://api.ebay.com/oauth/api_scope https://api.ebay.com/oauth/api_scope/sell.fulfillment https://api.ebay.com/oauth/api_scope/commerce.notification.subscription https://api.ebay.com/oauth/api_scope/sell.inventory"
            },
            timeout=15
        )
        if r.status_code == 200:
            token = r.json().get("access_token")
            log.info("eBay Access Token erneuert")
            return token
        else:
            log.error(f"eBay Token Fehler {r.status_code}: {r.text}")
    except Exception as e:
        log.error(f"eBay Token: {e}")
    return None


# Bereits verarbeitete eBay-Nachrichten merken (um Duplikate zu vermeiden)
ebay_processed_ids = set()


def ebay_check_messages():
    """Pruefe eBay-Nachrichten ueber die Trading API (GetMyMessages) - nur ungelesene."""
    if not EBAY_ENABLED:
        return
    token = ebay_get_access_token()
    if not token:
        return
    try:
        import re

        headers_xml = {
            "X-EBAY-API-SITEID": "77",  # 77 = eBay Deutschland
            "X-EBAY-API-COMPATIBILITY-LEVEL": "967",
            "X-EBAY-API-CALL-NAME": "GetMyMessages",
            "X-EBAY-API-IAF-TOKEN": token,
            "Content-Type": "text/xml"
        }

        # Nur UNGELESENE Nachrichten aus dem Posteingang holen
        xml_request = f"""<?xml version="1.0" encoding="utf-8"?>
<GetMyMessagesRequest xmlns="urn:ebay:apis:eBLBaseComponents">
    <RequesterCredentials>
        <eBayAuthToken>{token}</eBayAuthToken>
    </RequesterCredentials>
    <FolderID>0</FolderID>
    <DetailLevel>ReturnHeaders</DetailLevel>
</GetMyMessagesRequest>"""

        r = requests.post(
            "https://api.ebay.com/ws/api.dll",
            headers=headers_xml,
            data=xml_request.encode("utf-8"),
            timeout=20
        )

        if r.status_code != 200:
            log.warning(f"eBay GetMyMessages Fehler {r.status_code}")
            return

        response_text = r.text

        # Pruefen ob Fehler
        if "<Ack>Failure</Ack>" in response_text:
            error_match = re.search(r'<LongMessage>(.*?)</LongMessage>', response_text)
            error_msg = error_match.group(1) if error_match else "Unbekannt"
            log.warning(f"eBay API Fehler: {error_msg}")
            return

        # Message-IDs und Read-Status extrahieren
        # Jede Message hat <MessageID> und <Read>true/false</Read>
        messages = re.findall(r'<Message>(.*?)</Message>', response_text, re.DOTALL)
        unread_ids = []
        for msg_block in messages:
            mid_match = re.search(r'<MessageID>(.*?)</MessageID>', msg_block)
            read_match = re.search(r'<Read>(.*?)</Read>', msg_block)
            if mid_match:
                msg_id = mid_match.group(1)
                is_read = read_match and read_match.group(1).lower() == "true"
                if not is_read:
                    unread_ids.append(msg_id)

        if not unread_ids:
            log.info("eBay: Keine ungelesenen Nachrichten")
            return

        log.info(f"eBay: {len(unread_ids)} ungelesene Nachrichten gefunden")

        # Schritt 2: Details fuer jede ungelesene Nachricht abrufen
        for msg_id in unread_ids[:5]:  # Max 5 auf einmal
            detail_xml = f"""<?xml version="1.0" encoding="utf-8"?>
<GetMyMessagesRequest xmlns="urn:ebay:apis:eBLBaseComponents">
    <RequesterCredentials>
        <eBayAuthToken>{token}</eBayAuthToken>
    </RequesterCredentials>
    <MessageIDs>
        <MessageID>{msg_id}</MessageID>
    </MessageIDs>
    <DetailLevel>ReturnMessages</DetailLevel>
</GetMyMessagesRequest>"""

            r2 = requests.post(
                "https://api.ebay.com/ws/api.dll",
                headers=headers_xml,
                data=detail_xml.encode("utf-8"),
                timeout=20
            )

            if r2.status_code != 200:
                continue

            # Encoding sicherstellen (eBay sendet manchmal Latin-1)
            try:
                detail_text = r2.content.decode("utf-8")
            except UnicodeDecodeError:
                detail_text = r2.content.decode("latin-1", errors="replace")

            # Nachrichtendetails extrahieren
            sender_match = re.search(r'<Sender>(.*?)</Sender>', detail_text)
            subject_match = re.search(r'<Subject>(.*?)</Subject>', detail_text)
            body_match = re.search(r'<Text>(.*?)</Text>', detail_text, re.DOTALL)
            item_match = re.search(r'<ItemID>(.*?)</ItemID>', detail_text)
            ext_msg_id_match = re.search(r'<ExternalMessageID>(.*?)</ExternalMessageID>', detail_text)
            # Auch ResponseDetails/MessageType pruefen ob es eine eingehende Nachricht ist
            msg_type_match = re.search(r'<MessageType>(.*?)</MessageType>', detail_text)
            is_question = not msg_type_match or msg_type_match.group(1) != "AskSellerQuestion"

            sender = sender_match.group(1) if sender_match else "eBay-Kaeufer"
            subject = subject_match.group(1) if subject_match else "eBay Nachricht"
            raw_body = body_match.group(1) if body_match else ""
            item_id = item_match.group(1) if item_match else ""
            ext_id = ext_msg_id_match.group(1) if ext_msg_id_match else msg_id

            # HTML aus Body entfernen - nutze die html_to_text Funktion fuer komplettes HTML
            if raw_body.strip().startswith("<!") or raw_body.strip().startswith("<html") or "<body" in raw_body.lower():
                # Vollstaendiges HTML-Dokument -> html_to_text verwenden
                body = html_to_text(raw_body)
            else:
                # Einfache HTML-Tags entfernen
                body = re.sub(r'<[^>]+>', ' ', raw_body)
                body = body.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
                body = body.replace('&nbsp;', ' ').replace('&quot;', '"').replace('&apos;', "'")
                body = re.sub(r'\s+', ' ', body).strip()

            # Auch Subject bereinigen
            subject = subject.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
            subject = subject.replace('&quot;', '"').replace('&apos;', "'")

            if sender:
                # eBay-Artikel-ID im Betreff markieren (aber NICHT als Bestellnummer verwenden)
                full_subject = f"[eBay] {subject}"
                if item_id:
                    full_subject += f" (Artikel: {item_id})"

                # Body kann leer sein bei manchen eBay-Nachrichten - trotzdem weiterleiten
                if not body:
                    body = f"(Kunde hat eine Nachricht zu Artikel {item_id} gesendet, aber kein Text enthalten. Betreff: {subject})"

                log.info(f"eBay Nachricht von {sender}: {subject} | Body: {body[:100]}")
                process_mail(
                    full_subject, sender, body,
                    channel="ebay", ebay_thread_id=ext_id,
                    ebay_item_id=item_id, ebay_recipient=sender
                )

                # Nachricht bei eBay als gelesen markieren (damit sie nicht nochmal kommt)
                ebay_mark_as_read(token, msg_id, headers_xml)

        log.info(f"eBay: {len(unread_ids)} ungelesene Nachrichten verarbeitet")

    except Exception as e:
        log.warning(f"eBay Messages: {e}")


def ebay_mark_as_read(token, message_id, headers_xml):
    """Markiere eine eBay-Nachricht als gelesen ueber ReviseMyMessages."""
    try:
        headers_xml_rev = dict(headers_xml)
        headers_xml_rev["X-EBAY-API-CALL-NAME"] = "ReviseMyMessages"

        xml_request = f"""<?xml version="1.0" encoding="utf-8"?>
<ReviseMyMessagesRequest xmlns="urn:ebay:apis:eBLBaseComponents">
    <RequesterCredentials>
        <eBayAuthToken>{token}</eBayAuthToken>
    </RequesterCredentials>
    <MessageIDs>
        <MessageID>{message_id}</MessageID>
    </MessageIDs>
    <Read>true</Read>
</ReviseMyMessagesRequest>"""

        r = requests.post(
            "https://api.ebay.com/ws/api.dll",
            headers=headers_xml_rev,
            data=xml_request.encode("utf-8"),
            timeout=10
        )
        if r.status_code == 200 and "<Ack>Success</Ack>" in r.text:
            log.info(f"eBay Nachricht {message_id} als gelesen markiert")
        else:
            log.warning(f"eBay Nachricht als gelesen markieren fehlgeschlagen: {r.text[:100]}")
    except Exception as e:
        log.warning(f"eBay mark as read: {e}")


def ebay_send_reply(inquiry_id, message_text, recipient=None, item_id=None):
    """Antwort ueber eBay Trading API zurueckschicken (AddMemberMessageRTQ)."""
    if not EBAY_ENABLED:
        return False
    token = ebay_get_access_token()
    if not token:
        return False
    try:
        import re
        headers_xml = {
            "X-EBAY-API-SITEID": "77",
            "X-EBAY-API-COMPATIBILITY-LEVEL": "967",
            "X-EBAY-API-CALL-NAME": "AddMemberMessageRTQ",
            "X-EBAY-API-IAF-TOKEN": token,
            "Content-Type": "text/xml"
        }

        # HTML-Sonderzeichen escapen
        safe_text = message_text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

        # Empfaenger und Artikel-ID einbauen
        recipient_xml = f"<RecipientID>{recipient}</RecipientID>" if recipient else ""
        item_xml = f"<ItemID>{item_id}</ItemID>" if item_id else ""

        xml_request = f"""<?xml version="1.0" encoding="utf-8"?>
<AddMemberMessageRTQRequest xmlns="urn:ebay:apis:eBLBaseComponents">
    <RequesterCredentials>
        <eBayAuthToken>{token}</eBayAuthToken>
    </RequesterCredentials>
    {item_xml}
    <MemberMessage>
        <Body>{safe_text}</Body>
        {recipient_xml}
        <ParentMessageID>{inquiry_id}</ParentMessageID>
    </MemberMessage>
</AddMemberMessageRTQRequest>"""

        r = requests.post(
            "https://api.ebay.com/ws/api.dll",
            headers=headers_xml,
            data=xml_request.encode("utf-8"),
            timeout=15
        )

        if r.status_code == 200 and "<Ack>Success</Ack>" in r.text:
            log.info(f"eBay Antwort gesendet an {recipient} (Message: {inquiry_id})")
            return True
        elif r.status_code == 200 and "<Ack>Warning</Ack>" in r.text:
            log.info(f"eBay Antwort gesendet mit Warnung an {recipient} (Message: {inquiry_id})")
            return True
        else:
            error_match = re.search(r'<LongMessage>(.*?)</LongMessage>', r.text)
            error_msg = error_match.group(1) if error_match else r.text[:200]
            log.error(f"eBay Antwort Fehler: {error_msg}")
    except Exception as e:
        log.error(f"eBay Antwort: {e}")
    return False


def main():
    log.info("Modellbahn-Rhein-Main Mail Assistent v5 gestartet")

    # Feedback-Status anzeigen
    fb_count = len(load_feedback())
    fb_info = f"\n📊 {fb_count} Korrekturen im Lernarchiv" if fb_count > 0 else ""
    ebay_info = "✅ eBay API aktiv" if EBAY_ENABLED else "⏳ eBay API noch nicht konfiguriert"

    send_telegram_text(
        f"🚂 <b>Modellbahn Mail Assistent v5 gestartet!</b>\n"
        f"Ich ueberwache dein Postfach und eBay.\n\n"
        f"<b>Neu in v5:</b>\n"
        f"📂 Feine Kategorien (Lieferstatus, Retoure, Beschwerde, ...)\n"
        f"🧠 Lerne aus deinen Korrekturen\n"
        f"📦 Bessere Bestelldaten aus WooCommerce\n"
        f"🏪 {ebay_info}{fb_info}"
    )
    offset     = 0
    mail_timer = 0
    ebay_timer = 0
    summary_sent_today = False
    while True:
        updates = get_telegram_updates(offset)
        for upd in updates:
            handle_telegram_update(upd)
            offset = upd["update_id"] + 1
        if time.time() - mail_timer > 120:
            check_inbox()
            mail_timer = time.time()
        # eBay alle 3 Minuten pruefen (wenn API aktiv)
        if EBAY_ENABLED and time.time() - ebay_timer > 180:
            ebay_check_messages()
            ebay_timer = time.time()
        # Tages-Zusammenfassung um 20:00 senden
        current_hour = datetime.now().hour
        if current_hour == 20 and not summary_sent_today:
            send_daily_summary()
            summary_sent_today = True
        elif current_hour != 20:
            summary_sent_today = False
        time.sleep(2)


if __name__ == "__main__":
    main()
