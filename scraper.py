import os
import re
import json
import base64
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from email.mime.text import MIMEText

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ===== Configuración =====
FEED_URL = "http://cge.entrerios.gov.ar/category/uruguay-concursos/feed/"
KEYWORDS = ["artes visuales", "artes visuals", "artesvisuales", "lenguaje y producción visual", "lenguaje y produccion visual", "plástica", "plastica", "visuales", "preceptor", "preceptora", "preceptores", "preceptoría", "preceptoria"]
SEEN_FILE = "seen_urls.txt"

GMAIL_TOKEN_JSON = os.environ.get("GMAIL_TOKEN")
GMAIL_RECIPIENT = os.environ.get("GMAIL_RECIPIENT")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            return set(line.strip() for line in f if line.strip())
    return set()

def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        f.write("\n".join(seen))

def get_gmail_service():
    if not GMAIL_TOKEN_JSON:
        print("GMAIL_TOKEN no configurado")
        return None
    creds_data = json.loads(GMAIL_TOKEN_JSON)
    creds = Credentials.from_authorized_user_info(creds_data)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build('gmail', 'v1', credentials=creds)

def send_email(subject, body):
    if not GMAIL_RECIPIENT:
        print("GMAIL_RECIPIENT no configurado")
        return
    service = get_gmail_service()
    if not service:
        return
    message = MIMEText(body, 'plain', 'utf-8')
    message['to'] = GMAIL_RECIPIENT
    message['from'] = 'me'
    message['subject'] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    service.users().messages().send(userId='me', body={'raw': raw}).execute()
    print(f"Email enviado a {GMAIL_RECIPIENT}")
    
def check_feed():
    """Lee el RSS y busca coincidencias en cada publicación individual"""
    try:
        r = requests.get(FEED_URL, headers=HEADERS, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "xml")  # ← parser XML
        items = soup.find_all("item")

        seen = load_seen()
        new_matches = []

        for item in items:
            title = item.title.text if item.title else "Sin título"
            link = item.link.text if item.link else ""
            desc = item.description.text if item.description else ""

            text = (title + " " + desc).lower()
            matched = [kw for kw in KEYWORDS if kw in text]

            if matched and link not in seen:
                new_matches.append({
                    "title": title,
                    "link": link,
                    "matched": matched
                })
                seen.add(link)
                print(f"  ✅ Nuevo: {title}")

        save_seen(seen)
        return new_matches

    except Exception as e:
        print(f"Error al leer el feed: {e}")
        return []    

def main():
    print(f"Revisando RSS: {FEED_URL}")
    new_matches = check_feed()

    if new_matches:
        subject = f"CGE Uruguay — {len(new_matches)} nuevas publicaciones relevantes"
        body_lines = [f"Se encontraron {len(new_matches)} publicaciones nuevas:\n"]
        for i, m in enumerate(new_matches, 1):
            kw_str = ", ".join(m["matched"])
            body_lines.append(f"{i}. {m['title']}")
            body_lines.append(f"   Palabras clave: {kw_str}")
            body_lines.append(f"   Link: {m['link']}\n")

        body = "\n".join(body_lines)
        send_email(subject, body)
        print(f"Notificación enviada con {len(new_matches)} coincidencias.")
    else:
        print("Sin coincidencias nuevas en el feed.")

if __name__ == "__main__":
    main()
