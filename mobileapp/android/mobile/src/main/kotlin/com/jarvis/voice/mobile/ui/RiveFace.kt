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
import androidx.compose.runtime.getValue
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
 * Volto animato basato su Rive (state machine). Se il file `.riv` non è presente in
 * assets/, fa fallback automatico su [JarvisFace] (l'app resta funzionante).
 *
 * SETUP (una volta): esporta il file Rive (es. "Robocat" da rive.app) come .riv e
 * mettilo in  mobile/src/main/assets/jarvis_face.riv .
 * Poi comunica i nomi reali (visibili nell'editor Rive → pannello State Machine) di:
 *   - STATE_MACHINE (nome della state machine)
 *   - gli input che pilotano gli stati
 * e affiniamo la mappatura in [applyRiveInputs]. Finché non combaciano, l'animazione
 * riproduce comunque il suo stato di default (autoplay) — nessun crash.
 */
private const val RIVE_ASSET = "jarvis_face.riv"
private const val STATE_MACHINE = "State Machine 1"   // TODO: nome reale dal file Rive

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

    // Fallback: nessun .riv → usa l'avatar/animazioni attuali.
    if (!hasRive) {
        JarvisFace(state, amplitude, onTap, modifier)
        return
    }

    val accent by animateColorAsState(
        targetValue = when (state) {
            HeadState.IDLE -> JarvisColors.cyan.copy(alpha = 0.5f)
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
        0.3f, 0.7f, infiniteRepeatable(tween(1900, easing = LinearEasing), RepeatMode.Reverse), "glow",
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
                        listOf(accent.copy(alpha = glow * 0.5f), Color.Transparent),
                        center = c, radius = r,
                    ),
                    radius = r, center = c,
                )
            },
        contentAlignment = Alignment.Center,
    ) {
        AndroidView(
            factory = { ctx ->
                RiveAnimationView(ctx).apply {
                    runCatching {
                        val bytes = ctx.assets.open(RIVE_ASSET).readBytes()
                        setRiveBytes(bytes, stateMachineName = STATE_MACHINE, autoplay = true)
                    }.onFailure { Log.w("RiveFace", "load .riv: ${it.message}") }
                }
            },
            update = { view -> applyRiveInputs(view, state, amplitude) },
            modifier = Modifier.size(260.dp),
        )
    }
}

/**
 * Mappa stato + livello mic sugli input della state machine Rive.
 * Idempotente e "best-effort": input mancanti vengono ignorati (runCatching).
 * TODO: sostituire "state"/"level" con i nomi reali degli input del file scelto.
 */
private fun applyRiveInputs(view: RiveAnimationView, state: HeadState, amplitude: Float) {
    runCatching { view.setNumberState(STATE_MACHINE, "state", state.ordinal.toFloat()) }
    runCatching { view.setNumberState(STATE_MACHINE, "level", amplitude * 100f) }
}
