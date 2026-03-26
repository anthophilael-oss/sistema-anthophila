import os
import sys
import asyncio
import csv
import json
from datetime import datetime, timedelta
import mimetypes
from groq import Groq
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from google.oauth2.credentials import Credentials as UserCredentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from flask import Flask
from threading import Thread

# --- CONFIGURACIÓN ---
load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "telegram").lstrip("/")
WEBHOOK_SECRET_TOKEN = os.getenv("WEBHOOK_SECRET_TOKEN")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))

ANTHOPHILA_SHEET_ID = "1SjkwegVlaMUk7tRcn5CmoGonAtqE_lVenndsNwMmqoU"
DRIVE_FOLDER_AUDIOS_LECTURA_ID = "121QcJ1-7uJ8ZzkW65S8nsio1zehlo6Vo"
DRIVE_FOLDER_FOTOS_ESCRITURA_ID = "1EZvWbE8MtZffdFlZpINHaH0EjRu1cxSL"
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "/tmp/credentials.json")
GOOGLE_TOKEN_PATH = os.getenv("GOOGLE_TOKEN_PATH", "/tmp/token.json")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
GOOGLE_TOKEN_JSON = os.getenv("GOOGLE_TOKEN_JSON")

_groq_client = None

def get_groq_client():
    global _groq_client
    if _groq_client is not None:
        return _groq_client
    if not GROQ_API_KEY:
        raise RuntimeError("Falta GROQ_API_KEY en variables de entorno")
    _groq_client = Groq(api_key=GROQ_API_KEY)
    return _groq_client

def _ensure_google_files():
    os.makedirs(os.path.dirname(GOOGLE_CREDENTIALS_PATH) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(GOOGLE_TOKEN_PATH) or ".", exist_ok=True)
    if GOOGLE_CREDENTIALS_JSON and not os.path.isfile(GOOGLE_CREDENTIALS_PATH):
        with open(GOOGLE_CREDENTIALS_PATH, "w", encoding="utf-8") as f:
            f.write(GOOGLE_CREDENTIALS_JSON)
    if GOOGLE_TOKEN_JSON and not os.path.isfile(GOOGLE_TOKEN_PATH):
        with open(GOOGLE_TOKEN_PATH, "w", encoding="utf-8") as f:
            f.write(GOOGLE_TOKEN_JSON)

