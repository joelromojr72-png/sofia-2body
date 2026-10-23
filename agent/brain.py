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

from agent import calendar_tool, memory

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
            "Devuelve los HORARIOS LIBRES reales de un día (ya descontando las citas "
            "ocupadas y el horario del spa). Úsala SIEMPRE antes de proponerle u ofrecerle "
            "cualquier hora a la clienta: nunca ofrezcas una hora sin haberla consultado aquí. "
            "Ofrécele solo horarios que esta herramienta marque libres."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fecha": {
                    "type": "string",
                    "description": "Dia a consultar en formato YYYY-MM-DD.",
                },
                "duracion_min": {
                    "type": "integer",
                    "description": "Duración de la cita en minutos (si no sabes, usa 60).",
                },
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
                    "description": "Tratamiento o paquete que la clienta va a empezar.",
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
    {
        "name": "consultar_mis_citas",
        "description": (
            "Lista las próximas citas de la clienta con la que estás hablando (se identifica "
            "sola por su WhatsApp). Úsala cuando quiera cancelar, cambiar o confirmar su cita, "
            "para saber cuál es y obtener su identificador antes de cancelar."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "cancelar_cita",
        "description": (
            "Cancela (borra) una cita de la clienta. Usa el `evento_id` que te dio "
            "`consultar_mis_citas`. Llama esto SOLO después de confirmar con la clienta cuál "
            "cita quiere cancelar. Si tiene varias, pregúntale cuál antes de cancelar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "evento_id": {
                    "type": "string",
                    "description": "Identificador de la cita a cancelar (de consultar_mis_citas).",
                }
            },
            "required": ["evento_id"],
        },
    },
    {
        "name": "cerrar_conversacion",
        "description": (
            "Apaga el seguimiento proactivo de esta clienta (no le volveremos a escribir hasta "
            "que ella escriba). Úsala SOLO cuando la clienta declinó con amabilidad (dijo que no "
            "por ahora, que lo pensará y ahí queda) y TÚ ya te despediste dejando la puerta "
            "abierta. NO la uses si sigue interesada, dudando o haciendo preguntas."
        ),
        "input_schema": {"type": "object", "properties": {}},
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


# Último resultado del cerebro, para el health check (detectar a tiempo, p. ej., saldo agotado).
estado_cerebro: dict = {"ultimo_ok": None, "ultimo_error": None, "detalle_error": None}


def _marcar_cerebro(ok: bool, detalle: str = "") -> None:
    ahora_iso = datetime.now(TZ).isoformat(timespec="seconds")
    if ok:
        estado_cerebro["ultimo_ok"] = ahora_iso
    else:
        estado_cerebro["ultimo_error"] = ahora_iso
        estado_cerebro["detalle_error"] = detalle[:300]


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
            "Puedes agendar DE VERDAD. Háblale SIEMPRE de USTED. Tu gancho estrella es invitarla "
            "a su PRIMERA SESIÓN SIN COSTO (corporal o facial, según lo que le interese) para que "
            "venga a vivir la experiencia y vea resultados reales; también puedes agendar el "
            "tratamiento o paquete que ella pida. Cuando quiera apartar su espacio:\n"
            "1) Pide solo lo indispensable, de a poco y sin interrogar: su nombre y qué le "
            "gustaría (si busca figura/moldear → sesión corporal; si busca piel/rostro → facial). "
            "Si no lo dijo, recomiéndele con calidez la que mejor le acomoda.\n"
            "2) ANTES de proponer u ofrecer CUALQUIER hora, usa SIEMPRE "
            "`consultar_disponibilidad` de ese día y ofrécele 1-2 horarios ÚNICAMENTE de los que "
            "la herramienta marque libres. NUNCA ofrezcas, confirmes ni des por buena una hora sin "
            "haberla verificado ahí: así no se empalman las citas.\n"
            "3) Cuando ella acepte una hora concreta (de las libres), llama `agendar_cita` (en "
            "'servicio' pon lo que va a vivir: p. ej. 'Sesión corporal sin costo', 'Sesión facial "
            "sin costo' o el tratamiento/paquete que pidió).\n"
            "4) Al confirmarse, dígale con calidez que su espacio quedó apartado (por confirmar) y "
            "que el equipo le confirma por aquí. La sesión dura ~60 min.\n"
            "No inventes horarios: siempre revisa con la herramienta. Si la herramienta te dice "
            "que ese horario ya está ocupado, ofrece otra hora; si te dice que la clienta YA tenía "
            "esa cita, NO agendes de nuevo, solo confírmesela. Si la herramienta falla, toma la "
            "solicitud y dígale que el equipo confirma.\n"
            "Para CANCELAR: usa `consultar_mis_citas` para ver sus citas; si tiene una sola, "
            "confirma con ella que esa quiere cancelar y usa `cancelar_cita`; si tiene varias, "
            "pregúntele cuál. Nunca canceles sin confirmar. Los evento_id son internos: no se "
            "los muestres. Para REAGENDAR: cancela la anterior y agenda la nueva.\n"
            "Si la clienta declina con amabilidad (dice que no por ahora) y ya te despediste "
            "dejando la puerta abierta, usa `cerrar_conversacion` para apagar su seguimiento; no "
            "le volveremos a escribir hasta que ella escriba."
        )
    return (
        "\n\n## Agenda\n"
        "La agenda automática no está disponible ahora: háblale de USTED, toma la solicitud "
        "(nombre, qué le gustaría —sesión corporal o facial sin costo, o el tratamiento que pida—, "
        "día y hora que le acomoden) y dígale que una compañera del equipo le confirma la "
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
                return f"El {fecha.isoformat()} es domingo: el spa está cerrado. Ofrece otro día."
            dur = int(args.get("duracion_min") or 60)
            libres = await asyncio.to_thread(calendar_tool.slots_libres, fecha, dur)
            ap, ci = hd
            horario = f"Horario ese día: {ap.strftime('%H:%M')}-{ci.strftime('%H:%M')}. "
            if not libres:
                return (
                    horario + "Ese día YA NO tiene huecos libres (todo ocupado o ya pasó la hora). "
                    "Discúlpate con calidez y ofrécele otro día."
                )
            muestra = ", ".join(libres[:6])
            return (
                horario + f"HORARIOS LIBRES: {muestra}. Ofrécele 1-2 SOLO de estos (nunca una hora "
                "que no esté en esta lista). Cuando elija, agenda con agendar_cita."
            )

        if nombre == "agendar_cita":
            inicio = datetime.fromisoformat(args["fecha_hora"])
            res = await asyncio.to_thread(
                calendar_tool.crear_cita,
                args.get("nombre", "").strip() or "Clienta",
                telefono or "",
                args.get("servicio", "").strip() or "Tratamiento",
                inicio,
                int(args.get("duracion_min") or 60),
                args.get("notas", ""),
            )
            if res.get("ok"):
                # Ya cerró: apagar el seguimiento y guardar sus datos.
                try:
                    await memory.marcar_lead_agendado(telefono)
                    await memory.actualizar_lead(
                        telefono,
                        nombre=args.get("nombre", "").strip(),
                        interes=args.get("servicio", "").strip(),
                    )
                except Exception:  # noqa: BLE001
                    pass
                if res.get("duplicada"):
                    return (
                        f"OJO: la clienta YA TENÍA una cita para {res['cuando']} (NO se creó otra, "
                        "para no duplicar). Confírmale con calidez que su espacio para ese día/hora "
                        "sigue apartado; NO digas que acabas de agendar de nuevo ni intentes agendar "
                        "otra vez ese mismo horario."
                    )
                return (
                    f"CITA CREADA para {res['cuando']} (queda por confirmar). "
                    "Confírmale a la clienta con calidez que ya apartaste su espacio y que "
                    "el equipo le confirma por aquí."
                )
            return (
                f"NO se pudo agendar: {res.get('error', 'error desconocido')}. "
                "Ofrécele otra hora o pasar con el equipo, sin alarmarla."
            )

        if nombre == "consultar_mis_citas":
            citas = await asyncio.to_thread(calendar_tool.buscar_citas_de, telefono or "")
            if not citas:
                return (
                    "La clienta no tiene citas próximas registradas a su WhatsApp. "
                    "Si cree que sí tiene una, ofrece pasar con el equipo para revisarlo."
                )
            líneas = [
                f"- evento_id={c['evento_id']} | {c['servicio']} | {c['cuando']}"
                for c in citas
            ]
            return (
                "Citas próximas de esta clienta (NO le muestres los evento_id, son internos):\n"
                + "\n".join(líneas)
            )

        if nombre == "cancelar_cita":
            res = await asyncio.to_thread(
                calendar_tool.cancelar_cita, (args.get("evento_id") or "").strip(), telefono or ""
            )
            if res.get("ok"):
                cuando = res.get("cuando", "")
                return (
                    f"CITA CANCELADA{(' (' + cuando + ')') if cuando else ''}. "
                    "Confírmale con calidez que su cita quedó cancelada y ofrécele reagendar "
                    "cuando quiera."
                )
            return (
                f"NO se pudo cancelar: {res.get('error', 'error desconocido')}. "
                "Discúlpate con calidez y ofrece pasar con el equipo."
            )

        if nombre == "cerrar_conversacion":
            try:
                await memory.pausar_seguimiento(telefono or "")
            except Exception:  # noqa: BLE001
                pass
            logger.info(f"Seguimiento apagado (clienta declinó con amabilidad): {telefono}")
            return (
                "Listo: se apagó el seguimiento de esta clienta (no le escribiremos "
                "proactivamente hasta que ella escriba). Despídete con calidez dejando la puerta "
                "abierta y NO insistas más."
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
            _marcar_cerebro(True)
            return texto, True

        logger.error("Se agotaron las vueltas de tool use sin respuesta final")
        return obtener_mensaje_error(), False

    except Exception as e:  # noqa: BLE001
        logger.error(f"Error llamando a Claude: {e}")
        _marcar_cerebro(False, str(e))
        return obtener_mensaje_error(), False


NO_ENVIAR = "NO_ENVIAR"


async def generar_recordatorio_equipo(historial: list[dict]) -> str | None:
    """
    Recordatorio LIGERO en una charla que lleva el EQUIPO humano: la clienta no contestó el
    último mensaje del equipo en unas horas. Sofía retoma justo eso, en el tono del equipo.
    Devuelve el texto, NO_ENVIAR si la charla ya terminó de forma natural, o None si falló.
    """
    if not historial or historial[-1]["role"] != "assistant":
        return NO_ENVIAR  # no hay un mensaje del equipo esperando respuesta
    system_prompt = cargar_system_prompt() + _contexto_temporal()
    instruccion = (
        "[NOTA INTERNA — no la menciones: esta charla la lleva una persona del EQUIPO de 2Body. "
        "La clienta no ha contestado el ÚLTIMO mensaje del equipo desde hace unas horas. Escribe "
        "UN recordatorio LIGERO, de UNA sola línea, que retome justo lo último que el equipo le "
        "preguntó u ofreció (p. ej. los horarios propuestos), en el MISMO tono y trato que usa el "
        "equipo en el chat: si el equipo le habla de TÚ, háblale de TÚ (esto manda sobre tu regla "
        "de usted). Si un horario que ofreció el equipo YA PASÓ (revisa la fecha y hora actuales), "
        "no lo repitas: ofrécele con suavidad compartirle horarios nuevos. Nada de información "
        "nueva, precios ni presión, no la "
        "regañes por no contestar, máximo 1 emoji. Si el último mensaje del equipo le hizo una "
        "pregunta o propuesta (agendar, horarios, rostro o cuerpo, etc.) que ella no contestó, SÍ "
        "escribe el recordatorio. Responde EXACTAMENTE NO_ENVIAR solo si la charla ya cerró: ya "
        "agendó, se despidieron, ella dijo que lo piensa o que avisa, o pidió no escribirle]"
    )
    mensajes = [{"role": m["role"], "content": m["content"]} for m in historial]
    mensajes.append({"role": "user", "content": instruccion})
    try:
        r = await client.messages.create(
            model=MODELO, max_tokens=600, system=system_prompt, messages=mensajes
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error generando recordatorio del equipo: {e}")
        _marcar_cerebro(False, str(e))
        return None
    texto = _extraer_texto(r)
    if not texto:
        return None
    return NO_ENVIAR if NO_ENVIAR in texto else texto


async def generar_seguimiento(historial: list[dict]) -> str | None:
    """
    Redacta UN mensaje de reenganche cálido (estilo concierge) para una clienta que dejó la
    charla a medias, usando la personalidad de asesora + el historial. Devuelve el texto o None.
    """
    if not historial:
        return None
    system_prompt = cargar_system_prompt() + _contexto_temporal()
    instruccion = (
        "[NOTA INTERNA — no la menciones: la clienta dejó la charla a medias y aún no agenda. "
        "Háblale de USTED. Escríbele UN SOLO mensaje corto, cálido y con clase, tipo recordatorio "
        "de VALOR, NO una encuesta. Dale una razón concreta y amable (el resultado/beneficio de lo "
        "que le interesó) e invítala con suavidad a vivir su primera sesión SIN COSTO (corporal o "
        "facial) cuando guste, dejando la puerta abierta. PROHIBIDO volver a preguntarle qué le "
        "preocupa o qué busca (nada de '¿sigue siendo X lo que más le preocupa?': eso cansa). Lee "
        "todo el chat (incluye lo que dijo el equipo): no repitas lo ya dicho, no la regañes por no "
        "contestar y no la satures. Suena natural, sin presión. Máx 2 renglones, 1 emoji.]"
    )
    mensajes = [{"role": m["role"], "content": m["content"]} for m in historial]
    mensajes.append({"role": "user", "content": instruccion})
    try:
        r = await client.messages.create(
            model=MODELO, max_tokens=600, system=system_prompt, messages=mensajes
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error generando seguimiento: {e}")
        _marcar_cerebro(False, str(e))
        return None
    texto = _extraer_texto(r)
    return texto or None
