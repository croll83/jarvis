package com.jarvis.voice.wear

import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.res.painterResource
import com.jarvis.voice.shared.HeadState
import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.sin

private val CYAN = Color(0xFF3FC6F0)
private val GREEN = Color(0xFF67E0A0)
private val BLUE = Color(0xFF4D7CFF)
private val GOLD = Color(0xFFE5A83B)
private val RED = Color(0xFFFF5A6A)

/**
 * Volto del watch: Robocat STATICO (drawable `robocat_idle`) + overlay Compose per stato.
 * Nessun Rive → leggero e affidabile.
 *   LISTENING → waveform cyan reattiva al mic
 *   SPEAKING  → waveform verde ritmica
 *   THINKING  → anello glow blu Jarvis
 *   ERROR     → anello glow rosso   |  CONNECTING → oro
 */
@Composable
fun WearFace(state: HeadState, amplitude: Float, onTap: () -> Unit) {
    Box(
        Modifier
            .fillMaxSize()
            .clickable(
                interactionSource = remember { MutableInteractionSource() },
                indication = null,
            ) { onTap() },
        contentAlignment = Alignment.Center,
    ) {
        Image(
            painter = painterResource(R.drawable.robocat_idle),
            contentDescription = "JARVIS",
            contentScale = ContentScale.Fit,
            modifier = Modifier.fillMaxSize(0.58f),
        )

        // Batteria: l'overlay animato (transizione infinita + Canvas a 60fps) vive SOLO
        // negli stati attivi. In IDLE esce dalla composizione → il loop di frame si ferma
        // e la CPU/GPU tornano a riposo (prima girava all'infinito anche a schermo fermo).
        if (state != HeadState.IDLE) {
            StateOverlay(state, amplitude)
        }
    }
}

@Composable
private fun StateOverlay(state: HeadState, amplitude: Float) {
    val t = rememberInfiniteTransition(label = "wearfx")
    val wavePhase by t.animateFloat(
        0f, (2 * PI).toFloat(), infiniteRepeatable(tween(1400, easing = LinearEasing)), "wave",
    )
    val glow by t.animateFloat(
        0.35f, 0.9f, infiniteRepeatable(tween(1500, easing = LinearEasing), RepeatMode.Reverse), "glow",
    )
    val ampSmoothed by animateFloatAsState(amplitude, tween(90), label = "amp")

    val accent by animateColorAsState(
        targetValue = when (state) {
            HeadState.IDLE -> CYAN.copy(alpha = 0.4f)
            HeadState.CONNECTING -> GOLD
            HeadState.LISTENING -> CYAN
            HeadState.THINKING -> BLUE
            HeadState.SPEAKING -> GREEN
            HeadState.ERROR -> RED
        },
        animationSpec = tween(350), label = "accent",
    )

    Canvas(Modifier.fillMaxSize()) {
        val c = Offset(size.width / 2f, size.height / 2f)
        val minD = size.minDimension
        when (state) {
            HeadState.LISTENING, HeadState.SPEAKING -> {
                val listening = state == HeadState.LISTENING
                val color = if (listening) CYAN else GREEN
                val bars = 64
                val inner = minD * 0.36f
                val barMax = minD * 0.11f
                val w = minD * 0.012f
                for (i in 0 until bars) {
                    val ang = i.toFloat() / bars * 2f * PI.toFloat()
                    val live = 0.55f + 0.45f * sin(wavePhase + i * 0.4f)
                    val level = if (listening) ampSmoothed
                                else (0.5f + 0.4f * sin(wavePhase * 2f)).coerceIn(0f, 1f)
                    val v = ((0.2f + 0.8f * level) * live).coerceIn(0f, 1f)
                    val len = barMax * v
                    val ca = cos(ang); val sa = sin(ang)
                    drawLine(
                        color.copy(alpha = 0.9f),
                        Offset(c.x + ca * inner, c.y + sa * inner),
                        Offset(c.x + ca * (inner + len), c.y + sa * (inner + len)),
                        strokeWidth = w, cap = StrokeCap.Round,
                    )
                }
            }
            HeadState.THINKING, HeadState.ERROR, HeadState.CONNECTING -> {
                val r = minD * 0.42f
                drawCircle(accent.copy(alpha = glow), radius = r, center = c,
                    style = Stroke(width = minD * 0.03f))
                drawCircle(accent.copy(alpha = glow * 0.4f), radius = r, center = c,
                    style = Stroke(width = minD * 0.07f))
            }
            else -> {}
        }
    }
}
