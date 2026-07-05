# ── Log stripping (release) ────────────────────────────────────────────────
# Rimuove i log verbosi in release; Log.w/Log.e restano per la diagnostica.
-assumenosideeffects class android.util.Log {
    public static int v(...);
    public static int d(...);
    public static int i(...);
}

# ── Rive (runtime nativo + JNI + reflection) ───────────────────────────────
# L'AAR porta consumer-rules, ma teniamo esplicitamente per sicurezza.
-keep class app.rive.runtime.** { *; }
-keep class com.getkeepsafe.relinker.** { *; }
-dontwarn app.rive.runtime.**

# ── Concentus (decoder Opus, puro Java) ────────────────────────────────────
-keep class io.github.jaredmdobson.concentus.** { *; }

# ── OkHttp / Okio ──────────────────────────────────────────────────────────
-dontwarn okhttp3.**
-dontwarn okio.**
-dontwarn org.conscrypt.**

# ── Wearable Data Layer (relay verso il watch) ─────────────────────────────
-keep class com.google.android.gms.wearable.** { *; }
-dontwarn com.google.android.gms.**

# org.json fa parte del framework Android: nessuna regola necessaria.
