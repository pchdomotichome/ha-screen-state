# Screen State

Estado de la pantalla de tus PC publicado en **Home Assistant** como una entidad.
La lógica vive en HA (automatizaciones con condiciones); el PC sólo informa.

```
┌──────────── PC (Linux/GNOME) ────────────┐        ┌──────── Home Assistant ────────┐
│ pc-screen-agent                          │        │                                │
│  · org.gnome.ScreenSaver (pantalla)     │ ─────▶ │ binary_sensor.<slug>_pantalla  │
│  · logind PrepareForSleep (suspensión)   │  push  │            (on / off)          │
│                                          │◀────── │                                │
│  HTTP: /state  /events  /health         │  SSE   │ automatizaciones con           │
└──────────────────────────────────────────┘        │ condiciones + acciones         │
                                                     └────────────────────────────────┘
```

## Dos modos de uso

| | Modo **push** | Modo **integración** (HACS) |
|---|---|---|
| Qué necesitás | un token de HA en el PC | la integración instalada en HA |
| Entidad | `binary_sensor.<slug>_pantalla` (publicada por REST) | idem, **con dispositivo real** en Dispositivos y servicios |
| Latencia | inmediata | inmediata (SSE) |
| Reinicio de HA | el agente la republica sola | la entidad se reconstruye sola |
| Ideal para | probar rápido, setups chicos | varios equipos, entity registry,Diagnostics |

---

## Modo integración — instalación desde HACS

1. HACS → *Integraciones* → menú ⋮ → **Repositorios personalizados** →
   `owner/ha-screen-state` (categoría: *Integración*).
2. Buscá **Screen State** en HACS y descargala.
3. **Reiniciá Home Assistant** (obligatorio para integraciones nuevas).
4. *Configuración → Dispositivos y servicios → Añadir integración → Screen State*:
   - **Nombre corto**: identificador del equipo (ej. `escritorio`)
   - **IP o hostname** del PC
   - **Puerto**: `8099`
   - **Token**: sólo si configuraste `AGENT_TOKEN` en el agente
5. Queda la entidad `binary_sensor.<slug>_pantalla` dentro del dispositivo.

Para actualizar a futuro versiones: HACS → *Actualizar* → Download.

## Modo push — sin tocar Home Assistant

**En el PC:**

```bash
cd agent
mkdir -p ~/.config/pc-screen-agent
cp agent.env.example ~/.config/pc-screen-agent/agent.env   # editá HOST_SLUG, HA_URL, HA_TOKEN
./install.sh
```

La entidad aparece sola. El agente republica cada `HA_HEARTBEAT` segundos, así que
se recupera automáticamente si HA se reinició.

> Si instalás la integración en un equipo que ya usaba push, **vacíá `HA_URL` y
> `HA_TOKEN`** en el `.env` y reiniciá el agente: si no, HA creará
> `binary_sensor.<slug>_pantalla_2` por el choque de nombres.

---

## El agente en el PC

```bash
# Estado actual
pc-screen-agent.py --check
curl -s http://127.0.0.1:8099/state | python3 -m json.tool

# Ver el stream de cambios en vivo
curl -N http://127.0.0.1:8099/events

# Logs (el servicio escribe a archivo)
tail -f ~/.local/share/pc-screen-agent/agent.log
```

Detecta la pantalla con `org.gnome.ScreenSaver` (GNOME, X11 o Wayland) y la
suspensión con `login1.PrepareForSleep` (funciona aunque el agente corra como
servicio de usuario, sin privilegios extra).

### Configuración (`~/.config/pc-screen-agent/agent.env`)

