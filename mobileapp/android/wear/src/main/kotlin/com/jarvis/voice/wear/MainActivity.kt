package com.jarvis.voice.wear

import android.Manifest
import android.app.Activity
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
        // Schermo acceso all'avvio (parte subito in ascolto); poi WearRoot lo gestisce
        // per stato: acceso solo quando serve interagire, rilasciato in IDLE/ERROR
        // così l'OLED può spegnersi e non drena la batteria a sessione finita.
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

    // Batteria: tieni lo schermo acceso SOLO durante l'interazione attiva; rilascialo
    // in IDLE/ERROR così l'OLED può spegnersi (il flag da solo teneva acceso all'infinito).
    val window = (context as? Activity)?.window
    LaunchedEffect(state) {
        val keepOn = when (state) {
            HeadState.CONNECTING, HeadState.LISTENING,
            HeadState.THINKING, HeadState.SPEAKING -> true
            HeadState.IDLE, HeadState.ERROR -> false
        }
        window?.apply {
            if (keepOn) addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            else clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        }
    }

    // Autochiusura: torna IDLE dopo essere stata attiva (fine risposta), oppure resta in
    // ERROR → chiudi dopo un attimo per non lasciare l'Activity (e lo schermo) accesa.
    var wasActive by remember { mutableStateOf(false) }
    LaunchedEffect(state) {
        if (state != HeadState.IDLE && state != HeadState.CONNECTING && state != HeadState.ERROR) {
            wasActive = true
        }
        when {
            wasActive && state == HeadState.IDLE -> {
                delay(700)
                if (link.state.value == HeadState.IDLE) onClose()
            }
            state == HeadState.ERROR -> {
                delay(2500)
                if (link.state.value == HeadState.ERROR) onClose()
            }
        }
    }

    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        WearFace(state = state, amplitude = amp, onTap = { link.tap() })
    }
}
