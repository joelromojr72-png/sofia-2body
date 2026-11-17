"""
Espejo de los chats de WhatsApp en GoHighLevel → Conversaciones.

El número de WhatsApp sigue siendo de Sofía (Meta Cloud API); NO se conecta a GHL (eso le
quitaría el número a Sofía). Lo que hace este módulo: cada mensaje que se guarda en el historial
(de la clienta, de Sofía o del equipo) se copia a la conversación de esa clienta en GHL como
mensaje de WhatsApp — entrante si lo escribió ella, saliente si lo escribió Sofía o el equipo —
con su hora original. Así el equipo ve todo el historial en GHL junto a la tarjeta y las citas.

Contestar se sigue haciendo desde la Bandeja (o el WhatsApp del negocio): lo que se escribe en
GHL en un chat de WhatsApp NO le llega a la clienta (GHL no tiene el número).

Corre en segundo plano con una sola fila (respeta el orden de los mensajes). Si GHL falla, se
registra y se sigue: Sofía nunca se detiene por esto. Apagar: GHL_ESPEJO_WA=0.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

import httpx

logger = logging.getLogger("agentkit")

_fila: asyncio.Queue | None = None
_trabajador: asyncio.Task | None = None
_conv: dict[str, str] = {}  # telefono → id de la conversación en GHL
estado: dict = {"copiados": 0, "errores": 0, "relleno": {}}


def activo() -> bool:
    from agent import ghl

    return ghl.activo() and (os.getenv("GHL_ESPEJO_WA", "1").strip() not in ("0", "false", "no"))


def _texto(role: str, content: str) -> tuple[str, str] | None:
    """(dirección, texto para GHL) o None si no se copia."""
    from agent.consola import _autor_por

    autor, texto, por = _autor_por(role, content or "")
    texto = (texto or "").strip()
    if not texto:
        return None
    if autor == "cliente":
        return "inbound", texto
    if autor == "equipo":
        return "outbound", f"👩 {('Equipo · ' + por) if por else 'Equipo'}: {texto}"
    if autor == "sistema":
        return "outbound", f"⏰ Automático: {texto}"
    return "outbound", f"🤖 Sofía: {texto}"


async def _conversacion(c: httpx.AsyncClient, telefono: str) -> str:
    if telefono in _conv:
        return _conv[telefono]
    from agent import ghl

    contacto_id = await ghl._contacto(c, telefono, "")
    # Crear la conversación; si ya existe, GHL responde 400 con su conversationId.
    r = await c.post("/conversations/", json={"locationId": ghl._loc(), "contactId": contacto_id})
    datos = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    cid = (datos.get("conversation") or {}).get("id") or datos.get("conversationId")
    if not cid:
        r.raise_for_status()
        raise RuntimeError(f"GHL no dio conversación: {r.text[:200]}")
    _conv[telefono] = cid
    return cid


async def _copiar(c: httpx.AsyncClient, telefono: str, role: str, content: str, ts: datetime | None) -> None:
    t = _texto(role, content)
    if not t:
        return
    direccion, texto = t
    cuerpo = {
        "type": "WhatsApp",
        "conversationId": await _conversacion(c, telefono),
        "message": texto[:4000],
        "direction": direccion,
    }
    if ts:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        cuerpo["date"] = ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    r = await c.post("/conversations/messages/inbound", json=cuerpo)
    if r.status_code == 404 or (r.status_code == 400 and "conversation" in r.text.lower()):
        _conv.pop(telefono, None)  # la borraron en GHL: se crea de nuevo una vez
        cuerpo["conversationId"] = await _conversacion(c, telefono)
        r = await c.post("/conversations/messages/inbound", json=cuerpo)
    r.raise_for_status()


async def _trabajar() -> None:
    from agent import ghl

    async with ghl._cliente() as c:
        while True:
            telefono, role, content, ts = await _fila.get()
            try:
                await _copiar(c, telefono, role, content, ts)
                estado["copiados"] += 1
            except Exception as e:  # noqa: BLE001
                estado["errores"] += 1
                logger.warning(f"[GHL espejo] {telefono[-4:]}: {e}")
            finally:
                _fila.task_done()


def espejar(telefono: str, role: str, content: str, ts: datetime | None = None) -> None:
    """Encola la copia de un mensaje (no bloquea). Solo WhatsApp: Messenger ya entra solo a GHL."""
    global _fila, _trabajador
    if not telefono or telefono.startswith("msgr:") or not activo():
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    if _fila is None:
        _fila = asyncio.Queue(maxsize=20000)
    if _trabajador is None or _trabajador.done():
        _trabajador = asyncio.get_running_loop().create_task(_trabajar())
    try:
        _fila.put_nowait((telefono, role, content, ts))
    except asyncio.QueueFull:
        estado["errores"] += 1


async def rellenar(dias: int = 14, hasta_id: int | None = None) -> dict:
    """
    Copia el historial de los últimos `dias` (una sola vez) para que GHL no arranque vacío.
    Solo mensajes con id <= hasta_id (los posteriores ya se copian en vivo).
    """
    from datetime import timedelta

    from sqlalchemy import select

    from agent.memory import Mensaje, ahora, async_session

    if estado["relleno"].get("corriendo"):
        return estado["relleno"]
    estado["relleno"] = {"corriendo": True, "encolados": 0}
    desde = ahora() - timedelta(days=dias)
    async with async_session() as s:
        q = select(Mensaje).where(Mensaje.timestamp >= desde).order_by(Mensaje.telefono, Mensaje.id)
        if hasta_id:
            q = q.where(Mensaje.id <= hasta_id)
        filas = (await s.execute(q)).scalars().all()
    for m in filas:
        espejar(m.telefono, m.role, m.content, m.timestamp)
        estado["relleno"]["encolados"] += 1
        if estado["relleno"]["encolados"] % 200 == 0:
            await asyncio.sleep(0)
    estado["relleno"]["corriendo"] = False
    return estado["relleno"]


def pendientes() -> int:
    return _fila.qsize() if _fila else 0
