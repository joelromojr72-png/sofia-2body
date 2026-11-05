# tests/test_sofia.py — Pruebas sin IA ni red: horario, promos, webhook, memoria y relevo
"""
No gastan saldo ni escriben a nadie: base SQLite temporal, envíos y cerebro simulados.

    .venv/bin/python -m unittest tests.test_sofia -v
"""

import hashlib
import hmac
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from datetime import datetime as _dt
from datetime import timedelta, timezone
from pathlib import Path
import unittest.mock
from unittest.mock import AsyncMock, MagicMock, patch

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)
_tmp = tempfile.mkdtemp(prefix="sofia_unit_")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmp}/unit.db"
os.environ["ENVIRONMENT"] = "development"
os.environ["WHATSAPP_PROVIDER"] = "meta"
os.environ["META_APP_SECRET"] = "secreto-de-prueba"

import httpx  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402

from agent import brain, calendar_tool, followup, main, memory  # noqa: E402

TZ = main.TZ_NEGOCIO
TEL = "5210000000001"


def reloj(*args):
    class Fijo(_dt):
        @classmethod
        def now(cls, tz=None):
            d = _dt(*args)
            return d.replace(tzinfo=tz) if tz else d

    return Fijo


class Horario(unittest.TestCase):
    def test_turnos(self):
        casos = [
            ((2026, 11, 4, 8, 59), True),  # miércoles antes de abrir
            ((2026, 11, 4, 9, 0), False),
            ((2026, 11, 4, 19, 59), False),
            ((2026, 11, 4, 20, 0), True),
            ((2026, 11, 7, 14, 29), False),  # sábado
            ((2026, 11, 7, 14, 30), True),
            ((2026, 11, 8, 12, 0), True),  # domingo
        ]
        for f, esperado in casos:
            self.assertEqual(main.sofia_debe_responder(_dt(*f, tzinfo=TZ)), esperado, f)

    def test_relevo_solo_al_entrar_sofia(self):
        with patch("agent.main.sofia_debe_responder", main.sofia_debe_responder):
            self.assertFalse(followup._en_horario_relevo(_dt(2026, 11, 4, 19, 0, tzinfo=TZ)))
            self.assertTrue(followup._en_horario_relevo(_dt(2026, 11, 4, 20, 15, tzinfo=TZ)))
            self.assertFalse(followup._en_horario_relevo(_dt(2026, 11, 4, 22, 5, tzinfo=TZ)))
            self.assertTrue(followup._en_horario_relevo(_dt(2026, 11, 7, 14, 45, tzinfo=TZ)))


class Promos(unittest.TestCase):
    def bloque(self, *fecha):
        with patch.object(brain, "datetime", reloj(*fecha)):
            return brain._bloque_promociones()

    def vencida(self, texto, *fecha):
        with patch.object(brain, "datetime", reloj(*fecha)):
            return brain._frase_vencida(texto)

    def test_prompt_fijo_sin_precios_de_promo(self):
        fijo = brain.cargar_system_prompt()
        for p in ("6,500", "5,999", "5,644", "4,999", "5,075", "octubre", "Otoño"):
            self.assertNotIn(p, fijo, p)

    def test_octubre_no_llega_en_noviembre(self):
        b = self.bloque(2026, 11, 4, 21, 0)
        self.assertNotIn("6,500", b)
        self.assertNotIn("5,999", b)
        self.assertIn("YA TERMINARON", b)
        self.assertIn("NO HAY NINGUNA PROMOCIÓN VIGENTE", b)

    def test_octubre_si_vigente_el_31(self):
        self.assertIn("6,500", self.bloque(2026, 10, 31, 23, 0))

    def test_buen_fin_por_fechas(self):
        antes = self.bloque(2026, 11, 12, 21, 0)
        self.assertIn("TODAVÍA NO EMPIEZA", antes)
        self.assertIn("4,999", antes)
        for dia in (13, 17):
            b = self.bloque(2026, 11, dia, 12, 0)
            self.assertIn("VIGENTE, termina el 17 de noviembre", b)
            self.assertIn("10,000", b)
        despues = self.bloque(2026, 11, 18, 0, 5)
        self.assertNotIn("4,999", despues)
        self.assertIn("Buen Fin", despues)
        self.assertIn("YA TERMINARON", despues)

    def test_barrera_de_respuesta(self):
        self.assertTrue(self.vencida("Es nuestra promo, válida hasta el 31", 2026, 11, 4, 21, 0))
        # Decir que ya terminó sí se vale: es la respuesta correcta si la clienta pregunta.
        self.assertIsNone(self.vencida("La promo de octubre ya terminó", 2026, 11, 4, 21, 0))
        self.assertTrue(self.vencida("12 sesiones por $6,500", 2026, 11, 4, 21, 0))
        self.assertIsNone(self.vencida("12 sesiones por $6,500", 2026, 10, 30, 21, 0))
        self.assertIsNone(self.vencida("Faciales 12 sesiones $4,999", 2026, 11, 14, 21, 0))
        self.assertTrue(self.vencida("Faciales 12 sesiones $4,999", 2026, 11, 18, 21, 0))
        # Precios de lista que se parecen a los de promo: no deben bloquearse.
        for t in ("$6,580", "$16,500", "$14,000", "$1,000"):
            self.assertIsNone(self.vencida(t, 2026, 11, 4, 21, 0), t)


