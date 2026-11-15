"""
Espejo de las clientas de Sofía en GoHighLevel (CRM de 2Body Pachuca).

Sofía sigue siendo la que atiende y agenda; GHL solo refleja en el pipeline "Ventas 2Body":
  • nuevo       → "Lead nuevo"              (escribe por primera vez)
  • contactada  → "Contactada por WhatsApp" (Sofía le contestó)
  • agendada    → "Valoracion agendada"     (Sofía le agendó cita)
Las demás etapas (valoración realizada, paquete vendido...) las mueve el equipo a mano.
Nunca se regresa una tarjeta a una etapa anterior: si el equipo ya la avanzó, se respeta.

Todo corre en segundo plano y cualquier error de GHL se registra y se ignora: si GHL falla,
Sofía sigue contestando igual. Se activa con GHL_TOKEN (Private Integration) y GHL_LOCATION_ID.
"""

from __future__ import annotations

import asyncio
import logging
import os
import unicodedata
from collections import defaultdict

import httpx

logger = logging.getLogger("agentkit")

API = "https://services.leadconnectorhq.com"
PIPELINE_NOMBRE = os.getenv("GHL_PIPELINE", "Ventas 2Body")

# etapa interna → inicio del nombre de la etapa en GHL (sin acentos, minúsculas)
ETAPAS = {
    "nuevo": "lead nuevo",
    "contactada": "contactada",
    "agendada": "valoracion agendada",
}
# Etapas que NO mueve Sofía pero sí lee (ventas para Meta).
ETAPAS_LECTURA = {"vendido": "paquete vendido", "tratamiento": "en tratamiento"}

_pipeline: dict | None = None            # {"id": ..., "etapas": {clave: (id, posicion)}, "pos": {id: posicion}}
_opps: dict[str, tuple[str, int]] = {}   # telefono → (id de la oportunidad, posición de su etapa)
_candados: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
_tareas: set[asyncio.Task] = set()


def activo() -> bool:
    return bool(os.getenv("GHL_TOKEN") and os.getenv("GHL_LOCATION_ID"))


def _loc() -> str:
    return os.getenv("GHL_LOCATION_ID", "")


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {os.getenv('GHL_TOKEN', '')}",
        "Version": "2021-07-28",
        "Accept": "application/json",
    }


def _norm(t: str) -> str:
    t = unicodedata.normalize("NFD", t or "")
    return "".join(c for c in t if unicodedata.category(c) != "Mn").lower().strip()


class _Reintenta429(httpx.AsyncHTTPTransport):
    """GHL limita peticiones por minuto (429): esperar y reintentar, hasta 5 veces."""

    async def handle_async_request(self, request):
        for intento in range(5):
            resp = await super().handle_async_request(request)
            if resp.status_code != 429:
                return resp
            await resp.aclose()
            espera = float(resp.headers.get("retry-after") or 0) or 3 * (intento + 1)
            await asyncio.sleep(min(espera, 30))
        return await super().handle_async_request(request)


def _cliente() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=API, headers=_headers(), timeout=30, transport=_Reintenta429())


async def _cargar_pipeline(c: httpx.AsyncClient) -> dict:
    global _pipeline
    if _pipeline:
        return _pipeline
    r = await c.get("/opportunities/pipelines", params={"locationId": _loc()})
    r.raise_for_status()
    for p in r.json().get("pipelines", []):
        if _norm(p.get("name", "")) != _norm(PIPELINE_NOMBRE):
            continue
        pos = {s["id"]: int(s.get("position", 0)) for s in p.get("stages", [])}
        etapas = {}
        for clave, prefijo in {**ETAPAS, **ETAPAS_LECTURA}.items():
            s = next((s for s in p.get("stages", []) if _norm(s.get("name", "")).startswith(prefijo)), None)
            if s:
                etapas[clave] = (s["id"], int(s.get("position", 0)))
        _pipeline = {"id": p["id"], "etapas": etapas, "pos": pos}
        return _pipeline
    raise RuntimeError(f"No existe el pipeline '{PIPELINE_NOMBRE}' en GHL")


async def _contacto(c: httpx.AsyncClient, telefono: str, nombre: str) -> str:
    cuerpo = {
        "locationId": _loc(),
        "phone": "+" + telefono,
        "source": "WhatsApp (Sofía)",
        "tags": ["sofia", "whatsapp"],
    }
    if nombre:
        cuerpo["name"] = nombre
    r = await c.post("/contacts/upsert", json=cuerpo)
    r.raise_for_status()
    return r.json()["contact"]["id"]


