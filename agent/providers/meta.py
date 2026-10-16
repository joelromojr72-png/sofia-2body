# agent/providers/meta.py - Adaptador para Meta WhatsApp Cloud API
# Generado por AgentKit

"""
Conexion directa contra la API oficial de Meta.
Documentacion: https://developers.facebook.com/docs/whatsapp/cloud-api
"""

import hashlib
import hmac
import logging
import os

import httpx
from fastapi import Request

from agent.providers.base import MensajeEntrante, ProveedorWhatsApp

logger = logging.getLogger("agentkit")


def _a_segundos(valor) -> float | None:
    """Convierte el timestamp de Meta (segundos unix como texto) a número; None si no viene."""
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


_NOMBRES_MEDIA = {
    "audio": "un audio", "voice": "un audio", "image": "una imagen", "video": "un video",
    "document": "un documento", "sticker": "un sticker", "location": "una ubicación",
    "contacts": "un contacto", "template": "una plantilla",
}


def _texto_de_mensaje(msg: dict) -> str:
    """Texto escrito de un mensaje de Meta: texto normal, botón de plantilla o respuesta interactiva."""
    tipo = msg.get("type")
    if tipo == "text":
        return ((msg.get("text") or {}).get("body") or "").strip()
    if tipo == "button":  # la clienta tocó un botón de respuesta rápida de una plantilla
        return ((msg.get("button") or {}).get("text") or "").strip()
    if tipo == "interactive":
        inter = msg.get("interactive") or {}
        sel = inter.get("button_reply") or inter.get("list_reply") or {}
        return (sel.get("title") or "").strip()
    return ""


def _describir_media(msg: dict, quien: str) -> str:
    """Resumen legible de un mensaje que no es texto, para el historial: '[La clienta envió un audio]'."""
    tipo = msg.get("type") or "mensaje"
    nombre = _NOMBRES_MEDIA.get(tipo, "un mensaje que no es texto")
    caption = ((msg.get(tipo) or {}).get("caption") or "").strip() if isinstance(msg.get(tipo), dict) else ""
    return f"[{quien} envió {nombre}{': ' + caption if caption else ''}]"


