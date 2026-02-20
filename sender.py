"""
Sistema de Envío SMS con Reintentos Inteligentes
Implementa backoff exponencial, multi-operador y rate limiting adaptativo
"""

import requests
import logging
import time
from datetime import datetime
from typing import Dict, Optional, Tuple
from database import db
from operators import router
from rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# Rate limiters por operador
rate_limiters = {}


class SMSSender:
    """Gestor de envío de SMS con reintentos y validación"""

    # Configuración de backoff exponencial (segundos)
    BACKOFF_DELAYS = [1, 5, 30, 300, 1800]  # 1s, 5s, 30s, 5min, 30min

    @staticmethod
    def preparar_mensaje(mensaje: str, numero: str, row_data: Optional[Dict] = None, link_dinamico: Optional[str] = None) -> str:
        """Prepara el mensaje reemplazando variables"""
        mensaje_final = mensaje

        # Reemplazar variables de datos
        if row_data:
            for columna, valor in row_data.items():
                placeholder = "{" + columna + "}"
                if placeholder in mensaje_final:
                    mensaje_final = mensaje_final.replace(placeholder, str(valor))

        # Reemplazar link dinámico
        if link_dinamico and '{link}' in mensaje_final:
            mensaje_final = mensaje_final.replace('{link}', link_dinamico)

        return mensaje_final

    @staticmethod
    def enviar_sms_ahora(queue_id: int, numero: str, mensaje: str,
                         operador_nombre: str, row_data: Optional[Dict] = None,
                         link_dinamico: Optional[str] = None) -> Dict:
        """
        Envía un SMS inmediatamente a través del operador especificado

        Retorna:
        {
            "success": bool,
            "numero": str,
            "operador": str,
            "respuesta": dict,
            "tiempo_ms": int
        }
        """
        operador = router.obtener_operador(operador_nombre)

        if not operador:
            logger.error(f"Operador no encontrado: {operador_nombre}")
            return {
                "success": False,
                "numero": numero,
                "operador": operador_nombre,
                "error": "Operador no existe"
            }

        # Preparar mensaje
        mensaje_procesado = SMSSender.preparar_mensaje(mensaje, numero, row_data, link_dinamico)

        # Validar longitud
        if len(mensaje_procesado) == 0:
            logger.error(f"Mensaje vacío para {numero}")
            db.actualizar_intento(queue_id, operador_nombre, 'error',
                                error="Mensaje vacío después de procesar variables")
            return {
                "success": False,
                "numero": numero,
                "operador": operador_nombre,
                "error": "Mensaje vacío"
            }

        # Rate limiting
        if operador_nombre not in rate_limiters:
            rate_limiters[operador_nombre] = RateLimiter(operador.max_por_minuto)

        limiter = rate_limiters[operador_nombre]
        limiter.esperar()

        # Construir parámetros de API
        sign, timestamp = operador.generar_sign()

        params = {
            "account": operador.cuenta,
            "sign": sign,
            "datetime": timestamp
        }

        # Agregar prefijo de país si no existe
        numero_formateado = "57" + numero if not numero.startswith('57') else numero

        data = {
            "senderid": operador.sender_id,
            "numbers": numero_formateado,
            "content": mensaje_procesado
        }

        inicio = time.time()

        try:
            response = requests.post(
                operador.url_api,
                params=params,
                json=data,
                timeout=operador.timeout_segundos
            )

            tiempo_ms = int((time.time() - inicio) * 1000)

            # Intentar parsear respuesta
            try:
                respuesta = response.json()
            except:
                respuesta = {"text": response.text, "status_code": response.status_code}

            logger.info(f"[{operador_nombre}] Enviado a {numero} en {tiempo_ms}ms")

            # Actualizar en BD
            db.actualizar_intento(
                queue_id,
                operador_nombre,
                'enviado',
                respuesta_api=str(respuesta),
                tiempo_ms=tiempo_ms
            )

            return {
                "success": True,
                "numero": numero,
                "operador": operador_nombre,
                "respuesta": respuesta,
                "tiempo_ms": tiempo_ms
            }

        except requests.Timeout:
            logger.error(f"[{operador_nombre}] Timeout enviando a {numero}")
            db.actualizar_intento(
                queue_id,
                operador_nombre,
                'error',
                error="Timeout de conexión"
            )
            return {
                "success": False,
                "numero": numero,
                "operador": operador_nombre,
                "error": "Timeout"
            }

        except requests.RequestException as e:
            logger.error(f"[{operador_nombre}] Error HTTP enviando a {numero}: {e}")
            db.actualizar_intento(
                queue_id,
                operador_nombre,
                'error',
                error=str(e)
            )
            return {
                "success": False,
                "numero": numero,
                "operador": operador_nombre,
                "error": str(e)
            }

        except Exception as e:
            logger.error(f"[{operador_nombre}] Error inesperado: {e}")
            db.actualizar_intento(
                queue_id,
                operador_nombre,
                'error',
                error=str(e)
            )
            return {
                "success": False,
                "numero": numero,
                "operador": operador_nombre,
                "error": str(e)
            }

    @staticmethod
    def procesar_cola():
        """
        Procesa SMS pendientes de la cola
        Se ejecuta en segundo plano periódicamente
        """
        pendientes = db.obtener_pendientes(limit=50)

        if not pendientes:
            return

        logger.info(f"Procesando {len(pendientes)} SMS pendientes")

        for item in pendientes:
            queue_id = item['id']
            numero = item['numero']
            mensaje = item['mensaje']
            intento = item['intentos']

            # Seleccionar operador siguiente
            operador = router.obtener_operador_siguiente(intento, item['operador'])

            logger.debug(f"Reintento {intento + 1} para {numero} con {operador.operador}")

            # Enviar
            resultado = SMSSender.enviar_sms_ahora(
                queue_id,
                numero,
                mensaje,
                operador.operador
            )

            # Log del resultado
            if resultado['success']:
                logger.info(f"✓ {numero} enviado con {operador.operador}")
            else:
                logger.warning(f"✗ {numero} falló: {resultado.get('error', 'Unknown')}")

    @staticmethod
    def reintentar_fallidos():
        """Procesa SMS que necesitan reintentarse"""
        pendientes = db.obtener_pendientes(limit=100)

        reintentos_iniciados = 0

        for item in pendientes:
            if item['estado'] == 'reintentando':
                reintentos_iniciados += 1
                queue_id = item['id']
                numero = item['numero']
                mensaje = item['mensaje']
                intento = item['intentos']

                # Cambiar operador en cada reintento
                operador = router.obtener_operador_siguiente(intento)

                logger.info(f"Reintentando SMS {queue_id} (intento {intento + 1}) con {operador.operador}")

                SMSSender.enviar_sms_ahora(
                    queue_id,
                    numero,
                    mensaje,
                    operador.operador
                )

        if reintentos_iniciados > 0:
            logger.info(f"Iniciados {reintentos_iniciados} reintentos")
