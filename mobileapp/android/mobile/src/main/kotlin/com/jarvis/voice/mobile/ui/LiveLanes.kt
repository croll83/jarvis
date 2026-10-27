package com.jarvis.voice.mobile.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay

private const val HISTORY = 30        // ~2 s a ~15 fps
private const val FRAME_MS = 66L

/**
 * Vista full-duplex della sessione live hub: due corsie scorrevoli, sopra JARVIS che parla,
 * sotto l'utente. Esiste (e anima) solo durante la sessione live.
 */
@Composable
fun LiveLanes(jarvisLevel: Float, userLevel: Float, modifier: Modifier = Modifier) {
    val jarvis by rememberUpdatedState(jarvisLevel)
    val user by rememberUpdatedState(userLevel)
    val jHist = remember { FloatArray(HISTORY) }
    val uHist = remember { FloatArray(HISTORY) }
    var head by remember { mutableIntStateOf(0) }

    LaunchedEffect(Unit) {
        while (true) {
            delay(FRAME_MS)
            val i = (head + 1) % HISTORY
            jHist[i] = jarvis
            uHist[i] = user
            head = i
        }
    }

    Column(
        modifier
            .clip(RoundedCornerShape(16.dp))
            .background(JarvisColors.panel)
            .padding(horizontal = 14.dp, vertical = 10.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Lane("JARVIS", JarvisColors.cyan, jHist, head)
        Lane("TU", JarvisColors.green, uHist, head)
    }
}

@Composable
private fun Lane(label: String, color: Color, hist: FloatArray, head: Int) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Text(label, color = color, fontSize = 11.sp, fontWeight = FontWeight.Bold,
            letterSpacing = 1.5.sp, modifier = Modifier.width(56.dp))
        Spacer(Modifier.width(6.dp))
        Canvas(Modifier.fillMaxWidth().height(28.dp)) {
            val step = size.width / HISTORY
            val mid = size.height / 2f
            val w = step * 0.55f
            for (k in 0 until HISTORY) {
                // dal più vecchio (sinistra) al più recente (destra); head letto per ridisegnare
                val v = hist[(head + 1 + k) % HISTORY]
                val h = maxOf(2f, v * size.height) / 2f
                val x = step * k + step / 2f
                drawLine(color.copy(alpha = 0.35f + 0.65f * v), Offset(x, mid - h), Offset(x, mid + h),
                    strokeWidth = w, cap = StrokeCap.Round)
            }
        }
    }
}
