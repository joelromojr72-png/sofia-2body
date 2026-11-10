# agent/consola.py — API de la APEX Console (bandeja en vivo para el equipo)
"""
La consola (apex-console-*.vercel.app) lee y escribe aquí:

  GET  /console/chats              lista de conversaciones (WhatsApp y Messenger), la más reciente primero
  GET  /console/chat?tel=...       mensajes completos de una conversación + datos de la clienta y sus citas
  POST /console/enviar             {tel, texto}: manda un mensaje COMO EQUIPO (Sofía se calla en ese chat)
  POST /console/pausa              {tel, pausar}: apaga/prende a Sofía SOLO en ese chat
  GET  /console/estado             salud: Sofía, agenda, horario del equipo

Todo pide el encabezado `X-Console-Key` = variable CONSOLE_KEY (no es la ADMIN_KEY).
"""

import asyncio
import logging
import os
from datetime import datetime

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy import String, func, select
from sqlalchemy.orm import Mapped, mapped_column

from agent import memory
from agent.memory import Base, Lead, Mensaje, OrigenAnuncio, ahora, async_session

logger = logging.getLogger("agentkit")
router = APIRouter(prefix="/console", tags=["consola"])


class ChatPausa(Base):
    """Chats donde el equipo apagó a Sofía desde la consola (ella no contesta ahí)."""

    __tablename__ = "chat_pausa"

    telefono: Mapped[str] = mapped_column(String(50), primary_key=True)
    por: Mapped[str] = mapped_column(String(60), default="")
    desde: Mapped[datetime] = mapped_column(default=ahora)


async def chat_pausado(telefono: str) -> bool:
    try:
        async with async_session() as s:
            return (await s.get(ChatPausa, telefono)) is not None
    except Exception:  # noqa: BLE001  (si la tabla falla, Sofía sigue como siempre)
        return False


def _autorizar(clave: str | None) -> None:
    esperada = os.getenv("CONSOLE_KEY") or ""
    if not esperada or (clave or "") != esperada:
        raise HTTPException(status_code=401, detail="clave de consola inválida")


def _autor(role: str, texto: str) -> tuple[str, str]:
    """(autor, texto limpio): cliente · sofia · equipo · sistema."""
    if role == "user":
        return "cliente", texto
    t = texto or ""
    if t.startswith("[Equipo] "):
        return "equipo", t[len("[Equipo] "):]
    if t.startswith("[Recordatorio automático]") or t.startswith("Hola ") and "te recordamos tu cita" in t:
        return "sistema", t
    if t.startswith("[El equipo envió"):
        return "equipo", t
    return "sofia", t


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


@router.get("/estado")
async def estado(x_console_key: str | None = Header(default=None)):
    _autorizar(x_console_key)
    from agent import calendar_tool
    from agent.brain import estado_cerebro
    from agent.main import sofia_debe_responder, sofia_pausada

    return {
        "sofia_global_pausada": sofia_pausada(),
        "sofia_turno": sofia_debe_responder(),  # False = horario del equipo humano
        "agenda": calendar_tool.calendario_configurado(),
        "cerebro": estado_cerebro(),
        "ahora": ahora().isoformat(),
    }


@router.get("/chats")
async def chats(limite: int = 150, x_console_key: str | None = Header(default=None)):
    """Conversaciones más recientes con su último mensaje y lo que el equipo necesita ver."""
    _autorizar(x_console_key)
    limite = max(10, min(limite, 400))
    async with async_session() as s:
        ult_ids = (
            select(Mensaje.telefono, func.max(Mensaje.id).label("mid"), func.count(Mensaje.id).label("n"))
            .group_by(Mensaje.telefono)
            .order_by(func.max(Mensaje.id).desc())
            .limit(limite)
            .subquery()
        )
        filas = (await s.execute(
            select(Mensaje, ult_ids.c.n).join(ult_ids, Mensaje.id == ult_ids.c.mid).order_by(Mensaje.id.desc())
        )).all()
        tels = [m.telefono for m, _ in filas]
        leads = {l.telefono: l for l in (await s.execute(select(Lead).where(Lead.telefono.in_(tels)))).scalars()}
        pausas = {p.telefono for p in (await s.execute(select(ChatPausa).where(ChatPausa.telefono.in_(tels)))).scalars()}
        origenes: dict[str, str] = {}
        for o in (await s.execute(
            select(OrigenAnuncio).where(OrigenAnuncio.telefono.in_(tels)).order_by(OrigenAnuncio.creado_en)
        )).scalars():
            origenes[o.telefono] = o.titulo or o.ad_id
    salida = []
    for m, n in filas:
        lead = leads.get(m.telefono)
        autor, texto = _autor(m.role, m.content)
        salida.append({
            "tel": m.telefono,
            "canal": "messenger" if m.telefono.startswith("msgr:") else "whatsapp",
            "nombre": (lead.nombre if lead else "") or "",
            "interes": (lead.interes if lead else "") or "",
            "estado": (lead.estado if lead else "") or "",
            "anuncio": origenes.get(m.telefono, ""),
            "ultimo": {"id": m.id, "autor": autor, "texto": texto[:220], "ts": _iso(m.timestamp)},
            "mensajes": n,
            # La última palabra la tiene la clienta: alguien debe contestar.
            "pendiente": autor == "cliente",
            "sofia_pausada": m.telefono in pausas,
            "equipo_en": _iso(lead.ultimo_equipo_en) if lead else None,
        })
    return {"chats": salida, "ahora": ahora().isoformat()}


