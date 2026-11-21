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

from agent import atribucion, followup
from agent.consola import chat_pausado
from agent.consola import router as consola_router
from agent.brain import estado_cerebro, generar_respuesta, separar_botones
from agent import ghl
from agent import ghl_proveedor  # noqa: F401  (registra la tabla ghl_token)
from agent import ghl_whatsapp  # noqa: F401  (registra la tabla pendiente_wa)
from agent import automatizaciones  # noqa: F401  (registra la tabla auto_enviado)
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
    negocio_ya_respondio,
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
# OJO: corto a propósito. Antes eran 12 h y, si el equipo contestaba a las 7 pm (desde el cel o
# la consola), Sofía se quedaba callada TODA la noche en ese chat, que es justo su turno.
SILENCIO_TRAS_EQUIPO_MIN = int(os.getenv("SOFIA_SILENCIO_TRAS_EQUIPO_MIN") or "45")

# Pedido de Joel (18-nov-2026), tras analizar 591 chats: en horario del equipo Sofía contesta
# el PRIMER mensaje de una clienta nueva (en segundos, "¿rostro o cuerpo?") y luego el equipo
# sigue la plática. Contestar en <5 min triplica las citas. Apagar: SOFIA_PRIMER_MENSAJE=0.
SOFIA_PRIMER_MENSAJE = os.getenv("SOFIA_PRIMER_MENSAJE", "1").strip().lower() not in ("0", "false", "no")

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
        # Atribución: detecta ventas de clientas que llegaron por anuncio y las reporta a Meta.
        asyncio.create_task(atribucion.loop_ventas())
        # Ventas marcadas en GHL ("Paquete vendido") → Compra en Meta.
        asyncio.create_task(ghl.loop_ventas_ghl())
        # Citas de Google Calendar → etapa de la tarjeta en GHL.
        from agent import ghl_agenda
        asyncio.create_task(ghl_agenda.loop())
        # Automatizaciones de GHL por WhatsApp: pendientes de la noche (9:00) y reactivación.
        from agent import ghl_whatsapp
        asyncio.create_task(ghl_whatsapp.loop(proveedor))
        # Embudo (21-nov): avisar a Meta quién agendó, rescate de faltas y reseñas de Google.
        asyncio.create_task(automatizaciones.loop(proveedor))
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

# APEX Console (bandeja en vivo del equipo): API protegida con X-Console-Key.
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://((apex|2body)-[a-z0-9-]+\.vercel\.app|localhost(:\d+)?|anegocios\.com)|http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["X-Console-Key", "Content-Type"],
)
app.include_router(consola_router)


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


@app.post("/ghl/importar")
async def ghl_importar(tareas: BackgroundTasks, key: str = "", probar: bool = True):
    """Carga a GHL las clientas del historial. probar=true solo cuenta (no sube nada)."""
    clave = os.getenv("ADMIN_KEY") or os.getenv("META_VERIFY_TOKEN") or ""
    if not clave or key != clave:
        raise HTTPException(status_code=403, detail="clave invalida")
    from collections import Counter
    from agent.memory import clientas_para_ghl
    clientas = await clientas_para_ghl()
    plan = dict(Counter(ghl.etapa_de(c) or "omitidas_opt_out" for c in clientas))
    if probar or not ghl.activo() or ghl.importacion.get("corriendo"):
        return {"total": len(clientas), "plan": plan, "activo": ghl.activo(), "estado": ghl.importacion}
    tareas.add_task(ghl.importar_historial, clientas)
    return {"total": len(clientas), "plan": plan, "iniciada": True}


@app.post("/ghl/ventas")
async def ghl_ventas(key: str = ""):
    """Corre ya la revisión de ventas del pipeline de GHL (normalmente corre sola)."""
    clave = os.getenv("ADMIN_KEY") or os.getenv("META_VERIFY_TOKEN") or ""
    if not clave or key != clave:
        raise HTTPException(status_code=403, detail="clave invalida")
    return await ghl.ciclo_ventas_ghl()