class Bloque:
    def __init__(self, tipo, **kw):
        self.type = tipo
        self.__dict__.update(kw)


class Resp:
    def __init__(self, bloques, stop="end_turn"):
        self.content, self.stop_reason = bloques, stop
        self.usage = Bloque("u", input_tokens=1, output_tokens=1, cache_read_input_tokens=0)


class Cerebro(unittest.IsolatedAsyncioTestCase):
    async def correr(self, *respuestas):
        crear = AsyncMock(side_effect=list(respuestas))
        with patch.object(brain.client.messages, "create", crear), \
             patch.object(brain, "datetime", reloj(2026, 11, 4, 21, 0)), \
             patch.object(brain.calendar_tool, "calendario_configurado", lambda: True), \
             patch.object(brain, "_ejecutar_herramienta", AsyncMock(return_value="HORARIOS LIBRES: 11:00")):
            return (*await brain.generar_respuesta("Hola", [], TEL), crear)

    def texto(self, t):
        return Resp([Bloque("text", text=t)])

    async def test_nota_de_anuncio_solo_en_primer_mensaje_general(self):
        for msg, hist, esperado in (
            ("¡Hola! Quiero más información", [], True),
            ("Quiero más información", [], True),
            ("Más información por favor", [], True),
            ("Quiero mi plan facial", [], False),
            ("Quiero más información", [{"role": "user", "content": "Hola"}], False),
        ):
            crear = AsyncMock(return_value=self.texto("Hola 💜"))
            with patch.object(brain.client.messages, "create", crear), \
                 patch.object(brain, "datetime", reloj(2026, 11, 4, 21, 0)):
                await brain.generar_respuesta(msg, hist, TEL)
            self.assertEqual("NOTA INTERNA" in crear.await_args.kwargs["messages"][-1]["content"], esperado, msg)

    async def test_saludo_antes_de_herramienta_no_se_pierde(self):
        uso = Bloque("tool_use", id="t1", name="consultar_disponibilidad", input={"fecha": "2026-11-05"})
        t, real, _ = await self.correr(
            Resp([Bloque("text", text="¡Hola! Soy Sofía"), uso], "tool_use"), self.texto("Tengo a las 11:00")
        )
        self.assertTrue(real)
        self.assertEqual(t, "¡Hola! Soy Sofía\n\nTengo a las 11:00")

    async def test_promo_vencida_se_reescribe(self):
        t, real, crear = await self.correr(self.texto("12 sesiones por $6,500"), self.texto("Lista: $11,000"))
        self.assertEqual((t, real, crear.await_count), ("Lista: $11,000", True, 2))

    async def test_promo_vencida_dos_veces_se_calla(self):
        _, real, crear = await self.correr(self.texto("Son $6,500"), self.texto("Son $5,999"))
        self.assertFalse(real)
        self.assertEqual(crear.await_count, 2)

    async def test_prompt_cacheable_y_fecha_aparte(self):
        _, _, crear = await self.correr(self.texto("Hola 💜"))
        sistema = crear.await_args.kwargs["system"]
        self.assertEqual(sistema[0]["cache_control"], {"type": "ephemeral"})
        self.assertNotIn("Fecha y hora actual", sistema[0]["text"])
        self.assertIn("Fecha y hora actual", sistema[1]["text"])


