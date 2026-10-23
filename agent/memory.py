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
from sqlalchemy import DateTime, Integer, String, Text, delete, select, text
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
    # "Toque de cierre" DENTRO de la ventana de 24 h (mensaje libre, sin plantilla): cuándo
    # mandarlo y si ya se mandó. Sirve para cerrar a quien dejó la charla a medias.
    nudge_en: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    nudge_enviado: Mapped[bool] = mapped_column(default=False)
    # True cuando una persona del EQUIPO ya le escribió a esta clienta (echo de coexistencia).
    # Si es True, el seguimiento automático (toque y multi-día) NO le manda nada: la relación
    # ya la lleva una persona y Sofía no debe escribirle encima.
    equipo_atendio: Mapped[bool] = mapped_column(default=False, index=True)
    # Última vez que una persona del EQUIPO le escribió (echo). Sirve para que Sofía no se
    # meta en una charla que el equipo lleva en vivo (p. ej. alguien sigue contestando 8:05 pm).
    ultimo_equipo_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Recordatorio LIGERO en charlas que lleva el EQUIPO: si la clienta no contesta el último
    # mensaje del equipo en ~3-4 h, Sofía manda UN solo recordatorio (con el contexto del chat).
    nudge_equipo_enviado: Mapped[bool] = mapped_column(default=False)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


class DirectorioCliente(Base):
    """
    Directorio de clientas (nombre → teléfono), p. ej. exportado de Anegocios. Sirve para
    mandar el recordatorio del día antes a citas que el EQUIPO apuntó en la agenda solo con
    el nombre (sin teléfono).
    """

    __tablename__ = "directorio_clientes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    nombre: Mapped[str] = mapped_column(String(160))
    telefono: Mapped[str] = mapped_column(String(20), index=True)
    fuente: Mapped[str] = mapped_column(String(40), default="anegocios", index=True)
    actualizado: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


class OrigenAnuncio(Base):
    """
    Cada vez que una clienta escribe tocando un ANUNCIO de Meta (Click-to-WhatsApp).

    Meta manda 'referral' en ese mensaje con el id del anuncio y el ctwa_clid (id del clic).
    Con esto se sabe qué anuncio trajo a cada clienta y, cuando compra, se le puede
    reportar la venta a Meta para que aparezca en el Administrador de anuncios.
    """

    __tablename__ = "origen_anuncio"

    evento_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    ad_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    titulo: Mapped[str] = mapped_column(String(200), default="")
    texto: Mapped[str] = mapped_column(String(300), default="")
    ctwa_clid: Mapped[str] = mapped_column(String(300), default="")
    primer_mensaje: Mapped[str] = mapped_column(Text, default="")
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora, index=True)


async def registrar_origen_anuncio(telefono: str, evento_id: str, ref: dict, texto: str = "") -> None:
    """Guarda de qué anuncio llegó la clienta (idempotente por evento)."""
    if not evento_id:
        return
    async with async_session() as session:
        session.add(
            OrigenAnuncio(
                evento_id=evento_id,
                telefono=telefono,
                ad_id=ref.get("ad_id", ""),
                titulo=ref.get("titulo", ""),
                texto=ref.get("texto", ""),
                ctwa_clid=ref.get("ctwa_clid", ""),
                primer_mensaje=(texto or "")[:500],
                creado_en=ahora(),
            )
        )
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()


async def origenes_anuncio(desde: datetime | None = None) -> list[dict]:
    """Lista los registros de origen por anuncio (para el reporte de atribución)."""
    async with async_session() as session:
        q = select(OrigenAnuncio).order_by(OrigenAnuncio.creado_en)
        if desde is not None:
            q = q.where(OrigenAnuncio.creado_en >= desde)
        filas = (await session.execute(q)).scalars().all()
    return [
        {
            "telefono": f.telefono,
            "ad_id": f.ad_id,
            "titulo": f.titulo,
            "ctwa_clid": f.ctwa_clid,
            "primer_mensaje": f.primer_mensaje,
            "fecha": _aware(f.creado_en).isoformat() if f.creado_en else "",
        }
        for f in filas
    ]


# Cadencia de seguimiento (horas desde el último mensaje del cliente) por etapa.
# 3 toques máximo, con clase: +24 h, +3 días, +7 días. Más que esto arriesga el número.
_CADENCIA_HORAS = [24, 72, 168]

