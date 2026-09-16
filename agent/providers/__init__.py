# agent/providers/__init__.py — Factory de proveedores
# Generado por AgentKit

"""
Elige el proveedor de WhatsApp segun la variable WHATSAPP_PROVIDER del .env.
"""

import os

from agent.providers.base import MensajeEntrante, ProveedorWhatsApp

PROVEEDORES_SOPORTADOS = ("zernio", "meta")


def obtener_proveedor() -> ProveedorWhatsApp:
    """
    Retorna el proveedor configurado en .env.

    Ojo: esto NO se ejecuta al importar el modulo. Si la configuracion esta mal, el
    servidor igual tiene que arrancar y contarlo en el health check, en vez de morirse
    en el import y dejar a Railway reiniciando a ciegas.
    """
    proveedor = os.getenv("WHATSAPP_PROVIDER", "").strip().lower()

    if not proveedor:
        raise ValueError(
            "WHATSAPP_PROVIDER no esta configurado en el .env. "
            f"Valores validos: {' | '.join(PROVEEDORES_SOPORTADOS)}"
        )

    if proveedor == "zernio":
        from agent.providers.zernio import ProveedorZernio

        return ProveedorZernio()

    if proveedor == "meta":
        from agent.providers.meta import ProveedorMeta

        return ProveedorMeta()

    raise ValueError(
        f"Proveedor no soportado: '{proveedor}'. "
        f"Valores validos: {' | '.join(PROVEEDORES_SOPORTADOS)}"
    )


def obtener_proveedor_messenger():
    """
    Devuelve el proveedor de Facebook Messenger si está configurado, o None.

    Messenger es un canal EXTRA (no reemplaza a WhatsApp): convive con el proveedor
    principal. Se activa con solo poner MESSENGER_PAGE_ACCESS_TOKEN en el entorno.
    """
    if not os.getenv("MESSENGER_PAGE_ACCESS_TOKEN"):
        return None

    from agent.providers.messenger import ProveedorMessenger

    return ProveedorMessenger()


__all__ = [
    "MensajeEntrante",
    "ProveedorWhatsApp",
    "PROVEEDORES_SOPORTADOS",
    "obtener_proveedor",
    "obtener_proveedor_messenger",
]
