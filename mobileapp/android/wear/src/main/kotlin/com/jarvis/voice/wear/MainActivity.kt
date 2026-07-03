package com.jarvis.voice.wear

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.core.content.ContextCompat
import androidx.wear.compose.material.MaterialTheme
import com.jarvis.voice.shared.HeadState
import kotlinx.coroutines.delay

/**
 * Entry point Wear OS. Lanciata dalla Tile: parte GIÀ in ascolto e si AUTOCHIUDE a fine
 * risposta (torna IDLE dopo essere stata attiva) — tranne in multiturn/live session,
 * dove lo stato resta attivo e quindi non chiude.
 */
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Tieni lo schermo acceso durante l'interazione vocale (niente ambient/standby
        // che nascondeva l'UI con l'overlay nero durante risposte lunghe).
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        setContent { MaterialTheme { WearRoot(onClose = { finish() }) } }
    }
}

@Composable
private fun WearRoot(onClose: () -> Unit) {
    val context = LocalContext.current
    val link = remember { PhoneLink(context) }
    val state by link.state.collectAsState()
    val amp by link.amplitude.collectAsState()

    val permLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted -> if (granted) link.tap() }

    LaunchedEffect(Unit) {
        link.start()
        val granted = ContextCompat.checkSelfPermission(
            context, Manifest.permission.RECORD_AUDIO
        ) == PackageManager.PERMISSION_GRANTED
        if (granted) link.tap()                      // auto-ascolto all'avvio
        else permLauncher.launch(Manifest.permission.RECORD_AUDIO)
    }
    DisposableEffect(Unit) { onDispose { link.stop() } }

    // Autochiusura: quando torna IDLE dopo essere stata attiva (fine risposta).
    var wasActive by remember { mutableStateOf(false) }
    LaunchedEffect(state) {
        if (state != HeadState.IDLE && state != HeadState.CONNECTING) wasActive = true
        if (wasActive && state == HeadState.IDLE) {
            delay(700)
            if (link.state.value == HeadState.IDLE) onClose()
        }
    }

    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        WearFace(state = state, amplitude = amp, onTap = { link.tap() })
    }
}
