# agent/memory.py — Memoria de conversaciones
# Generado por AgentKit

"""
Guarda el historial de cada conversacion por numero de telefono, y lleva registro de
que eventos de webhook ya se atendieron.

SQLite en local, PostgreSQL en produccion.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from sqlalchemy import DateTime, Integer, String, Text, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

load_dotenv()
logger = logging.getLogger("agentkit")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./agentkit.db")

# Railway entrega la URL de PostgreSQL con el esquema "postgresql://" (o "postgres://").
# SQLAlchemy en modo asincrono necesita que el driver sea explicito.
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

# En produccion, SQLite vive dentro del contenedor y el disco del contenedor es efimero:
# cada redespliegue borra el historial de todas las conversaciones. Avisarlo fuerte, porque
# el agente arranca igual y el problema recien se nota cuando un cliente vuelve a escribir.
if DATABASE_URL.startswith("sqlite") and os.getenv("ENVIRONMENT") == "production":
    logger.warning(
        "Estas en produccion con SQLite. El historial se va a borrar en cada redespliegue. "
        "Agrega PostgreSQL y configura DATABASE_URL para que el agente recuerde a sus clientes."
    )

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def ahora() -> datetime:
    """Hora actual en UTC, con zona horaria."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Mensaje(Base):
    """Un mensaje del historial de conversacion."""

    __tablename__ = "mensajes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    role: Mapped[str] = mapped_column(String(20))  # "user" o "assistant"
    content: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


class EventoProcesado(Base):
    """
    Eventos de webhook que ya se atendieron.

    Los proveedores entregan "al menos una vez": el mismo evento puede llegar dos veces.
    Sin esta tabla, el cliente recibiria la misma respuesta repetida.
    """

    __tablename__ = "eventos_procesados"

    evento_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora, index=True)


class Lead(Base):
    """
    Un prospecto para SEGUIMIENTO de ventas.

    Sofía usa esto para reactivar a quien mostró interés pero no agendó, aun días después.
    Ojo: fuera de 24 h WhatsApp exige plantilla aprobada, por eso el envío real de
    seguimiento usa plantillas (ver agent/followup.py).

    estado: 'activo' (en juego) · 'agendado' (ya cerró, no molestar) · 'opt_out' (pidió no más)
    """

    __tablename__ = "leads"

    telefono: Mapped[str] = mapped_column(String(50), primary_key=True)
    nombre: Mapped[str] = mapped_column(String(120), default="")
    interes: Mapped[str] = mapped_column(String(200), default="")
    estado: Mapped[str] = mapped_column(String(20), default="activo", index=True)
    etapa_seguimiento: Mapped[int] = mapped_column(Integer, default=0)  # cuántos seguimientos enviados
    last_inbound: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora, index=True)
    last_outbound: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    proximo_seguimiento: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


# Cadencia de seguimiento (horas desde el último mensaje del cliente) por etapa.
# 3 toques máximo, con clase: +24 h, +3 días, +7 días. Más que esto arriesga el número.
_CADENCIA_HORAS = [24, 72, 168]


