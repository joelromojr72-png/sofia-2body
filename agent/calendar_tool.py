# agent/calendar_tool.py — Agenda real en Google Calendar
# Generado por AgentKit

"""
Conexion con Google Calendar para que Sofia agende citas de verdad.

Se autentica con una CUENTA DE SERVICIO (service account) que tiene permiso de
edicion sobre el calendario dedicado "Citas 2Body". Asi el agente no depende de que
ningun humano tenga sesion iniciada.

Variables de entorno:
  GOOGLE_CREDENTIALS_JSON  -> el JSON de la cuenta de servicio, en base64 (recomendado)
                             o pegado tal cual. Si esta vacio, la agenda queda apagada
                             y Sofia cae al modo "el equipo confirma".
  GOOGLE_CALENDAR_ID       -> el id del calendario "Citas 2Body".
  GOOGLE_CALENDAR_TZ       -> opcional, default America/Mexico_City.

Las funciones son SINCRONAS (la libreria de Google lo es). brain.py las llama dentro
de asyncio.to_thread para no bloquear el event loop.
"""

import base64
import binascii
import json
import logging
import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger("agentkit")

TZ = ZoneInfo(os.getenv("GOOGLE_CALENDAR_TZ") or "America/Mexico_City")
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "").strip()
# Si se define, la cuenta de servicio impersona a este usuario del dominio (Delegación a
# nivel de dominio). Necesario cuando el Workspace no deja compartir edición con externos:
# se autoriza el client_id del SA + este scope en el Admin console, y el SA actúa como este
# usuario (ej. pachuca@2body.mx), con acceso pleno a su propio calendario.
IMPERSONAR = os.getenv("GOOGLE_IMPERSONATE_SUBJECT", "").strip()

# OAuth de usuario (la app actua COMO este usuario, ej. pachuca@2body.mx). Se usa cuando
# el Workspace no deja compartir edicion con una cuenta de servicio externa: se crea una
# app OAuth interna del dominio, el usuario autoriza una vez y aqui va su refresh token.
# Si estan estas tres, se prefiere OAuth sobre la cuenta de servicio.
OAUTH_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
OAUTH_REFRESH_TOKEN = os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN", "").strip()

_SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Horario del negocio por dia de la semana (0=Lun ... 6=Dom). None = cerrado.
HORARIO = {
    0: (time(9, 0), time(20, 0)),
    1: (time(9, 0), time(20, 0)),
    2: (time(9, 0), time(20, 0)),
    3: (time(9, 0), time(20, 0)),
    4: (time(9, 0), time(20, 0)),
    5: (time(9, 0), time(14, 0)),
    6: None,  # domingo cerrado
}
_DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

# La libreria de Google es pesada; si no esta instalada, la agenda queda apagada
# pero el agente igual arranca y sigue contestando lo demas.
try:
    from google.oauth2 import service_account
    from google.oauth2.credentials import Credentials as UserCredentials
    from googleapiclient.discovery import build

    _LIBS_OK = True
except ImportError:  # pragma: no cover
    _LIBS_OK = False
    logger.warning("google-api-python-client no instalado: la agenda queda apagada")


def _oauth_configurado() -> bool:
    """True si hay credenciales OAuth de usuario completas (refresh token incluido)."""
    return bool(OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET and OAUTH_REFRESH_TOKEN)

_servicio = None  # cache del cliente de Calendar


def _cargar_credenciales_json() -> dict | None:
    """Lee el JSON de la cuenta de servicio desde la env (base64 o texto plano)."""
    crudo = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
    if not crudo:
        return None
    # Primero se intenta como base64; si no, se asume que es el JSON pegado directo.
    try:
        crudo = base64.b64decode(crudo).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        pass
    try:
        return json.loads(crudo)
    except json.JSONDecodeError as e:
        logger.error(f"GOOGLE_CREDENTIALS_JSON no es un JSON valido: {e}")
        return None


def calendario_configurado() -> bool:
    """True si hay librerias, id de calendario y ALGUNA credencial (OAuth o cuenta de servicio)."""
    tiene_credenciales = _oauth_configurado() or bool(
        os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
    )
    return bool(_LIBS_OK and CALENDAR_ID and tiene_credenciales)


def _obtener_servicio():
    """Construye (y cachea) el cliente de Google Calendar."""
    global _servicio
    if _servicio is not None:
        return _servicio

    # 1) OAuth de usuario (preferido): la app actua como el usuario que autorizo.
    if _oauth_configurado():
        creds = UserCredentials(
            token=None,
            refresh_token=OAUTH_REFRESH_TOKEN,
            client_id=OAUTH_CLIENT_ID,
            client_secret=OAUTH_CLIENT_SECRET,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=_SCOPES,
        )
        _servicio = build("calendar", "v3", credentials=creds, cache_discovery=False)
        return _servicio

    # 2) Cuenta de servicio (respaldo).
    info = _cargar_credenciales_json()
    if not info:
        return None
    creds = service_account.Credentials.from_service_account_info(info, scopes=_SCOPES)
    if IMPERSONAR:
        # Delegación a nivel de dominio: actuar como este usuario del Workspace.
        creds = creds.with_subject(IMPERSONAR)
    _servicio = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return _servicio


