# agent/brain.py — Cerebro del agente: conexion con Claude
# Generado por AgentKit

"""
Logica de IA del agente. Lee el system prompt de config/prompts.yaml y genera las
respuestas con la API de Anthropic.

Ademas del texto, Sofia puede USAR HERRAMIENTAS (tool use) para agendar de verdad en
Google Calendar: consulta la disponibilidad y crea la cita. Las herramientas solo se
ofrecen si la agenda esta configurada (ver agent/calendar_tool.py); si no, Sofia cae al
modo "el equipo confirma".
"""

import asyncio
import logging
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

import yaml
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

from agent import calendar_tool

load_dotenv()
logger = logging.getLogger("agentkit")

client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# El modelo se cambia desde .env, sin tocar el codigo.
#   claude-opus-5     el mas capaz             $5 / $25 por millon de tokens
#   claude-sonnet-5   el balanceado (default)  $3 / $15
#   claude-haiku-4-5  el mas barato y rapido   $1 / $5
# El "or" y no el default de os.getenv: una variable declarada vacia en el .env
# devuelve "" y dejaria al agente sin modelo.
MODELO = os.getenv("ANTHROPIC_MODEL") or "claude-sonnet-5"

# Es un bot de respuestas cortas: con esfuerzo bajo contesta mas rapido y mas barato.
# Dejalo vacio en el .env para no mandar el parametro.
ESFUERZO = os.getenv("ANTHROPIC_EFFORT", "low").strip()

# WhatsApp son mensajes cortos, pero este tope NO es solo la respuesta: en los modelos
# actuales el razonamiento interno tambien cuenta contra el. Con el margen justo, una
# pregunta que exija pensar un poco deja al agente sin espacio para contestar.
MAX_TOKENS = int(os.getenv("ANTHROPIC_MAX_TOKENS") or "4096")

# Los modelos mas viejos no aceptan output_config. Si la primera llamada falla por eso,
# se reintenta sin el parametro y se recuerda para las siguientes.
_soporta_esfuerzo = True

TZ = ZoneInfo(os.getenv("GOOGLE_CALENDAR_TZ") or "America/Mexico_City")
_DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_MESES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]

# Tope de vueltas del ciclo de tool use, por si el modelo se cicla pidiendo herramientas.
MAX_VUELTAS_TOOLS = 6

# Definicion de las herramientas que Sofia puede llamar (formato Anthropic tool use).
HERRAMIENTAS = [
    {
        "name": "consultar_disponibilidad",
        "description": (
            "Consulta el horario del spa y las citas ya ocupadas de un dia concreto, "
            "para proponerle a la clienta horarios libres antes de agendar. Usala ANTES "
            "de agendar cuando la clienta menciona un dia."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fecha": {
                    "type": "string",
                    "description": "Dia a consultar en formato YYYY-MM-DD.",
                }
            },
            "required": ["fecha"],
        },
    },
    {
        "name": "agendar_cita",
        "description": (
            "Registra una cita (queda 'por confirmar') en la agenda de 2 Body Pachuca. "
            "Usala SOLO cuando ya tengas: nombre de la clienta, servicio/tratamiento, y un "
            "dia y hora concretos que ella acepto. No inventes horas: confirma con la clienta "
            "antes de llamar esta herramienta."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre de la clienta."},
                "servicio": {
                    "type": "string",
                    "description": "Servicio o tratamiento (o 'valoración').",
                },
                "fecha_hora": {
                    "type": "string",
                    "description": "Inicio de la cita en formato YYYY-MM-DDTHH:MM (24h, hora de Pachuca).",
                },
                "duracion_min": {
                    "type": "integer",
                    "description": "Duracion en minutos. Si no sabes, usa 60.",
                },
            },
            "required": ["nombre", "servicio", "fecha_hora"],
        },
    },
]


def cargar_config_prompts() -> dict:
    """Lee toda la configuracion desde config/prompts.yaml."""
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/prompts.yaml no encontrado")
        return {}


def cargar_system_prompt() -> str:
    """El system prompt: quien es el agente y que sabe del negocio."""
    return cargar_config_prompts().get(
        "system_prompt", "Eres un asistente util. Responde siempre en espanol."
    )


def obtener_mensaje_error() -> str:
    """Que decirle al cliente cuando algo falla de nuestro lado."""
    return cargar_config_prompts().get(
        "error_message",
        "Lo siento, estoy teniendo problemas tecnicos. Por favor intenta de nuevo en unos minutos.",
    )


