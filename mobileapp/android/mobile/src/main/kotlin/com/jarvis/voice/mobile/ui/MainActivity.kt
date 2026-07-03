package com.jarvis.voice.mobile.ui

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
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
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Text
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import com.jarvis.voice.mobile.R
import com.jarvis.voice.mobile.config.JarvisConfig
import com.jarvis.voice.mobile.core.JarvisRuntime
import com.jarvis.voice.mobile.service.JarvisForegroundService
import com.jarvis.voice.shared.HeadState

/**
 * Client standalone del telefono + entry point. Un tap sul volto avvia il turno vocale
 * (stessa UX dello watch). Il widget in home lancia questa Activity con EXTRA_TAP.
 */
class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        JarvisRuntime.init(this)
        // Il foreground service (tipo microphone) va avviato SOLO dopo RECORD_AUDIO
        // (lo fa AppRoot nel callback dei permessi), altrimenti Android 14+ crasha.
        setContent {
            MaterialTheme(
                colorScheme = darkColorScheme(
                    primary = JarvisColors.cyan,
                    onPrimary = Color(0xFF04141C),
                    secondary = JarvisColors.purple,
                    background = JarvisColors.bgTop,
                    surface = JarvisColors.bgMid,
                    onSurface = JarvisColors.textPrimary,
                    onBackground = JarvisColors.textPrimary,
                )
            ) { AppRoot() }
        }
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
    val context = LocalContext.current
    val config = JarvisRuntime.config
    var showSettings by remember { mutableStateOf(!config.isConfigured) }

    val permLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { result ->
        if (result[Manifest.permission.RECORD_AUDIO] == true) {
            JarvisForegroundService.start(context)
        }
    }

    LaunchedEffect(Unit) {
        val micGranted = ContextCompat.checkSelfPermission(
            context, Manifest.permission.RECORD_AUDIO
        ) == PackageManager.PERMISSION_GRANTED
        if (micGranted) {
            JarvisForegroundService.start(context)
        } else {
            val perms = mutableListOf(Manifest.permission.RECORD_AUDIO)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                perms += Manifest.permission.POST_NOTIFICATIONS
            }
            permLauncher.launch(perms.toTypedArray())
        }
    }

    AuroraBackground {
        if (showSettings) {
            SettingsScreen(config = config, onDone = { showSettings = false })
        } else {
            HomeScreen(onOpenSettings = { showSettings = true })
        }
    }
}

@Composable
private fun HomeScreen(onOpenSettings: () -> Unit) {
    val controller = JarvisRuntime.controller
    val state by controller.state.collectAsState()
    val amplitude by controller.amplitude.collectAsState()

    Box(Modifier.fillMaxSize()) {
        // Barra superiore: wordmark + gear
        Row(
            Modifier.fillMaxWidth().padding(20.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                "JARVIS",
                color = JarvisColors.textPrimary,
                fontWeight = FontWeight.Bold,
                fontSize = 22.sp,
                letterSpacing = 4.sp,
            )
            IconButton(onClick = onOpenSettings) {
                Icon(
                    painterResource(R.drawable.ic_settings),
                    contentDescription = "Impostazioni",
                    tint = JarvisColors.textMuted,
                )
            }
        }

        // Volto + stato al centro
        Column(
            Modifier.fillMaxSize(),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center,
        ) {
            JarvisFace(
                state = state,
                amplitude = amplitude,
                onTap = {
                    controller.relayMode = false
                    controller.onTap()
                },
            )
            Spacer(Modifier.height(8.dp))
            Text(
                state.label(),
                color = state.labelColor(),
                fontSize = 17.sp,
                fontWeight = FontWeight.Medium,
            )
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

    Column(
        Modifier.fillMaxSize().padding(20.dp).verticalScroll(rememberScrollState()),
    ) {
        Spacer(Modifier.height(8.dp))
        Text("Impostazioni", color = JarvisColors.textPrimary,
            fontWeight = FontWeight.Bold, fontSize = 26.sp)
        Spacer(Modifier.height(18.dp))

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

        Text("device_id", color = JarvisColors.textPrimary,
            fontWeight = FontWeight.SemiBold, fontSize = 16.sp)
        Spacer(Modifier.height(4.dp))

        IdOption(
            selected = idMode == JarvisConfig.MODE_AUTO,
            onSelect = { idMode = JarvisConfig.MODE_AUTO },
            title = "MAC del dispositivo (automatico)",
            subtitle = autoId,
        )
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
            color = JarvisColors.textMuted,
            fontSize = 12.sp,
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
                colors = ButtonDefaults.buttonColors(containerColor = JarvisColors.cyan),
                modifier = Modifier.padding(start = 12.dp),
            ) { Text("Salva") }
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
            Text(title, color = JarvisColors.textPrimary, fontSize = 15.sp)
            Text(subtitle, color = JarvisColors.textMuted, fontSize = 12.sp)
        }
    }
}

private fun HeadState.label(): String = when (this) {
    HeadState.IDLE -> "Tocca il volto per parlare"
    HeadState.CONNECTING -> "Connessione…"
    HeadState.LISTENING -> "Ti ascolto…"
    HeadState.THINKING -> "Sto pensando…"
    HeadState.SPEAKING -> "Rispondo…"
    HeadState.ERROR -> "Connessione persa"
}

private fun HeadState.labelColor(): Color = when (this) {
    HeadState.ERROR -> JarvisColors.red
    HeadState.IDLE -> JarvisColors.textMuted
    HeadState.CONNECTING -> JarvisColors.gold
    else -> JarvisColors.cyanSoft
}