@app.post("/ghl/anegocios")
async def ghl_anegocios(request: Request, tareas: BackgroundTasks, key: str = ""):
    """Ventas de Anegocios ([[nombre, teléfono, total], ...]) → tarjetas en 'Paquete vendido'."""
    clave = os.getenv("ANEGOCIOS_SYNC_KEY") or ""
    if not clave or key != clave:
        raise HTTPException(status_code=403, detail="clave invalida")
    filas = await request.json()
    if not isinstance(filas, list):
        raise HTTPException(status_code=400, detail="se esperaba una lista")
    async def correr():
        r = await ghl.ligar_ventas_anegocios(filas[:2000])
        try:
            r["meta"] = await ghl.ciclo_ventas_ghl()
        except Exception as e:  # noqa: BLE001
            r["meta"] = str(e)[:200]
        ghl.ultimo_anegocios.clear()
        ghl.ultimo_anegocios.update(r)
        logger.info(f"[Anegocios→GHL] {r}")

    tareas.add_task(correr)
    return {"recibidas": len(filas), "procesando": True, "anterior": dict(ghl.ultimo_anegocios)}


@app.post("/ghl/anegocios/completo")
async def ghl_anegocios_completo(request: Request, tareas: BackgroundTasks, key: str = "", pagos: bool = True):
    """Anegocios completo ({dir, notas, abonos}) → contactos, tarjetas y pagos (facturas) en GHL."""
    from agent import ghl_anegocios

    clave = os.getenv("ANEGOCIOS_SYNC_KEY") or ""
    if not clave or key != clave:
        raise HTTPException(status_code=403, detail="clave invalida")
    datos = await request.json()
    if not isinstance(datos, dict) or not isinstance(datos.get("notas"), dict):
        raise HTTPException(status_code=400, detail="se esperaba {dir, notas, abonos}")

    async def correr():
        ghl_anegocios.ultimo.clear()
        ghl_anegocios.ultimo.update({"corriendo": True})
        try:
            r = await ghl_anegocios.sincronizar(datos, pagos=pagos)
        except Exception as e:  # noqa: BLE001
            r = {"error": str(e)[:300]}
        try:
            r["meta"] = await ghl.ciclo_ventas_ghl()
        except Exception as e:  # noqa: BLE001
            r["meta"] = str(e)[:200]
        ghl_anegocios.ultimo.clear()
        ghl_anegocios.ultimo.update(r)
        logger.info(f"[Anegocios→GHL completo] {r}")

    anterior = dict(ghl_anegocios.ultimo)
    tareas.add_task(correr)
    return {"clientas": len(datos.get("dir") or []), "tickets": len(datos["notas"]), "procesando": True,
            "anterior": anterior}


@app.get("/ghl/anegocios/completo")
async def ghl_anegocios_completo_estado(key: str = ""):
    from agent import ghl_anegocios

    clave = os.getenv("ANEGOCIOS_SYNC_KEY") or ""
    if not clave or key != clave:
        raise HTTPException(status_code=403, detail="clave invalida")
    return dict(ghl_anegocios.ultimo)


@app.post("/ghl/whatsapp")
@app.post("/conexion/whatsapp")  # por si GHL no acepta "ghl" en la URL del webhook
async def ghl_whatsapp(request: Request, plantilla: str = ""):
    """
    Acción Webhook de un workflow de GHL → plantilla de WhatsApp desde el número de 2Body.
    Sin clave: Sofía verifica en GHL (con su token) que la clienta y el motivo sean reales.
    """
    from agent import ghl_whatsapp as gw

    try:
        datos = await request.json()
    except Exception:  # noqa: BLE001
        datos = {}
    if not isinstance(datos, dict):
        datos = {}
    return await gw.enviar(plantilla, datos, proveedor)


@app.get("/ghl/whatsapp")
async def ghl_whatsapp_estado(key: str = ""):
    from agent import ghl_whatsapp as gw

    if not gw.clave_admin(key):
        raise HTTPException(status_code=403, detail="clave invalida")
    return {**gw.estado, "plantillas": {k: v[0] for k, v in gw.PLANTILLAS.items()}}


