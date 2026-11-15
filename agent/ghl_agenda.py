"""
Google Calendar → GHL: mueve la tarjeta de cada clienta según sus citas (también las que apunta
el equipo a mano), sin que nadie capture nada en GHL.

  • Valoración / Exp. Única próxima ............................ "Valoracion agendada"
  • Valoración / Exp. Única que ya pasó (últimos 7 días) ....... "Valoracion realizada"
  • Valoración / Exp. Única de hace más de 7 días, sin compra ... "Reactivacion"
  • Sesión pagada de un servicio (facial, corporal, masaje, depilación) ... "En tratamiento"

Las tarjetas nunca se regresan, y a quien ya compró no se le manda a Reactivación (ver
ghl.sincronizar). La clienta se identifica por su WhatsApp en la cita (las de Sofía) o por su
nombre en el directorio de Anegocios / leads (las que apunta el equipo).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("agentkit")

CADA_HORAS = float(os.getenv("GHL_AGENDA_CADA_HORAS") or "6")
DIAS_REALIZADA = 7

_GRATIS = re.compile(r"valoraci|exp\.?\s*[uú]nica|experiencia|cortes[ií]a|sin\s+costo|gratis", re.I)
_DESCARTAR = re.compile(r"cancel|no\s+asisti|no\s+vino|no\s+lleg|reagend|bloque|comida|junta|festivo|cerrad", re.I)
_PAGADOS = {"faciales", "corporales", "masajes", "depilacion"}
_STOP = {"de", "la", "del", "los", "las", "y", "sesion", "facial", "corporal", "depilacion", "masaje",
         "masajes", "valoracion", "experiencia", "unica", "exp", "cita", "por", "confirmar", "sin",
         "costo", "con", "paquete", "laser", "maderoterapia", "limpieza", "profunda"}

ultimo: dict = {}


def _norm(s: str) -> str:
    import unicodedata

    s = unicodedata.normalize("NFD", (s or "").lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def _toks(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", _norm(s)) if len(w) > 2 and w not in _STOP}


def _tel_de(texto: str) -> str:
    for run in re.findall(r"\d[\d\s\-+]{9,}", texto or ""):
        d = re.sub(r"\D", "", run)
        if len(d) >= 10:
            return d[-10:]
    return ""


def clasificar(ev: dict, ahora: datetime) -> str | None:
    """Etapa que indica UNA cita, o None si no dice nada (bloqueos, canceladas, juntas…)."""
    cal, titulo, desc = ev["cal"], ev["titulo"], ev["desc"]
    if "cancel" in cal or "festivo" in cal or _DESCARTAR.search(titulo):
        return None
    if not isinstance(ev["inicio"], datetime) or ev.get("todo_el_dia"):
        return None
    m = re.search(r"Servicio:\s*(.+)", desc or "")
    texto = titulo + " " + (m.group(1) if m else "")
    gratis = bool(_GRATIS.search(texto)) or "venta" in cal or "ex unica" in cal
    if gratis:
        if ev["inicio"] >= ahora:
            return "agendada"
        return "realizada" if ev["inicio"] >= ahora - timedelta(days=DIAS_REALIZADA) else "reactivacion"
    if cal in _PAGADOS:
        return "tratamiento"
    return None


_ORDEN = {"tratamiento": 4, "agendada": 3, "realizada": 2, "reactivacion": 1}


def decidir(etapas: list[str]) -> str | None:
    """Con varias citas de la misma clienta, gana la más avanzada / más reciente que importa."""
    return max(etapas, key=lambda e: _ORDEN[e]) if etapas else None


async def ciclo() -> dict:
    from agent import atribucion, ghl
    from agent.memory import directorio_clientes

    if not ghl.activo():
        return {"activo": False}
    eventos = await asyncio.to_thread(atribucion._leer_agenda, 60, 60)
    directorio = await directorio_clientes()
    indice = [(_toks(n), t) for n, t in directorio if t and len(_toks(n)) >= 2]
    ahora = datetime.now(timezone.utc)

    por_tel: dict[str, list[str]] = {}
    sin_clienta = 0
    for ev in eventos:
        etapa = clasificar(ev, ahora)
        if not etapa:
            continue
        tel = _tel_de(ev["titulo"] + " " + ev["desc"])
        if not tel:
            nombre_tk = _toks(re.split(r" - |/|\(", ev["titulo"])[0])
            cands = {t for tk, t in indice if len(tk & nombre_tk) >= 2}
            tel = cands.pop() if len(cands) == 1 else ""
        if not tel:
            sin_clienta += 1
            continue
        por_tel.setdefault(tel, []).append(etapa)

    from collections import Counter

    cuenta: Counter = Counter()
    for tel, etapas in por_tel.items():
        etapa = decidir(etapas)
        try:
            await ghl.sincronizar("521" + tel, etapa)
            cuenta[etapa] += 1
        except Exception as e:  # noqa: BLE001
            cuenta["errores"] += 1
            logger.warning(f"[Agenda→GHL] {tel[-4:]}: {e}")
        await asyncio.sleep(0.3)
    r = {"citas": len(eventos), "clientas": len(por_tel), "sin_identificar": sin_clienta, **dict(cuenta)}
    ultimo.clear()
    ultimo.update(r)
    return r


async def loop() -> None:
    from agent import ghl

    if not ghl.activo():
        return
    await asyncio.sleep(120)
    logger.info(f"[Agenda→GHL] cada {CADA_HORAS:g} h")
    while True:
        try:
            logger.info(f"[Agenda→GHL] {await ciclo()}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Agenda→GHL] ciclo falló: {e}")
        await asyncio.sleep(CADA_HORAS * 3600)
