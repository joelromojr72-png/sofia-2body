"""
Anegocios (punto de venta de 2Body) → GHL, completo.

Cada noche la revisión de Anegocios manda TODO lo que hay en el punto de venta:
  dir     [[nombre, teléfono, correo], ...]                      directorio de clientas
  notas   {folio: [fecha, clienta, total, descuento, [[producto, unitario, importe], ...], vendio]}
  abonos  [[folio, "YYYY-MM-DD HH:MM", efectivo, tarjeta, transferencia], ...]

Y aquí se deja GHL al día, sin duplicar nada (se puede correr cuantas veces sea):
  1. Contactos: cada clienta del directorio existe en GHL (se busca por teléfono; si el
     teléfono es "de relleno" —compartido por varias clientas— se busca por nombre).
  2. Tarjetas: cada clienta con compras queda en "Paquete vendido" (o se respeta si ya está
     "En tratamiento") con valor = total comprado. Si el ticket dice "Vendió: Fátima", la
     tarjeta sin propietaria se le asigna a ella.
  3. Pagos: cada ticket es una factura "Ticket Anegocios #folio" con sus productos, y cada
     abono se registra como pago manual (efectivo / tarjeta / transferencia) con su fecha.
     Lo pendiente queda como saldo de la factura.

Nunca le manda nada a las clientas: las facturas se quedan sin enviar, sin correo, y los
recibos automáticos de GHL están apagados (Pagos > Configuración > Recibos).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import httpx

from agent import ghl

logger = logging.getLogger("agentkit")

TZ_MX = timezone(timedelta(hours=-6))
PREFIJO = "Ticket Anegocios #"
# Correos que no son de la clienta (la gerente los usaba de relleno al dar de alta).
CORREOS_EQUIPO = {"blancavg1977@gmail.com", "alesalg281@gmail.com", "fatimacarbajal.1023@gmail.com",
                  "donizsay@gmail.com"}

ultimo: dict = {}


def _clave(nombre: str) -> str:
    """Nombre normalizado para comparar (sin acentos, mayúsculas, espacios simples)."""
    return " ".join(ghl._norm(nombre).upper().split())


def _tokens(nombre: str) -> frozenset:
    return frozenset(_clave(nombre).split())


def _bonito(nombre: str) -> str:
    return " ".join(p.capitalize() for p in nombre.split())


def _tel10(t) -> str:
    d = "".join(ch for ch in str(t or "") if ch.isdigit())
    return d[-10:] if len(d) >= 10 else ""


def preparar(datos: dict) -> dict:
    """Agrupa el directorio y los tickets por clienta. Lógica pura (sin red), probada aparte."""
    directorio: dict[str, dict] = {}
    nombres_por_tel: dict[str, set] = defaultdict(set)
    correos: dict[str, set] = defaultdict(set)
    for fila in datos.get("dir") or []:
        try:
            nombre, tel, correo = str(fila[0]), _tel10(fila[1]), str(fila[2] or "").strip().lower()
        except (IndexError, TypeError):
            continue
        k = _clave(nombre)
        if not k:
            continue
        d = directorio.setdefault(k, {"nombre": _bonito(" ".join(nombre.split())), "tel": "", "correo": ""})
        if tel:
            d["tel"] = d["tel"] or tel
            nombres_por_tel[tel].add(k)
        if correo and "@" in correo:
            d["correo"] = d["correo"] or correo
            correos[correo].add(k)
    # Un teléfono que usan 3 o más clientas distintas es de relleno (p. ej. el de la gerente).
    # Con 2 suele ser la misma persona escrita distinto ("Neri"/"Nery"): ese sí sirve.
    relleno = {t for t, ks in nombres_por_tel.items() if len(ks) >= 3}
    for d in directorio.values():
        if d["tel"] in relleno:
            d["tel"] = ""
        if d["correo"] in CORREOS_EQUIPO or len(correos.get(d["correo"], ())) > 1:
            d["correo"] = ""

    abonos: dict[str, list] = defaultdict(list)
    for a in datos.get("abonos") or []:
        try:
            folio, cuando = str(a[0]), str(a[1])
            montos = {"cash": float(a[2] or 0), "card": float(a[3] or 0), "bank_transfer": float(a[4] or 0)}
        except (IndexError, TypeError, ValueError):
            continue
        abonos[folio].append((cuando, montos))

    clientas: dict[str, dict] = {k: {**d, "tickets": []} for k, d in directorio.items()}
    sin_clienta = []
    for folio, n in (datos.get("notas") or {}).items():
        try:
            fecha, cliente, total, desc, lineas = n[0], str(n[1] or ""), float(n[2] or 0), float(n[3] or 0), n[4]
            vendio = str(n[5] or "") if len(n) > 5 else ""
        except (IndexError, TypeError, ValueError):
            continue
        k = _clave(cliente)
        t = {"folio": str(folio), "fecha": fecha or "", "total": total, "desc": desc, "lineas": lineas or [],
             "vendio": vendio, "abonos": sorted(abonos.get(str(folio), []))}
        if not k:
            sin_clienta.append(t)
            continue
        c = clientas.setdefault(k, {"nombre": _bonito(" ".join(cliente.split())), "tel": "", "correo": "",
                                    "tickets": []})
        c["tickets"].append(t)
    return {"clientas": clientas, "sin_clienta": sin_clienta, "telefonos_relleno": sorted(relleno)}


def items_factura(t: dict) -> tuple[list[dict], float]:
    """Partidas de la factura y el descuento, cuadrando con el total del ticket."""
    items = []
    for ln in t["lineas"]:
        try:
            nombre, unit, importe = str(ln[0])[:200], float(ln[1]), float(ln[2])
        except (IndexError, TypeError, ValueError):
            continue
        qty = max(1, round(importe / unit)) if unit else 1
        items.append({"name": nombre or "Servicio", "amount": round(importe / qty, 2), "qty": qty,
                      "currency": "MXN", "type": "one_time"})
    if not items:
        items = [{"name": "Venta Anegocios", "amount": round(t["total"], 2), "qty": 1, "currency": "MXN",
                  "type": "one_time"}]
    suma = sum(i["amount"] * i["qty"] for i in items)
    descuento = round(max(0.0, suma - t["total"]), 2)
    if t["total"] > suma:  # cargos extra (comisión, propina): se agregan como partida
        items.append({"name": "Cargos adicionales", "amount": round(t["total"] - suma, 2), "qty": 1,
                      "currency": "MXN", "type": "one_time"})
        descuento = 0.0
    return items, descuento


def pagos_pendientes(t: dict, ya_pagado: float) -> list[dict]:
    """Abonos que faltan registrar: se saltan, en orden, los que ya suman lo pagado en GHL.
    Nunca se registra más que el total del ticket (GHL no acepta sobrepagos)."""
    pagos, acumulado, restante = [], 0.0, max(0.0, t["total"] - ya_pagado)
    for cuando, montos in t["abonos"]:
        for modo in ("cash", "card", "bank_transfer"):
            m = round(montos.get(modo, 0), 2)
            if m <= 0:
                continue
            if acumulado + m <= ya_pagado + 0.5:
                acumulado += m
                continue
            m = min(m, round(restante, 2))
            if m <= 0.5:
                return pagos
            pagos.append({"modo": modo, "monto": m, "cuando": cuando})
            restante -= m
            acumulado += m
    return pagos


def _iso(cuando: str) -> str:
    """'2026-11-13 13:36' (hora de CDMX) → ISO en UTC. Sin hora: mediodía."""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            d = datetime.strptime(cuando.strip()[:16], fmt)
            if fmt == "%Y-%m-%d":
                d = d.replace(hour=12)
            return d.replace(tzinfo=TZ_MX).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        except ValueError:
            continue
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ── GHL ──────────────────────────────────────────────────────────────────────
async def _usuarios(c: httpx.AsyncClient) -> dict[str, str]:
    r = await c.get("/users/", params={"locationId": ghl._loc()})
    if r.status_code != 200:
        return {}
    out = {}
    for u in r.json().get("users", []):
        if u.get("deleted"):
            continue
        out[_clave(u.get("firstName") or u.get("name") or "").split(" ")[0]] = u["id"]
    if "ALEJANDRA" in out:
        out.setdefault("ALE", out["ALEJANDRA"])
    return out


async def _facturas(c: httpx.AsyncClient) -> dict[str, dict]:
    """Facturas que ya existen, por folio de Anegocios."""
    out, offset = {}, 0
    while True:
        r = await c.get("/invoices/", params={"altId": ghl._loc(), "altType": "location", "limit": "100",
                                              "offset": str(offset)})
        r.raise_for_status()
        lote = r.json().get("invoices", [])
        for f in lote:
            nombre = f.get("name") or ""
            if nombre.startswith(PREFIJO) and f.get("status") != "void":
                out[nombre[len(PREFIJO):].strip()] = f
        if len(lote) < 100:
            return out
        offset += 100


async def _por_nombre(c: httpx.AsyncClient, nombre: str) -> dict | None:
    """Contacto cuyo nombre tiene exactamente las mismas palabras (en cualquier orden)."""
    buscado = _tokens(nombre)
    palabras = sorted(buscado, key=len, reverse=True)[:2]
    vistos = {}
    for q in palabras:
        r = await c.get("/contacts/", params={"locationId": ghl._loc(), "query": q, "limit": 50})
        if r.status_code != 200:
            continue
        for x in r.json().get("contacts", []):
            if _tokens(x.get("contactName") or "") == buscado:
                vistos[x["id"]] = x
    return next(iter(vistos.values())) if len(vistos) == 1 else None


async def _asegurar_contacto(c: httpx.AsyncClient, cl: dict, cuenta: Counter) -> dict | None:
    contacto = await ghl._buscar_contacto_tel(c, cl["tel"]) if cl["tel"] else None
    if not contacto:
        contacto = await _por_nombre(c, cl["nombre"])
    if not contacto:
        partes = cl["nombre"].split()
        # GHL no acepta contactos solo con "name": hay que mandar nombre y apellido.
        # Anegocios guarda "APELLIDOS NOMBRE(S)": con 3+ palabras, las dos primeras son apellidos.
        corte = 2 if len(partes) >= 3 else 1
        cuerpo = {"locationId": ghl._loc(), "firstName": " ".join(partes[corte:]) or cl["nombre"],
                  "lastName": " ".join(partes[:corte]) if len(partes) > 1 else "",
                  "name": cl["nombre"], "source": "Anegocios", "tags": ["anegocios"]}
        if cl["tel"]:
            cuerpo["phone"] = "+521" + cl["tel"]
        if cl["correo"]:
            cuerpo["email"] = cl["correo"]
        r = await c.post("/contacts/upsert" if cl["tel"] else "/contacts/", json=cuerpo)
        if r.status_code >= 400 and cl["correo"]:  # correo repetido en otro contacto: sin correo
            cuerpo.pop("email")
            r = await c.post("/contacts/upsert" if cl["tel"] else "/contacts/", json=cuerpo)
        r.raise_for_status()
        cuenta["contactos_nuevos"] += 1
        return r.json()["contact"]
    etiquetas = ["anegocios"] + (["vendido"] if cl["tickets"] else [])
    if not set(etiquetas) <= set(contacto.get("tags") or []):
        await c.post(f"/contacts/{contacto['id']}/tags", json={"tags": etiquetas})
    if cl["correo"] and not contacto.get("email"):
        r = await c.put(f"/contacts/{contacto['id']}", json={"email": cl["correo"]})
        if r.status_code < 400:
            cuenta["correos_agregados"] += 1
    return contacto


async def _asegurar_tarjeta(c: httpx.AsyncClient, pipe: dict, contacto: dict, cl: dict, dueno: str,
                            cuenta: Counter) -> None:
    total = round(sum(t["total"] for t in cl["tickets"]), 2)
    vend_id, vend_pos = pipe["etapas"]["vendido"]
    respetar = {vend_id} | ({pipe["etapas"]["tratamiento"][0]} if "tratamiento" in pipe["etapas"] else set())
    r = await c.get("/opportunities/search", params={"location_id": ghl._loc(), "pipeline_id": pipe["id"],
                                                     "contact_id": contacto["id"], "limit": 20})
    r.raise_for_status()
    opp = next((o for o in r.json().get("opportunities", []) if o.get("status", "open") == "open"), None)
    if not opp:
        cuerpo = {"pipelineId": pipe["id"], "locationId": ghl._loc(), "name": cl["nombre"],
                  "pipelineStageId": vend_id, "status": "open", "contactId": contacto["id"],
                  "source": "Anegocios", "monetaryValue": total}
        if dueno:
            cuerpo["assignedTo"] = dueno
        (await c.post("/opportunities/", json=cuerpo)).raise_for_status()
        cuenta["tarjetas_nuevas"] += 1
        return
    cambios = {}
    if abs(float(opp.get("monetaryValue") or 0) - total) > 0.5:
        cambios["monetaryValue"] = total
    if opp.get("pipelineStageId") not in respetar:  # incluye "Reactivación": si compró, es venta
        cambios["pipelineStageId"] = vend_id
        cuenta["movidas_a_vendido"] += 1
    if dueno and not opp.get("assignedTo"):
        cambios["assignedTo"] = dueno
        cuenta["propietarias_asignadas"] += 1
    if cambios:
        (await c.put(f"/opportunities/{opp['id']}", json=cambios)).raise_for_status()
        cuenta["tarjetas_actualizadas"] += 1


async def _asegurar_factura(c: httpx.AsyncClient, t: dict, contacto: dict, existente: dict | None,
                            cuenta: Counter, cl_nombre: str = "") -> None:
    loc = {"altId": ghl._loc(), "altType": "location"}
    if existente is None:
        items, descuento = items_factura(t)
        hoy = datetime.now(TZ_MX).strftime("%Y-%m-%d")
        fecha = (t["fecha"] or hoy)[:10]
        cuerpo = {**loc, "name": f"{PREFIJO}{t['folio']}", "title": f"Ticket #{t['folio']}", "currency": "MXN",
                  "businessDetails": {"name": "2Body Pachuca"},
                  # Sin correo a propósito: la factura es solo registro, nunca se envía.
                  "contactDetails": {"id": contacto["id"], "name": contacto.get("contactName") or contacto.get("name") or cl_nombre,
                                     **({"phoneNo": contacto["phone"]} if contacto.get("phone") else {})},
                  "items": items, "discount": {"type": "fixed", "value": descuento},
                  "issueDate": fecha, "dueDate": max(fecha, hoy), "liveMode": True,
                  "termsNotes": f"Venta registrada en Anegocios (ticket {t['folio']})."
                                + (f" Vendió: {t['vendio']}." if t["vendio"] else "")}
        r = await c.post("/invoices/", json=cuerpo)
        if r.status_code >= 400:
            # Se guarda el motivo que da GHL (antes solo se veía "422 Unprocessable Entity").
            raise RuntimeError(f"GHL rechazó la factura del ticket {t['folio']} [{r.status_code}]: {r.text[:300]} "
                               f"· items={items} descuento={descuento}")
        existente = r.json()
        cuenta["facturas_nuevas"] += 1
    ya = float(existente.get("amountPaid") or 0)
    for p in pagos_pendientes(t, ya):
        modo_txt = {"cash": "efectivo", "card": "tarjeta", "bank_transfer": "transferencia"}[p["modo"]]
        for intento in range(4):  # GHL responde 409 si llegan dos pagos seguidos a la misma factura
            r = await c.post(f"/invoices/{existente['_id']}/record-payment", json={
                **loc, "mode": p["modo"], "amount": p["monto"], "fulfilledAt": _iso(p["cuando"]),
                "notes": f"Anegocios ticket {t['folio']} · {modo_txt} · {p['cuando'][:10]}"})
            if r.status_code != 409:
                break
            await asyncio.sleep(4 * (intento + 1))
        r.raise_for_status()
        cuenta["pagos_registrados"] += 1
        cuenta["monto_registrado"] += p["monto"]
        await asyncio.sleep(1.5)


async def sincronizar(datos: dict, pagos: bool = True) -> dict:
    prep = preparar(datos)
    cuenta: Counter = Counter()
    fallas: list[str] = []
    async with ghl._cliente() as c:
        pipe = await ghl._cargar_pipeline(c)
        if "vendido" not in pipe["etapas"]:
            return {"error": "no existe la etapa Paquete vendido"}
        usuarios = await _usuarios(c)
        facturas = await _facturas(c) if pagos else {}
        for k, cl in prep["clientas"].items():
            try:
                contacto = await _asegurar_contacto(c, cl, cuenta)
                if not contacto:
                    continue
                cuenta["clientas"] += 1
                if not cl["tickets"]:
                    continue
                vendio = next((t["vendio"] for t in sorted(cl["tickets"], key=lambda t: t["fecha"], reverse=True)
                               if t["vendio"]), "")
                dueno = usuarios.get(_clave(vendio).split(" ")[0], "") if vendio else ""
                await _asegurar_tarjeta(c, pipe, contacto, cl, dueno, cuenta)
                if pagos:
                    for t in cl["tickets"]:
                        await _asegurar_factura(c, t, contacto, facturas.get(t["folio"]), cuenta, cl["nombre"])
            except Exception as e:  # noqa: BLE001 — una clienta con error no detiene a las demás
                cuenta["errores"] += 1
                fallas.append(f"{cl['nombre']}: {str(e)[:160]}")
                logger.warning(f"[Anegocios→GHL] {cl['nombre']}: {e}")
                await asyncio.sleep(3)
            await asyncio.sleep(0.3)
    cuenta["monto_registrado"] = round(cuenta["monto_registrado"], 2)
    return {**dict(cuenta), "tickets_sin_clienta": [t["folio"] for t in prep["sin_clienta"]],
            "telefonos_relleno": len(prep["telefonos_relleno"]), "fallas": fallas[:20]}
