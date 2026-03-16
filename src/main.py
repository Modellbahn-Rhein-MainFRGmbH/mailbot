"""
Modellbahn-Rhein-Main Mail Assistent v3
Fabian Rauch - IMAP + Brevo SMTP + Bildanzeige
"""

import imaplib
import smtplib
import email as email_lib
import os
import json
import time
import logging
import requests
import hashlib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from anthropic import Anthropic

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Mail empfangen (IMAP)
MAIL_HOST  = os.environ["MAIL_HOST"]
MAIL_USER  = os.environ["MAIL_USER"]
MAIL_PASS  = os.environ["MAIL_PASS"]

# Mail senden (Brevo SMTP)
SMTP_HOST  = os.environ.get("SMTP_HOST", "smtp-relay.brevo.com")
SMTP_PORT  = int(os.environ.get("SMTP_PORT", "587"))
BREVO_USER = os.environ.get("BREVO_USER", "")
BREVO_PASS = os.environ.get("BREVO_PASS", "")

# Shop APIs
WC_URL     = os.environ.get("WC_URL", "")
WC_KEY     = os.environ.get("WC_KEY", "")
WC_SECRET  = os.environ.get("WC_SECRET", "")
SC_KEY     = os.environ.get("SC_KEY", "")
SC_SECRET  = os.environ.get("SC_SECRET", "")
EBAY_TOKEN = os.environ.get("EBAY_TOKEN", "")

