plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
}

android {
    namespace = "com.jarvis.voice.mobile"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.jarvis.voice"
        minSdk = 28
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
    }

    buildFeatures {
        compose = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }
}

dependencies {
    implementation(project(":shared"))

    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    implementation(libs.androidx.lifecycle.service)

    // Compose
    implementation(libs.androidx.activity.compose)
    implementation(platform(libs.compose.bom))
    implementation(libs.compose.ui)
    implementation(libs.compose.ui.graphics)
    implementation(libs.compose.material3)
    implementation(libs.compose.ui.tooling.preview)
    debugImplementation(libs.compose.ui.tooling)

    // WebSocket
    implementation(libs.okhttp)

    // Coroutines + Play Services (Wearable Task.await())
    implementation(libs.kotlinx.coroutines.android)
    implementation(libs.kotlinx.coroutines.play.services)

    // Wearable Data Layer (relay verso lo watch)
    implementation(libs.play.services.wearable)

    // Home-screen widget
    implementation(libs.glance.appwidget)
    implementation(libs.glance.material3)

    // Opus decode (TTS downlink) — pure Java, no NDK
    implementation(libs.concentus)

    // Rive — avatar animato interattivo (state machine) per il volto centrale
    implementation(libs.rive.android)
}
