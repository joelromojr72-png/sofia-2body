# agent/main.py — Servidor FastAPI + Webhook de WhatsApp
# Generado por AgentKit

"""
Servidor principal del agente.
Funciona con cualquier proveedor (Zernio, Meta) gracias a la capa de providers.
"""

import asyncio
import logging
import os
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date, datetime, time
import time as _reloj
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from agent import followup
from agent.brain import estado_cerebro, generar_respuesta
from agent.memory import (
    guardar_mensaje,
    inicializar_db,
    liberar_evento,
    limpiar_eventos_viejos,
    marcar_evento_procesado,
    obtener_historial,
    registrar_inbound_lead,
    registrar_origen_anuncio,
    origenes_anuncio,
    registrar_outbound_equipo,
    equipo_activo_reciente,
    reporte_leads,
    ultimas_conversaciones,
)
from agent.providers import obtener_proveedor, obtener_proveedor_messenger
from agent.providers.base import MensajeEntrante

load_dotenv()

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("agentkit")
# En desarrollo queremos el detalle de NUESTRO agente, no el de las librerias.
# Poner el nivel raiz en DEBUG llena la terminal de ruido de aiosqlite y httpx
# y hace imposible leer lo que hizo el agente.
logger.setLevel(logging.DEBUG if ENVIRONMENT == "development" else logging.INFO)

PORT = int(os.getenv("PORT", "8000"))

# ── Horario de respuesta de Sofía ──────────────────────────────────────────
# Sofía auto-responde TODOS LOS DÍAS, solo fuera del horario en que 2 Body está
# laborando (cuando no están las humanas). En esas franjas la atención es 100%
# del equipo humano.
#
#   Sofía CALLADA:   Lun-Vie 09:00-20:00  ·  Sábado 09:00-14:30 (atiende el equipo)
#   Sofía RESPONDE:  todo lo demás (noches entre semana, sábado tarde y domingo completo)
#
# Aplica a AMBOS canales (WhatsApp siempre; Messenger si MESSENGER_RESPETA_HORARIO=true).
# Zona horaria de Pachuca. Railway corre en UTC, por eso convertimos explícito.
TZ_NEGOCIO = ZoneInfo("America/Mexico_City")

# Horario en que el equipo humano atiende (Sofía responde fuera de estas franjas).
ENTRE_SEMANA_EQUIPO = (time(9, 0), time(20, 0))  # Lunes a Viernes
SABADO_EQUIPO = (time(9, 0), time(14, 30))

# ── Días especiales (excepciones puntuales al horario normal) ───────────────
# Para forzar que Sofía RESPONDA un día entre semana concreto (ej. feriado en que el
# equipo no trabaja), agrega la fecha con valor None. Con una franja (inicio, fin) el
# equipo atiende ese día en esa franja y Sofía responde el resto. Fuera de estas fechas
# rige el horario normal de arriba. Déjalo vacío si no hay excepciones.
DIAS_ESPECIALES: dict[date, tuple[time, time] | None] = {}


MAX_ANTIGUEDAD_MIN = int(os.getenv("SOFIA_MAX_ANTIGUEDAD_MIN") or "30")


def mensaje_es_viejo(msg: MensajeEntrante) -> bool:
    """True si la clienta escribió el mensaje hace más de MAX_ANTIGUEDAD_MIN minutos."""
    # Si Sofía estuvo caída, Meta reentrega los mensajes horas o días después. Esos NO se
    # contestan (sería fuera de contexto) y quedan registrados para que los atienda el equipo.
    enviado_en = msg.contexto.get("enviado_en")
    if not enviado_en:
        return False
    return (_reloj.time() - enviado_en) > MAX_ANTIGUEDAD_MIN * 60


# ── Protecciones extra ──────────────────────────────────────────────────────
# Si una persona del equipo le escribió a la clienta hace menos de esto, Sofía NO contesta:
# la charla la lleva el equipo en vivo (p. ej. alguien sigue atendiendo a las 8:05 pm).
SILENCIO_TRAS_EQUIPO_MIN = int(os.getenv("SOFIA_SILENCIO_TRAS_EQUIPO_MIN") or "120")

