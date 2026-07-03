package com.jarvis.voice.mobile.ui

import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color

/**
 * Sfondo ambient animato ispirato alla dashboard HA "Wagmi": base navy→viola con un
 * grande glow magenta che respira in basso e un alone cyan tenue in alto.
 */
@Composable
fun AuroraBackground(modifier: Modifier = Modifier, content: @Composable BoxScope.() -> Unit) {
    val t = rememberInfiniteTransition(label = "aurora")
    val drift by t.animateFloat(
        0f, 1f, infiniteRepeatable(tween(14000, easing = LinearEasing), RepeatMode.Reverse), "drift"
    )
    val pulse by t.animateFloat(
        0.88f, 1.12f, infiniteRepeatable(tween(7000, easing = LinearEasing), RepeatMode.Reverse), "pulse"
    )

    Box(
        modifier
            .fillMaxSize()
            .drawBehind {
                // Base verticale
                drawRect(Brush.verticalGradient(listOf(JarvisColors.bgTop, JarvisColors.bgMid)))

                // Glow viola/magenta in basso-centro (respira e deriva)
                val pc = Offset(size.width * (0.5f + 0.08f * (drift - 0.5f)), size.height * 0.95f)
                val pr = size.maxDimension * 0.85f * pulse
                drawRect(
                    Brush.radialGradient(
                        listOf(JarvisColors.glowPurple.copy(alpha = 0.75f), Color.Transparent),
                        center = pc, radius = pr,
                    )
                )
                // Alone cyan tenue in alto
                val cc = Offset(size.width * (0.22f + 0.06f * drift), size.height * 0.06f)
                val cr = size.maxDimension * 0.55f
                drawRect(
                    Brush.radialGradient(
                        listOf(JarvisColors.glowCyan.copy(alpha = 0.14f), Color.Transparent),
                        center = cc, radius = cr,
                    )
                )
            },
        content = content,
    )
}
