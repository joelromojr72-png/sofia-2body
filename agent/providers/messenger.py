# agent/providers/messenger.py — Adaptador para Facebook Messenger (Meta)
# Generado por AgentKit

"""
Conexión directa contra la Messenger Platform de Meta (Send API + webhooks).

Comparte la MISMA app de Facebook que WhatsApp, así que reutiliza el App Secret
(firma de webhooks) y el Verify Token. Lo único propio de Messenger es el
**Page Access Token** de la página de Facebook del negocio.

Implementa la misma interfaz que el resto de proveedores (ProveedorWhatsApp), de modo
que main.py lo trata igual: parsea el webhook, genera respuesta con Claude y contesta.

Documentación: https://developers.facebook.com/docs/messenger-platform
"""

import hashlib
import hmac
import logging
import os

import httpx
from fastapi import Request

from agent.providers.base import MensajeEntrante, ProveedorWhatsApp

logger = logging.getLogger("agentkit")

# Un PSID de Messenger es numérico igual que un teléfono de WhatsApp. Para que NUNCA se
# confundan en la memoria (historial, candados, leads), a los de Messenger les ponemos
# este prefijo en el campo "telefono". El PSID limpio para responder viaja en el contexto.
PREFIJO_MSGR = "msgr:"


class ProveedorMessenger(ProveedorWhatsApp):
    """Proveedor de Facebook Messenger usando la Send API de Meta."""

    def __init__(self):
        self.page_access_token = os.getenv("MESSENGER_PAGE_ACCESS_TOKEN", "")
        self.page_id = os.getenv("MESSENGER_PAGE_ID", "")
        # Comparte app con WhatsApp: si no se define uno propio, cae al de Meta.
        self.verify_token = (
            os.getenv("MESSENGER_VERIFY_TOKEN")
            or os.getenv("META_VERIFY_TOKEN")
            or "agentkit-verify"
        )
        self.app_secret = os.getenv("MESSENGER_APP_SECRET") or os.getenv("META_APP_SECRET", "")
        self.api_version = (
            os.getenv("MESSENGER_API_VERSION") or os.getenv("META_API_VERSION") or "v25.0"
        )

        if not self.page_access_token:
            logger.warning(
                "MESSENGER_PAGE_ACCESS_TOKEN no está configurado: "
                "Sofía no podrá responder en Messenger"
            )
        if not self.app_secret:
            logger.warning(
                "Sin App Secret para Messenger: los webhooks NO se verifican. "
                "Sirve para probar, pero no lo dejes así en producción."
            )

    # ── Recibir ──────────────────────────────────────────────────────────

    async def validar_webhook(self, request: Request) -> str | None:
        """
        Meta hace un GET con hub.challenge la primera vez, para comprobar que la URL es tuya.
        Hay que devolver el challenge tal cual, como texto plano.
        """
        params = request.query_params
        if (
            params.get("hub.mode") == "subscribe"
            and params.get("hub.verify_token") == self.verify_token
        ):
            return params.get("hub.challenge") or ""
        return None

    async def verificar_firma(self, request: Request) -> bool:
        """Compara el header X-Hub-Signature-256 contra el HMAC-SHA256 del cuerpo crudo."""
        if not self.app_secret:
            return True  # modo pruebas, ya se advirtió al arrancar

        cabecera = request.headers.get("X-Hub-Signature-256", "")
        if not cabecera.startswith("sha256="):
            logger.warning("Webhook de Messenger sin firma X-Hub-Signature-256: rechazado")
            return False

        cuerpo = await request.body()
        firma_esperada = hmac.new(
            self.app_secret.encode("utf-8"), cuerpo, hashlib.sha256
        ).hexdigest()

        # Igual que en meta.py: compare_digest sobre str exige ASCII puro; un header con
        # bytes raros tiraría TypeError y devolvería 500 en vez de 401.
        try:
            iguales = hmac.compare_digest(firma_esperada, cabecera.removeprefix("sha256="))
        except TypeError:
            logger.warning("La firma de Messenger trae caracteres inválidos: rechazado")
            return False

        if not iguales:
            logger.warning("Firma de webhook de Messenger inválida: rechazado")
            return False
        return True

    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """
        Normaliza el payload de Messenger.

        Estructura: object="page", entry[].messaging[] con sender.id (PSID del cliente),
        message.text y message.mid. Se ignoran los "echo" (mensajes que manda la propia
        página) y los eventos que no son mensajes de texto (delivery, read, postback...).
        """
        body = await request.json()
        mensajes: list[MensajeEntrante] = []

        if body.get("object") != "page":
            return []  # no es un webhook de Messenger

        for entry in body.get("entry", []):
            for evento in entry.get("messaging", []):
                mensaje = evento.get("message")
                if not mensaje:
                    continue  # delivery, read, postback, etc.
                if mensaje.get("is_echo"):
                    continue  # lo mandó la propia página, no un cliente
                texto = mensaje.get("text")
                if not texto:
                    continue  # por ahora solo texto (adjuntos/stickers se ignoran)

                psid = (evento.get("sender") or {}).get("id", "")
                if not psid:
                    continue

                mensajes.append(
                    MensajeEntrante(
                        telefono=f"{PREFIJO_MSGR}{psid}",
                        texto=texto,
                        mensaje_id=mensaje.get("mid", ""),
                        es_propio=False,
                        contexto={"evento_id": mensaje.get("mid", ""), "psid": psid},
                    )
                )
        return mensajes

    # ── Enviar ───────────────────────────────────────────────────────────

    async def enviar_mensaje(
        self, telefono: str, mensaje: str, contexto: dict | None = None
    ) -> bool:
        """
        Responde al cliente por la Send API.

        El PSID limpio viene en el contexto; si no, se recupera quitándole el prefijo al
        "telefono". messaging_type=RESPONSE = estamos contestando dentro de la ventana.
        """
        contexto = contexto or {}
        psid = contexto.get("psid") or telefono.removeprefix(PREFIJO_MSGR)

        if not self.page_access_token:
            logger.error("No se puede enviar a Messenger: falta MESSENGER_PAGE_ACCESS_TOKEN")
            return False

        url = f"https://graph.facebook.com/{self.api_version}/me/messages"

        try:
            async with httpx.AsyncClient(timeout=30.0) as cliente:
                r = await cliente.post(
                    url,
                    params={"access_token": self.page_access_token},
                    json={
                        "recipient": {"id": psid},
                        "messaging_type": "RESPONSE",
                        "message": {"text": mensaje},
                    },
                )
        except httpx.HTTPError as e:
            logger.error(f"Error de red hablando con Messenger: {e}")
            return False

        if r.status_code == 200:
            return True

        logger.error(f"Messenger rechazó el envío [{r.status_code}]: {r.text[:500]}")
        return False

    # ── Diagnóstico ──────────────────────────────────────────────────────

    async def verificar_conexion(self) -> tuple[bool, str]:
        """
        Confirma que el Page Token sirve para MENSAJERÍA.

        Ojo: /me?fields=name pediría el permiso pages_read_engagement (que no necesitamos
        y que el token de mensajería no trae). En su lugar consultamos las apps suscritas
        de la página, endpoint que funciona con pages_messaging y confirma que el webhook
        quedó enganchado.
        """
        if not self.page_access_token:
            return False, "Falta MESSENGER_PAGE_ACCESS_TOKEN"

        try:
            async with httpx.AsyncClient(timeout=15.0) as cliente:
                r = await cliente.get(
                    f"https://graph.facebook.com/{self.api_version}/me/subscribed_apps",
                    params={"access_token": self.page_access_token},
                )
        except httpx.HTTPError as e:
            return False, f"No se pudo contactar a Messenger: {e}"

        if r.status_code != 200:
            return False, f"Messenger respondió {r.status_code}: {r.text[:200]}"

        return True, f"Página conectada (id {self.page_id or '?'}), token de mensajería OK"