# "Toque de cierre" dentro de la ventana de 24 h: horas de silencio antes de reactivar.
_NUDGE_HORAS = float(os.getenv("NUDGE_HORAS") or "3")


async def inicializar_db():
    """Crea las tablas si no existen y aplica migraciones ligeras de columnas."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _migrar_columnas_nudge()


async def _migrar_columnas_nudge():
    """
    Agrega las columnas del 'toque de cierre' a la tabla 'leads' si ya existía sin ellas.
    create_all NO altera tablas existentes, por eso se hace a mano. Idempotente.
    """
    es_pg = engine.dialect.name == "postgresql"
    if es_pg:
        stmts = [
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS nudge_en TIMESTAMPTZ",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS nudge_enviado BOOLEAN DEFAULT FALSE",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS equipo_atendio BOOLEAN DEFAULT FALSE",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS ultimo_equipo_en TIMESTAMPTZ",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS nudge_equipo_enviado BOOLEAN DEFAULT FALSE",
        ]
    else:  # sqlite u otros: intentar agregar, ignorar si ya existe
        stmts = [
            "ALTER TABLE leads ADD COLUMN nudge_en TIMESTAMP",
            "ALTER TABLE leads ADD COLUMN nudge_enviado BOOLEAN DEFAULT 0",
            "ALTER TABLE leads ADD COLUMN equipo_atendio BOOLEAN DEFAULT 0",
            "ALTER TABLE leads ADD COLUMN ultimo_equipo_en TIMESTAMP",
            "ALTER TABLE leads ADD COLUMN nudge_equipo_enviado BOOLEAN DEFAULT 0",
        ]
    for s in stmts:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(s))
        except Exception as e:  # noqa: BLE001 — normal si la columna ya existe
            logger.debug(f"migración columna (ok si ya existe): {e}")


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


_SIN_REGISTRO_CLIENTA = (
    "[La clienta le escribió al negocio; sus mensajes de esa vez no quedaron guardados. "
    "Abajo está lo que le contestó el equipo.]"
)


async def obtener_historial_equipo(telefono: str, limite: int = 20) -> list[dict]:
    """
    Historial para el recordatorio ligero de charlas del EQUIPO: igual que obtener_historial,
    pero si los mensajes de la clienta no quedaron guardados (antes no se guardaban en horario
    del equipo) NO se descarta lo que dijo el equipo: se antepone una nota en lugar de ellos.
    """
    async with async_session() as session:
        r = await session.execute(
            select(Mensaje).where(Mensaje.telefono == telefono).order_by(Mensaje.id.desc()).limit(limite)
        )
        mensajes = list(r.scalars().all())
    mensajes.reverse()
    salida: list[dict] = []
    for m in mensajes:
        if salida and salida[-1]["role"] == m.role:
            salida[-1]["content"] += "\n" + m.content
        else:
            salida.append({"role": m.role, "content": m.content})
    if salida and salida[0]["role"] != "user":
        salida.insert(0, {"role": "user", "content": _SIN_REGISTRO_CLIENTA})
    return salida


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

    # Fusiona turnos consecutivos del MISMO rol en uno solo. Hace falta porque ahora el
    # historial incluye mensajes del equipo humano (guardados como 'assistant'): si el equipo
    # manda 2 seguidos, o el equipo y luego Sofía, quedarían 2 'assistant' pegados y la API
    # de Anthropic exige que los roles se alternen.
    salida: list[dict] = []
    for m in mensajes:
        if salida and salida[-1]["role"] == m.role:
            salida[-1]["content"] += "\n" + m.content
        else:
            salida.append({"role": m.role, "content": m.content})
    return salida


async def limpiar_historial(telefono: str):
    """Borra todo el historial de una conversacion."""
    async with async_session() as session:
        await session.execute(delete(Mensaje).where(Mensaje.telefono == telefono))
        await session.commit()


async def ultimas_conversaciones(
    max_convos: int = 10,
    max_msgs: int = 40,
    solo_whatsapp: bool = False,
    filtro_tel: list[str] | None = None,
) -> list[dict]:
    """
    Conversaciones más recientes con sus últimos mensajes (para revisión del dueño).
    Agrupa por teléfono, de la más reciente a la más vieja. `solo_whatsapp=True` excluye
    las de Messenger (prefijo 'msgr:'). `filtro_tel` = lista de fragmentos de teléfono; si
    se da, solo trae las conversaciones cuyo teléfono contenga alguno (para ver un caso).
    """
    # Si filtramos por teléfonos concretos, escaneamos una ventana más grande para
    # alcanzar conversaciones más viejas.
    ventana = 3000 if filtro_tel else 800
    async with async_session() as session:
        resultado = await session.execute(
            select(Mensaje).order_by(Mensaje.id.desc()).limit(ventana)
        )
        mensajes = list(resultado.scalars().all())

    convos: dict[str, list] = {}
    orden: list[str] = []
    for m in mensajes:  # del más nuevo al más viejo
        if solo_whatsapp and m.telefono.startswith("msgr:"):
            continue
        if filtro_tel and not any(f in m.telefono for f in filtro_tel):
            continue
        if m.telefono not in convos:
            if len(orden) >= max_convos:
                continue
            convos[m.telefono] = []
            orden.append(m.telefono)
        if len(convos[m.telefono]) < max_msgs:
            convos[m.telefono].append(
                {
                    "role": m.role,
                    "content": m.content,
                    "ts": m.timestamp.isoformat() if m.timestamp else None,
                }
            )

    return [
        {
            "telefono": tel,
            "canal": "messenger" if tel.startswith("msgr:") else "whatsapp",
            "mensajes": list(reversed(convos[tel])),  # cronológico
        }
        for tel in orden
    ]


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
            lead.nudge_en = None
        elif lead.estado != "opt_out":
            # ¿Es una conversación NUEVA (pasaron horas desde el último mensaje)?
            gap_grande = bool(
                lead.last_inbound and (ahora() - _aware(lead.last_inbound)) > timedelta(hours=8)
            )
            if gap_grande:
                lead.nudge_enviado = False  # re-armar el toque para esta nueva charla
                lead.nudge_equipo_enviado = False  # y el recordatorio ligero del equipo
            lead.estado = "activo"
            lead.last_inbound = ahora()
            lead.etapa_seguimiento = 0
            lead.proximo_seguimiento = ahora() + timedelta(hours=_CADENCIA_HORAS[0])
            # Programar el toque de cierre solo si aún no se mandó en esta charla.
            lead.nudge_en = None if lead.nudge_enviado else ahora() + timedelta(hours=_NUDGE_HORAS)
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


async def registrar_outbound_equipo(telefono: str) -> None:
    """
    Registra que el EQUIPO humano le respondió a esta clienta (echo de coexistencia): marca
    last_outbound y apaga el 'toque de cierre' pendiente, para que el seguimiento automático
    de Sofía NO le escriba encima a una conversación que una persona ya está atendiendo.
    """
    if not telefono:
        return
    async with async_session() as s:
        lead = await s.get(Lead, telefono)
        if lead is None:
            # El equipo le escribió PRIMERO (aún no había lead): se crea ya marcado, para que
            # cuando la clienta conteste Sofía no la meta al seguimiento automático.
            lead = Lead(telefono=telefono, creado_en=ahora())
            s.add(lead)
        lead.last_outbound = ahora()
        lead.ultimo_equipo_en = ahora()
        lead.nudge_en = None
        lead.nudge_enviado = True
        lead.equipo_atendio = True  # ya la atiende una persona: Sofía no le da seguimiento
        await s.commit()


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite devuelve fechas sin zona: se asumen UTC para poder comparar."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def equipo_activo_reciente(telefono: str, minutos: int) -> bool:
    """True si una persona del equipo le escribió a esta clienta en los últimos N minutos."""
    if not telefono or minutos <= 0:
        return False
    async with async_session() as s:
        lead = await s.get(Lead, telefono)
        ult = _aware(lead.ultimo_equipo_en) if lead else None
    return bool(ult and (ahora() - ult) < timedelta(minutes=minutos))


async def lead_sigue_elegible(telefono: str, para: str = "seguimiento") -> bool:
    """
    Re-chequeo FRESCO justo antes de mandar algo proactivo (toque o plantilla): entre que se
    armó la lista y el envío, una persona del equipo pudo haberle escrito o la clienta pudo
    haber agendado/pedido baja. Si algo cambió, NO se manda.
    """
    async with async_session() as s:
        lead = await s.get(Lead, telefono)
        if lead is None or lead.estado != "activo" or lead.equipo_atendio:
            return False
        if para == "nudge" and lead.nudge_enviado:
            return False
        return True


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


async def pausar_seguimiento(telefono: str) -> None:
    """
    La clienta declinó con amabilidad y Sofía ya se despidió dejando la puerta abierta:
    se apaga el seguimiento proactivo (toque de cierre y multi-día) para NO insistir. No es
    opt-out duro: si la clienta vuelve a escribir, su lead se reactiva solo (registrar_inbound_lead
    lo pone 'activo' de nuevo).
    """
    if not telefono:
        return
    async with async_session() as s:
        lead = await s.get(Lead, telefono)
        if lead is None:
            lead = Lead(telefono=telefono, creado_en=ahora())
            s.add(lead)
        lead.estado = "pausado"
        lead.proximo_seguimiento = None
        lead.nudge_en = None
        lead.nudge_enviado = True
        await s.commit()


async def leads_para_seguimiento(limite: int = 40) -> list[dict]:
    """Leads activos cuyo próximo seguimiento ya venció y aún les quedan toques."""
    async with async_session() as s:
        r = await s.execute(
            select(Lead)
            .where(
                Lead.estado == "activo",
                Lead.equipo_atendio.is_(False),  # si ya la atendió una persona, Sofía no le escribe
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


async def leads_para_nudge(limite: int = 40) -> list[dict]:
    """
    Leads activos que dejaron la charla a medias y toca el 'toque de cierre' DENTRO de
    las 24 h (mensaje libre, sin plantilla). Se excluye a quien ya agendó o pidió baja,
    y a quien ya se le mandó el toque.
    """
    limite_ventana = ahora() - timedelta(hours=23)  # aún dentro de la ventana de 24 h
    async with async_session() as s:
        r = await s.execute(
            select(Lead)
            .where(
                Lead.estado == "activo",
                Lead.equipo_atendio.is_(False),  # si ya la atendió una persona, Sofía no le escribe
                Lead.nudge_enviado.is_(False),
                Lead.nudge_en.is_not(None),
                Lead.nudge_en <= ahora(),
                Lead.last_inbound >= limite_ventana,
            )
            .order_by(Lead.nudge_en)
            .limit(limite)
        )
        leads = list(r.scalars().all())
    return [{"telefono": l.telefono, "nombre": l.nombre, "interes": l.interes} for l in leads]


_NUDGE_EQUIPO_HORAS = float(os.getenv("NUDGE_EQUIPO_HORAS") or "3")


async def leads_para_nudge_equipo(limite: int = 40) -> list[dict]:
    """
    Charlas que lleva el EQUIPO donde la clienta dejó sin contestar el último mensaje del
    equipo hace >= NUDGE_EQUIPO_HORAS (3 h), aún dentro de las 24 h de WhatsApp, y todavía
    no se le mandó el recordatorio ligero en esta charla.
    """
    corte = ahora() - timedelta(hours=_NUDGE_EQUIPO_HORAS)
    limite_ventana = ahora() - timedelta(hours=23)
    async with async_session() as s:
        r = await s.execute(
            select(Lead)
            .where(
                Lead.estado == "activo",
                Lead.equipo_atendio.is_(True),
                Lead.nudge_equipo_enviado.is_(False),
                Lead.ultimo_equipo_en.is_not(None),
                Lead.ultimo_equipo_en > Lead.last_inbound,  # el equipo habló al último
                Lead.ultimo_equipo_en <= corte,
                Lead.last_inbound >= limite_ventana,
            )
            .order_by(Lead.ultimo_equipo_en)
            .limit(limite)
        )
        leads = list(r.scalars().all())
    return [{"telefono": l.telefono, "nombre": l.nombre} for l in leads]


async def nudge_equipo_sigue_elegible(telefono: str) -> bool:
    """Re-chequeo justo antes de enviar: la clienta no contestó y el equipo no volvió a escribir."""
    corte = ahora() - timedelta(hours=_NUDGE_EQUIPO_HORAS)
    async with async_session() as s:
        lead = await s.get(Lead, telefono)
        if lead is None or lead.estado != "activo" or lead.nudge_equipo_enviado:
            return False
        ult_eq, ult_in = _aware(lead.ultimo_equipo_en), _aware(lead.last_inbound)
        return bool(ult_eq and ult_in and ult_eq > ult_in and ult_eq <= corte)


async def marcar_nudge_equipo_enviado(telefono: str) -> None:
    """Un solo recordatorio ligero por charla (se re-arma cuando la clienta vuelve tras horas)."""
    async with async_session() as s:
        lead = await s.get(Lead, telefono)
        if lead is None:
            return
        lead.nudge_equipo_enviado = True
        await s.commit()


# ─────────────────── Directorio de clientas (nombre → teléfono) ───────────────────


async def reemplazar_directorio(items: list[dict], fuente: str = "anegocios") -> int:
    """Reemplaza el directorio de una fuente. items: [{"nombre", "telefono"}]. Devuelve cuántos guardó."""
    filas = []
    for it in items:
        nombre = (it.get("nombre") or "").strip()
        dig = "".join(c for c in str(it.get("telefono") or "") if c.isdigit())
        if nombre and len(dig) >= 10:
            filas.append(DirectorioCliente(nombre=nombre[:160], telefono=dig[-10:], fuente=fuente, actualizado=ahora()))
    async with async_session() as s:
        await s.execute(delete(DirectorioCliente).where(DirectorioCliente.fuente == fuente))
        s.add_all(filas)
        await s.commit()
    return len(filas)


async def directorio_clientes() -> list[tuple[str, str]]:
    """(nombre, teléfono 10 dígitos) del directorio + los leads de WhatsApp con nombre conocido."""
    async with async_session() as s:
        d = (await s.execute(select(DirectorioCliente))).scalars().all()
        l = (await s.execute(select(Lead).where(Lead.nombre != ""))).scalars().all()
    out = [(x.nombre, x.telefono) for x in d]
    for x in l:
        dig = "".join(c for c in x.telefono if c.isdigit())
        if len(dig) >= 10:
            out.append((x.nombre, dig[-10:]))
    return out


async def estado_lead(telefono_10: str) -> str | None:
    """Estado del lead por los últimos 10 dígitos (p. ej. 'opt_out'), o None si no existe."""
    async with async_session() as s:
        r = await s.execute(select(Lead).where(Lead.telefono.like(f"%{telefono_10}")))
        lead = r.scalars().first()
    return lead.estado if lead else None


async def marcar_nudge_enviado(telefono: str) -> None:
    """Marca que ya se mandó el toque de cierre dentro de la ventana."""
    async with async_session() as s:
        lead = await s.get(Lead, telefono)
        if lead is None:
            return
        lead.nudge_enviado = True
        lead.nudge_en = None
        lead.last_outbound = ahora()
        await s.commit()


async def resumen_leads() -> dict:
    """Conteo rápido de leads por estado (para reportes)."""
    async with async_session() as s:
        r = await s.execute(select(Lead.estado))
        estados = [x for (x,) in r.all()]
    from collections import Counter
    return dict(Counter(estados))


async def reporte_leads() -> dict:
    """
    Reporte completo de leads para el panel/endpoint /leads.
    'tibios' = activos que aún no agendan (candidatos a seguimiento).
    """
    async with async_session() as s:
        r = await s.execute(select(Lead).order_by(Lead.last_inbound.desc()))
        leads = list(r.scalars().all())

    def _d(l: "Lead") -> dict:
        return {
            "telefono": l.telefono,
            "nombre": l.nombre or "",
            "interes": l.interes or "",
            "estado": l.estado,
            "etapa_seguimiento": l.etapa_seguimiento,
            "ultimo_mensaje": l.last_inbound.isoformat() if l.last_inbound else None,
            "proximo_seguimiento": (
                l.proximo_seguimiento.isoformat() if l.proximo_seguimiento else None
            ),
        }

    from collections import Counter
    return {
        "total": len(leads),
        "resumen": dict(Counter(l.estado for l in leads)),
        "tibios": [_d(l) for l in leads if l.estado == "activo"],
        "agendaron": [_d(l) for l in leads if l.estado == "agendado"],
        "opt_out": [_d(l) for l in leads if l.estado == "opt_out"],
    }
