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
MAIN_URL = "https://cge.entrerios.gov.ar/departamental-uruguay/"
KEYWORDS = ["artes visuales", "lenguaje y producción visual", "plástica", "preceptor", "preceptora"]
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

def check_page():
    """Lee la página principal y busca coincidencias"""
    try:
        r = requests.get(MAIN_URL, headers=HEADERS, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        
        # Obtener todo el texto visible de la página
        text = soup.get_text(separator=" ").lower()
        
        matched = [kw for kw in KEYWORDS if kw in text]
        if not matched:
            print("No se encontraron palabras clave en la página principal.")
            return None

        # Si hay coincidencias, extraer el título y la fecha
        title_tag = soup.select_one("h1, h2, .entry-title")
        title = title_tag.get_text(strip=True) if title_tag else "Página Departamental Uruguay"
        
        date = datetime.now().strftime("%d/%m/%Y")
        
        return {
            "url": MAIN_URL,
            "title": title,
            "date": date,
            "matched": matched
        }
    except Exception as e:
        print(f"Error al leer la página: {e}")
        return None

def main():
    seen = load_seen()
    new_matches = []

    print(f"Revisando: {MAIN_URL}")
    result = check_page()
    
    # Usamos la URL como identificador único
    if result and MAIN_URL not in seen:
        new_matches.append(result)
        seen.add(MAIN_URL)
        print(f"  ✅ Coincidencias encontradas: {result['matched']}")
    elif result:
        print("  - La página ya fue revisada anteriormente o no hay cambios.")
    else:
        print("  - Sin coincidencias nuevas.")

    save_seen(seen)

    if new_matches:
        subject = f"CGE Uruguay — Nuevas publicaciones relevantes"
        body_lines = [f"Se encontraron coincidencias en la página de la Departamental Uruguay:\n"]
        for m in new_matches:
            kw_str = ", ".join(m["matched"])
            body_lines.append(f"Título: {m['title']}")
            body_lines.append(f"Fecha de revisión: {m['date']}")
            body_lines.append(f"Palabras clave: {kw_str}")
            body_lines.append(f"Link: {m['url']}\n")
        
        body = "\n".join(body_lines)
        send_email(subject, body)
        print(f"Notificación enviada.")
    else:
        print("Sin novedades para notificar.")

if __name__ == "__main__":
    main()
