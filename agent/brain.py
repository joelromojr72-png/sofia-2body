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
import re
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
            "Ofrécele solo horarios que esta herramienta marque libres. Pásale SIEMPRE el "
            "servicio tal como lo vas a agendar (con 'sin costo' si lo es): las citas de OTRO "
            "servicio a la misma hora no estorban (cada servicio tiene su cabina), así salen más "
            "horarios; pero dos sesiones sin costo no coinciden (salvo el sábado, si son de servicio distinto)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fecha": {
                    "type": "string",
                    "description": "Dia a consultar en formato YYYY-MM-DD.",
                },
                "servicio": {
                    "type": "string",
                    "description": "Servicio que quiere la clienta (p. ej. 'facial', 'corporal', "
                    "'depilación', 'masaje', 'EMSculpt'). Si aún no lo sabes, déjalo vacío.",
                },
                "duracion_min": {
                    "type": "integer",
                    "description": "Duración de la cita en minutos: 30 si es la cita sin costo "
                    "(valoración o primera sesión); si no sabes, usa 60.",
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
                    "description": "Duracion en minutos. La cita sin costo siempre dura 30. Si no "
                    "sabes, usa 60.",
                },
                "reemplaza_evento_id": {
                    "type": "string",
                    "description": "SOLO para REAGENDAR: el evento_id (de consultar_mis_citas) de la "
                    "cita que se cambia. La vieja se quita sola hasta que la nueva quedó apartada; "
                    "NO la canceles antes.",
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


def _a_fecha(valor) -> date | None:
    """Las fechas de prompts.yaml llegan como date (sin comillas) o como texto."""
    if isinstance(valor, date):
        return valor
    try:
        return date.fromisoformat(str(valor))
    except ValueError:
        return None


def _promociones() -> list[dict]:
    return cargar_config_prompts().get("promociones") or []


def _bloque_promociones() -> str:
    """
    Las promos que el modelo puede ver HOY, calculadas por código desde prompts.yaml.

    Una promo vencida no le llega con sus precios: un aviso en el prompt tipo "si ya pasó el
    31, no la ofrezcas" no bastó (el 3 de noviembre seguía ofreciendo la de octubre).
    """
    hoy = datetime.now(TZ).date()
    vigentes, proximas, terminadas = [], [], []
    for p in _promociones():
        desde, hasta = _a_fecha(p.get("desde")), _a_fecha(p.get("hasta"))
        if not desde or not hasta:
            logger.error(f"Promo con fechas inválidas en prompts.yaml: {p.get('nombre')}")
            continue
        fin = f"{hasta.day} de {_MESES[hasta.month - 1]}"
        rango = f"del {desde.day} de {_MESES[desde.month - 1]} al {fin}"
        contenido = (p.get("contenido") or "").strip()
        if hasta < hoy:
            terminadas.append(p.get("nombre", ""))
        elif desde <= hoy:
            vigentes.append(f"### {p.get('nombre')} — VIGENTE, termina el {fin}\n{contenido}")
        elif (_a_fecha(p.get("avisar_desde")) or desde) <= hoy:
            proximas.append(
                f"### {p.get('nombre')} — TODAVÍA NO EMPIEZA, será {rango}\n{contenido}\n"
                "Menciónela solo si pregunta por promociones, descuentos o Buen Fin. Entonces dele con "
                "gusto las fechas y los precios, y aclare que esos "
                "precios solo aplican esos días; hoy rigen los precios de lista. Puede apartar ya "
                "su valoración sin costo para aprovecharla."
            )
    partes = ["\n\n## Promociones (calculado con la fecha de hoy; esto manda)"]
    partes += vigentes or ["HOY NO HAY NINGUNA PROMOCIÓN VIGENTE: usa la lista de precios y el plan de pagos."]
    partes += proximas
    if terminadas:
        partes.append(
            "YA TERMINARON y NO EXISTEN: " + "; ".join(terminadas) + ". Si la clienta o un mensaje "
            "anterior del chat las menciona, aclárele con amabilidad que esa promo ya terminó y "
            "ofrézcale lo vigente o la lista de precios. Nunca repitas sus precios ni sus fechas."
        )
    return "\n\n".join(partes)


def _frase_vencida(texto: str) -> str | None:
    """Última barrera: devuelve lo que `texto` menciona de una promo que ya terminó, si algo."""
    hoy = datetime.now(TZ).date()
    for p in _promociones():
        hasta = _a_fecha(p.get("hasta"))
        if not hasta or hasta >= hoy:
            continue
        for patron in p.get("frases_vencida") or []:
            m = re.search(patron, texto, re.IGNORECASE)
            if m:
                return m.group(0)
    return None


_SALUDO_GENERAL = re.compile(r"^\W*(hola\W+)?(quiero|m[aá]s|me das?|me dan?)\W*(m[aá]s\W+)?informaci[oó]n\b", re.I)
_NOTA_ANUNCIO_GENERAL = (
    "\n\n[NOTA INTERNA — no la menciones: viene del anuncio de su primera sesión SIN COSTO. Usa "
    "consultar_disponibilidad y en UN solo mensaje salúdala, dile que su primera sesión es SIN COSTO, "
    "ofrécele dos horarios y pregúntale si la quiere para rostro o para cuerpo.]"
)

_NOTA_ANUNCIO_BUEN_FIN = (
    "\n\n[NOTA INTERNA — no la menciones: viene del anuncio «{titulo}», que promete precios especiales "
    "de Buen Fin y agendar valoración, NO una sesión gratis: no le ofrezcas primera sesión sin costo. "
    "Si ya preguntó algo concreto, contéstalo primero. Usa consultar_disponibilidad y en UN solo "
    "mensaje salúdala, dile que los precios de Buen Fin son del 13 al 17 de noviembre, que su "
    "valoración es sin costo y ofrécele dos horarios. Si el título ya dice qué le interesa, no le "
    "preguntes rostro o cuerpo.]"
)


def _buen_fin_terminado() -> bool:
    hoy = datetime.now(TZ).date()
    for p in _promociones():
        if "buen fin" in (p.get("nombre") or "").lower():
            hasta = _a_fecha(p.get("hasta"))
            return bool(hasta and hasta < hoy)
    return True


async def _nota_primer_mensaje(mensaje: str, telefono: str) -> str:
    """Nota interna del primer mensaje según el anuncio del que viene. No se guarda en el historial."""
    titulo = ""
    if telefono:
        try:
            titulo = await memory.titulo_anuncio(telefono)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"No se pudo leer el anuncio de origen: {e}")
    if "buen fin" in titulo.lower():
        # Su texto es el mismo "Quiero más información" de los anuncios de sesión sin costo.
        return "" if _buen_fin_terminado() else _NOTA_ANUNCIO_BUEN_FIN.format(titulo=titulo)
    return _NOTA_ANUNCIO_GENERAL if _SALUDO_GENERAL.match(mensaje.strip()) else ""


