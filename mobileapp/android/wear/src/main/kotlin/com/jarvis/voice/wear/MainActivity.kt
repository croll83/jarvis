package com.jarvis.voice.wear

import android.Manifest
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.wear.compose.material.MaterialTheme

/**
 * Entry point Wear OS. Mostra la testa robot; tap = avvia il turno (via PhoneLink).
 * Lanciata dalla Tile o dal launcher.
 */
class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent { MaterialTheme { WearRoot() } }
    }
}

@Composable
private fun WearRoot() {
    val context = androidx.compose.ui.platform.LocalContext.current
    val link = remember { PhoneLink(context) }
    val state by link.state.collectAsState()

    val permLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { /* on-demand */ }

    LaunchedEffect(Unit) {
        permLauncher.launch(Manifest.permission.RECORD_AUDIO)
        link.start()
    }
    DisposableEffect(Unit) { onDispose { link.stop() } }

    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        RobotHeadScreen(state = state, onTap = { link.tap() })
    }
}
