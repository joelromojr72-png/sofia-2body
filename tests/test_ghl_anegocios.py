import unittest

from agent.ghl_anegocios import items_factura, pagos_pendientes, preparar


class TestPreparar(unittest.TestCase):
    def test_relleno_y_correos(self):
        datos = {
            "dir": [["Perez Ana", "5530469425", "blancavg1977@gmail.com"],
                    ["Lopez Eva", "5530469425", "eva@x.com"],
                    ["Ruiz Sol", "55 3046 9425", ""],
                    ["Neri Cris", "5511472944", ""], ["Nery Cris", "5511472944", ""],
                    ["GARCIA  ARMIDA", "7711238323", "a@x.com"], ["Garcia Armida", "7711238323", "a@x.com"]],
            "notas": {"1": ["2026-11-01", "garcia armida", 100, 0, []], "2": ["2026-11-02", "", 50, 0, []]},
            "abonos": [],
        }
        p = preparar(datos)
        cl = p["clientas"]
        self.assertEqual(cl["PEREZ ANA"]["tel"], "")          # teléfono de relleno (3 clientas)
        self.assertEqual(cl["PEREZ ANA"]["correo"], "")       # correo del equipo
        self.assertEqual(cl["LOPEZ EVA"]["correo"], "eva@x.com")
        self.assertEqual(cl["NERI CRIS"]["tel"], "5511472944")  # 2 nombres: sí sirve
        self.assertEqual(cl["GARCIA ARMIDA"]["correo"], "a@x.com")  # duplicado de la misma clienta
        self.assertEqual(len(cl["GARCIA ARMIDA"]["tickets"]), 1)
        self.assertEqual([t["folio"] for t in p["sin_clienta"]], ["2"])


class TestFactura(unittest.TestCase):
    def test_descuento(self):
        items, d = items_factura({"total": 6249, "lineas": [["A", 3999, 3999], ["B", 3500, 3500]]})
        self.assertEqual(d, 1250)
        self.assertEqual(sum(i["amount"] * i["qty"] for i in items) - d, 6249)

    def test_cantidad(self):
        items, d = items_factura({"total": 1000, "lineas": [["A", 500, 1000]]})
        self.assertEqual((items[0]["qty"], items[0]["amount"], d), (2, 500, 0))

    def test_cargo_extra(self):
        items, d = items_factura({"total": 1050, "lineas": [["A", 1000, 1000]]})
        self.assertEqual(sum(i["amount"] * i["qty"] for i in items) - d, 1050)


class TestPagos(unittest.TestCase):
    T = {"total": 3000, "abonos": [("2026-11-01 10:00", {"cash": 500}),
                                   ("2026-11-05 11:00", {"card": 1000, "bank_transfer": 500})]}

    def test_todos(self):
        p = pagos_pendientes(self.T, 0)
        self.assertEqual([(x["modo"], x["monto"]) for x in p], [("cash", 500), ("card", 1000), ("bank_transfer", 500)])

    def test_salta_ya_registrados(self):
        p = pagos_pendientes(self.T, 500)
        self.assertEqual([(x["modo"], x["monto"]) for x in p], [("card", 1000), ("bank_transfer", 500)])
        self.assertEqual(pagos_pendientes(self.T, 2000), [])

    def test_sobrepago_se_topa(self):
        t = {"total": 3500, "abonos": [("2026-11-01", {"card": 4000})]}
        self.assertEqual([x["monto"] for x in pagos_pendientes(t, 0)], [3500])


if __name__ == "__main__":
    unittest.main()
