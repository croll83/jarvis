package com.jarvis.voice.mobile.ui

import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.size
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import com.jarvis.voice.shared.HeadState

/**
 * Testa robot animata che riflette lo stato della sessione vocale.
 * Placeholder disegnato con Canvas — sostituibile con Lottie/asset dedicati.
 */
@Composable
fun RobotHead(state: HeadState, modifier: Modifier = Modifier) {
    val transition = rememberInfiniteTransition(label = "head")
    val pulse by transition.animateFloat(
        initialValue = 0.9f, targetValue = 1.1f,
        animationSpec = infiniteRepeatable(tween(700), RepeatMode.Reverse),
        label = "pulse",
    )
    val accent by animateColorAsState(
        targetValue = when (state) {
            HeadState.IDLE -> Color(0xFF3A3F4B)
            HeadState.CONNECTING -> Color(0xFFB08900)
            HeadState.LISTENING -> Color(0xFF00C2FF)
            HeadState.THINKING -> Color(0xFFB388FF)
            HeadState.SPEAKING -> Color(0xFF00E676)
        },
        label = "accent",
    )

    val eyeScale = when (state) {
        HeadState.LISTENING, HeadState.SPEAKING -> pulse
        else -> 1f
    }

    Canvas(modifier = modifier.size(220.dp)) {
        val w = size.width
        val h = size.height
        val faceColor = Color(0xFF20242E)

        // Testa
        drawRoundRect(
            color = faceColor,
            topLeft = Offset(w * 0.12f, h * 0.14f),
            size = androidx.compose.ui.geometry.Size(w * 0.76f, h * 0.66f),
            cornerRadius = androidx.compose.ui.geometry.CornerRadius(w * 0.16f, w * 0.16f),
        )
        // Antenna
        drawCircle(accent, radius = w * 0.035f, center = Offset(w * 0.5f, h * 0.08f))
        drawLine(accent, Offset(w * 0.5f, h * 0.11f), Offset(w * 0.5f, h * 0.16f), strokeWidth = w * 0.02f)

        // Occhi
        val eyeR = w * 0.09f * eyeScale
        drawCircle(accent, radius = eyeR, center = Offset(w * 0.36f, h * 0.42f))
        drawCircle(accent, radius = eyeR, center = Offset(w * 0.64f, h * 0.42f))

        // Bocca: cambia con lo stato
        val mouthY = h * 0.62f
        when (state) {
            HeadState.SPEAKING -> drawRoundRect(
                color = accent,
                topLeft = Offset(w * 0.34f, mouthY - h * 0.03f * pulse),
                size = androidx.compose.ui.geometry.Size(w * 0.32f, h * 0.06f * pulse),
                cornerRadius = androidx.compose.ui.geometry.CornerRadius(w * 0.02f, w * 0.02f),
            )
            else -> drawLine(
                accent,
                Offset(w * 0.36f, mouthY),
                Offset(w * 0.64f, mouthY),
                strokeWidth = w * 0.025f,
            )
        }
    }
}