@app.post("/ghl/agenda")
async def ghl_agenda_ahora(tareas: BackgroundTasks, key: str = ""):
    """Corre ya la revisión de Google Calendar → GHL (normalmente corre sola cada 6 h)."""
    clave = os.getenv("ADMIN_KEY") or os.getenv("META_VERIFY_TOKEN") or ""
    if not clave or key != clave:
        raise HTTPException(status_code=403, detail="clave invalida")
    from agent import ghl_agenda
    tareas.add_task(ghl_agenda.ciclo)
    return {"procesando": True, "anterior": dict(ghl_agenda.ultimo)}


@app.post("/ghl/espejo")
async def ghl_espejo_rellenar(tareas: BackgroundTasks, key: str = "", dias: int = 14, hasta_id: int = 0):
    """Copia a GHL → Conversaciones el historial de WhatsApp de los últimos `dias` (una vez)."""
    clave = os.getenv("ADMIN_KEY") or os.getenv("META_VERIFY_TOKEN") or ""
    if not clave or key != clave:
        raise HTTPException(status_code=403, detail="clave invalida")
    from agent import ghl_espejo
    tareas.add_task(ghl_espejo.rellenar, max(1, min(dias, 60)), hasta_id or None)
    return {"procesando": True}


@app.get("/ghl/espejo")
async def ghl_espejo_estado(key: str = ""):
    clave = os.getenv("ADMIN_KEY") or os.getenv("META_VERIFY_TOKEN") or ""
    if not clave or key != clave:
        raise HTTPException(status_code=403, detail="clave invalida")
    from agent import ghl_espejo
    return {"activo": ghl_espejo.activo(), "en_fila": ghl_espejo.pendientes(), **ghl_espejo.estado}


@app.get("/ghl/oauth")
@app.get("/conexion/oauth")  # GHL no acepta URLs que digan "ghl"/"highlevel"
async def ghl_oauth(code: str = ""):
    """GHL regresa aquí al instalar la app "Sofía WhatsApp" en la sub-cuenta."""
    from fastapi.responses import HTMLResponse

    if not code:
        raise HTTPException(status_code=400, detail="falta code")
    try:
        r = await ghl_proveedor.instalar(code)
    except Exception as e:  # noqa: BLE001
        logger.error(f"[GHL proveedor] instalación falló: {e}")
        return HTMLResponse(f"<h2>No se pudo instalar</h2><p>{str(e)[:300]}</p>", status_code=500)
    return HTMLResponse(
        "<h2>Listo ✅</h2><p>Sofía WhatsApp quedó conectada a GHL (" + (r.get("locationId") or "") +
        "). Ya puedes cerrar esta pestaña.</p>")


@app.post("/ghl/proveedor")
@app.post("/conexion/entrega")  # misma razón: sin "ghl" en la URL
async def ghl_proveedor_delivery(request: Request, tareas: BackgroundTasks):
    """Delivery URL: el equipo escribió en GHL → se manda por WhatsApp."""
    cuerpo = await request.body()
    if not ghl_proveedor.firma_valida(cuerpo, request.headers.get("x-ghl-signature")):
        logger.warning("[GHL proveedor] firma inválida: rechazado")
        raise HTTPException(status_code=401, detail="firma invalida")
    import json as _json

    datos = _json.loads(cuerpo or b"{}")
    mid = datos.get("messageId") or ""
    if mid and not await marcar_evento_procesado("ghl-" + mid):
        return {"status": "repetido"}
    tareas.add_task(ghl_proveedor.entregar, datos)
    return {"status": "ok"}


@app.get("/ghl/proveedor")
async def ghl_proveedor_estado(key: str = ""):
    clave = os.getenv("ADMIN_KEY") or os.getenv("META_VERIFY_TOKEN") or ""
    if not clave or key != clave:
        raise HTTPException(status_code=403, detail="clave invalida")
    return await ghl_proveedor.info()


@app.get("/ghl/importar")
async def ghl_importar_estado(key: str = ""):
    clave = os.getenv("ADMIN_KEY") or os.getenv("META_VERIFY_TOKEN") or ""
    if not clave or key != clave:
        raise HTTPException(status_code=403, detail="clave invalida")
    return ghl.importacion


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