def _prompt_sin_agenda() -> str:
    """System prompt de los mensajes proactivos (recordatorio y seguimiento)."""
    return cargar_system_prompt() + _contexto_temporal() + _bloque_promociones()


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
            "`consultar_disponibilidad` de ese día (con el 'servicio' que quiere, si ya lo sabes) y "
            "ofrécele 1-2 horarios ÚNICAMENTE de los que la herramienta marque libres. NUNCA "
            "ofrezcas, confirmes ni des por buena una hora sin haberla verificado ahí. Hay UNA "
            "sola cabina por servicio (p. ej. una de facial): a la misma hora pueden coincidir un "
            "facial y un corporal, pero NUNCA dos faciales (ni dos del mismo servicio) ni dos "
            "sesiones sin costo o valoraciones, aunque una sea facial y la otra corporal (el sábado sí pueden coincidir una facial y una corporal sin costo, nunca dos del mismo servicio). Si el "
            "horario que pide está lleno para ese servicio, ofrécele otro horario o, si le "
            "interesa, otro servicio en esa hora.\n"
            "REGLA: a cada persona se le regala UNA SOLA sesión sin costo en total (facial O "
            "corporal, no ambas). Si ya tuvo o ya tiene apartada su sesión gratis, no le ofrezcas "
            "otra: ofrécele con calidez el tratamiento o paquete con su precio. Si `agendar_cita` "
            "te dice que ya la tuvo, explícaselo así de amable.\n"
            "3) Cuando ella acepte una hora concreta (de las libres), llama `agendar_cita` (en "
            "'servicio' pon lo que va a vivir: p. ej. 'Sesión corporal sin costo', 'Sesión facial "
            "sin costo' o el tratamiento/paquete que pidió).\n"
            "(Si viene por una VALORACIÓN, en 'servicio' pon p. ej. 'Valoración facial sin costo'; "
            "cuenta como su única cita sin costo.)\n"
            "4) Al confirmarse, dígale con calidez que su espacio quedó apartado (por confirmar) y "
            "que el equipo le confirma por aquí. La cita sin costo dura unos 30 minutos.\n"
            "No inventes horarios: siempre revisa con la herramienta. Si la herramienta te dice "
            "que ese horario ya está ocupado, ofrece otra hora; si te dice que la clienta YA tenía "
            "esa cita, NO agendes de nuevo, solo confírmesela. Si la herramienta falla, toma la "
            "solicitud y dígale que el equipo confirma.\n"
            "LEE TODO EL CHAT antes de contestar: lo que ya dijo la clienta (nombre, servicio, "
            "día/hora), lo que TÚ ya ofreciste y lo que prometió el EQUIPO (los mensajes que "
            "empiezan con [Equipo] los escribió una persona del spa: respétalos, no los "
            "contradigas, no cambies lo que el equipo acordó y no repitas lo que ya se le dijo). "
            "No vuelvas a preguntar lo que ya contestó ni le ofrezcas dos veces lo mismo. Si "
            "pasaron horas desde tu última oferta (verás [escrito ... h después]), vuelve a "
            "consultar ANTES de ofrecer o aceptar una hora, nunca después de que ella eligió.\n"
            "Si la clienta quiere apartar o pide su cita/promo, usa `consultar_disponibilidad` "
            "YA y en ese mismo mensaje ofrécele 2 horarios concretos (no esperes a tener su "
            "nombre; el nombre se pide cuando elija).\n"
            "OFERTA = COMPROMISO: ofrece SOLO horas que `consultar_disponibilidad` te acaba de dar "
            "(consulta con el MISMO servicio con el que vas a agendar) y, cuando elija una de esas, "
            "agéndala directo con `agendar_cita` sin volver a consultar. No escribas \"listo\" ni "
            "\"le aparté\" antes de que la herramienta lo confirme.\n"
            "CONFIRMACIONES (MUY IMPORTANTE): cuando la clienta responde \"confirmado\", \"sí\", "
            "\"ok\", \"✅\" o similar a la cita que acabas de apartar o a un RECORDATORIO de su cita, "
            "SOLO le agradeces y le confirmas que su cita sigue en pie (mismo día y hora). NO "
            "vuelvas a consultar disponibilidad, NO la muevas, NO canceles y NUNCA le digas que "
            "\"ya no está disponible\" o que hubo un \"percance\": su cita YA es suya. Solo se "
            "cambia una cita cuando ELLA pide expresamente otro día u otra hora. Si dudas de qué "
            "cita confirma, usa `consultar_mis_citas` (solo para leer).\n"
            "Para CANCELAR: usa `consultar_mis_citas` para ver sus citas; si tiene una sola, "
            "confirma con ella que esa quiere cancelar y usa `cancelar_cita`; si tiene varias, "
            "pregúntele cuál. Nunca canceles sin confirmar. Los evento_id son internos: no se "
            "los muestres.\n"
            "Para REAGENDAR (cambiar su cita de día u hora): 1) `consultar_mis_citas`; 2) lee el chat: "
            "si el equipo ya le movió o le confirmó algo, respeta eso y no lo cambies; 3) "
            "`consultar_disponibilidad` del día nuevo con el mismo servicio; 4) cuando ella elija, "
            "`agendar_cita` con `reemplaza_evento_id` = la cita vieja. NUNCA canceles primero para "
            "luego agendar: la vieja se quita sola cuando la nueva ya quedó. Solo puedes mover o "
            "cancelar citas que tú apartaste y siguen por confirmar; si la herramienta dice que es "
            "del EQUIPO, no la toques: dile que le pasas su solicitud al equipo para que le "
            "confirmen. Nunca le digas que ya quedó cambiada si la herramienta no lo confirmó.\n"
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


