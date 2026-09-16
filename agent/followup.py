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
from datetime import datetime
from zoneinfo import ZoneInfo

from agent import memory

logger = logging.getLogger("agentkit")

TZ = ZoneInfo(os.getenv("FOLLOWUP_TZ") or "America/Mexico_City")


def habilitado() -> bool:
    return os.getenv("FOLLOWUP_ENABLED", "").strip().lower() in ("1", "true", "si", "yes", "on")


def _templates() -> list[str]:
    raw = os.getenv("FOLLOWUP_TEMPLATES", "seguimiento_1,seguimiento_2,seguimiento_3")
    return [t.strip() for t in raw.split(",") if t.strip()]


def _lang() -> str:
    return os.getenv("FOLLOWUP_LANG") or "es_MX"


def _en_horario(now: datetime | None = None) -> bool:
    """Plantillas (multi-día): lun-sáb, 10:00-19:00 hora de México."""
    now = now or datetime.now(TZ)
    if now.weekday() == 6:  # domingo
        return False
    return 10 <= now.hour < 19


def _en_horario_nudge(now: datetime | None = None) -> bool:
    """Toque de cierre dentro de 24h: 09:00-21:00 todos los días (más amplio, es reenganche)."""
    now = now or datetime.now(TZ)
    return 9 <= now.hour < 21


async def _ciclo_nudge(proveedor) -> int:
    """
    Toque de cierre DENTRO de la ventana de 24 h: mensaje LIBRE (sin plantilla) a quien
    dejó la charla a medias. Funciona sin esperar aprobación de plantillas.
    """
    from agent import brain, memory  # import diferido para evitar ciclos al cargar

    leads = await memory.leads_para_nudge()
    enviados = 0
    for l in leads:
        tel = l["telefono"]
        historial = await memory.obtener_historial(tel)
        texto = await brain.generar_seguimiento(historial)
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
    enviados = 0
    for l in leads:
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


async def loop_seguimiento(proveedor, intervalo_seg: int = 900) -> None:
    """Corre en segundo plano dentro del servidor. Revisa cada ~15 min."""
    if not habilitado():
        logger.info("Seguimiento multi-día DESACTIVADO (FOLLOWUP_ENABLED != true).")
        return
    if not hasattr(proveedor, "enviar_plantilla"):
        logger.warning("El proveedor no soporta plantillas: seguimiento multi-día no disponible.")
        return
    logger.info(
        "Seguimiento ACTIVADO — toque de cierre 24h (9-21h) + multi-día por plantilla "
        "(+24h/+3d/+7d, 10-19h lun-sáb)."
    )
    while True:
        try:
            # 1) Toque de cierre dentro de 24 h (mensaje libre, no necesita plantilla).
            if _en_horario_nudge():
                await _ciclo_nudge(proveedor)
            # 2) Seguimiento multi-día por plantilla aprobada (fuera de 24 h).
            if _en_horario():
                await _ciclo(proveedor)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error en el ciclo de seguimiento: {e}")
        await asyncio.sleep(intervalo_seg)
