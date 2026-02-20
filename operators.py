"""
Sistema de Operadores Multi-Carrier
Gestiona múltiples proveedores de SMS con fallover automático
"""

import hashlib
import json
import logging
from datetime import datetime
from typing import Optional, Dict, List
import pytz
from database import db

logger = logging.getLogger(__name__)

class OperadorConfig:
    """Configuración de un operador de SMS"""

    def __init__(self, operador, url_api, cuenta, contraseña, sender_id,
                 prioridad=1, max_por_minuto=100, max_reintentos=5,
                 timeout_segundos=10, habilitado=True):
        self.operador = operador
        self.url_api = url_api
        self.cuenta = cuenta
        self.contraseña = contraseña
        self.sender_id = sender_id
        self.prioridad = prioridad
        self.max_por_minuto = max_por_minuto
        self.max_reintentos = max_reintentos
        self.timeout_segundos = timeout_segundos
        self.habilitado = habilitado

    def generar_sign(self, timestamp=None):
        """Genera firma MD5 requerida por la API (formato específico del operador)"""
        if timestamp is None:
            zona = pytz.timezone("Asia/Shanghai")
            timestamp = datetime.now(zona).strftime('%Y%m%d%H%M%S')

        texto = self.cuenta + self.contraseña + timestamp
        sign = hashlib.md5(texto.encode()).hexdigest()
        return sign, timestamp

    def to_dict(self):
        """Convierte a diccionario"""
        return {
            'operador': self.operador,
            'url_api': self.url_api,
            'prioridad': self.prioridad,
            'habilitado': self.habilitado,
            'max_reintentos': self.max_reintentos
        }


class OperadorRouter:
    """Gestor de operadores con enrutamiento inteligente"""

    # Detección automática de operador por prefijos
    OPERADOR_PREFIJOS = {
        'movistar': ['310', '311', '320', '321'],  # Movistar Colombia
        'claro': ['301', '302', '303', '304', '305'],  # Claro Colombia
        'wom': ['322', '323'],  # WOM Colombia
        'directv': ['312'],  # DIRECTV Colombia
    }

    # Configuración predefinida de operadores
    OPERADORES_PREDEFINIDOS = {
        'principal': OperadorConfig(
            'principal',
            'http://sms.yx19999.com:20003/sendsmsV2',
            'cs_p8bh8b',
            'iGcMIQxT',
            'teddy',
            prioridad=1,
            max_reintentos=5
        ),
        'backup1': OperadorConfig(
            'backup1',
            'http://api-backup1.sms.com/send',
            'account_backup1',
            'password_backup1',
            'sender_backup1',
            prioridad=2,
            habilitado=False  # Deshabilitar hasta configurar
        ),
        'backup2': OperadorConfig(
            'backup2',
            'http://api-backup2.sms.com/send',
            'account_backup2',
            'password_backup2',
            'sender_backup2',
            prioridad=3,
            habilitado=False
        )
    }

    def __init__(self):
        self.operadores: Dict[str, OperadorConfig] = self.OPERADORES_PREDEFINIDOS.copy()
        logger.info(f"Router iniciado con {len(self.operadores)} operadores")

    def detectar_operador_por_numero(self, numero: str) -> str:
        """Detecta el operador probable basado en el número celular"""
        numero = numero.strip()
        if numero.startswith('57'):
            numero = numero[2:]

        # Primeros 3 dígitos
        prefijo = numero[:3]

        for operador, prefijos in self.OPERADOR_PREFIJOS.items():
            if prefijo in prefijos:
                return operador

        return 'principal'  # Por defecto

    def obtener_operador_siguiente(self, intento: int, operador_fallido: Optional[str] = None) -> OperadorConfig:
        """
        Obtiene el siguiente operador a usar (con fallover inteligente)

        Estrategia:
        1. Primer intento: operador detectado o principal
        2. Reintentos: alternar entre operadores disponibles
        """
        operadores_habilitados = [
            op for op in self.operadores.values()
            if op.habilitado
        ]

        if not operadores_habilitados:
            logger.error("No hay operadores habilitados!")
            return self.operadores['principal']

        # Ordenar por prioridad
        operadores_habilitados.sort(key=lambda x: x.prioridad)

        # Seleccionar operador basado en intento
        idx = intento % len(operadores_habilitados)
        return operadores_habilitados[idx]

    def obtener_operador(self, nombre: str) -> Optional[OperadorConfig]:
        """Obtiene configuración de un operador específico"""
        return self.operadores.get(nombre)

    def listar_operadores(self) -> List[Dict]:
        """Lista todos los operadores disponibles"""
        return [op.to_dict() for op in self.operadores.values()]

    def habilitar_operador(self, nombre: str, habilitado: bool = True):
        """Habilita o deshabilita un operador"""
        if nombre in self.operadores:
            self.operadores[nombre].habilitado = habilitado
            logger.info(f"Operador {nombre} {'habilitado' if habilitado else 'deshabilitado'}")
            return True
        return False

    def agregar_operador(self, config: OperadorConfig):
        """Agrega un operador personalizado"""
        self.operadores[config.operador] = config
        logger.info(f"Operador agregado: {config.operador}")

    def obtener_stats_operadores(self) -> List[Dict]:
        """Obtiene estadísticas de todos los operadores"""
        stats = []
        for nombre, operador in self.operadores.items():
            db_stats = db.obtener_stats_operador(nombre)
            stat = operador.to_dict()

            if db_stats:
                total_enviados = db_stats['total_enviados'] or 0
                total_entregados = db_stats['total_entregados'] or 0
                total_fallidos = db_stats['total_fallidos'] or 0

                stat['total_enviados'] = total_enviados
                stat['total_entregados'] = total_entregados
                stat['total_fallidos'] = total_fallidos
                stat['tasa_exito'] = (
                    (total_entregados / total_enviados * 100) if total_enviados > 0 else 0
                )
                stat['tasa_error'] = db_stats['tasa_error_actual']
                stat['ultimo_error'] = db_stats['ultimo_error']
            else:
                stat['total_enviados'] = 0
                stat['total_entregados'] = 0
                stat['total_fallidos'] = 0
                stat['tasa_exito'] = 0

            stats.append(stat)

        return sorted(stats, key=lambda x: x['prioridad'])


# Instancia global
router = OperadorRouter()
