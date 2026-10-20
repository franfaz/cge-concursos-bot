import os
import json
import base64
import time
import re
import requests
from bs4 import BeautifulSoup
from email.mime.text import MIMEText
from datetime import datetime, timedelta

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False
    print("⚠️ openai no está instalado")

# ===== Configuración =====
CGE_FEED_URL = "https://cge.entrerios.gov.ar/category/uruguay-concursos/feed/"
UADER_URL = "https://fcyt.uader.edu.ar/concursos-docentes-del-colegio-del-uruguay/"

KEYWORDS = [
    "artes visuales", "artes visuals", "artesvisuales",
    "lenguaje y producción visual", "lenguaje y produccion visual",
    "plástica", "plastica", "visuales",
    "preceptor", "preceptora", "preceptores", "preceptoría", "preceptoria",
]
SEEN_FILE = "seen_urls.txt"
DIAS_MAX = 14

GMAIL_TOKEN_JSON = os.environ.get("GMAIL_TOKEN")
GMAIL_RECIPIENT = os.environ.get("GMAIL_RECIPIENT")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

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

# ===== Extracción con IA =====
def extraer_con_ia(texto_aviso, url, fuente):
    if not client:
        return None

    system_prompt = """Eres un asistente especializado en extraer información de convocatorias docentes.
El usuario te proporcionará el texto de una convocatoria. Debes devolver ÚNICAMENTE un objeto JSON válido.

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
            return None
            
        datos = json.loads(content)
        datos["url"] = url
        datos["fuente"] = fuente
        return datos
    except Exception as e:
        print(f"  ⚠️ Falló la extracción con IA: {e}")
        return None

# ===== 1. Lógica CGE (RSS) =====
def get_cge_items():
    try:
        r = requests.get(CGE_FEED_URL, headers=HEADERS, timeout=60)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "xml")
        items = soup.find_all("item")

        result = []
        for item in items:
            link = item.link.text.strip() if item.link else ""
            if link:
                result.append(link)
        return result
    except Exception as e:
        print(f"Error al leer el feed CGE: {e}")
        return []

def procesar_cge(link):
    try:
        r = requests.get(link, headers=HEADERS, timeout=60)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        
        text = soup.get_text(separator=" ", strip=True)
        matched = [kw for kw in KEYWORDS if kw in text.lower()]
        if not matched:
            return None

        datos = extraer_con_ia(text, link, "CGE")
        if datos:
            datos["matched"] = matched
            title_tag = soup.select_one("h1.entry-title, h1")
            datos["title"] = title_tag.get_text(strip=True) if title_tag else "Aviso CGE"
            return datos
        return None
    except Exception as e:
        print(f"  ⚠️ Error CGE {link}: {e}")
        return None

# ===== 2. Lógica UADER (Página Web) =====
def get_uader_items():
    """Lee la página de UADER, la divide en bloques por fecha y devuelve los relevantes"""
    try:
        r = requests.get(UADER_URL, headers=HEADERS, timeout=60)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()

        # Obtener todo el texto
        text = soup.get_text(separator="\n", strip=True)
        
        # El texto de UADER empieza con "Llamados a concursos". Buscamos el inicio.
        inicio = text.find("Llamados a concursos")
        if inicio != -1:
            text = text[inicio:]

        # Dividir usando los días de la semana en mayúscula/minúscula como separadores
        # Busca patrones como: Lunes 19 de octubre, MIÉRCOLES 30 DE SEPTIEMBRE, Viernes 9 de noviembre
        # Usamos una expresión regular que busque el inicio de una línea con un día
        patron_dias = r'(?m)^(?:lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)\s+\d{1,2}\s+de\s+[a-zA-ZáéíóúÁÉÍÓÚ]+(?:\s+de\s+\d{4})?'
        
        matches = list(re.finditer(patron_dias, text, re.IGNORECASE))
        bloques = []
        
        for i, match in enumerate(matches):
            inicio_bloque = match.start()
            fin_bloque = matches[i+1].start() if i+1 < len(matches) else len(text)
            bloque_texto = text[inicio_bloque:fin_bloque].strip()
            
            # Filtrar si tiene las palabras clave
            if any(kw in bloque_texto.lower() for kw in KEYWORDS):
                # Para el ID único, usamos un hash del texto (así si cambia, se detecta como nuevo)
                # O el título de la fecha. Como el texto puede cambiar por errores de tipeo,
                # usamos el texto completo como identificador.
                bloques.append(bloque_texto)
                
        return bloques
    except Exception as e:
        print(f"Error al leer UADER: {e}")
        return []

def procesar_uader(bloque_texto):
    """Procesa un bloque individual de UADER con IA"""
    # Creamos un ID único para el bloque (usando la primera línea, ej: "Lunes 19 de octubre")
    lineas = bloque_texto.split('\n')
    titulo = lineas[0] if lineas else "Aviso UADER"
    id_bloque = f"uader_{hash(bloque_texto)}"
    
    datos = extraer_con_ia(bloque_texto, UADER_URL, "Colegio del Uruguay (UADER)")
    if datos:
        # La IA extrae "escuela" pero en UADER siempre es el Colegio
        datos["escuela"] = "Colegio del Uruguay (UADER)"
        datos["title"] = titulo
        datos["id"] = id_bloque
        return datos
    return None

# ===== Main =====
def main():
    seen = load_seen()
    new_matches = []

    # --- 1. Procesar CGE ---
    print(f"Revisando RSS CGE...")
    cge_links = get_cge_items()
    print(f"Se encontraron {len(cge_links)} publicaciones en CGE")
    for link in cge_links:
        if link in seen:
            continue
        seen.add(link)
        print(f"  🔎 CGE: {link[:60]}...")
        datos = procesar_cge(link)
        if datos:
            new_matches.append(datos)
            print(f"    ✅ Encontrado!")
        time.sleep(1)

    # --- 2. Procesar UADER ---
    print(f"\nRevisando página web UADER...")
    uader_bloques = get_uader_items()
    print(f"Se encontraron {len(uader_bloques)} bloques relevantes en UADER")
    for bloque in uader_bloques:
        # Usamos un hash del texto como ID para el archivo seen_urls
        id_bloque = f"uader_{hash(bloque)}"
        if id_bloque in seen:
            continue
        seen.add(id_bloque)
        
        print(f"  🔎 UADER: {bloque.split(chr(10))[0][:40]}...")
        datos = procesar_uader(bloque)
        if datos:
            datos["id"] = id_bloque # Aseguramos el ID
            new_matches.append(datos)
            print(f"    ✅ Encontrado!")
        time.sleep(1)

    save_seen(seen)

    # --- Enviar Correo ---
    if new_matches:
        subject = f"Concursos Docentes — {len(new_matches)} avisos relevantes"
        body_lines = [f"Se encontraron {len(new_matches)} convocatorias relevantes:\n"]

        for i, m in enumerate(new_matches, 1):
            body_lines.append(f"━━━ {i}. {m.get('title', 'Sin título')} ━━━")
            body_lines.append(f"🏛️  Fuente: {m.get('fuente', 'Desconocida')}")
            
            if m.get("escuela") and m.get("fuente") != "CGE":
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
            if m.get("matched"):
                 body_lines.append(f"🔑 Keywords: {', '.join(m.get('matched', []))}")
            body_lines.append("\n")

        body = "\n".join(body_lines)
        send_email(subject, body)
        print(f"\n📧 Notificación enviada, {len(new_matches)} coincidencias.")
    else:
        print("\nSin coincidencias nuevas hoy.")

if __name__ == "__main__":
    main()