def horario_dia(fecha: date) -> tuple[time, time] | None:
    """Horario de atencion de ese dia, o None si esta cerrado."""
    return HORARIO.get(fecha.weekday())


def verificar_conexion() -> tuple[bool, str]:
    """Chequeo de arranque: confirma que el SA puede leer el calendario."""
    if not calendario_configurado():
        return False, "Agenda apagada (faltan GOOGLE_CREDENTIALS_JSON o GOOGLE_CALENDAR_ID)"
    try:
        svc = _obtener_servicio()
        cal = svc.calendars().get(calendarId=CALENDAR_ID).execute()
        return True, f"Calendario '{cal.get('summary', '?')}' conectado"
    except Exception as e:  # noqa: BLE001
        return False, f"No se pudo abrir el calendario: {e}"


def citas_del_dia(fecha: date) -> list[dict]:
    """
    Devuelve las citas YA agendadas ese dia (para no encimar).
    Cada item: {"inicio": "HH:MM", "fin": "HH:MM", "titulo": str}.
    """
    svc = _obtener_servicio()
    if not svc:
        return []
    inicio = datetime.combine(fecha, time(0, 0), tzinfo=TZ)
    fin = inicio + timedelta(days=1)
    try:
        resp = (
            svc.events()
            .list(
                calendarId=CALENDAR_ID,
                timeMin=inicio.isoformat(),
                timeMax=fin.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error leyendo el calendario: {e}")
        return []

    citas = []
    for ev in resp.get("items", []):
        ini = ev.get("start", {}).get("dateTime")
        f = ev.get("end", {}).get("dateTime")
        if not ini or not f:
            continue  # eventos de dia completo se ignoran
        try:
            di = datetime.fromisoformat(ini).astimezone(TZ)
            df = datetime.fromisoformat(f).astimezone(TZ)
        except ValueError:
            continue
        citas.append(
            {
                "inicio": di.strftime("%H:%M"),
                "fin": df.strftime("%H:%M"),
                "titulo": ev.get("summary", "Ocupado"),
            }
        )
    return citas


def crear_cita(
    nombre: str,
    telefono: str,
    servicio: str,
    inicio: datetime,
    duracion_min: int = 60,
    notas: str = "",
) -> dict:
    """
    Crea un evento (marcado 'por confirmar') en el calendario Citas 2Body.

    Retorna {"ok": True, "cuando": "...", "link": "..."} o {"ok": False, "error": "..."}.
    """
    svc = _obtener_servicio()
    if not svc:
        return {"ok": False, "error": "La agenda no esta configurada."}

    if inicio.tzinfo is None:
        inicio = inicio.replace(tzinfo=TZ)
    else:
        inicio = inicio.astimezone(TZ)

    # Validaciones de sentido comun (no agendar en el pasado ni fuera de horario).
    ahora = datetime.now(TZ)
    if inicio < ahora - timedelta(minutes=1):
        return {"ok": False, "error": "Esa fecha/hora ya pasó."}

    hd = horario_dia(inicio.date())
    if hd is None:
        return {"ok": False, "error": "Ese día el spa está cerrado (domingo)."}
    apertura, cierre = hd
    fin = inicio + timedelta(minutes=max(15, duracion_min))
    if inicio.time() < apertura or fin.time() > cierre:
        return {
            "ok": False,
            "error": f"Fuera del horario de ese día ({apertura.strftime('%H:%M')}"
            f"-{cierre.strftime('%H:%M')}).",
        }

    tel = telefono if telefono.startswith("+") else f"+{telefono}"
    cuerpo = {
        "summary": f"Cita (por confirmar) - {servicio} - {nombre}",
        "description": (
            "Solicitud tomada por Sofía (bot de WhatsApp).\n"
            f"Cliente: {nombre}\n"
            f"WhatsApp: {tel}\n"
            f"Servicio: {servicio}\n"
            + (f"Notas: {notas}\n" if notas else "")
            + "\nEl equipo debe confirmar disponibilidad con la clienta."
        ),
        "start": {"dateTime": inicio.isoformat(), "timeZone": str(TZ)},
        "end": {"dateTime": fin.isoformat(), "timeZone": str(TZ)},
        "status": "tentative",
        "colorId": "5",  # amarillo = por confirmar
        "reminders": {"useDefault": True},
    }
    try:
        ev = svc.events().insert(calendarId=CALENDAR_ID, body=cuerpo).execute()
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error creando la cita: {e}")
        return {"ok": False, "error": "No se pudo guardar en la agenda."}

    dia_txt = _DIAS[inicio.weekday()]
    cuando = f"{dia_txt} {inicio.day} a las {inicio.strftime('%H:%M')}"
    logger.info(f"Cita creada en calendario: {cuerpo['summary']} @ {inicio.isoformat()}")
    return {"ok": True, "cuando": cuando, "link": ev.get("htmlLink", "")}
