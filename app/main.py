"""Aplicación FastAPI para la gestión de usuarios con Amazon DynamoDB y S3."""

from __future__ import annotations

import logging
import os
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import aws_service

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

ALLOWED_ROLES = ("Admin", "Editor", "Visualizador")
ALLOWED_STATUSES = ("Activo", "Inactivo")
EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def _load_secret_key() -> str:
    """Obtiene SECRET_KEY desde entorno o genera una clave efímera para desarrollo."""
    secret = os.getenv("SECRET_KEY", "").strip()
    if not secret:
        logger.warning(
            "SECRET_KEY no configurada en el entorno; usando clave temporal en memoria."
        )
        return secrets.token_urlsafe(32)
    return secret


def _format_datetime(value: str | None) -> str:
    """Formatea una fecha ISO 8601 UTC para mostrarla de forma legible en la tabla."""
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.strftime("%d/%m/%Y %H:%M UTC")
    except ValueError:
        return str(value)


def _get_initials(full_name: str | None) -> str:
    """Genera las iniciales (1-2 letras) para avatares sin imagen."""
    parts = [part for part in (full_name or "").strip().split() if part]
    if not parts:
        return "U"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return f"{parts[0][0]}{parts[-1][0]}".upper()


app = FastAPI(
    title="CloudAdmin · Gestión de Usuarios",
    description="Panel administrativo con FastAPI, Amazon DynamoDB y Amazon S3.",
    version="1.0.0",
)

app.add_middleware(
    SessionMiddleware,
    secret_key=_load_secret_key(),
    session_cookie="cloud_admin_session",
    max_age=60 * 60 * 8,
    same_site="lax",
    https_only=os.getenv("SESSION_HTTPS_ONLY", "false").lower() == "true",
)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["format_dt"] = _format_datetime
templates.env.filters["initials"] = _get_initials


def _get_current_admin(request: Request) -> str | None:
    """Retorna el nombre del administrador autenticado o None si no hay sesión."""
    admin = request.session.get("admin_user")
    return str(admin) if admin else None


def _require_admin(request: Request) -> RedirectResponse | None:
    """Redirige a /login si no existe una sesión administrativa activa."""
    if not _get_current_admin(request):
        return RedirectResponse(url="/login", status_code=303)
    return None


def _set_flash(request: Request, category: str, message: str) -> None:
    """Guarda una notificación temporal en la cookie de sesión firmada."""
    request.session["flash"] = {"category": category, "message": message}


def _pop_flash(request: Request) -> dict[str, str] | None:
    """Extrae y elimina la notificación temporal de la sesión."""
    flash = request.session.pop("flash", None)
    return flash if isinstance(flash, dict) else None


def _validate_user_input(
    full_name: str,
    email: str,
    role: str,
    status: str,
) -> tuple[str, str, str, str]:
    """Normaliza y valida los campos de usuario antes de guardar en DynamoDB."""
    clean_name = " ".join(full_name.strip().split())
    clean_email = email.strip().lower()
    clean_role = role.strip()
    clean_status = status.strip()

    if not clean_name or len(clean_name) > 120:
        raise ValueError("El nombre completo es obligatorio (máximo 120 caracteres).")
    if not clean_email or len(clean_email) > 180 or not EMAIL_REGEX.match(clean_email):
        raise ValueError("Ingresa un correo electrónico válido.")
    if clean_role not in ALLOWED_ROLES:
        raise ValueError("Selecciona un rol válido (Admin, Editor o Visualizador).")
    if clean_status not in ALLOWED_STATUSES:
        raise ValueError("Selecciona un estado válido (Activo o Inactivo).")

    return clean_name, clean_email, clean_role, clean_status


async def _read_validated_avatar(
    avatar: UploadFile | None,
) -> tuple[bytes, str, str] | None:
    """Lee hasta 3 MB + 1 byte y valida extensión, MIME y cabecera de la imagen."""
    if avatar is None or not avatar.filename or not avatar.filename.strip():
        return None

    try:
        file_bytes = await avatar.read(aws_service.MAX_IMAGE_SIZE_BYTES + 1)
    finally:
        await avatar.close()

    ext, mime = aws_service.validate_profile_image(
        filename=avatar.filename,
        content_type=avatar.content_type,
        data=file_bytes,
    )
    return file_bytes, ext, mime


def _compute_stats(users: list[dict[str, Any]]) -> dict[str, int]:
    """Calcula métricas rápidas para la cabecera del panel."""
    return {
        "total": len(users),
        "active": sum(1 for u in users if u.get("status") == "Activo"),
        "admins": sum(1 for u in users if u.get("role") == "Admin"),
    }