| Clave | Default | Descripción |
|---|---|---|
| `HOST_SLUG` | `pc` | Define el `entity_id`: `binary_sensor.<slug>_pantalla` |
| `HOST_LABEL` | = slug | Nombre visible |
| `HA_URL` / `HA_TOKEN` | vacío | Modo push. Vacíos = sólo modo pull/integración |
| `HA_ENTITY_ID` | derivado | Por si preferís otra entidad |
| `HA_HEARTBEAT` | `300` | Segundos entre republicaciones sin cambios |
| `AGENT_BIND` / `AGENT_PORT` | `0.0.0.0` / `8099` | Servidor HTTP/SSE |
| `AGENT_TOKEN` | vacío | Bearer para `/state`, `/events`, `/health` |
| `DETECTORS` | `gnome,logind` | Qué señales escuchar |
| `POLL_INTERVAL` | `30` | Segundos del sondeo de seguridad (reconcilia estado y reconecta detectores) |
| `LOG_LEVEL` | `INFO` | `DEBUG` para ver cada señal |

## API del agente

| Endpoint | Respuesta |
|---|---|
| `GET /health` | `{"ok": true, ...}` — lo usa el config flow de HA |
| `GET /state` | estado completo (abajo) |
| `GET /events` | Server-Sent Events: un evento por cada cambio |

```json
{
  "state": "on",
  "active": true,
  "suspended": false,
  "since": "2026-09-30T09:53:09-03:00",
  "reason": "inicio",
  "host_slug": "escritorio",
  "hostname": "mi-pc",
  "version": "1.0.0",
  "uptime_s": 0
}
```

## Automatizaciones

`examples/automaciones.yaml` tiene cuatro variantes: básica, sólo de noche
(`condition: sun`), multi-PC (mapeo + `repeat`) y una que respeta el apagado
manual con un `input_boolean`.

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.escritorio_pantalla
    to: "on"
    id: pantalla_on
actions:
  - choose:
      - conditions:
          - condition: trigger
            id: pantalla_on
        sequence:
          - action: light.turn_on
            target:
              entity_id: light.luces_escritorio
```

Podés combinar con `condition: sun`, `condition: time` (con `weekday`),
`condition: state` sobre otras entidades, o `condition: template` si necesitás
lógica con los atributos (`active`, `suspended`, `reason`).

## Diagnóstico

`/state` incluye `detectors_active`. Si `gnome` está en `false`, el agente **no está
detectando cambios de pantalla** aunque `/health` responda bien — la entidad en HA
se queda congelada en el último valor y las automatizaciones no disparan:

```bash
curl -s http://127.0.0.1:8099/state | python3 -m json.tool | grep -A3 detectors_active
```

El agente reconecta solo los detectores caídos cada `POLL_INTERVAL` segundos, y los
errores de arranque quedan en el log con la causa concreta:

```bash
grep -E "detector|No se pudo activar" ~/.local/share/pc-screen-agent/agent.log
```

En HA, el atributo `detectors_active` de la entidad permite montar un watchdog
(un `binary_sensor` template con `device_class: problem` más una automatización que
notifique cuando el agente queda sordo).

## Agregar otro equipo

1. Instalá el agente en ese PC (`install.sh`).
2. Cambiá `HOST_SLUG` (ej. `dormitorio`).
3. En el modo integración: agregá la entrada desde la UI de HA.
   En el modo push: no hacés nada, aparece `binary_sensor.dormitorio_pantalla`.
4. Sumá el equipo al mapeo de la automatización multi-PC.

Nada del agente es específico de una máquina: el slug define la entidad.

## Requisitos

- Linux con GNOME (o el detector que corresponda en `DETECTORS`) y
  `/usr/bin/python3` con PyGObject (`sudo apt install python3-gi` en Debian/Ubuntu)
- Opcional: corta el puerto 8099 desde fuera con `AGENT_TOKEN`

## Desinstalar

```bash
systemctl --user disable --now pc-screen-agent
rm -rf ~/.local/share/pc-screen-agent ~/.config/pc-screen-agent \
       ~/.config/systemd/user/pc-screen-agent.service
```

En HA: borrá la automatización y, si usaste la integración, la entrada desde
*Dispositivos y servicios* (y la integración desde HACS).