#!/usr/bin/env bash
# Virtual display + VNC (so you can sign in to Walmart and solve the odd CAPTCHA), then the service.
set -euo pipefail
export DISPLAY=:99
mkdir -p "${DATA_DIR:-/data}"

# Settings and tokens live with the data (/data/grocer.env), so the container can be deleted and
# re-pulled freely. Plain KEY=value lines; values are taken literally (no shell expansion).
# Anything set on the container itself (docker -e, Unraid template) wins over the file.
ENV_FILE="${GROCER_ENV_FILE:-${DATA_DIR:-/data}/grocer.env}"
if [[ -f "$ENV_FILE" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
    key="${line%%=*}"; key="${key//[[:space:]]/}"; val="${line#*=}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    [[ -z "${!key+x}" ]] && export "$key=$val"
  done < "$ENV_FILE"
fi
# a container restart can leave Chrome's profile lock behind
rm -f "${DATA_DIR:-/data}"/browser-profile/Singleton* 2>/dev/null || true

# a plain container restart keeps /tmp, and a leftover lock stops the screen from starting
pkill -f "Xvfb :99" 2>/dev/null || true
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
Xvfb :99 -screen 0 1366x900x24 -nolisten tcp &
for _ in $(seq 1 50); do [[ -e /tmp/.X11-unix/X99 ]] && break; sleep 0.1; done
fluxbox >/dev/null 2>&1 &
if [[ -n "${VNC_PASSWORD:-}" ]]; then
  x11vnc -storepasswd "$VNC_PASSWORD" /tmp/vncpass >/dev/null
  x11vnc -display :99 -forever -shared -quiet -rfbport 5900 -localhost -rfbauth /tmp/vncpass &
else
  # never leave the Walmart browser open to the whole network: make a password and say where to find it
  VNC_PASSWORD="$(head -c 12 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 8)"
  PW_FILE="${DATA_DIR:-/data}/vnc-password.txt"
  (umask 077; printf '%s\n' "$VNC_PASSWORD" > "$PW_FILE")
  echo "VNC_PASSWORD was not set. Made a browser view password and saved it in $PW_FILE (set VNC_PASSWORD in grocer.env to choose your own)."
  x11vnc -storepasswd "$VNC_PASSWORD" /tmp/vncpass >/dev/null
  x11vnc -display :99 -forever -shared -quiet -rfbport 5900 -localhost -rfbauth /tmp/vncpass &
fi
websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &

exec uvicorn grocer.app:app --host 0.0.0.0 --port 8790 --log-level info
