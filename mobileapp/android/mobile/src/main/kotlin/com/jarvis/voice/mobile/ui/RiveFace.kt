package com.jarvis.voice.mobile.ui

import android.util.Log
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
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
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import app.rive.runtime.kotlin.RiveAnimationView
import com.jarvis.voice.shared.HeadState

/**
 * Volto animato Rive (Robocat / "Catbot"). Se `assets/jarvis_face.riv` non c'è,
 * fa fallback su [JarvisFace] (l'app resta funzionante).
 *
 * File Rive analizzato:
 *   artboard      = "Catbot"
 *   state machine = "State Machine"
 *   input (Trigger) = Chat, Error, Download, No Internet, Reset Face, Face to Center, Face Follow Cursor
 * I trigger sono one-shot → li spariamo SOLO al cambio di stato (LaunchedEffect(state)).
 * Robocat non ha un input "livello voce", quindi la waveform reattiva resta nel fallback.
 */
private const val RIVE_ASSET = "jarvis_face.riv"
private const val STATE_MACHINE = "State Machine"

/** Mappa il nostro HeadState sul trigger della faccia Robocat da attivare. */
private fun triggerFor(state: HeadState): String = when (state) {
    HeadState.IDLE -> "Reset Face"
    HeadState.CONNECTING -> "Download"
    HeadState.LISTENING -> "Chat"
    HeadState.THINKING -> "Download"
    HeadState.SPEAKING -> "Chat"
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

    // Fallback: nessun .riv → avatar/animazioni attuali (con waveform mic).
    if (!hasRive) {
        JarvisFace(state, amplitude, onTap, modifier)
        return
    }

    val riveView = remember { mutableStateOf<RiveAnimationView?>(null) }

    // Fire del trigger SOLO al cambio di stato (o quando la view è pronta).
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
    }
}
