# agent/automatizaciones.py — Automatizaciones del embudo (21-nov-2026)

"""
Lo que faltaba del research de embudos (iCloud "2 Body Pachuca/Research embudos/"), leyendo la
misma agenda de Google que ya usan atribucion.py y ghl_agenda.py. Nadie captura nada a mano.

1) Meta sabe quién AGENDÓ (no solo quién compró): cuando una clienta que llegó por un anuncio
   tiene cita de valoración / Experiencia Única en la agenda (la apartó Sofía o el equipo), se
   manda el evento LeadSubmitted al dataset de WhatsApp con su ctwa_clid. Así Meta puede
   optimizar hacia chats que agendan, no solo hacia chats que escriben.
2) Rescate de faltas: si el equipo marca la cita de primera visita como "no asistió / no vino /
   no llegó", al día siguiente (10-19 h) sale la plantilla `cita_no_asistio` con botones
   "Reagendar mi cita" / "Ya no me interesa". Cuando contesta, Sofía toma la charla y reagenda.
3) Reseñas de Google: el día después de su PRIMERA sesión pagada, la plantilla `resena_google`
   con el botón al link oficial de reseñas. Una sola vez por clienta, nunca más.
4) Medición de asistencia: GET /reporte/asistencia — primeras visitas de los últimos N días,
   cuántas se marcaron como falta y cuántas ya compraron (venta = asistió seguro).

Todo respeta opt-out, solo manda plantillas aprobadas por Meta y cada aviso sale una sola vez
(tabla `auto_enviado`, permanente: la de eventos se limpia a los 7 días).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import DateTime, String, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from agent.memory import Base, Lead, async_session

logger = logging.getLogger("agentkit")

CADA_MIN = float(os.getenv("AUTOMATIZACIONES_CADA_MIN") or "120")
NO_ASISTIO = re.compile(r"no\s*asisti|no\s*vino|no\s*lleg|no\s*se\s*present|falt[oó]\b", re.I)
_CANCELADA = re.compile(r"cancel|reagend|bloque|comida|junta|festivo|cerrad", re.I)
PLANTILLA_FALTA = os.getenv("PLANTILLA_FALTA") or "cita_no_asistio"
PLANTILLA_RESENA = os.getenv("PLANTILLA_RESENA") or "resena_google"

ultimo: dict = {}


def activo(nombre: str) -> bool:
    """Cada automatización se puede apagar con AUTO_<NOMBRE>=0 (CAPI, FALTAS, RESENAS)."""
    return os.getenv(f"AUTO_{nombre}", "1").strip().lower() not in ("0", "false", "no", "off")


class AutoEnviado(Base):
    """Marca permanente de 'esto ya se mandó' (evento a Meta o plantilla a una clienta)."""

    __tablename__ = "auto_enviado"

    clave: Mapped[str] = mapped_column(String(200), primary_key=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


async def una_vez(clave: str) -> bool:
    """True la primera vez que se pide esta clave; False si ya se había hecho."""
    async with async_session() as s:
        s.add(AutoEnviado(clave=clave))
        try:
            await s.commit()
            return True
        except IntegrityError:
            await s.rollback()
            return False


async def soltar(clave: str) -> None:
    """Deshace la marca (falló el envío): se reintenta en la siguiente vuelta."""
    async with async_session() as s:
        await s.execute(delete(AutoEnviado).where(AutoEnviado.clave == clave))
        await s.commit()


async def _opt_out(tel_wa: str) -> bool:
    async with async_session() as s:
        lead = await s.get(Lead, tel_wa)
        return bool(lead and lead.estado == "opt_out")


# ── Lectura de la agenda ─────────────────────────────────────────────────────

def es_primera_visita(ev: dict) -> bool:
    from agent.ghl_agenda import _GRATIS

    m = re.search(r"Servicio:\s*(.+)", ev.get("desc") or "")
    texto = ev["titulo"] + " " + (m.group(1) if m else "")
    return bool(_GRATIS.search(texto)) or "venta" in ev["cal"] or "ex unica" in ev["cal"]


def es_pagada(ev: dict) -> bool:
    from agent.ghl_agenda import _PAGADOS

    return ev["cal"] in _PAGADOS and not es_primera_visita(ev)


def nombre_de(ev: dict) -> str:
    """Primer nombre de la clienta, o 'de nuevo' (la plantilla dice 'Hola {{1}}')."""
    m = re.search(r"Cliente:\s*(.+)", ev.get("desc") or "")
    if m:
        n = m.group(1).strip().split(" ")[0]
    else:
        from agent.followup import _tokens_nombre

        toks = _tokens_nombre(ev["titulo"])
        n = toks[0] if toks else ""
    return n.title() if len(n) >= 2 else "de nuevo"


def _servicio_falta(ev: dict) -> str:
    t = ev["titulo"].lower()
    if "valoraci" in t or "venta" in ev["cal"]:
        base = "valoración sin costo"
    else:
        base = "primera sesión sin costo"
    if "facial" in t:
        base += " facial"
    elif "corporal" in t:
        base += " corporal"
    return base


async def _con_telefono(eventos: list[dict]) -> list[tuple[str, dict]]:
    """(tel10, evento) por cada cita en la que se reconoce a la clienta (teléfono o nombre único)."""
    from agent.ghl_agenda import _tel_de, _toks
    from agent.memory import directorio_clientes

    directorio = await directorio_clientes()
    indice = [(_toks(n), t) for n, t in directorio if t and len(_toks(n)) >= 2]
    salida = []
    for ev in eventos:
        if ev.get("todo_el_dia") or not isinstance(ev.get("inicio"), datetime):
            continue
        tel = _tel_de(ev["titulo"] + " " + ev["desc"])
        if not tel:
            nombre_tk = _toks(re.split(r" - |/|\(", ev["titulo"])[0])
            cands = {t for tk, t in indice if len(tk & nombre_tk) >= 2}
            tel = cands.pop() if len(cands) == 1 else ""
        if tel:
            salida.append((tel[-10:], ev))
    return salida


# ── 1) Meta: LeadSubmitted cuando una clienta de anuncio agenda ─────────────────

async def reportar_cita_meta(tel10: str, ctwa_clid: str) -> tuple[bool, str]:
    from agent.atribucion import DATASET_ID, WABA_ID

    token = os.getenv("META_ACCESS_TOKEN", "")
    if not (token and WABA_ID and DATASET_ID and ctwa_clid):
        return False, "sin configuración o sin ctwa_clid"
    evento = {
        "event_name": "LeadSubmitted",
        "event_time": int(datetime.now(timezone.utc).timestamp()),
        "event_id": f"cita-{tel10}",
        "action_source": "business_messaging",
        "messaging_channel": "whatsapp",
        "user_data": {"whatsapp_business_account_id": WABA_ID, "ctwa_clid": ctwa_clid},
    }
    datos = {"data": [evento]}
    if os.getenv("META_TEST_EVENT_CODE", "").strip():
        datos["test_event_code"] = os.getenv("META_TEST_EVENT_CODE").strip()
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                f"https://graph.facebook.com/{os.getenv('META_API_VERSION') or 'v25.0'}/{DATASET_ID}/events",
                params={"access_token": token}, json=datos,
            )
        return r.status_code == 200, r.text[:300]
    except httpx.HTTPError as e:
        return False, str(e)


async def _paso_capi(citas: list[tuple[str, dict]]) -> int:
    from agent.atribucion import _clientas_de_anuncio

    fuentes = {re.sub(r"\D", "", t)[-10:]: info for t, info in (await _clientas_de_anuncio()).items()}
    enviados = 0
    for tel10, ev in citas:
        info = fuentes.get(tel10)
        if not info or not info.get("ctwa_clid") or not es_primera_visita(ev):
            continue
        if _CANCELADA.search(ev["titulo"]) or "cancel" in ev["cal"]:
            continue
        if info.get("fecha") and ev["inicio"] < info["fecha"] - timedelta(days=1):
            continue  # cita de antes de que llegara por el anuncio
        clave = f"capi-cita-{tel10}"
        if not await una_vez(clave):
            continue
        ok, detalle = await reportar_cita_meta(tel10, info["ctwa_clid"])
        if ok:
            enviados += 1
            logger.info(f"[Automatizaciones] Meta: LeadSubmitted (agendó) …{tel10[-4:]} anuncio {info.get('ad_id')}")
        else:
            await soltar(clave)
            logger.warning(f"[Automatizaciones] Meta no aceptó LeadSubmitted …{tel10[-4:]}: {detalle}")
    return enviados


# ── 2 y 3) Plantillas: rescate de faltas y reseñas ─────────────────────────────

async def _enviar(proveedor, tel10: str, plantilla: str, params: list[str], clave: str) -> bool:
    from agent.followup import _al_historial, _lang
    from agent.ghl_whatsapp import plantilla_aprobada

    tel_wa = "521" + tel10
    if not await plantilla_aprobada(plantilla) or await _opt_out(tel_wa):
        return False
    if not await una_vez(clave):
        return False
    try:
        ok = await proveedor.enviar_plantilla(tel_wa, plantilla, params, _lang())
    except Exception as e:  # noqa: BLE001
        logger.error(f"[Automatizaciones] {plantilla} a …{tel10[-4:]}: {e}")
        ok = False
    if not ok:
        await soltar(clave)
        return False
    texto = {
        PLANTILLA_FALTA: f"[Aviso automático] Hola {params[0]}, te esperábamos en tu cita de {params[-1]} y no "
                         "pudimos verte. ¿Te ayudamos a reagendarla? (botones: Reagendar mi cita / Ya no me interesa)",
        PLANTILLA_RESENA: f"[Aviso automático] Hola {params[0]}, gracias por visitarnos. ¿Nos regalas tu opinión "
                          "en Google? (botón: Dejar mi opinión)",
    }.get(plantilla, f"[Aviso automático: {plantilla}]")
    await _al_historial(tel_wa, texto)
    logger.info(f"[Automatizaciones] {plantilla} enviada a …{tel10[-4:]}")
    return True


def _en_horario(now: datetime) -> bool:
    return now.weekday() != 6 and 10 <= now.hour < 19


async def _paso_faltas(proveedor, citas: list[tuple[str, dict]], ahora: datetime) -> int:
    enviados = 0
    for tel10, ev in citas:
        horas = (ahora - ev["inicio"]).total_seconds() / 3600
        if not (2 <= horas <= 36) or not es_primera_visita(ev) or not NO_ASISTIO.search(ev["titulo"]):
            continue
        clave = f"falta-{tel10}-{ev['inicio'].date().isoformat()}"
        if await _enviar(proveedor, tel10, PLANTILLA_FALTA, [nombre_de(ev), _servicio_falta(ev)], clave):
            enviados += 1
        await asyncio.sleep(1.5)
    return enviados


async def _paso_resenas(proveedor, citas: list[tuple[str, dict]], ahora: datetime) -> int:
    # Solo la PRIMERA sesión pagada que se ve en la agenda: a quien ya tiene sesiones pagadas
    # de antes no se le pide (ya la conocemos y la clave es por clienta, una sola vez).
    pagadas: dict[str, list[dict]] = {}
    for tel10, ev in citas:
        if es_pagada(ev) and not _CANCELADA.search(ev["titulo"]) and not NO_ASISTIO.search(ev["titulo"]):
            pagadas.setdefault(tel10, []).append(ev)
    enviados = 0
    for tel10, evs in pagadas.items():
        evs.sort(key=lambda e: e["inicio"])
        primera = evs[0]
        horas = (ahora - primera["inicio"]).total_seconds() / 3600
        if not (16 <= horas <= 52):
            continue  # el día siguiente a su primera sesión pagada
        if await _enviar(proveedor, tel10, PLANTILLA_RESENA, [nombre_de(primera)], f"resena-{tel10}"):
            enviados += 1
        await asyncio.sleep(1.5)
    return enviados


# ── 4) Asistencia ─────────────────────────────────────────────────────────────

async def reporte_asistencia(dias: int = 30) -> dict:
    from agent.atribucion import _leer_agenda

    ahora = datetime.now(timezone.utc)
    eventos = await asyncio.to_thread(_leer_agenda, max(dias, 1) + 45, 1)
    citas = await _con_telefono(eventos)
    compras: dict[str, datetime] = {}
    for tel10, ev in citas:
        if es_pagada(ev) and not NO_ASISTIO.search(ev["titulo"]):
            compras[tel10] = min(compras.get(tel10, ev["inicio"]), ev["inicio"])
    primeras = [(t, ev) for t, ev in citas
                if es_primera_visita(ev) and ahora - timedelta(days=dias) <= ev["inicio"] < ahora
                and not _CANCELADA.search(ev["titulo"])]
    sin_identificar = sum(1 for ev in eventos if es_primera_visita(ev)
                          and ahora - timedelta(days=dias) <= ev["inicio"] < ahora) - len(primeras)
    faltas = [t for t, ev in primeras if NO_ASISTIO.search(ev["titulo"])]
    compraron = [t for t, ev in primeras if t in compras and compras[t] >= ev["inicio"] - timedelta(days=1)]
    total = len(primeras)
    return {
        "dias": dias,
        "primeras_visitas": total,
        "marcadas_como_falta": len(faltas),
        "compraron_despues": len(set(compraron)),
        "tasa_falta_marcada": round(len(faltas) / total, 3) if total else None,
        "tasa_compra": round(len(set(compraron)) / total, 3) if total else None,
        "sin_identificar": max(0, sin_identificar),
        "nota": ("Una falta solo cuenta si el equipo la marca en el título de la cita "
                 "('no asistió', 'no vino', 'no llegó'). Sin esa marca no hay forma de saberlo."),
    }


# ── Ciclo ─────────────────────────────────────────────────────────────────────

async def ciclo(proveedor) -> dict:
    from agent.atribucion import _leer_agenda

    ahora = datetime.now(timezone.utc)
    local = datetime.now(_tz())
    # 120 días atrás: para saber si una sesión pagada es de verdad la PRIMERA de la clienta.
    eventos = await asyncio.to_thread(_leer_agenda, 120, 60)
    citas = await _con_telefono(eventos)
    r = {"citas": len(citas)}
    if activo("CAPI"):
        r["capi_agendaron"] = await _paso_capi(citas)
    if _en_horario(local) and hasattr(proveedor, "enviar_plantilla"):
        if activo("FALTAS"):
            r["faltas_rescatadas"] = await _paso_faltas(proveedor, citas, ahora)
        if activo("RESENAS"):
            r["resenas_pedidas"] = await _paso_resenas(proveedor, citas, ahora)
    ultimo.clear()
    ultimo.update(r, en=local.isoformat(timespec="minutes"))
    return r


def _tz():
    from zoneinfo import ZoneInfo

    return ZoneInfo(os.getenv("FOLLOWUP_TZ") or "America/Mexico_City")


async def loop(proveedor) -> None:
    from agent.followup import _pausada

    await asyncio.sleep(180)
    logger.info(f"[Automatizaciones] activas cada {CADA_MIN:g} min (CAPI agendó, rescate de faltas, reseñas)")
    while True:
        try:
            if not _pausada():
                logger.info(f"[Automatizaciones] {await ciclo(proveedor)}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Automatizaciones] ciclo falló: {e}")
        await asyncio.sleep(CADA_MIN * 60)
