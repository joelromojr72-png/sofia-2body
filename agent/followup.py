# agent/followup.py — Motor de seguimiento multi-día (reactivación de leads)
# Generado por AgentKit

"""
Sofía como cerradora que NO se rinde: reactiva a los leads que mostraron interés pero
no agendaron, aun días después.

REGLA DE WHATSAPP (clave): fuera de las 24 h desde el último mensaje del cliente SOLO se
pueden mandar PLANTILLAS aprobadas por Meta. Por eso el seguimiento usa plantillas
(`proveedor.enviar_plantilla`). Cuando el cliente responde, Sofía (modo conversación) toma
el control y cierra.

SEGURIDAD DEL NÚMERO: el spam baja la calidad del número y puede banearlo. Por eso:
  - Máximo 3 toques (cadencia +24 h, +3 días, +7 días — definida en memory._CADENCIA_HORAS).
  - Solo en horario diurno (10:00-19:00, lun-sáb).
  - Respeta opt-out y a quien ya agendó.
  - Nace DESACTIVADO: se enciende con FOLLOWUP_ENABLED=true y requiere plantillas aprobadas.
"""

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agent import memory

logger = logging.getLogger("agentkit")

TZ = ZoneInfo(os.getenv("FOLLOWUP_TZ") or "America/Mexico_City")


def habilitado() -> bool:
    return os.getenv("FOLLOWUP_ENABLED", "").strip().lower() in ("1", "true", "si", "yes", "on")


def _pausada() -> bool:
    """Interruptor global de pausa (SOFIA_PAUSADA): durante pruebas no se envía seguimiento."""
    return os.getenv("SOFIA_PAUSADA", "").strip().lower() in ("1", "true", "si", "sí", "yes", "on")


def _templates() -> list[str]:
    raw = os.getenv("FOLLOWUP_TEMPLATES", "seguimiento_1,seguimiento_2,seguimiento_3")
    return [t.strip() for t in raw.split(",") if t.strip()]


def _lang() -> str:
    return os.getenv("FOLLOWUP_LANG") or "es_MX"


def _sofia_de_turno(now: datetime | None = None) -> bool:
    """
    True si Sofía está de turno (fuera del horario del equipo humano). El seguimiento
    solo escribe cuando ella atiende, para no encimarse con el equipo en vivo. Si no se
    puede determinar, deja pasar (como antes) para no bloquear el reenganche.
    """
    try:
        from agent.main import sofia_debe_responder  # import diferido (evita ciclo)

        return sofia_debe_responder(now)
    except Exception:  # noqa: BLE001
        return True


def _en_horario(now: datetime | None = None) -> bool:
    """Plantillas (multi-día): lun-sáb 10:00-19:00 y SOLO en el turno de Sofía."""
    now = now or datetime.now(TZ)
    if now.weekday() == 6:  # domingo
        return False
    if not (10 <= now.hour < 19):
        return False
    return _sofia_de_turno(now)


def _en_horario_nudge(now: datetime | None = None) -> bool:
    """Toque de cierre (24h): 09:00-21:00 y SOLO en el turno de Sofía (fuera del horario del equipo)."""
    now = now or datetime.now(TZ)
    if not (9 <= now.hour < 21):
        return False
    return _sofia_de_turno(now)


async def _telefonos_con_cita() -> set[str] | None:
    """Teléfonos (10 dígitos) con cita en el calendario. None si no se pudo leer."""
    try:
        from agent import calendar_tool

        if not calendar_tool.calendario_configurado():
            return set()
        return await asyncio.to_thread(calendar_tool.telefonos_con_cita)
    except Exception as e:  # noqa: BLE001
        logger.error(f"No se pudo leer el calendario para el seguimiento: {e}")
        return None


