# agent/atribucion.py — Ventas generadas por los anuncios de Meta

"""
Liga cada VENTA con el ANUNCIO que trajo a la clienta, sin que el equipo capture nada:

1. Origen: Sofía guarda el anuncio de cada clienta que escribe tocando un anuncio
   (tabla `origen_anuncio`, con el ctwa_clid). Para conversaciones viejas, antes de que
   existiera eso, se reconoce el mensaje automático del anuncio ("Quiero más información").
2. Venta: en la agenda de Google, una sesión en un calendario de servicio PAGADO
   (Faciales, corporales, Depilación, masajes) — no la Experiencia Única ni valoraciones,
   que viven en "cita de venta o ex unica" — de esa misma clienta (por teléfono o nombre).
3. Meta: si la clienta trae ctwa_clid, se le reporta a Meta un evento Purchase por la
   Conversions API para mensajería; así aparece como "Compra" en el Administrador de
   anuncios, en el anuncio exacto que la trajo.

El valor de cada venta es el precio del paquete del servicio (configurable por variable de
entorno), porque el cobro real se hace en caja y no queda en WhatsApp.
"""

import asyncio
import logging
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import Boolean, DateTime, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from agent.memory import Base, Lead, Mensaje, OrigenAnuncio, _aware, ahora, async_session

logger = logging.getLogger("agentkit")

# Calendario de servicio pagado → nombre del servicio y variable con el valor del paquete
_CAL_PAGADOS = {
    "faciales": ("Facial", "VALOR_VENTA_FACIAL", 5999),
    "corporales": ("Corporal", "VALOR_VENTA_CORPORAL", 6500),
    "depilacion": ("Depilación", "VALOR_VENTA_DEPILACION", 5644),
    "masajes": ("Masaje", "VALOR_VENTA_MASAJE", 4000),
}
# Sesiones que NO son venta aunque estén en un calendario de servicio
_NO_ES_VENTA = re.compile(r"cortes[ií]a|experiencia|exp\.?\s*[uú]nica|valoraci[oó]n|gratis|sin costo", re.I)
# Mensaje que Meta pone por defecto en los anuncios de clic a WhatsApp
_SALUDO_ANUNCIO = re.compile(
    r"^(¡?hola!?\s*)?quiero más información|^¡?hola!? quiero mi promo|can i get more info", re.I
)
# Cuentas del equipo / pruebas que no son clientas
_EXCLUIR_NOMBRES = re.compile(r"joel|edgar romo|isabella", re.I)
_STOP = {"de", "la", "del", "los", "las", "y", "sesion", "facial", "corporal", "depilacion",
         "masaje", "valoracion", "experiencia", "unica", "exp", "ma", "maria", "cita",
         "por", "confirmar"}

WABA_ID = os.getenv("META_WABA_ID", "").strip()
DATASET_ID = os.getenv("META_DATASET_ID", "").strip()
CADA_HORAS = float(os.getenv("VENTAS_CADA_HORAS") or "6")


class VentaAnuncio(Base):
    """Una clienta que llegó por anuncio y compró un servicio (una fila por clienta)."""

    __tablename__ = "ventas_anuncio"

    telefono: Mapped[str] = mapped_column(String(50), primary_key=True)
    nombre: Mapped[str] = mapped_column(String(160), default="")
    servicio: Mapped[str] = mapped_column(String(60), default="")
    valor: Mapped[int] = mapped_column(Integer, default=0)
    fecha_sesion: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    llego_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ad_id: Mapped[str] = mapped_column(String(64), default="")
    ad_titulo: Mapped[str] = mapped_column(String(200), default="")
    ctwa_clid: Mapped[str] = mapped_column(String(300), default="")
    reportada_meta: Mapped[bool] = mapped_column(Boolean, default=False)
    respuesta_meta: Mapped[str] = mapped_column(Text, default="")
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora, index=True)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", (s or "").lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def _tel10(tel: str) -> str:
    return re.sub(r"\D", "", tel or "")[-10:]


