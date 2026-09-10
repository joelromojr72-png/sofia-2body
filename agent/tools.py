# agent/tools.py — Herramientas del agente
# Generado por AgentKit

"""
Herramientas especificas del negocio (2 Body Pachuca).

OJO: estas funciones NO se ejecutan solas todavia. La informacion del negocio le llega
a Sofia por el system prompt (config/prompts.yaml), asi que para CONTESTAR preguntas
no hace falta nada de aca. Este archivo es el lugar para las ACCIONES —agendar una cita,
registrar un lead— y conectarlas al ciclo de tool use de Claude es un paso aparte.

Casos de uso elegidos: preguntas frecuentes, agendar citas/valoraciones, leads/ventas.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import yaml

logger = logging.getLogger("agentkit")

CARPETA_KNOWLEDGE = Path("knowledge")
CARPETA_DATOS = Path("data")


def cargar_info_negocio() -> dict:
    """Carga la informacion del negocio desde config/business.yaml."""
    try:
        with open("config/business.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/business.yaml no encontrado")
        return {}


def obtener_horario() -> dict:
    """Retorna el horario de atencion del negocio."""
    info = cargar_info_negocio()
    return {
        "horario": info.get("negocio", {}).get("horario", "No disponible"),
        "esta_abierto": True,  # TODO: calcular segun la hora actual y el horario
    }


def buscar_en_knowledge(consulta: str) -> str:
    """
    Busca informacion en los archivos de /knowledge.
    Retorna los fragmentos que coinciden con la consulta.
    """
    if not CARPETA_KNOWLEDGE.is_dir():
        return "No hay archivos de conocimiento disponibles."

    resultados = []
    for ruta in sorted(CARPETA_KNOWLEDGE.iterdir()):
        if ruta.name.startswith(".") or not ruta.is_file():
            continue
        try:
            contenido = ruta.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binarios y archivos ilegibles se saltean
        if consulta.lower() in contenido.lower():
            resultados.append(f"[{ruta.name}]: {contenido[:500]}")

    if resultados:
        return "\n---\n".join(resultados)
    return "No encontre informacion especifica sobre eso en mis archivos."


# ════════════════════════════════════════════════════════════
# ACCIONES para 2 Body Pachuca — segun los casos de uso elegidos.
#
# Por ahora estas funciones solo REGISTRAN la solicitud en un archivo local
# (data/solicitudes.jsonl) para que quede constancia. Sofia toma los datos en la
# conversacion; el equipo confirma la disponibilidad real. Conectar esto a una agenda
# de verdad (Google Calendar, un CRM, etc.) es un paso siguiente: pideselo a Claude Code.
# ════════════════════════════════════════════════════════════


def _guardar_registro(tipo: str, datos: dict) -> None:
    """Anexa un registro a data/solicitudes.jsonl con marca de tiempo."""
    CARPETA_DATOS.mkdir(exist_ok=True)
    registro = {
        "tipo": tipo,
        "creado_en": datetime.now(timezone.utc).isoformat(),
        **datos,
    }
    with open(CARPETA_DATOS / "solicitudes.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(registro, ensure_ascii=False) + "\n")


def solicitar_cita(telefono: str, nombre: str, servicio: str, fecha_deseada: str) -> dict:
    """
    Registra una solicitud de cita/valoracion.

    NO confirma la cita: solo deja la solicitud para que el equipo confirme la
    disponibilidad real. Retorna un dict con el estado.
    """
    _guardar_registro(
        "cita",
        {
            "telefono": telefono,
            "nombre": nombre,
            "servicio": servicio,
            "fecha_deseada": fecha_deseada,
        },
    )
    logger.info(f"Solicitud de cita registrada: {nombre} — {servicio} — {fecha_deseada}")
    return {
        "ok": True,
        "mensaje": "Solicitud registrada. El equipo confirmara la disponibilidad.",
    }


def registrar_lead(telefono: str, nombre: str, interes: str, notas: str = "") -> dict:
    """
    Registra un lead (posible cliente interesado en un tratamiento).

    Sirve para que el equipo de ventas de seguimiento. Retorna un dict con el estado.
    """
    _guardar_registro(
        "lead",
        {
            "telefono": telefono,
            "nombre": nombre,
            "interes": interes,
            "notas": notas,
        },
    )
    logger.info(f"Lead registrado: {nombre} — interes: {interes}")
    return {"ok": True, "mensaje": "Lead registrado para seguimiento."}