async def _puede_escribirle(tel: str, con_cita: set[str] | None, para: str) -> bool:
    """
    Último candado antes de un mensaje proactivo: el lead sigue activo, nadie del equipo le
    escribió y NO tiene cita en el calendario. Si ya tiene cita, se marca como agendado.
    """
    if not await memory.lead_sigue_elegible(tel, para):
        return False
    if con_cita is None:
        return False  # sin calendario no sabemos si ya agendó: mejor no escribir
    if "".join(c for c in tel if c.isdigit())[-10:] in con_cita:
        await memory.marcar_lead_agendado(tel)
        logger.info(f"Seguimiento cancelado: {tel} ya tiene cita en el calendario")
        return False
    return True


async def _ciclo_nudge(proveedor) -> int:
    """
    Toque de cierre DENTRO de la ventana de 24 h: mensaje LIBRE (sin plantilla) a quien
    dejó la charla a medias. Funciona sin esperar aprobación de plantillas.
    """
    from agent import brain, memory  # import diferido para evitar ciclos al cargar

    leads = await memory.leads_para_nudge()
    if not leads:
        return 0
    con_cita = await _telefonos_con_cita()
    enviados = 0
    for l in leads:
        tel = l["telefono"]
        if not await _puede_escribirle(tel, con_cita, "nudge"):
            continue
        historial = await memory.obtener_historial(tel)
        texto = await brain.generar_seguimiento(historial)
        # Re-chequeo justo antes de enviar: generar el texto tarda unos segundos y en ese
        # lapso pudo escribir alguien del equipo.
        if texto and not await memory.lead_sigue_elegible(tel, "nudge"):
            continue
        if not texto:
            # no marcamos enviado: se reintenta el próximo ciclo
            continue
        try:
            ok = await proveedor.enviar_mensaje(tel, texto)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error enviando toque de cierre a {tel}: {e}")
            ok = False
        if ok:
            await memory.guardar_mensaje(tel, "assistant", texto)
            await memory.marcar_nudge_enviado(tel)
            enviados += 1
            logger.info(f"Toque de cierre (24h) enviado a {tel}: {texto[:60]}")
        await asyncio.sleep(1.5)
    if enviados:
        logger.info(f"Toques de cierre enviados este ciclo: {enviados}")
    return enviados


async def _ciclo(proveedor) -> int:
    templates = _templates()
    lang = _lang()
    con_nombre = os.getenv("FOLLOWUP_WITH_NAME", "1") == "1"
    leads = await memory.leads_para_seguimiento()
    if not leads:
        return 0
    con_cita = await _telefonos_con_cita()
    enviados = 0
    for l in leads:
        if not await _puede_escribirle(l["telefono"], con_cita, "seguimiento"):
            continue
        etapa = l["etapa"]
        if etapa >= len(templates):
            continue
        tname = templates[etapa]
        primer_nombre = (l.get("nombre") or "").strip().split(" ")[0]
        params = [primer_nombre or "¿cómo estás?"] if con_nombre else None
        try:
            ok = await proveedor.enviar_plantilla(l["telefono"], tname, params, lang)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error enviando plantilla a {l['telefono']}: {e}")
            ok = False
        if ok:
            await memory.registrar_seguimiento_enviado(l["telefono"])
            enviados += 1
            logger.info(
                f"Seguimiento etapa {etapa + 1} enviado a {l['telefono']} (plantilla {tname})"
            )
        else:
            # No avanzamos la etapa si falló, para reintentar en el próximo ciclo.
            logger.warning(f"No se pudo enviar seguimiento a {l['telefono']} (plantilla {tname})")
        await asyncio.sleep(1.5)  # ritmo suave, no ráfaga
    if enviados:
        logger.info(f"Seguimientos enviados este ciclo: {enviados}")
    return enviados


# ─────────────────── Confirmación de citas (el día antes) ───────────────────


def _confirm_habilitado() -> bool:
    return os.getenv("CONFIRM_CITAS_ENABLED", "").strip().lower() in ("1", "true", "si", "yes", "on")


def _confirm_template() -> str:
    return os.getenv("CONFIRM_TEMPLATE") or "confirmacion_cita"


def _debe_confirmar_ahora(now: datetime | None = None) -> bool:
    """
    Recordatorio del día antes: es un aviso automático (no una charla), así que corre
    también en horario del equipo. Todos los días de 10:00 a 20:00.
    """
    now = now or datetime.now(TZ)
    return 10 <= now.hour < 20