@app.get("/reporte/asistencia")
async def reporte_asistencia(key: str = "", dias: int = 30):
    """Primeras visitas de los últimos N días: cuántas se marcaron como falta y cuántas compraron."""
    clave = os.getenv("ADMIN_KEY") or os.getenv("META_VERIFY_TOKEN") or ""
    if not clave or key != clave:
        raise HTTPException(status_code=403, detail="clave invalida")
    rep = await automatizaciones.reporte_asistencia(max(1, min(dias, 120)))
    return {**rep, "automatizaciones": dict(automatizaciones.ultimo)}


@app.get("/reporte/ventas")
async def reporte_ventas(key: str = "", dias: int = 7, actualizar: bool = True):
    """
    Ventas generadas por los anuncios de Meta (clientas que llegaron por anuncio y luego
    tuvieron sesión pagada en la agenda). actualizar=true revisa la agenda antes de responder
    y reporta a Meta las compras nuevas. Protegido con clave.
    """
    clave = os.getenv("ADMIN_KEY") or os.getenv("META_VERIFY_TOKEN") or ""
    if not clave or key != clave:
        raise HTTPException(status_code=403, detail="clave invalida")
    ciclo = await atribucion.ciclo_ventas() if actualizar else None
    rep = await atribucion.reporte(max(1, min(dias, 365)))
    rep["ciclo"] = ciclo
    return rep


@app.post("/admin/origen")
async def registrar_origen_manual(key: str = "", tel: str = "", titulo: str = "", fecha: str = ""):
    """
    Registra a mano de qué anuncio llegó una clienta (verificado en la bandeja de Meta, donde
    el chat dice "Este chat contiene una respuesta a <anuncio>"). Para chats de antes de que
    Sofía guardara el anuncio. fecha = AAAA-MM-DD del primer mensaje. Protegido con clave.
    """
    clave = os.getenv("ADMIN_KEY") or os.getenv("META_VERIFY_TOKEN") or ""
    if not clave or key != clave:
        raise HTTPException(status_code=403, detail="clave invalida")
    digitos = "".join(c for c in tel if c.isdigit())
    if len(digitos) < 10 or not titulo:
        raise HTTPException(status_code=400, detail="falta tel o titulo")
    from datetime import timezone as _tz

    from agent.memory import OrigenAnuncio, async_session

    tel_wa = "521" + digitos[-10:]
    cuando = datetime.fromisoformat(fecha).replace(tzinfo=_tz.utc) if fecha else datetime.now(_tz.utc)
    async with async_session() as s:
        evento = f"manual:{tel_wa}"
        if await s.get(OrigenAnuncio, evento) is None:
            s.add(OrigenAnuncio(evento_id=evento, telefono=tel_wa, titulo=titulo[:200], creado_en=cuando))
            await s.commit()
    return {"ok": True, "telefono": tel_wa, "titulo": titulo, "otra_agencia": atribucion.es_de_otra_agencia(titulo)}


@app.post("/admin/directorio")
async def cargar_directorio(request: Request, key: str = "", fuente: str = "anegocios"):
    """
    Carga/reemplaza el directorio de clientas (nombre → teléfono), p. ej. exportado de
    Anegocios. Cuerpo JSON: [{"nombre": "...", "telefono": "..."}]. Protegido con clave.
    Sirve para el recordatorio del día antes de citas que el equipo apuntó solo con nombre.
    """
    clave = os.getenv("ADMIN_KEY") or os.getenv("META_VERIFY_TOKEN") or ""
    if not clave or key != clave:
        raise HTTPException(status_code=403, detail="clave invalida")
    from agent.memory import reemplazar_directorio

    items = await request.json()
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="se espera una lista")
    n = await reemplazar_directorio(items, fuente=fuente[:40])
    return {"ok": True, "guardados": n, "fuente": fuente}