async def _anuncio(telefono: str) -> str:
    """Título del anuncio por el que llegó la clienta ("" si no llegó por anuncio)."""
    try:
        from agent.memory import titulo_anuncio
        return (await titulo_anuncio(telefono) or "").strip()[:80]
    except Exception:  # noqa: BLE001
        return ""


async def _etiquetar_anuncio(c: httpx.AsyncClient, contacto_id: str, anuncio: str) -> None:
    try:
        await c.post(f"/contacts/{contacto_id}/tags", json={"tags": ["anuncio", f"anuncio: {anuncio[:60]}"]})
    except httpx.HTTPError:
        pass


async def _buscar_opp(c: httpx.AsyncClient, pipe: dict, contacto_id: str) -> tuple[str, int] | None:
    r = await c.get("/opportunities/search", params={
        "location_id": _loc(), "pipeline_id": pipe["id"], "contact_id": contacto_id, "limit": 20,
    })
    r.raise_for_status()
    for o in r.json().get("opportunities", []):
        if o.get("status", "open") == "open":
            return o["id"], pipe["pos"].get(o.get("pipelineStageId"), 0)
    return None


async def sincronizar(telefono: str, etapa: str, nombre: str = "", nota: str = "") -> None:
    """Crea o avanza la tarjeta de la clienta en el pipeline. Nunca la regresa."""
    if not activo() or not telefono or not telefono.isdigit() or etapa not in ETAPAS:
        return
    async with _candados[telefono]:
        async with _cliente() as c:
            pipe = await _cargar_pipeline(c)
            if etapa not in pipe["etapas"]:
                return
            etapa_id, etapa_pos = pipe["etapas"][etapa]
            conocida = _opps.get(telefono)
            if conocida and conocida[1] >= etapa_pos and not nombre:
                return  # ya está en esa etapa o más adelante: nada que hacer (sin llamar a GHL)
            contacto_id = await _contacto(c, telefono, nombre)
            # Si ya la conocemos no se busca (la búsqueda de GHL tarda en ver tarjetas recién creadas).
            opp = conocida or await _buscar_opp(c, pipe, contacto_id)
            titulo = nombre or f"WhatsApp +{telefono}"
            anuncio = "" if conocida else await _anuncio(telefono)
            fuente = f"Anuncio: {anuncio}" if anuncio else "WhatsApp (Sofía)"
            if anuncio:
                await _etiquetar_anuncio(c, contacto_id, anuncio)
            if not opp:
                r = await c.post("/opportunities/", json={
                    "pipelineId": pipe["id"], "locationId": _loc(), "name": titulo,
                    "pipelineStageId": etapa_id, "status": "open", "contactId": contacto_id,
                    "source": fuente,
                })
                r.raise_for_status()
                _opps[telefono] = (r.json()["opportunity"]["id"], etapa_pos)
                logger.info(f"[GHL] nueva tarjeta '{etapa}' para {telefono}")
            else:
                opp_id, pos = opp
                cambios = {}
                if etapa_pos > pos:
                    cambios["pipelineStageId"] = etapa_id
                if nombre:
                    cambios["name"] = nombre
                if anuncio:
                    cambios["source"] = fuente
                if cambios:
                    r = await c.put(f"/opportunities/{opp_id}", json=cambios)
                    r.raise_for_status()
                    logger.info(f"[GHL] tarjeta de {telefono} → {etapa if etapa_pos > pos else 'mismo lugar'}")
                _opps[telefono] = (opp_id, max(pos, etapa_pos))
            if nota:
                try:
                    await c.post(f"/contacts/{contacto_id}/notes", json={"body": nota})
                except httpx.HTTPError:
                    pass


async def _seguro(telefono: str, etapa: str, nombre: str, nota: str) -> None:
    try:
        await sincronizar(telefono, etapa, nombre, nota)
    except Exception as e:  # noqa: BLE001 — GHL nunca debe tumbar a Sofía
        logger.warning(f"[GHL] no se pudo sincronizar {telefono} ({etapa}): {e}")


def programar(telefono: str, etapa: str, nombre: str = "", nota: str = "") -> None:
    """Lanza la sincronización en segundo plano (no bloquea la respuesta de Sofía)."""
    if not activo():
        return
    try:
        t = asyncio.get_running_loop().create_task(_seguro(telefono, etapa, nombre, nota))
    except RuntimeError:
        return
    _tareas.add(t)
    t.add_done_callback(_tareas.discard)


# ── Carga del historial (una vez) ────────────────────────────────────────────
importacion: dict = {"corriendo": False}


def etapa_de(c: dict) -> str | None:
    """Etapa en GHL de una clienta del historial. Las que pidieron no ser contactadas no se cargan."""
    if c.get("estado") == "opt_out":
        return None
    if c.get("estado") == "agendado":
        return "agendada"
    return "contactada" if c.get("contestada") else "nuevo"


