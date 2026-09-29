import os
import json
import base64
import time
import requests
from bs4 import BeautifulSoup
from email.mime.text import MIMEText

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False
    print("⚠️ openai no está instalado; la extracción con IA no estará disponible")


# ===== Configuración =====
FEED_URL = "http://cge.entrerios.gov.ar/category/uruguay-concursos/feed/"
KEYWORDS = [
    "artes visuales",
    "artes visuals",
    "artesvisuales",
    "lenguaje y producción visual",
    "lenguaje y produccion visual",
    "plástica",
    "plastica",
    "visuales",
    "preceptor",
    "preceptora",
    "preceptores",
    "preceptoría",
    "preceptoria",
]
SEEN_FILE = "seen_urls.txt"
DIAS_MAX = 14

GMAIL_TOKEN_JSON = os.environ.get("GMAIL_TOKEN")
GMAIL_RECIPIENT = os.environ.get("GMAIL_RECIPIENT")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

# Inicializar cliente DeepSeek (si hay API key y librería)
client = None
if OPENAI_AVAILABLE and DEEPSEEK_API_KEY:
    client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com"
    )


# ===== Persistencia =====
def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            return set(line.strip() for line in f if line.strip())
    return set()


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        f.write("\n".join(seen))


# ===== Gmail =====
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
    print(f"📧 Email enviado a {GMAIL_RECIPIENT}")


# ===== Feed =====
def get_feed_items():
    """Lee el RSS con reintentos por si el servidor está lento"""
    max_intentos = 3
    for intento in range(1, max_intentos + 1):
        try:
            r = requests.get(FEED_URL, headers=HEADERS, timeout=90)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "xml")
            items = soup.find_all("item")

            result = []
            for item in items:
                title = item.title.text.strip() if item.title else "Sin título"
                link = item.link.text.strip() if item.link else ""
                pub_date = item.pubDate.text.strip() if item.pubDate else ""
                if link:
                    result.append({
                        "title": title,
                        "link": link,
                        "pub_date": pub_date
                    })
            return result

        except Exception as e:
            print(f"  ⚠️ Intento {intento}/{max_intentos} falló: {e}")
            if intento < max_intentos:
                time.sleep(60)  # esperar 60 segundos antes de reintentar

    print(f"  ❌ No se pudo leer el feed tras {max_intentos} intentos")
    return []


# ===== Extracción con IA =====
def extraer_con_ia(texto_aviso, url):
    """Usar DeepSeek para extraer información estructurada del texto"""
    if not client:
        print("  ⚠️ Cliente IA no disponible (falta API key o librería)")
        return None

    system_prompt = """Eres un asistente especializado en extraer información de convocatorias docentes.
El usuario te proporcionará el texto de una convocatoria. Debes devolver ÚNICAMENTE un objeto JSON válido,
sin texto adicional ni bloques de código Markdown.

Formato exacto:
{
  "escuela": "string o null",
  "cargo": "string o null",
  "horas": "string o null",
  "caracter": "string o null",
  "fecha_concurso": "string o null",
  "hora_concurso": "string o null",
  "lugar": "string o null",
  "dias_horario": "string o null"
}

Si un dato no está en el texto, usa null. No inventes información."""

    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": texto_aviso[:8000]}
            ],
            response_format={"type": "json_object"},
            max_tokens=500,
            temperature=0
        )

        content = response.choices[0].message.content
        if not content or not content.strip():
            print("  ⚠️ La IA devolvió contenido vacío")
            return None

        datos = json.loads(content)
        datos["url"] = url
        return datos

    except Exception as e:
        print(f"  ⚠️ Falló la extracción con IA: {e}")
        return None


# ===== Chequeo de artículo =====
def check_article_content(url):
    """Entra a la publicación, filtra por keywords y extrae datos con IA"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")

        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()

        text = soup.get_text(separator=" ", strip=True)
        text_lower = text.lower()

        matched = [kw for kw in KEYWORDS if kw in text_lower]
        if not matched:
            return None

        print(f"    ✅ Coincidencia: {', '.join(matched)}")

        datos = extraer_con_ia(text, url)
        if datos:
            datos["matched"] = matched
            title_tag = soup.select_one("h1.entry-title, h1")
            datos["title"] = title_tag.get_text(strip=True) if title_tag else "Sin título"
            return datos

        # Fallback: si IA falla, devolver datos mínimos con texto crudo
        return {
            "url": url,
            "matched": matched,
            "title": "Sin título (falló IA)",
            "raw_text": text[:600]
        }

    except Exception as e:
        print(f"  ⚠️ Error al leer {url}: {e}")
        return None


# ===== Main =====
def main():
    print(f"Revisando RSS: {FEED_URL}")
    items = get_feed_items()
    print(f"Se encontraron {len(items)} publicaciones en el feed")

    seen = load_seen()
    new_matches = []

    for item in items:
        link = item["link"]
        if link in seen:
            continue

        seen.add(link)
        print(f"  🔎 Revisando: {item['title'][:60]}...")

        datos = check_article_content(link)
        if datos:
            datos["pub_date"] = item["pub_date"]
            # El título del feed es más limpio que el de la página
            datos["title"] = item["title"]
            new_matches.append(datos)
        else:
            print("    - Sin coincidencias")

        time.sleep(1)

    save_seen(seen)

    if new_matches:
        subject = f"CGE Uruguay — {len(new_matches)} convocatorias relevantes"
        body_lines = [f"Se encontraron {len(new_matches)} convocatorias relevantes:\n"]

        for i, m in enumerate(new_matches, 1):
            body_lines.append(f"━━━ {i}. {m.get('title', 'Sin título')} ━━━")

            if m.get("escuela"):
                body_lines.append(f"🏫 Escuela: {m['escuela']}")
            if m.get("cargo"):
                body_lines.append(f"📚 Cargo: {m['cargo']}")
            if m.get("horas"):
                body_lines.append(f"⏱️  Horas: {m['horas']}")
            if m.get("caracter"):
                body_lines.append(f"👤 Carácter: {m['caracter']}")
            if m.get("fecha_concurso"):
                body_lines.append(f"📅 Fecha: {m['fecha_concurso']}")
            if m.get("hora_concurso"):
                body_lines.append(f"🕐 Hora: {m['hora_concurso']}")
            if m.get("dias_horario"):
                body_lines.append(f"🗓️  Días y horario: {m['dias_horario']}")
            if m.get("lugar"):
                body_lines.append(f"📍 Lugar: {m['lugar']}")

            body_lines.append(f"🔗 Enlace: {m['url']}")
            body_lines.append(f"🔑 Keywords: {', '.join(m.get('matched', []))}\n")

            if m.get("raw_text"):
                body_lines.append(f"📄 Texto original (parcial): {m['raw_text'][:300]}...\n")

        body = "\n".join(body_lines)
        send_email(subject, body)
        print(f"\n📧 Notificación enviada, {len(new_matches)} coincidencias.")
    else:
        print("\nSin coincidencias nuevas en el feed.")


if __name__ == "__main__":
    main()