@app.post("/admin/enviar")
async def enviar_manual(request: Request, key: str = "", tel: str = ""):
    """
    Manda un mensaje de texto (dentro de las 24 h de WhatsApp) por el número de Sofía y lo
    guarda en el historial, para que Sofía tenga el contexto cuando la clienta conteste.
    Cuerpo JSON: {"texto": "..."}. Protegido con clave.
    """
    clave = os.getenv("ADMIN_KEY") or os.getenv("META_VERIFY_TOKEN") or ""
    if not clave or key != clave:
        raise HTTPException(status_code=403, detail="clave invalida")
    if proveedor is None:
        raise HTTPException(status_code=503, detail="proveedor no configurado")
    texto = ((await request.json()) or {}).get("texto", "").strip()
    digitos = "".join(c for c in tel if c.isdigit())
    if len(digitos) < 10 or not texto:
        raise HTTPException(status_code=400, detail="falta tel o texto")
    tel_wa = digitos if len(digitos) > 10 else "521" + digitos
    ok = await proveedor.enviar_mensaje(tel_wa, texto)
    if ok:
        await guardar_mensaje(tel_wa, "assistant", texto)
    return {"ok": bool(ok), "telefono": tel_wa}


@app.post("/admin/nudge_equipo/rearmar")
async def rearmar_nudge_equipo(key: str = ""):
    """
    Vuelve a habilitar el recordatorio ligero en charlas del equipo que siguen esperando
    respuesta de la clienta (dentro de 24 h). Útil tras ajustar el criterio. Protegido.
    """
    clave = os.getenv("ADMIN_KEY") or os.getenv("META_VERIFY_TOKEN") or ""
    if not clave or key != clave:
        raise HTTPException(status_code=403, detail="clave invalida")
    from datetime import timedelta as _td
    from sqlalchemy import update as _upd

    from agent.memory import Lead, ahora, async_session

    async with async_session() as s:
        r = await s.execute(
            _upd(Lead)
            .where(
                Lead.equipo_atendio.is_(True),
                Lead.estado == "activo",
                Lead.ultimo_equipo_en > Lead.last_inbound,
                Lead.last_inbound >= ahora() - _td(hours=23),
            )
            .values(nudge_equipo_enviado=False)
        )
        await s.commit()
    return {"ok": True, "rearmados": r.rowcount}