async def importar_historial(clientas: list[dict], pausa: float = 0.8) -> None:
    """Sube a GHL cada clienta del historial a su etapa (sin regresar las que ya estén más adelante)."""
    from collections import Counter
    cuenta: Counter = Counter()
    importacion.update(corriendo=True, total=len(clientas), hechas=0, errores=0, por_etapa={})
    try:
        for c in clientas:
            etapa = etapa_de(c)
            if etapa:
                for intento in range(3):
                    try:
                        await sincronizar(c["telefono"], etapa, c.get("nombre", ""))
                        cuenta[etapa] += 1
                        break
                    except Exception as e:  # noqa: BLE001 — 429 = límite de GHL: esperar y reintentar
                        if intento == 2:
                            importacion["errores"] += 1
                            logger.warning(f"[GHL] importación: falló {c['telefono']}: {e}")
                        await asyncio.sleep(10 * (intento + 1))
                await asyncio.sleep(pausa)
            else:
                cuenta["omitidas_opt_out"] += 1
            importacion["hechas"] += 1
            importacion["por_etapa"] = dict(cuenta)
    finally:
        importacion["corriendo"] = False
        logger.info(f"[GHL] importación terminada: {importacion}")


# ── Ventas de GHL → Meta ─────────────────────────────────────────────────────
# Cuando el equipo mueve una tarjeta a "Paquete vendido" (o "En tratamiento"), y la clienta
# llegó por un anuncio de clic a WhatsApp (tenemos su ctwa_clid), se le reporta a Meta la
# Compra con el valor de la tarjeta. Mismo event_id que la atribución por agenda
# ("venta-<tel10>"), así Meta no la cuenta doble. Se revisa cada pocos minutos (sin webhook).
VALOR_DEFECTO = int(os.getenv("VALOR_VENTA_DEFECTO") or "5000")
GHL_VENTAS_CADA_MIN = float(os.getenv("GHL_VENTAS_CADA_MIN") or "15")


async def _vendidas(c: httpx.AsyncClient, pipe: dict) -> list[dict]:
    out = []
    for clave in ("vendido", "tratamiento"):
        if clave not in pipe["etapas"]:
            continue
        pagina = 1
        while pagina <= 20:
            r = await c.get("/opportunities/search", params={
                "location_id": _loc(), "pipeline_id": pipe["id"],
                "pipeline_stage_id": pipe["etapas"][clave][0], "limit": 100, "page": pagina,
            })
            r.raise_for_status()
            ops = r.json().get("opportunities", [])
            for o in ops:
                tel = "".join(ch for ch in ((o.get("contact") or {}).get("phone") or "") if ch.isdigit())
                if tel:
                    out.append({"telefono": tel, "nombre": o.get("name", ""),
                                "valor": int(float(o.get("monetaryValue") or 0)) or VALOR_DEFECTO})
            if len(ops) < 100:
                break
            pagina += 1
    return out


async def ciclo_ventas_ghl() -> dict:
    from sqlalchemy import select

    from agent import atribucion
    from agent.memory import OrigenAnuncio, async_session

    async with _cliente() as c:
        pipe = await _cargar_pipeline(c)
        vendidas = await _vendidas(c, pipe)
    reportadas = sin_anuncio = 0
    async with async_session() as s:
        for v in vendidas:
            t10 = v["telefono"][-10:]
            origen = (await s.execute(
                select(OrigenAnuncio).where(OrigenAnuncio.telefono.like(f"%{t10}"), OrigenAnuncio.ctwa_clid != "")
                .order_by(OrigenAnuncio.creado_en.desc()).limit(1)
            )).scalars().first()
            if not origen:
                sin_anuncio += 1
                continue
            fila = await s.get(atribucion.VentaAnuncio, origen.telefono)
            if fila and fila.reportada_meta:
                continue
            if fila is None:
                fila = atribucion.VentaAnuncio(telefono=origen.telefono)
                s.add(fila)
            fila.nombre, fila.servicio, fila.valor = v["nombre"][:160], "Paquete (GHL)", v["valor"]
            fila.ad_id, fila.ad_titulo, fila.ctwa_clid = origen.ad_id, origen.titulo, origen.ctwa_clid
            fila.llego_en = origen.creado_en
            ok, detalle = await atribucion.reportar_compra_meta(fila)
            fila.respuesta_meta = detalle
            if ok:
                fila.reportada_meta = True
                reportadas += 1
                logger.info(f"[GHL→Meta] Compra reportada: {origen.telefono} ${v['valor']} (anuncio {origen.ad_id})")
            else:
                logger.warning(f"[GHL→Meta] Meta no aceptó la compra de {origen.telefono}: {detalle}")
        await s.commit()
    return {"vendidas_en_ghl": len(vendidas), "reportadas_meta": reportadas, "sin_anuncio": sin_anuncio}


