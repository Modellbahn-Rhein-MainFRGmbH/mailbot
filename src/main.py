"""
Modellbahn-Rhein-Main Mail Assistent v6
Fabian Rauch - Brevo API + IMAP + Telegram
v6: Ueberarbeiteter System-Prompt (Kaeufer vs. Interessent, kuerzere Antworten, Wunschliste, Ankauf-Regeln)
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

# WordPress (fuer Rechnungs-PDF Download)
WP_USER       = os.environ.get("WP_USER", "")
WP_APP_PASS   = os.environ.get("WP_APP_PASS", "")
WP_LOGIN_PASS = os.environ.get("WP_LOGIN_PASS", "")

# Telegram
TG_TOKEN   = os.environ["TG_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]

# Claude
ANTHROPIC_KEY = os.environ["ANTHROPIC_KEY"]

# Groq (Spracherkennung fuer Telegram Voice Messages)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

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
- Schreib wie ein Experte mit echter Leidenschaft fuer Modellbahn.
- Kein Corporate Talk. Nicht: Wir bedauern die Unanehmlichkeiten. Sondern: Das ist natuerlich aergerlich, wir loesen das aber sofort und unkompliziert.
- Pragmatisch: Wenn eine Teil-Erstattung schneller ist als Hin-und-Her-Versand, schlag sie direkt vor.
- Kurze Saetze, klare Aussagen, keine unnoetige Buerokratie.
- Deutsch fuer deutsche Kunden, Englisch fuer internationale Kunden.
- Du/Sie je nach Kontext: eBay-Kunden oft du (informell), Shop-Kunden Sie (formell).

WICHTIGSTE REGEL - KAEUFER VS. INTERESSENT:
- Wenn BESTELLDATEN vorhanden sind (Bestellung #..., Status, Artikel): Der Absender ist KAEUFER!
  Er hat den Artikel BEREITS GEKAUFT. Der Artikel gehoert IHM.
  NIEMALS sagen "der Artikel ist leider ausverkauft" oder "bereits verkauft" wenn der Absender der Kaeufer ist!
  Stattdessen: Auf seine konkrete Frage eingehen (Versand, Lieferzeit, Tracking, etc.)
- Wenn KEINE Bestelldaten vorhanden sind und der Kunde nach Verfuegbarkeit fragt: Dann ist es ein Interessent.
  Nur DANN pruefen ob der Artikel noch verfuegbar ist.
- Im Zweifel: Lieber davon ausgehen dass der Kunde bereits gekauft hat, als faelschlich "ausverkauft" zu sagen.

LAENGE UND STIL - WENIGER IST MEHR:
- Halte Antworten KURZ. 2-4 Saetze sind oft genug. Lieber zu kurz als zu lang.
- KEIN Schluss-Absatz mit Floskeln! Kein "Falls Sie weitere Fragen haben...", kein "Zoegern Sie nicht...", kein "Wir freuen uns auf...".
- KEINE Sendungsverfolgung-Hinweise wie "Sie erhalten automatisch eine Tracking-Nummer" - der Kunde weiss das.
- KEINE Lobeshymnen auf Modelle ("seidenweicher Lauf", "Traumstueck") AUSSER der Kontext passt wirklich.
- KEINE Google-Bewertung vorschlagen oder darum bitten.
- Wenn die Antwort in 2 Saetzen klar ist: Schreib nur 2 Saetze.
- Wenn du unsicher bist ob ein Absatz noetig ist: Lass ihn weg.

VERSAND - NIEMALS TERMINE ERFINDEN:
- Sage NIEMALS "geht heute raus", "wurde heute an DHL uebergeben" oder "wird heute versendet".
- Du weisst NICHT wann ein Paket versendet wird. Das entscheidet Fabian.
- Wenn ein Kunde nach Versand fragt, schreib NUR:
  "Ihr Paket wird schnellstmoeglich versendet." oder "Das Paket geht in Kuerze raus."
- Erst wenn im Mail-Verlauf EIN KONKRETES VERSANDDATUM VON FABIAN steht, darfst du dieses wiederholen.
- Bei Feiertagen/Wochenenden: Nicht spekulieren wann versendet wird. Einfach sagen es wird bearbeitet.

GESCHAEFTSREGELN:
- Artikel unter 15 EUR: Nicht zurueckfordern. Ersatz oder Geld zurueck. Kunde behaelt Teil als Ersatzteilspender.
- Fehlende/falsche Teile ohne Ersatz: Wahl zwischen Teilrueckzahlung oder Rueckgabe.
- Falscher Artikel geliefert: IMMER anbieten den richtigen Artikel nachzusenden + Retourschein fuer den falschen beilegen. Das ist die einfachste Loesung.
- Ruecksendelabels: NUR bei berechtigten Beschwerden. NIEMALS bei einfacher Stornierung!
- eBay-Retouren: https://modellbahnrheinmain.shipping-portal.com/rp
- Shop-Retouren: https://modellbahnrheinmainshop.shipping-portal.com/rp/
- Tax-Free: Wir verkaufen nach Paragraph 25a UStG. Keine MwSt. ausgewiesen, kein Export-Refund.
- Kombiversand: Kunden duerfen 14 Tage Auktionen sammeln bevor Zahlung faellig wird.
- Rabatte: Kaum Rabatte, hoechstens bei schon laenger eingestellten Artikeln.
- Widerruf/einfache Rueckgabe: Kein Ruecksendelabel, Kunde schickt auf eigene Kosten an Max-Planck-Str. 18, 63322 Roedermark. Kurz und sachlich antworten, NICHT entschuldigen.

WICHTIG - LAGER UND SORTIMENT:
- Wir verkaufen ausschliesslich gebrauchte Modellbahn-Ware (Sammlerstuecke, Gebrauchtware).
- Jeden Artikel haben wir nur einmal. Wenn ausverkauft, dann wirklich weg.
- Nachbestellen ist NICHT moeglich. Wir kaufen Sammlungen auf - ob ein Artikel wiederkommt, ist Zufall.
- Wenn ein Artikel als "Ausverkauft" angezeigt wird UND der Absender KEIN Kaeufer ist:
  Ehrlich sagen, dass der Artikel leider schon verkauft wurde und auf die WUNSCHLISTE verweisen.
- NIEMALS sagen "Wir bestellen nach" oder "Wir koennen den Artikel fuer Sie reservieren/bestellen".

WUNSCHLISTE / SUCHLISTE (NEU):
- Wir haben eine Wunschliste auf der Website. Kunden koennen Hersteller + Herstellernummer eintragen.
- Sobald der Artikel bei uns vorraetig ist, bekommt der Kunde automatisch eine E-Mail.
- Wo zu finden: Orangener Button auf der Startseite ODER ganz unten auf jeder Seite "Wunschliste/Suchauftrag".
- Funktioniert wie ein Newsletter: Kommt nur eine Mail wenn genau dieser Artikel auftaucht.
- Bei JEDER Anfrage nach nicht-verfuegbaren Artikeln auf die Wunschliste hinweisen!
- Bei eBay-Anfragen: "In unserem gleichnamigen Online-Shop findest du uebrigens auch eine Wunschliste. Dort kannst du dich eintragen und bekommst automatisch eine Benachrichtigung wenn der Artikel verfuegbar wird."

EBAY vs. SHOP - PREISSTRUKTUR:
- Unsere Artikel sind im eigenen Online-Shop GUENSTIGER als auf eBay.
- Grund: eBay erhebt ca. 13% Gebuehren. Wir schlagen daher 8% auf den Shop-Preis drauf fuer eBay.
- Bei eBay-Preisanfragen die EXAKTE Formulierung verwenden:
  "Hier auf eBay kann ich leider nichts am Preis machen, aber wenn du mal in unseren gleichnamigen Online-Shop schaust, findest du das Modell guenstiger."
- NIEMALS einen direkten Link zur Website in eBay-Nachrichten schreiben!
- NIEMALS die URL www.modellbahn-rhein-main.de in eBay-Antworten nennen!
- Nur den Hinweis auf den "gleichnamigen Online-Shop" geben.
- Bei eBay-Nachrichten: Der Preis in den ARTIKEL-INFORMATIONEN ist der SHOP-Preis. Den eBay-Preis NICHT nennen.

LADEN UND ABHOLUNG:
- Wir haben KEINE festen Oeffnungszeiten. Besuch nur nach Terminvereinbarung.
- Samstags haben wir NICHT geoeffnet.
- Abholung ist kein Problem: Beim Bestellvorgang kann man "Abholung" als Versandart waehlen.
- Bezahlung vor Ort mit Karte ist moeglich.
- Adresse: Max-Planck-Str. 18, 63322 Roedermark.

ZAHLUNGSARTEN:
- PayPal, Visa, Mastercard, American Express, Kauf auf Rechnung, SEPA Lastschrift, PayPal Ratenzahlung, Vorkasse/Bankueberweisung.
- OHNE Kundenkonto geht nur Bankueberweisung.
- MIT Kundenkonto hat man freie Wahl aller Zahlungsarten.

FUNKTIONSPRUEFUNG UND ZUSTAND:
- Alle Modelle werden auf einer Teststrecke geprueft (ebenes Oval).
- Getestet wird: Fahrbetrieb (vorwaerts/rueckwaerts), Licht, Digital- und Soundfunktionen.
- NICHT getestet: Steigungen, verschiedene Radien, Weichen.
- Detaillierte Produktfotos - man erhaelt exakt das abgebildete Modell.

LIEFERUMFANG:
- Alles was im Lieferumfang enthalten ist, ist auf den Bildern zu sehen.
- Ist etwas NICHT abgebildet: Davon ausgehen dass es fehlt.

SAMMLUNG VERKAUFEN / ANKAUF:
- Wir kaufen Sammlungen, egal welche Spurgroesse, Hersteller oder Epoche.
- Mindestgroesse: Ab 50 Lokomotiven in Originalverpackung (Spur H0 Richtwert).
- Unter 50 Loks: eBay Privatverkauf empfehlen. "Dort zahlen Sie keine Gebuehren und bekommen das meiste Geld." Fuer Haendler lohnt es sich unter 50 Loks nicht.
- Anlagen, Gebaeude, Baeume, Gleise, Streumaterial = KEIN INTERESSE. Nicht erwaehnen, nicht nachfragen. Null Wert fuer uns.
- Einzelne Modelle werden NICHT angekauft und NICHT in Zahlung genommen.
- Einzelne Verpackungen (OVPs) werden NICHT angekauft.
- NIEMALS selbst Ankaufspreise nennen, schaetzen oder kalkulieren!
- NIEMALS auf das Ankaufformular verweisen, ausser Fabian tut es im Mail-Verlauf.
- Stattdessen: "Ich schaue mir die Modelle an und nenne Ihnen dann den Ankaufspreis."

BESTELLNUMMERN:
- Bei Bestellnummern im Text: OHNE Hashtag (#) schreiben. Einfach die Nummer: "Bestellnummer 1519080".

KATEGORIE-SPEZIFISCHE ANWEISUNGEN:

Bei LIEFERSTATUS:
- Nutze die Sendcloud-Tracking-Daten falls vorhanden.
- Gib dem Kunden die Trackingnummer und den aktuellen Status.
- Wenn kein Tracking vorhanden: "Das Paket wird schnellstmoeglich versendet." NICHT "geht heute raus".

Bei RETOURE:
- Unterscheide: eBay oder Shop? Jeweils anderen Retourenlink senden.
- Widerruf (Kunde will einfach zurueckgeben): Kurz und sachlich. Kein Ruecksendelabel. Adresse nennen.
- Berechtigte Beschwerde (Defekt, falsch geliefert): Ruecksendelabel anbieten.

Bei BESCHWERDE:
- Erst Verstaendnis zeigen, dann Loesung anbieten.
- Unter 15 EUR: Sofort Geld zurueck oder Ersatz, Artikel behalten.
- Ueber 15 EUR: Optionen anbieten (Teilerstattung oder Rueckgabe).
- Falscher Artikel geliefert: Nachsendung des richtigen Artikels + Retourschein fuer falschen Artikel beilegen.

Bei PRODUKTFRAGE:
- Fachkundig antworten.
- Wenn Artikel "Auf Lager" und Absender KEIN Kaeufer: Verfuegbarkeit bestaetigen.
- Wenn Artikel "Ausverkauft" und Absender KEIN Kaeufer: Auf Wunschliste verweisen.
- Wenn Absender KAEUFER ist (Bestelldaten vorhanden): Auf seine Frage eingehen, NICHT Verfuegbarkeit diskutieren!

Bei STORNIERUNG:
- Wenn schon versendet: Kunde informieren, Retoure anbieten.
- Wenn noch nicht versendet: Stornierung bestaetigen.

Bei RECHNUNG_STEUER:
- Paragraph 25a UStG Differenzbesteuerung.
- Keine MwSt. ausweisbar, kein Export-Refund.
- Rechnung angefordert: "Die Rechnung zu Ihrer Bestellung finden Sie im Anhang dieser E-Mail."
- NIEMALS "[Rechnung als PDF-Anhang beifuegen]" oder aehnliche Platzhalter schreiben!

Bei KOMBIVERSAND:
- 14 Tage Sammelzeit bestaetigen.

Bei RABATTANFRAGE:
- Hoeflich aber bestimmt: Kaum Rabatte moeglich.
- Bei eBay: Auf guenstigeren Shop verweisen (exakte Formulierung oben benutzen).

Bei KONTAKTFORMULAR:
- Anrede: Sie (formell).
- Bei nicht-verfuegbaren Artikeln: Auf Wunschliste verweisen.

Bei TERMIN:
- Adresse: Max-Planck-Str. 18, 63322 Roedermark.
- Keine festen Oeffnungszeiten, nur nach Terminvereinbarung. Samstags NICHT moeglich.
- WICHTIG: Wenn ein konkretes Datum und Uhrzeit vereinbart wird, schreibe in der LETZTEN Zeile vor der Signatur:
  TERMIN: YYYY-MM-DD HH:MM | Typ | Kundenname
  Beispiel: TERMIN: 2026-03-20 14:00 | Abholung | Herr Mueller
  Wenn der Kunde nur allgemein nach einem Termin fragt: Termine vorschlagen, KEINE TERMIN-Zeile.

ECHTE BEISPIELE VON FABIAN:

BEISPIEL 1 - Falsche Achsen (eBay, informell):
Antwort: Hallo Karl, es tut mir sehr leid, dass die Achsen des Roco-Wagens faelschlicherweise als DC beschrieben wurden. Ich habe im Lager nachgesehen, aber leider haben wir keine passenden Austauschachsen vorraetig. Ich kann dir eine Teilrueckerstattung anbieten, wenn du den Wagen behalten moechtest, oder die komplette Rueckgabe.

BEISPIEL 2 - Transportschaden (Shop, formell):
Antwort: Hallo Herr Baierl, auf unseren Artikelfotos war die Haltestange noch vorhanden, sie muss also beim Transport abgefallen sein. Da ich das Teil nicht vorraetig habe, biete ich Ihnen einen 10,00 EUR Gutschein fuer den naechsten Einkauf oder die komplette Rueckgabe an.

BEISPIEL 3 - Kaufabbruch ablehnen (eBay, kurz):
Antwort: Hallo Klaus, alles klar, ich habe deinen Wunsch beruecksichtigt und die Anfrage zum Kaufabbruch soeben abgelehnt. Der Kauf bleibt bestehen.

BEISPIEL 4 - Stornierung (eBay, kurz):
Antwort: Kein Problem, das kann mal passieren. Ich habe den Kaufabbruch soeben fuer dich im System durchgefuehrt.

BEISPIEL 5 - Kombiversand (eBay, kurz):
Antwort: Das ist bereits erledigt. Ich habe die Kaeufe zusammengefasst, sodass du die Zahlung nun gesammelt vornehmen kannst.

BEISPIEL 6 - Verschmutzung (Shop, unter 15 EUR):
Antwort: Sehr geehrter Herr Schminke, die Verschmutzung haette uns auffallen muessen, das tut mir leid. 1. Sie behalten den Wagen, ich erstatte 5 EUR. 2. Ruecksendung gegen vollen Kaufpreis.

BEISPIEL 7 - eBay Preisanfrage:
Antwort: Hallo Kalle, hier auf eBay kann ich leider nichts am Preis machen, aber wenn du mal in unseren gleichnamigen Online-Shop schaust, findest du das Modell guenstiger.

BEISPIEL 8 - Falscher Artikel geliefert (Shop):
Antwort: Sehr geehrter Herr Beeckmann, das tut mir sehr leid, dass es zu einer Verwechslung gekommen ist! Die einfachste Loesung waere: Wir senden Ihnen den korrekten Wagen nach und legen einen Retourschein bei, so dass Sie den falschen Wagen einfach zurueckschicken koennen. Ich schaue morgen im Lager nach ob der Wagen noch da ist und melde mich dann bei Ihnen.

BEISPIEL 9 - Widerruf (Shop, kurz und sachlich):
Antwort: Sehr geehrter Herr Proels, das ist kein Problem. Sie koennen den Artikel gerne per Widerruf zurueckschicken an unsere Adresse: Max-Planck-Str. 18, 63322 Roedermark.

BEISPIEL 10 - Nicht verfuegbarer Artikel mit Wunschliste:
Antwort: Hallo Ralf, die Fleischmann ICE-2-Mittelwagen 7494 und 7496 habe ich aktuell nicht am Lager. Wir haben aber eine Wunschliste auf unserer Website - dort kannst du Hersteller und Nummer eintragen und bekommst automatisch eine Mail wenn der Artikel bei uns verfuegbar wird. Die Wunschliste findest du auf der Startseite (orangener Button) oder ganz unten auf jeder Seite.

BEISPIEL 11 - Fehler zugeben (ehrlich):
Antwort: Sehr geehrter Herr Borgert, ich habe auf dem Konto nachgeschaut, das war tatsaechlich mein Fehler. Ich habe wirklich die Ueberweisung uebersehen. Das Paket wird schnellstmoeglich fertig gemacht.

BEISPIEL 12 - eBay Bewertungsaufforderung (kurz):
Antwort: Hallo, das ist vollkommen normal - eBay macht das automatisch und fragt immer mal wieder nach Bewertungen. Die Pakete sind aber unterwegs. Warte einfach bis die Pakete da sind und gib dann die Bewertungen ab.

STIL-ZUSAMMENFASSUNG:
- DIREKT und LOESUNGSORIENTIERT - keine langen Einleitungen.
- Bei eBay: Du, locker, kurze Saetze. Bei Shop: Sie, formell aber trotzdem persoenlich.
- Konkrete Optionen nennen (1. ... oder 2. ...), nicht vage bleiben.
- KURZE Antworten. Lieber 3 praezise Saetze als 10 Fuellsaetze.
- Keinen unnuetzen Schluss-Absatz anhaengen. Wenn die Antwort fertig ist, hoer auf.
- NIEMALS: "Wir entschuldigen uns fuer die Unannehmlichkeiten" / "Zoegern Sie nicht" / "Bei weiteren Fragen stehe ich gerne zur Verfuegung".
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


def decode_payload(part):
    """Dekodiere Mail-Payload mit korrektem Charset (UTF-8, ISO-8859-1, Windows-1252 etc.)."""
    raw = part.get_payload(decode=True)
    if not raw:
        return ""
    # Charset aus dem Mail-Header lesen (z.B. Content-Type: text/plain; charset="iso-8859-1")
    charset = part.get_content_charset()
    # Versuch 1: Charset aus dem Header verwenden
    if charset:
        try:
            return raw.decode(charset)
        except (UnicodeDecodeError, LookupError):
            pass
    # Versuch 2: UTF-8
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    # Versuch 3: Windows-1252 (gaengigstes Nicht-UTF-8 Encoding in DE-Mails)
    try:
        return raw.decode("windows-1252")
    except UnicodeDecodeError:
        pass
    # Versuch 4: Latin-1 (kann alles dekodieren, verliert aber ggf. Info)
    return raw.decode("latin-1", errors="replace")


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
                    body = decode_payload(part)
                except:
                    pass
            elif ct == "text/html" and "attachment" not in cd and not html_body:
                try:
                    html_body = decode_payload(part)
                except:
                    pass
            elif ct.startswith("image/") and len(images) < 3:
                try:
                    img_data = part.get_payload(decode=True)
                    if img_data:
                        # Logo-Filter: Kleine Bilder (<15KB) und Inline-Bilder (Signatur-Logos) ignorieren
                        content_id = part.get("Content-ID", "")
                        filename = part.get_filename() or ""
                        is_inline = bool(content_id) or "inline" in cd.lower()
                        is_small = len(img_data) < 15000  # < 15KB = wahrscheinlich Logo
                        # Bekannte Logo-Dateinamen filtern
                        logo_names = ["logo", "banner", "signature", "icon", "footer", "header", "brand"]
                        is_logo_name = any(n in filename.lower() for n in logo_names)
                        if is_small or is_inline or is_logo_name:
                            log.info(f"Bild gefiltert (Logo/Inline): {filename or 'unbenannt'} ({len(img_data)} Bytes, inline={is_inline})")
                            continue
                        images.append(img_data)
                except:
                    pass
    else:
        ct = msg.get_content_type()
        try:
            raw = decode_payload(msg)
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


def ebay_html_to_text(html):
    """Aggressive HTML-zu-Text Konvertierung speziell fuer eBay-Nachrichten.
    eBay packt die Kaeufer-Nachricht in ein riesiges HTML-E-Mail-Template
    mit CSS, Tables, Bildern etc. Diese Funktion extrahiert NUR den Text."""
    import re

    text = html

    # Schritt 1: Versuche den eigentlichen Nachrichtentext zu finden
    # eBay hat den User-Text oft in einem bestimmten Container
    # Typische Muster: "Nachricht von <user>" gefolgt vom eigentlichen Text
    user_msg_patterns = [
        # eBay DE: Nachricht ist oft nach "Nachricht von" oder in einem spezifischen div
        r'(?:Nachricht\s+von\s+\w+[^<]*?:?\s*</[^>]+>\s*(?:<[^>]+>\s*)*)(.*?)(?:<[^>]*(?:Antworten|Respond|Diese Nachricht|This message|Marketplace|eBay International))',
        # eBay: Content nach dem letzten Header, vor dem Footer
        r'<!-- BUYER.?MESSAGE -->(.+?)<!-- END.?BUYER',
        r'class="[^"]*message[^"]*"[^>]*>(.*?)</(?:div|td)',
        # Fallback: Text zwischen bekannten eBay-Wrappern
        r'<td[^>]*class="[^"]*(?:message|content|body)[^"]*"[^>]*>(.*?)</td>',
    ]

    extracted = None
    for pattern in user_msg_patterns:
        match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if match:
            candidate = match.group(1).strip()
            # Nur verwenden wenn genuegend Text drin ist
            clean_candidate = re.sub(r'<[^>]+>', '', candidate).strip()
            if len(clean_candidate) > 10:
                extracted = candidate
                log.info(f"eBay Nachrichtentext per Pattern extrahiert ({len(clean_candidate)} Zeichen)")
                break

    # Schritt 2: Falls Pattern-Extraktion geklappt hat, nur diesen Teil nehmen
    if extracted:
        text = extracted

    # Schritt 3: Aggressives Cleaning
    # CDATA Bloecke entfernen
    text = re.sub(r'<!\[CDATA\[.*?\]\]>', '', text, flags=re.DOTALL)
    # Kommentare entfernen
    text = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)
    # Style-Bloecke entfernen (auch mit CDATA drin)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    # Script-Bloecke entfernen
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    # Head-Bereich komplett entfernen
    text = re.sub(r'<head[^>]*>.*?</head>', '', text, flags=re.DOTALL | re.IGNORECASE)

    # Zeilenumbrueche vor Tag-Entfernung
    text = re.sub(r'<br\s*/?\s*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</p>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</div>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</tr>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</li>', '\n', text, flags=re.IGNORECASE)

    # Alle HTML-Tags entfernen (auch mehrzeilige)
    text = re.sub(r'<[^>]+>', ' ', text, flags=re.DOTALL)

    # HTML-Entities
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&nbsp;', ' ').replace('&quot;', '"').replace('&#39;', "'")
    text = re.sub(r'&#\d+;', ' ', text)  # Numerische Entities
    text = re.sub(r'&\w+;', ' ', text)   # Restliche benannte Entities

    # CSS-Reste entfernen die durchgerutscht sind
    # Inline-Styles: alles was wie CSS aussieht
    text = re.sub(r'\{[^}]*\}', ' ', text)  # CSS-Bloecke { ... }
    text = re.sub(r'[\w-]+\s*:\s*[^;{}\n]{0,100}\s*(?:!important)?;', ' ', text)  # property: value;
    text = re.sub(r'@media[^{]*\{[^}]*\}', ' ', text, flags=re.DOTALL)  # @media queries
    text = re.sub(r'\.[\w-]+\s*\{', ' ', text)  # .class {
    text = re.sub(r'#[\w-]+\s*\{', ' ', text)  # #id {

    # HTML-Attribut-Reste entfernen
    text = re.sub(r'\b(?:style|class|width|height|border|cellpadding|cellspacing|align|valign|bgcolor|colspan|rowspan)\s*=\s*"[^"]*"', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'\b(?:style|class|width|height|border|cellpadding|cellspacing|align|valign|bgcolor|colspan|rowspan)\s*=\s*\'[^\']*\'', ' ', text, flags=re.IGNORECASE)

    # URLs die kein Tracking/Shop sind entfernen (eBay-Template-Bilder etc.)
    text = re.sub(r'https?://(?:ir\.ebaystatic|pics\.ebaystatic|(?:www\.)?ebaystatic)[^\s]*', ' ', text)

    # eBay-Boilerplate Texte entfernen
    boilerplate = [
        r'Diese Nachricht wurde.*?(?:gesendet|geschickt).*',
        r'This message was sent.*',
        r'Antworten Sie nicht auf diese E-Mail.*',
        r'Do not reply to this email.*',
        r'Marketplace-Nachrichten.*',
        r'Copyright.*?eBay.*',
        r'eBay International AG.*',
        r'Datenschutzrichtlinie.*',
        r'Privacy Policy.*',
        r'Klicken Sie hier.*?(?:antworten|respond).*',
        r'Click here to respond.*',
        r'Weitere Informationen finden Sie.*',
        r'eBay-Kaufabwicklung.*',
        r'Um mehr zu erfahren.*',
        r'Learn more about.*',
        r'eBay hat diese Nachricht.*',
        r'eBay sent this message.*',
        r'Alle Rechte vorbehalten.*',
        r'All rights reserved.*',
        r'Hilfe.*?Kontakt.*?Sicherheitsportal.*',
        r'Help.*?Contact.*?Security.*',
        # eBay Kauf-Details Boilerplate
        r'Einzelheiten zum Kauf ansehen.*',
        r'Nur K.ufe bei eBay sind.*?(?:abgesichert|erlaubt)\.?.*',
        r'Beim Handelspartner nachzufragen.*?(?:erlaubt|allowed)\.?.*',
        r'E-Mail-Referenznummer:?\s*\[?#?[a-z0-9\-_#\[\]]+\]?.*',
        r'Nachrichten an dieses Postfach werden nicht gelesen.*',
        r'Bitte antworten Sie nicht auf diese Nachricht.*',
        r'Bei Fragen gehen Sie bitte zu Hilfe.*',
        # eBay Bestellinfo-Zeilen
        r'Bestellstatus:\s*\w+',
        # eBay Button-Texte und UI-Elemente
        r'^Antworten$',
        r'^Mit Preisvorschlag antworten$',
        r'^Neue Nachricht von:?\s*$',
        r'^Details zum Kauf$',
        r'^View purchase details$',
    ]
    for pattern in boilerplate:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE | re.DOTALL | re.MULTILINE)

    # Whitespace aufraeumen
    text = re.sub(r'[ \t]+', ' ', text)          # Mehrfache Leerzeichen
    text = re.sub(r' *\n *', '\n', text)          # Leerzeichen um Newlines
    text = re.sub(r'\n{3,}', '\n\n', text)        # Max 2 Leerzeilen
    text = re.sub(r'(\| *)+', ' ', text)          # Pipe-Reste von Tabellen
    text = re.sub(r'^\s*\|?\s*$', '', text, flags=re.MULTILINE)  # Leere Zeilen mit Pipes

    text = text.strip()

    # Schritt 4: Qualitaetspruefung - wenn Text zu kurz oder immer noch zu viel Muell
    if text:
        # Zeilen filtern die wie Code/CSS aussehen
        clean_lines = []
        for line in text.split('\n'):
            line = line.strip()
            if not line:
                clean_lines.append('')
                continue
            # Zeile ueberspringen wenn sie hauptsaechlich aus CSS/HTML besteht
            css_chars = sum(1 for c in line if c in '{}:;=<>"\'')
            if len(line) > 10 and css_chars / len(line) > 0.3:
                continue  # Mehr als 30% Sonderzeichen = wahrscheinlich Muell
            # Zeile ueberspringen wenn sie bekannte HTML/CSS Fragmente enthaelt
            if re.search(r'(?:font-size|font-family|text-decoration|border-collapse|padding|margin|!important|cellpadding|cellspacing|text-align|line-height|vertical-align|background-color)', line, re.IGNORECASE):
                continue
            clean_lines.append(line)
        text = '\n'.join(clean_lines)
        text = re.sub(r'\n{3,}', '\n\n', text).strip()

    if not text or len(text) < 5:
        text = "(eBay-Nachricht konnte nicht gelesen werden - bitte im eBay-Portal pruefen)"

    # Schritt 5: Deduplizierung - eBay schickt den Nachrichtentext oft doppelt
    # (einmal als Vorschau/Header, einmal als eigentliche Nachricht)
    if text and len(text) > 50:
        paragraphs = re.split(r'\n\s*\n', text)
        if len(paragraphs) >= 2:
            seen = []
            unique_paragraphs = []
            for para in paragraphs:
                para_clean = para.strip()
                if not para_clean:
                    continue
                # Pruefen ob dieser Absatz schon vorkam (fuzzy: erste 60 Zeichen vergleichen)
                para_key = re.sub(r'\s+', ' ', para_clean)[:60].lower()
                is_dupe = False
                for seen_key in seen:
                    if para_key == seen_key:
                        is_dupe = True
                        break
                if not is_dupe:
                    seen.append(para_key)
                    unique_paragraphs.append(para_clean)
                else:
                    log.info(f"eBay Duplikat entfernt: {para_clean[:60]}...")
            text = '\n\n'.join(unique_paragraphs)

    log.info(f"eBay HTML bereinigt: {len(html)} -> {len(text)} Zeichen")
    return text


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
    "termin",            # Abholung, Besichtigung, Terminvereinbarung
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
            "termin = Abholung, Besichtigung, Terminvereinbarung, Terminbestaetigung, Terminwunsch\n"
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
    """Sendcloud Tracking abrufen. Verifiziert dass das Paket zur Bestellung gehoert."""
    if not SC_KEY:
        return None

    search_terms = []
    order_id_str = ""
    if order_data and order_data.get("order_id"):
        order_id_str = str(order_data["order_id"])
        search_terms.append(order_id_str)
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
            if not parcels:
                continue

            # WICHTIG: Verifiziere dass das Paket wirklich zur Bestellung gehoert
            # Sendcloud search ist fuzzy und kann falsche Ergebnisse liefern
            matched_parcel = None
            for p in parcels:
                parcel_order_nr = str(p.get("order_number", ""))
                parcel_ext_order = str(p.get("external_order_id", ""))
                parcel_ext_ref = str(p.get("external_reference", ""))

                # Exakter Match auf Bestellnummer pruefen
                if order_id_str and (
                    parcel_order_nr == order_id_str or
                    parcel_ext_order == order_id_str or
                    parcel_ext_ref == order_id_str
                ):
                    matched_parcel = p
                    log.info(f"Sendcloud: Exakter Match fuer Bestellung #{order_id_str}")
                    break
                # Auch gegen extra_search pruefen (z.B. Rechnungsnummer)
                if extra_search and (
                    parcel_order_nr == str(extra_search) or
                    parcel_ext_order == str(extra_search) or
                    parcel_ext_ref == str(extra_search)
                ):
                    matched_parcel = p
                    log.info(f"Sendcloud: Match ueber Suchbegriff {extra_search}")
                    break

            if not matched_parcel:
                log.warning(f"Sendcloud: {len(parcels)} Paket(e) gefunden fuer '{search}', aber keins gehoert zu Bestellung #{order_id_str}. Verworfen.")
                continue

            result = {
                "tracking_number": matched_parcel.get("tracking_number"),
                "status": matched_parcel.get("status", {}).get("message", ""),
                "carrier": matched_parcel.get("carrier", {}).get("code", ""),
                "tracking_url": matched_parcel.get("tracking_url", "")
            }
            log.info(f"Sendcloud Tracking verifiziert (Bestellung #{order_id_str}): {result['carrier']} {result['tracking_number']} - {result['status']}")
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
        # Klare Zahlungsstatus-Interpretation fuer Claude
        status = order_data.get("status", "")
        payment = order_data.get("payment_method", "")
        if status == "Wartend" and "Vorkasse" in payment:
            lines.append("ACHTUNG: Zahlung noch NICHT eingegangen! Bestellung wartet auf Bankueberweisung.")
        elif status == "Wartend":
            lines.append("ACHTUNG: Bestellung wartet noch auf Zahlungseingang.")
        elif status == "In Bearbeitung":
            lines.append("Zahlung eingegangen, Bestellung wird bearbeitet/versendet.")
        elif status == "Abgeschlossen":
            lines.append("Bestellung abgeschlossen und versendet.")
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
    elif order_data:
        lines.append("Sendungsverfolgung: Kein Tracking vorhanden (Bestellung wurde noch nicht versendet)")
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
            f"AKTUELLES DATUM: {datetime.now().strftime('%A, %d. %B %Y')} (Wochentag auf Deutsch: {['Montag','Dienstag','Mittwoch','Donnerstag','Freitag','Samstag','Sonntag'][datetime.now().weekday()]})\n"
            f"AKTUELLE UHRZEIT: {datetime.now().strftime('%H:%M')} Uhr\n\n"
            f"Kanal: {channel_hint}\n"
            f"Kategorie: {category}\n"
            f"Absender: {sender}\n"
            f"Betreff: {subject}\n\n"
            f"BESTELLDATEN:\n{context}\n\n"
            f"KUNDEN-NACHRICHT (inkl. evtl. vorheriger Mail-Verlauf):\n{body}\n\n"
            f"WICHTIG: Falls die Nachricht einen Mail-Verlauf enthaelt (zitierte fruehere Nachrichten), "
            f"beruecksichtige den GESAMTEN Kontext der bisherigen Konversation fuer deine Antwort. "
            f"Antworte nur auf die NEUESTE Nachricht des Kunden, aber mit Wissen ueber den gesamten Verlauf.\n\n"
            f"WICHTIG: Wenn der Kunde 'morgen', 'uebermorgen', 'Freitag' etc. schreibt, berechne das korrekte Datum basierend auf dem AKTUELLEN DATUM oben.\n\n"
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
        else:
            log.error(f"Telegram Fehler {r.status_code}: {r.text[:200]}")
            # Fallback: Ohne HTML-Parsing nochmal versuchen
            payload["parse_mode"] = ""
            r2 = requests.post(url, json=payload, timeout=10)
            if r2.status_code == 200:
                log.info("Telegram: Fallback ohne HTML erfolgreich")
                return r2.json().get("result", {}).get("message_id")
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
        "kombiversand": "📮", "rabattanfrage": "💰", "kontaktformular": "📋",
        "termin": "📅"
    }
    cat_icon = cat_emoji.get(category, "📧")

    # HTML-Sonderzeichen escapen damit Telegram nicht abbricht
    def tg_escape(text):
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # === SICHERHEITSNETZ: Body vor Telegram-Anzeige bereinigen ===
    import re as _re
    display_body = body.strip()

    # Pruefen ob der Body noch HTML/CSS-Muell enthaelt
    html_indicators = ['<div', '<table', '<td', '<tr', '<style', '<html', '<body',
                       'cellpadding', 'cellspacing', 'border-collapse', 'font-family',
                       'text-decoration', '!important', 'font-size:', 'line-height:',
                       'background-color:', 'padding:', 'margin:', 'width:', 'border:']
    indicator_count = sum(1 for ind in html_indicators if ind.lower() in display_body.lower())

    if indicator_count >= 3:
        # Body ist noch voller HTML/CSS - nochmal aggressiv bereinigen
        log.warning(f"Body enthaelt noch HTML/CSS ({indicator_count} Indikatoren) - bereinige nochmal")
        display_body = ebay_html_to_text(display_body)

    # Zweite Pruefung: Wenn immer noch Muell drin ist, Zeilen einzeln filtern
    if display_body:
        clean_lines = []
        for line in display_body.split('\n'):
            line_stripped = line.strip()
            if not line_stripped:
                clean_lines.append('')
                continue
            # Zeilen mit CSS/HTML-Mustern rauswerfen
            if _re.search(r'(?:font-size|font-family|text-decoration|border-collapse|cellpadding|cellspacing|text-align|line-height|vertical-align|background-color|!important|padding:\s*\d|margin:\s*\d|\.[\w-]+\s*\{|border:\s*\d|width:\s*\d+px)', line_stripped, _re.IGNORECASE):
                continue
            # Zeilen die hauptsaechlich aus HTML-Attributen bestehen
            if _re.search(r'(?:style=|class=|align=|valign=|bgcolor=|colspan=|rowspan=)', line_stripped, _re.IGNORECASE):
                continue
            # eBay-Boilerplate Zeilen rauswerfen
            if _re.search(r'(?:Einzelheiten zum Kauf|Nur K.ufe bei eBay sind|K.uferschutzprogramme|Handelspartner nachzufragen|Transaktion au.erhalb|E-Mail-Referenznummer|Nachrichten an dieses Postfach|antworten Sie nicht auf diese|Hilfe \& Kontakt|^Antworten$|^Mit Preisvorschlag antworten$|^Neue Nachricht von|^Details zum Kauf$|Bestellstatus:\s*\w)', line_stripped, _re.IGNORECASE):
                continue
            # Referenznummer-Hashes (z.B. [#a01-qjk8af1n67#]_[#2446d5a5bde...])
            if _re.search(r'\[#[a-z0-9\-]+#\]', line_stripped, _re.IGNORECASE):
                continue
            # HTML-Tag-Fragmente (z.B. '50" >', '" > <td', 'border="0"')
            if _re.search(r'["\']?\s*/?>', line_stripped) and len(line_stripped) < 50:
                # Kurze Zeile mit > drin - pruefen ob es echter Text ist
                text_chars = sum(1 for c in line_stripped if c.isalpha())
                if text_chars < 5:
                    continue  # Fast keine Buchstaben = HTML-Fragment
            # Zeilen die fast nur Sonderzeichen sind (auch kurze Zeilen!)
            if len(line_stripped) > 3:
                alnum = sum(1 for c in line_stripped if c.isalnum() or c == ' ')
                if alnum / len(line_stripped) < 0.5:
                    continue  # Weniger als 50% Buchstaben/Zahlen/Leerzeichen = Muell
            clean_lines.append(line_stripped)
        display_body = '\n'.join(clean_lines)
        display_body = _re.sub(r'\n{3,}', '\n\n', display_body).strip()

    # Wenn nach Bereinigung nichts uebrig: Fallback-Text
    if not display_body or len(display_body) < 5:
        display_body = "(Nachrichtentext konnte nicht extrahiert werden - bitte im eBay-Portal pruefen)"

    # Max 2000 Zeichen fuer Kunden-Nachricht in Telegram (verhindert Multi-Message-Spam)
    if len(display_body) > 2000:
        display_body = display_body[:1950] + "\n\n[... gekuerzt ...]"

    full_body = tg_escape(display_body)
    safe_subject = tg_escape(subject)
    safe_mail_body = tg_escape(mail_body)

    # Nachricht zusammenbauen
    msg = (
        f"{kanal} {cat_icon} <b>{category.upper()}</b>\n"
        f"Von: <code>{tg_escape(sender)}</code>\n"
        f"Betreff: {safe_subject}\n"
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
            f"{tg_escape(translation_customer)}\n"
            f"--------------------"
        )

    # URLs aus Bestelldaten extrahieren und klickbar machen
    import re as _re
    clickable_links = []
    ctx_for_code = order_context

    # Shop-Links extrahieren
    for match in _re.finditer(r'Shop-Link:\s*(https?://\S+)', order_context):
        url = match.group(1)
        clickable_links.append(f"🛒 <a href=\"{tg_escape(url)}\">Shop-Link</a>")
        ctx_for_code = ctx_for_code.replace(url, "(siehe Link unten)")

    # Tracking-Links extrahieren
    for match in _re.finditer(r'Tracking:\s*(https?://\S+)', order_context):
        url = match.group(1)
        clickable_links.append(f"📦 <a href=\"{tg_escape(url)}\">Tracking-Link</a>")
        ctx_for_code = ctx_for_code.replace(url, "(siehe Link unten)")

    ctx_display_clean = tg_escape(ctx_for_code)
    links_section = ""
    if clickable_links:
        links_section = "\n" + "\n".join(clickable_links) + "\n"

    msg += (
        f"\n\n<b>Bestelldaten:</b>\n<code>{ctx_display_clean}</code>"
        f"{links_section}\n"
        f"<b>Mein Vorschlag:</b>\n"
        f"--------------------\n"
        f"{safe_mail_body}\n"
        f"--------------------"
    )

    # Bei fremdsprachigen Antworten: Deutsche Uebersetzung anhaengen
    if translation_draft:
        msg += (
            f"\n\n🌐 <b>Deutsche Übersetzung (Antwort):</b>\n"
            f"--------------------\n"
            f"{tg_escape(translation_draft)}\n"
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
    """Rechnungs-PDF von WordPress/German Market herunterladen.
    Methode: WordPress-Login per Cookie, Nonce holen, dann PDF downloaden."""
    if not WP_USER or not order_id:
        return None
    if not WP_LOGIN_PASS and not WP_APP_PASS:
        return None
    try:
        import re as regex

        session = requests.Session()
        login_password = WP_LOGIN_PASS or WP_APP_PASS

        # Schritt 1: WordPress Login per wp-login.php (Cookie-basiert)
        login_r = session.post(
            f"{WC_URL}/wp-login.php",
            data={
                "log": WP_USER,
                "pwd": login_password,
                "wp-submit": "Log In",
                "redirect_to": f"{WC_URL}/wp-admin/",
                "testcookie": "1"
            },
            allow_redirects=True,
            timeout=30
        )

        # Pruefen ob Login erfolgreich (Redirect zum Dashboard)
        if "wp-admin" not in login_r.url and login_r.status_code != 200:
            log.warning(f"WordPress Login fehlgeschlagen (Status: {login_r.status_code})")

            # Fallback: Application Password als Basic Auth
            import base64
            credentials = base64.b64encode(f"{WP_USER}:{WP_APP_PASS}".encode()).decode()
            session.headers.update({"Authorization": f"Basic {credentials}"})

        # Schritt 2: Bestellseite laden um Nonce zu holen
        order_page = session.get(
            f"{WC_URL}/wp-admin/post.php",
            params={"post": str(order_id), "action": "edit"},
            timeout=30
        )

        if order_page.status_code != 200:
            log.warning(f"Bestellseite nicht erreichbar (Status: {order_page.status_code})")
            # Trotzdem versuchen ohne Nonce
            nonce = ""
        else:
            # Nonce aus der Seite extrahieren
            nonce_match = regex.search(r'_wpnonce["\s]*(?:value="|:)["\s]*([a-f0-9]+)', order_page.text)
            if not nonce_match:
                # Alternativer Nonce-Suche
                nonce_match = regex.search(r'wp_nonce["\s]*(?:value="|:)["\s]*([a-f0-9]+)', order_page.text)
            nonce = nonce_match.group(1) if nonce_match else ""
            log.info(f"WordPress Nonce gefunden: {nonce[:10]}...")

        # Schritt 3: Rechnungs-PDF herunterladen
        params = {
            "action": "woocommerce_wp_wc_invoice_pdf_invoice_download",
            "order_id": str(order_id)
        }
        if nonce:
            params["_wpnonce"] = nonce

        r = session.get(
            f"{WC_URL}/wp-admin/admin-ajax.php",
            params=params,
            timeout=30
        )

        if r.status_code == 200 and r.headers.get("Content-Type", "").startswith("application/pdf"):
            log.info(f"Rechnungs-PDF heruntergeladen fuer Bestellung #{order_id} ({len(r.content)} Bytes)")
            return r.content
        elif r.status_code == 200 and len(r.content) > 1000:
            # Manchmal kommt PDF ohne korrekten Content-Type
            if r.content[:5] == b'%PDF-':
                log.info(f"Rechnungs-PDF heruntergeladen (ohne Content-Type) fuer #{order_id} ({len(r.content)} Bytes)")
                return r.content
            else:
                log.warning(f"Rechnungs-PDF: Unerwarteter Content fuer #{order_id} (Status: {r.status_code}, Type: {r.headers.get('Content-Type', 'unbekannt')})")
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


def process_mail(subject, sender, body, channel="shop", ebay_thread_id=None, images=None, ebay_item_id=None, ebay_recipient=None, original_message_id=None, original_references=None, imap_uid=None, ebay_msg_id_for_flag=None):
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
        "ebay_msg_id_for_flag": ebay_msg_id_for_flag,
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
    """Pruefe ob die Mail eine eBay-Benachrichtigung ist (ignorieren).
    Blockt alle eBay-Domains unabhaengig vom Laendercode (.de, .com, .ca, .co.uk, .fr, etc.)."""
    sender_lower = sender.lower()
    ebay_patterns = ["@members.ebay.", "@ebay.", "@reply.ebay."]
    return any(pattern in sender_lower for pattern in ebay_patterns)


def is_system_notification(sender):
    """Pruefe ob die Mail eine System-Benachrichtigung ist die ignoriert werden soll.
    Betrifft Sendcloud, Versanddienstleister-Benachrichtigungen etc."""
    sender_lower = sender.lower()
    system_senders = [
        "no-reply@sendcloud.com",
        "noreply@sendcloud.com",
    ]
    return any(addr in sender_lower for addr in system_senders)


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
                    imap.store(mid, '+FLAGS', '\\Seen')
                    continue

                # System-Benachrichtigungen ignorieren (Sendcloud etc.)
                if is_system_notification(sender):
                    log.info(f"System-Mail ignoriert: {sender} - {subject}")
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


def transcribe_voice_message(file_id):
    """Telegram Voice Message herunterladen und per Groq Whisper transkribieren."""
    if not GROQ_API_KEY:
        log.warning("Sprachnachricht empfangen, aber GROQ_API_KEY nicht konfiguriert")
        return None

    try:
        # Schritt 1: File-Info von Telegram holen
        r = requests.get(
            f"https://api.telegram.org/bot{TG_TOKEN}/getFile",
            params={"file_id": file_id},
            timeout=10
        )
        if r.status_code != 200:
            log.error(f"Telegram getFile Fehler: {r.status_code}")
            send_telegram_text(f"⚠️ Telegram File-Info Fehler: {r.status_code}")
            return None

        file_path = r.json().get("result", {}).get("file_path", "")
        if not file_path:
            log.error("Telegram getFile: Kein file_path")
            send_telegram_text("⚠️ Telegram hat keinen Dateipfad zurückgegeben.")
            return None

        # Schritt 2: Datei herunterladen
        file_r = requests.get(
            f"https://api.telegram.org/file/bot{TG_TOKEN}/{file_path}",
            timeout=30
        )
        if file_r.status_code != 200:
            log.error(f"Telegram File-Download Fehler: {file_r.status_code}")
            send_telegram_text(f"⚠️ Audio-Download Fehler: {file_r.status_code}")
            return None

        audio_data = file_r.content
        log.info(f"Voice Message heruntergeladen: {len(audio_data)} Bytes ({file_path})")

        # Schritt 3: An Groq Whisper API senden
        # Telegram sendet Voice als .oga - Groq akzeptiert das nicht
        # .oga ist technisch OGG/Opus, daher als .ogg senden
        filename = "voice.ogg"

        # Beide Modelle probieren (whisper-large-v3-turbo ist schneller)
        models = ["whisper-large-v3-turbo", "whisper-large-v3"]

        for model in models:
            log.info(f"Versuche Groq Modell: {model}")
            groq_r = requests.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": (filename, audio_data, "audio/ogg")},
                data={
                    "model": model,
                    "language": "de",
                    "response_format": "text"
                },
                timeout=30
            )

            if groq_r.status_code == 200:
                transcript = groq_r.text.strip()
                if transcript:
                    log.info(f"Sprachnachricht transkribiert ({model}): {transcript[:100]}")
                    return transcript
                else:
                    log.warning(f"Groq {model}: Leere Antwort")
                    continue
            else:
                error_text = groq_r.text[:300]
                log.error(f"Groq {model} Fehler {groq_r.status_code}: {error_text}")
                # Beim ersten Modell: nochmal mit dem anderen versuchen
                if model == models[0]:
                    log.info("Versuche Fallback-Modell...")
                    continue
                else:
                    # Beide Modelle fehlgeschlagen - Fehler in Telegram zeigen
                    send_telegram_text(
                        f"⚠️ Groq Whisper Fehler ({groq_r.status_code}):\n"
                        f"<code>{error_text[:200]}</code>"
                    )
                    return None

        # Kein Modell hat funktioniert
        send_telegram_text("⚠️ Spracherkennung fehlgeschlagen bei allen Modellen.")
        return None

    except Exception as e:
        log.error(f"Voice-Transkription: {e}")
        send_telegram_text(f"⚠️ Voice-Fehler: {str(e)[:200]}")
        return None


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
                # eBay-Nachricht als beantwortet markieren (Flagged-Häkchen im eBay-Portal)
                if p.get("ebay_msg_id_for_flag"):
                    ebay_mark_as_flagged(p["ebay_msg_id_for_flag"])
                # Statistik
                reset_daily_stats_if_needed()
                daily_stats["answered"] += 1
                if p.get("channel") == "ebay":
                    daily_stats["ebay_answered"] += 1
                else:
                    daily_stats["shop_answered"] += 1

                # Termin erkennen und ICS-Datei + Google Calendar Link senden
                termin = extract_termin_from_draft(p.get("draft", ""))
                if termin:
                    ics = create_ics_file(termin)
                    filename = f"Termin_{termin['date']}_{termin['typ'].replace(' ', '_')}.ics"
                    send_telegram_document(
                        ics, filename,
                        f"📅 Termin: {termin['typ']} mit {termin['kunde']} am {termin['date']} um {termin['time']} Uhr"
                    )

                    # Google Calendar Link als Alternative (funktioniert immer)
                    from datetime import datetime as dt, timedelta
                    start = dt.strptime(f"{termin['date']} {termin['time']}", "%Y-%m-%d %H:%M")
                    end = start + timedelta(minutes=30)
                    gcal_start = start.strftime("%Y%m%dT%H%M%S")
                    gcal_end = end.strftime("%Y%m%dT%H%M%S")
                    gcal_title = f"{termin['typ']} - {termin['kunde']}".replace(" ", "+")
                    gcal_location = "Max-Planck-Str.+18,+63322+Rödermark"
                    gcal_url = f"https://calendar.google.com/calendar/render?action=TEMPLATE&text={gcal_title}&dates={gcal_start}/{gcal_end}&location={gcal_location}&ctz=Europe/Berlin"
                    send_telegram_text(
                        f"📅 <b>Alternativ direkt im Browser hinzufügen:</b>\n"
                        f"<a href=\"{gcal_url}\">➕ Google Kalender</a>"
                    )
            else:
                send_telegram_text(f"⚠️ Fehler! Bitte manuell antworten an {p['sender']}")
            # Alte Nachrichten + Edit-Zwischen-Nachrichten aus Telegram loeschen
            delete_telegram_messages(p.get("telegram_msg_ids", []))
            delete_telegram_messages(p.get("edit_msg_ids", []))
        elif action == "edit":
            pending[token]["awaiting_edit"] = True
            if "edit_msg_ids" not in pending[token]:
                pending[token]["edit_msg_ids"] = []
            # Statistik: Korrektur zaehlen
            reset_daily_stats_if_needed()
            daily_stats["edited"] += 1
            edit_prompt_id = send_telegram_text(
                f"✏️ Was soll ich aendern?\n\n"
                f"Beispiele:\n"
                f"- 15 EUR Erstattung anbieten\n"
                f"- Freundlicher formulieren\n"
                f"- Retourenlink Shop einfuegen\n"
                f"- Auf Englisch schreiben\n\n"
                f"💡 Du kannst auch eine Sprachnachricht schicken!\n\n"
                f"Token: <code>{token}</code>"
            )
            if edit_prompt_id:
                pending[token]["edit_msg_ids"].append(edit_prompt_id)
        elif action == "ignore":
            tg_ids = p.get("telegram_msg_ids", [])
            edit_ids = p.get("edit_msg_ids", [])
            # Mail als gelesen markieren
            mark_mail_as_seen(p.get("imap_uid"))
            # Statistik
            reset_daily_stats_if_needed()
            daily_stats["ignored"] += 1
            del pending[token]
            send_telegram_text("🗑️ Vorgang ignoriert.")
            # Alte Nachrichten + Edit-Zwischen-Nachrichten aus Telegram loeschen
            delete_telegram_messages(tg_ids)
            delete_telegram_messages(edit_ids)

    elif "message" in update:
        text = update["message"].get("text", "").strip()
        msg_id = update["message"].get("message_id")

        # Voice Message: Transkribieren und als Text weiterverarbeiten
        voice = update["message"].get("voice") or update["message"].get("audio")
        if voice and not text:
            file_id = voice.get("file_id")
            duration = voice.get("duration", 0)
            if not GROQ_API_KEY:
                send_telegram_text("🎤 Sprachnachricht empfangen, aber Spracherkennung nicht konfiguriert.\nBitte GROQ_API_KEY in Railway eintragen.")
                return
            if duration > 300:  # Max 5 Minuten
                send_telegram_text("⚠️ Sprachnachricht zu lang (max 5 Minuten).")
                return

            hoere_zu_id = send_telegram_text("🎤 Höre zu...")
            transcript = transcribe_voice_message(file_id)
            if transcript:
                text = transcript
                verstanden_id = send_telegram_text(f"🎤 Verstanden:\n<i>{transcript}</i>")
                # Voice-Message + Bot-Antworten zum aktiven Edit-Vorgang tracken
                for _token, _p in pending.items():
                    if _p.get("awaiting_edit"):
                        if "edit_msg_ids" not in _p:
                            _p["edit_msg_ids"] = []
                        if msg_id:
                            _p["edit_msg_ids"].append(msg_id)  # User Voice-Message
                        if hoere_zu_id:
                            _p["edit_msg_ids"].append(hoere_zu_id)
                        if verstanden_id:
                            _p["edit_msg_ids"].append(verstanden_id)
                        break
            else:
                send_telegram_text("⚠️ Sprachnachricht konnte nicht erkannt werden. Bitte nochmal versuchen oder als Text schreiben.")
                return

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
            voice_status = "aktiv" if GROQ_API_KEY else "nicht konfiguriert (GROQ_API_KEY fehlt)"
            status_msg = (
                f"📊 <b>Bot-Status</b>\n"
                f"Offene Vorgaenge: {pending_count}\n"
                f"Korrekturen im Archiv: {fb_count}\n"
                f"eBay API: {ebay_status}\n"
                f"eBay verarbeitet: {len(ebay_processed_ids)} Nachrichten\n"
                f"🎤 Sprachnachrichten: {voice_status}"
            )
            send_telegram_text(status_msg)
            return

        if text == "/stats" or text == "/stats@ModellbahnAssistentBot":
            reset_daily_stats_if_needed()
            send_daily_summary()
            return

        if text == "/feedback" or text == "/feedback@ModellbahnAssistentBot":
            try:
                fb = load_feedback()
                if fb:
                    fb_text = json.dumps(fb, ensure_ascii=False, indent=2)
                    send_telegram_document(fb_text, "feedback_history.json", f"📊 {len(fb)} Korrekturen")
                else:
                    send_telegram_text("Keine Feedback-Daten vorhanden.")
            except Exception as e:
                send_telegram_text(f"⚠️ Fehler: {e}")
            return

        if text.startswith("/"):
            return

        for token, p in list(pending.items()):
            if p.get("awaiting_edit"):
                # User-Nachricht (Text) zum Edit-Vorgang tracken
                if "edit_msg_ids" not in p:
                    p["edit_msg_ids"] = []
                if msg_id:
                    p["edit_msg_ids"].append(msg_id)

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

                # Alte Telegram-Nachrichten + Edit-Zwischen-Nachrichten loeschen
                delete_telegram_messages(p.get("telegram_msg_ids", []))
                delete_telegram_messages(p.get("edit_msg_ids", []))
                pending[token]["edit_msg_ids"] = []

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
# KALENDER: ICS-Datei erstellen und per Telegram senden
# ============================================================

def extract_termin_from_draft(draft_text):
    """Extrahiere Termindaten aus dem Antwort-Entwurf (TERMIN: YYYY-MM-DD HH:MM | Typ | Name)."""
    import re
    match = re.search(r'TERMIN:\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s*\|\s*([^|]+)\s*\|\s*(.+)', draft_text)
    if match:
        return {
            "date": match.group(1),
            "time": match.group(2),
            "typ": match.group(3).strip(),
            "kunde": match.group(4).strip()
        }
    return None


def create_ics_file(termin_data):
    """Erstelle eine ICS-Kalenderdatei fuer den Termin."""
    from datetime import datetime, timedelta
    import uuid

    date_str = termin_data["date"]
    time_str = termin_data["time"]
    typ = termin_data["typ"]
    kunde = termin_data["kunde"]

    # Start- und Endzeit berechnen (30 Minuten Dauer)
    start = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    end = start + timedelta(minutes=30)

    uid = str(uuid.uuid4())
    now = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    start_str = start.strftime("%Y%m%dT%H%M%S")
    end_str = end.strftime("%Y%m%dT%H%M%S")

    # ICS Format - KEINE Einrueckung, jede Zeile muss am Anfang beginnen!
    # VTIMEZONE fuer Apple Kalender Kompatibilitaet
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:REQUEST",
        "PRODID:-//Modellbahn-Rhein-Main//Mailbot//DE",
        "BEGIN:VTIMEZONE",
        "TZID:Europe/Berlin",
        "BEGIN:STANDARD",
        "DTSTART:19701025T030000",
        "RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=10",
        "TZOFFSETFROM:+0200",
        "TZOFFSETTO:+0100",
        "TZNAME:CET",
        "END:STANDARD",
        "BEGIN:DAYLIGHT",
        "DTSTART:19700329T020000",
        "RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=3",
        "TZOFFSETFROM:+0100",
        "TZOFFSETTO:+0200",
        "TZNAME:CEST",
        "END:DAYLIGHT",
        "END:VTIMEZONE",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{now}",
        f"DTSTART;TZID=Europe/Berlin:{start_str}",
        f"DTEND;TZID=Europe/Berlin:{end_str}",
        f"SUMMARY:{typ} - {kunde}",
        "LOCATION:Max-Planck-Str. 18\\, 63322 Roedermark",
        f"DESCRIPTION:{typ} mit {kunde} bei Modellbahn-Rhein-Main",
        "STATUS:CONFIRMED",
        "BEGIN:VALARM",
        "TRIGGER:-PT30M",
        "ACTION:DISPLAY",
        f"DESCRIPTION:Termin in 30 Minuten: {typ} mit {kunde}",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR"
    ]

    # ICS braucht CRLF Zeilenenden
    return "\r\n".join(lines)


def send_telegram_document(file_content, filename, caption=""):
    """Sende ein Dokument (z.B. ICS-Datei) per Telegram."""
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument"
    try:
        file_bytes = file_content.encode("utf-8") if isinstance(file_content, str) else file_content

        # Versuche verschiedene MIME-Types fuer beste iOS-Kompatibilitaet
        # text/calendar ist der offizielle Standard
        r = requests.post(
            url,
            files={"document": (filename, file_bytes, "text/calendar")},
            data={"chat_id": TG_CHAT_ID, "caption": caption + "\n\n💡 Tipp: Datei antippen → Teilen (↗️) → 'In Kalender kopieren' oder direkt in Dateien speichern und von dort öffnen."},
            timeout=15
        )
        if r.status_code == 200:
            log.info(f"Kalender-Datei gesendet: {filename}")
            return r.json().get("result", {}).get("message_id")
        else:
            log.error(f"Telegram Dokument Fehler {r.status_code}: {r.text[:100]}")
    except Exception as e:
        log.error(f"Telegram Dokument: {e}")
    return None


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
            msg_type_match = re.search(r'<MessageType>(.*?)</MessageType>', detail_text)

            sender = sender_match.group(1) if sender_match else ""
            subject = subject_match.group(1) if subject_match else "eBay Nachricht"
            raw_body = body_match.group(1) if body_match else ""
            item_id = item_match.group(1) if item_match else ""
            ext_id = ext_msg_id_match.group(1) if ext_msg_id_match else msg_id
            msg_type = msg_type_match.group(1) if msg_type_match else ""

            # === FILTER: Nur echte Kaeufer-Nachrichten durchlassen ===

            # eBay System-Absender ignorieren
            ebay_system_senders = ["ebay", "ebay kundenservice", "ebay customer service",
                                   "ebay.de", "ebay.com", "members.ebay"]
            if sender.lower() in ebay_system_senders or not sender:
                log.info(f"eBay System-Nachricht ignoriert von '{sender}': {subject[:80]}")
                ebay_mark_as_read(token, msg_id, headers_xml)
                continue

            # eBay System-Betreff-Muster ignorieren
            system_subjects = [
                "ruecksendung", "rücksendung", "rückerstattung", "rueckerstattung",
                "erstattung", "refund", "return",
                "auszahlung", "zahlung eingegangen", "payment",
                "bewertung", "feedback", "review",
                "angebot läuft", "angebot lauft", "listing",
                "rechnung", "invoice",
                "versanddetails", "shipping",
                "erinnerung", "reminder",
                "ihr konto", "your account",
                "promotion", "angebot für sie", "special offer",
                "verkaufsaktion", "sales event",
                "wichtige information", "important information",
                "richtlinien", "policy", "policies",
                "verifizierung", "verification"
            ]
            subject_lower = subject.lower()
            is_system = any(kw in subject_lower for kw in system_subjects)

            # MessageType pruefen - nur AskSellerQuestion und andere Kaeufer-Typen durchlassen
            buyer_msg_types = ["AskSellerQuestion", "ResponseToASQQuestion", "ContactEbayMember",
                              "ContacteBayMemberViaCommunityLink", ""]
            if msg_type and msg_type not in buyer_msg_types:
                is_system = True

            if is_system:
                log.info(f"eBay System-Nachricht ignoriert: {subject[:80]} (Typ: {msg_type})")
                ebay_mark_as_read(token, msg_id, headers_xml)
                continue

            # === FILTER BESTANDEN: Echte Kaeufer-Nachricht ===

            # HTML aus Body entfernen - eBay-spezifische aggressive Bereinigung
            body = ebay_html_to_text(raw_body)

            # Auch Subject bereinigen
            subject = subject.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
            subject = subject.replace('&quot;', '"').replace('&apos;', "'")

            if sender:
                full_subject = f"[eBay] {subject}"
                if item_id:
                    full_subject += f" (Artikel: {item_id})"

                if not body:
                    body = f"(Kunde hat eine Nachricht zu Artikel {item_id} gesendet, aber kein Text enthalten. Betreff: {subject})"

                log.info(f"eBay Kaeufer-Nachricht von {sender}: {subject} | Body: {body[:100]}")

                # Vorherige Nachrichten im Thread extrahieren (Konversationshistorie)
                prev_messages = re.findall(r'<ResponseDetails>(.*?)</ResponseDetails>', detail_text, re.DOTALL)
                conversation_history = ""
                if prev_messages:
                    history_parts = []
                    for prev in prev_messages:
                        prev_sender_m = re.search(r'<SenderLoginName>(.*?)</SenderLoginName>', prev)
                        prev_body_m = re.search(r'<ResponseText>(.*?)</ResponseText>', prev, re.DOTALL)
                        prev_date_m = re.search(r'<CreationDate>(.*?)</CreationDate>', prev)
                        if prev_body_m:
                            prev_sender_name = prev_sender_m.group(1) if prev_sender_m else "Unbekannt"
                            prev_body_text = prev_body_m.group(1)
                            if "<" in prev_body_text:
                                prev_body_text = ebay_html_to_text(prev_body_text)
                            prev_date = prev_date_m.group(1)[:10] if prev_date_m else ""
                            history_parts.append(f"[{prev_date} {prev_sender_name}]: {prev_body_text.strip()}")
                    if history_parts:
                        conversation_history = "\n\n--- Bisheriger Verlauf ---\n" + "\n\n".join(history_parts)

                # Bilder aus der eBay-Nachricht extrahieren
                ebay_images = []
                img_urls = re.findall(r'<MessageMediaURL>(.*?)</MessageMediaURL>', detail_text)
                for img_url in img_urls[:3]:  # Max 3 Bilder
                    try:
                        img_r = requests.get(img_url, timeout=10)
                        if img_r.status_code == 200 and img_r.headers.get("Content-Type", "").startswith("image"):
                            ebay_images.append(img_r.content)
                            log.info(f"eBay Bild heruntergeladen: {img_url[:60]}...")
                    except Exception as e:
                        log.warning(f"eBay Bild laden: {e}")

                # Vollstaendigen Body mit Verlauf zusammenbauen
                full_body_with_history = body + conversation_history

                process_mail(
                    full_subject, sender, full_body_with_history,
                    channel="ebay", ebay_thread_id=ext_id,
                    ebay_item_id=item_id, ebay_recipient=sender,
                    images=ebay_images if ebay_images else None,
                    ebay_msg_id_for_flag=msg_id
                )

                # Nachricht bei eBay als gelesen markieren
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


def ebay_mark_as_flagged(message_id):
    """Markiere eine eBay-Nachricht als beantwortet (Flagged) ueber ReviseMyMessages."""
    if not EBAY_ENABLED:
        return
    token = ebay_get_access_token()
    if not token:
        return
    try:
        headers_xml = {
            "X-EBAY-API-SITEID": "77",
            "X-EBAY-API-COMPATIBILITY-LEVEL": "967",
            "X-EBAY-API-CALL-NAME": "ReviseMyMessages",
            "X-EBAY-API-IAF-TOKEN": token,
            "Content-Type": "text/xml"
        }

        xml_request = f"""<?xml version="1.0" encoding="utf-8"?>
