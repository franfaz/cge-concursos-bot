import os
import json
import base64
import time
import re
import requests
import hashlib
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
DIAS_MAX = 15 # Ignorar avisos de más de 15 días

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

MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12
}

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
        return None
    creds_data = json.loads(GMAIL_TOKEN_JSON)
    creds = Credentials.from_authorized_user_info(creds_data)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build('gmail', 'v1', credentials=creds)

def send_email(subject, body):
    if not GMAIL_RECIPIENT:
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

# ===== IA =====
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

# ===== Lógica UADER (Mejorada) =====
def es_fecha_reciente_uader(texto_fecha):
    """Verifica si la fecha del texto (ej: 'Lunes 19 de octubre') es reciente"""
    try:
        # Buscar día y mes en el texto
        match = re.search(r'(\d{1,2})\s+de\s+([a-zA-ZáéíóúÁÉÍÓÚ]+)', texto_fecha, re.IGNORECASE)
        if not match:
            return False
            
        dia = int(match.group(1))
        mes_str = match.group(2).lower()
        
        if mes_str not in MESES:
            return False
            
        mes = MESES[mes_str]
        anio_actual = datetime.now().year
        
        fecha_aviso = datetime(anio_actual, mes, dia)
        hoy = datetime.now()
        
        # Si la fecha es mayor a hoy por más de 1 mes, asumimos que es del año pasado
        if fecha_aviso > hoy + timedelta(days=30):
            fecha_aviso = datetime(anio_actual - 1, mes, dia)
            
        diferencia = hoy - fecha_aviso
        return diferencia.days <= DIAS_MAX and diferencia.days >= -5 # Permitir hasta 5 días a futuro
    except:
        return False

def get_uader_items():
    """Lee la página de UADER, filtra por fecha y separa por materia/horario"""
    try:
        r = requests.get(UADER_URL, headers=HEADERS, timeout=60)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()

        text = soup.get_text(separator="\n", strip=True)
        
        inicio = text.find("Llamados a concursos")
        if inicio != -1:
            text = text[inicio:]

        # 1. Encontrar todas las fechas
        patron_fecha = r'(?m)^(?:lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)\s+\d{1,2}\s+de\s+[a-zA-ZáéíóúÁÉÍÓÚ]+(?:\s+de\s+\d{4})?'
        matches_fechas = list(re.finditer(patron_fecha, text, re.IGNORECASE))
        
        bloques = []
        
        for i, match in enumerate(matches_fechas):
            inicio_bloque = match.start()
            fin_bloque = matches_fechas[i+1].start() if i+1 < len(matches_fechas) else len(text)
            
            texto_fecha = match.group(0).strip()
            
            # Si la fecha es vieja, la ignoramos por completo
            if not es_fecha_reciente_uader(texto_fecha):
                continue
                
            contenido_fecha = text[inicio_bloque:fin_bloque].strip()
            
            # 2. Separar el bloque por horarios (ej: "08:30 hs –", "09:15 hs -")
            patron_horario = r'(?m)^\d{1,2}[:.,]\d{2}\s*(?:hs|HS|hs\.)?\s*[–-]\s*'
            matches_horarios = list(re.finditer(patron_horario, contenido_fecha))
            
            if not matches_horarios:
                # Si no hay horarios, procesamos todo el bloque de la fecha
                if any(kw in contenido_fecha.lower() for kw in KEYWORDS):
                    bloques.append(contenido_fecha)
            else:
                # Si hay horarios, separamos por cada horario
                for j, match_hora in enumerate(matches_horarios):
                    inicio_hora = match_hora.start()
                    fin_hora = matches_horarios[j+1].start() if j+1 < len(matches_horarios) else len(contenido_fecha)
                    
                    bloque_materia = contenido_fecha[inicio_hora:fin_hora].strip()
                    
                    # Verificar si este bloque específico tiene nuestras palabras clave
                    if any(kw in bloque_materia.lower() for kw in KEYWORDS):
                        # Le agregamos la fecha arriba para que la IA sepa de qué día es
                        bloque_completo = f"{texto_fecha}\n{bloque_materia}"
                        bloques.append(bloque_completo)
                        
        return bloques
    except Exception as e:
        print(f"Error al leer UADER: {e}")
        return []