async def loop_ventas_ghl() -> None:
    if not activo():
        return
    logger.info(f"[GHL→Meta] ventas del pipeline cada {GHL_VENTAS_CADA_MIN:g} min")
    while True:
        try:
            r = await ciclo_ventas_ghl()
            if r["reportadas_meta"]:
                logger.info(f"[GHL→Meta] {r}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[GHL→Meta] ciclo falló: {e}")
        await asyncio.sleep(GHL_VENTAS_CADA_MIN * 60)


ultimo_anegocios: dict = {}

# ── Ventas de Anegocios → GHL ────────────────────────────────────────────────
# Cada noche, la revisión de Anegocios manda [nombre, teléfono, total] de cada clienta con
# venta. Aquí se busca su contacto (en GHL los teléfonos son +521XXXXXXXXXX), se mueve su
# tarjeta a "Paquete vendido" (nunca hacia atrás) con valor = total comprado, y se etiqueta.
# Se omiten teléfonos que comparten varias clientas distintas (números de relleno).
async def _buscar_contacto_tel(c: httpx.AsyncClient, tel10: str) -> dict | None:
    for num in ("+521" + tel10, "+52" + tel10):
        r = await c.get("/contacts/search/duplicate", params={"locationId": _loc(), "number": num})
        if r.status_code == 200 and (r.json() or {}).get("contact"):
            return r.json()["contact"]
    return None


async def ligar_ventas_anegocios(filas: list) -> dict:
    from collections import Counter, OrderedDict, defaultdict

    nombres_tel: dict[str, set] = defaultdict(set)
    limpias = []
    for f in filas:
        try:
            nombre, tel, total = str(f[0]), "".join(ch for ch in str(f[1]) if ch.isdigit())[-10:], float(f[2])
        except (IndexError, TypeError, ValueError):
            continue
        if len(tel) == 10 and nombre.strip():
            limpias.append((nombre, tel, total))
            nombres_tel[tel].add(" ".join(nombre.upper().split()))
    agg: OrderedDict = OrderedDict()
    omitidas = []
    for nombre, tel, total in limpias:
        if len(nombres_tel[tel]) > 1:
            omitidas.append(nombre)
            continue
        a = agg.setdefault(tel, {"nombre": " ".join(nombre.split()).title(), "total": 0.0, "notas": 0})
        a["total"] += total
        a["notas"] += 1
    cuenta: Counter = Counter()
    async with _cliente() as c:
        pipe = await _cargar_pipeline(c)
        if "vendido" not in pipe["etapas"]:
            return {"error": "no existe la etapa Paquete vendido"}
        vend_id, vend_pos = pipe["etapas"]["vendido"]
        for tel, a in agg.items():
            try:
                total = round(a["total"], 2)
                contacto = await _buscar_contacto_tel(c, tel)
                if not contacto:
                    r = await c.post("/contacts/upsert", json={
                        "locationId": _loc(), "phone": "+521" + tel, "name": a["nombre"],
                        "source": "Anegocios", "tags": ["anegocios", "vendido"]})
                    r.raise_for_status()
                    contacto = r.json()["contact"]
                    cuenta["contactos_nuevos"] += 1
                else:
                    await c.post(f"/contacts/{contacto['id']}/tags", json={"tags": ["anegocios", "vendido"]})
                opp = await _buscar_opp(c, pipe, contacto["id"])
                if opp:
                    opp_id, pos = opp
                    cuerpo = {"monetaryValue": total}
                    if pos < vend_pos:
                        cuerpo["pipelineStageId"] = vend_id
                        cuenta["movidas_a_vendido"] += 1
                    r = await c.put(f"/opportunities/{opp_id}", json=cuerpo)
                    r.raise_for_status()
                    _opps.pop(tel, None)
                else:
                    r = await c.post("/opportunities/", json={
                        "pipelineId": pipe["id"], "locationId": _loc(), "name": a["nombre"],
                        "pipelineStageId": vend_id, "status": "open", "contactId": contacto["id"],
                        "source": "Anegocios", "monetaryValue": total})
                    r.raise_for_status()
                    cuenta["tarjetas_nuevas"] += 1
                cuenta["ok"] += 1
            except Exception as e:  # noqa: BLE001
                cuenta["errores"] += 1
                logger.warning(f"[Anegocios→GHL] {tel[-4:]}: {e}")
                await asyncio.sleep(5)
            await asyncio.sleep(0.4)
    return {"clientas": len(agg), "omitidas_tel_compartido": len(omitidas), **dict(cuenta)}
