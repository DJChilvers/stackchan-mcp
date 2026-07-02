# Aperture · Wheatley — Android Companion App

Native Android control console for Wheatley, styled after Aperture Laboratories.
Talks to the **PC gateway** over the LAN (the gateway's `/api/*` companion API on
port `8770`), which in turn drives the ESP32. No Bluetooth / no firmware changes.

## Status

Phase 0–2 (control): Status/telemetry, Control (head, torque, expression, LEDs,
motor stub), Sayings (category buttons + free text), Demo + reactions.
Camera (Phase 3) and Faces/Visitor log (Phase 4) are placeholder screens.

## Prerequisites (one-time, on the Windows PC)

1. Install **Android Studio** (bundles the Android SDK + JDK 17).
2. Open this `companion-android/` folder in Android Studio; let it sync Gradle
   (it will download the Gradle 8.10.2 distribution and generate the wrapper jar).
3. Enable **USB debugging** on the Android phone (or use an emulator).

> The Gradle wrapper `.jar` isn't checked in. Android Studio regenerates it on
> first sync; from a CLI you can run `gradle wrapper` once (needs a system Gradle)
> before `./gradlew`.

## Run

1. Ensure the gateway daemon is running and reachable on the LAN (companion API
   on `:8770`). Confirm from the PC: `curl http://127.0.0.1:8770/api/health`.
2. Build & install: `./gradlew installDebug` (or Run ▶ in Android Studio).
3. On first launch open **Status → Gateway connection** and set:
   - **Host** = the PC's LAN IP (e.g. `192.168.1.138`)
   - **Port** = `8770`
   - **Token** = only if `COMPANION_TOKEN` / `STACKCHAN_TOKEN` is set on the gateway
4. The Status banner should read **ONLINE** when the device is awake.

## Architecture

- `data/GatewayConfig` + `AppSettings` — connection settings (DataStore).
- `data/GatewayClient` — suspend wrapper over `/api/*` (OkHttp + kotlinx.serialization).
- `MainViewModel` — polls `/api/status`, holds categories, runs actions with feedback.
- `ui/theme` — Aperture palette + monospace type.
- `ui/screens` — one screen per bottom-nav tab.

Phone → `http://<pc-lan-ip>:8770/api/*` → gateway → WebSocket → Wheatley.
