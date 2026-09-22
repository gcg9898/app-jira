"""Confianza TLS del sistema para Jira detrás de certificados corporativos."""

import ssl

import requests
from requests.adapters import HTTPAdapter


class SystemTrustAdapter(HTTPAdapter):
    """Añade las CA del sistema sin desactivar certificado ni nombre del servidor.

    requests usa habitualmente su bundle certifi; en Windows no incluye las CA
    corporativas instaladas en el almacén del equipo. create_default_context()
    carga ese almacén. Se añaden también las raíces públicas de requests.
    La API de pools de requests aplica este contexto tanto en conexión directa
    como detrás de un proxy. Un bundle explícito del usuario conserva prioridad.
    """

    def __init__(self, *args, **kwargs):
        self.system_context = ssl.create_default_context()
        self.system_context.load_verify_locations(cafile=requests.certs.where())
        super().__init__(*args, **kwargs)

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host_params, pool_kwargs = super().build_connection_pool_key_attributes(request, verify, cert)
        if verify is True:
            pool_kwargs["ssl_context"] = self.system_context
        return host_params, pool_kwargs


def use_system_certificates(session):
    """Solo cambia esta sesión. No parchea ssl globalmente ni reintenta sin TLS."""
    session.mount("https://", SystemTrustAdapter())
    session.verify = True


def request_error_detail(error):
    """Avisos útiles sin volcar URL de proxy, credenciales ni cuerpo de la petición."""
    if isinstance(error, requests.exceptions.SSLError):
        return ("no se pudo validar el certificado SSL/TLS. Revisa que la CA corporativa "
                "esté instalada en Windows o configura REQUESTS_CA_BUNDLE con su fichero PEM")
    if isinstance(error, requests.exceptions.Timeout):
        return "Jira no respondió dentro del tiempo de espera"
    if isinstance(error, requests.exceptions.ConnectionError):
        return "no se pudo conectar con Jira; comprueba la red o VPN"
    return f"error de consulta ({type(error).__name__})"