<ReviseMyMessagesRequest xmlns="urn:ebay:apis:eBLBaseComponents">
    <RequesterCredentials>
        <eBayAuthToken>{token}</eBayAuthToken>
    </RequesterCredentials>
    <MessageIDs>
        <MessageID>{message_id}</MessageID>
    </MessageIDs>
    <Flagged>true</Flagged>
</ReviseMyMessagesRequest>"""

        r = requests.post(
            "https://api.ebay.com/ws/api.dll",
            headers=headers_xml,
            data=xml_request.encode("utf-8"),
            timeout=10
        )
        if r.status_code == 200 and "<Ack>Success</Ack>" in r.text:
            log.info(f"eBay Nachricht als beantwortet markiert (Flagged)")
        else:
            log.warning(f"eBay Flagged fehlgeschlagen: {r.text[:100]}")
    except Exception as e:
        log.warning(f"eBay mark as flagged: {e}")


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
    log.info("Modellbahn-Rhein-Main Mail Assistent v6 gestartet")

    # Feedback-Status anzeigen
    fb_count = len(load_feedback())
    fb_info = f"\n📊 {fb_count} Korrekturen im Lernarchiv" if fb_count > 0 else ""
    ebay_info = "✅ eBay API aktiv" if EBAY_ENABLED else "⏳ eBay API noch nicht konfiguriert"
    voice_info = "✅ Sprachnachrichten aktiv" if GROQ_API_KEY else "⏳ Sprachnachrichten nicht konfiguriert (GROQ_API_KEY)"

    send_telegram_text(
        f"🚂 <b>Modellbahn Mail Assistent v6 gestartet!</b>\n"
        f"Ich ueberwache dein Postfach und eBay.\n\n"
        f"📂 Feine Kategorien (Lieferstatus, Retoure, Beschwerde, ...)\n"
        f"🧠 Lerne aus deinen Korrekturen\n"
        f"📦 Bessere Bestelldaten aus WooCommerce\n"
        f"🏪 {ebay_info}\n"
        f"🎤 {voice_info}{fb_info}"
    )
    offset     = 0
    mail_timer = 0
    ebay_timer = 0
    summary_sent_today = False
    while True:
        try:
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
        except Exception as e:
            log.error(f"Main loop error: {e}")
            time.sleep(10)


if __name__ == "__main__":
    main()