def get_google_credentials(credentials_path=GOOGLE_CREDENTIALS_PATH):
    _ensure_google_files()
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    try:
        with open(credentials_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[google_credentials] error abriendo {credentials_path}: {repr(e)}", file=sys.stderr)
        raise

    if data.get("type") == "service_account":
        return Credentials.from_service_account_file(credentials_path, scopes=scopes)

    client_config = data.get("installed") or data.get("web")
    if client_config:
        creds = None
        if os.path.isfile(GOOGLE_TOKEN_PATH):
            creds = UserCredentials.from_authorized_user_file(GOOGLE_TOKEN_PATH, scopes=scopes)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_config(data, scopes=scopes)
                creds = flow.run_local_server(port=0)
            with open(GOOGLE_TOKEN_PATH, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
        return creds

    raise RuntimeError("credentials.json no es service_account ni OAuth client (installed/web)")

def _find_first_file(search_dirs, extensions):
    wanted = {ext.lower() for ext in extensions}
    for base_dir in search_dirs:
        if not base_dir or not os.path.isdir(base_dir):
            continue
        for root, _, files in os.walk(base_dir):
            for filename in files:
                _, ext = os.path.splitext(filename)
                if ext.lower() in wanted:
                    return os.path.join(root, filename)
    return None

def registrar_en_anthophila(alumno, pestana, archivo_local=None, transcripcion=None):
    pestana_norm = (pestana or "").strip().lower()
    if pestana_norm not in {"escritura", "lectura"}:
        raise ValueError("pestana debe ser 'Escritura' o 'Lectura'")

    if archivo_local is None:
        alumno_dir = os.path.join("/tmp", "EXPEDIENTES", alumno)
        if pestana_norm == "escritura":
            archivo_local = _find_first_file(
                search_dirs=[os.path.join(alumno_dir, "ESCRITURA"), "EXPEDIENTES", "."],
                extensions=[".jpg", ".jpeg", ".png"],
            )
        else:
            archivo_local = _find_first_file(
                search_dirs=[os.path.join(alumno_dir, "LECTURA"), "EXPEDIENTES", "."],
                extensions=[".ogg", ".mp3", ".wav", ".m4a"],
            )

    if not archivo_local or not os.path.isfile(archivo_local):
        raise FileNotFoundError("No se encontró archivo_local de prueba para subir")

    folder_id = DRIVE_FOLDER_FOTOS_ESCRITURA_ID if pestana_norm == "escritura" else DRIVE_FOLDER_AUDIOS_LECTURA_ID
    tipo = "FOTO" if pestana_norm == "escritura" else "AUDIO"

    creds = get_google_credentials()
    drive = build("drive", "v3", credentials=creds)
    gc = gspread.authorize(creds)

    mime, _ = mimetypes.guess_type(archivo_local)
    if not mime: mime = "application/octet-stream"

    file_metadata = {"name": os.path.basename(archivo_local), "parents": [folder_id]}
    media = MediaFileUpload(archivo_local, mimetype=mime, resumable=False)
    
    uploaded = drive.files().create(
        body=file_metadata, media_body=media, fields="id, webViewLink", supportsAllDrives=True
    ).execute()

    sh = gc.open_by_key(ANTHOPHILA_SHEET_ID)
    ws = sh.worksheet("Escritura" if pestana_norm == "escritura" else "Lectura")

    ahora = datetime.utcnow() + timedelta(hours=-5)
    row = [
        ahora.strftime("%d/%m/%Y"), ahora.strftime("%H:%M:%S"),
        alumno, tipo, os.path.basename(archivo_local),
        uploaded.get("id"), uploaded.get("webViewLink"), transcripcion or ""
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")

    return {
        "archivo_local": archivo_local,
        "drive_file_id": uploaded.get("id"),
        "drive_link": uploaded.get("webViewLink"),
        "sheet_tab": ws.title,
    }

# --- BASE DE DATOS Y UTILIDADES ---
USUARIOS_AUTORIZADOS = {
    8122112934: {"nombre": "Gabriel", "funciones": ["CHAT", "LECTURA", "ESCRITURA"]},
    8619941263: {"nombre": "Paolo", "funciones": ["LECTURA", "ESCRITURA"]},
    8745176048: {"nombre": "Anthophila", "funciones": ["LECTURA", "ESCRITURA"]}, 
}

def log_datos(archivo, columnas, datos):
    file_exists = os.path.isfile(archivo)
    with open(archivo, mode="a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        if not file_exists: writer.writerow(columnas)
        writer.writerow(datos)

# --- MANEJADORES ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in USUARIOS_AUTORIZADOS: return
    nombre = USUARIOS_AUTORIZADOS[uid]["nombre"]
    user_path = os.path.join("/tmp/EXPEDIENTES", nombre)
    for d in ["LECTURA", "ESCRITURA", "WHATSAPP"]:
        os.makedirs(os.path.join(user_path, d), exist_ok=True)
    botones = [["📖 Opción 2: Lectura de 1 minuto"], ["📷 Opción 3: Foto de Escritura"]]
    await update.message.reply_text(f"¡Hola {nombre}! Elige una opción:", reply_markup=ReplyKeyboardMarkup(botones, resize_keyboard=True))

async def handle_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = msg.from_user.id
    if uid not in USUARIOS_AUTORIZADOS: return
    nombre = USUARIOS_AUTORIZADOS[uid]["nombre"]
    user_path = os.path.join("/tmp","EXPEDIENTES", nombre)
    text = msg.text or ""

    if "Opción 2" in text:
        context.user_data["modo"] = "LECTURA"
        return await msg.reply_text(f"🎤 Modo Lectura activo, {nombre}. Envía tu audio.")
    if "Opción 3" in text:
        context.user_data["modo"] = "ESCRITURA"
        return await msg.reply_text(f"📷 Modo Escritura activo, {nombre}. Envía la foto.")

    modo = context.user_data.get("modo")
    if msg.voice and modo == "LECTURA":
        wait = await msg.reply_text("Procesando lectura... 🎧")
        file = await context.bot.get_file(msg.voice.file_id)
        fpath = os.path.join(user_path, "LECTURA", f"{datetime.now().strftime('%Y%m%d_%H%M')}_{nombre}.ogg")
        await file.download_to_drive(fpath)
        with open(fpath, "rb") as f:
            ts = get_groq_client().audio.transcriptions.create(file=(fpath, f.read()), model="whisper-large-v3-turbo", language="es")
        drive_ok = True
        try:
            await asyncio.to_thread(registrar_en_anthophila, nombre, "Lectura", fpath, ts.text)
        except Exception: drive_ok = False
        await wait.delete()
        await msg.reply_text(f"✅ Lectura registrada. Meta cumplida.")
        context.user_data["modo"] = None

    elif (msg.photo or msg.document) and modo == "ESCRITURA":
        wait = await msg.reply_text("Guardando imagen... 📷")
        file = await (msg.photo[-1].get_file() if msg.photo else msg.document.get_file())
        fpath = os.path.join(user_path, "ESCRITURA", f"{datetime.now().strftime('%Y%m%d_%H%M')}_{nombre}.jpg")
        await file.download_to_drive(fpath)
        drive_ok = True
        try:
            await asyncio.to_thread(registrar_en_anthophila, nombre, "Escritura", fpath, "")
        except Exception: drive_ok = False
        await wait.delete()
        await msg.reply_text(f"✅ Foto recibida. ¡Meta cumplida!")
        context.user_data["modo"] = None

# --- INTEGRACIÓN RENDER ---
flask_app = Flask('')
@flask_app.route('/')
def home(): return "Servidor Anthophila activo."

def run_flask():
    flask_app.run(host='0.0.0.0', port=PORT)

if __name__ == "__main__":
    if "--upload-test" in sys.argv:
        # Lógica de test original
        alumno = "Paolo"
        pestana = "Escritura"
        resultado = registrar_en_anthophila(alumno, pestana)
        print(json.dumps(resultado, indent=2))
        sys.exit(0)

    if not TELEGRAM_TOKEN:
        raise RuntimeError("Falta TELEGRAM_TOKEN")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.ALL, handle_all))

    if WEBHOOK_URL:
        webhook_url = f"{WEBHOOK_URL.rstrip('/')}/{WEBHOOK_PATH}"
        app.run_webhook(
            listen=HOST, port=PORT, url_path=WEBHOOK_PATH,
            webhook_url=webhook_url, secret_token=WEBHOOK_SECRET_TOKEN,
            stop_signals=None # Evita error de loop cerrado
        )
    else:
        thread = Thread(target=run_flask)
        thread.start()
        app.run_polling(stop_signals=None)