# Servicio de PAGA dicho explícitamente (si no, al consultar se usa la regla de la sesión sin costo).
_DE_PAGA = re.compile(r"con\s+costo|pagad|paquete|\d+\s*(a|ª|da|ra|ta|va|na)?\s*sesi[oó]n|tratamiento|promo|plan", re.I)


def servicio_para_consultar(servicio: str) -> str:
    """Servicio con el que se consulta disponibilidad: el estricto (sin costo) salvo que sea de paga."""
    s = (servicio or "").strip()
    if not s:
        return s  # sin servicio: cualquier cita ocupa el horario (lo más estricto)
    if calendar_tool.es_sesion_gratis(s) or _DE_PAGA.search(s):
        return s
    return f"{s} sin costo"


# Horarios que se le OFRECIERON a cada clienta (para no ofrecer de más ni contradecirse).
_ofrecidos: dict[str, dict[str, list[str]]] = {}


def _recordar_ofrecidos(telefono: str, fecha, libres: list[str]) -> None:
    if telefono:
        _ofrecidos.setdefault(telefono, {})[fecha.isoformat()] = list(libres)


# Frases que contradicen una cita ya apartada (no se le dicen a quien solo confirma).
_CONTRADICE = re.compile(
    r"ya no (est[aá]|tengo|hay|qued[oóa]|aplica)|se (acaba de )?ocup[oó]|percance|disculpe la confusi|"
    r"no (est[aá]|qued[oó]) (disponible|libre)|ya se ocup",
    re.I,
)
# Marcas internas del historial que el modelo podría copiar en su respuesta.
_QUITAR_MARCAS = re.compile(r"^\s*\[(Equipo|escrito[^\]]*)\]\s*", re.I | re.M)