@app.get("/admin/remarketing")
async def remarketing(key: str = "", dias: int = 60):
    """
    Público de remarketing para Meta: quienes escribieron por WhatsApp en los últimos `dias` y NO
    agendaron (sin cita en la agenda, no agendadas, sin baja). Devuelve los teléfonos YA CIFRADOS
    (SHA-256, formato de Meta: 52+10 dígitos y 521+10), nunca en claro. Protegido con clave.
    """
    clave = os.getenv("ADMIN_KEY") or os.getenv("META_VERIFY_TOKEN") or ""
    if not clave or key != clave:
        raise HTTPException(status_code=403, detail="clave invalida")
    import hashlib
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from sqlalchemy import select as _sel
    from agent import calendar_tool
    from agent.memory import Lead, async_session

    dias = max(7, min(dias, 180))
    desde = _dt.now(_tz.utc) - _td(days=dias)
    con_cita = await asyncio.to_thread(calendar_tool.telefonos_con_cita, dias + 30, 90)
    async with async_session() as s:
        leads = (await s.execute(_sel(Lead).where(Lead.last_inbound >= desde))).scalars().all()
    hashes, n = [], 0
    for l in leads:
        t10 = "".join(c for c in l.telefono if c.isdigit())[-10:]
        if len(t10) != 10 or l.telefono.startswith("msgr:") or l.estado in ("opt_out", "agendado") or t10 in con_cita:
            continue
        n += 1
        for v in ("52" + t10, "521" + t10):
            hashes.append(hashlib.sha256(v.encode()).hexdigest())
    return {"dias": dias, "personas": n, "con_cita_excluidas": len(con_cita), "hashes": hashes}


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
                # Se marca como del EQUIPO para que Sofía distinga lo que prometió una persona
                # (y lo respete: no lo cambia ni lo vuelve a ofrecer).
                await guardar_mensaje(msg.telefono, "assistant", "[Equipo] " + msg.texto)
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

        primer_mensaje = (
            en_horario_equipo
            and SOFIA_PRIMER_MENSAJE
            and not msg.telefono.startswith("msgr:")
            and msg.contexto.get("tipo") != "media"
            and not await negocio_ya_respondio(msg.telefono)
        )
        if primer_mensaje:
            logger.info(f"[horario equipo] clienta nueva: Sofía contesta el primer mensaje: {msg.telefono}")
        elif en_horario_equipo:
            if not msg.telefono.startswith("msgr:"):
                await registrar_inbound_lead(msg.telefono, msg.texto)
                # Se guarda en el historial para que Sofía tenga TODO el contexto (lo que dijo
                # la clienta y lo que contestó el equipo) si después le manda un recordatorio.
                await guardar_mensaje(msg.telefono, "user", msg.texto)
            logger.info(f"[horario equipo] lead registrado (Sofía no responde): {msg.telefono}")
            continue

        # El equipo le escribió hace poco: la charla la llevan ellos, Sofía no se mete.
        if await equipo_activo_reciente(msg.telefono, SILENCIO_TRAS_EQUIPO_MIN):
            if not msg.telefono.startswith("msgr:"):
                await registrar_inbound_lead(msg.telefono, msg.texto)
                await guardar_mensaje(msg.telefono, "user", msg.texto)
            logger.info(
                f"[equipo en vivo] Sofía no responde (el equipo escribió hace < "
                f"{SILENCIO_TRAS_EQUIPO_MIN} min): {msg.telefono}"
            )
            continue

        # El equipo apagó a Sofía en ESTE chat desde la consola: se guarda y no se contesta.
        if await chat_pausado(msg.telefono):
            if not msg.telefono.startswith("msgr:"):
                await registrar_inbound_lead(msg.telefono, msg.texto)
            await guardar_mensaje(msg.telefono, "user", msg.texto)
            logger.info(f"[chat pausado en consola] Sofía no responde: {msg.telefono}")
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
                ghl.programar(msg.telefono, "nuevo")  # tarjeta en GHL (segundo plano)

            # El historial se lee ANTES de guardar el mensaje actual: brain.py agrega
            # el mensaje nuevo al final, y asi no queda duplicado.
            historial = await obtener_historial(msg.telefono)
            respuesta, es_respuesta_real = await generar_respuesta(
                msg.texto, historial, msg.telefono
            )

            if not es_respuesta_real:
                # Se guarda lo que escribió, para no perderlo: el equipo y el relevo lo ven.
                await guardar_mensaje(msg.telefono, "user", msg.texto)
                logger.warning(f"Sofía no pudo generar respuesta para {msg.telefono}; no se envía nada (lo atiende el equipo)")
                return

            # Re-chequeo JUSTO antes de enviar: mientras Sofía pensaba (10-30 s con agenda), una
            # persona del equipo pudo contestar desde el cel o la consola, o apagarla en este chat.
            # Si pasó, NO se manda nada (se encimaría con el equipo); solo se guarda lo de la clienta.
            if await equipo_activo_reciente(msg.telefono, SILENCIO_TRAS_EQUIPO_MIN) or await chat_pausado(msg.telefono):
                await guardar_mensaje(msg.telefono, "user", msg.texto)
                logger.info(f"[equipo entró mientras Sofía pensaba] no se envía su respuesta: {msg.telefono}")
                return

            # Si Sofía pidió botones ([BOTONES: a | b]), sale como mensaje con botones.
            respuesta, botones = separar_botones(respuesta)
            if botones:
                enviado = await prov.enviar_botones(msg.telefono, respuesta, botones, msg.contexto)
            else:
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
                if not es_messenger:
                    ghl.programar(msg.telefono, "contactada")

            logger.info(f"Respuesta enviada a {msg.telefono}: {respuesta}")

        except Exception as e:  # noqa: BLE001
            logger.exception(f"Error procesando el mensaje de {msg.telefono} (no se envía nada): {e}")
            await liberar_evento(evento_id)