def obtener_mensaje_fallback() -> str:
    """Que decirle al cliente cuando no se entendio el mensaje."""
    return cargar_config_prompts().get(
        "fallback_message", "Disculpa, no entendi tu mensaje. Podrias reformularlo?"
    )


def _contexto_temporal() -> str:
    """Una linea con la fecha/hora actual para que Sofia entienda 'hoy', 'el sabado', etc."""
    ahora = datetime.now(TZ)
    return (
        f"\n\n## Fecha y hora actual (Pachuca)\n"
        f"Ahora es {_DIAS[ahora.weekday()]} {ahora.day} de {_MESES[ahora.month - 1]} "
        f"de {ahora.year}, {ahora.strftime('%H:%M')} h. Usa esto para interpretar 'hoy', "
        f"'mañana', 'el sábado', etc., y nunca agendes en el pasado."
    )


def _instrucciones_agenda(hay_agenda: bool) -> str:
    """Instruccion en runtime segun si la agenda automatica esta prendida o no."""
    if hay_agenda:
        return (
            "\n\n## Agenda automática (tienes herramientas)\n"
            "Puedes agendar DE VERDAD. Cuando la clienta quiera cita o valoración:\n"
            "1) Reúne su nombre y el servicio (uno a la vez, con calidez).\n"
            "2) Cuando mencione un día, usa `consultar_disponibilidad` y ofrécele 1-2 horarios "
            "libres dentro del horario del spa.\n"
            "3) Cuando ella acepte una hora concreta, llama `agendar_cita`.\n"
            "4) Al confirmarse, dile que su espacio quedó apartado (por confirmar) y que el "
            "equipo le confirma por aquí. Las valoraciones son sin costo y duran ~60 min.\n"
            "No inventes horarios: siempre revisa con la herramienta. Si la herramienta falla, "
            "toma la solicitud y dile que el equipo confirma."
        )
    return (
        "\n\n## Agenda\n"
        "La agenda automática no está disponible ahora: toma la solicitud (nombre, servicio, "
        "día y hora que le acomoden) y dile que una compañera del equipo le confirma la "
        "disponibilidad por aquí."
    )


def _extraer_texto(respuesta) -> str:
    """
    Junta el texto de la respuesta de Claude.

    Ojo: NO se puede hacer respuesta.content[0].text. La respuesta es una lista de
    bloques y el primero no siempre es texto (los modelos que razonan devuelven
    primero un bloque de pensamiento). Hay que filtrar por tipo.
    """
    partes = [b.text for b in respuesta.content if b.type == "text"]
    return "\n".join(p for p in partes if p).strip()


def _es_error_de_esfuerzo(error: Exception) -> bool:
    """
    True solo si el modelo rechazo la llamada POR el parametro output_config/effort.

    Se exige que sea un 400 de peticion invalida y no cualquier error que mencione la
    palabra: un 529 de sobrecarga que la nombre de paso no debe apagar el parametro
    para todo el proceso.
    """
    if getattr(error, "status_code", None) != 400:
        return False
    texto = str(error).lower()
    return "output_config" in texto or "effort" in texto


async def _ejecutar_herramienta(nombre: str, args: dict, telefono: str) -> str:
    """Corre una herramienta y devuelve un texto para el bloque tool_result."""
    try:
        if nombre == "consultar_disponibilidad":
            fecha = date.fromisoformat(args["fecha"])
            hd = calendar_tool.horario_dia(fecha)
            if hd is None:
                return f"El {fecha.isoformat()} es domingo: el spa está cerrado."
            citas = await asyncio.to_thread(calendar_tool.citas_del_dia, fecha)
            ap, ci = hd
            base = (
                f"Horario ese día: {ap.strftime('%H:%M')} a {ci.strftime('%H:%M')}. "
            )
            if citas:
                ocup = "; ".join(f"{c['inicio']}-{c['fin']}" for c in citas)
                return base + f"Ya ocupado: {ocup}. Ofrece un hueco libre distinto."
            return base + "No hay citas todavía ese día; casi cualquier hora sirve."

        if nombre == "agendar_cita":
            inicio = datetime.fromisoformat(args["fecha_hora"])
            res = await asyncio.to_thread(
                calendar_tool.crear_cita,
                args.get("nombre", "").strip() or "Clienta",
                telefono or "",
                args.get("servicio", "").strip() or "Valoración",
                inicio,
                int(args.get("duracion_min") or 60),
                args.get("notas", ""),
            )
            if res.get("ok"):
                return (
                    f"CITA CREADA para {res['cuando']} (queda por confirmar). "
                    "Confírmale a la clienta con calidez que ya apartaste su espacio y que "
                    "el equipo le confirma por aquí."
                )
            return (
                f"NO se pudo agendar: {res.get('error', 'error desconocido')}. "
                "Ofrécele otra hora o pasar con el equipo, sin alarmarla."
            )
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error en herramienta {nombre}: {e}")
        return "Hubo un problema con la agenda. Toma la solicitud y di que el equipo confirma."

    return f"Herramienta desconocida: {nombre}"


