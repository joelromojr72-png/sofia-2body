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


def _cliente() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=API, headers=_headers(), timeout=20)


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
        for clave, prefijo in ETAPAS.items():
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
            if not opp:
                r = await c.post("/opportunities/", json={
                    "pipelineId": pipe["id"], "locationId": _loc(), "name": titulo,
                    "pipelineStageId": etapa_id, "status": "open", "contactId": contacto_id,
                    "source": "WhatsApp (Sofía)",
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


async def importar_historial(clientas: list[dict], pausa: float = 0.4) -> None:
    """Sube a GHL cada clienta del historial a su etapa (sin regresar las que ya estén más adelante)."""
    from collections import Counter
    cuenta: Counter = Counter()
    importacion.update(corriendo=True, total=len(clientas), hechas=0, errores=0, por_etapa={})
    try:
        for c in clientas:
            etapa = etapa_de(c)
            if etapa:
                try:
                    await sincronizar(c["telefono"], etapa, c.get("nombre", ""))
                    cuenta[etapa] += 1
                except Exception as e:  # noqa: BLE001
                    importacion["errores"] += 1
                    logger.warning(f"[GHL] importación: falló {c['telefono']}: {e}")
                    await asyncio.sleep(3)  # por si es límite de GHL
                await asyncio.sleep(pausa)
            else:
                cuenta["omitidas_opt_out"] += 1
            importacion["hechas"] += 1
            importacion["por_etapa"] = dict(cuenta)
    finally:
        importacion["corriendo"] = False
        logger.info(f"[GHL] importación terminada: {importacion}")
