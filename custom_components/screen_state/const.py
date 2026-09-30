"""Constantes de la integración Screen State."""

DOMAIN = "screen_state"
PLATFORMS = ["binary_sensor"]

CONF_SLUG = "slug"
CONF_HOST = "host"
CONF_PORT = "port"
CONF_TOKEN = "token"

DEFAULT_PORT = 8099
SCAN_INTERVAL_SECONDS = 300  # respaldo por sondeo si se cae el stream SSE