async def generar_respuesta(
    mensaje: str, historial: list[dict], telefono: str = ""
) -> tuple[str, bool]:
    """
    Genera una respuesta con Claude, con posibilidad de agendar en calendario (tool use).

    Args:
        mensaje: el mensaje nuevo del cliente
        historial: los mensajes anteriores, [{"role": "user"|"assistant", "content": "..."}]
        telefono: numero de la clienta (para guardarlo en la cita). No lo elige el modelo.

    Returns:
        (texto, es_respuesta_real)

        "es_respuesta_real" es False cuando lo que se devuelve es un aviso tecnico
        (error o fallback) y no una respuesta del agente. main.py lo usa para no
        guardar esos avisos en el historial.
    """
    global _soporta_esfuerzo

    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback(), False

    hay_agenda = calendar_tool.calendario_configurado()
    system_prompt = (
        cargar_system_prompt() + _contexto_temporal() + _instrucciones_agenda(hay_agenda)
    )
    herramientas = HERRAMIENTAS if hay_agenda else []

    mensajes = [{"role": m["role"], "content": m["content"]} for m in historial]
    mensajes.append({"role": "user", "content": mensaje})

    async def _crear():
        global _soporta_esfuerzo
        extras = {"output_config": {"effort": ESFUERZO}} if (_soporta_esfuerzo and ESFUERZO) else {}
        params = {
            "model": MODELO,
            "max_tokens": MAX_TOKENS,
            "system": system_prompt,
            "messages": mensajes,
        }
        if herramientas:
            params["tools"] = herramientas
        try:
            return await client.messages.create(**params, **extras)
        except Exception as e:  # noqa: BLE001
            if extras and _es_error_de_esfuerzo(e):
                logger.warning(
                    f"El modelo {MODELO} no acepta output_config.effort; se reintenta sin ese parametro."
                )
                _soporta_esfuerzo = False
                return await client.messages.create(**params)
            raise

    try:
        for _ in range(MAX_VUELTAS_TOOLS):
            respuesta = await _crear()

            if respuesta.stop_reason == "tool_use":
                # Guardamos el turno del modelo (con los bloques tool_use) y respondemos
                # cada herramienta con su tool_result, para que Claude siga con el resultado.
                mensajes.append({"role": "assistant", "content": respuesta.content})
                resultados = []
                for bloque in respuesta.content:
                    if bloque.type == "tool_use":
                        salida = await _ejecutar_herramienta(
                            bloque.name, bloque.input or {}, telefono
                        )
                        resultados.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": bloque.id,
                                "content": salida,
                            }
                        )
                mensajes.append({"role": "user", "content": resultados})
                continue

            # Turno normal: ya hay respuesta final para la clienta.
            if getattr(respuesta, "stop_reason", None) == "max_tokens":
                logger.warning(
                    f"La respuesta se corto por el tope de {MAX_TOKENS} tokens. "
                    "Sube ANTHROPIC_MAX_TOKENS o acorta el system prompt si pasa seguido."
                )
            texto = _extraer_texto(respuesta)
            if not texto:
                logger.warning("Claude devolvio una respuesta sin texto")
                return obtener_mensaje_fallback(), False
            logger.info(
                f"Respuesta generada con {MODELO} "
                f"({respuesta.usage.input_tokens} in / {respuesta.usage.output_tokens} out)"
            )
            return texto, True

        logger.error("Se agotaron las vueltas de tool use sin respuesta final")
        return obtener_mensaje_error(), False

    except Exception as e:  # noqa: BLE001
        logger.error(f"Error llamando a Claude: {e}")
        return obtener_mensaje_error(), False