# Tope de respuestas de Sofía por clienta por hora: corta bucles (otro bot contestándole a
# Sofía) y abusos que gastarían saldo de Anthropic. Pasado el tope, se calla y lo ve el equipo.
MAX_RESPUESTAS_HORA = int(os.getenv("SOFIA_MAX_RESPUESTAS_HORA") or "15")
_respuestas_recientes: dict[str, list[float]] = defaultdict(list)

# Números que NO son clientas (avisos automáticos de Meta, etc.): nunca se les contesta.
TELEFONOS_IGNORADOS = {
    t.strip() for t in (os.getenv("SOFIA_IGNORAR_TELEFONOS") or "447710173736").split(",") if t.strip()
}

# Respuesta fija (sin gastar IA) cuando la clienta manda audio/foto/archivo: Sofía no puede
# escucharlos ni verlos, así que le pide con amabilidad que lo escriba. Máx 1 vez cada 30 min.
MENSAJE_MEDIA = os.getenv("SOFIA_MENSAJE_MEDIA") or (
    "¡Gracias por su mensaje! 💜 Por este medio no puedo escuchar audios ni ver archivos. "
    "¿Me lo podría escribir? Con gusto le ayudo."
)
_media_respondida: dict[str, float] = {}


def _excede_tope_respuestas(telefono: str) -> bool:
    """True si Sofía ya le respondió MAX_RESPUESTAS_HORA veces a esta clienta en la última hora."""
    ahora_s = _reloj.time()
    recientes = [t for t in _respuestas_recientes[telefono] if ahora_s - t < 3600]
    _respuestas_recientes[telefono] = recientes
    return len(recientes) >= MAX_RESPUESTAS_HORA


def sofia_pausada() -> bool:
    """
    Interruptor global de pausa (variable de entorno SOFIA_PAUSADA).

    Cuando está activo, Sofía NO responde a nadie ni registra leads/seguimiento: sirve para
    hacer pruebas sin que conteste a clientes reales. Se sigue capturando lo que escribe el
    equipo humano (echos de coexistencia) para no perder el historial. Es totalmente
    reversible: basta con quitar la variable (o ponerla en false) en Railway.
    """
    return os.getenv("SOFIA_PAUSADA", "").strip().lower() in ("1", "true", "si", "sí", "yes", "on")


def sofia_debe_responder(ahora: datetime | None = None) -> bool:
    """
    True si Sofía debe auto-responder ahora.

    Regla: Sofía responde TODOS LOS DÍAS, solo fuera del horario en que las humanas
    atienden. CALLADA Lun-Vie 09:00-20:00 y Sábado 09:00-14:30 (atiende el equipo);
    RESPONDE todo lo demás (noches entre semana, sábado tarde y domingo completo).
    Las fechas en DIAS_ESPECIALES son excepciones puntuales (p. ej. un feriado entre
    semana en que el equipo no trabaja y sí queremos que Sofía cubra).
    """
    ahora = ahora or datetime.now(TZ_NEGOCIO)
    t = ahora.time()

    # Excepción puntual del día (tiene prioridad sobre el horario semanal).
    if ahora.date() in DIAS_ESPECIALES:
        franja = DIAS_ESPECIALES[ahora.date()]
        if franja is None:
            return True  # el equipo no trabaja ese día: Sofía responde todo el día
        inicio, fin = franja
        return not (inicio <= t < fin)  # Sofía responde fuera de esa franja

    dow = ahora.weekday()  # 0=Lun ... 5=Sáb, 6=Dom

    if dow <= 4:  # Lunes a Viernes: el equipo atiende ENTRE_SEMANA_EQUIPO; Sofía el resto
        inicio, fin = ENTRE_SEMANA_EQUIPO
        return not (inicio <= t < fin)
    if dow == 5:  # Sábado: el equipo atiende SABADO_EQUIPO; Sofía responde el resto
        inicio, fin = SABADO_EQUIPO
        return not (inicio <= t < fin)
    return True  # Domingo: el equipo no atiende, Sofía responde todo el día