def _tokens(nombre: str) -> tuple[str, ...]:
    return tuple(w for w in re.findall(r"[a-z]+", _norm(nombre)) if len(w) > 2 and w not in _STOP)[:2]


def _valor(var: str, defecto: int) -> int:
    try:
        return int(os.getenv(var) or defecto)
    except ValueError:
        return defecto


# ── Agenda ────────────────────────────────────────────────────────────────

def _leer_agenda(dias_atras: int = 150, dias_adelante: int = 180) -> list[dict]:
    """Todos los eventos de todos los calendarios de 2Body en un rango (síncrono)."""
    from agent import calendar_tool as ct

    svc = ct._obtener_servicio()
    ini = (datetime.now(timezone.utc) - timedelta(days=dias_atras)).isoformat()
    fin = (datetime.now(timezone.utc) + timedelta(days=dias_adelante)).isoformat()
    eventos = []
    for cal in svc.calendarList().list().execute().get("items", []):
        nombre_cal = cal.get("summary", "")
        if "festivo" in _norm(nombre_cal):
            continue
        token = None
        while True:
            r = svc.events().list(
                calendarId=cal["id"], timeMin=ini, timeMax=fin, singleEvents=True,
                orderBy="startTime", maxResults=2500, pageToken=token,
            ).execute()
            for e in r.get("items", []):
                if e.get("status") == "cancelled":
                    continue
                s = (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date")
                try:
                    inicio = datetime.fromisoformat(s)
                    if inicio.tzinfo is None:
                        inicio = inicio.replace(tzinfo=ct.TZ)
                except (TypeError, ValueError):
                    continue
                eventos.append({
                    "cal": _norm(nombre_cal).strip(),
                    "titulo": e.get("summary", ""),
                    "desc": e.get("description") or "",
                    "inicio": inicio,
                })
            token = r.get("nextPageToken")
            if not token:
                break
    return eventos


# ── Origen por anuncio ──────────────────────────────────────────────────────

# Anuncios que NO son de nuestra agencia (otra agencia también le hizo anuncios a 2Body con
# el mismo WhatsApp). Se reconocen por el título que Meta manda en el referral. Separados por |.
_OTRA_AGENCIA = [
    t.strip().lower()
    for t in (os.getenv("ANUNCIOS_OTRA_AGENCIA")
              or "Solicita tu precio especial aquí|Hidrofacial con 50% por apertura").split("|")
    if t.strip()
]


def es_de_otra_agencia(titulo: str) -> bool:
    t = (titulo or "").strip().lower()
    return bool(t) and any(t.startswith(x) for x in _OTRA_AGENCIA)


async def _clientas_de_anuncio() -> dict[str, dict]:
    """
    teléfono → {fecha, ad_id, titulo, ctwa_clid} de las clientas que llegaron por un anuncio
    NUESTRO. Solo cuenta el anuncio exacto que guardó Sofía (o que se registró a mano tras
    verificarlo en la bandeja); ya no se adivina por el saludo, porque el saludo automático es
    igual en los anuncios de la otra agencia.
    """
    fuentes: dict[str, dict] = {}
    async with async_session() as s:
        for o in (await s.execute(select(OrigenAnuncio).order_by(OrigenAnuncio.creado_en))).scalars():
            if es_de_otra_agencia(o.titulo):
                continue
            if o.telefono not in fuentes:  # se queda con el PRIMER anuncio nuestro que la trajo
                fuentes[o.telefono] = {"fecha": _aware(o.creado_en), "ad_id": o.ad_id,
                                       "titulo": o.titulo, "ctwa_clid": o.ctwa_clid}
    return fuentes


# ── Detección de ventas ────────────────────────────────────────────────────

async def detectar_ventas() -> list[dict]:
    """Cruza clientas de anuncio con sesiones pagadas de la agenda."""
    eventos = await asyncio.to_thread(_leer_agenda)
    fuentes = await _clientas_de_anuncio()
    async with async_session() as s:
        nombres_lead = {l.telefono: l.nombre for l in (await s.execute(select(Lead))).scalars()}

    ventas = []
    for tel, info in fuentes.items():
        t10 = _tel10(tel)
        if not t10:
            continue
        # Nombres con los que el equipo anota a esta clienta: el del lead y los de cualquier
        # evento que tenga su teléfono (p. ej. su cita de Experiencia Única).
        nombres = set()
        if _tokens(nombres_lead.get(tel, "")).__len__() == 2:
            nombres.add(_tokens(nombres_lead[tel]))
        for e in eventos:
            if t10 in re.sub(r"\D", "", e["titulo"] + e["desc"]):
                tk = _tokens(e["titulo"].split(" - ")[0].split("/")[0])
                if len(tk) == 2:
                    nombres.add(tk)
        desde = info["fecha"] or datetime(2000, 1, 1, tzinfo=timezone.utc)
        pagadas = []
        for e in eventos:
            servicio = _CAL_PAGADOS.get(e["cal"])
            if not servicio or e["inicio"] < desde - timedelta(hours=1):
                continue
            if _NO_ES_VENTA.search(e["titulo"]) or _EXCLUIR_NOMBRES.search(e["titulo"]):
                continue
            texto = _norm(e["titulo"] + " " + e["desc"])
            por_tel = t10 in re.sub(r"\D", "", e["titulo"] + e["desc"])
            por_nombre = any(all(w in texto for w in n) for n in nombres)
            if por_tel or por_nombre:
                pagadas.append((e, servicio))
        if not pagadas:
            continue
        e, (servicio, var, defecto) = min(pagadas, key=lambda x: x[0]["inicio"])
        ventas.append({
            "telefono": tel, "nombre": e["titulo"].split(" - ")[0].strip()[:160],
            "servicio": servicio, "valor": _valor(var, defecto), "fecha_sesion": e["inicio"],
            "sesiones": len(pagadas), "llego_en": info["fecha"], "ad_id": info["ad_id"],
            "ad_titulo": info["titulo"], "ctwa_clid": info["ctwa_clid"],
        })
    return ventas


# ── Reporte a Meta (Conversions API para mensajería) ───────────────────────

async def reportar_compra_meta(v: VentaAnuncio) -> tuple[bool, str]:
    """Manda el evento Purchase al dataset de WhatsApp. Requiere ctwa_clid."""
    token = os.getenv("META_ACCESS_TOKEN", "")
    if not (token and WABA_ID and DATASET_ID and v.ctwa_clid):
        return False, "falta ctwa_clid o configuración (META_WABA_ID / META_DATASET_ID)"
    version = os.getenv("META_API_VERSION") or "v25.0"
    evento = {
        "event_name": "Purchase",
        "event_time": int(datetime.now(timezone.utc).timestamp()),
        "event_id": f"venta-{_tel10(v.telefono)}",
        "action_source": "business_messaging",
        "messaging_channel": "whatsapp",
        "user_data": {"whatsapp_business_account_id": WABA_ID, "ctwa_clid": v.ctwa_clid},
        "custom_data": {"currency": "MXN", "value": v.valor, "content_name": v.servicio},
    }
    datos = {"data": [evento]}
    codigo_prueba = os.getenv("META_TEST_EVENT_CODE", "").strip()
    if codigo_prueba:
        datos["test_event_code"] = codigo_prueba
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                f"https://graph.facebook.com/{version}/{DATASET_ID}/events",
                params={"access_token": token}, json=datos,
            )
        return r.status_code == 200, r.text[:500]
    except httpx.HTTPError as e:
        return False, str(e)


