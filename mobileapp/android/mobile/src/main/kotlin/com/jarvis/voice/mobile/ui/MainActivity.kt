package com.jarvis.voice.mobile.ui

import android.Manifest
import android.content.Intent
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.jarvis.voice.mobile.core.JarvisRuntime
import com.jarvis.voice.mobile.service.JarvisForegroundService
import com.jarvis.voice.shared.HeadState

/**
 * Client standalone del telefono + entry point. Un tap sulla testa avvia il turno vocale
 * (stessa UX dello watch). Il widget in home lancia questa Activity con EXTRA_TAP.
 */
class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        JarvisRuntime.init(this)
        JarvisForegroundService.start(this)

        setContent { MaterialTheme { JarvisScreen() } }
        maybeAutoTap(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        maybeAutoTap(intent)
    }

    private fun maybeAutoTap(intent: Intent?) {
        if (intent?.getBooleanExtra(EXTRA_TAP, false) == true) {
            JarvisRuntime.controller.relayMode = false
            JarvisRuntime.controller.onTap()
        }
    }

    companion object {
        const val EXTRA_TAP = "auto_tap"
    }
}

@Composable
private fun JarvisScreen() {
    val controller = JarvisRuntime.controller
    val config = JarvisRuntime.config
    val state by controller.state.collectAsState()

    var showSettings by remember { mutableStateOf(!config.isConfigured) }

    val permLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { /* risultato ignorato: gestito on-demand */ }

    LaunchedEffect(Unit) {
        val perms = mutableListOf(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            perms += Manifest.permission.POST_NOTIFICATIONS
        }
        permLauncher.launch(perms.toTypedArray())
    }

    Surface(Modifier.fillMaxSize()) {
        Column(
            Modifier.fillMaxSize().padding(24.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center,
        ) {
            RobotHead(state)
            Text(state.label(), Modifier.padding(top = 16.dp), style = MaterialTheme.typography.titleMedium)

            Button(
                onClick = {
                    controller.relayMode = false
                    controller.onTap()
                },
                modifier = Modifier.padding(top = 24.dp),
            ) { Text(if (state == HeadState.LISTENING) "Invia (2° tap)" else "Parla") }

            TextButton(onClick = { showSettings = true }, modifier = Modifier.padding(top = 8.dp)) {
                Text("Impostazioni orchestrator")
            }
        }
    }

    if (showSettings) {
        SettingsDialog(
            initialUrl = config.url,
            initialToken = config.token,
            initialDeviceId = config.deviceId,
            onDismiss = { showSettings = false },
            onSave = { url, token, deviceId ->
                config.url = url
                config.token = token
                if (deviceId.isNotBlank()) config.deviceId = deviceId
                showSettings = false
            },
        )
    }
}

@Composable
private fun SettingsDialog(
    initialUrl: String,
    initialToken: String,
    initialDeviceId: String,
    onDismiss: () -> Unit,
    onSave: (String, String, String) -> Unit,
) {
    var url by remember { mutableStateOf(initialUrl) }
    var token by remember { mutableStateOf(initialToken) }
    var deviceId by remember { mutableStateOf(initialDeviceId) }

    androidx.compose.material3.AlertDialog(
        onDismissRequest = onDismiss,
        confirmButton = { TextButton(onClick = { onSave(url, token, deviceId) }) { Text("Salva") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Annulla") } },
        title = { Text("Orchestrator") },
        text = {
            Column {
                OutlinedTextField(url, { url = it }, label = { Text("URL (ws://ip:5000)") })
                OutlinedTextField(token, { token = it }, label = { Text("Token") })
                OutlinedTextField(deviceId, { deviceId = it }, label = { Text("device_id (MAC)") })
            }
        },
    )
}

private fun HeadState.label(): String = when (this) {
    HeadState.IDLE -> "Tocca per parlare"
    HeadState.CONNECTING -> "Connessione…"
    HeadState.LISTENING -> "Ti ascolto…"
    HeadState.THINKING -> "Sto pensando…"
    HeadState.SPEAKING -> "…"
}
