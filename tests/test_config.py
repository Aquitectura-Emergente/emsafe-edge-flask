"""Configuración por entorno: valores por defecto y errores claros."""
import unittest

from shared.config import EdgeSettings


class DefaultsTest(unittest.TestCase):
    def test_sin_variables_usa_los_defaults_documentados(self):
        s = EdgeSettings.from_env({})

        self.assertEqual(s.edge_node_id, "EDGE-HOGAR-01")
        self.assertEqual(s.organization_id, "org-demo")
        self.assertEqual(s.device_name, "EMSafe-DOS-01")
        self.assertEqual(s.device_names, ("EMSafe-DOS-01",))
        self.assertEqual(s.caution_ut, 100.0)
        self.assertEqual(s.danger_ut, 200.0)
        self.assertIsNone(s.restore_ut)
        self.assertEqual(s.saturation_ut, 790.0)
        self.assertFalse(s.auto_cutoff_enabled)
        self.assertEqual(s.plug_id, "EMSafe-PLUG-01")
        self.assertEqual(s.actuator_url, "")
        self.assertEqual(s.log_level, "INFO")
        self.assertEqual(s.http_host, "127.0.0.1")
        self.assertEqual(s.mqtt_host, "localhost")
        self.assertEqual(s.mqtt_port, 1883)
        self.assertFalse(s.mqtt_tls)
        self.assertIsNone(s.mqtt_username)
        self.assertIsNone(s.mqtt_password)
        self.assertEqual(s.db_path, "emsafe_edge.db")
        self.assertEqual(s.outbox_retention_days, 7)

    def test_retencion_del_outbox(self):
        self.assertEqual(EdgeSettings.from_env({"EMSAFE_OUTBOX_RETENTION_DAYS": "30"}).outbox_retention_days, 30)
        for raw in ("0", "-1", "siete"):
            with self.assertRaises(ValueError, msg=raw) as ctx:
                EdgeSettings.from_env({"EMSAFE_OUTBOX_RETENTION_DAYS": raw})
            self.assertIn("EMSAFE_OUTBOX_RETENTION_DAYS", str(ctx.exception))

    def test_variables_vacias_cuentan_como_no_definidas(self):
        s = EdgeSettings.from_env({"EMSAFE_MQTT_HOST": "  ", "EMSAFE_MQTT_PORT": ""})

        self.assertEqual(s.mqtt_host, "localhost")
        self.assertEqual(s.mqtt_port, 1883)


class ParsingTest(unittest.TestCase):
    def test_lee_todas_las_variables(self):
        s = EdgeSettings.from_env({
            "EMSAFE_EDGE_NODE_ID": "EDGE-HOGAR-07",
            "EMSAFE_ORGANIZATION_ID": "org-casa",
            "EMSAFE_DEVICE_NAME": "EMSafe-FIX-02",
            "EMSAFE_CAUTION_UT": "120",
            "EMSAFE_DANGER_UT": "250.5",
            "EMSAFE_RESTORE_UT": "180",
            "EMSAFE_AUTO_CUTOFF_ENABLED": "true",
            "EMSAFE_PLUG_ID": "EMSafe-PLUG-07",
            "EMSAFE_ACTUATOR_URL": "http://192.168.1.50/",
            "EMSAFE_LOG_LEVEL": "debug",
            "EMSAFE_HTTP_HOST": "0.0.0.0",
            "EMSAFE_SATURATION_UT": "780",
            "EMSAFE_MQTT_HOST": "broker.local",
            "EMSAFE_MQTT_PORT": "8883",
            "EMSAFE_MQTT_TLS": "true",
            "EMSAFE_MQTT_USERNAME": "edge07",
            "EMSAFE_MQTT_PASSWORD": "secreto",
            "EMSAFE_DB_PATH": "/tmp/edge.db",
        })

        self.assertEqual(s.edge_node_id, "EDGE-HOGAR-07")
        self.assertEqual(s.organization_id, "org-casa")
        self.assertEqual(s.device_names, ("EMSafe-FIX-02",))
        self.assertEqual(s.caution_ut, 120.0)
        self.assertEqual(s.danger_ut, 250.5)
        self.assertEqual(s.restore_ut, 180.0)
        self.assertTrue(s.auto_cutoff_enabled)
        self.assertEqual(s.plug_id, "EMSafe-PLUG-07")
        self.assertEqual(s.actuator_url, "http://192.168.1.50")
        self.assertEqual(s.log_level, "DEBUG")
        self.assertEqual(s.http_host, "0.0.0.0")
        self.assertEqual(s.saturation_ut, 780.0)
        self.assertEqual(s.mqtt_host, "broker.local")
        self.assertEqual(s.mqtt_port, 8883)
        self.assertTrue(s.mqtt_tls)
        self.assertEqual(s.mqtt_username, "edge07")
        self.assertEqual(s.mqtt_password, "secreto")
        self.assertEqual(s.db_path, "/tmp/edge.db")

    def test_booleanos_aceptan_variantes_habituales(self):
        for raw in ("1", "true", "TRUE", "yes", "si"):
            self.assertTrue(EdgeSettings.from_env({"EMSAFE_MQTT_TLS": raw}).mqtt_tls, raw)
        for raw in ("0", "false", "no", "off"):
            self.assertFalse(EdgeSettings.from_env({"EMSAFE_MQTT_TLS": raw}).mqtt_tls, raw)

    def test_booleano_ambiguo_falla_en_vez_de_adivinar(self):
        with self.assertRaises(ValueError) as ctx:
            EdgeSettings.from_env({"EMSAFE_MQTT_TLS": "quizas"})
        self.assertIn("EMSAFE_MQTT_TLS", str(ctx.exception))

    def test_corte_automatico_se_puede_activar(self):
        self.assertFalse(
            EdgeSettings.from_env({"EMSAFE_AUTO_CUTOFF_ENABLED": "no"}).auto_cutoff_enabled
        )
        self.assertTrue(
            EdgeSettings.from_env({"EMSAFE_AUTO_CUTOFF_ENABLED": "si"}).auto_cutoff_enabled
        )


