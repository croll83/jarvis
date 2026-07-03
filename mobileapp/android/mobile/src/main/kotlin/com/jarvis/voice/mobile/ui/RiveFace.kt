package com.jarvis.voice.mobile.ui

import android.util.Log
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.size
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import app.rive.runtime.kotlin.RiveAnimationView
import com.jarvis.voice.mobile.core.RiveAvailability
import com.jarvis.voice.shared.HeadState
import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.sin

/**
 * Volto animato Rive (Robocat / "Catbot"). Se `assets/jarvis_face.riv` non c'è,
 * fa fallback su [JarvisFace].
 *
 * File Rive: artboard "Catbot", state machine "State Machine", trigger inputs
 * (Chat/Error/Download/No Internet/Reset Face/...).
 *
 * Il Robocat ha solo 4 facce oltre a idle e nessun input "voce", quindi listening e
 * speaking userebbero la stessa faccia. Per distinguerli aggiungiamo un OVERLAY Compose
 * sopra l'avatar: anello-waveform cyan reattivo al mic (listening) e anello verde
 * ritmico (speaking). Nessuna modifica al .riv necessaria.
 */
private const val RIVE_ASSET = "jarvis_face.riv"
private const val STATE_MACHINE = "State Machine"

/** Faccia Rive per ogni stato (listening/speaking restano su idle + overlay Compose). */
private fun triggerFor(state: HeadState): String = when (state) {
    HeadState.IDLE -> "Reset Face"
    HeadState.CONNECTING -> "Download"
    HeadState.LISTENING -> "Reset Face"   // idle + overlay waveform mic
    HeadState.THINKING -> "Chat"
    HeadState.SPEAKING -> "Reset Face"    // idle + overlay parlato
    HeadState.ERROR -> "Error"
}

@Composable
fun RiveFace(
    state: HeadState,
    amplitude: Float,
    onTap: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val context = LocalContext.current
    val hasRive = remember {
        runCatching { context.assets.open(RIVE_ASSET).close(); true }.getOrDefault(false)
    }

    // Fallback se il .riv manca O se Rive non si è inizializzato (native non caricabili).
    if (!hasRive || !RiveAvailability.ok) {
        JarvisFace(state, amplitude, onTap, modifier)
        return
    }

    val riveView = remember { mutableStateOf<RiveAnimationView?>(null) }

    // Fire del trigger SOLO al cambio di stato (i trigger sono one-shot).
    LaunchedEffect(state, riveView.value) {
        val v = riveView.value ?: return@LaunchedEffect
        runCatching { v.fireState(STATE_MACHINE, triggerFor(state)) }
            .onFailure { Log.w("RiveFace", "fireState(${triggerFor(state)}): ${it.message}") }
    }

    val accent by animateColorAsState(
        targetValue = when (state) {
            HeadState.IDLE -> JarvisColors.cyan.copy(alpha = 0.45f)
            HeadState.CONNECTING -> JarvisColors.gold
            HeadState.LISTENING -> JarvisColors.cyan
            HeadState.THINKING -> JarvisColors.purple
            HeadState.SPEAKING -> JarvisColors.green
            HeadState.ERROR -> JarvisColors.red
        },
        animationSpec = tween(400), label = "accent",
    )
    val t = rememberInfiniteTransition(label = "rivfx")
    val glow by t.animateFloat(
        0.3f, 0.65f, infiniteRepeatable(tween(1900, easing = LinearEasing), RepeatMode.Reverse), "glow",
    )
    val wavePhase by t.animateFloat(
        0f, (2 * PI).toFloat(), infiniteRepeatable(tween(1400, easing = LinearEasing)), "wave",
    )
    val ampSmoothed by animateFloatAsState(amplitude, tween(90), label = "amp")

    Box(
        modifier = modifier
            .size(300.dp)
            .clickable(
                interactionSource = remember { MutableInteractionSource() },
                indication = null,
            ) { onTap() }
            .drawBehind {
                val c = Offset(size.width / 2f, size.height / 2f)
                val r = size.minDimension * 0.5f
                drawCircle(
                    brush = Brush.radialGradient(
                        listOf(accent.copy(alpha = glow * 0.45f), Color.Transparent),
                        center = c, radius = r,
                    ),
                    radius = r, center = c,
                )
            },
        contentAlignment = Alignment.Center,
    ) {
        // Avatar Rive
        AndroidView(
            factory = { ctx ->
                RiveAnimationView(ctx).also { view ->
                    runCatching {
                        val bytes = ctx.assets.open(RIVE_ASSET).readBytes()
                        view.setRiveBytes(bytes, stateMachineName = STATE_MACHINE, autoplay = true)
                    }.onFailure { Log.w("RiveFace", "load .riv: ${it.message}") }
                    riveView.value = view
                }
            },
            modifier = Modifier.size(280.dp),
        )

        // Overlay waveform: distingue LISTENING (cyan, reattivo al mic) da SPEAKING (verde, ritmico)
        if (state == HeadState.LISTENING || state == HeadState.SPEAKING) {
            val listening = state == HeadState.LISTENING
            val ringColor = if (listening) JarvisColors.cyan else JarvisColors.green
            Canvas(Modifier.size(300.dp)) {
                val c = Offset(size.width / 2f, size.height / 2f)
                val bars = 64
                val inner = size.minDimension * 0.43f   // anello sul bordo esterno, non copre il volto
                val barMax = size.minDimension * 0.06f
                val w = size.minDimension * 0.007f
                for (i in 0 until bars) {
                    val ang = i.toFloat() / bars * 2f * PI.toFloat()
                    val live = 0.6f + 0.4f * sin(wavePhase + i * 0.4f)
                    val level = if (listening) ampSmoothed
                                else (0.45f + 0.35f * sin(wavePhase * 2f)).coerceIn(0f, 1f)
                    val v = ((0.15f + 0.85f * level) * live).coerceIn(0f, 1f)
                    val len = barMax * v
                    val cosA = cos(ang); val sinA = sin(ang)
                    val p1 = Offset(c.x + cosA * inner, c.y + sinA * inner)
                    val p2 = Offset(c.x + cosA * (inner + len), c.y + sinA * (inner + len))
                    drawLine(ringColor.copy(alpha = 0.9f), p1, p2, strokeWidth = w, cap = StrokeCap.Round)
                }
            }
        }
    }
}
