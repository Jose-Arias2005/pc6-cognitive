"""Servicios de integración con Amazon Web Services (DynamoDB y S3) usando boto3."""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

logger = logging.getLogger(__name__)

MAX_IMAGE_SIZE_BYTES = 3 * 1024 * 1024  # 3 MB

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MIME_TO_EXTENSION = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
EXTENSION_TO_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


class CloudServiceError(Exception):
    """Error al comunicarse con los servicios de AWS."""


class ImageValidationError(ValueError):
    """Error de validación de archivo de imagen antes de subirlo a S3."""


def get_aws_config() -> dict[str, str]:
    """Obtiene la configuración de AWS desde variables de entorno."""
    return {
        "region": os.getenv("AWS_REGION", "us-east-1").strip() or "us-east-1",
        "bucket_name": os.getenv("S3_BUCKET_NAME", "").strip(),
        "table_name": os.getenv("DYNAMODB_TABLE", "lab6_usuarios").strip() or "lab6_usuarios",
    }


def _create_boto3_session() -> boto3.Session:
    """Crea una sesión boto3 compatible con credenciales .env o IAM Role en EC2."""
    region = get_aws_config()["region"]
    access_key = os.getenv("AWS_ACCESS_KEY_ID", "").strip()
    secret_key = os.getenv("AWS_SECRET_ACCESS_KEY", "").strip()
    session_token = os.getenv("AWS_SESSION_TOKEN", "").strip()

    session_kwargs: dict[str, str] = {"region_name": region}
    if access_key and secret_key:
        session_kwargs["aws_access_key_id"] = access_key
        session_kwargs["aws_secret_access_key"] = secret_key
        if session_token:
            session_kwargs["aws_session_token"] = session_token

    return boto3.Session(**session_kwargs)


def get_dynamodb_table():
    """Retorna el recurso Table de Amazon DynamoDB configurado."""
    config = get_aws_config()
    session = _create_boto3_session()
    dynamodb = session.resource("dynamodb", region_name=config["region"])
    return dynamodb.Table(config["table_name"])


def get_s3_client():
    """Retorna el cliente de Amazon S3 configurado."""
    config = get_aws_config()
    session = _create_boto3_session()
    return session.client("s3", region_name=config["region"])


def _has_valid_image_signature(data: bytes, extension: str) -> bool:
    """Verifica los magic bytes del archivo para asegurar que sea JPG, PNG o WEBP real."""
    if extension in {".jpg", ".jpeg"}:
        return data.startswith(b"\xff\xd8\xff")
    if extension == ".png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if extension == ".webp":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False


def validate_profile_image(
    filename: str | None,
    content_type: str | None,
    data: bytes,
) -> tuple[str, str]:
    """Valida extensión, tipo MIME, cabecera real y tamaño máximo (3 MB) de la imagen."""
    clean_name = (filename or "").strip()
    if not clean_name:
        raise ImageValidationError("El nombre del archivo de imagen no es válido.")

    _, ext = os.path.splitext(clean_name.lower())
    if ext not in ALLOWED_EXTENSIONS:
        raise ImageValidationError(
            "Formato no permitido. Usa una imagen JPG, PNG o WEBP."
        )

    if not data:
        raise ImageValidationError("El archivo de imagen está vacío.")

    if len(data) > MAX_IMAGE_SIZE_BYTES:
        raise ImageValidationError("La imagen supera el límite máximo de 3 MB.")

    normalized_ct = (content_type or "").split(";")[0].strip().lower()
    if normalized_ct and normalized_ct not in MIME_TO_EXTENSION:
        raise ImageValidationError(
            "Tipo de contenido inválido. Solo se permiten imágenes JPG, PNG o WEBP."
        )

    if not _has_valid_image_signature(data, ext):
        raise ImageValidationError(
            "El archivo no corresponde a una imagen JPG, PNG o WEBP válida."
        )

    canonical_ext = ".jpg" if ext == ".jpeg" else ext
    canonical_mime = EXTENSION_TO_MIME[canonical_ext]
    return canonical_ext, canonical_mime