_SERVICIO_POR_CALENDARIO = {
    "faciales": "sesión facial",
    "corporales": "sesión corporal",
    "depilacion": "sesión de depilación",
    "masajes": "masaje",
    "venta": "valoración",
    "principal": "cita",
}

# Palabras que en el título de la cita NO son parte del nombre de la clienta.
_NO_NOMBRE = {
    "CITA", "POR", "CONFIRMAR", "SESION", "SIN", "COSTO", "EXP", "UNICA", "EXPERIENCIA",
    "VALORACION", "FACIAL", "CORPORAL", "MASAJE", "DEPILACION", "LASER", "RA", "DA", "VA",
    "PRIMERA", "SEGUNDA", "GRATIS", "BODY", "PACHUCA", "PROMO", "PAQUETE", "Y", "DE", "LA",
    "DEL", "LOS", "LAS", "MA", "EMSCULPT", "HIFU", "HIDROFACIAL", "LIMPIEZA",
}


def _norm(s: str) -> str:
    import unicodedata

    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^A-Z ]", " ", s.upper())


def _tokens_nombre(texto: str) -> list[str]:
    """Tokens del nombre de la clienta en un título de cita (sin servicio, paréntesis ni números)."""
    texto = re.sub(r"\(.*?\)", " ", texto or "")
    mejor: list[str] = []
    for seg in re.split(r"\s[-.]\s|/|\|", texto):
        toks = [t for t in _norm(seg).split() if len(t) > 1 and t not in _NO_NOMBRE]
        if len(toks) > len(mejor):
            mejor = toks
    return mejor


def _token_igual(a: str, b: str) -> bool:
    return a == b or (min(len(a), len(b)) >= 5 and (a.startswith(b) or b.startswith(a)))


def _buscar_telefono(tokens: list[str], directorio: list[tuple[str, str]]) -> str | None:
    """
    Teléfono por nombre: todos los tokens del título (mínimo 2, p. ej. nombre + apellido)
    deben estar en el nombre del directorio, en cualquier orden. Solo si la coincidencia es
    única y el teléfono no es compartido por personas distintas (números 'comodín').
    """
    if len(tokens) < 2:
        return None
    por_tel: dict[str, list[set[str]]] = {}
    for nombre, tel in directorio:
        por_tel.setdefault(tel, []).append(set(_norm(nombre).split()))
    candidatos = set()
    for tel, nombres in por_tel.items():
        for dt in nombres:
            if all(any(_token_igual(t, d) for d in dt) for t in tokens):
                candidatos.add(tel)
    if len(candidatos) != 1:
        return None
    tel = candidatos.pop()
    nombres = por_tel[tel]
    for i in range(len(nombres)):
        for j in range(i + 1, len(nombres)):
            if len(nombres[i] & nombres[j]) < 2:
                return None  # el mismo número es de personas distintas: no se usa
    return tel


def _servicio_de(cita: dict) -> str:
    desc, titulo = cita["descripcion"], cita["titulo"]
    m = re.search(r"Servicio:\s*(.+)", desc)
    if m:
        return m.group(1).strip()
    t = _norm(titulo)
    if re.search(r"\bEXP\b|EXPERIENCIA", t):
        base = "Experiencia Única"
        if "FACIAL" in t:
            return base + " facial"
        if "CORPORAL" in t:
            return base + " corporal"
        return base
    for palabra, servicio in (("FACIAL", "sesión facial"), ("CORPORAL", "sesión corporal"),
                              ("DEPIL", "sesión de depilación"), ("MASAJE", "masaje")):
        if palabra in t:
            return servicio
    return _SERVICIO_POR_CALENDARIO.get(cita["calendario"], "cita")


