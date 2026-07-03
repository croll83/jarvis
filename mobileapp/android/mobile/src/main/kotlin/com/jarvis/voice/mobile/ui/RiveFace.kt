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
import app.rive.runtime.kotlin.core.SMIBoolean
import app.rive.runtime.kotlin.core.SMINumber
import app.rive.runtime.kotlin.core.SMITrigger
import com.jarvis.voice.mobile.core.RiveAvailability
import com.jarvis.voice.shared.HeadState
import kotlinx.coroutines.delay
import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.sin

/**
 * Volto animato Rive (Robocat / "Catbot"). Fallback su [JarvisFace] se manca il .riv o
 * se Rive non è disponibile.
 *
 * IMPORTANTE: firare un input inesistente fa CRASHARE nativamente (l'eccezione esplode
 * sul thread di render di Rive, non catturabile dal chiamante). Perciò enumeriamo gli
 * input REALI della state machine (`inputNames`) e spariamo SOLO quelli presenti.
 * I nomi disponibili vengono loggati (tag "RiveFace") per finalizzare la mappa.
 *
 * File: artboard "Catbot", state machine "State Machine". Robocat non ha un input
 * "livello voce" → listening/speaking usano un overlay Compose (waveform).
 */
private const val RIVE_ASSET = "jarvis_face.riv"
private const val STATE_MACHINE = "State Machine"

/** Miglior tentativo di trigger per stato; null = non firare (resta faccia corrente). */
private fun triggerFor(state: HeadState): String? = when (state) {
    HeadState.IDLE -> "Reset"
    HeadState.CONNECTING -> "Download"
    HeadState.LISTENING -> "Listening"  // nuova faccia dedicata (bool)
    HeadState.THINKING -> "Chat"
    HeadState.SPEAKING -> "Speaking"    // nuova faccia dedicata (bool)
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

    if (!hasRive || !RiveAvailability.ok) {
        JarvisFace(state, amplitude, onTap, modifier)
        return
    }

    val riveView = remember { mutableStateOf<RiveAnimationView?>(null) }

    // Fire al cambio di stato — SOLO se l'input esiste ED è un Trigger (fireState su un
    // Boolean/Number ricrasha nativamente sul thread di render, non catturabile).
    LaunchedEffect(state, riveView.value) {
        val v = riveView.value ?: return@LaunchedEffect
        // Attendi che la state machine sia istanziata (max ~1s).
        var sm = runCatching { v.stateMachines.firstOrNull() }.getOrNull()
        var tries = 0
        while (sm == null && tries < 20) {
            delay(50); tries++
            sm = runCatching { v.stateMachines.firstOrNull() }.getOrNull()
        }
        val machine = sm ?: return@LaunchedEffect
        val target = triggerFor(state) ?: return@LaunchedEffect

        // One-hot: attiva l'input del target e disattiva gli altri, usando il setter
        // corretto per il tipo reale di ciascun input (Boolean/Number/Trigger) → no crash.
        for (name in machine.inputNames) {
            val inp = runCatching { machine.input(name) }.getOrNull() ?: continue
            val on = name == target
            runCatching {
                when {
                    inp is SMIBoolean -> v.setBooleanState(STATE_MACHINE, name, on)
                    inp is SMINumber -> v.setNumberState(STATE_MACHINE, name, if (on) 1f else 0f)
                    inp is SMITrigger -> if (on) v.fireState(STATE_MACHINE, name)
                }
            }.onFailure { Log.w("RiveFace", "set '$name': ${it.message}") }
        }
        Log.i("RiveFace", "stato=$state faccia=$target | tipi=" + machine.inputNames.joinToString {
            val i = runCatching { machine.input(it) }.getOrNull()
            it + ":" + when {
                i is SMIBoolean -> "bool"; i is SMINumber -> "num"; i is SMITrigger -> "trig"; else -> "?"
            }
        })
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
            .size(320.dp)
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
            modifier = Modifier.size(230.dp),
        )

        // Overlay waveform: LISTENING (cyan, reattivo al mic) vs SPEAKING (verde, ritmico)
        if (state == HeadState.LISTENING || state == HeadState.SPEAKING) {
            val listening = state == HeadState.LISTENING
            val ringColor = if (listening) JarvisColors.cyan else JarvisColors.green
            Canvas(Modifier.matchParentSize()) {
                val c = Offset(size.width / 2f, size.height / 2f)
                val bars = 72
                val inner = size.minDimension * 0.38f    // appena fuori dall'avatar (230dp)
                val barMax = size.minDimension * 0.11f    // barre ben più alte
                val w = size.minDimension * 0.009f
                for (i in 0 until bars) {
                    val ang = i.toFloat() / bars * 2f * PI.toFloat()
                    val live = 0.55f + 0.45f * sin(wavePhase + i * 0.4f)
                    val level = if (listening) ampSmoothed
                                else (0.5f + 0.4f * sin(wavePhase * 2f)).coerceIn(0f, 1f)
                    val v = ((0.2f + 0.8f * level) * live).coerceIn(0f, 1f)
                    val len = barMax * v
                    val cosA = cos(ang); val sinA = sin(ang)
                    val p1 = Offset(c.x + cosA * inner, c.y + sinA * inner)
                    val p2 = Offset(c.x + cosA * (inner + len), c.y + sinA * (inner + len))
                    drawLine(ringColor.copy(alpha = 0.9f), p1, p2, strokeWidth = w, cap = StrokeCap.Round)
                }
            }
        }

        // Layer trasparente SOPRA Rive per catturare il tap: Rive consuma il touch
        // (cursor/mouse tracking), quindi il click va intercettato in cima.
        Box(
            Modifier
                .matchParentSize()
                .clickable(
                    interactionSource = remember { MutableInteractionSource() },
                    indication = null,
                ) { onTap() }
        )
    }
}
