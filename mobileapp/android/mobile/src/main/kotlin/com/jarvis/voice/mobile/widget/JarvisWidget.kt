package com.jarvis.voice.mobile.widget

import android.content.Context
import android.content.Intent
import androidx.compose.ui.unit.dp
import androidx.glance.GlanceId
import androidx.glance.GlanceModifier
import androidx.glance.action.actionStartActivity
import androidx.glance.action.clickable
import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.GlanceAppWidgetReceiver
import androidx.glance.appwidget.provideContent
import androidx.glance.background
import androidx.glance.layout.Alignment
import androidx.glance.layout.Box
import androidx.glance.layout.fillMaxSize
import androidx.glance.layout.padding
import androidx.glance.text.Text
import androidx.glance.text.TextStyle
import androidx.glance.unit.ColorProvider
import androidx.compose.ui.graphics.Color
import com.jarvis.voice.mobile.ui.MainActivity

/**
 * Widget home-screen: un tap lancia MainActivity con EXTRA_TAP → avvia subito il turno.
 * "Costo zero": riusa lo stesso controller/servizio del client standalone.
 */
class JarvisWidget : GlanceAppWidget() {
    override suspend fun provideGlance(context: Context, id: GlanceId) {
        provideContent {
            val intent = Intent(context, MainActivity::class.java).apply {
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                putExtra(MainActivity.EXTRA_TAP, true)
            }
            Box(
                modifier = GlanceModifier
                    .fillMaxSize()
                    .background(Color(0xFF20242E))
                    .padding(8.dp)
                    .clickable(actionStartActivity(intent)),
                contentAlignment = Alignment.Center,
            ) {
                Text("JARVIS", style = TextStyle(color = ColorProvider(Color(0xFF00C2FF))))
            }
        }
    }
}

class JarvisWidgetReceiver : GlanceAppWidgetReceiver() {
    override val glanceAppWidget: GlanceAppWidget = JarvisWidget()
}
