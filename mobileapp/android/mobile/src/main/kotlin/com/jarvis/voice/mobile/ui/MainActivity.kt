package com.jarvis.voice.mobile.ui

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
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
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.ui.draw.clip
import com.jarvis.voice.mobile.ws.DialogTurn
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
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import com.jarvis.voice.mobile.R
import com.jarvis.voice.mobile.core.RiveAvailability
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
        RiveAvailability.tryInit(this)  // init Rive guardato; se fallisce RiveFace usa il fallback
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
    var micDenied by remember { mutableStateOf(false) }

    val permLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { result ->
        if (result[Manifest.permission.RECORD_AUDIO] == true) {
            micDenied = false
            JarvisForegroundService.start(context)
        } else {
            micDenied = true
        }
    }

    LaunchedEffect(Unit) {
        val micGranted = ContextCompat.checkSelfPermission(
            context, Manifest.permission.RECORD_AUDIO
        ) == PackageManager.PERMISSION_GRANTED
        if (micGranted) {
            micDenied = false
            JarvisForegroundService.start(context)
        } else {
            val perms = mutableListOf(Manifest.permission.RECORD_AUDIO)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                perms += Manifest.permission.POST_NOTIFICATIONS
            }
            permLauncher.launch(perms.toTypedArray())
        }
    }

    // Apre la schermata di dettaglio app di sistema (funziona anche dopo "non chiedere più").
    val openAppSettings = {
        val intent = Intent(
            Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
            Uri.fromParts("package", context.packageName, null),
        ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        context.startActivity(intent)
    }

    AuroraBackground {
        if (showSettings) {
            SettingsScreen(config = config, onDone = { showSettings = false })
        } else {
            ResponsiveHome(
                onOpenSettings = { showSettings = true },
                micDenied = micDenied,
                onFixPermission = openAppSettings,
            )
        }
    }
}

/**
 * Layout responsive: schermo largo (Fold aperto / tablet / iPad) → due pannelli
 * (storico a sinistra, avatar+stato a destra). Schermo stretto → vista singola.
 */
@Composable
private fun ResponsiveHome(
    onOpenSettings: () -> Unit,
    micDenied: Boolean = false,
    onFixPermission: () -> Unit = {},
) {
    BoxWithConstraints(Modifier.fillMaxSize()) {
        if (maxWidth >= 720.dp) {
            Row(Modifier.fillMaxSize()) {
                HistoryPane(Modifier.weight(1f).fillMaxHeight())
                Box(Modifier.width(1.dp).fillMaxHeight().background(JarvisColors.panelBorder))
                Box(Modifier.weight(1.1f).fillMaxHeight()) {
                    HomeScreen(onOpenSettings, micDenied, onFixPermission)
                }
            }
        } else {
            HomeScreen(onOpenSettings, micDenied, onFixPermission)
        }
    }
}

@Composable
private fun HistoryPane(modifier: Modifier = Modifier) {
    // Storico UNIFICATO: turni del telefono (📱) + del watch (⌚), in ordine cronologico.
    val phone by JarvisRuntime.controller.history.collectAsState()
    val watch by JarvisRuntime.relayController.history.collectAsState()
    val merged = remember(phone, watch) {
        (phone.map { it to "📱" } + watch.map { it to "⌚" })
            .sortedBy { it.first.ts }
            .takeLast(20)
    }
    val listState = rememberLazyListState()
    LaunchedEffect(merged.size) {
        if (merged.isNotEmpty()) listState.animateScrollToItem(merged.size - 1)
    }
    Column(modifier.statusBarsPadding().padding(horizontal = 20.dp, vertical = 16.dp)) {
        Text("Conversazione", color = JarvisColors.textPrimary,
            fontWeight = FontWeight.Bold, fontSize = 20.sp)
        Spacer(Modifier.height(12.dp))
        if (merged.isEmpty()) {
            Text(
                "Nessuna conversazione ancora.\nTocca il volto e parla.",
                color = JarvisColors.textMuted, fontSize = 14.sp,
            )
        } else {
            LazyColumn(
                state = listState,
                modifier = Modifier.fillMaxSize(),
                verticalArrangement = Arrangement.spacedBy(14.dp),
            ) {
                items(merged) { (turn, src) -> TurnCard(turn, src) }
            }
        }
    }
}

