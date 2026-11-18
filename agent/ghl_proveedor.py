"""
Contestar WhatsApp DESDE GoHighLevel (proveedor de conversación propio).

Cómo funciona:
  • En el portal de desarrolladores de GHL hay una app privada "Sofía WhatsApp" con un
    Conversation Provider tipo SMS marcado como "Custom" (canal extra, no reemplaza el SMS).
    Su Delivery URL apunta a POST /conexion/entrega (= /ghl/proveedor; GHL no acepta 'ghl' en URLs) de este servidor.
  • La app se instala en la sub-cuenta 2Body Pachuca (OAuth): GHL redirige a GET /ghl/oauth con
    un código; se cambia por tokens y se guardan en la base (tabla ghl_token). El token se
    renueva solo antes de vencer.
  • El espejo (agent/ghl_espejo.py) copia los chats como mensajes de ESE canal, así que en
    GHL el cuadro de respuesta ya sale con "WhatsApp 2Body".
  • Cuando alguien del equipo escribe ahí, GHL manda el mensaje a /ghl/proveedor (firmado con
    Ed25519). Se verifica la firma, se manda por WhatsApp (Meta Cloud API, el mismo número de
    Sofía), se guarda en el historial como mensaje del EQUIPO (Sofía se calla en ese chat, igual
    que desde la Bandeja) y se le avisa a GHL si se entregó o falló (p. ej. ventana de 24 h).

Variables: GHL_APP_CLIENT_ID, GHL_APP_CLIENT_SECRET, GHL_PROVIDER_ID (id del provider),
GHL_APP_REDIRECT (opcional; por defecto https://<dominio público>/ghl/oauth).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from agent.memory import Base, ahora, async_session

logger = logging.getLogger("agentkit")

API = "https://services.leadconnectorhq.com"

# Llave pública Ed25519 de GHL (docs: Conversation Provider - Outbound Message).
GHL_PUBLIC_KEY_PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MCowBQYDK2VwAyEAi2HR1srL4o18O8BRa7gVJY7G7bupbN3H9AwJrHCDiOg=\n"
    "-----END PUBLIC KEY-----\n"
)

estado: dict = {"recibidos": 0, "enviados": 0, "fallidos": 0, "ultimo_error": ""}
_candado = asyncio.Lock()
_nombres: dict[str, str] = {}  # userId de GHL → nombre


class GhlToken(Base):
    """Tokens OAuth de la app proveedora instalada en la sub-cuenta (una sola fila, id=1)."""

    __tablename__ = "ghl_token"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    location_id: Mapped[str] = mapped_column(String(60), default="")
    access_token: Mapped[str] = mapped_column(Text, default="")
    refresh_token: Mapped[str] = mapped_column(Text, default="")
    expira: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


def configurado() -> bool:
    return bool(os.getenv("GHL_APP_CLIENT_ID") and os.getenv("GHL_APP_CLIENT_SECRET") and os.getenv("GHL_PROVIDER_ID"))


def provider_id() -> str:
    return os.getenv("GHL_PROVIDER_ID", "").strip()


def redirect_uri() -> str:
    r = os.getenv("GHL_APP_REDIRECT", "").strip()
    if r:
        return r
    dominio = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    return f"https://{dominio}/conexion/oauth" if dominio else ""


# ── Tokens ─────────────────────────────────────────────────────────────────────

async def _pedir_token(datos: dict) -> dict:
    base = {
        "client_id": os.getenv("GHL_APP_CLIENT_ID", ""),
        "client_secret": os.getenv("GHL_APP_CLIENT_SECRET", ""),
        "user_type": "Location",
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{API}/oauth/token", data={**base, **datos},
                         headers={"Accept": "application/json"})
    if r.status_code >= 400:
        raise RuntimeError(f"GHL oauth {r.status_code}: {r.text[:300]}")
    return r.json()


async def _guardar(t: dict) -> None:
    async with async_session() as s:
        fila = await s.get(GhlToken, 1)
        if fila is None:
            fila = GhlToken(id=1)
            s.add(fila)
        fila.access_token = t["access_token"]
        fila.refresh_token = t.get("refresh_token") or fila.refresh_token
        fila.location_id = t.get("locationId") or fila.location_id or ""
        fila.expira = ahora() + timedelta(seconds=int(t.get("expires_in") or 86399))
        await s.commit()


async def instalar(code: str) -> dict:
    """Cambia el código de instalación por tokens (GET /ghl/oauth)."""
    t = await _pedir_token({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri()})
    await _guardar(t)
    logger.info(f"[GHL proveedor] app instalada en la sub-cuenta {t.get('locationId', '?')}")
    return {"ok": True, "locationId": t.get("locationId")}


async def token() -> str | None:
    """Access token vigente de la app (renueva si le quedan <10 min). None si no está instalada."""
    if not configurado():
        return None
    async with _candado:
        async with async_session() as s:
            fila = await s.get(GhlToken, 1)
        if fila is None or not fila.access_token:
            return None
        exp = fila.expira if fila.expira.tzinfo else fila.expira.replace(tzinfo=timezone.utc)
        if exp - ahora() > timedelta(minutes=10):
            return fila.access_token
        try:
            t = await _pedir_token({"grant_type": "refresh_token", "refresh_token": fila.refresh_token})
            await _guardar(t)
            return t["access_token"]
        except Exception as e:  # noqa: BLE001
            estado["ultimo_error"] = f"renovar token: {e}"
            logger.error(f"[GHL proveedor] no se pudo renovar el token: {e}")
            return None


def headers(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}", "Version": "2021-04-15", "Accept": "application/json"}


async def info() -> dict:
    async with async_session() as s:
        fila = await s.get(GhlToken, 1)
    return {
        "configurado": configurado(),
        "instalado": bool(fila and fila.access_token),
        "location": fila.location_id if fila else None,
        "token_vence": fila.expira.isoformat() if fila else None,
        "redirect_uri": redirect_uri(),
        **estado,
    }


# ── Firma ──────────────────────────────────────────────────────────────────────

def firma_valida(cuerpo: bytes, firma: str | None) -> bool:
    if not firma:
        return False
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_public_key

        llave = load_pem_public_key(GHL_PUBLIC_KEY_PEM.encode())
        llave.verify(base64.b64decode(firma), cuerpo)
        return True
    except Exception:  # noqa: BLE001
        return False


# ── Mensaje del equipo escrito en GHL → WhatsApp ───────────────────────────────

async def _nombre_usuario(c: httpx.AsyncClient, user_id: str) -> str:
    if not user_id:
        return ""
    if user_id in _nombres:
        return _nombres[user_id]
    nombre = ""
    try:
        r = await c.get(f"/users/{user_id}")
        if r.status_code == 200:
            u = r.json()
            nombre = (u.get("name") or f"{u.get('firstName', '')} {u.get('lastName', '')}").strip()
    except httpx.HTTPError:
        pass
    _nombres[user_id] = nombre
    return nombre


async def _telefono_local(telefono: str) -> str:
    """El teléfono como lo guarda Sofía (52 vs 521): el que ya tenga historial."""
    from sqlalchemy import select

    from agent.memory import Mensaje

    t = "".join(ch for ch in telefono if ch.isdigit())
    candidatos = [t]
    if t.startswith("52") and len(t) == 12:
        candidatos.append("521" + t[2:])
    if t.startswith("521") and len(t) == 13:
        candidatos.append("52" + t[3:])
    async with async_session() as s:
        for cand in candidatos:
            hay = (await s.execute(select(Mensaje.id).where(Mensaje.telefono == cand).limit(1))).first()
            if hay:
                return cand
    return t


async def _status(c: httpx.AsyncClient, message_id: str, ok: bool, error: str = "") -> None:
    cuerpo: dict = {"status": "delivered" if ok else "failed"}
    if not ok:
        cuerpo["error"] = {"code": "1", "type": "saas", "message": error[:300]}
    try:
        r = await c.put(f"/conversations/messages/{message_id}/status", json=cuerpo)
        if r.status_code >= 400:
            logger.warning(f"[GHL proveedor] status {message_id}: {r.status_code} {r.text[:200]}")
    except httpx.HTTPError as e:
        logger.warning(f"[GHL proveedor] status {message_id}: {e}")


async def entregar(datos: dict) -> None:
    """Manda por WhatsApp lo que el equipo escribió en GHL. Corre en segundo plano."""
    from agent import main as app_main
    from agent import memory
    from agent.consola import _prefijo

    estado["recibidos"] += 1
    message_id = datos.get("messageId") or ""
    texto = (datos.get("message") or "").strip()
    adjuntos = [a for a in (datos.get("attachments") or []) if isinstance(a, str)]
    if adjuntos:  # por ahora solo texto: los archivos van como enlace
        texto = (texto + "\n" + "\n".join(adjuntos)).strip()
    tok = await token()
    if not tok:
        estado["fallidos"] += 1
        estado["ultimo_error"] = "app no instalada / sin token"
        return
    async with httpx.AsyncClient(base_url=API, headers=headers(tok), timeout=30) as c:
        tel = await _telefono_local(datos.get("phone") or "")
        prov = app_main.proveedor
        if not tel or not texto or prov is None:
            await _status(c, message_id, False, "Sin teléfono o sin texto")
            estado["fallidos"] += 1
            return
        try:
            ok = await prov.enviar_mensaje(tel, texto)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[GHL proveedor] error enviando a …{tel[-4:]}: {e}")
            ok = False
        if not ok:
            estado["fallidos"] += 1
            estado["ultimo_error"] = f"WhatsApp rechazó …{tel[-4:]}"
            await _status(c, message_id, False,
                          "WhatsApp no lo aceptó. Si la clienta no ha escrito en 24 h, solo se pueden mandar "
                          "plantillas: escríbele desde el celular del spa.")
            return
        nombre = await _nombre_usuario(c, datos.get("userId") or "")
        # espejo=False: el mensaje ya está en GHL (lo escribieron ahí), no se copia de vuelta.
        await memory.guardar_mensaje(tel, "assistant", _prefijo(nombre) + texto, espejo=False)
        try:
            await memory.registrar_outbound_equipo(tel)  # Sofía se calla en ese chat
        except Exception:  # noqa: BLE001
            pass
        await _status(c, message_id, True)
        estado["enviados"] += 1
        logger.info(f"GHL ({nombre or 'equipo'}) → …{tel[-4:]}: {texto[:60]}")