async def ciclo_ventas() -> dict:
    """Detecta ventas nuevas, las guarda y reporta a Meta las que se puedan."""
    ventas = await detectar_ventas()
    nuevas, reportadas = 0, 0
    vigentes = {d["telefono"] for d in ventas}
    async with async_session() as s:
        # Quita ventas que ya no aplican (p. ej. clientas de anuncios de la otra agencia).
        for v in (await s.execute(select(VentaAnuncio))).scalars().all():
            if v.telefono not in vigentes and not v.reportada_meta:
                await s.delete(v)
        for d in ventas:
            v = await s.get(VentaAnuncio, d["telefono"])
            if v is None:
                v = VentaAnuncio(telefono=d["telefono"])
                s.add(v)
                nuevas += 1
            for k in ("nombre", "servicio", "valor", "fecha_sesion", "llego_en", "ad_id", "ad_titulo"):
                setattr(v, k, d[k])
            if d["ctwa_clid"] and not v.ctwa_clid:
                v.ctwa_clid = d["ctwa_clid"]
        await s.commit()
        pendientes = (await s.execute(
            select(VentaAnuncio).where(VentaAnuncio.reportada_meta.is_(False), VentaAnuncio.ctwa_clid != "")
        )).scalars().all()
        for v in pendientes:
            ok, detalle = await reportar_compra_meta(v)
            v.respuesta_meta = detalle
            if ok:
                v.reportada_meta = True
                reportadas += 1
                logger.info(f"[ventas] Compra reportada a Meta: {v.telefono} {v.servicio} ${v.valor} (anuncio {v.ad_id})")
            else:
                logger.warning(f"[ventas] Meta no aceptó la compra de {v.telefono}: {detalle}")
        await s.commit()
    return {"detectadas": len(ventas), "nuevas": nuevas, "reportadas_meta": reportadas}


