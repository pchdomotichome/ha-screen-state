"""Constantes de la integración Screen State."""

DOMAIN = "screen_state"
PLATFORMS = ["binary_sensor"]

CONF_SLUG = "slug"
CONF_HOST = "host"
CONF_PORT = "port"
CONF_TOKEN = "token"

DEFAULT_PORT = 8099
SCAN_INTERVAL_SECONDS = 120  # respaldo por sondeo si se cae el stream SSE
# El agente manda un latido cada ~25 s: si pasan 90 s sin datos, el stream está
# muerto (socket colgado a medias). Sin este timeout aiohttp esperaba para
# siempre y la entidad quedaba congelada sin ningún error visible.
STREAM_READ_TIMEOUT = 90