"""
Automatizaciones de GHL que salen por WhatsApp (en lugar de SMS).

En un workflow de GHL, donde iría "Send SMS", se pone una acción **Webhook** (POST) a:

    https://<dominio>/conexion/whatsapp?plantilla=<nombre corto>

GHL manda en el cuerpo los datos de la clienta (contact_id, phone, first_name...). La URL NO
lleva clave: en vez de confiar en quien llama, Sofía lee la clienta DIRECTO de GHL (por su
contact_id, con su propio token) y solo manda si GHL confirma el motivo:
  • pago_recibido   → hay un pago de esa clienta en GHL en las últimas 48 h (el monto sale de ahí)
  • valoracion_dN   → su tarjeta sigue en "Valoración realizada" o "Reactivación"
  • reactivacion    → tiene la etiqueta `reactivar`
  • cumpleanos      → su fecha de nacimiento en GHL es hoy
El teléfono y el nombre también salen de GHL, no del cuerpo. Así nadie puede usar el endpoint
para mandar mensajes a números ajenos ni fuera de motivo. El mensaje queda en el historial (y
por el espejo, en Conversaciones de GHL).

Reglas de seguridad:
  • Solo plantillas de esta lista (nada de texto libre: fuera de 24 h WhatsApp no lo permite).
  • No se manda a quien pidió baja (lead en opt_out).
  • Una sola vez por clienta, plantilla y día (si GHL reintenta o el workflow se repite).
  • Solo de 9:00 a 20:00 (hora de Pachuca). Lo que llega fuera de ese horario (p. ej. pagos que
    registra la sincronización nocturna) se guarda y sale a las 9:00 (tabla pendiente_wa).

Reactivación: una vez al día (10:00) Sofía revisa las tarjetas en "Paquete vendido"/"En
tratamiento" que no cambian desde hace 45+ días y cuya clienta NO tiene cita en Google Calendar
en los últimos 45 días ni en los próximos 60 (por teléfono o por nombre). A esas les pone la
etiqueta `reactivar` en GHL; el workflow de GHL "Reactivación por WhatsApp" la detecta, manda la
plantilla (vía este endpoint) y quita la etiqueta. Para no repetir, el workflow pone
`reactivacion_enviada` y la quita 60 días después; aquí se salta a quien la tenga.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime, Integer, String, Text, delete, select
from sqlalchemy.orm import Mapped, mapped_column

from agent.memory import Base, ahora, async_session

logger = logging.getLogger("agentkit")
TZ = ZoneInfo("America/Mexico_City")

# nombre corto → (plantilla en Meta, texto para el historial con {nombre}/{monto})
PLANTILLAS: dict[str, tuple[str, str]] = {
    "pago_recibido": (
        "pago_recibido",
        "Hola {nombre}, en 2Body Pachuca recibimos tu pago de {monto}. ¡Gracias por tu confianza! "
        "Cualquier duda, escríbenos por aquí 💜"),
    "valoracion_d3": (
        "valoracion_sin_compra_1",
        "Hola {nombre} 💜 Gracias por venir a tu valoración en 2Body Pachuca. ¿Te quedó alguna duda "
        "sobre el plan que vimos? Escríbenos por aquí y te ayudamos a elegir la mejor opción."),
    "valoracion_d7": (
        "valoracion_sin_compra_2",
        "Hola {nombre}, ¿cómo vas? ✨ Seguimos guardando tu plan de 2Body Pachuca. Si quieres, te "
        "apartamos tu primera sesión esta semana. ¿Te acomoda mejor entre semana o sábado?"),
    "valoracion_d14": (
        "valoracion_sin_compra_3",
        "Hola {nombre} 💜 Solo para no perderte la pista: tu plan de 2Body Pachuca sigue disponible. "
        "Si quieres retomarlo, respóndenos por aquí y lo vemos juntas."),
    "reactivacion": (
        "reactivacion_clienta",
        "Hola {nombre} 💜 Te extrañamos en 2Body Pachuca. ¿Te apartamos un espacio para retomar tu "
        "tratamiento? Respóndenos por aquí y vemos el día que mejor te acomode."),
    "cumpleanos": (
        "cumpleanos_clienta",
        "¡Feliz cumpleaños, {nombre}! 🎂💜 Todo el equipo de 2Body Pachuca te desea un día increíble. "
        "Gracias por dejarnos ser parte de tu cuidado."),
}

estado: dict = {"recibidos": 0, "enviados": 0, "omitidos": 0, "fallidos": 0, "programados": 0,
                "ultimo": "", "reactivacion": {}}


class PendienteWA(Base):
    """Aviso que llegó fuera de horario: sale en la siguiente ventana (9:00)."""

    __tablename__ = "pendiente_wa"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    corto: Mapped[str] = mapped_column(String(40))
    datos: Mapped[str] = mapped_column(Text)
    creado: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


def clave_admin(key: str) -> bool:
    clave = os.getenv("ADMIN_KEY") or ""
    return bool(clave) and key == clave


def _campo(datos: dict, *nombres: str) -> str:
    """Busca un campo en el cuerpo de GHL (raíz, customData o contact)."""
    for fuente in (datos.get("customData") or {}, datos, datos.get("contact") or {}):
        if not isinstance(fuente, dict):
            continue
        for n in nombres:
            v = fuente.get(n)
            if v not in (None, ""):
                return str(v).strip()
    return ""


def telefono(datos: dict) -> str:
    """Teléfono en formato de WhatsApp México (521 + 10 dígitos)."""
    d = re.sub(r"\D", "", _campo(datos, "phone", "telefono", "Phone"))
    if len(d) < 10:
        return ""
    if d.startswith("52") or len(d) == 10:
        return "521" + d[-10:]
    return d  # número extranjero: tal cual


def nombre(datos: dict) -> str:
    n = _campo(datos, "nombre", "first_name", "firstName")
    if not n:
        n = _campo(datos, "full_name", "name").split(" ")[0] if _campo(datos, "full_name", "name") else ""
    n = n.split(" ")[0] if n else ""
    return n.title() if n else "hola"


def monto(datos: dict) -> str:
    m = _campo(datos, "monto", "amount", "total")
    try:
        v = float(re.sub(r"[^\d.]", "", m))
    except ValueError:
        return ""
    if v <= 0:
        return ""
    return f"${v:,.2f}".replace(".00", "")


_aprobadas: dict = {"en": None, "nombres": set()}


async def plantilla_aprobada(nombre_meta: str) -> bool:
    """¿Meta ya aprobó la plantilla? (se consulta cada 30 min; si no se puede saber, se asume que sí)."""
    import httpx

    token = os.getenv("META_ACCESS_TOKEN") or ""
    waba = os.getenv("META_WABA_ID") or "1783295019696260"
    if not token:
        return True
    if not _aprobadas["en"] or datetime.now(timezone.utc) - _aprobadas["en"] > timedelta(minutes=30):
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(f"https://graph.facebook.com/{os.getenv('META_API_VERSION') or 'v25.0'}/{waba}/message_templates",
                                params={"fields": "name,status", "limit": 200, "access_token": token})
            if r.status_code == 200:
                _aprobadas["nombres"] = {t["name"] for t in r.json().get("data", []) if t.get("status") == "APPROVED"}
                _aprobadas["en"] = datetime.now(timezone.utc)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[GHL→WhatsApp] no pude consultar plantillas: {e}")
            return True
    return not _aprobadas["en"] or nombre_meta in _aprobadas["nombres"]


def en_horario(now: datetime | None = None) -> bool:
    now = now or datetime.now(TZ)
    return 9 <= now.hour < 20


async def _verificar(corto: str, contact_id: str) -> tuple[dict | None, list[str], str]:
    """Lee la clienta en GHL y confirma el motivo. (datos, params, error)."""
    from agent import ghl

    if not contact_id or not ghl.activo():
        return None, [], "sin_contacto"
    async with ghl._cliente() as c:
        r = await c.get(f"/contacts/{contact_id}")
        if r.status_code != 200:
            return None, [], "contacto_no_existe"
        con = r.json().get("contact") or {}
        datos = {"phone": con.get("phone") or "", "first_name": con.get("firstName") or "",
                 "full_name": con.get("contactName") or con.get("name") or ""}
        params = [nombre(datos)]
        hoy = datetime.now(TZ)
        if corto == "pago_recibido":
            r = await c.get("/payments/transactions", params={
                "altId": ghl._loc(), "altType": "location", "contactId": contact_id, "limit": 5})
            txs = (r.json().get("data") if r.status_code == 200 else None) or []
            limite = datetime.now(timezone.utc) - timedelta(hours=48)
            reciente = None
            for tx in txs:
                try:
                    f = datetime.fromisoformat((tx.get("createdAt") or "").replace("Z", "+00:00"))
                except ValueError:
                    continue
                if f >= limite and (tx.get("status") in (None, "succeeded", "paid")):
                    reciente = tx
                    break
            m = monto({"monto": (reciente or {}).get("amount")})
            if not m:
                return None, [], "sin_pago_reciente"
            params.append(m)
        elif corto.startswith("valoracion_"):
            pipe = await ghl._cargar_pipeline(c)
            validas = {pipe["etapas"][k][0] for k in ("realizada", "reactivacion") if k in pipe["etapas"]}
            r = await c.get("/opportunities/search", params={
                "location_id": ghl._loc(), "pipeline_id": pipe["id"], "contact_id": contact_id, "limit": 20})
            ops = r.json().get("opportunities", []) if r.status_code == 200 else []
            if not any(o.get("pipelineStageId") in validas and o.get("status", "open") == "open" for o in ops):
                return None, [], "ya_no_esta_en_valoracion"
        elif corto == "reactivacion":
            # El workflow quita `reactivar` y pone `reactivacion_enviada` justo después de
            # llamar aquí; si el aviso quedó guardado para más tarde, vale cualquiera de las dos.
            if not {"reactivar", "reactivacion_enviada"} & {t.lower() for t in (con.get("tags") or [])}:
                return None, [], "sin_etiqueta_reactivar"
        elif corto == "cumpleanos":
            dob = (con.get("dateOfBirth") or "")[:10]
            if dob[5:] != hoy.strftime("%m-%d"):
                return None, [], "no_es_su_cumpleanos"
    return datos, params, ""


async def enviar(corto: str, datos: dict, proveedor) -> dict:
    """Manda la plantilla. Regresa {"status": ...} para que GHL lo deje en su historial."""
    from agent import memory

    estado["recibidos"] += 1
    if corto not in PLANTILLAS:
        return {"status": "plantilla_desconocida", "validas": sorted(PLANTILLAS)}
    contact_id = _campo(datos, "contact_id", "contactId")
    if not en_horario() or not await plantilla_aprobada(PLANTILLAS[corto][0]):
        # Fuera de horario, o Meta aún no aprueba la plantilla: se guarda y se reintenta
        # (cada 10 min en horario, hasta 3 días).
        async with async_session() as ses:
            ses.add(PendienteWA(corto=corto, datos=json.dumps({"contact_id": contact_id})))
            await ses.commit()
        estado["programados"] += 1
        return {"status": "programado"}

    try:
        ghl_datos, params, error = await _verificar(corto, contact_id)
    except Exception as e:  # noqa: BLE001
        logger.error(f"[GHL→WhatsApp] no pude verificar en GHL: {e}")
        ghl_datos, params, error = None, [], "error_ghl"
    if error:
        estado["omitidos"] += 1
        return {"status": error}
    tel = telefono(ghl_datos)
    if not tel:
        estado["omitidos"] += 1
        return {"status": "sin_telefono"}
    if await memory.estado_lead(tel[-10:]) == "opt_out":
        estado["omitidos"] += 1
        return {"status": "pidio_baja"}

    plantilla, texto = PLANTILLAS[corto]
    hoy = datetime.now(TZ).date().isoformat()
    clave = f"ghlwa:{corto}:{tel[-10:]}:{hoy}" + (f":{params[1]}" if len(params) > 1 else "")
    if not await memory.marcar_evento_procesado(clave):
        estado["omitidos"] += 1
        return {"status": "ya_enviado_hoy"}

    if proveedor is None or not hasattr(proveedor, "enviar_plantilla"):
        await memory.liberar_evento(clave)
        return {"status": "sin_proveedor"}
    try:
        ok = await proveedor.enviar_plantilla(tel, plantilla, params, os.getenv("FOLLOWUP_LANG") or "es_MX")
    except Exception as e:  # noqa: BLE001
        logger.error(f"[GHL→WhatsApp] error mandando {plantilla} a …{tel[-4:]}: {e}")
        ok = False
    if not ok:
        await memory.liberar_evento(clave)  # p. ej. plantilla aún no aprobada: GHL puede reintentar
        estado["fallidos"] += 1
        estado["ultimo"] = f"{plantilla} rechazada para …{tel[-4:]}"
        return {"status": "whatsapp_rechazo"}

    texto = texto.format(nombre=params[0], monto=params[1] if len(params) > 1 else "")
    try:
        await memory.guardar_mensaje(tel, "assistant", "[Recordatorio automático] " + texto)
    except Exception as e:  # noqa: BLE001
        logger.error(f"[GHL→WhatsApp] no se guardó en historial …{tel[-4:]}: {e}")
    estado["enviados"] += 1
    estado["ultimo"] = f"{plantilla} → …{tel[-4:]}"
    logger.info(f"[GHL→WhatsApp] {plantilla} enviada a …{tel[-4:]} ({params})")
    return {"status": "enviado", "plantilla": plantilla}


async def enviar_pendientes(proveedor) -> int:
    """Manda lo que quedó guardado fuera de horario (los de hace más de 3 días se descartan)."""
    if not en_horario():
        return 0
    limite = ahora() - timedelta(days=3)
    listos = []
    async with async_session() as ses:
        for f in list((await ses.execute(select(PendienteWA).order_by(PendienteWA.id))).scalars()):
            creado = f.creado if f.creado.tzinfo else f.creado.replace(tzinfo=timezone.utc)
            if creado < limite:
                await ses.delete(f)  # más de 3 días: ya no tiene sentido
            elif f.corto in PLANTILLAS and await plantilla_aprobada(PLANTILLAS[f.corto][0]):
                listos.append((f.corto, f.datos))
                await ses.delete(f)
            # si la plantilla sigue sin aprobar, se queda (conserva su fecha original)
        await ses.commit()
    n = 0
    for corto, datos in listos:
        r = await enviar(corto, json.loads(datos), proveedor)
        n += r.get("status") == "enviado"
        await asyncio.sleep(1.5)
    return n


def _norm(t: str) -> str:
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z ]", " ", t)


def _ocupadas(dias_atras: int = 45, dias_adelante: int = 60) -> tuple[set[str], str]:
    """(teléfonos de 10 dígitos, texto normalizado de todos los títulos) con cita en la ventana."""
    from agent import calendar_tool as ct

    svc = ct._obtener_servicio()
    if not svc:
        raise RuntimeError("sin acceso al calendario")
    hoy = datetime.now(TZ)
    ini, fin = (hoy - timedelta(days=dias_atras)).isoformat(), (hoy + timedelta(days=dias_adelante)).isoformat()
    tels: set[str] = set()
    textos = []
    for cal_id in ct._calendarios_disponibilidad():
        for ev in ct._listar_eventos(svc, cal_id, ini, fin, estricto=True):
            if ev.get("status") == "cancelled":
                continue
            texto = f"{ev.get('summary', '')} {ev.get('description', '')}"
            textos.append(_norm(texto))
            for m in re.findall(r"(?:\d[\s\-]?){10,13}", texto):
                d = re.sub(r"\D", "", m)
                if len(d) >= 10:
                    tels.add(d[-10:])
    return tels, " | ".join(textos)


def _tiene_cita_por_nombre(nombre_completo: str, textos: str) -> bool:
    """Coincide si su nombre de pila y un apellido aparecen juntos en el título de alguna cita."""
    partes = [p for p in _norm(nombre_completo).split() if len(p) > 2]
    if len(partes) < 2:
        return False
    for bloque in textos.split(" | "):
        palabras = set(bloque.split())
        if sum(p in palabras for p in partes) >= 2:
            return True
    return False


async def ciclo_reactivacion(dias: int = 45) -> dict:
    """Pone la etiqueta `reactivar` en GHL a clientas que compraron y no tienen cita hace 45+ días."""
    from agent import ghl

    if not ghl.activo():
        return {"activo": False}
    if not await plantilla_aprobada(PLANTILLAS["reactivacion"][0]):
        return {"esperando": "plantilla reactivacion_clienta sin aprobar"}
    tels, textos = await asyncio.to_thread(_ocupadas, dias, 60)
    corte = datetime.now(timezone.utc) - timedelta(days=dias)
    marcadas, revisadas = [], 0
    async with ghl._cliente() as c:
        pipe = await ghl._cargar_pipeline(c)
        for clave in ("vendido", "tratamiento"):
            if clave not in pipe["etapas"]:
                continue
            pagina = 1
            while pagina <= 20:
                r = await c.get("/opportunities/search", params={
                    "location_id": ghl._loc(), "pipeline_id": pipe["id"],
                    "pipeline_stage_id": pipe["etapas"][clave][0], "limit": 100, "page": pagina})
                r.raise_for_status()
                ops = r.json().get("opportunities", [])
                for o in ops:
                    revisadas += 1
                    con = o.get("contact") or {}
                    cid = o.get("contactId") or con.get("id")
                    tel = re.sub(r"\D", "", con.get("phone") or "")[-10:]
                    cambio = o.get("lastStageChangeAt") or o.get("createdAt") or ""
                    try:
                        viejo = datetime.fromisoformat(cambio.replace("Z", "+00:00")) < corte
                    except ValueError:
                        viejo = False
                    etiquetas = {t.lower() for t in (con.get("tags") or [])}
                    if (not cid or len(tel) < 10 or not viejo or tel in tels
                            or {"reactivar", "reactivacion_enviada"} & etiquetas
                            or _tiene_cita_por_nombre(o.get("name") or con.get("name") or "", textos)):
                        continue
                    t = await c.post(f"/contacts/{cid}/tags", json={"tags": ["reactivar"]})
                    if t.status_code < 300:
                        marcadas.append(tel[-4:])
                if len(ops) < 100:
                    break
                pagina += 1
    res = {"revisadas": revisadas, "marcadas": len(marcadas), "fecha": datetime.now(TZ).isoformat()[:16]}
    logger.info(f"[Reactivación] {res}")
    return res


async def loop(proveedor, intervalo_seg: int = 600) -> None:
    """Cada 10 min: manda pendientes en horario; una vez al día (10:00) marca reactivaciones."""
    ultimo_dia = ""
    await asyncio.sleep(60)
    while True:
        try:
            await enviar_pendientes(proveedor)
            hoy = datetime.now(TZ)
            if (10 <= hoy.hour < 18 and ultimo_dia != hoy.date().isoformat()
                    and os.getenv("REACTIVACION_ENABLED", "1") not in ("0", "false", "no")):
                ultimo_dia = hoy.date().isoformat()
                estado["reactivacion"] = await ciclo_reactivacion()
        except Exception as e:  # noqa: BLE001
            logger.error(f"[GHL→WhatsApp] loop: {e}")
        await asyncio.sleep(intervalo_seg)
