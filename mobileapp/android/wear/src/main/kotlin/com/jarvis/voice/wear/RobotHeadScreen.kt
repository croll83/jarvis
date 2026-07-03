package com.jarvis.voice.wear

import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import com.jarvis.voice.shared.HeadState

/**
 * Testa robot per Wear OS: tap → onTap(). Il colore/animazione riflette lo stato.
 * Placeholder Canvas — sostituibile con un asset animato dedicato.
 */
@Composable
fun RobotHeadScreen(state: HeadState, onTap: () -> Unit) {
    val transition = rememberInfiniteTransition(label = "head")
    val pulse by transition.animateFloat(
        initialValue = 0.9f, targetValue = 1.15f,
        animationSpec = infiniteRepeatable(tween(650), RepeatMode.Reverse),
        label = "pulse",
    )
    val accent by animateColorAsState(
        targetValue = when (state) {
            HeadState.IDLE -> Color(0xFF3A3F4B)
            HeadState.CONNECTING -> Color(0xFFB08900)
            HeadState.LISTENING -> Color(0xFF00C2FF)
            HeadState.THINKING -> Color(0xFFB388FF)
            HeadState.SPEAKING -> Color(0xFF00E676)
            HeadState.ERROR -> Color(0xFFFF5A6A)
        },
        label = "accent",
    )
    val eyeScale = if (state == HeadState.LISTENING || state == HeadState.SPEAKING) pulse else 1f

    Canvas(
        modifier = Modifier
            .fillMaxSize()
            .clickable(onClick = onTap)
    ) {
        val w = size.width
        val h = size.height
        drawRoundRect(
            color = Color(0xFF20242E),
            topLeft = Offset(w * 0.22f, h * 0.24f),
            size = Size(w * 0.56f, h * 0.5f),
            cornerRadius = CornerRadius(w * 0.12f, w * 0.12f),
        )
        val eyeR = w * 0.06f * eyeScale
        drawCircle(accent, radius = eyeR, center = Offset(w * 0.4f, h * 0.44f))
        drawCircle(accent, radius = eyeR, center = Offset(w * 0.6f, h * 0.44f))
        if (state == HeadState.SPEAKING) {
            drawRoundRect(
                color = accent,
                topLeft = Offset(w * 0.4f, h * 0.58f - h * 0.02f * pulse),
                size = Size(w * 0.2f, h * 0.04f * pulse),
                cornerRadius = CornerRadius(w * 0.01f, w * 0.01f),
            )
        } else {
            drawLine(accent, Offset(w * 0.42f, h * 0.6f), Offset(w * 0.58f, h * 0.6f), strokeWidth = w * 0.02f)
        }
    }
}
