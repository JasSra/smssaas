#!/bin/bash
# Build the SmsSaaS Android APK in a Docker container with Android SDK.
# Run this from inside /opt/smssaas/android-app/ on docker-host.
#
# Output: app/build/outputs/apk/release/app-release-unsigned.apk
# Then sign with: apksigner sign --ks debug.keystore --ks-pass pass:android ...

set -euo pipefail

cd "$(dirname "$0")"

# Generate a debug keystore if missing (for self-signed installable APK)
if [[ ! -f debug.keystore ]]; then
  docker run --rm -v "$PWD":/work -w /work \
    eclipse-temurin:17-jdk \
    keytool -genkey -v -keystore debug.keystore -storepass android \
      -alias androiddebugkey -keypass android -keyalg RSA -keysize 2048 \
      -validity 10000 -dname "CN=Android Debug,O=Android,C=US"
fi

# Build with gradle + Android SDK in a single container
docker run --rm \
  -v "$PWD":/project \
  -v gradle-cache:/root/.gradle \
  -w /project \
  mingc/android-build-box:latest \
  bash -lc "
    # Generate gradle wrapper if missing
    if [[ ! -f gradlew ]]; then
      gradle wrapper --gradle-version 8.7
    fi
    chmod +x gradlew
    ./gradlew assembleRelease --no-daemon --console=plain
  "

echo ""
echo "=== Build artifacts ==="
find app/build/outputs/apk -name '*.apk' -exec ls -lh {} \;

# Sign the release APK with the debug keystore (for sideloading)
APK_UNSIGNED=app/build/outputs/apk/release/app-release-unsigned.apk
APK_SIGNED=app/build/outputs/apk/release/smssaas-release.apk
if [[ -f "$APK_UNSIGNED" ]]; then
  docker run --rm -v "$PWD":/work -w /work \
    mingc/android-build-box:latest \
    bash -c "
      \$ANDROID_HOME/build-tools/34.0.0/zipalign -v 4 $APK_UNSIGNED $APK_SIGNED.aligned
      \$ANDROID_HOME/build-tools/34.0.0/apksigner sign \
        --ks debug.keystore --ks-pass pass:android --key-pass pass:android \
        --out $APK_SIGNED $APK_SIGNED.aligned
      rm $APK_SIGNED.aligned
    "
  echo ""
  echo "=== Signed APK: $APK_SIGNED ==="
  ls -lh "$APK_SIGNED"
fi