def _render_dashboard(
    request: Request,
    *,
    search_query: str = "",
    form_data: dict[str, str] | None = None,
    form_error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Carga los usuarios desde DynamoDB y renderiza el panel principal."""
    users: list[dict[str, Any]] = []
    aws_error: str | None = None

    try:
        users = aws_service.list_users(search_query=search_query)
    except aws_service.CloudServiceError as exc:
        aws_error = str(exc)

    context = {
        "request": request,
        "admin_user": _get_current_admin(request),
        "users": users,
        "stats": _compute_stats(users),
        "search_query": search_query,
        "flash": _pop_flash(request),
        "aws_error": aws_error,
        "form_error": form_error,
        "form_data": form_data or {},
        "roles": ALLOWED_ROLES,
        "statuses": ALLOWED_STATUSES,
        "aws_config": aws_service.get_aws_config(),
    }
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context=context,
        status_code=status_code,
    )


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Endpoint ligero de verificación de estado para Docker y balanceadores."""
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, logged_out: str = "") -> Any:
    """Muestra el formulario de inicio de sesión del administrador."""
    if _get_current_admin(request):
        return RedirectResponse(url="/", status_code=303)

    info_message = (
        "Sesión cerrada correctamente." if logged_out == "1" else None
    )
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "request": request,
            "error": None,
            "info": info_message,
            "username": "",
        },
    )


@app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
) -> Any:
    """Valida las credenciales contra ADMIN_USER / ADMIN_PASSWORD y crea la sesión."""
    expected_user = os.getenv("ADMIN_USER", "").strip()
    expected_password = os.getenv("ADMIN_PASSWORD", "")

    if not expected_user or not expected_password:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "request": request,
                "error": "Las variables ADMIN_USER y ADMIN_PASSWORD no están configuradas.",
                "info": None,
                "username": username,
            },
            status_code=500,
        )

    user_ok = secrets.compare_digest(
        username.strip().encode("utf-8"),
        expected_user.encode("utf-8"),
    )
    pass_ok = secrets.compare_digest(
        password.encode("utf-8"),
        expected_password.encode("utf-8"),
    )

    if not (user_ok and pass_ok):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "request": request,
                "error": "Usuario o contraseña incorrectos.",
                "info": None,
                "username": username.strip(),
            },
            status_code=401,
        )

    request.session.clear()
    request.session["admin_user"] = expected_user
    _set_flash(request, "success", f"Bienvenido, {expected_user}.")
    return RedirectResponse(url="/", status_code=303)


@app.api_route("/logout", methods=["GET", "POST"])
async def logout(request: Request) -> RedirectResponse:
    """Cierra la sesión administrativa y redirige al login."""
    request.session.clear()
    return RedirectResponse(url="/login?logged_out=1", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, q: str = "") -> Any:
    """Panel principal con listado, búsqueda y formulario de creación de usuarios."""
    redirect = _require_admin(request)
    if redirect:
        return redirect

    return _render_dashboard(request, search_query=q)


@app.post("/users", response_class=HTMLResponse)
async def create_user_route(
    request: Request,
    full_name: str = Form(""),
    email: str = Form(""),
    role: str = Form("Visualizador"),
    status: str = Form("Activo"),
    avatar: UploadFile | None = File(default=None),
) -> Any:
    """Crea un nuevo usuario en DynamoDB y sube su foto de perfil opcional a S3."""
    redirect = _require_admin(request)
    if redirect:
        return redirect

    form_data = {
        "full_name": full_name,
        "email": email,
        "role": role,
        "status": status,
    }

    try:
        clean_name, clean_email, clean_role, clean_status = _validate_user_input(
            full_name, email, role, status
        )
        uploaded_image = await _read_validated_avatar(avatar)
    except (ValueError, aws_service.ImageValidationError) as exc:
        return _render_dashboard(
            request,
            form_data=form_data,
            form_error=str(exc),
            status_code=400,
        )

    avatar_key = ""
    avatar_url = ""

    try:
        if uploaded_image is not None:
            file_bytes, ext, mime = uploaded_image
            avatar_key, avatar_url = aws_service.upload_avatar_to_s3(
                file_bytes=file_bytes,
                extension=ext,
                content_type=mime,
            )

        aws_service.create_user(
            full_name=clean_name,
            email=clean_email,
            role=clean_role,
            status=clean_status,
            avatar_url=avatar_url,
            avatar_key=avatar_key,
        )
    except aws_service.CloudServiceError as exc:
        if avatar_key:
            try:
                aws_service.delete_avatar_from_s3(avatar_key=avatar_key)
            except aws_service.CloudServiceError:
                logger.warning("No se pudo limpiar imagen huérfana en S3: %s", avatar_key)
        return _render_dashboard(
            request,
            form_data=form_data,
            form_error=str(exc),
            status_code=502,
        )

    _set_flash(request, "success", f"Usuario '{clean_name}' creado correctamente.")
    return RedirectResponse(url="/", status_code=303)


@app.get("/users/{user_id}/edit", response_class=HTMLResponse)
async def edit_user_page(request: Request, user_id: str) -> Any:
    """Muestra el formulario de edición para un usuario existente."""
    redirect = _require_admin(request)
    if redirect:
        return redirect

    try:
        user = aws_service.get_user_by_id(user_id)
    except aws_service.CloudServiceError as exc:
        _set_flash(request, "error", str(exc))
        return RedirectResponse(url="/", status_code=303)

    if not user:
        _set_flash(request, "error", "El usuario solicitado no existe.")
        return RedirectResponse(url="/", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="edit.html",
        context={
            "request": request,
            "admin_user": _get_current_admin(request),
            "user": user,
            "form_error": None,
            "roles": ALLOWED_ROLES,
            "statuses": ALLOWED_STATUSES,
            "aws_config": aws_service.get_aws_config(),
        },
    )