# Telegram
TG_TOKEN   = os.environ["TG_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]

# Claude
ANTHROPIC_KEY = os.environ["ANTHROPIC_KEY"]

client  = Anthropic(api_key=ANTHROPIC_KEY)
pending = {}

SIGNATURE = """Beste Gruesse,

Fabian Rauch
Geschaeftsfuehrer
Modellbahn-Rhein-Main FR GmbH

Tel: 0160 3833340
E-Mail: info@modellbahn-rhein-main.de
Web: www.modellbahn-rhein-main.de
Adresse: Max-Planck-Str. 18, 63322 Roedrmark

Handelsregister: Amtsgericht Offenbach, HRB 58191
Umsatzsteuer-ID gemass 27a UStG: DE456540670

Hinweis: Diese E-Mail enthaelt vertrauliche Informationen. Wenn Sie nicht der
beabsichtigte Empfaenger sind, informieren Sie bitte den Absender und loeschen
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

BEISPIEL 1 - Falsche Achsen:
Antwort: Hallo Karl, es tut mir sehr leid, dass die Achsen falsch beschrieben waren. Leider haben wir aktuell keinen Ersatz. 1. Teilrueckerstattung als Entschaedigung. 2. Rueckgabe gegen vollen Kaufpreis.

BEISPIEL 2 - Technischer Defekt:
Antwort: Sehr geehrter Herr Schmeller, das Pulsieren deutet auf einen undefinierten Decoder-Zustand hin. Gerne koennen Sie die Lok zur Ueberpruefung einsenden. Bitte Fehlerbeschreibung und Kontaktdaten beilegen.

BEISPIEL 3 - Verschmutzung:
Antwort: Sehr geehrter Herr Schminke, die Verschmutzung haette uns auffallen muessen, das tut mir leid. 1. Sie behalten den Wagen, ich erstatte 5 EUR. 2. Ruecksendung gegen vollen Kaufpreis.

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
    body   = ""
    images = []
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd and not body:
                try:
                    body = part.get_payload(decode=True).decode("utf-8", errors="replace")
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
        try:
            body = msg.get_payload(decode=True).decode("utf-8", errors="replace")
        except:
            body = ""
    return body, images


def classify_mail(subject, body):
    resp = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=20,
        messages=[{"role": "user", "content": (
            "Klassifiziere diese E-Mail fuer einen Modellbahn-Haendler.\n"
            "Antworte NUR mit: 'question' oder 'ignore'.\n"
            "question = Kundenfrage (Lieferstatus, Retoure, Beschaedigung, Produktfrage, Beschwerde)\n"
            "ignore = Rechnung, Newsletter, Spam, automatische Benachrichtigung\n\n"
            f"Betreff: {subject}\nInhalt: {body[:400]}"
        )}]
    )
    return "question" if "question" in resp.content[0].text.lower() else "ignore"


def fetch_woocommerce_order(sender_email):
    if not WC_KEY:
        return None
    try:
        r = requests.get(
            f"{WC_URL}/wp-json/wc/v3/orders",
            auth=(WC_KEY, WC_SECRET),
            params={"search": sender_email, "per_page": 1},
            timeout=10
        )
        orders = r.json()
        if orders and isinstance(orders, list):
            o     = orders[0]
            items = ", ".join(f"{i['name']} (x{i['quantity']})" for i in o.get("line_items", []))
            return {"order_id": o.get("id"), "status": o.get("status"),
                    "total": o.get("total"), "date": o.get("date_created", "")[:10], "items": items}
    except Exception as e:
        log.warning(f"WooCommerce: {e}")
    return None


def fetch_sendcloud_tracking(order_data):
    if not SC_KEY or not order_data:
        return None
    try:
        r = requests.get(
            "https://panel.sendcloud.sc/api/v2/parcels",
            auth=(SC_KEY, SC_SECRET),
            params={"search": str(order_data.get("order_id", ""))},
            timeout=10
        )
        parcels = r.json().get("parcels", [])
        if parcels:
            p = parcels[0]
            return {"tracking_number": p.get("tracking_number"),
                    "status": p.get("status", {}).get("message", ""),
                    "carrier": p.get("carrier", {}).get("code", ""),
                    "tracking_url": p.get("tracking_url", "")}
    except Exception as e:
        log.warning(f"Sendcloud: {e}")
    return None


def build_context(sender_email, order_data, tracking_data):
    lines = []
    if order_data:
        lines.append(f"Bestellung #{order_data['order_id']}: {order_data['items']}")
        lines.append(f"Status: {order_data['status']} | Datum: {order_data['date']} | Betrag: {order_data['total']} EUR")
    if tracking_data:
        lines.append(f"Sendung: {tracking_data['carrier']} {tracking_data['tracking_number']}")
        lines.append(f"Paketstatus: {tracking_data['status']}")
        if tracking_data.get("tracking_url"):
            lines.append(f"Tracking: {tracking_data['tracking_url']}")
    return "\n".join(lines) if lines else "Keine Bestelldaten gefunden."


def generate_draft(subject, body, sender, channel, order_context):
    channel_hint = "eBay-Nachricht" if channel == "ebay" else "Shop-Mail"
    resp = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": (
            f"Kanal: {channel_hint}\n"
            f"Absender: {sender}\n"
            f"Betreff: {subject}\n\n"
            f"BESTELLDATEN:\n{order_context}\n\n"
            f"KUNDEN-NACHRICHT:\n{body}\n\n"
            f"Erstelle die fertige Antwort. Erste Zeile: BETREFF: Re: {subject}"
        )}]
    )
    return resp.content[0].text.strip()


def send_telegram_text(text, reply_markup=None):
    url     = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        log.error(f"Telegram: {e}")


def send_telegram_photo(image_data, caption=""):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto"
    try:
        requests.post(url, files={"photo": ("image.jpg", image_data, "image/jpeg")},
                      data={"chat_id": TG_CHAT_ID, "caption": caption}, timeout=15)
    except Exception as e:
        log.error(f"Telegram Foto: {e}")


def send_approval_request(token, sender, subject, body, draft, channel, order_context, images):
    lines        = draft.split("\n")
    mail_body    = "\n".join(l for l in lines if not l.startswith("BETREFF:")).strip()
    kanal        = "🏪 eBay" if channel == "ebay" else "🛒 Shop"
    body_preview = body.strip()[:500] + ("..." if len(body.strip()) > 500 else "")
    ctx_short    = order_context[:200] + ("..." if len(order_context) > 200 else "")
    draft_prev   = mail_body[:500] + ("..." if len(mail_body) > 500 else "")

    msg = (
        f"{kanal} <b>Neue Kundenanfrage</b>\n"
        f"Von: <code>{sender}</code>\n"
        f"Betreff: {subject}\n\n"
        f"<b>Kunden-Nachricht:</b>\n"
        f"--------------------\n"
        f"{body_preview}\n"
        f"--------------------\n\n"
        f"<b>Bestelldaten:</b>\n<code>{ctx_short}</code>\n\n"
        f"<b>Mein Vorschlag:</b>\n"
        f"--------------------\n"
        f"{draft_prev}\n"
        f"--------------------"
    )
    keyboard = {"inline_keyboard": [
        [{"text": "✅ Senden",     "callback_data": f"approve:{token}"},
         {"text": "✏️ Aendern",   "callback_data": f"edit:{token}"}],
        [{"text": "🗑️ Ignorieren","callback_data": f"ignore:{token}"}]
    ]}
    send_telegram_text(msg, keyboard)
    for i, img in enumerate(images):
        send_telegram_photo(img, f"📷 Bild {i+1} von {len(images)}")


def send_mail(to_addr, subject, body):
    full_body  = body.strip() + "\n\n-- \n" + SIGNATURE
    msg        = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"Modellbahn-Rhein-Main <{MAIL_USER}>"
    msg["To"]      = to_addr
    msg.attach(MIMEText(full_body, "plain", "utf-8"))
    smtp_user  = BREVO_USER if BREVO_USER else MAIL_USER
    smtp_pass  = BREVO_PASS if BREVO_PASS else MAIL_PASS
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)
        log.info(f"Mail gesendet an {to_addr}: {subject}")
        return True
    except Exception as e:
        log.error(f"SMTP Fehler: {e}")
        return False


def process_mail(subject, sender, body, channel="shop", ebay_thread_id=None, images=None):
    token = hashlib.md5(f"{sender}{subject}{body[:50]}".encode()).hexdigest()[:8]
    if token in pending:
        return
    if classify_mail(subject, body) == "ignore":
        log.info(f"Ignoriert: {subject}")
        return
    sender_email = sender.split("<")[-1].replace(">", "").strip()
    order_data   = fetch_woocommerce_order(sender_email)
    tracking     = fetch_sendcloud_tracking(order_data)
    context      = build_context(sender_email, order_data, tracking)
    draft        = generate_draft(subject, body, sender, channel, context)
    pending[token] = {
        "sender": sender_email, "subject": subject, "body": body,
        "draft": draft, "channel": channel,
        "ebay_thread_id": ebay_thread_id,
        "order_context": context, "images": images or []
    }
    send_approval_request(token, sender_email, subject, body, draft, channel, context, images or [])
    log.info(f"Entwurf gesendet fuer {sender_email} (Token: {token})")


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
                body, images = get_mail_body_and_images(msg)
                process_mail(subject, sender, body, channel="shop", images=images)
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
            ok    = send_mail(p["sender"], subj, body)
            del pending[token]
            if ok:
                send_telegram_text(f"✅ Mail an <code>{p['sender']}</code> gesendet!")
            else:
                send_telegram_text(f"⚠️ Fehler! Bitte manuell antworten an {p['sender']}")
        elif action == "edit":
            pending[token]["awaiting_edit"] = True
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
            del pending[token]
            send_telegram_text("🗑️ Vorgang ignoriert.")

    elif "message" in update:
        text = update["message"].get("text", "")
        if not text or text.startswith("/"):
            return
        for token, p in list(pending.items()):
            if p.get("awaiting_edit"):
                lines    = p["draft"].split("\n")
                old_body = "\n".join(l for l in lines if not l.startswith("BETREFF:")).strip()
                resp = client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=1000,
                    system=SYSTEM_PROMPT,
                    messages=[
                        {"role": "user",      "content": f"Bisheriger Entwurf:\n\n{old_body}"},
                        {"role": "assistant", "content": old_body},
                        {"role": "user",      "content": f"Bitte aendere: {text}\n\nVollstaendige ueberarbeitete Mail, erste Zeile: BETREFF: ..."}
                    ]
                )
                new_draft = resp.content[0].text.strip()
                pending[token]["draft"]         = new_draft
                pending[token]["awaiting_edit"] = False
                send_approval_request(
                    token, p["sender"], p["subject"], p["body"],
                    new_draft, p["channel"], p["order_context"], p["images"]
                )
                break


def main():
    log.info("Modellbahn-Rhein-Main Mail Assistent v3 gestartet")
    send_telegram_text("🚂 <b>Modellbahn Mail Assistent v3 gestartet!</b>\nIch ueberwache dein Postfach und eBay.")
    offset     = 0
    mail_timer = 0
    while True:
        updates = get_telegram_updates(offset)
        for upd in updates:
            handle_telegram_update(upd)
            offset = upd["update_id"] + 1
        if time.time() - mail_timer > 120:
            check_inbox()
            mail_timer = time.time()
        time.sleep(2)


if __name__ == "__main__":
    main()