class ProveedorMeta(ProveedorWhatsApp):
    """Proveedor de WhatsApp usando la API oficial de Meta (Cloud API)."""

    def __init__(self):
        self.access_token = os.getenv("META_ACCESS_TOKEN", "")
        self.phone_number_id = os.getenv("META_PHONE_NUMBER_ID", "")
        # Mismo cuidado que en zernio.py: una variable declarada pero vacia en el .env
        # devuelve "" y no el default, asi que se usa "or".
        self.verify_token = os.getenv("META_VERIFY_TOKEN") or "agentkit-verify"
        self.app_secret = os.getenv("META_APP_SECRET", "")
        self.api_version = os.getenv("META_API_VERSION") or "v25.0"

        if not self.access_token or not self.phone_number_id:
            logger.warning(
                "Faltan META_ACCESS_TOKEN o META_PHONE_NUMBER_ID: el agente no va a poder responder"
            )
        if not self.app_secret:
            logger.warning(
                "META_APP_SECRET no esta configurado: los webhooks NO se verifican. "
                "Sirve para probar, pero no lo dejes asi en produccion."
            )

    # -- Recibir ----------------------------------------------------------

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
            return True  # modo pruebas, ya se advirtio al arrancar

        cabecera = request.headers.get("X-Hub-Signature-256", "")
        if not cabecera.startswith("sha256="):
            logger.warning("Llego un webhook sin firma X-Hub-Signature-256: rechazado")
            return False

        cuerpo = await request.body()
        firma_esperada = hmac.new(
            self.app_secret.encode("utf-8"), cuerpo, hashlib.sha256
        ).hexdigest()

        # Igual que en zernio.py: compare_digest sobre str exige ASCII puro y un header
        # con bytes raros tiraria TypeError, devolviendo 500 en vez de 401.
        try:
            iguales = hmac.compare_digest(firma_esperada, cabecera.removeprefix("sha256="))
        except TypeError:
            logger.warning("La firma del webhook trae caracteres invalidos: rechazado")
            return False

        if not iguales:
            logger.warning("Firma de webhook invalida: rechazado")
            return False
        return True

    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """
        Recorre el payload anidado de Meta Cloud API.

        Además de los mensajes entrantes del cliente (`messages`), captura los ECHOES de
        COEXISTENCIA (`smb_message_echoes`): cuando una persona del EQUIPO contesta desde la
        app de WhatsApp Business en el mismo número, Meta manda una copia. La marcamos como
        `tipo=echo_equipo` para que main.py la guarde en el historial (turno del negocio) y
        Sofía sepa qué ya respondió una persona, sin volver a contestar ese echo.
        """
        body = await request.json()
        mensajes: list[MensajeEntrante] = []

        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value") or {}

                # 1) Echoes del equipo humano (coexistencia): el negocio escribió desde la app.
                #    La conversación es con `to` (el cliente). Se capturan TODOS los tipos
                #    (texto, imagen, audio, documento, sticker…): aunque el equipo solo mande
                #    una foto de la promo, ya está atendiendo y Sofía no debe escribir encima.
                for eco in value.get("message_echoes", []):
                    cliente = eco.get("to", "")
                    if not cliente:
                        continue
                    eco_id = eco.get("id", "")
                    texto_eco = _texto_de_mensaje(eco) or _describir_media(eco, "El equipo")
                    mensajes.append(
                        MensajeEntrante(
                            telefono=cliente,
                            texto=texto_eco,
                            mensaje_id=eco_id,
                            es_propio=False,  # main.py lo enruta por 'tipo', no lo contesta
                            contexto={"evento_id": f"eco:{eco_id}", "tipo": "echo_equipo"},
                        )
                    )

                # 2) Mensajes entrantes del cliente. Texto y botones se contestan normal; los
                #    audios/fotos/archivos se marcan como 'media' para que main.py le pida
                #    con amabilidad que lo escriba (Sofía no puede escucharlos ni verlos).
                for msg in value.get("messages", []):
                    tipo_msg = msg.get("type")
                    if tipo_msg in ("reaction", "unsupported", "system", "ephemeral"):
                        continue  # reacciones (👍 sobre un mensaje) y avisos: no se contestan
                    texto = _texto_de_mensaje(msg)
                    contexto = {
                        "evento_id": msg.get("id", ""),
                        # Hora en que la clienta lo escribió (segundos unix). Sirve
                        # para no contestar mensajes viejos que Meta reentrega tarde.
                        "enviado_en": _a_segundos(msg.get("timestamp")),
                    }
                    if not texto:
                        texto = _describir_media(msg, "La clienta")
                        contexto["tipo"] = "media"
                    mensajes.append(
                        MensajeEntrante(
                            telefono=msg.get("from", ""),
                            texto=texto,
                            mensaje_id=msg.get("id", ""),
                            # Meta solo entrega mensajes entrantes por este canal
                            es_propio=False,
                            contexto=contexto,
                        )
                    )
        return mensajes

    # -- Enviar -----------------------------------------------------------

    async def enviar_mensaje(
        self, telefono: str, mensaje: str, contexto: dict | None = None
    ) -> bool:
        """Envia un mensaje de texto por la Cloud API. Meta no necesita el contexto."""
        if not self.access_token or not self.phone_number_id:
            logger.error("No se puede enviar: faltan META_ACCESS_TOKEN o META_PHONE_NUMBER_ID")
            return False

        url = f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages"

        try:
            async with httpx.AsyncClient(timeout=30.0) as cliente:
                r = await cliente.post(
                    url,
                    json={
                        "messaging_product": "whatsapp",
                        "to": telefono,
                        "type": "text",
                        "text": {"body": mensaje},
                    },
                    headers={
                        "Authorization": f"Bearer {self.access_token}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.HTTPError as e:
            logger.error(f"Error de red hablando con Meta: {e}")
            return False

        if r.status_code == 200:
            return True

        logger.error(f"Meta rechazo el envio [{r.status_code}]: {r.text[:500]}")
        return False

    # -- Plantillas (seguimiento fuera de la ventana de 24 h) -------------

    async def enviar_plantilla(
        self, telefono: str, template_name: str, params: list[str] | None = None,
        lang: str = "es_MX",
    ) -> bool:
        """
        Envia una PLANTILLA aprobada por Meta. Fuera de las 24 h desde el ultimo mensaje
        del cliente, WhatsApp SOLO permite plantillas. `params` llena las variables {{1}},
        {{2}}... del cuerpo, en orden.
        """
        if not self.access_token or not self.phone_number_id:
            logger.error("No se puede enviar plantilla: faltan credenciales de Meta")
            return False

        template: dict = {"name": template_name, "language": {"code": lang}}
        if params:
            template["components"] = [
                {"type": "body",
                 "parameters": [{"type": "text", "text": str(p)} for p in params]}
            ]

        url = f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages"
        try:
            async with httpx.AsyncClient(timeout=30.0) as cliente:
                r = await cliente.post(
                    url,
                    json={"messaging_product": "whatsapp", "to": telefono,
                          "type": "template", "template": template},
                    headers={"Authorization": f"Bearer {self.access_token}",
                             "Content-Type": "application/json"},
                )
        except httpx.HTTPError as e:
            logger.error(f"Error de red enviando plantilla: {e}")
            return False

        if r.status_code == 200:
            return True
        logger.error(f"Meta rechazo la plantilla [{r.status_code}]: {r.text[:400]}")
        return False

    # -- Diagnostico ------------------------------------------------------

    async def verificar_conexion(self) -> tuple[bool, str]:
        """Lee el numero desde la Graph API para confirmar que el token sirve."""
        if not self.access_token or not self.phone_number_id:
            return False, "Faltan META_ACCESS_TOKEN o META_PHONE_NUMBER_ID"

        try:
            async with httpx.AsyncClient(timeout=15.0) as cliente:
                r = await cliente.get(
                    f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}",
                    params={"fields": "display_phone_number,verified_name,quality_rating"},
                    headers={"Authorization": f"Bearer {self.access_token}"},
                )
        except httpx.HTTPError as e:
            return False, f"No se pudo contactar a Meta: {e}"

        if r.status_code != 200:
            return False, f"Meta respondio {r.status_code}: {r.text[:200]}"

        datos = r.json()
        return True, (
            f"Numero {datos.get('display_phone_number', '?')} conectado "
            f"(calidad: {datos.get('quality_rating', '?')})"
        )