def build_s3_public_url(bucket_name: str, region: str, key: str) -> str:
    """Construye la URL pública en formato virtual-hosted de Amazon S3."""
    clean_key = key.lstrip("/")
    return f"https://{bucket_name}.s3.{region}.amazonaws.com/{clean_key}"


def extract_s3_key_from_url(url: str | None, bucket_name: str) -> str | None:
    """Extrae la clave del objeto S3 a partir de su URL pública cuando es necesario."""
    if not url:
        return None
    parsed = urlparse(url)
    path = parsed.path.lstrip("/")
    if not path:
        return None
    if parsed.netloc.startswith(f"{bucket_name}.s3"):
        return path
    if path.startswith(f"{bucket_name}/"):
        return path[len(bucket_name) + 1 :]
    return path if "avatars/" in path else None


def _format_aws_error(exc: Exception, action: str) -> CloudServiceError:
    """Convierte excepciones de boto3 en errores claros sin exponer secretos."""
    if isinstance(exc, NoCredentialsError):
        return CloudServiceError(
            f"No se encontraron credenciales de AWS para {action}. "
            "Configura .env o asigna un IAM Role a la instancia EC2."
        )
    if isinstance(exc, ClientError):
        error_info = exc.response.get("Error", {})
        code = error_info.get("Code", "ClientError")
        message = error_info.get("Message", str(exc))
        logger.error("Error AWS (%s) al %s: %s", code, action, message)
        return CloudServiceError(f"Error de AWS ({code}) al {action}: {message}")
    logger.error("Error inesperado de AWS al %s: %s", action, exc)
    return CloudServiceError(f"Error de conexión con AWS al {action}.")


def upload_avatar_to_s3(
    file_bytes: bytes,
    extension: str,
    content_type: str,
) -> tuple[str, str]:
    """Sube una imagen de perfil a S3 bajo avatars/ con UUID único y retorna (key, public_url)."""
    config = get_aws_config()
    bucket_name = config["bucket_name"]
    region = config["region"]

    if not bucket_name:
        raise CloudServiceError(
            "La variable de entorno S3_BUCKET_NAME no está configurada."
        )

    object_key = f"avatars/{uuid.uuid4().hex}{extension}"
    s3 = get_s3_client()

    try:
        try:
            s3.put_object(
                Bucket=bucket_name,
                Key=object_key,
                Body=file_bytes,
                ContentType=content_type,
                ACL="public-read",
            )
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            # Los buckets modernos suelen deshabilitar ACLs o bloquear ACLs públicas en favor de Bucket Policy.
            if error_code in {
                "AccessControlListNotSupported",
                "InvalidRequest",
                "AccessDenied",
            }:
                s3.put_object(
                    Bucket=bucket_name,
                    Key=object_key,
                    Body=file_bytes,
                    ContentType=content_type,
                )
            else:
                raise
    except (ClientError, NoCredentialsError, BotoCoreError) as exc:
        raise _format_aws_error(exc, "subir la imagen a S3") from exc

    public_url = build_s3_public_url(bucket_name, region, object_key)
    return object_key, public_url


def delete_avatar_from_s3(
    avatar_key: str | None = None,
    avatar_url: str | None = None,
) -> None:
    """Elimina una imagen de perfil del bucket S3 si existe una clave asociada."""
    config = get_aws_config()
    bucket_name = config["bucket_name"]
    if not bucket_name:
        return

    key = (avatar_key or "").strip() or extract_s3_key_from_url(avatar_url, bucket_name)
    if not key:
        return

    s3 = get_s3_client()
    try:
        s3.delete_object(Bucket=bucket_name, Key=key)
    except (ClientError, NoCredentialsError, BotoCoreError) as exc:
        raise _format_aws_error(exc, "eliminar la imagen anterior en S3") from exc