async def _citas_de_manana_con_telefono() -> list[dict]:
    """
    Citas de MAÑANA (de Sofía y del equipo) agrupadas por clienta, con su teléfono:
    1) 'WhatsApp: +...' en la descripción (citas de Sofía), 2) un número en el título o la
    descripción, 3) el directorio de clientas por nombre. Las que no tienen teléfono se omiten.
    """
    from agent import calendar_tool, memory

    manana = (datetime.now(TZ) + timedelta(days=1)).date()
    citas = await asyncio.to_thread(calendar_tool.eventos_con_hora, manana)
    directorio = await memory.directorio_clientes()
    por_tel: dict[str, dict] = {}
    for c in citas:
        titulo, desc = c["titulo"], c["descripcion"]
        if re.search(r"cancel|bloque|no disponible", titulo, re.I):
            continue
        m = re.search(r"WhatsApp:\s*\+?([0-9]{10,15})", desc)
        if m:
            tel = m.group(1)[-10:]
        else:
            nums = re.findall(r"(?:\d[\s\-]?){10,13}", titulo + " " + desc)
            tel = re.sub(r"\D", "", nums[0])[-10:] if nums else ""
        if len(tel) < 10:
            tel = _buscar_telefono(_tokens_nombre(titulo), directorio) or ""
        if not tel:
            continue
        m_nom = re.search(r"Cliente:\s*(.+)", desc)
        toks = _tokens_nombre(titulo)
        nombre = (m_nom.group(1).strip().split(" ")[0] if m_nom else (toks[0] if toks else "")).title()
        g = por_tel.setdefault(tel, {"telefono": tel, "nombre": nombre, "inicio": c["inicio"],
                                     "servicios": [], "eventos": []})
        g["inicio"] = min(g["inicio"], c["inicio"])
        g["nombre"] = g["nombre"] or nombre
        serv = _servicio_de(c)
        if serv not in g["servicios"]:
            g["servicios"].append(serv)
        g["eventos"].append(c["evento_id"])
    return list(por_tel.values())


async def _ciclo_confirmaciones(proveedor) -> int:
    """
    Recordatorio del día antes (plantilla aprobada) a TODAS las clientas con cita mañana,
    las haya agendado Sofía o el equipo. Una sola vez por clienta y día.
    """
    from agent import calendar_tool, memory  # import diferido

    template = _confirm_template()
    lang = _lang()
    manana = (datetime.now(TZ) + timedelta(days=1)).date()
    try:
        grupos = await _citas_de_manana_con_telefono()
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error leyendo citas de mañana para el recordatorio: {e}")
        return 0

    enviados = 0
    for g in grupos:
        tel10 = g["telefono"]
        if await memory.estado_lead(tel10) == "opt_out":
            continue
        dkey = f"conf:{tel10}:{manana.isoformat()}"
        if not await memory.marcar_evento_procesado(dkey):
            continue  # ya se le recordó esta fecha
        # Compatibilidad: citas que ya se confirmaron con la clave anterior (por evento).
        ya_confirmada = False
        for eid in g["eventos"]:
            if not await memory.marcar_evento_procesado(f"conf:{eid}"):
                ya_confirmada = True
        if ya_confirmada:
            continue
        params = [g["nombre"] or "💜", " y ".join(g["servicios"]), calendar_tool.cuando_legible(g["inicio"])]
        try:
            ok = await proveedor.enviar_plantilla("521" + tel10, template, params, lang)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error enviando recordatorio del día antes a {tel10}: {e}")
            ok = False
        if ok:
            enviados += 1
            logger.info(f"Recordatorio del día antes enviado a …{tel10[-4:]} ({params[1]}, {params[2]})")
        else:
            await memory.liberar_evento(dkey)  # soltar para reintentar el próximo ciclo
            for eid in g["eventos"]:
                await memory.liberar_evento(f"conf:{eid}")
        await asyncio.sleep(1.5)
    if enviados:
        logger.info(f"Recordatorios del día antes enviados este ciclo: {enviados}")
    return enviados


# ─────────────── Recordatorio ligero en charlas que lleva el equipo ───────────────


def _en_horario_nudge_equipo(now: datetime | None = None) -> bool:
    """El recordatorio ligero corre también en horario del equipo: todos los días 9:00-21:00."""
    now = now or datetime.now(TZ)
    return 9 <= now.hour < 21


