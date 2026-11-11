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
import hmac
import os
from datetime import datetime, timezone

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


def pausa_vence(desde: datetime) -> datetime:
    """
    Hasta cuándo dura un "apagar a Sofía en este chat": cubre el SIGUIENTE turno de Sofía y se
    vence solo cuando vuelve el equipo. Así nunca se queda apagada para siempre en un chat y
    Sofía sigue cubriendo su horario de siempre (noches, sábado tarde, domingo) al día siguiente.
    """
    from datetime import timedelta

    from agent.main import TZ_NEGOCIO, sofia_debe_responder

    t = desde.astimezone(TZ_NEGOCIO) if desde.tzinfo else desde.replace(tzinfo=timezone.utc).astimezone(TZ_NEGOCIO)
    t = t.replace(second=0, microsecond=0)
    vio_turno_sofia = False
    for _ in range(8 * 24 * 4):  # pasos de 15 min, máx. 8 días
        t += timedelta(minutes=15)
        if sofia_debe_responder(t):
            vio_turno_sofia = True
        elif vio_turno_sofia:
            return t.replace(minute=(t.minute // 15) * 15)
    return t


def _vigente(p: "ChatPausa | None") -> bool:
    if p is None:
        return False
    desde = p.desde if p.desde.tzinfo else p.desde.replace(tzinfo=timezone.utc)
    return ahora() < pausa_vence(desde)


async def chat_pausado(telefono: str) -> bool:
    try:
        async with async_session() as s:
            p = await s.get(ChatPausa, telefono)
            if p is not None and not _vigente(p):
                await s.delete(p)  # ya volvió el equipo: la pausa se vence sola
                await s.commit()
                logger.info(f"Consola: pausa vencida en …{telefono[-4:]}, Sofía vuelve a su horario")
                return False
            return p is not None
    except Exception:  # noqa: BLE001  (si la tabla falla, Sofía sigue como siempre)
        return False


def _usuarios() -> dict[str, str]:
    """Accesos personales: CONSOLE_USUARIOS = "clave1=Nombre Apellido;clave2=Otro Nombre"."""
    out: dict[str, str] = {}
    for parte in (os.getenv("CONSOLE_USUARIOS") or "").split(";"):
        if "=" in parte:
            clave, nombre = parte.split("=", 1)
            if clave.strip() and nombre.strip():
                out[clave.strip()] = nombre.strip()
    return out


def _autorizar(clave: str | None) -> str:
    """Valida la clave y regresa quién es ("" = acceso general del equipo)."""
    clave = (clave or "").strip()
    general = os.getenv("CONSOLE_KEY") or ""
    if clave and general and hmac.compare_digest(clave, general):
        return ""
    for k, nombre in _usuarios().items():
        if clave and hmac.compare_digest(clave, k):
            return nombre
    raise HTTPException(status_code=401, detail="clave de consola inválida")


PREFIJO_EQUIPO = "[Equipo] "


def _prefijo(usuario: str) -> str:
    """Cómo se guarda un mensaje del equipo. Con nombre: "[Equipo · Blanca Velázquez] "."""
    return f"[Equipo · {usuario}] " if usuario else PREFIJO_EQUIPO


def _autor(role: str, texto: str) -> tuple[str, str]:
    """(autor, texto limpio): cliente · sofia · equipo · sistema."""
    return _autor_por(role, texto)[:2]


def _autor_por(role: str, texto: str) -> tuple[str, str, str]:
    """(autor, texto limpio, quién del equipo lo mandó desde la consola o "")."""
    if role == "user":
        return "cliente", texto, ""
    t = texto or ""
    if t.startswith(PREFIJO_EQUIPO):
        return "equipo", t[len(PREFIJO_EQUIPO):], ""
    if t.startswith("[Equipo · ") and "] " in t:
        cab, resto = t.split("] ", 1)
        return "equipo", resto, cab[len("[Equipo · "):].strip()
    if t.startswith("[Recordatorio automático]") or t.startswith("Hola ") and "te recordamos tu cita" in t:
        return "sistema", t, ""
    if t.startswith("[El equipo envió"):
        return "equipo", t, ""
    return "sofia", t, ""


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


@router.get("/estado")
async def estado(x_console_key: str | None = Header(default=None)):
    usuario = _autorizar(x_console_key)
    from agent import calendar_tool
    from agent.brain import estado_cerebro
    from agent.main import sofia_debe_responder, sofia_pausada

    return {
        "sofia_global_pausada": sofia_pausada(),
        "sofia_turno": sofia_debe_responder(),  # False = horario del equipo humano
        "agenda": calendar_tool.calendario_configurado(),
        "cerebro": dict(estado_cerebro) if isinstance(estado_cerebro, dict) else str(estado_cerebro),
        "ahora": ahora().isoformat(),
        "usuario": usuario,  # "" = acceso general del equipo
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
        pausas = {p.telefono for p in (await s.execute(select(ChatPausa).where(ChatPausa.telefono.in_(tels)))).scalars()
                  if _vigente(p)}
        origenes: dict[str, str] = {}
        for o in (await s.execute(
            select(OrigenAnuncio).where(OrigenAnuncio.telefono.in_(tels)).order_by(OrigenAnuncio.creado_en)
        )).scalars():
            origenes[o.telefono] = o.titulo or o.ad_id
    salida = []
    for m, n in filas:
        lead = leads.get(m.telefono)
        autor, texto, por = _autor_por(m.role, m.content)
        salida.append({
            "tel": m.telefono,
            "canal": "messenger" if m.telefono.startswith("msgr:") else "whatsapp",
            "nombre": (lead.nombre if lead else "") or "",
            "interes": (lead.interes if lead else "") or "",
            "estado": (lead.estado if lead else "") or "",
            "anuncio": origenes.get(m.telefono, ""),
            "ultimo": {"id": m.id, "autor": autor, "por": por, "texto": texto[:220], "ts": _iso(m.timestamp)},
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
        autor, texto, por = _autor_por(m.role, m.content)
        items.append({"id": m.id, "autor": autor, "por": por, "texto": texto, "ts": _iso(m.timestamp)})
    out = {
        "tel": tel,
        "canal": "messenger" if tel.startswith("msgr:") else "whatsapp",
        "mensajes": items,
        "sofia_pausada": _vigente(pausa),
        "pausa_hasta": _iso(pausa_vence(pausa.desde if pausa.desde.tzinfo else pausa.desde.replace(tzinfo=timezone.utc)))
        if _vigente(pausa) else None,
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
    usuario = _autorizar(x_console_key)
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
    await memory.guardar_mensaje(tel, "assistant", _prefijo(usuario) + texto)
    try:
        await memory.registrar_outbound_equipo(tel)
    except Exception:  # noqa: BLE001
        pass
    logger.info(f"Consola ({usuario or 'equipo'}) → …{tel[-4:]}: {texto[:60]}")
    return {"ok": True, "por": usuario}


@router.post("/pausa")
async def pausa(request: Request, x_console_key: str | None = Header(default=None)):
    """{tel, pausar: true|false}: apaga o prende a Sofía SOLO en ese chat."""
    usuario = _autorizar(x_console_key)
    datos = (await request.json()) or {}
    tel = (datos.get("tel") or "").strip()
    if not tel:
        raise HTTPException(status_code=400, detail="falta tel")
    pausar = bool(datos.get("pausar"))
    async with async_session() as s:
        actual = await s.get(ChatPausa, tel)
        if pausar and actual is not None and not _vigente(actual):
            await s.delete(actual)  # pausa vieja ya vencida: se reemplaza por una nueva
            await s.flush()
            actual = None
        if pausar and actual is None:
            s.add(ChatPausa(telefono=tel, por=(usuario or datos.get("por") or "consola")[:60], desde=ahora()))
        elif not pausar and actual is not None:
            await s.delete(actual)
        await s.commit()
    if pausar:
        try:
            await memory.pausar_seguimiento(tel)  # tampoco le manda recordatorios de venta
        except Exception:  # noqa: BLE001
            pass
    logger.info(f"Consola: Sofía {'PAUSADA' if pausar else 'reactivada'} en …{tel[-4:]}")
    return {"ok": True, "sofia_pausada": pausar, "pausa_hasta": _iso(pausa_vence(ahora())) if pausar else None}