def list_users(search_query: str = "") -> list[dict[str, Any]]:
    """Lista los usuarios desde DynamoDB con filtro opcional por nombre o correo."""
    table = get_dynamodb_table()
    items: list[dict[str, Any]] = []

    try:
        response = table.scan()
        items.extend(response.get("Items", []))
        while "LastEvaluatedKey" in response:
            response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
            items.extend(response.get("Items", []))
    except (ClientError, NoCredentialsError, BotoCoreError) as exc:
        raise _format_aws_error(exc, "consultar usuarios en DynamoDB") from exc

    query = search_query.strip().lower()
    if query:
        items = [
            user
            for user in items
            if query in str(user.get("full_name", "")).lower()
            or query in str(user.get("email", "")).lower()
        ]

    items.sort(key=lambda u: str(u.get("created_at", "")), reverse=True)
    return items


def get_user_by_id(user_id: str) -> dict[str, Any] | None:
    """Obtiene un usuario específico por su llave primaria user_id."""
    table = get_dynamodb_table()
    try:
        response = table.get_item(Key={"user_id": user_id})
    except (ClientError, NoCredentialsError, BotoCoreError) as exc:
        raise _format_aws_error(exc, "obtener el usuario en DynamoDB") from exc
    return response.get("Item")


def create_user(
    full_name: str,
    email: str,
    role: str,
    status: str,
    avatar_url: str = "",
    avatar_key: str = "",
) -> dict[str, Any]:
    """Crea un nuevo registro de usuario en DynamoDB con user_id UUID."""
    table = get_dynamodb_table()
    now_iso = datetime.now(timezone.utc).isoformat()
    item: dict[str, Any] = {
        "user_id": str(uuid.uuid4()),
        "full_name": full_name,
        "email": email,
        "role": role,
        "status": status,
        "avatar_url": avatar_url,
        "avatar_key": avatar_key,
        "created_at": now_iso,
        "updated_at": now_iso,
    }

    try:
        table.put_item(Item=item)
    except (ClientError, NoCredentialsError, BotoCoreError) as exc:
        raise _format_aws_error(exc, "crear el usuario en DynamoDB") from exc

    return item


def update_user(
    user_id: str,
    full_name: str,
    email: str,
    role: str,
    status: str,
    avatar_url: str,
    avatar_key: str,
) -> dict[str, Any]:
    """Actualiza los datos de un usuario existente en DynamoDB."""
    table = get_dynamodb_table()
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        response = table.update_item(
            Key={"user_id": user_id},
            UpdateExpression=(
                "SET #full_name = :full_name, "
                "#email = :email, "
                "#role = :role, "
                "#status = :status, "
                "#avatar_url = :avatar_url, "
                "#avatar_key = :avatar_key, "
                "#updated_at = :updated_at"
            ),
            ConditionExpression="attribute_exists(user_id)",
            ExpressionAttributeNames={
                "#full_name": "full_name",
                "#email": "email",
                "#role": "role",
                "#status": "status",
                "#avatar_url": "avatar_url",
                "#avatar_key": "avatar_key",
                "#updated_at": "updated_at",
            },
            ExpressionAttributeValues={
                ":full_name": full_name,
                ":email": email,
                ":role": role,
                ":status": status,
                ":avatar_url": avatar_url,
                ":avatar_key": avatar_key,
                ":updated_at": now_iso,
            },
            ReturnValues="ALL_NEW",
        )
    except (ClientError, NoCredentialsError, BotoCoreError) as exc:
        raise _format_aws_error(exc, "actualizar el usuario en DynamoDB") from exc

    return response.get("Attributes", {})


def delete_user(user_id: str) -> dict[str, Any] | None:
    """Elimina un usuario de DynamoDB y retorna sus atributos previos si existía."""
    table = get_dynamodb_table()
    try:
        response = table.delete_item(
            Key={"user_id": user_id},
            ReturnValues="ALL_OLD",
        )
    except (ClientError, NoCredentialsError, BotoCoreError) as exc:
        raise _format_aws_error(exc, "eliminar el usuario en DynamoDB") from exc

    return response.get("Attributes")
