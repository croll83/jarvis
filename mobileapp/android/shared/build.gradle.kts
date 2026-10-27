plugins {
    alias(libs.plugins.android.library)
    alias(libs.plugins.kotlin.android)
}

android {
    namespace = "com.jarvis.voice.shared"
    compileSdk = 36

    defaultConfig {
        // minSdk deve essere <= a quello di mobile (28) e wear (30): usiamo il più basso
        minSdk = 28
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    testOptions {
        // android.util.Log nei test JVM: no-op invece di "Method not mocked"
        unitTests.isReturnDefaultValues = true
    }
}

dependencies {
    implementation(libs.androidx.core.ktx)

    // Test JVM (framing hub, HubClient, resampler). org.json reale: quello di android.jar è uno stub.
    testImplementation(libs.junit)
    testImplementation(libs.org.json)
}
