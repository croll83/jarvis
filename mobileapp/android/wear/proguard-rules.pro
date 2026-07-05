# ── Log stripping (release) ────────────────────────────────────────────────
# Rimuove i log verbosi in release; Log.w/Log.e restano per la diagnostica.
-assumenosideeffects class android.util.Log {
    public static int v(...);
    public static int d(...);
    public static int i(...);
}

# ── Wearable Data Layer (link col telefono) ────────────────────────────────
-keep class com.google.android.gms.wearable.** { *; }
-dontwarn com.google.android.gms.**

# ── Wear Tiles / ProtoLayout (usano reflection su builder e servizi) ───────
-keep class androidx.wear.tiles.** { *; }
-keep class androidx.wear.protolayout.** { *; }
-keep class * extends androidx.wear.tiles.TileService { *; }
-dontwarn androidx.wear.**

# ── Guava (Futures per le Tile) ────────────────────────────────────────────
-dontwarn com.google.common.**
-dontwarn java.lang.SafeVarargs