class AnuncioYDuracion(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await memory.inicializar_db()

    async def asyncTearDown(self):
        async with memory.async_session() as session:
            await session.execute(delete(memory.OrigenAnuncio))
            await session.commit()

    async def _nota(self, tel, titulo, msg="Quiero más información"):
        if titulo:
            await memory.registrar_origen_anuncio(tel, f"ev-{tel}", {"titulo": titulo}, msg)
        return await brain._nota_primer_mensaje(msg, tel)

    async def test_anuncio_buen_fin_no_ofrece_sesion_gratis(self):
        with patch.object(brain, "datetime", reloj(2026, 11, 4, 21, 0)):
            nota = await self._nota("5210000000901", "¡Buen Fin de tratamientos corporales! 🔥")
        self.assertIn("no le ofrezcas primera sesión", nota)
        self.assertIn("13 al 17", nota)

    async def test_anuncio_buen_fin_terminado_sin_nota(self):
        with patch.object(brain, "datetime", reloj(2026, 11, 18, 21, 0)):
            self.assertEqual(await self._nota("5210000000902", "¡Buen Fin de depilación láser! 🔥"), "")

    async def test_anuncio_normal_y_sin_anuncio_siguen_igual(self):
        self.assertIn("SIN COSTO", await self._nota("5210000000903", "Tu primera sesión SIN COSTO en 2Body"))
        self.assertIn("SIN COSTO", await self._nota("5210000000904", ""))
        self.assertEqual(await self._nota("5210000000905", "", "Hacen botox?"), "")

    async def _duracion(self, servicio, pedida=None):
        crear = unittest.mock.Mock(return_value={"ok": False, "error": "x"})
        args = {"nombre": "Ana", "servicio": servicio, "fecha_hora": "2026-11-06T11:00"}
        if pedida:
            args["duracion_min"] = pedida
        with patch.object(brain.calendar_tool, "crear_cita", crear):
            await brain._ejecutar_herramienta("agendar_cita", args, TEL)
        return crear.call_args.args[4]

    async def test_cita_sin_costo_dura_30(self):
        self.assertEqual(await self._duracion("Valoración facial sin costo", 60), 30)
        self.assertEqual(await self._duracion("Sesión corporal sin costo"), 30)
        self.assertEqual(await self._duracion("Paquete facial 12 sesiones"), 60)
        self.assertEqual(await self._duracion("Masaje", 90), 90)


class UnaGratisALaVez(unittest.TestCase):
    """Dos sesiones sin costo nunca a la misma hora, aunque una sea facial y la otra corporal."""

    FECHA = date(2030, 1, 7)  # lunes

    def _evento(self, titulo, hora=10, id_="ajeno", creado="2030-01-01T00:00:00.000Z", tel="5217710000000"):
        return ("cal", {
            "id": id_, "summary": titulo, "created": creado, "status": "tentative",
            "description": f"WhatsApp: +{tel}\nServicio: {titulo}",
            "start": {"dateTime": f"2030-01-07T{hora:02d}:00:00-06:00"},
            "end": {"dateTime": f"2030-01-07T{hora + 1:02d}:00:00-06:00"},
        })

    def _crear(self, servicio, existentes, recheck=None, tel="5217719999999"):
        svc = MagicMock()
        svc.events.return_value.insert.return_value.execute.return_value = {
            "id": "nuestro", "created": "2030-01-01T00:00:01.000Z"}
        lecturas = [existentes, recheck if recheck is not None else existentes]
        with patch.object(calendar_tool, "_obtener_servicio", lambda: svc), \
             patch.object(calendar_tool, "sesion_gratis_previa", lambda *a, **k: None), \
             patch.object(calendar_tool, "_eventos_dia_todos", side_effect=lecturas):
            inicio = _dt(2030, 1, 7, 10, 0, tzinfo=calendar_tool.TZ)
            return calendar_tool.crear_cita("Ana Ruiz", tel, servicio, inicio, 30), svc

    def _libres(self, servicio, citas):
        with patch.object(calendar_tool, "citas_del_dia", lambda f: citas):
            return calendar_tool.slots_libres(self.FECHA, 30, 60, 20, servicio)

    def test_rechaza_gratis_de_otro_servicio(self):
        r, svc = self._crear("Sesión corporal sin costo", [self._evento("Lupita Exp. Única - Facial")])
        self.assertFalse(r["ok"])
        self.assertIn("sin costo", r["error"])
        svc.events.return_value.insert.assert_not_called()

    def test_valoracion_tambien_cuenta(self):
        r, _ = self._crear("Valoración facial sin costo", [self._evento("Lupita Exp. Única - Corporal")])
        self.assertFalse(r["ok"])

    def test_gratis_junto_a_pagada_de_otro_servicio_si(self):
        r, _ = self._crear("Sesión corporal sin costo", [self._evento("Tratamiento facial")])
        self.assertTrue(r["ok"])

    def test_pagada_junto_a_gratis_de_otro_servicio_si(self):
        r, _ = self._crear("Tratamiento corporal", [self._evento("Lupita Exp. Única - Facial")])
        self.assertTrue(r["ok"])

    def test_matriz_de_la_regla_de_joel(self):
        # (nueva, ya agendada, ¿se puede?)
        casos = [
            ("Sesión corporal sin costo", "Lupita Exp. Única - Facial", False),   # dos gratis, nunca
            ("Sesión facial sin costo", "Valoración facial sin costo", False),
            ("Tratamiento facial", "Lupita Exp. Única - Corporal", True),         # gratis corporal + facial de paga
            ("Sesión corporal sin costo", "Tratamiento facial", True),
            ("Tratamiento facial", "Lupita Exp. Única - Facial", False),          # misma cabina
            ("Sesión facial sin costo", "Tratamiento facial", False),
            ("Tratamiento corporal", "Tratamiento facial", True),                 # otros servicios, distintos
            ("Masaje relajante", "Depilación axilas", True),
            ("Tratamiento facial", "Hidrofacial", False),                         # mismo servicio
        ]
        for nueva, agendada, se_puede in casos:
            with self.subTest(nueva=nueva, agendada=agendada):
                r, _ = self._crear(nueva, [self._evento(agendada)])
                self.assertEqual(r["ok"], se_puede, r)

    def test_exp_unica_con_costo_no_es_gratis(self):
        r, _ = self._crear("Exp. Única con costo - Facial", [self._evento("Lupita Exp. Única - Corporal")])
        self.assertTrue(r["ok"], r)

    def test_libres_gratis_corporal_deja_facial_de_paga(self):
        citas = [{"inicio": "10:00", "fin": "10:30", "categoria": "corporales", "gratis": True}]
        self.assertIn("10:00", self._libres("Tratamiento facial", citas))
        self.assertNotIn("10:00", self._libres("Sesión facial sin costo", citas))
        self.assertNotIn("10:00", self._libres("Tratamiento corporal", citas))

    def test_con_costo_no_cuenta_como_gratis(self):
        r, _ = self._crear("Sesión corporal sin costo", [self._evento("Lupita Exp. Única CON COSTO - Facial")])
        self.assertTrue(r["ok"])

    def test_carrera_cede_si_la_otra_gratis_fue_primero(self):
        r, svc = self._crear("Sesión corporal sin costo", [], [self._evento("Sesión facial sin costo")])
        self.assertFalse(r["ok"])
        svc.events.return_value.delete.assert_called_once()

    def test_carrera_empate_exacto_solo_cede_una(self):
        hora = "2030-01-01T00:00:01.000Z"
        menor, _ = self._crear("Sesión corporal sin costo", [], [self._evento("Sesión facial sin costo", id_="aaa", creado=hora)])
        mayor, _ = self._crear("Sesión corporal sin costo", [], [self._evento("Sesión facial sin costo", id_="zzz", creado=hora)])
        self.assertFalse(menor["ok"])  # "aaa" va antes que "nuestro": cedemos
        self.assertTrue(mayor["ok"])   # "zzz" va después: cede la otra

    def test_slots_gratis_no_ofrece_la_hora_de_otra_gratis(self):
        cita = {"inicio": "10:00", "fin": "11:00", "titulo": "x", "categoria": "faciales", "gratis": True}
        self.assertNotIn("10:00", self._libres("Sesión corporal sin costo", [cita]))
        self.assertIn("11:00", self._libres("Sesión corporal sin costo", [cita]))

    def test_slots_pagada_si_ofrece_esa_hora(self):
        cita = {"inicio": "10:00", "fin": "11:00", "titulo": "x", "categoria": "faciales", "gratis": True}
        self.assertIn("10:00", self._libres("Tratamiento corporal", [cita]))


class CategoriaServicio(unittest.TestCase):
    def test_lo_explicito_manda_sobre_palabras_sueltas(self):
        casos = {
            "Exp. Única - Corporal (abdomen, laterales, espalda)": "corporales",
            "Tratamiento facial para manchas en espalda": "faciales",
            "Masaje corporal relajante": "masajes",
            "Masaje de espalda": "masajes",
            "Depilación facial": "depilacion",
        }
        for texto, esperada in casos.items():
            self.assertEqual(calendar_tool.categoria_servicio(texto), esperada, texto)
            self.assertEqual(calendar_tool._calendario_para_servicio(texto), calendar_tool._CAL_POR_CLAVE[esperada], texto)


class RelevoCortesia(unittest.IsolatedAsyncioTestCase):
    async def test_no_gasta_ia_en_cortesias(self):
        for t in ("Gracias", "Gracias, ya quedó", "Ok", "Si", "Sí 👍", "Confirmado", "Muchas gracias!!", "👍"):
            with patch.object(brain, "generar_respuesta", AsyncMock()) as g:
                self.assertEqual(await brain.generar_relevo([{"role": "user", "content": t}]), brain.NO_ENVIAR, t)
                g.assert_not_awaited()

    async def test_lo_demas_si_se_contesta(self):
        for t in ("Quiero más información", "Rostro", "Costo", "Sí me interesa el facial", "Gracias, y cuánto cuesta?"):
            with patch.object(brain, "generar_respuesta", AsyncMock(return_value=("Hola", True))):
                self.assertEqual(await brain.generar_relevo([{"role": "user", "content": t}]), "Hola", t)


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await memory.inicializar_db()
        async with memory.async_session() as s:
            for tabla in (memory.Mensaje, memory.Lead, memory.EventoProcesado):
                await s.execute(delete(tabla))
            await s.commit()
        main._respuestas_recientes.clear()
        main._media_respondida.clear()

    async def asyncTearDown(self):
        await memory.engine.dispose()


class Memoria(Base):
    async def test_imagenes_repetidas_no_llenan_historial(self):
        await memory.guardar_mensaje(TEL, "user", "Quiero el plan corporal")
        await memory.guardar_mensaje(TEL, "assistant", "Con gusto, ¿mañana a las 11?")
        for _ in range(40):
            await memory.guardar_mensaje(TEL, "assistant", "[El equipo envió una imagen]")
        h = await memory.obtener_historial(TEL)
        texto = " ".join(m["content"] for m in h)
        self.assertIn("plan corporal", texto)
        self.assertEqual(texto.count("[El equipo envió una imagen]"), 1)

    async def test_opt_out_no_se_pierde(self):
        await memory.registrar_inbound_lead(TEL, "ya no me escriban por favor")
        for f in (memory.pausar_seguimiento, memory.marcar_lead_agendado):
            await f(TEL)
            async with memory.async_session() as s:
                self.assertEqual((await s.get(memory.Lead, TEL)).estado, "opt_out", f.__name__)

    async def test_pausado_sigue_funcionando(self):
        await memory.registrar_inbound_lead(TEL, "hola")
        await memory.pausar_seguimiento(TEL)
        async with memory.async_session() as s:
            self.assertEqual((await s.get(memory.Lead, TEL)).estado, "pausado")


class Relevo(Base):
    async def _lead(self, minutos, ultimo="user"):
        await memory.registrar_inbound_lead(TEL, "Quiero más información")
        await memory.guardar_mensaje(TEL, "user", "Quiero más información")
        if ultimo == "assistant":
            await memory.guardar_mensaje(TEL, "assistant", "¡Hola! Con gusto")
        async with memory.async_session() as s:
            lead = await s.get(memory.Lead, TEL)
            lead.last_inbound = memory.ahora() - timedelta(minutes=minutos)
            await s.commit()

    async def test_seleccion(self):
        await self._lead(30)
        self.assertEqual(len(await memory.leads_para_relevo()), 1)

    async def test_muy_reciente_no(self):
        await self._lead(5)
        self.assertEqual(await memory.leads_para_relevo(), [])

    async def test_ya_contestada_no(self):
        await self._lead(30, "assistant")
        self.assertEqual(await memory.leads_para_relevo(), [])

    async def test_muy_vieja_no(self):
        await self._lead(60 * 7)
        self.assertEqual(await memory.leads_para_relevo(), [])

    async def test_opt_out_no(self):
        await self._lead(30)
        await memory.marcar_lead_opt_out(TEL)
        self.assertEqual(await memory.leads_para_relevo(), [])

    async def _ciclo(self, texto):
        prov = AsyncMock()
        prov.enviar_mensaje.return_value = True
        with patch.object(brain, "generar_relevo", AsyncMock(return_value=texto)):
            return prov, await followup._ciclo_relevo(prov)

    async def test_contesta_una_sola_vez(self):
        await self._lead(30)
        prov, n = await self._ciclo("Disculpe la demora 💜")
        self.assertEqual(n, 1)
        prov.enviar_mensaje.assert_awaited_once()
        prov, n = await self._ciclo("Disculpe la demora 💜")
        self.assertEqual(n, 0)

    async def test_no_enviar_se_respeta(self):
        await self._lead(30)
        prov, n = await self._ciclo(brain.NO_ENVIAR)
        self.assertEqual(n, 0)
        prov.enviar_mensaje.assert_not_awaited()

    async def test_equipo_en_vivo_no_se_encima(self):
        await self._lead(30)
        await memory.registrar_outbound_equipo(TEL)
        prov, n = await self._ciclo("Disculpe")
        self.assertEqual(n, 0)

    async def test_falla_ia_se_reintenta(self):
        await self._lead(30)
        prov, n = await self._ciclo(None)
        self.assertEqual(n, 0)
        prov, n = await self._ciclo("Disculpe")
        self.assertEqual(n, 1)


def payload(msgs=None, ecos=None):
    v = {}
    if msgs:
        v["messages"] = msgs
    if ecos:
        v["message_echoes"] = ecos
    return {"entry": [{"changes": [{"value": v}]}]}


def texto(i, t, **extra):
    return {"id": i, "from": TEL, "type": "text", "text": {"body": t}, **extra}


class Webhook(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.enviar = AsyncMock(return_value=True)
        self.cerebro = AsyncMock(return_value=("Hola 💜", True))
        self.parches = [
            patch.object(main.proveedor, "enviar_mensaje", self.enviar),
            patch.object(main, "generar_respuesta", self.cerebro),
            patch.object(main, "sofia_debe_responder", lambda *_: True),
        ]
        for p in self.parches:
            p.start()

    async def asyncTearDown(self):
        for p in self.parches:
            p.stop()
        await super().asyncTearDown()

    async def post(self, cuerpo, firma=True):
        crudo = json.dumps(cuerpo).encode()
        h = {"Content-Type": "application/json"}
        if firma:
            h["X-Hub-Signature-256"] = (
                "sha256=" + hmac.new(b"secreto-de-prueba", crudo, hashlib.sha256).hexdigest()
            )
        t = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=t, base_url="http://x") as c:
            return await c.post("/webhook", content=crudo, headers=h)

    async def test_sin_firma_401_y_nada_se_envia(self):
        r = await self.post(payload([texto("a", "hola")]), firma=False)
        self.assertEqual(r.status_code, 401)
        self.enviar.assert_not_awaited()

    async def test_texto_se_contesta_y_guarda(self):
        r = await self.post(payload([texto("a", "Hola, info")]))
        self.assertEqual(r.status_code, 200)
        self.enviar.assert_awaited_once()
        h = await memory.obtener_historial(TEL)
        self.assertEqual([m["role"] for m in h], ["user", "assistant"])

    async def test_evento_repetido_una_sola_respuesta(self):
        await self.post(payload([texto("a", "Hola")]))
        await self.post(payload([texto("a", "Hola")]))
        self.enviar.assert_awaited_once()

    async def test_sticker_y_reaccion_no_se_contestan(self):
        await self.post(payload([{"id": "s", "from": TEL, "type": "sticker", "sticker": {}},
                                 {"id": "r", "from": TEL, "type": "reaction", "reaction": {}}]))
        self.enviar.assert_not_awaited()

    async def test_audio_pide_escribirlo_una_vez(self):
        await self.post(payload([{"id": "m1", "from": TEL, "type": "audio", "audio": {}}]))
        await self.post(payload([{"id": "m2", "from": TEL, "type": "audio", "audio": {}}]))
        self.enviar.assert_awaited_once()
        self.cerebro.assert_not_awaited()

    async def test_boton_de_plantilla_se_contesta(self):
        await self.post(payload([{"id": "b", "from": TEL, "type": "button", "button": {"text": "Confirmado"}}]))
        self.assertEqual(self.cerebro.await_args.args[0], "Confirmado")

    async def test_echo_equipo_silencia_a_sofia(self):
        await self.post(payload(ecos=[{"id": "e", "to": TEL, "type": "text", "text": {"body": "Hola, soy Ana"}}]))
        self.enviar.assert_not_awaited()
        await self.post(payload([texto("a", "Gracias Ana")]))
        self.enviar.assert_not_awaited()
        self.assertEqual((await memory.obtener_historial(TEL))[-1]["content"], "Gracias Ana")

    async def test_horario_equipo_guarda_y_calla(self):
        with patch.object(main, "sofia_debe_responder", lambda *_: False):
            await self.post(payload([texto("a", "Quiero más información")]))
        self.enviar.assert_not_awaited()
        self.assertEqual((await memory.obtener_historial(TEL))[-1]["role"], "user")
        async with memory.async_session() as s:
            self.assertIsNotNone(await s.get(memory.Lead, TEL))

    async def test_mensaje_viejo_no_se_contesta(self):
        ts = str(int(memory.ahora().timestamp()) - 3600)
        await self.post(payload([texto("a", "Hola", timestamp=ts)]))
        self.enviar.assert_not_awaited()

    async def test_si_falla_la_ia_no_se_pierde_el_mensaje(self):
        self.cerebro.return_value = ("Uy, problema técnico", False)
        await self.post(payload([texto("a", "Quiero mi plan facial")]))
        self.enviar.assert_not_awaited()
        self.assertEqual((await memory.obtener_historial(TEL))[-1]["content"], "Quiero mi plan facial")

    async def test_referral_se_guarda(self):
        await self.post(payload([texto("a", "Quiero mi plan", referral={"source_id": "123", "headline": "Tu plan"})]))
        filas = await memory.origenes_anuncio(None)
        self.assertEqual(filas[0]["ad_id"], "123")

    async def test_tope_por_hora(self):
        for i in range(main.MAX_RESPUESTAS_HORA + 3):
            await self.post(payload([texto(f"t{i}", f"Mensaje {i}")]))
        self.assertEqual(self.enviar.await_count, main.MAX_RESPUESTAS_HORA)

    async def test_endpoints_admin_exigen_clave(self):
        t = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=t, base_url="http://x") as c:
            for ruta in ("/leads", "/anuncios", "/reporte/ventas", "/mensajes", "/admin/export"):
                self.assertEqual((await c.get(ruta, params={"key": "mala"})).status_code, 403, ruta)
            self.assertEqual((await c.post("/admin/enviar", params={"key": "mala", "tel": "5512345678"},
                                           json={"texto": "x"})).status_code, 403)


if __name__ == "__main__":
    unittest.main()