async def _ciclo_nudge_equipo(proveedor) -> int:
    """
    Si la clienta no contestó el último mensaje del EQUIPO en ~3-4 h, Sofía manda UN solo
    recordatorio ligero que retoma ese mensaje (lee todo el chat, incluido lo del equipo).
    No escribe si la charla ya terminó de forma natural ni si la clienta ya tiene cita.
    """
    from agent import brain, memory  # import diferido

    leads = await memory.leads_para_nudge_equipo()
    if not leads:
        return 0
    con_cita = await _telefonos_con_cita()
    if con_cita is None:
        return 0  # sin agenda no sabemos si ya agendó: mejor no escribir
    enviados = 0
    for l in leads:
        tel = l["telefono"]
        if "".join(c for c in tel if c.isdigit())[-10:] in con_cita:
            await memory.marcar_nudge_equipo_enviado(tel)
            continue
        historial = await memory.obtener_historial_equipo(tel)
        if not historial:
            continue  # sin nada guardado de esta charla: no hay contexto para escribirle
        texto = await brain.generar_recordatorio_equipo(historial)
        if texto is None:
            continue  # falló la IA: se reintenta el próximo ciclo
        if texto == brain.NO_ENVIAR:
            await memory.marcar_nudge_equipo_enviado(tel)
            logger.info(f"Recordatorio ligero omitido (charla cerrada) …{tel[-4:]}")
            continue
        # Re-chequeo: mientras se redactaba pudo contestar la clienta o escribir el equipo.
        if not await memory.nudge_equipo_sigue_elegible(tel):
            continue
        try:
            ok = await proveedor.enviar_mensaje(tel, texto)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error enviando recordatorio ligero a {tel}: {e}")
            ok = False
        if ok:
            await memory.guardar_mensaje(tel, "assistant", texto)
            await memory.marcar_nudge_equipo_enviado(tel)
            enviados += 1
            logger.info(f"Recordatorio ligero (charla del equipo) enviado a …{tel[-4:]}: {texto[:60]}")
        await asyncio.sleep(1.5)
    return enviados


def _recordatorio_template() -> str:
    return os.getenv("RECORDATORIO_TEMPLATE") or "recordatorio_cita_hoy"


# Primeras visitas (las que más faltan): Experiencia Única y valoraciones.
_PRIMERA_VISITA = re.compile(r"exp\.?\s*[uú]nica|experiencia|valoraci[oó]n", re.I)