# Un candado por numero de telefono. En WhatsApp es normal que alguien mande "hola" y
# medio segundo despues la pregunta de verdad: sin esto los dos mensajes se procesarian
# en paralelo, los dos leerian el mismo historial y las escrituras quedarian intercaladas.
_candados: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# Si la configuracion esta mal, guardamos el error y lo mostramos en el health check,
# en vez de reventar en el import y dejar a Railway reiniciando el contenedor a ciegas.
proveedor = None
error_configuracion: str | None = None
try:
    proveedor = obtener_proveedor()
except Exception as e:  # noqa: BLE001 — cualquier problema de configuracion
    error_configuracion = str(e)

# Resultado del chequeo de credenciales que se hace al arrancar. Se expone en el health
# check: que el servidor conteste no significa que el agente pueda responder por WhatsApp.
estado_proveedor: dict = {"ok": None, "detalle": "sin verificar"}

# ── Canal EXTRA: Facebook Messenger ─────────────────────────────────────────
# Convive con WhatsApp (no lo reemplaza). Se activa solo si hay Page Access Token.
# Comparte el mismo cerebro (Claude), memoria y calendario que WhatsApp.
proveedor_messenger = obtener_proveedor_messenger()
estado_messenger: dict = {"ok": None, "detalle": "sin configurar"}