@router.get("/chat")
async def chat(tel: str, desde_id: int = 0, x_console_key: str | None = Header(default=None)):
    """Mensajes de una conversación (los 400 más recientes, o solo los nuevos con desde_id)."""
    _autorizar(x_console_key)
    async with async_session() as s:
        q = select(Mensaje).where(Mensaje.telefono == tel)
        if desde_id:
            q = q.where(Mensaje.id > desde_id).order_by(Mensaje.id)
            msgs = list((await s.execute(q.limit(200))).scalars())
        else:
            msgs = list(reversed(list((await s.execute(q.order_by(Mensaje.id.desc()).limit(400))).scalars())))
        lead = await s.get(Lead, tel)
        pausa = await s.get(ChatPausa, tel)
        origen = (await s.execute(
            select(OrigenAnuncio).where(OrigenAnuncio.telefono == tel).order_by(OrigenAnuncio.creado_en.desc()).limit(1)
        )).scalars().first()
    items = []
    for m in msgs:
        autor, texto = _autor(m.role, m.content)
        items.append({"id": m.id, "autor": autor, "texto": texto, "ts": _iso(m.timestamp)})
    out = {
        "tel": tel,
        "canal": "messenger" if tel.startswith("msgr:") else "whatsapp",
        "mensajes": items,
        "sofia_pausada": pausa is not None,
        "lead": {
            "nombre": lead.nombre, "interes": lead.interes, "estado": lead.estado,
            "creado": _iso(lead.creado_en), "equipo_en": _iso(lead.ultimo_equipo_en),
        } if lead else None,
        "anuncio": (origen.titulo or origen.ad_id) if origen else "",
    }
    if not desde_id and not tel.startswith("msgr:"):
        out["citas"] = await _citas(tel, lead.nombre if lead else "")
    return out


async def _citas(tel: str, nombre: str) -> list[dict]:
    from agent import calendar_tool

    try:
        nombres = await memory.nombres_de(tel)
        if nombre:
            nombres = list({*nombres, nombre})
        citas = await asyncio.wait_for(asyncio.to_thread(calendar_tool.buscar_citas_de, tel, 60, nombres), 12)
        return [{"cuando": c["cuando"], "servicio": c["servicio"], "titulo": c["titulo"],
                 "por_confirmar": c.get("de_sofia", False)} for c in citas]
    except Exception as e:  # noqa: BLE001
        logger.error(f"Consola: no se pudieron leer las citas de …{tel[-4:]}: {e}")
        return []


@router.post("/enviar")
async def enviar(request: Request, x_console_key: str | None = Header(default=None)):
    """Manda un mensaje como EQUIPO. Sofía se calla en ese chat (igual que cuando escriben desde el cel)."""
    _autorizar(x_console_key)
    datos = (await request.json()) or {}
    tel = (datos.get("tel") or "").strip()
    texto = (datos.get("texto") or "").strip()
    if not tel or not texto:
        raise HTTPException(status_code=400, detail="falta tel o texto")
    if len(texto) > 3000:
        raise HTTPException(status_code=400, detail="mensaje demasiado largo")
    from agent import main as app_main

    prov = app_main.proveedor_messenger if tel.startswith("msgr:") else app_main.proveedor
    if prov is None:
        raise HTTPException(status_code=503, detail="canal no configurado")
    try:
        ok = await prov.enviar_mensaje(tel, texto)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Consola: error enviando a …{tel[-4:]}: {e}")
        ok = False
    if not ok:
        # Casi siempre: pasaron más de 24 h desde el último mensaje de la clienta (regla de Meta).
        raise HTTPException(status_code=409, detail=(
            "No se pudo enviar. Si la clienta no ha escrito en las últimas 24 h, WhatsApp solo "
            "permite plantillas: escríbele desde el celular del spa."))
    await memory.guardar_mensaje(tel, "assistant", "[Equipo] " + texto)
    try:
        await memory.registrar_outbound_equipo(tel)
    except Exception:  # noqa: BLE001
        pass
    logger.info(f"Consola (equipo) → …{tel[-4:]}: {texto[:60]}")
    return {"ok": True}


@router.post("/pausa")
async def pausa(request: Request, x_console_key: str | None = Header(default=None)):
    """{tel, pausar: true|false}: apaga o prende a Sofía SOLO en ese chat."""
    _autorizar(x_console_key)
    datos = (await request.json()) or {}
    tel = (datos.get("tel") or "").strip()
    if not tel:
        raise HTTPException(status_code=400, detail="falta tel")
    pausar = bool(datos.get("pausar"))
    async with async_session() as s:
        actual = await s.get(ChatPausa, tel)
        if pausar and actual is None:
            s.add(ChatPausa(telefono=tel, por=(datos.get("por") or "consola")[:60]))
        elif not pausar and actual is not None:
            await s.delete(actual)
        await s.commit()
    if pausar:
        try:
            await memory.pausar_seguimiento(tel)  # tampoco le manda recordatorios de venta
        except Exception:  # noqa: BLE001
            pass
    logger.info(f"Consola: Sofía {'PAUSADA' if pausar else 'reactivada'} en …{tel[-4:]}")
    return {"ok": True, "sofia_pausada": pausar}