class ValidationTest(unittest.TestCase):
    def test_umbral_no_numerico_nombra_la_variable(self):
        with self.assertRaises(ValueError) as ctx:
            EdgeSettings.from_env({"EMSAFE_DANGER_UT": "alto"})
        self.assertIn("EMSAFE_DANGER_UT", str(ctx.exception))

    def test_umbral_nan_se_rechaza(self):
        with self.assertRaises(ValueError):
            EdgeSettings.from_env({"EMSAFE_CAUTION_UT": "nan"})

    def test_danger_debe_quedar_bajo_la_saturacion(self):
        # El umbral de DANGER va al sensor: igual o mayor que la saturación nunca dispararía.
        with self.assertRaises(ValueError):
            EdgeSettings.from_env({"EMSAFE_DANGER_UT": "790"})

    def test_caution_no_supera_danger_y_es_positivo(self):
        with self.assertRaises(ValueError):
            EdgeSettings.from_env({"EMSAFE_CAUTION_UT": "250"})
        with self.assertRaises(ValueError):
            EdgeSettings.from_env({"EMSAFE_CAUTION_UT": "0"})
        s = EdgeSettings.from_env({"EMSAFE_CAUTION_UT": "200"})
        self.assertEqual(s.caution_ut, s.danger_ut)

    def test_restablecimiento_bajo_danger(self):
        for raw in ("200", "250", "0", "-5"):
            with self.assertRaises(ValueError, msg=raw):
                EdgeSettings.from_env({"EMSAFE_RESTORE_UT": raw})

    def test_nivel_de_log_invalido(self):
        with self.assertRaises(ValueError):
            EdgeSettings.from_env({"EMSAFE_LOG_LEVEL": "verbose"})

    def test_puerto_invalido(self):
        with self.assertRaises(ValueError):
            EdgeSettings.from_env({"EMSAFE_MQTT_PORT": "abc"})
        with self.assertRaises(ValueError):
            EdgeSettings.from_env({"EMSAFE_MQTT_PORT": "70000"})


class SecretsTest(unittest.TestCase):
    def test_las_credenciales_no_aparecen_en_repr(self):
        s = EdgeSettings.from_env(
            {"EMSAFE_MQTT_USERNAME": "edge07", "EMSAFE_MQTT_PASSWORD": "secreto-123"}
        )

        self.assertNotIn("secreto-123", repr(s))
        self.assertNotIn("edge07", repr(s))


if __name__ == "__main__":
    unittest.main()
