"""Pruebas del proveedor de conversación GHL (contestar WhatsApp desde GHL)."""
import asyncio, base64, os, sys, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./_prueba_ghlprov.db")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402

from agent import ghl_proveedor  # noqa: E402


class Firma(unittest.TestCase):
    def setUp(self):
        self.priv = Ed25519PrivateKey.generate()
        pem = self.priv.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        self.p = mock.patch.object(ghl_proveedor, "GHL_PUBLIC_KEY_PEM", pem.decode())
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_valida_y_rechaza(self):
        cuerpo = b'{"messageId":"x","message":"hola"}'
        firma = base64.b64encode(self.priv.sign(cuerpo)).decode()
        self.assertTrue(ghl_proveedor.firma_valida(cuerpo, firma))
        self.assertFalse(ghl_proveedor.firma_valida(cuerpo + b" ", firma))
        self.assertFalse(ghl_proveedor.firma_valida(cuerpo, None))
        self.assertFalse(ghl_proveedor.firma_valida(cuerpo, "basura"))

    def test_llave_real_carga(self):
        self.p.stop()
        self.assertFalse(ghl_proveedor.firma_valida(b"{}", base64.b64encode(b"0" * 64).decode()))
        self.p.start()


class Entregar(unittest.TestCase):
    def test_manda_guarda_sin_espejo_y_marca_entregado(self):
        from agent import main as app_main, memory
        enviados, guardados, status = [], [], []

        class Prov:
            async def enviar_mensaje(self, tel, texto, contexto=None):
                enviados.append((tel, texto)); return True

        async def guardar(tel, role, content, espejo=True):
            guardados.append((tel, role, content, espejo))

        async def fake_status(c, mid, ok, error=""):
            status.append((mid, ok))

        async def correr():
            with mock.patch.object(app_main, "proveedor", Prov()), \
                 mock.patch.object(ghl_proveedor, "token", mock.AsyncMock(return_value="tok")), \
                 mock.patch.object(ghl_proveedor, "_telefono_local", mock.AsyncMock(return_value="5217711111821")), \
                 mock.patch.object(ghl_proveedor, "_nombre_usuario", mock.AsyncMock(return_value="Blanca")), \
                 mock.patch.object(ghl_proveedor, "_status", fake_status), \
                 mock.patch.object(memory, "guardar_mensaje", guardar), \
                 mock.patch.object(memory, "registrar_outbound_equipo", mock.AsyncMock()) as reg:
                await ghl_proveedor.entregar({"messageId": "m1", "phone": "+5217711111821", "message": "Hola, te esperamos"})
                reg.assert_awaited_once_with("5217711111821")

        asyncio.run(correr())
        self.assertEqual(enviados, [("5217711111821", "Hola, te esperamos")])
        self.assertEqual(guardados[0][3], False)
        self.assertIn("Blanca", guardados[0][2])
        self.assertEqual(status, [("m1", True)])

    def test_falla_whatsapp_marca_fallido(self):
        from agent import main as app_main
        status = []

        class Prov:
            async def enviar_mensaje(self, tel, texto, contexto=None):
                return False

        async def fake_status(c, mid, ok, error=""):
            status.append((mid, ok, "24 h" in error))

        async def correr():
            with mock.patch.object(app_main, "proveedor", Prov()), \
                 mock.patch.object(ghl_proveedor, "token", mock.AsyncMock(return_value="tok")), \
                 mock.patch.object(ghl_proveedor, "_telefono_local", mock.AsyncMock(return_value="5217711111821")), \
                 mock.patch.object(ghl_proveedor, "_status", fake_status):
                await ghl_proveedor.entregar({"messageId": "m2", "phone": "+5217711111821", "message": "hola"})

        asyncio.run(correr())
        self.assertEqual(status, [("m2", False, True)])


if __name__ == "__main__":
    unittest.main()