@Composable
private fun TurnCard(turn: DialogTurn, source: String = "") {
    Column(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(16.dp))
            .background(JarvisColors.panel)
            .padding(14.dp),
    ) {
        turn.user?.let {
            Text(if (source.isNotEmpty()) "$source  TU" else "TU",
                color = JarvisColors.cyan, fontSize = 11.sp,
                fontWeight = FontWeight.Bold, letterSpacing = 1.5.sp)
            Text(it, color = JarvisColors.textPrimary, fontSize = 15.sp)
        }
        turn.jarvis?.let {
            if (turn.user != null) Spacer(Modifier.height(10.dp))
            Text("JARVIS", color = JarvisColors.green, fontSize = 11.sp,
                fontWeight = FontWeight.Bold, letterSpacing = 1.5.sp)
            Text(it, color = JarvisColors.textPrimary.copy(alpha = 0.92f), fontSize = 15.sp)
        }
    }
}

@Composable
private fun HomeScreen(
    onOpenSettings: () -> Unit,
    micDenied: Boolean = false,
    onFixPermission: () -> Unit = {},
) {
    val controller = JarvisRuntime.controller
    val state by controller.state.collectAsState()
    val amplitude by controller.amplitude.collectAsState()
    val errorMsg by controller.statusMessage.collectAsState()

    Box(Modifier.fillMaxSize()) {
        // Barra superiore: wordmark + gear
        Row(
            Modifier.fillMaxWidth().statusBarsPadding().padding(horizontal = 20.dp, vertical = 12.dp),
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
            RiveFace(
                state = state,
                amplitude = amplitude,
                onTap = {
                    controller.relayMode = false
                    controller.onTap()
                },
            )
            Spacer(Modifier.height(8.dp))
            Text(
                text = if (state == HeadState.ERROR && !errorMsg.isNullOrBlank()) errorMsg!!
                       else state.label(),
                color = state.labelColor(),
                fontSize = 17.sp,
                fontWeight = FontWeight.Medium,
                textAlign = TextAlign.Center,
                modifier = Modifier.padding(horizontal = 28.dp),
            )
        }

        // Banner permesso microfono negato: senza mic l'app non può funzionare.
        if (micDenied) {
            Box(
                Modifier.align(Alignment.BottomCenter).fillMaxWidth().padding(20.dp)
                    .clip(RoundedCornerShape(14.dp)).background(JarvisColors.red.copy(alpha = 0.16f))
                    .selectable(selected = false, onClick = onFixPermission).padding(16.dp),
            ) {
                Text(
                    "Permesso microfono negato. Tocca per abilitarlo nelle impostazioni di sistema.",
                    color = JarvisColors.red, fontSize = 14.sp,
                )
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
    var wearId by remember { mutableStateOf(config.wearDeviceId) }

    Column(
        Modifier.fillMaxSize().statusBarsPadding().padding(20.dp).verticalScroll(rememberScrollState()),
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

        Spacer(Modifier.height(18.dp))
        Text("device_id Wear (watch)", color = JarvisColors.textPrimary,
            fontWeight = FontWeight.SemiBold, fontSize = 16.sp)
        Spacer(Modifier.height(4.dp))
        OutlinedTextField(
            value = wearId, onValueChange = { wearId = it.uppercase() },
            label = { Text("device_id Wear") },
            singleLine = true, modifier = Modifier.fillMaxWidth(),
        )
        Text(
            "Il watch è un device separato lato orchestrator: configuralo in dashboard con " +
                "use_internal_speaker=true (altrimenti niente TTS sul watch).",
            color = JarvisColors.textMuted, fontSize = 12.sp, modifier = Modifier.padding(top = 6.dp),
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
                    if (wearId.isNotBlank()) config.wearDeviceId = wearId
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