async def inicializar_db():
    """Crea las tablas si no existen."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def marcar_evento_procesado(evento_id: str) -> bool:
    """
    Registra un evento. Retorna True si es nuevo, False si ya se habia procesado.

    La unicidad la garantiza la base de datos (clave primaria), no una consulta previa:
    asi dos webhooks que llegan al mismo tiempo no pasan los dos.
    """
    if not evento_id:
        return True  # sin id no podemos deduplicar: se procesa

    async with async_session() as session:
        session.add(EventoProcesado(evento_id=evento_id, creado_en=ahora()))
        try:
            await session.commit()
            return True
        except IntegrityError:
            await session.rollback()
            return False


async def liberar_evento(evento_id: str):
    """
    Borra la marca de un evento para que el reintento del proveedor SI se procese.

    Se usa cuando el mensaje se marco como procesado pero despues fallo el envio de la
    respuesta. Sin esto, el reintento se descartaria por duplicado y el cliente se
    quedaria sin respuesta para siempre.
    """
    if not evento_id:
        return
    async with async_session() as session:
        await session.execute(delete(EventoProcesado).where(EventoProcesado.evento_id == evento_id))
        await session.commit()


async def limpiar_eventos_viejos(dias: int = 7):
    """Borra los eventos de hace mas de N dias para que la tabla no crezca sin fin."""
    limite = ahora() - timedelta(days=dias)
    async with async_session() as session:
        resultado = await session.execute(
            delete(EventoProcesado).where(EventoProcesado.creado_en < limite)
        )
        await session.commit()
    if resultado.rowcount:
        logger.info(f"Se limpiaron {resultado.rowcount} eventos de mas de {dias} dias")


async def guardar_mensaje(telefono: str, role: str, content: str):
    """Guarda un mensaje en el historial de esa conversacion."""
    async with async_session() as session:
        session.add(Mensaje(telefono=telefono, role=role, content=content, timestamp=ahora()))
        await session.commit()


async def obtener_historial(telefono: str, limite: int = 20) -> list[dict]:
    """
    Devuelve los ultimos N mensajes de una conversacion, en orden cronologico.

    Se ordena por id y no por timestamp: dos mensajes guardados en el mismo instante
    tienen el mismo timestamp, y el orden entre ellos quedaria librado al azar.
    """
    async with async_session() as session:
        resultado = await session.execute(
            select(Mensaje)
            .where(Mensaje.telefono == telefono)
            .order_by(Mensaje.id.desc())
            .limit(limite)
        )
        mensajes = list(resultado.scalars().all())

    mensajes.reverse()  # vienen del mas nuevo al mas viejo: los damos vuelta

    # La API de Claude exige que el historial empiece con un mensaje del usuario.
    # Si por un error anterior quedo un "assistant" suelto al principio, lo sacamos.
    while mensajes and mensajes[0].role != "user":
        mensajes.pop(0)

    return [{"role": m.role, "content": m.content} for m in mensajes]


async def limpiar_historial(telefono: str):
    """Borra todo el historial de una conversacion."""
    async with async_session() as session:
        await session.execute(delete(Mensaje).where(Mensaje.telefono == telefono))
        await session.commit()


# ─────────────────────────── Seguimiento de leads ───────────────────────────

# Frases que significan "no me sigan escribiendo". Conservadoras para no marcar opt-out
# por error a media conversación.
_OPT_OUT_KEYS = (
    "no me interesa", "no me escriban", "no me manden", "dejen de escribir",
    "dejen de molestar", "ya no me escriban", "date de baja", "darme de baja", "stop",
)


async def registrar_inbound_lead(telefono: str, texto: str = "") -> None:
    """
    Registra que un cliente escribió: crea/actualiza su lead, reinicia la cadencia de
    seguimiento (primer toque a +24 h del último mensaje) y detecta opt-out.
    """
    if not telefono:
        return
    t = (texto or "").lower()
    async with async_session() as s:
        lead = await s.get(Lead, telefono)
        if lead is None:
            lead = Lead(telefono=telefono, creado_en=ahora())
            s.add(lead)
        if any(k in t for k in _OPT_OUT_KEYS):
            lead.estado = "opt_out"
            lead.proximo_seguimiento = None
        elif lead.estado != "opt_out":
            lead.estado = "activo"
            lead.last_inbound = ahora()
            lead.etapa_seguimiento = 0
            lead.proximo_seguimiento = ahora() + timedelta(hours=_CADENCIA_HORAS[0])
        await s.commit()


async def actualizar_lead(telefono: str, nombre: str = "", interes: str = "") -> None:
    """Guarda el nombre/servicio de interés del lead (para personalizar el seguimiento)."""
    if not telefono:
        return
    async with async_session() as s:
        lead = await s.get(Lead, telefono)
        if lead is None:
            return
        if nombre and not lead.nombre:
            lead.nombre = nombre[:120]
        if interes:
            lead.interes = interes[:200]
        await s.commit()


async def marcar_lead_agendado(telefono: str) -> None:
    """La clienta agendó: se apaga el seguimiento (no molestar a quien ya cerró)."""
    if not telefono:
        return
    async with async_session() as s:
        lead = await s.get(Lead, telefono)
        if lead is None:
            lead = Lead(telefono=telefono, creado_en=ahora())
            s.add(lead)
        lead.estado = "agendado"
        lead.proximo_seguimiento = None
        await s.commit()


async def marcar_lead_opt_out(telefono: str) -> None:
    """Marca que el lead ya no quiere seguimiento."""
    if not telefono:
        return
    async with async_session() as s:
        lead = await s.get(Lead, telefono)
        if lead is None:
            return
        lead.estado = "opt_out"
        lead.proximo_seguimiento = None
        await s.commit()


async def leads_para_seguimiento(limite: int = 40) -> list[dict]:
    """Leads activos cuyo próximo seguimiento ya venció y aún les quedan toques."""
    async with async_session() as s:
        r = await s.execute(
            select(Lead)
            .where(
                Lead.estado == "activo",
                Lead.proximo_seguimiento.is_not(None),
                Lead.proximo_seguimiento <= ahora(),
                Lead.etapa_seguimiento < len(_CADENCIA_HORAS),
            )
            .order_by(Lead.proximo_seguimiento)
            .limit(limite)
        )
        leads = list(r.scalars().all())
    return [
        {"telefono": l.telefono, "nombre": l.nombre, "interes": l.interes,
         "etapa": l.etapa_seguimiento}
        for l in leads
    ]


async def registrar_seguimiento_enviado(telefono: str) -> None:
    """Avanza la etapa tras enviar un seguimiento y programa el siguiente toque (o lo detiene)."""
    async with async_session() as s:
        lead = await s.get(Lead, telefono)
        if lead is None:
            return
        lead.etapa_seguimiento += 1
        lead.last_outbound = ahora()
        if lead.etapa_seguimiento < len(_CADENCIA_HORAS):
            base = lead.last_inbound or ahora()
            lead.proximo_seguimiento = base + timedelta(hours=_CADENCIA_HORAS[lead.etapa_seguimiento])
        else:
            lead.proximo_seguimiento = None  # se agotaron los toques
        await s.commit()


async def resumen_leads() -> dict:
    """Conteo rápido de leads por estado (para reportes)."""
    async with async_session() as s:
        r = await s.execute(select(Lead.estado))
        estados = [x for (x,) in r.all()]
    from collections import Counter
    return dict(Counter(estados))
