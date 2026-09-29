import os
import json
from openai import OpenAI
import base64
import time
import requests
from bs4 import BeautifulSoup
from email.mime.text import MIMEText

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

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

GMAIL_TOKEN_JSON = os.environ.get("GMAIL_TOKEN")
GMAIL_RECIPIENT = os.environ.get("GMAIL_RECIPIENT")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

# Inicializar el cliente de DeepSeek (usando el formato compatible de OpenAI)
client = OpenAI(
    api_key=os.environ.get("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com"
)

def extraer_con_ia(texto_aviso, url):
    """
    Usar DeepSeek para extraer información estructurada del texto de la convocatoria
    """
    system_prompt = """
    Eres un asistente especializado en extraer información de convocatorias docentes.
    El usuario te proporcionará el texto de una convocatoria, debes devolver ÚNICAMENTE un objeto JSON válido, sin ningún texto explicativo ni bloques de código Markdown.

    El formato JSON es el siguiente:
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

    Si un dato no está en el texto, usa null. No inventes información.
    """

    try:
        response = client.chat.completions.create(
            model="deepseek-flash",  # Usar el modelo económico Flash
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": texto_aviso}
            ],
            response_format={"type": "json_object"},  # Habilitar salida JSON
            max_tokens=500,
            temperature=0,  # Extracción determinista, usar 0
            extra_body={"thinking": {"type": "disabled"}}  # Modo de extracción, desactivar razonamiento
        )
        
        # Verificar si la respuesta está vacía
        content = response.choices[0].message.content
        if not content or not content.strip():
            print("  ⚠️ La IA devolvió contenido vacío")
            return None
            
        datos = json.loads(content)
        datos["url"] = url
        return datos

    except Exception as e:
        print(f"  ⚠️ Falló la extracción con IA: {e}")
        return None  # Devolver None en caso de fallo, no interrumpir el flujo principal


def check_article_content(url):
    """Entrar en el enlace y usar IA para extraer datos"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        
        text = soup.get_text(separator=" ").strip()
        text_lower = text.lower()
        
        # Primero usar palabras clave para filtrar rápidamente
        matched = [kw for kw in KEYWORDS if kw in text_lower]
        if not matched:
            return None
        
        print(f"    ✅ Coincidencia de palabras clave: {', '.join(matched)}")
        
        # Extraer datos con IA
        datos = extraer_con_ia(text, url)
        if datos:
            datos["matched"] = matched
            # Mantener el título original
            title_tag = soup.select_one("h1.entry-title, h1")
            datos["title"] = title_tag.get_text(strip=True) if title_tag else "Sin título"
            return datos
        else:
            # Si la IA falla, devolver un objeto mínimo para no perder esta convocatoria
            return {
                "url": url,
                "matched": matched,
                "title": "Error en extracción con IA",
                "raw_text": text[:500]  # Guardar los primeros 500 caracteres del texto original
            }

    except Exception as e:
        print(f"  ⚠️ Error al leer {url}: {e}")
        return None
        
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


def get_feed_items():
    """Lee el RSS y devuelve la lista de items (title, link, pubDate)"""
    try:
        r = requests.get(FEED_URL, headers=HEADERS, timeout=20)
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
        print(f"Error al leer el feed: {e}")
        return []


def check_article_content(url):
    """Entra a la publicación individual y busca keywords en el contenido completo"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")

        # Eliminar scripts y estilos para no contaminar el texto
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()

        text = soup.get_text(separator=" ").lower()
        matched = [kw for kw in KEYWORDS if kw in text]
        return matched

    except Exception as e:
        print(f"  ⚠️ Error al leer {url}: {e}")
        return []


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

        # Marcar como vista (aunque no coincida, para no re-revisar)
        seen.add(link)

        print(f"  🔎 Revisando: {item['title'][:60]}...")
        matched = check_article_content(link)

        if matched:
            print(f"    ✅ Coincide: {', '.join(matched)}")
            new_matches.append({
                "title": item["title"],
                "link": link,
                "pub_date": item["pub_date"],
                "matched": matched
            })
        else:
            print(f"    - Sin coincidencias")

        # Pequeña pausa para no saturar el servidor
        time.sleep(1)

    save_seen(seen)

        if new_matches:
        subject = f"CGE Uruguay — {len(new_matches)} convocatorias relevantes"
        body_lines = [f"Se encontraron {len(new_matches)} convocatorias relevantes:\n"]
        
        for i, m in enumerate(new_matches, 1):
            body_lines.append(f"━━━ {i}. {m.get('title', 'Sin título')} ━━━")
            
            # Mostrar campos extraídos por IA
            if m.get("escuela"):
                body_lines.append(f"🏫 Escuela: {m['escuela']}")
            if m.get("cargo"):
                body_lines.append(f"📚 Cargo: {m['cargo']}")
            if m.get("horas"):
                body_lines.append(f"⏱️ Horas: {m['horas']}")
            if m.get("caracter"):
                body_lines.append(f"👤 Carácter: {m['caracter']}")
            if m.get("fecha_concurso"):
                body_lines.append(f"📅 Fecha: {m['fecha_concurso']}")
            if m.get("hora_concurso"):
                body_lines.append(f"🕐 Hora: {m['hora_concurso']}")
            if m.get("dias_horario"):
                body_lines.append(f"🗓️ Días y horario: {m['dias_horario']}")
            if m.get("lugar"):
                body_lines.append(f"📍 Lugar: {m['lugar']}")
            
            body_lines.append(f"🔗 Enlace: {m['url']}")
            body_lines.append(f"🔑 Palabras clave: {', '.join(m.get('matched', []))}\n")
            
            # Si hay error en extracción con IA, mostrar texto original
            if m.get("raw_text"):
                body_lines.append(f"📄 Texto original (parcial): {m['raw_text'][:200]}...\n")
        
        body = "\n".join(body_lines)
        send_email(subject, body)
        print(f"\n📧 Notificación enviada, {len(new_matches)} coincidencias en total.")
    else:
        print("\nSin coincidencias nuevas en el feed.")


if __name__ == "__main__":
    main()