# En Messenger, por defecto Sofía responde 24/7 (canal nuevo, sin equipo humano vigilando
# la bandeja de la página). Pon MESSENGER_RESPETA_HORARIO=true si quieres que respete el
# mismo horario de coexistencia que WhatsApp (callada mientras atiende el equipo).
MESSENGER_RESPETA_HORARIO = os.getenv("MESSENGER_RESPETA_HORARIO", "false").strip().lower() == "true"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Prepara la base de datos y chequea el proveedor al arrancar."""
    await inicializar_db()
    await limpiar_eventos_viejos()
    logger.info("Base de datos lista")
    logger.info(f"Servidor AgentKit escuchando en el puerto {PORT}")

    global estado_proveedor
    if proveedor is not None:
        logger.info(f"Proveedor de WhatsApp: {proveedor.__class__.__name__}")
        ok, detalle = await proveedor.verificar_conexion()
        estado_proveedor = {"ok": ok, "detalle": detalle}
        logger.info(f"Conexion con el proveedor: {'OK' if ok else 'ERROR'} — {detalle}")
        # Motor de seguimiento multi-día (dormido salvo FOLLOWUP_ENABLED=true).
        # Solo WhatsApp: usa plantillas de Meta, que no existen en Messenger.
        asyncio.create_task(followup.loop_seguimiento(proveedor))
    else:
        logger.error(f"Proveedor de WhatsApp NO configurado: {error_configuracion}")

    # Canal extra de Messenger (si está configurado).
    global estado_messenger
    if proveedor_messenger is not None:
        ok_m, detalle_m = await proveedor_messenger.verificar_conexion()
        estado_messenger = {"ok": ok_m, "detalle": detalle_m}
        modo = "respeta horario de equipo" if MESSENGER_RESPETA_HORARIO else "24/7"
        logger.info(f"Canal Messenger: {'OK' if ok_m else 'ERROR'} ({modo}) — {detalle_m}")
    else:
        logger.info("Canal Messenger: no configurado (falta MESSENGER_PAGE_ACCESS_TOKEN)")

    yield


app = FastAPI(title="AgentKit — WhatsApp AI Agent", version="2.0.0", lifespan=lifespan)


@app.get("/")
async def health_check():
    """Endpoint de salud para Railway y monitoreo."""
    if error_configuracion:
        return {"status": "error", "service": "agentkit", "detalle": error_configuracion}

    # Se responde 200 aunque las credenciales esten mal, para que Railway no marque el
    # deploy como caido y puedas leer el diagnostico. El detalle esta en el cuerpo.
    return {
        "status": "ok" if estado_proveedor["ok"] else "degradado",
        "service": "agentkit",
        "sofia_pausada": sofia_pausada(),
        # Cerebro (Anthropic): si "ultimo_error" es más reciente que "ultimo_ok", Sofía no está
        # pudiendo contestar (p. ej. saldo agotado) y se está quedando callada.
        "cerebro": {
            **estado_cerebro,
            "ok": not (
                estado_cerebro.get("ultimo_error")
                and (estado_cerebro.get("ultimo_ok") or "") < estado_cerebro["ultimo_error"]
            ),
        },
        "proveedor": proveedor.__class__.__name__ if proveedor else None,
        "conexion": estado_proveedor,
        "messenger": {
            "activo": proveedor_messenger is not None,
            "modo": "respeta_horario" if MESSENGER_RESPETA_HORARIO else "24/7",
            "conexion": estado_messenger,
        },
    }


@app.get("/leads")
async def leads_report(key: str = ""):
    """Reporte de leads (tibios/agendaron/opt-out) para el equipo. Protegido con clave."""
    clave = os.getenv("ADMIN_KEY") or os.getenv("META_VERIFY_TOKEN") or ""
    if not clave or key != clave:
        raise HTTPException(status_code=403, detail="clave invalida")
    return await reporte_leads()


@app.get("/anuncios")
async def anuncios_report(key: str = "", dias: int = 60):
    """
    Atribución: qué clientas llegaron por qué anuncio de Meta (Click-to-WhatsApp), con su
    teléfono, fecha y primer mensaje. Base para cruzar con citas y ventas. Protegido con clave.
    """
    clave = os.getenv("ADMIN_KEY") or os.getenv("META_VERIFY_TOKEN") or ""
    if not clave or key != clave:
        raise HTTPException(status_code=403, detail="clave invalida")
    from datetime import timedelta, timezone as _tz

    desde = datetime.now(_tz.utc) - timedelta(days=max(1, min(dias, 365)))
    filas = await origenes_anuncio(desde)
    por_anuncio: dict[str, dict] = {}
    for f in filas:
        a = por_anuncio.setdefault(f["ad_id"] or "sin_id", {"titulo": f["titulo"], "clientas": set(), "mensajes": 0})
        a["clientas"].add(f["telefono"])
        a["mensajes"] += 1
    resumen = [
        {"ad_id": k, "titulo": v["titulo"], "clientas": len(v["clientas"]), "entradas": v["mensajes"]}
        for k, v in sorted(por_anuncio.items(), key=lambda kv: -len(kv[1]["clientas"]))
    ]
    return {"desde": desde.isoformat(), "resumen": resumen, "detalle": filas}


@app.get("/admin/export")
async def export_conversaciones(key: str = ""):
    """Exporta TODAS las conversaciones de WhatsApp y los leads (para análisis de ventas). Protegido."""
    clave = os.getenv("ADMIN_KEY") or os.getenv("META_VERIFY_TOKEN") or ""
    if not clave or key != clave:
        raise HTTPException(status_code=403, detail="clave invalida")
    from sqlalchemy import select as _sel
    from agent.memory import Lead, Mensaje, async_session

    async with async_session() as s:
        msgs = (await s.execute(_sel(Mensaje).order_by(Mensaje.id))).scalars().all()
        leads = (await s.execute(_sel(Lead))).scalars().all()
    return {
        "mensajes": [
            {"telefono": m.telefono, "role": m.role, "content": m.content,
             "ts": m.timestamp.isoformat() if m.timestamp else None}
            for m in msgs
        ],
        "leads": [
            {"telefono": l.telefono, "nombre": l.nombre, "interes": l.interes, "estado": l.estado,
             "equipo_atendio": l.equipo_atendio,
             "creado_en": l.creado_en.isoformat() if l.creado_en else None}
            for l in leads
        ],
        "anuncios": await origenes_anuncio(None),
    }


@app.post("/admin/recuperar")
async def recuperar_pendientes(key: str = ""):
    """
    Reactiva bajo demanda a las clientas que quedaron a medias (dentro de la ventana de 24 h):
    Sofía les manda UN mensaje cálido para retomar y cerrar. Útil tras una caída (p. ej. sin
    saldo de la API). Reusa el motor de 'toque de cierre' (respeta opt-out, no molesta a quien
    ya agendó ni a quien ya atendió el equipo, y no manda dos veces). Protegido con clave.
    """
    clave = os.getenv("ADMIN_KEY") or os.getenv("META_VERIFY_TOKEN") or ""
    if not clave or key != clave:
        raise HTTPException(status_code=403, detail="clave invalida")
    if proveedor is None:
        raise HTTPException(status_code=503, detail="proveedor no configurado")
    enviados = await followup._ciclo_nudge(proveedor)
    return {"status": "ok", "recuperados": enviados}


@app.get("/mensajes")
async def mensajes_report(key: str = "", canal: str = "todos", n: int = 10, tel: str = ""):
    """
    Últimas conversaciones (para el dueño). Protegido con clave.
    canal=whatsapp|todos · n=cuántas conversaciones · tel=fragmentos de teléfono separados
    por coma (para ver casos concretos, p.ej. tel=6708,9661).
    """
    clave = os.getenv("ADMIN_KEY") or os.getenv("META_VERIFY_TOKEN") or ""
    if not clave or key != clave:
        raise HTTPException(status_code=403, detail="clave invalida")
    filtro = [t.strip() for t in tel.split(",") if t.strip()] or None
    return await ultimas_conversaciones(
        max_convos=max(1, min(n, 50)),
        solo_whatsapp=(canal == "whatsapp"),
        filtro_tel=filtro,
    )


@app.get("/webhook")
async def webhook_verificacion(request: Request):
    """Verificacion GET del webhook. La pide Meta; para Zernio no hace nada."""
    if proveedor is None:
        raise HTTPException(status_code=503, detail=error_configuracion or "Proveedor no configurado")

    respuesta = await proveedor.validar_webhook(request)
    if respuesta is not None:
        return PlainTextResponse(respuesta)

    # Meta pide un 403 cuando manda hub.mode=subscribe y el verify_token no coincide.
    # Devolverle 200 le hace creer que la URL quedo verificada cuando no es cierto.
    if request.query_params.get("hub.mode") == "subscribe":
        raise HTTPException(status_code=403, detail="Verify token incorrecto")

    return {"status": "ok"}


async def _procesar_entrantes(request, tareas, prov, aplicar_horario):
    """
    Lógica común de un webhook entrante, compartida por WhatsApp y Messenger.

    Contesta 200 de inmediato y procesa cada mensaje en segundo plano. Esto NO es un
    detalle de estilo: los proveedores esperan un 2xx en unos 5 segundos y, si no lo
    reciben, reintentan el mismo evento hasta 7 veces. Por eso: responder primero,
    trabajar después, y deduplicar por id de evento.

    `aplicar_horario`: True en WhatsApp (Sofía se calla mientras atiende el equipo);
    en Messenger normalmente False (responde 24/7).
    """
    if prov is None:
        raise HTTPException(status_code=503, detail="Canal no configurado")

    if not await prov.verificar_firma(request):
        raise HTTPException(status_code=401, detail="Firma del webhook invalida")

    try:
        mensajes = await prov.parsear_webhook(request)
    except Exception as e:  # noqa: BLE001
        # Un payload raro no debe hacer que el proveedor reintente para siempre
        logger.error(f"No se pudo leer el webhook: {e}")
        return {"status": "ignorado"}

    # Horario de respuesta: dentro del horario del equipo humano, Sofía se calla y
    # deja que el equipo atienda (coexistencia). Solo aplica al canal que lo pida.
    en_horario_equipo = aplicar_horario and not sofia_debe_responder()

    encolados = 0
    for msg in mensajes:
        # Echo del EQUIPO humano (coexistencia): cuando una persona contesta desde la app de
        # WhatsApp Business, se guarda en el historial como turno del negocio ("assistant")
        # para que Sofía sepa qué ya dijo el equipo y no se confunda ni repita. NO se contesta
        # y se captura SIEMPRE (aunque sea horario de equipo).
        if msg.contexto.get("tipo") == "echo_equipo":
            eid = msg.contexto.get("evento_id") or msg.mensaje_id
            if eid and not await marcar_evento_procesado(eid):
                continue
            if msg.texto.strip():
                await guardar_mensaje(msg.telefono, "assistant", msg.texto)
                try:
                    await registrar_outbound_equipo(msg.telefono)
                except Exception:  # noqa: BLE001
                    pass
                logger.info(f"Equipo humano (coexistencia) → {msg.telefono}: {msg.texto[:60]}")
            continue

        # ¿Llegó tocando un anuncio de Meta? Se guarda SIEMPRE (aunque sea horario de equipo
        # o Sofía esté pausada): es lo que permite saber qué anuncio trae clientas y ventas.
        ref = msg.contexto.get("referral")
        if ref:
            try:
                await registrar_origen_anuncio(
                    msg.telefono, msg.contexto.get("evento_id") or msg.mensaje_id, ref, msg.texto
                )
                logger.info(f"[anuncio] {msg.telefono} llegó por el anuncio {ref.get('ad_id')} ({ref.get('titulo', '')[:40]})")
            except Exception as e:  # noqa: BLE001
                logger.error(f"No se pudo guardar el origen del anuncio: {e}")

        # PAUSA global (pruebas): Sofía no responde ni registra seguimiento. El echo del
        # equipo (arriba) sí se sigue guardando para no perder el hilo de la conversación.
        if sofia_pausada():
            continue

        if msg.es_propio or not msg.texto.strip():
            continue

        if msg.telefono in TELEFONOS_IGNORADOS:
            continue  # aviso automático (no es una clienta): no se contesta

        # La entrega es "al menos una vez": el mismo evento puede llegar dos veces
        evento_id = msg.contexto.get("evento_id") or msg.mensaje_id
        if evento_id and not await marcar_evento_procesado(evento_id):
            logger.info(f"Evento repetido, se ignora: {evento_id}")
            continue

        # En horario del equipo Sofía NO responde (atiende el equipo en vivo), PERO en
        # WhatsApp registra el lead para que ENTRE al seguimiento y no se enfríe: si la
        # clienta no agenda, el motor le manda un recordatorio (plantilla) pasadas 24 h,
        # en horario de Sofía (10-19h). No contesta el mismo momento, así no se encima con
        # la charla que el equipo esté teniendo en vivo.
        if mensaje_es_viejo(msg):
            if not msg.telefono.startswith("msgr:"):
                await registrar_inbound_lead(msg.telefono, msg.texto)
            logger.info(f"[mensaje viejo] no se contesta (> {MAX_ANTIGUEDAD_MIN} min): {msg.telefono}: {msg.texto[:60]}")
            continue

        if en_horario_equipo:
            if not msg.telefono.startswith("msgr:"):
                await registrar_inbound_lead(msg.telefono, msg.texto)
            logger.info(f"[horario equipo] lead registrado (Sofía no responde): {msg.telefono}")
            continue

        # El equipo le escribió hace poco: la charla la llevan ellos, Sofía no se mete.
        if await equipo_activo_reciente(msg.telefono, SILENCIO_TRAS_EQUIPO_MIN):
            if not msg.telefono.startswith("msgr:"):
                await registrar_inbound_lead(msg.telefono, msg.texto)
            logger.info(
                f"[equipo en vivo] Sofía no responde (el equipo escribió hace < "
                f"{SILENCIO_TRAS_EQUIPO_MIN} min): {msg.telefono}"
            )
            continue

        if _excede_tope_respuestas(msg.telefono):
            logger.warning(f"[tope] {msg.telefono} superó {MAX_RESPUESTAS_HORA} respuestas/hora; Sofía se calla")
            continue

        # Audio / foto / archivo: respuesta fija pidiendo que lo escriba (sin IA), 1 vez/30 min.
        if msg.contexto.get("tipo") == "media":
            if not msg.telefono.startswith("msgr:"):
                await registrar_inbound_lead(msg.telefono, "")
            await guardar_mensaje(msg.telefono, "user", msg.texto)
            ultima = _media_respondida.get(msg.telefono, 0)
            if _reloj.time() - ultima > 1800:
                if await prov.enviar_mensaje(msg.telefono, MENSAJE_MEDIA, msg.contexto):
                    _media_respondida[msg.telefono] = _reloj.time()
                    _respuestas_recientes[msg.telefono].append(_reloj.time())
                    await guardar_mensaje(msg.telefono, "assistant", MENSAJE_MEDIA)
                    logger.info(f"Media de {msg.telefono}: se le pidió escribirlo")
            continue

        logger.info(f"Mensaje de {msg.telefono}: {msg.texto}")
        _respuestas_recientes[msg.telefono].append(_reloj.time())
        tareas.add_task(procesar_mensaje, msg, prov)
        encolados += 1

    return {"status": "ok", "encolados": encolados, "horario_equipo": en_horario_equipo}


@app.post("/webhook")
async def webhook_handler(request: Request, tareas: BackgroundTasks):
    """Webhook de WhatsApp (Meta Cloud API). Respeta el horario del equipo humano."""
    if proveedor is None:
        raise HTTPException(status_code=503, detail=error_configuracion or "Proveedor no configurado")
    return await _procesar_entrantes(request, tareas, proveedor, aplicar_horario=True)


@app.get("/webhook/messenger")
async def messenger_verificacion(request: Request):
    """
    Verificación GET del webhook de Messenger (Meta manda hub.challenge).

    NO depende de que el Page Access Token ya esté puesto: valida el verify token
    directo del entorno. Así se puede verificar la URL en Meta ANTES de generar el
    token de la página (evita el problema del huevo y la gallina).
    """
    params = request.query_params
    verify_token = (
        os.getenv("MESSENGER_VERIFY_TOKEN")
        or os.getenv("META_VERIFY_TOKEN")
        or "agentkit-verify"
    )
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == verify_token:
        return PlainTextResponse(params.get("hub.challenge") or "")
    if params.get("hub.mode") == "subscribe":
        raise HTTPException(status_code=403, detail="Verify token incorrecto")
    return {"status": "ok"}


@app.post("/webhook/messenger")
async def messenger_handler(request: Request, tareas: BackgroundTasks):
    """Webhook de Facebook Messenger. Por defecto responde 24/7 (ver MESSENGER_RESPETA_HORARIO)."""
    return await _procesar_entrantes(
        request, tareas, proveedor_messenger, aplicar_horario=MESSENGER_RESPETA_HORARIO
    )


async def procesar_mensaje(msg: MensajeEntrante, prov):
    """
    Genera la respuesta y la manda de vuelta por `prov`. Corre fuera del ciclo del webhook.

    Se toma un candado por telefono: dos mensajes seguidos del mismo cliente se
    atienden en orden, no en paralelo, para que el historial no se mezcle.
    """
    evento_id = msg.contexto.get("evento_id") or msg.mensaje_id
    es_messenger = msg.telefono.startswith("msgr:")

    async with _candados[msg.telefono]:
        try:
            # Registrar/actualizar el lead para el seguimiento (reinicia la cadencia y
            # detecta opt-out). Solo WhatsApp: el motor de seguimiento manda plantillas de
            # Meta, que no existen en Messenger; registrar un PSID ahí haría que intentara
            # enviarle una plantilla de WhatsApp a un id que no es un teléfono.
            if not es_messenger:
                await registrar_inbound_lead(msg.telefono, msg.texto)

            # El historial se lee ANTES de guardar el mensaje actual: brain.py agrega
            # el mensaje nuevo al final, y asi no queda duplicado.
            historial = await obtener_historial(msg.telefono)
            respuesta, es_respuesta_real = await generar_respuesta(
                msg.texto, historial, msg.telefono
            )

            if not es_respuesta_real:
                logger.warning(f"Sofía no pudo generar respuesta para {msg.telefono}; no se envía nada (lo atiende el equipo)")
                return

            enviado = await prov.enviar_mensaje(msg.telefono, respuesta, msg.contexto)

            if not enviado:
                # El evento se marco como procesado ANTES de llegar hasta aca, para que dos
                # entregas simultaneas no se dupliquen. Si el envio fallo, hay que soltarlo:
                # si no, el reintento del proveedor se descartaria por duplicado y el cliente
                # se quedaria sin respuesta para siempre.
                logger.error(f"No se pudo enviar la respuesta a {msg.telefono}; se libera el evento")
                await liberar_evento(evento_id)
                return

            # Solo se guarda en el historial lo que de verdad es conversacion. Los avisos
            # tecnicos ("estoy teniendo problemas") no son un turno del agente: guardarlos
            # los deja contaminando el contexto de todos los mensajes que vengan despues.
            if es_respuesta_real:
                await guardar_mensaje(msg.telefono, "user", msg.texto)
                await guardar_mensaje(msg.telefono, "assistant", respuesta)

            logger.info(f"Respuesta enviada a {msg.telefono}: {respuesta}")

        except Exception as e:  # noqa: BLE001
            logger.exception(f"Error procesando el mensaje de {msg.telefono} (no se envía nada): {e}")
            await liberar_evento(evento_id)
