import unittest
from datetime import datetime

from agent.ghl_whatsapp import TZ, en_horario, monto, nombre, telefono


class TestDatos(unittest.TestCase):
    def test_telefono(self):
        self.assertEqual(telefono({"phone": "+5215581319747"}), "5215581319747")
        self.assertEqual(telefono({"phone": "+52 55 8131 9747"}), "5215581319747")
        self.assertEqual(telefono({"customData": {"telefono": "7711898583"}}), "5217711898583")
        self.assertEqual(telefono({"phone": "123"}), "")

    def test_nombre(self):
        self.assertEqual(nombre({"first_name": "KAREN"}), "Karen")
        self.assertEqual(nombre({"first_name": "María Guadalupe"}), "María")
        self.assertEqual(nombre({"full_name": "Cinthia Razo"}), "Cinthia")
        self.assertEqual(nombre({}), "hola")

    def test_monto(self):
        self.assertEqual(monto({"customData": {"monto": "1500"}}), "$1,500")
        self.assertEqual(monto({"amount": 999.5}), "$999.50")
        self.assertEqual(monto({"monto": "$0"}), "")

    def test_horario(self):
        self.assertTrue(en_horario(datetime(2026, 11, 20, 9, 0, tzinfo=TZ)))
        self.assertFalse(en_horario(datetime(2026, 11, 20, 20, 0, tzinfo=TZ)))
        self.assertFalse(en_horario(datetime(2026, 11, 20, 8, 59, tzinfo=TZ)))


if __name__ == "__main__":
    unittest.main()