def procesar_uader(bloque_texto):
    # Normalizamos el texto (quitamos espacios extra) para que el MD5 sea 100% estable
    texto_normalizado = re.sub(r'\s+', ' ', bloque_texto).strip()
    id_bloque = "uader_" + hashlib.md5(texto_normalizado.encode('utf-8')).hexdigest()
    
    lineas = bloque_texto.split('\n')
    titulo = lineas[0] if lineas else "Aviso UADER"
    
    datos = extraer_con_ia(bloque_texto, UADER_URL, "Colegio del Uruguay (UADER)")
    if datos:
        datos["escuela"] = "Colegio del Uruguay (UADER)"
        datos["title"] = titulo
        datos["id"] = id_bloque
        return datos
    return None

# ===== Lógica CGE =====
def get_cge_items():
    try:
        r = requests.get(CGE_FEED_URL, headers=HEADERS, timeout=60)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "xml")
        return [item.link.text.strip() for item in soup.find_all("item") if item.link]
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
        return None

# ===== Main =====
def main():
    seen = load_seen()
    new_matches = []

    # --- CGE ---
    print("Revisando CGE...")
    for link in get_cge_items():
        if link in seen: continue
        seen.add(link)
        print(f"  🔎 CGE: {link[:50]}...")
        datos = procesar_cge(link)
        if datos:
            new_matches.append(datos)
            print("    ✅ Encontrado!")
        time.sleep(1)

    # --- UADER ---
    print("\nRevisando UADER...")
    for bloque in get_uader_items():
        # Calculamos el ID antes de ver si lo ignoramos
        texto_normalizado = re.sub(r'\s+', ' ', bloque).strip()
        id_bloque = "uader_" + hashlib.md5(texto_normalizado.encode('utf-8')).hexdigest()
        
        if id_bloque in seen:
            continue
            
        seen.add(id_bloque)
        print(f"  🔎 UADER: {bloque.split(chr(10))[0][:40]}...")
        datos = procesar_uader(bloque)
        if datos:
            new_matches.append(datos)
            print("    ✅ Encontrado!")
        time.sleep(1)

    save_seen(seen)

    # --- Correo ---
    if new_matches:
        subject = f"Concursos Docentes — {len(new_matches)} avisos relevantes"
        body_lines = [f"Se encontraron {len(new_matches)} convocatorias relevantes:\n"]

        for i, m in enumerate(new_matches, 1):
            body_lines.append(f"━━━ {i}. {m.get('title', 'Sin título')} ━━━")
            body_lines.append(f"🏛️  Fuente: {m.get('fuente', 'Desconocida')}")
            
            if m.get("escuela") and m.get("fuente") != "CGE":
                body_lines.append(f"🏫 Escuela: {m['escuela']}")
            if m.get("cargo"): body_lines.append(f"📚 Cargo: {m['cargo']}")
            if m.get("horas"): body_lines.append(f"⏱️  Horas: {m['horas']}")
            if m.get("caracter"): body_lines.append(f"👤 Carácter: {m['caracter']}")
            if m.get("fecha_concurso"): body_lines.append(f"📅 Fecha: {m['fecha_concurso']}")
            if m.get("hora_concurso"): body_lines.append(f"🕐 Hora: {m['hora_concurso']}")
            if m.get("dias_horario"): body_lines.append(f"🗓️  Días y horario: {m['dias_horario']}")
            if m.get("lugar"): body_lines.append(f"📍 Lugar: {m['lugar']}")

            body_lines.append(f"🔗 Enlace: {m['url']}")
            if m.get("matched"): body_lines.append(f"🔑 Keywords: {', '.join(m.get('matched', []))}")
            body_lines.append("\n")

        body = "\n".join(body_lines)
        send_email(subject, body)
        print(f"\n📧 Notificación enviada, {len(new_matches)} coincidencias.")
    else:
        print("\nSin coincidencias nuevas hoy.")

if __name__ == "__main__":
    main()
