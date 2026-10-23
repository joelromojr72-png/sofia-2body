# agent/calendar_tool.py — Agenda real en Google Calendar (multi-calendario)
# Generado por AgentKit

"""
Conexion con Google Calendar para que Sofia agende citas de verdad.

2 Body Pachuca NO usa un solo calendario: el equipo tiene un calendario POR SERVICIO
(masajes, Faciales, corporales, Depilación, "cita de venta o ex unica"), cada uno con su
propio color. Las citas reales viven ahí, no en el calendario principal (pachuca@2body.mx,
que está vacío). Por eso Sofia:

  • LEE la disponibilidad de TODOS los calendarios de servicio a la vez (más el principal),
    para no ofrecer un horario que en realidad ya está ocupado y no encimar citas.
  • ESCRIBE cada cita nueva en el calendario del SERVICIO que corresponde, para que aparezca
    con el color de ese servicio y el equipo la vea donde toca.

NO crea bloqueos, espejos ni eventos "Ocupado": el calendario del equipo NO se ensucia,
solo aparece lo que de verdad se agendó.

Se autentica con OAuth de usuario (la app actúa COMO pachuca@2body.mx) o, de respaldo, con
una cuenta de servicio. Así el agente no depende de que ningún humano tenga sesión iniciada.

Variables de entorno:
  GOOGLE_OAUTH_CLIENT_ID / _SECRET / _REFRESH_TOKEN -> credenciales OAuth (preferido).
  GOOGLE_CREDENTIALS_JSON  -> JSON de cuenta de servicio (base64 o texto), respaldo.
  GOOGLE_CALENDAR_ID       -> calendario principal (pachuca@2body.mx). Gatilla la agenda.
  GOOGLE_CALENDAR_TZ       -> opcional, default America/Mexico_City.
  GOOGLE_SERVICE_CALENDARS -> opcional, JSON {"masajes":"<id>", ...} para sobrescribir los
                              ids de los calendarios de servicio sin tocar el código.

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
# nivel de dominio). Necesario cuando el Workspace no deja compartir edición con externos.
IMPERSONAR = os.getenv("GOOGLE_IMPERSONATE_SUBJECT", "").strip()

# OAuth de usuario (la app actua COMO este usuario, ej. pachuca@2body.mx). Si estan estas
# tres, se prefiere OAuth sobre la cuenta de servicio.
OAUTH_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
OAUTH_REFRESH_TOKEN = os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN", "").strip()

_SCOPES = ["https://www.googleapis.com/auth/calendar"]

# ── Calendarios por servicio ─────────────────────────────────────────────
# (clave, id del calendario, palabras clave para rutear un servicio a este calendario).
# Cada calendario ya tiene su color en Google Calendar: la cita hereda ese color sola, por
# eso NO forzamos colorId. El orden importa: gana la primera coincidencia de palabra clave;
# si nada coincide, la cita cae al calendario "venta" (valoración / experiencia única).
_DEFAULT_SERVICE_CALENDARS: list[tuple[str, str, list[str]]] = [
    (
        "masajes",
        "c_f54cdd584a9a7008155f1687943a06c6c4047d22fba1cca8453b9e7c5f1d0488@group.calendar.google.com",
        ["masaje", "relaj", "descontractur", "sueco", "deportivo", "piedras", "espalda"],
    ),
    (
        "faciales",
        "c_9734a2365fd1b75d5dd8194adcf9aba3c389abcee716a97e4bed2ab79e4bcfc8@group.calendar.google.com",
        ["facial", "rostro", "limpieza", "hidrataci", "mancha", "acné", "acne",
         "rejuven", "porcelana", "peeling", "dermapen", "piel", "célula", "celula",
         "muerta", "exfoli", "expresión", "expresion", "arruga"],
    ),
    (
        "depilacion",
        "c_9d868531d58565c5b3a443bc99d66f207f10300a1502c0234c173002465c1215@group.calendar.google.com",
        # OJO: sin "láser" a secas, porque "lipoláser" es corporal. Se rutea por "depil",
        # las tecnologías (tridiodo/cuatridiodo/SHR) y las zonas típicas de depilación.
        ["depil", "tridiodo", "cuatridiodo", "shr", "axila", "bikini", "ingle", "vello",
         "entrecejo", "patilla", "bigote", "barba", "nuca"],
    ),
    (
        "corporales",
        "c_f17c2782edafa27e35cf708468788cda8a540fd400247a82842672b8647621eb@group.calendar.google.com",
        ["corpor", "moldea", "cavitaci", "mesoterap", "lipo", "reafirm", "celulit",
         "glúteo", "gluteo", "busto", "estría", "estria", "post-parto", "postparto",
         "tonific", "abdomen", "cintura", "vela", "madero", "maderoterap", "drenaje",
         "linfátic", "linfatic", "radiofrec", "vacum", "vacuum", "gimnasia", "pasiva",
         "ems", "electromagn", "emsculpt", "s-shape", "sshape", "s shape", "lipoláser",
         "lipolaser", "ultrasonido"],
    ),
    (
        "venta",
        "c_c227eba35cacc025662e6a6cf42ac2ff402aa33a76792bf2923a6335f86ec25c@group.calendar.google.com",
        ["valorac", "venta", "primera", "diagn", "experiencia", "gratis", "sin costo",
         "cortesía", "cortesia", "conocer", "cita"],
    ),
]

# El calendario "venta" es el de respaldo (valoración / experiencia única gratis).
_CAL_VENTA_KEY = "venta"


def _cargar_service_calendars() -> list[tuple[str, str, list[str]]]:
    """Aplica el override de GOOGLE_SERVICE_CALENDARS (JSON) sobre los ids por defecto."""
    override_raw = os.getenv("GOOGLE_SERVICE_CALENDARS", "").strip()
    override: dict = {}
    if override_raw:
        try:
            override = json.loads(override_raw) or {}
        except json.JSONDecodeError as e:
            logger.error(f"GOOGLE_SERVICE_CALENDARS no es JSON valido, se ignora: {e}")
    salida = []
    for clave, cal_id, kws in _DEFAULT_SERVICE_CALENDARS:
        salida.append((clave, str(override.get(clave, cal_id)).strip() or cal_id, kws))
    return salida


_SERVICE_CALENDARS = _cargar_service_calendars()
_CAL_POR_CLAVE = {clave: cal_id for clave, cal_id, _ in _SERVICE_CALENDARS}


def _calendario_venta() -> str:
    """Calendario de respaldo: 'cita de venta o ex unica'."""
    return _CAL_POR_CLAVE.get(_CAL_VENTA_KEY) or CALENDAR_ID


def _calendario_para_servicio(texto: str) -> str:
    """Devuelve el id del calendario que le toca a un servicio, por palabras clave."""
    t = (texto or "").lower()
    for clave, cal_id, kws in _SERVICE_CALENDARS:
        if clave == _CAL_VENTA_KEY:
            continue  # el de venta es el respaldo: se evalúa al final
        if any(k in t for k in kws):
            return cal_id
    # Palabras clave del propio calendario de venta (valoración/experiencia) o, si nada
    # coincide, el respaldo: la primera sesión de valoración / experiencia única.
    return _calendario_venta()


def _calendarios_disponibilidad() -> list[str]:
    """Todos los calendarios que se leen para saber qué horarios están ocupados."""
    ids = [cal_id for _, cal_id, _ in _SERVICE_CALENDARS]
    if CALENDAR_ID and CALENDAR_ID not in ids:
        ids.append(CALENDAR_ID)  # por si el equipo apunta algo en el principal
    return ids


def _componer_evento_id(cal_id: str, ev_id: str) -> str:
    """Empaqueta calendario+evento en un solo id opaco para el modelo (para poder cancelar)."""
    return f"{cal_id}|{ev_id}"


def _partir_evento_id(evento_id: str) -> tuple[str, str]:
    """Separa el id compuesto. Compatibilidad: sin '|' se asume el calendario principal."""
    if evento_id and "|" in evento_id:
        cal, _, eid = evento_id.partition("|")
        return cal, eid
    return CALENDAR_ID, evento_id


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
        creds = creds.with_subject(IMPERSONAR)
    _servicio = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return _servicio


def horario_dia(fecha: date) -> tuple[time, time] | None:
    """Horario de atencion de ese dia, o None si esta cerrado."""
    return HORARIO.get(fecha.weekday())


def verificar_conexion() -> tuple[bool, str]:
    """Chequeo de arranque: confirma que se puede leer el calendario principal."""
    if not calendario_configurado():
        return False, "Agenda apagada (faltan credenciales o GOOGLE_CALENDAR_ID)"
    try:
        svc = _obtener_servicio()
        cal = svc.calendars().get(calendarId=CALENDAR_ID).execute()
        n = len(_calendarios_disponibilidad())
        return True, f"Calendario '{cal.get('summary', '?')}' conectado ({n} calendarios en lectura)"
    except Exception as e:  # noqa: BLE001
        return False, f"No se pudo abrir el calendario: {e}"


def _listar_eventos(svc, cal_id: str, ini_iso: str, fin_iso: str) -> list[dict]:
    """Lista los eventos de UN calendario en un rango. Un calendario que falla no tumba el resto."""
    try:
        resp = (
            svc.events()
            .list(
                calendarId=cal_id,
                timeMin=ini_iso,
                timeMax=fin_iso,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        return resp.get("items", [])
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error leyendo calendario {cal_id}: {e}")
        return []


def _eventos_dia_todos(svc, fecha: date) -> list[tuple[str, dict]]:
    """Todos los eventos de un día en TODOS los calendarios de disponibilidad. (cal_id, evento)."""
    ini = datetime.combine(fecha, time(0, 0), tzinfo=TZ)
    fin = ini + timedelta(days=1)
    salida: list[tuple[str, dict]] = []
    for cal_id in _calendarios_disponibilidad():
        for ev in _listar_eventos(svc, cal_id, ini.isoformat(), fin.isoformat()):
            salida.append((cal_id, ev))
    return salida


def citas_del_dia(fecha: date) -> list[dict]:
    """
    Citas YA agendadas ese dia en TODOS los calendarios de servicio (para no encimar).
    Cada item: {"inicio": "HH:MM", "fin": "HH:MM", "titulo": str}.
    """
    svc = _obtener_servicio()
    if not svc:
        return []

    citas = []
    for _cal_id, ev in _eventos_dia_todos(svc, fecha):
        if ev.get("status") == "cancelled":
            continue
        ini = ev.get("start", {}).get("dateTime")
        f = ev.get("end", {}).get("dateTime")
        if not ini or not f:
            continue  # eventos de dia completo (ej. festivos) no bloquean horarios
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


def slots_libres(
    fecha: date, duracion_min: int = 60, paso_min: int = 60, max_slots: int = 8
) -> list[str]:
    """
    Horarios de inicio REALMENTE LIBRES de un día (descontando lo ocupado en TODOS los
    calendarios), para que Sofía ofrezca solo huecos que no chocan con ninguna cita.
    Solo horas EN PUNTO: la clínica agenda cada hora (9:00, 10:00, 11:00...).
    """
    hd = horario_dia(fecha)
    if hd is None:
        return []  # domingo / cerrado
    apertura, cierre = hd
    dur = max(15, int(duracion_min or 60))

    ocupadas: list[tuple[time, time]] = []
    for c in citas_del_dia(fecha):
        try:
            hi = datetime.strptime(c["inicio"], "%H:%M").time()
            hf = datetime.strptime(c["fin"], "%H:%M").time()
        except (ValueError, KeyError):
            continue
        ocupadas.append((hi, hf))

    ahora = datetime.now(TZ)
    t = datetime.combine(fecha, apertura, tzinfo=TZ)
    if t.minute or t.second:  # arranca siempre en hora en punto
        t = t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    fin_dia = datetime.combine(fecha, cierre, tzinfo=TZ)
    paso = timedelta(minutes=max(5, paso_min))
    d = timedelta(minutes=dur)

    libres: list[str] = []
    while t + d <= fin_dia:
        ini_t, fin_t = t.time(), (t + d).time()
        en_futuro = t >= ahora + timedelta(minutes=1)
        choca = any(ini_t < of and fin_t > oi for (oi, of) in ocupadas)
        if en_futuro and not choca:
            libres.append(t.strftime("%H:%M"))
            if len(libres) >= max_slots:
                break
        t += paso
    return libres


def citas_para_confirmar(fecha: date) -> list[dict]:
    """
    Citas de un día (en todos los calendarios) con datos para confirmar por WhatsApp.
    Solo las que Sofía tomó (tienen 'WhatsApp: +...' en la descripción).
    Cada item: {"evento_id", "telefono", "nombre", "servicio", "cuando", "hora"}.
    """
    import re

    svc = _obtener_servicio()
    if not svc:
        return []

    out = []
    for cal_id, ev in _eventos_dia_todos(svc, fecha):
        if ev.get("status") == "cancelled":
            continue
        ini = ev.get("start", {}).get("dateTime")
        if not ini:
            continue
        desc = ev.get("description", "") or ""
        m_tel = re.search(r"WhatsApp:\s*\+?([0-9]{8,15})", desc)
        if not m_tel:
            continue  # sin teléfono no se puede confirmar (evento manual del equipo)
        m_serv = re.search(r"Servicio:\s*(.+)", desc)
        m_nom = re.search(r"Cliente:\s*(.+)", desc)
        try:
            di = datetime.fromisoformat(ini).astimezone(TZ)
        except ValueError:
            continue
        out.append(
            {
                "evento_id": _componer_evento_id(cal_id, ev.get("id", "")),
                "telefono": m_tel.group(1),
                "nombre": (m_nom.group(1).strip() if m_nom else ""),
                "servicio": (m_serv.group(1).strip() if m_serv else ev.get("summary", "tu cita")),
                "cuando": f"{_DIAS[di.weekday()]} {di.day} a las {di.strftime('%H:%M')}",
                "hora": di.strftime("%H:%M"),
            }
        )
    return out


def eventos_con_hora(fecha: date) -> list[dict]:
    """
    TODAS las citas con hora de un día (las de Sofía y las que apuntó el equipo), con el
    calendario de servicio al que pertenecen. Para el recordatorio del día antes.
    Cada item: {"evento_id", "calendario", "titulo", "descripcion", "inicio"(datetime)}.
    """
    svc = _obtener_servicio()
    if not svc:
        return []
    clave_por_id = {cal_id: clave for clave, cal_id, _ in _SERVICE_CALENDARS}
    out = []
    for cal_id, ev in _eventos_dia_todos(svc, fecha):
        if ev.get("status") == "cancelled":
            continue
        ini = (ev.get("start") or {}).get("dateTime")
        if not ini:
            continue
        try:
            di = datetime.fromisoformat(ini).astimezone(TZ)
        except ValueError:
            continue
        out.append(
            {
                "evento_id": _componer_evento_id(cal_id, ev.get("id", "")),
                "calendario": clave_por_id.get(cal_id, "principal"),
                "titulo": ev.get("summary", "") or "",
                "descripcion": ev.get("description", "") or "",
                "inicio": di,
            }
        )
    return out


def cuando_legible(di: datetime) -> str:
    """'viernes 24 a las 17:00'."""
    return f"{_DIAS[di.weekday()]} {di.day} a las {di.strftime('%H:%M')}"


def crear_cita(
    nombre: str,
    telefono: str,
    servicio: str,
    inicio: datetime,
    duracion_min: int = 60,
    notas: str = "",
) -> dict:
    """
    Crea la cita (marcada 'por confirmar') en el calendario del SERVICIO que corresponde.
    Antes revisa disponibilidad en TODOS los calendarios para no encimar.

    Retorna {"ok": True, "cuando": "...", "link": "..."} o {"ok": False, "error": "..."}.
    """
    svc = _obtener_servicio()
    if not svc:
        return {"ok": False, "error": "La agenda no esta configurada."}

    if inicio.tzinfo is None:
        inicio = inicio.replace(tzinfo=TZ)
    else:
        inicio = inicio.astimezone(TZ)

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

    # La clínica solo agenda en hora EN PUNTO (no :30). Rechazamos para que Sofía
    # ofrezca una hora en punto en lugar de meter una cita a la media hora.
    if inicio.minute != 0 or inicio.second != 0:
        return {
            "ok": False,
            "error": "Solo agendamos en horario en punto (por ejemplo 12:00 o 13:00). "
            "Ofrece una hora en punto.",
        }

    tel = telefono if telefono.startswith("+") else f"+{telefono}"
    tel_digits = "".join(c for c in tel if c.isdigit())
    cal_destino = _calendario_para_servicio(servicio)

    # ── Anti-doble-agenda (candado del lado del servidor) ────────────────
    # Se revisa el traslape contra TODOS los calendarios de servicio:
    #   • Traslape de ESTA MISMA clienta → ya tenía cita: NO duplicamos (idempotente).
    #   • Traslape de OTRA clienta → el horario está ocupado: se rechaza.
    existentes = list(_eventos_dia_todos(svc, inicio.date()))  # [(cal_id, evento), ...]

    for _cal, ev in existentes:
        if ev.get("status") == "cancelled":
            continue
        s = ev.get("start", {}).get("dateTime")
        e = ev.get("end", {}).get("dateTime")
        if not s or not e:
            continue  # eventos de día completo no bloquean horarios
        try:
            ev_ini = datetime.fromisoformat(s).astimezone(TZ)
            ev_fin = datetime.fromisoformat(e).astimezone(TZ)
        except ValueError:
            continue
        if inicio < ev_fin and fin > ev_ini:  # hay traslape
            ev_desc_digits = "".join(c for c in ev.get("description", "") if c.isdigit())
            mismo_cliente = bool(tel_digits) and tel_digits in ev_desc_digits
            cuando_ev = f"{_DIAS[ev_ini.weekday()]} {ev_ini.day} a las {ev_ini.strftime('%H:%M')}"
            if mismo_cliente:
                logger.info(f"Doble-agenda evitada: {tel} ya tenía cita @ {ev_ini.isoformat()}")
                return {
                    "ok": True,
                    "duplicada": True,
                    "cuando": cuando_ev,
                    "link": ev.get("htmlLink", ""),
                }
            logger.info(f"Horario ocupado, se rechaza: {inicio.isoformat()} (choca con {ev_ini.isoformat()})")
            return {
                "ok": False,
                "error": f"Ese horario ({inicio.strftime('%H:%M')}) ya está ocupado; ofrece otra hora.",
            }

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
        # Sin colorId: la cita hereda el color del calendario del servicio.
        "reminders": {"useDefault": True},
    }
    # ── Reagenda = mover, no duplicar ────────────────────────────────────
    # Si ESTA clienta ya tiene una cita de Sofía del MISMO servicio ese día en otro
    # horario (sin traslape), es una reprogramación: se MUEVE esa cita al nuevo horario
    # en vez de crear otra. Evita duplicados como 9:30 / 10:30 / 11:30 de la misma
    # persona el mismo día. Solo toca citas que tomó Sofía, nunca las del equipo.
    mismos = []
    for cal_id, ev_x in existentes:
        if ev_x.get("status") == "cancelled":
            continue
        if cal_id != cal_destino or not ev_x.get("start", {}).get("dateTime"):
            continue
        desc_x = ev_x.get("description", "")
        if "Sofía" not in desc_x and "bot de WhatsApp" not in desc_x:
            continue
        if tel_digits and tel_digits in "".join(c for c in desc_x if c.isdigit()):
            mismos.append((cal_id, ev_x))

    if mismos:
        cal_id, ev_mover = mismos[0]
        for cal_extra, ev_extra in mismos[1:]:  # limpia duplicados previos de la misma clienta
            try:
                svc.events().delete(calendarId=cal_extra, eventId=ev_extra.get("id", "")).execute()
            except Exception:  # noqa: BLE001
                pass
        try:
            ev = svc.events().update(
                calendarId=cal_id, eventId=ev_mover.get("id", ""), body=cuerpo
            ).execute()
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error reagendando la cita en {cal_id}: {e}")
            return {"ok": False, "error": "No se pudo reagendar la cita."}
        dia_txt = _DIAS[inicio.weekday()]
        cuando = f"{dia_txt} {inicio.day} a las {inicio.strftime('%H:%M')}"
        logger.info(f"Cita reagendada (movida) en {cal_id} @ {inicio.isoformat()}")
        return {"ok": True, "reagendada": True, "cuando": cuando, "link": ev.get("htmlLink", "")}

    try:
        ev = svc.events().insert(calendarId=cal_destino, body=cuerpo).execute()
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error creando la cita en {cal_destino}: {e}")
        return {"ok": False, "error": "No se pudo guardar en la agenda."}

    # ── Recheck anti-carrera ─────────────────────────────────────────────
    # Si DOS clientas distintas caen al mismo hueco casi al mismo tiempo, volvemos a leer
    # TODOS los calendarios: si otra cita de OTRA clienta se traslapa y se creó ANTES que la
    # nuestra, ella gana el hueco y borramos la nuestra para no empalmar.
    try:
        nuestro_id = ev.get("id", "")
        nuestro_created = ev.get("created", "")
        for _cal, otro in _eventos_dia_todos(svc, inicio.date()):
            if otro.get("id") == nuestro_id or otro.get("status") == "cancelled":
                continue
            s = otro.get("start", {}).get("dateTime")
            e = otro.get("end", {}).get("dateTime")
            if not s or not e:
                continue
            try:
                oi = datetime.fromisoformat(s).astimezone(TZ)
                of = datetime.fromisoformat(e).astimezone(TZ)
            except ValueError:
                continue
            if inicio < of and fin > oi:  # se traslapan
                od = "".join(c for c in otro.get("description", "") if c.isdigit())
                if tel_digits and tel_digits in od:
                    continue  # otra cita de la MISMA clienta, no un empalme con alguien más
                if otro.get("created", "") < nuestro_created:
                    try:
                        svc.events().delete(calendarId=cal_destino, eventId=nuestro_id).execute()
                    except Exception:  # noqa: BLE001
                        pass
                    logger.info(f"Carrera: se cede el hueco {inicio.isoformat()} a una cita previa")
                    return {
                        "ok": False,
                        "error": f"Ese horario ({inicio.strftime('%H:%M')}) se acaba de ocupar; ofrece otra hora.",
                    }
    except Exception as e:  # noqa: BLE001
        logger.error(f"No se pudo re-verificar traslape post-insert: {e}")

    dia_txt = _DIAS[inicio.weekday()]
    cuando = f"{dia_txt} {inicio.day} a las {inicio.strftime('%H:%M')}"
    logger.info(f"Cita creada en {cal_destino}: {cuerpo['summary']} @ {inicio.isoformat()}")
    return {"ok": True, "cuando": cuando, "link": ev.get("htmlLink", "")}


def telefonos_con_cita(dias_atras: int = 1, dias_adelante: int = 60) -> set[str]:
    """
    Últimos 10 dígitos de cada teléfono que aparece en una cita (título o descripción) de
    CUALQUIER calendario, de ayer a 60 días. Lo usa el seguimiento para NO mandarle
    recordatorios a quien ya tiene cita (aunque la haya agendado el equipo a mano y Sofía no
    se haya enterado). Se lee una vez por ciclo.
    """
    import re

    svc = _obtener_servicio()
    if not svc:
        return set()
    ahora = datetime.now(TZ)
    ini = (ahora - timedelta(days=dias_atras)).isoformat()
    fin = (ahora + timedelta(days=dias_adelante)).isoformat()
    tels: set[str] = set()
    for cal_id in _calendarios_disponibilidad():
        for ev in _listar_eventos(svc, cal_id, ini, fin):
            if ev.get("status") == "cancelled":
                continue
            texto = f"{ev.get('summary', '')} {ev.get('description', '')}"
            # Números de 10+ dígitos aunque vengan con espacios o guiones (771 842 5808).
            for m in re.findall(r"(?:\d[\s\-]?){10,13}", texto):
                digitos = re.sub(r"\D", "", m)
                if len(digitos) >= 10:
                    tels.add(digitos[-10:])
    return tels


def buscar_citas_de(telefono: str, dias_adelante: int = 90) -> list[dict]:
    """
    Próximas citas de UNA clienta (por su WhatsApp), en TODOS los calendarios, para
    cancelar/reagendar. Cada item: {"evento_id", "cuando", "servicio", "titulo"}.
    El evento_id incluye el calendario (para poder cancelarlo después).
    """
    svc = _obtener_servicio()
    if not svc:
        return []
    tel_digits = "".join(c for c in (telefono or "") if c.isdigit())
    if not tel_digits:
        return []
    ahora = datetime.now(TZ)
    fin = ahora + timedelta(days=dias_adelante)

    citas = []
    for cal_id in _calendarios_disponibilidad():
        for ev in _listar_eventos(svc, cal_id, ahora.isoformat(), fin.isoformat()):
            if ev.get("status") == "cancelled":
                continue
            desc_digits = "".join(c for c in ev.get("description", "") if c.isdigit())
            if tel_digits not in desc_digits:
                continue
            ini = ev.get("start", {}).get("dateTime")
            if not ini:
                continue
            try:
                di = datetime.fromisoformat(ini).astimezone(TZ)
            except ValueError:
                continue
            partes = ev.get("summary", "").split(" - ")
            servicio = partes[1] if len(partes) >= 2 else ev.get("summary", "cita")
            citas.append(
                {
                    "evento_id": _componer_evento_id(cal_id, ev.get("id", "")),
                    "cuando": f"{_DIAS[di.weekday()]} {di.day} a las {di.strftime('%H:%M')}",
                    "servicio": servicio,
                    "titulo": ev.get("summary", ""),
                    "_inicio": di,
                }
            )
    citas.sort(key=lambda c: c["_inicio"])
    for c in citas:
        c.pop("_inicio", None)
    return citas


def cancelar_cita(evento_id: str, telefono: str) -> dict:
    """
    Cancela (borra) una cita. Por seguridad SOLO borra si la cita pertenece a esa clienta
    (su WhatsApp está en la descripción). El evento_id trae el calendario codificado.

    Retorna {"ok": True, "cuando": "..."} o {"ok": False, "error": "..."}.
    """
    svc = _obtener_servicio()
    if not svc:
        return {"ok": False, "error": "La agenda no esta configurada."}
    if not evento_id:
        return {"ok": False, "error": "Falta el identificador de la cita."}

    cal_id, ev_id = _partir_evento_id(evento_id)
    if not ev_id:
        return {"ok": False, "error": "Identificador de cita inválido."}

    try:
        ev = svc.events().get(calendarId=cal_id, eventId=ev_id).execute()
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error leyendo la cita a cancelar ({cal_id}): {e}")
        return {"ok": False, "error": "No encontré esa cita."}

    tel_digits = "".join(c for c in (telefono or "") if c.isdigit())
    desc_digits = "".join(c for c in ev.get("description", "") if c.isdigit())
    if tel_digits and tel_digits not in desc_digits:
        return {"ok": False, "error": "Esa cita no está a tu nombre; no puedo cancelarla."}

    ini = ev.get("start", {}).get("dateTime")
    cuando = ""
    if ini:
        try:
            di = datetime.fromisoformat(ini).astimezone(TZ)
            cuando = f"{_DIAS[di.weekday()]} {di.day} a las {di.strftime('%H:%M')}"
        except ValueError:
            pass

    try:
        svc.events().delete(calendarId=cal_id, eventId=ev_id).execute()
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error cancelando la cita ({cal_id}): {e}")
        return {"ok": False, "error": "No se pudo cancelar en la agenda."}

    logger.info(f"Cita cancelada: {ev.get('summary','')} ({cal_id}/{ev_id})")
    return {"ok": True, "cuando": cuando}
