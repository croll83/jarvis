# Distribuzione JARVIS mobile (Android + Wear OS)

App = **due APK** con lo stesso `applicationId` (`com.jarvis.voice`): una per il
telefono (`:mobile`) e una per il watch (`:wear`). **Vanno firmate con lo stesso
keystore** — Wear OS accoppia le due app solo se hanno lo stesso certificato.

## 1. Crea il keystore (UNA volta sola)

Dalla cartella `mobileapp/android/`:

```bash
keytool -genkeypair -v \
  -keystore jarvis-release.keystore \
  -alias jarvis \
  -keyalg RSA -keysize 2048 -validity 10000 \
  -storepass 'LA_TUA_PASSWORD' -keypass 'LA_TUA_PASSWORD' \
  -dname "CN=JARVIS, O=Casa, C=IT"
```

⚠️ **Conserva keystore + password per sempre**: se li perdi non potrai più
aggiornare l'app installata (dovrai disinstallare/reinstallare da zero).

Poi crea `keystore.properties` (copia dal template) con i valori reali:

```bash
cp keystore.properties.template keystore.properties
# poi edita storePassword / keyPassword
```

Sia `jarvis-release.keystore` che `keystore.properties` sono già in `.gitignore`.

## 2. Genera le APK di release firmate

```bash
./gradlew :mobile:assembleRelease :wear:assembleRelease
```

Output:
- Telefono: `mobile/build/outputs/apk/release/mobile-release.apk`
- Watch:    `wear/build/outputs/apk/release/wear-release.apk`

(La release usa R8: minify + shrink risorse + strip dei log verbosi. `Log.w`/`Log.e`
restano per la diagnostica.)

## 3. Installa

⚠️ **Prima installazione**: se hai già il build di *debug* installato (quello di
Android Studio in questi giorni di test), disinstallalo prima — ha un certificato
diverso e l'installazione della release fallirebbe con *signature mismatch*:
```bash
adb uninstall com.jarvis.voice           # sul telefono
adb -s <IP_DEL_WATCH>:5555 uninstall com.jarvis.voice   # sul watch
```
Dalla release in poi gli aggiornamenti si installano sopra senza disinstallare
(stessa firma), basta incrementare `versionCode`.

**Telefono** (via USB o Wi-Fi debug):
```bash
adb install -r mobile/build/outputs/apk/release/mobile-release.apk
```

**Watch** (Galaxy Watch via Wi-Fi debug — abilita Opzioni sviluppatore + Debug ADB
+ Debug via Wi-Fi sull'orologio, poi):
```bash
adb connect <IP_DEL_WATCH>:5555
adb -s <IP_DEL_WATCH>:5555 install -r wear/build/outputs/apk/release/wear-release.apk
```
Se hai più device collegati usa `-s <serial>` per scegliere quello giusto.

## 4. Aggiornamenti futuri

Ad ogni release incrementa `versionCode` (e volendo `versionName`) in
`mobile/build.gradle.kts` e `wear/build.gradle.kts`, poi ripeti lo step 2.

## Play Store (opzionale, internal testing)

In alternativa al sideload: `./gradlew :mobile:bundleRelease` genera un `.aab` da
caricare su Play Console → traccia *Internal testing* (privata, fino a 100 tester).
Il watch può essere pubblicato nello stesso listing. Richiede account Play Developer
(una tantum $25). Per il sideload personale NON serve.

## Nota se R8 rompe qualcosa

`isMinifyEnabled = true` è attivo in release. Se dopo il build una funzione crasha
(sospetti principali: volto Rive, Tile), aggiungi la classe alle keep-rules in
`mobile/proguard-rules.pro` / `wear/proguard-rules.pro`. Per sbloccarti in fretta puoi
temporaneamente mettere `isMinifyEnabled = false` in quel modulo e ricostruire.
