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
BASE_URL = "https://cge.entrerios.gov.ar"
ARCHIVE_URL = f"{BASE_URL}/author/dptal-uruguay/"
KEYWORDS = ["artes visuales", "lenguaje y producción visual", "plástica", "preceptor", "preceptora"]
SEEN_FILE = "seen_urls.txt"

# Desde variables de entorno
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
    """Construye el servicio de Gmail API desde el token en variable de entorno"""
    if not GMAIL_TOKEN_JSON:
        print("GMAIL_TOKEN no configurado")
        return None

    creds_data = json.loads(GMAIL_TOKEN_JSON)
    creds = Credentials.from_authorized_user_info(creds_data)

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())

    return build('gmail', 'v1', credentials=creds)


def send_email(subject, body):
    """Envía un email usando la Gmail API"""
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


def fetch_archive(page=1):
    """Obtiene los links de artículos desde la página de archivo"""
    url = f"{ARCHIVE_URL}page/{page}/" if page > 1 else ARCHIVE_URL
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        links = soup.select("h2.entry-title a, h1.entry-title a, article h2 a")
        return [a["href"] for a in links if a.get("href")]
    except Exception as e:
        print(f"Error al obtener página {page}: {e}")
        return []


def check_article(url):
    """Revisa si un artículo contiene palabras clave"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        text = soup.get_text(separator=" ").lower()

        matched = [kw for kw in KEYWORDS if kw in text]
        if not matched:
            return None

        title_tag = soup.select_one("h1.entry-title, h1")
        title = title_tag.get_text(strip=True) if title_tag else url

        date_tag = soup.select_one("time.entry-date, .entry-date")
        date = date_tag.get_text(strip=True) if date_tag else datetime.now().strftime("%d/%m/%Y")

        return {
            "url": url,
            "title": title,
            "date": date,
            "matched": matched
        }
    except Exception as e:
        print(f"Error al revisar {url}: {e}")
        return None


def main():
    seen = load_seen()
    new_matches = []

    # Revisar las primeras 3 páginas del archivo
    for page in range(1, 4):
        links = fetch_archive(page)
        if not links:
            break
        print(f"Página {page}: {len(links)} artículos")

        for link in links:
            if link in seen:
                continue

            result = check_article(link)
            if result:
                new_matches.append(result)
                print(f"  ✅ Coincide: {result['title']}")

            seen.add(link)

    save_seen(seen)

    if new_matches:
        subject = f"CGE Uruguay — {len(new_matches)} nuevas publicaciones relevantes"
        body_lines = [f"Se encontraron {len(new_matches)} publicaciones relevantes:\n"]
        for i, m in enumerate(new_matches[:15], 1):
            kw_str = ", ".join(m["matched"])
            body_lines.append(f"{i}. {m['title']}")
            body_lines.append(f"   Fecha: {m['date']} | Palabras clave: {kw_str}")
            body_lines.append(f"   Link: {m['url']}\n")
        if len(new_matches) > 15:
            body_lines.append(f"... y {len(new_matches) - 15} más")
        
        body = "\n".join(body_lines)
        send_email(subject, body)
        print(f"Enviadas {len(new_matches)} notificaciones")
    else:
        print("Sin coincidencias nuevas")


if __name__ == "__main__":
    main()
