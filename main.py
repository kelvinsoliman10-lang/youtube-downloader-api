
# Instrucciones de instalación:
# 1. Instala Python 3.8+ si no lo tienes.
# 2. Crea un entorno virtual (recomendado):
#    python -m venv venv
#    source venv/bin/activate  # En Windows: venv\Scripts\activate
# 3. Instala las dependencias:
#    pip install "fastapi[all]" uvicorn yt-dlp boto3
# 4. Instala ffmpeg:
#    - Windows: Descarga desde https://ffmpeg.org/download.html y añade el directorio 'bin' a tu PATH.
#    - macOS (usando Homebrew): brew install ffmpeg
#    - Linux (usando apt): sudo apt update && sudo apt install ffmpeg
# 5. Configura las variables de entorno (puedes usar un archivo .env con python-dotenv):
#    export API_KEY="tu_clave_secreta"
#    export AWS_ACCESS_KEY_ID="tu_access_key"
#    export AWS_SECRET_ACCESS_KEY="tu_secret_key"
#    export AWS_REGION="tu_region"
#    export S3_BUCKET="tu_nombre_de_bucket"
# 6. Ejecuta la aplicación:
#    uvicorn main:app --host 0.0.0.0 --port 8000

import os
import uuid
import logging
from contextlib import asynccontextmanager

import boto3
from botocore.exceptions import NoCredentialsError, PartialCredentialsError, ClientError
from fastapi import FastAPI, Request, HTTPException, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, HttpUrl
import uvicorn
import yt_dlp

# --- Configuración de Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Carga de configuración desde variables de entorno ---
API_KEY = os.getenv("API_KEY")
S3_BUCKET = os.getenv("S3_BUCKET")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.getenv("AWS_REGION")

# --- Validación de configuración ---
if not API_KEY:
    raise ValueError("La variable de entorno API_KEY no está configurada.")
if not all([S3_BUCKET, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION]):
    logger.warning("Faltan una o más variables de entorno de AWS. La subida a S3 no funcionará.")

# --- Modelos de Pydantic para validación de datos ---
class DownloadRequest(BaseModel):
    url: HttpUrl = Field(..., description="URL del video de YouTube a descargar.")
    format: str = Field("mp4", description="Formato de salida del archivo.")
    to_s3: bool = Field(True, description="Indica si se debe subir el archivo a S3.")

# --- Seguridad con API Key ---
api_key_header = APIKeyHeader(name="Authorization", auto_error=True)

async def get_api_key(key: str = Security(api_key_header)):
    """
    Valida que el API Key enviado en el header 'Authorization' sea correcto.
    Se espera el formato 'Bearer <clave>'.
    """
    if not key.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Formato de 'Authorization' header inválido. Usar 'Bearer <clave>'.",
        )
    token = key.split(" ")[1]
    if token != API_KEY:
        raise HTTPException(status_code=401, detail="API Key inválido o expirado.")
    return token

# --- Ciclo de vida de la aplicación para crear un directorio temporal ---
TEMP_DIR = "temp_downloads"

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Al iniciar, crea el directorio temporal si no existe
    os.makedirs(TEMP_DIR, exist_ok=True)
    logger.info(f"Directorio temporal '{TEMP_DIR}' asegurado.")
    yield
    # Al finalizar, se podrían limpiar archivos residuales si es necesario,
    # aunque la lógica actual limpia por cada request.

app = FastAPI(
    title="YouTube Downloader API",
    description="Una API para descargar videos de YouTube y subirlos a S3.",
    version="1.0.0",
    lifespan=lifespan
)

# --- Endpoint de descarga ---
@app.post("/download", dependencies=[Security(get_api_key)])
async def download_video(request: DownloadRequest):
    """
    Descarga un video de YouTube, opcionalmente lo sube a S3 y devuelve una URL
    prefirmada o la ruta local del archivo.
    """
    unique_id = uuid.uuid4()
    output_filename = f"{unique_id}.{request.format}"
    output_path = os.path.join(TEMP_DIR, output_filename)
    
    # Opciones para yt-dlp
    # 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
    # 1. Intenta obtener el mejor video en MP4 y el mejor audio en M4A y fusionarlos.
    # 2. Si no es posible, obtiene el mejor formato pre-fusionado en MP4.
    # 3. Como último recurso, obtiene el mejor formato disponible.
    ydl_opts = {
        'format': f'bestvideo[ext={request.format}]+bestaudio/best',
        'outtmpl': output_path,
        'merge_output_format': request.format,
        'postprocessors': [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': request.format,
        }],
        'logger': logger,
        'progress_hooks': [], # Se puede usar para monitorear el progreso
        'noplaylist': True, # Evita descargar listas de reproducción completas
    }

    logger.info(f"Iniciando descarga para URL: {request.url} con ID: {unique_id}")

    try:
        # Ejecutar la descarga de yt-dlp
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([str(request.url)])
        
        logger.info(f"Descarga completada. Archivo guardado en: {output_path}")

        if not os.path.exists(output_path):
             raise HTTPException(status_code=500, detail="El archivo no se generó después de la descarga.")

        if request.to_s3:
            if not all([S3_BUCKET, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION]):
                 raise HTTPException(status_code=501, detail="El servidor no está configurado para subir a S3.")
            
            try:
                s3_client = boto3.client(
                    's3',
                    aws_access_key_id=AWS_ACCESS_KEY_ID,
                    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
                    region_name=AWS_REGION
                )
                
                logger.info(f"Subiendo '{output_filename}' al bucket '{S3_BUCKET}'...")
                s3_client.upload_file(output_path, S3_BUCKET, output_filename)
                
                logger.info("Generando URL prefirmada...")
                presigned_url = s3_client.generate_presigned_url(
                    'get_object',
                    Params={'Bucket': S3_BUCKET, 'Key': output_filename},
                    ExpiresIn=3600  # 1 hora
                )
                
                return {"file_url": presigned_url}

            except (NoCredentialsError, PartialCredentialsError):
                logger.error("Credenciales de AWS no encontradas.")
                raise HTTPException(status_code=500, detail="Error de configuración de credenciales de AWS.")
            except ClientError as e:
                logger.error(f"Error de cliente de S3: {e}")
                raise HTTPException(status_code=500, detail=f"Error al interactuar con S3: {e}")
        
        else:
            # Devolver la ruta local si no se sube a S3
            return {
                "message": "Archivo descargado localmente.",
                "filename": output_filename,
                "local_path": os.path.abspath(output_path)
            }

    except yt_dlp.utils.DownloadError as e:
        logger.error(f"Error de yt-dlp: {e}")
        raise HTTPException(status_code=400, detail=f"No se pudo descargar el video. Causa: {str(e)}")
    except Exception as e:
        logger.error(f"Un error inesperado ocurrió: {e}")
        raise HTTPException(status_code=500, detail=f"Error interno del servidor: {str(e)}")
    finally:
        # Limpieza del archivo temporal
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
                logger.info(f"Archivo temporal '{output_path}' eliminado.")
            except OSError as e:
                logger.error(f"Error al eliminar el archivo temporal '{output_path}': {e}")

# --- Bloque para ejecución directa con uvicorn ---
if __name__ == "__main__":
    """
    Para ejecutar la aplicación, usa el comando:
    uvicorn main:app --reload
    
    El flag --reload reiniciará el servidor automáticamente al detectar cambios en el código.
    """
    uvicorn.run(app, host="0.0.0.0", port=8000)
