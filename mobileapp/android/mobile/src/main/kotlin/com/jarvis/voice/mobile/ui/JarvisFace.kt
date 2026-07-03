package com.jarvis.voice.mobile.ui

import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.FastOutSlowInEasing
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Image
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.draw.drawWithContent
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ColorFilter
import androidx.compose.ui.graphics.ColorMatrix
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.unit.dp
import com.jarvis.voice.mobile.R
import com.jarvis.voice.shared.HeadState
import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.sin

/**
 * Avatar JARVIS animato (l'orb centrale, stile Siri/Alexa ma con l'identità del logo).
 *  IDLE      → dormiente: dim, respiro lento, glow tenue
 *  LISTENING → reattivo: anello di barre-waveform pilotate dal mic reale
 *  THINKING  → arco-ingranaggio che ruota
 *  SPEAKING  → pulsazione morbida
 *  ERROR     → volto desaturato + anello rosso + occhi X
 */
@Composable
fun JarvisFace(
    state: HeadState,
    amplitude: Float,
    onTap: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val accent by animateColorAsState(
        targetValue = when (state) {
            HeadState.IDLE -> JarvisColors.cyan.copy(alpha = 0.55f)
            HeadState.CONNECTING -> JarvisColors.gold
            HeadState.LISTENING -> JarvisColors.cyan
            HeadState.THINKING -> JarvisColors.purple
            HeadState.SPEAKING -> JarvisColors.green
            HeadState.ERROR -> JarvisColors.red
        },
        animationSpec = tween(400), label = "accent",
    )

    val t = rememberInfiniteTransition(label = "face")
    val breath by t.animateFloat(
        0.975f, 1.025f,
        infiniteRepeatable(tween(2600, easing = FastOutSlowInEasing), RepeatMode.Reverse), "breath",
    )
    val rot by t.animateFloat(
        0f, 360f, infiniteRepeatable(tween(3600, easing = LinearEasing)), "rot",
    )
    val glow by t.animateFloat(
        0.35f, 0.8f, infiniteRepeatable(tween(1900, easing = LinearEasing), RepeatMode.Reverse), "glow",
    )
    val wavePhase by t.animateFloat(
        0f, (2 * PI).toFloat(), infiniteRepeatable(tween(1500, easing = LinearEasing)), "wave",
    )
    val amp by animateFloatAsState(amplitude, tween(90), label = "amp")

    val isError = state == HeadState.ERROR
    val isIdle = state == HeadState.IDLE

    Box(
        modifier = modifier
            .size(300.dp)
            .clickable(
                interactionSource = remember { MutableInteractionSource() },
                indication = null,
            ) { onTap() }
            // Aure DIETRO l'avatar
            .drawBehind {
                val c = Offset(size.width / 2f, size.height / 2f)
                val faceR = size.minDimension / 3f  // avatar Ø 200 in box 300
                val gap = size.minDimension * 0.03f
                val glowBoost = if (state == HeadState.SPEAKING) 1.25f else 1f

                // Glow morbido
                val glowR = faceR * 1.85f * (if (isIdle) 0.85f else 1f) * glowBoost
                drawCircle(
                    brush = Brush.radialGradient(
                        listOf(accent.copy(alpha = glow * 0.55f), Color.Transparent),
                        center = c, radius = glowR,
                    ),
                    radius = glowR, center = c,
                )

                // Waveform reattiva (listening)
                if (state == HeadState.LISTENING) {
                    val bars = 60
                    val inner = faceR + gap
                    val barMax = size.minDimension * 0.11f
                    val w = size.minDimension * 0.008f
                    for (i in 0 until bars) {
                        val ang = i.toFloat() / bars * 2f * PI.toFloat()
                        val live = 0.6f + 0.4f * sin(wavePhase + i * 0.4f)
                        val v = ((0.15f + 0.85f * amp) * live).coerceIn(0f, 1f)
                        val len = barMax * v
                        val cosA = cos(ang); val sinA = sin(ang)
                        val p1 = Offset(c.x + cosA * inner, c.y + sinA * inner)
                        val p2 = Offset(c.x + cosA * (inner + len), c.y + sinA * (inner + len))
                        drawLine(accent.copy(alpha = 0.85f), p1, p2, strokeWidth = w, cap = StrokeCap.Round)
                    }
                }

                // Arco-ingranaggio (thinking)
                if (state == HeadState.THINKING) {
                    val r = faceR + gap + size.minDimension * 0.04f
                    val tl = Offset(c.x - r, c.y - r)
                    val sz = Size(r * 2, r * 2)
                    val stroke = Stroke(width = size.minDimension * 0.014f, cap = StrokeCap.Round)
                    drawArc(accent, rot, 90f, false, tl, sz, style = stroke)
                    drawArc(accent.copy(alpha = 0.5f), rot + 180f, 60f, false, tl, sz, style = stroke)
                }
            },
        contentAlignment = Alignment.Center,
    ) {
        // Avatar
        Image(
            painter = painterResource(R.drawable.jarvis_avatar),
            contentDescription = "JARVIS",
            contentScale = ContentScale.Crop,
            colorFilter = if (isError) ColorFilter.colorMatrix(ColorMatrix().apply { setToSaturation(0f) }) else null,
            modifier = Modifier
                .size(200.dp)
                .graphicsLayer {
                    scaleX = breath; scaleY = breath
                    alpha = if (isIdle) 0.82f else 1f
                }
                .clip(CircleShape)
                // Bordo + occhi X DAVANTI all'avatar
                .drawWithContent {
                    drawContent()
                    val c = Offset(size.width / 2f, size.height / 2f)
                    val r = size.minDimension / 2f
                    // anello bordo
                    drawCircle(accent, radius = r - 2f, style = Stroke(width = size.minDimension * 0.02f))
                    if (isError) {
                        // occhi X rossi (posizioni tarate sugli occhi-ingranaggio del volto)
                        val eyeY = c.y + r * 0.30f
                        val exL = c.x + r * 0.04f
                        val exR = c.x + r * 0.53f
                        val s = r * 0.13f
                        val sw = size.minDimension * 0.018f
                        for (ex in listOf(exL, exR)) {
                            drawLine(JarvisColors.red, Offset(ex - s, eyeY - s), Offset(ex + s, eyeY + s), sw, StrokeCap.Round)
                            drawLine(JarvisColors.red, Offset(ex - s, eyeY + s), Offset(ex + s, eyeY - s), sw, StrokeCap.Round)
                        }
                    }
                },
        )
    }
}
