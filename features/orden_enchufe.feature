# language: es
Característica: Ejecución de la orden del enchufe en el Edge hogar

  Escenario: La orden del usuario corta el aparato
    Dado el actuador del enchufe disponible
    Cuando llega la orden "OFF" por el tópico "iot/EMSafe-PLUG-01/command"
    Entonces el Edge solicita el corte al actuador
    Y publica el estado "OFF" con el mismo commandId en "iot/EMSafe-PLUG-01/state"

  Escenario: El actuador no responde
    Dado el actuador del enchufe sin respuesta
    Cuando llega la orden "OFF" por el tópico "iot/EMSafe-PLUG-01/command"
    Entonces el Edge registra el error
    Y sigue recibiendo y publicando las lecturas del sensor
