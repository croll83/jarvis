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
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
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
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.ui.unit.dp
import com.jarvis.voice.mobile.config.JarvisConfig
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

        setContent { MaterialTheme { AppRoot() } }
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
private fun AppRoot() {
    val config = JarvisRuntime.config
    var showSettings by remember { mutableStateOf(!config.isConfigured) }

    val permLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { /* risultato gestito on-demand */ }

    LaunchedEffect(Unit) {
        val perms = mutableListOf(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            perms += Manifest.permission.POST_NOTIFICATIONS
        }
        permLauncher.launch(perms.toTypedArray())
    }

    if (showSettings) {
        SettingsScreen(config = config, onDone = { showSettings = false })
    } else {
        HomeScreen(onOpenSettings = { showSettings = true })
    }
}

@Composable
private fun HomeScreen(onOpenSettings: () -> Unit) {
    val controller = JarvisRuntime.controller
    val state by controller.state.collectAsState()

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

            TextButton(onClick = onOpenSettings, modifier = Modifier.padding(top = 8.dp)) {
                Text("⚙︎  Impostazioni")
            }
        }
    }
}

@Composable
private fun SettingsScreen(config: JarvisConfig, onDone: () -> Unit) {
    var url by remember { mutableStateOf(config.url) }
    var token by remember { mutableStateOf(config.token) }
    var ttlMin by remember { mutableStateOf((config.ttlMillis / 60_000L).toString()) }
    var idMode by remember { mutableStateOf(config.deviceIdMode) }
    var customId by remember { mutableStateOf(config.customDeviceId) }
    val autoId = remember { config.autoDeviceId }

    Surface(Modifier.fillMaxSize()) {
        Column(
            Modifier.fillMaxSize().padding(20.dp).verticalScroll(rememberScrollState()),
        ) {
            Text("Impostazioni orchestrator", style = MaterialTheme.typography.headlineSmall)
            Spacer(Modifier.height(16.dp))

            OutlinedTextField(
                value = url, onValueChange = { url = it },
                label = { Text("URL (ws://ip:5000)") },
                singleLine = true, modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(12.dp))

            OutlinedTextField(
                value = token, onValueChange = { token = it },
                label = { Text("Token (DEVICE_API_TOKEN)") },
                singleLine = true,
                visualTransformation = PasswordVisualTransformation(),
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(12.dp))

            OutlinedTextField(
                value = ttlMin, onValueChange = { ttlMin = it.filter { c -> c.isDigit() } },
                label = { Text("TTL connessione (minuti)") },
                singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(20.dp))

            Text("device_id", style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(4.dp))

            // Opzione 1: MAC del dispositivo (automatico)
            IdOption(
                selected = idMode == JarvisConfig.MODE_AUTO,
                onSelect = { idMode = JarvisConfig.MODE_AUTO },
                title = "MAC del dispositivo (automatico)",
                subtitle = autoId,
            )
            // Opzione 2: personalizzato
            IdOption(
                selected = idMode == JarvisConfig.MODE_CUSTOM,
                onSelect = { idMode = JarvisConfig.MODE_CUSTOM },
                title = "Personalizzato",
                subtitle = "Inserisci un device_id (es. lo stesso dello spike)",
            )
            if (idMode == JarvisConfig.MODE_CUSTOM) {
                OutlinedTextField(
                    value = customId, onValueChange = { customId = it.uppercase() },
                    label = { Text("device_id personalizzato") },
                    singleLine = true, modifier = Modifier.fillMaxWidth().padding(start = 32.dp),
                )
            }

            Text(
                "Android non espone il MAC hardware reale: l'automatico è un ID stabile del " +
                    "dispositivo in formato MAC.",
                style = MaterialTheme.typography.bodySmall,
                modifier = Modifier.padding(top = 8.dp),
            )

            Spacer(Modifier.height(24.dp))
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                OutlinedButton(onClick = onDone) { Text("Annulla") }
                Button(
                    onClick = {
                        config.url = url
                        config.token = token
                        config.deviceIdMode = idMode
                        if (idMode == JarvisConfig.MODE_CUSTOM) config.customDeviceId = customId
                        ttlMin.toLongOrNull()?.let { if (it > 0) config.ttlMillis = it * 60_000L }
                        onDone()
                    },
                    modifier = Modifier.padding(start = 12.dp),
                ) { Text("Salva") }
            }
        }
    }
}

@Composable
private fun IdOption(selected: Boolean, onSelect: () -> Unit, title: String, subtitle: String) {
    Row(
        Modifier
            .fillMaxWidth()
            .selectable(selected = selected, onClick = onSelect)
            .padding(vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        RadioButton(selected = selected, onClick = onSelect)
        Column(Modifier.padding(start = 4.dp)) {
            Text(title, style = MaterialTheme.typography.bodyLarge)
            Text(subtitle, style = MaterialTheme.typography.bodySmall)
        }
    }
}

private fun HeadState.label(): String = when (this) {
    HeadState.IDLE -> "Tocca per parlare"
    HeadState.CONNECTING -> "Connessione…"
    HeadState.LISTENING -> "Ti ascolto…"
    HeadState.THINKING -> "Sto pensando…"
    HeadState.SPEAKING -> "…"
}
