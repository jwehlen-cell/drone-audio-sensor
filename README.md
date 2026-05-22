# Drone Audio Sensor System

Android + Google Cloud system for continuous drone audio detection using YAMNet, with TAK/CoT dissemination.

## Repository layout

```
proto/      Shared gRPC/protobuf contract (Android, backend, TAK publisher)
android/    Dedicated-device sensor app
```

Future sessions will add `backend/` (gateway + inference + TAK publisher) and `iac/` (Terraform).

## Session 1 — Phone-side streaming core

Delivered:

- `proto/drone_audio.proto` — full gRPC service + message contract
- Android Gradle project (Kotlin, gRPC-OkHttp, protobuf-lite)
- `AudioCaptureService` — foreground service, 16 kHz mono PCM, 1-second frames
- `StreamingClient` — persistent bidi gRPC stream, handshake-then-frames, exponential backoff with jitter, ServerCommand handling
- `BootReceiver` — auto-launches the service on `BOOT_COMPLETED`
- `MainActivity` — minimal status screen with state/metrics
- `DeviceIdentity` — UUID-backed device ID (Keystore-backed identity comes in Session 5)
- `DeviceHealthSnapshot` — battery, network, thermal, queue depth

## Building

Open the `android/` directory in Android Studio (Iguana or newer). Sync Gradle, then run on a device with API 28+.

To build from CLI you'll need Android SDK installed and `local.properties` pointing at it. From `android/`:

```
gradle wrapper                       # first time only, if no gradlew present
./gradlew :app:assembleDebug
```

## Configuring the gRPC endpoint

The default endpoint is `10.0.2.2:50051` (Android emulator → host machine), plaintext. Override via `BuildConfig` constants in `app/build.gradle.kts` or programmatically via `AppConfig.endpoint`. TLS support is wired but disabled by default for R&D.

## Session roadmap

1. ✅ Proto + Android streaming core
2. Cloud gateway + Terraform skeleton
3. YAMNet inference worker
4. TAK/CoT publisher + Android hardening
5. Security, kiosk mode, ops, docs