async def loop_ventas():
    """Revisa la agenda cada pocas horas para detectar y reportar ventas de anuncios."""
    from agent import calendar_tool as ct

    if not ct.calendario_configurado():
        logger.info("Atribución de ventas: sin calendario configurado, desactivada.")
        return
    logger.info(f"Atribución de ventas ACTIVADA (cada {CADA_HORAS:g} h; Meta: {'sí' if DATASET_ID else 'no configurado'}).")
    await asyncio.sleep(90)
    while True:
        try:
            r = await ciclo_ventas()
            logger.info(f"[ventas] ciclo: {r}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[ventas] error en el ciclo: {e}")
        await asyncio.sleep(CADA_HORAS * 3600)


# ── Reporte ────────────────────────────────────────────────────────────────

async def reporte(dias: int = 7) -> dict:
    """Resumen para el dueño: conversaciones de anuncio y ventas (de la semana y totales)."""
    corte = datetime.now(timezone.utc) - timedelta(days=dias)
    fuentes = await _clientas_de_anuncio()
    async with async_session() as s:
        ventas = (await s.execute(select(VentaAnuncio).order_by(VentaAnuncio.fecha_sesion))).scalars().all()

    def fila(v: VentaAnuncio) -> dict:
        return {
            "clienta": v.nombre, "telefono": v.telefono[-4:], "servicio": v.servicio, "valor": v.valor,
            "llego": _aware(v.llego_en).date().isoformat() if v.llego_en else "",
            "primera_sesion_pagada": _aware(v.fecha_sesion).date().isoformat() if v.fecha_sesion else "",
            "anuncio": v.ad_titulo or ("(anuncio, antes de guardar cuál)" if not v.ad_id else v.ad_id),
            "ad_id": v.ad_id, "reportada_a_meta": v.reportada_meta,
        }

    nuevas = [v for v in ventas if v.creado_en and _aware(v.creado_en) >= corte]
    por_anuncio: dict[str, dict] = {}
    for v in ventas:
        k = v.ad_titulo or v.ad_id or "Anuncio (sin identificar)"
        a = por_anuncio.setdefault(k, {"ventas": 0, "valor": 0})
        a["ventas"] += 1
        a["valor"] += v.valor
    return {
        "periodo_dias": dias,
        "conversaciones_de_anuncio_periodo": sum(1 for f in fuentes.values() if f["fecha"] and f["fecha"] >= corte),
        "conversaciones_de_anuncio_total": len(fuentes),
        "ventas_nuevas_periodo": [fila(v) for v in nuevas],
        "ventas_total": len(ventas),
        "valor_estimado_total": sum(v.valor for v in ventas),
        "por_anuncio": por_anuncio,
        "todas_las_ventas": [fila(v) for v in ventas],
    }