# Mensaje que SOLO confirma ("sí", "confirmado ✅", "ok"): con eso nunca se mueve ni se cancela
# una cita. Pasó el 9/10-nov: la clienta decía "Confirmado" y Sofía le cambiaba la hora.
_SOLO_CONFIRMA = re.compile(
    r"^[\s¡!.,]*(s[ií]+|sip|confirmad[oa]s?|confirmo|ok(ay)?|va|vale|listo|perfecto|claro( que s[ií])?|"
    r"de acuerdo|correcto|ah[ií] estar[eé]|all[aá] nos vemos|gracias)?[\s!.,💜✅👍🙏🌸😊☺️❤️]*$",
    re.I,
)


def solo_confirma(mensaje: str) -> bool:
    m = (mensaje or "").strip()
    return bool(m) and bool(_SOLO_CONFIRMA.match(m))


async def _ejecutar_herramienta(nombre: str, args: dict, telefono: str, mensaje: str = "") -> str:
    """Corre una herramienta y devuelve un texto para el bloque tool_result."""
    if solo_confirma(mensaje) and (
        nombre == "cancelar_cita"
        or (nombre == "agendar_cita" and (args.get("reemplaza_evento_id") or "").strip())
    ):
        logger.info(f"Candado: la clienta solo confirmó ({mensaje!r}); no se mueve/cancela su cita")
        return (
            "NO se hizo nada: la clienta SOLO está confirmando su cita. Su cita sigue apartada "
            "igual (mismo día y hora). Agradécele y confírmale con calidez; no le ofrezcas otro "
            "horario ni le digas que ya no hay espacio."
        )
    try:
        if nombre == "consultar_disponibilidad":
            fecha = date.fromisoformat(args["fecha"])
            hd = calendar_tool.horario_dia(fecha)
            if hd is None:
                return f"El {fecha.isoformat()} es domingo: el spa está cerrado. Ofrece otro día."
            servicio_c = (args.get("servicio") or "").strip()
            # Lo que se OFRECE debe poder AGENDARSE después. Antes se consultaba "facial" y se
            # agendaba "Sesión facial sin costo" (regla más estricta) → "ya no está disponible".
            # Por eso: si no dice claramente que es de PAGA, se consulta con la regla de la
            # sesión sin costo (la más estricta) y con la misma duración que usará agendar_cita.
            # Duración: 30 min SOLO si ya se sabe que es la sesión sin costo; si no se sabe, 60
            # (lo más largo que podría agendarse), para que la hora ofrecida siempre quepa.
            dur = 30 if calendar_tool.es_sesion_gratis(servicio_c) else max(60, int(args.get("duracion_min") or 60))
            servicio_c = servicio_para_consultar(servicio_c)
            libres = await asyncio.to_thread(
                calendar_tool.slots_libres, fecha, dur, 60, 8, servicio_c, telefono or "",
            )
            _recordar_ofrecidos(telefono, fecha, libres)
            ap, ci = hd
            horario = f"Horario ese día: {ap.strftime('%H:%M')}-{ci.strftime('%H:%M')}. "
            # Si la clienta YA tiene cita ese día, se le dice al modelo: esa hora es SUYA.
            try:
                suyas = [c for c in await asyncio.to_thread(calendar_tool.buscar_citas_de, telefono or "", 30)
                         if c.get("fecha") == fecha.isoformat()]
            except Exception:  # noqa: BLE001
                suyas = []
            if suyas:
                horario = (
                    "OJO: esta clienta YA TIENE cita ese día ("
                    + "; ".join(f"{c['servicio']} {c['cuando']}" for c in suyas)
                    + "). Ese horario es SUYO y sigue apartado: si solo está confirmando, NO se lo "
                    "cambies ni le digas que ya no hay. " + horario
                )
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
            servicio = args.get("servicio", "").strip() or "Tratamiento"
            # La cita sin costo dura 30 min aunque el modelo pida otra cosa: bloquea la agenda.
            duracion = 30 if calendar_tool.es_sesion_gratis(servicio) else int(args.get("duracion_min") or 60)
            res = await asyncio.to_thread(
                calendar_tool.crear_cita,
                args.get("nombre", "").strip() or "Clienta",
                telefono or "",
                servicio,
                inicio,
                duracion,
                args.get("notas", ""),
                (args.get("reemplaza_evento_id") or "").strip(),
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
            if res.get("equipo"):
                return f"NO se movió nada: {res['error']}"
            # Para no decir "sí hay" y luego "no hay": los huecos reales de ese día, ya con este servicio.
            try:
                libres = await asyncio.to_thread(
                    calendar_tool.slots_libres, inicio.date(), duracion, 60, 6, servicio, telefono or ""
                )
            except Exception:  # noqa: BLE001
                libres = []
            reales = (f" Horarios que SÍ están libres ese día para este servicio: {', '.join(libres)}."
                      if libres else " Ese día ya no queda ningún horario libre para este servicio.")
            logger.warning(f"Agendar rechazado ({telefono}): {servicio} {inicio.isoformat()} → {res.get('error')}")
            return (
                f"NO se pudo agendar: {res.get('error', 'error desconocido')}.{reales} "
                "Ofrécele SOLO de esos (u otro día consultando de nuevo); no le vuelvas a ofrecer "
                "la hora rechazada ni una que no esté en la lista, y no le des vueltas: una sola "
                "disculpa breve y la alternativa."
            )

        if nombre == "consultar_mis_citas":
            try:
                nombres = await memory.nombres_de(telefono or "")
            except Exception:  # noqa: BLE001
                nombres = []
            citas = await asyncio.to_thread(calendar_tool.buscar_citas_de, telefono or "", 90, nombres)
            if not citas:
                return (
                    "La clienta no tiene citas próximas registradas a su WhatsApp. "
                    "Si cree que sí tiene una, ofrece pasar con el equipo para revisarlo."
                )
            líneas = [
                f"- evento_id={c['evento_id']} | {c['servicio']} | {c['cuando']}"
                + ("" if c.get("de_sofia") else " | LA AGENDÓ O CONFIRMÓ EL EQUIPO: no la canceles ni la muevas")
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
            if res.get("equipo"):
                return f"NO se canceló nada: {res['error']}"
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
    # Lo que no cambia va primero y se marca para el caché de Anthropic: cada mensaje lleva unos
    # 13 mil tokens de prompt. La fecha y las promos van después, porque cambian.
    system_prompt = [
        {
            "type": "text",
            "text": cargar_system_prompt() + _instrucciones_agenda(hay_agenda),
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": _contexto_temporal() + _bloque_promociones()},
    ]
    herramientas = HERRAMIENTAS if hay_agenda else []

    mensaje_original = mensaje
    mensajes = [{"role": m["role"], "content": m["content"]} for m in historial]
    # Si la clienta vuelve a escribir horas después, se le dice a Sofía cuándo (lo ofrecido antes
    # pudo ocuparse: hay que volver a revisar ANTES de ofrecer, no después de que elija).
    ult_ts = historial[-1].get("ts") if historial else None
    if ult_ts is not None:
        try:
            from datetime import timezone as _tz
            if ult_ts.tzinfo is None:
                ult_ts = ult_ts.replace(tzinfo=_tz.utc)
            horas = (datetime.now(TZ) - ult_ts).total_seconds() / 3600
            if horas > 3:
                mensaje = f"[escrito {int(horas)} h después del mensaje anterior] " + mensaje
        except Exception:  # noqa: BLE001
            pass
    # Es el saludo con más volumen de los anuncios y solo con la regla del prompt, a veces contesta
    # con una pregunta abierta (3 de 5 en pruebas). La nota no se guarda en el historial.
    nota = await _nota_primer_mensaje(mensaje, telefono) if not historial else ""
    mensajes.append({"role": "user", "content": mensaje + nota})

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

    corregida = False
    corregida_conf = False
    accion_agenda = False
    # Lo que escribe ANTES de usar una herramienta (el saludo, casi siempre) también es para la
    # clienta: si solo se manda el último turno, le llega "Tengo mañana a las 11" sin contexto.
    previos: list[str] = []
    try:
        for _ in range(MAX_VUELTAS_TOOLS):
            respuesta = await _crear()

            if respuesta.stop_reason == "tool_use":
                # Guardamos el turno del modelo (con los bloques tool_use) y respondemos
                # cada herramienta con su tool_result, para que Claude siga con el resultado.
                mensajes.append({"role": "assistant", "content": respuesta.content})
                if _extraer_texto(respuesta):
                    previos.append(_extraer_texto(respuesta))
                resultados = []
                for bloque in respuesta.content:
                    if bloque.type == "tool_use":
                        if bloque.name in ("agendar_cita", "cancelar_cita"):
                            # Lo que escribió ANTES de agendar/cancelar ("¡Listo, le aparto las 2!")
                            # no se manda: si la herramienta falla, la clienta recibiría un "sí" y
                            # luego un "ya no hay". El mensaje final ya dice el resultado real.
                            previos.clear()
                            accion_agenda = True
                        salida = await _ejecutar_herramienta(
                            bloque.name, bloque.input or {}, telefono, mensaje_original
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
            if accion_agenda:
                previos.clear()
            texto = "\n\n".join(previos + [texto])
            texto = _QUITAR_MARCAS.sub("", texto).strip()
            # Auditoría final: a quien SOLO confirma nunca se le dice que su hora ya no está.
            if solo_confirma(mensaje_original) and _CONTRADICE.search(texto):
                logger.warning(f"Respuesta contradictoria a una confirmación (…{telefono[-4:]}): {texto[:120]}")
                if corregida_conf:
                    return obtener_mensaje_fallback(), False  # mejor que lo vea el equipo
                corregida_conf = True
                previos.clear()
                mensajes.append({"role": "assistant", "content": texto})
                mensajes.append({"role": "user", "content": (
                    "[NOTA INTERNA — no la menciones: la clienta SOLO confirmó su cita. Tu mensaje le "
                    "decía que su hora ya no está o le cambiaba el horario, y NO se envió. Su cita "
                    "sigue igual: reescríbelo agradeciéndole y confirmándole su mismo día y hora.]"
                )})
                continue
            vencida = _frase_vencida(texto)
            if vencida:
                logger.warning(f"Respuesta con promo vencida ('{vencida}') para …{telefono[-4:]}")
                if corregida:
                    # Mejor callar (lo atiende el equipo) que prometer un precio que ya no existe.
                    return obtener_mensaje_fallback(), False
                corregida = True
                previos.clear()
                mensajes.append({"role": "assistant", "content": texto})
                mensajes.append({"role": "user", "content": (
                    f"[NOTA INTERNA — no la menciones: tu mensaje dice '{vencida}', de una promo que "
                    "YA TERMINÓ, y no se envió. Reescríbelo sin mencionar ese precio ni esa fecha.]"
                )})
                continue
            logger.info(
                f"Respuesta generada con {MODELO} "
                f"({respuesta.usage.input_tokens} in / {respuesta.usage.output_tokens} out, "
                f"caché {getattr(respuesta.usage, 'cache_read_input_tokens', 0) or 0} leídos)"
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
    system_prompt = _prompt_sin_agenda()
    instruccion = (
        "[NOTA INTERNA — no la menciones: esta charla la lleva una persona del EQUIPO de 2Body. "
        "La clienta no ha contestado el ÚLTIMO mensaje del equipo desde hace unas horas. Escribe "
        "UN recordatorio LIGERO, de UNA sola línea, que retome justo lo último que el equipo le "
        "preguntó u ofreció (p. ej. los horarios propuestos), en el MISMO tono y trato que usa el "
        "equipo en el chat: si el equipo le habla de TÚ, háblale de TÚ (esto manda sobre tu regla "
        "de usted). Si un horario que ofreció el equipo YA PASÓ (revisa la fecha y hora actuales), "
        "no lo repitas: ofrécele con suavidad compartirle horarios nuevos. Nada de información "
        "nueva, precios ni presión, no la "
        "regañes por no contestar, máximo 1 emoji. Por defecto SÍ escribes el recordatorio: si "
        "el equipo le ofreció agendar (aunque sea con 'nos encantaría agendarte…'), le mandó una "
        "promo, le propuso horarios o le hizo cualquier pregunta que ella no contestó, retómalo. "
        "Responde EXACTAMENTE NO_ENVIAR SOLO si: ya tiene cita confirmada, ella dijo que lo piensa "
        "o que ella avisa, pidió no escribirle, o la charla ya cerró con despedida de las dos partes]"
    )
    mensaje_original = mensaje
    mensajes = [{"role": m["role"], "content": m["content"]} for m in historial]
    # Si la clienta vuelve a escribir horas después, se le dice a Sofía cuándo (lo ofrecido antes
    # pudo ocuparse: hay que volver a revisar ANTES de ofrecer, no después de que elija).
    ult_ts = historial[-1].get("ts") if historial else None
    if ult_ts is not None:
        try:
            from datetime import timezone as _tz
            if ult_ts.tzinfo is None:
                ult_ts = ult_ts.replace(tzinfo=_tz.utc)
            horas = (datetime.now(TZ) - ult_ts).total_seconds() / 3600
            if horas > 3:
                mensaje = f"[escrito {int(horas)} h después del mensaje anterior] " + mensaje
        except Exception:  # noqa: BLE001
            pass
    mensajes.append({"role": "user", "content": instruccion})
    try:
        r = await client.messages.create(
            model=MODELO, max_tokens=600, system=system_prompt, messages=mensajes
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error generando recordatorio del equipo: {e}")
        _marcar_cerebro(False, str(e))
        return None
    texto = _QUITAR_MARCAS.sub("", _extraer_texto(r)).strip()
    if not texto:
        return None
    return NO_ENVIAR if (NO_ENVIAR in texto or _frase_vencida(texto)) else texto


async def generar_seguimiento(historial: list[dict]) -> str | None:
    """
    Redacta UN mensaje de reenganche cálido (estilo concierge) para una clienta que dejó la
    charla a medias, usando la personalidad de asesora + el historial. Devuelve el texto o None.
    """
    if not historial:
        return None
    system_prompt = _prompt_sin_agenda()
    instruccion = (
        "[NOTA INTERNA — no la menciones: la clienta dejó la charla a medias y aún no agenda. "
        "Háblale de USTED. Escríbele UN SOLO mensaje corto, cálido y con clase, tipo recordatorio "
        "de VALOR, NO una encuesta. Dale una razón concreta y amable (el resultado/beneficio de lo "
        "que le interesó) e invítala con suavidad a lo que ya venían platicando: su plan con su "
        "valoración sin costo, o su primera sesión sin costo si así llegó y aún no la tiene; si ya "
        "la tuvo, no se la ofrezcas. Déjale la puerta abierta. Habla de lo que ofrece 2 Body; no "
        "pongas palabras en su boca ('le encantaría…'). PROHIBIDO volver a preguntarle qué le "
        "preocupa o qué busca (nada de '¿sigue siendo X lo que más le preocupa?': eso cansa). Lee "
        "todo el chat (incluye lo que dijo el equipo): no repitas lo ya dicho, no la regañes por no "
        "contestar y no la satures. Suena natural, sin presión. Máx 2 renglones, 1 emoji.]"
    )
    mensaje_original = mensaje
    mensajes = [{"role": m["role"], "content": m["content"]} for m in historial]
    # Si la clienta vuelve a escribir horas después, se le dice a Sofía cuándo (lo ofrecido antes
    # pudo ocuparse: hay que volver a revisar ANTES de ofrecer, no después de que elija).
    ult_ts = historial[-1].get("ts") if historial else None
    if ult_ts is not None:
        try:
            from datetime import timezone as _tz
            if ult_ts.tzinfo is None:
                ult_ts = ult_ts.replace(tzinfo=_tz.utc)
            horas = (datetime.now(TZ) - ult_ts).total_seconds() / 3600
            if horas > 3:
                mensaje = f"[escrito {int(horas)} h después del mensaje anterior] " + mensaje
        except Exception:  # noqa: BLE001
            pass
    mensajes.append({"role": "user", "content": instruccion})
    try:
        r = await client.messages.create(
            model=MODELO, max_tokens=600, system=system_prompt, messages=mensajes
        )
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error generando seguimiento: {e}")
        _marcar_cerebro(False, str(e))
        return None
    texto = _QUITAR_MARCAS.sub("", _extraer_texto(r)).strip()
    if texto and _frase_vencida(texto):
        logger.warning("Seguimiento descartado: mencionaba una promo vencida")
        return None
    return texto or None


# Cortesías que no piden respuesta. Con esto el relevo ni gasta IA ni la deja insistir.
_SOLO_CORTESIA = re.compile(
    r"^\W*$|^\W*((muchas |mil )?gracias|ok(ay|i)?|va|vale|sale|listo|perfecto|excelente|enterad[oa]|"
    r"confirmad[oa]|s[ií]|no|bien|bye|adi[oó]s|hasta luego|ya qued[oó])"
    r"(\W+((muchas |mil )?gracias|ya qued[oó]|listo|perfecto|ok|bye|adi[oó]s))*\W*$",
    re.IGNORECASE,
)


async def generar_relevo(historial: list[dict], telefono: str = "") -> str | None:
    """
    Contesta a quien escribió casi al cerrar el equipo y se quedó sin respuesta. El historial
    termina en el mensaje de la clienta. Devuelve el texto, NO_ENVIAR o None si falló.
    """
    if not historial or historial[-1]["role"] != "user":
        return NO_ENVIAR
    if _SOLO_CORTESIA.match(historial[-1]["content"].strip()):
        return NO_ENVIAR
    nota = (
        "\n\n[NOTA INTERNA — no la menciones: esto lo escribió hace rato y nadie le contestó. "
        "Contéstale normal, con una disculpa breve por la demora y sin repetir nada que ya le "
        "hayan dicho. Si su mensaje no necesita respuesta (gracias, ok, una despedida, una "
        "confirmación) o ya tiene lo que pidió, responde EXACTAMENTE NO_ENVIAR.]"
    )
    texto, real = await generar_respuesta(historial[-1]["content"] + nota, historial[:-1], telefono)
    if not real:
        return None
    return NO_ENVIAR if NO_ENVIAR in texto else texto