async def _ciclo_recordatorio_hoy(proveedor) -> int:
    """
    Recordatorio EL MISMO DÍA, 2-3 h antes, a las PRIMERAS visitas (Experiencia Única /
    valoración), las tome Sofía o el equipo. Plantilla aprobada con dirección y botón de
    "Cómo llegar". Una sola vez por cita. Es un aviso automático (no una charla), por eso
    corre también en horario del equipo, entre 7:00 y 19:00.
    """
    from agent import calendar_tool, memory  # import diferido

    ahora = datetime.now(TZ)
    if not (7 <= ahora.hour < 19):
        return 0
    try:
        svc = calendar_tool._obtener_servicio()
        eventos = calendar_tool._eventos_dia_todos(svc, ahora.date()) if svc else []
    except Exception as e:  # noqa: BLE001
        logger.error(f"Recordatorio del día: no se pudo leer la agenda: {e}")
        return 0

    enviados = 0
    for cal_id, ev in eventos:
        if ev.get("status") == "cancelled":
            continue
        ini = (ev.get("start") or {}).get("dateTime")
        if not ini:
            continue
        try:
            inicio = datetime.fromisoformat(ini).astimezone(TZ)
        except ValueError:
            continue
        faltan_h = (inicio - ahora).total_seconds() / 3600
        if not (2 <= faltan_h <= 3):
            continue
        titulo = ev.get("summary", "") or ""
        desc = ev.get("description", "") or ""
        if "cancel" in titulo.lower() or not (_PRIMERA_VISITA.search(titulo) or "Sofía" in desc):
            continue
        m = re.search(r"WhatsApp:\s*\+?([0-9]{10,15})", desc)
        if m:
            digitos = m.group(1)
        else:
            nums = re.findall(r"(?:\d[\s\-]?){10,13}", titulo + " " + desc)
            digitos = re.sub(r"\D", "", nums[0]) if nums else ""
        if len(digitos) < 10:
            continue  # sin teléfono no hay a quién avisar
        tel = "521" + digitos[-10:]
        clave = f"rec:{calendar_tool._componer_evento_id(cal_id, ev.get('id', ''))}"
        if not await memory.marcar_evento_procesado(clave):
            continue
        m_nom = re.search(r"Cliente:\s*(.+)", desc)
        nombre = (m_nom.group(1) if m_nom else re.split(r"\s[-/]\s|/", titulo)[0]).strip().split(" ")[0].title() or "Hola"
        t = titulo.lower()
        servicio = "Experiencia Única" if _PRIMERA_VISITA.search(t) and "valoraci" not in t else "valoración sin costo"
        if "facial" in t:
            servicio += " facial"
        elif "corporal" in t:
            servicio += " corporal"
        params = [nombre, inicio.strftime("%H:%M"), servicio]
        try:
            ok = await proveedor.enviar_plantilla(tel, _recordatorio_template(), params, _lang())
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error enviando recordatorio del día a {tel}: {e}")
            ok = False
        if ok:
            enviados += 1
            logger.info(f"Recordatorio del día enviado a {tel} ({nombre}, {inicio.strftime('%H:%M')}, {servicio})")
        else:
            await memory.liberar_evento(clave)  # p. ej. plantilla aún no aprobada: se reintenta
        await asyncio.sleep(1.5)
    return enviados


async def loop_seguimiento(proveedor, intervalo_seg: int = 900) -> None:
    """Corre en segundo plano dentro del servidor. Revisa cada ~15 min."""
    if not habilitado() and not _confirm_habilitado():
        logger.info("Seguimiento y confirmaciones DESACTIVADOS.")
        return
    if not hasattr(proveedor, "enviar_plantilla"):
        logger.warning("El proveedor no soporta plantillas: seguimiento/confirmaciones no disponibles.")
        return
    logger.info(
        "Seguimiento ACTIVADO — toque de cierre 24h (9-21h) + multi-día por plantilla "
        "(+24h/+3d/+7d, 10-19h lun-sáb), SOLO en el turno de Sofía (fuera del horario del equipo)."
        + (" + recordatorio el día antes a TODAS las citas (10-20h, también en horario del equipo)."
           if _confirm_habilitado() else "")
        + " + recordatorio ligero (1 vez) si la clienta no contesta al equipo en 3 h (9-21h)."
    )
    while True:
        try:
            if _pausada():
                # Pausa global para pruebas: no se manda ningún seguimiento/confirmación.
                await asyncio.sleep(intervalo_seg)
                continue
            if habilitado():
                # 1) Toque de cierre dentro de 24 h (mensaje libre, no necesita plantilla).
                if _en_horario_nudge():
                    await _ciclo_nudge(proveedor)
                # 2) Seguimiento multi-día por plantilla aprobada (fuera de 24 h).
                if _en_horario():
                    await _ciclo(proveedor)
                # 2b) Recordatorio ligero en charlas del equipo (clienta no contestó en 3 h).
                if _en_horario_nudge_equipo():
                    await _ciclo_nudge_equipo(proveedor)
            # 3) Recordatorio del día antes a todas las citas de mañana (plantilla).
            if _confirm_habilitado() and _debe_confirmar_ahora():
                await _ciclo_confirmaciones(proveedor)
            # 4) Recordatorio el mismo día (2-3 h antes) a primeras visitas.
            if _confirm_habilitado():
                await _ciclo_recordatorio_hoy(proveedor)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error en el ciclo de seguimiento: {e}")
        await asyncio.sleep(intervalo_seg)