@app.post("/users/{user_id}/edit", response_class=HTMLResponse)
async def edit_user_submit(
    request: Request,
    user_id: str,
    full_name: str = Form(""),
    email: str = Form(""),
    role: str = Form("Visualizador"),
    status: str = Form("Activo"),
    remove_avatar: str = Form(""),
    avatar: UploadFile | None = File(default=None),
) -> Any:
    """Actualiza datos en DynamoDB y reemplaza/elimina la foto en S3 si corresponde."""
    redirect = _require_admin(request)
    if redirect:
        return redirect

    try:
        existing_user = aws_service.get_user_by_id(user_id)
    except aws_service.CloudServiceError as exc:
        _set_flash(request, "error", str(exc))
        return RedirectResponse(url="/", status_code=303)

    if not existing_user:
        _set_flash(request, "error", "El usuario solicitado no existe.")
        return RedirectResponse(url="/", status_code=303)

    draft_user = {
        **existing_user,
        "full_name": full_name,
        "email": email,
        "role": role,
        "status": status,
    }

    try:
        clean_name, clean_email, clean_role, clean_status = _validate_user_input(
            full_name, email, role, status
        )
        uploaded_image = await _read_validated_avatar(avatar)
    except (ValueError, aws_service.ImageValidationError) as exc:
        return templates.TemplateResponse(
            request=request,
            name="edit.html",
            context={
                "request": request,
                "admin_user": _get_current_admin(request),
                "user": draft_user,
                "form_error": str(exc),
                "roles": ALLOWED_ROLES,
                "statuses": ALLOWED_STATUSES,
                "aws_config": aws_service.get_aws_config(),
            },
            status_code=400,
        )

    old_key = str(existing_user.get("avatar_key", "") or "")
    old_url = str(existing_user.get("avatar_url", "") or "")
    new_key = old_key
    new_url = old_url
    uploaded_new_key = ""

    try:
        if uploaded_image is not None:
            file_bytes, ext, mime = uploaded_image
            uploaded_new_key, new_url = aws_service.upload_avatar_to_s3(
                file_bytes=file_bytes,
                extension=ext,
                content_type=mime,
            )
            new_key = uploaded_new_key
        elif remove_avatar == "1":
            new_key = ""
            new_url = ""

        aws_service.update_user(
            user_id=user_id,
            full_name=clean_name,
            email=clean_email,
            role=clean_role,
            status=clean_status,
            avatar_url=new_url,
            avatar_key=new_key,
        )
    except aws_service.CloudServiceError as exc:
        if uploaded_new_key:
            try:
                aws_service.delete_avatar_from_s3(avatar_key=uploaded_new_key)
            except aws_service.CloudServiceError:
                logger.warning("No se pudo limpiar imagen nueva en S3: %s", uploaded_new_key)
        return templates.TemplateResponse(
            request=request,
            name="edit.html",
            context={
                "request": request,
                "admin_user": _get_current_admin(request),
                "user": draft_user,
                "form_error": str(exc),
                "roles": ALLOWED_ROLES,
                "statuses": ALLOWED_STATUSES,
                "aws_config": aws_service.get_aws_config(),
            },
            status_code=502,
        )

    if (uploaded_image is not None or remove_avatar == "1") and (old_key or old_url):
        try:
            aws_service.delete_avatar_from_s3(avatar_key=old_key, avatar_url=old_url)
        except aws_service.CloudServiceError as exc:
            logger.warning("No se pudo eliminar la imagen anterior en S3: %s", exc)

    _set_flash(request, "success", f"Usuario '{clean_name}' actualizado correctamente.")
    return RedirectResponse(url="/", status_code=303)


@app.post("/users/{user_id}/delete")
async def delete_user_route(request: Request, user_id: str) -> RedirectResponse:
    """Elimina el registro en DynamoDB y la imagen asociada en Amazon S3."""
    redirect = _require_admin(request)
    if redirect:
        return redirect

    try:
        user = aws_service.get_user_by_id(user_id)
        if not user:
            _set_flash(request, "error", "El usuario no fue encontrado.")
            return RedirectResponse(url="/", status_code=303)

        aws_service.delete_avatar_from_s3(
            avatar_key=user.get("avatar_key"),
            avatar_url=user.get("avatar_url"),
        )
        aws_service.delete_user(user_id)
    except aws_service.CloudServiceError as exc:
        _set_flash(request, "error", str(exc))
        return RedirectResponse(url="/", status_code=303)

    deleted_name = user.get("full_name", "Usuario")
    _set_flash(
        request,
        "success",
        f"Usuario '{deleted_name}' y su imagen en S3 eliminados correctamente.",
    )
    return RedirectResponse(url="/", status_code=303)
