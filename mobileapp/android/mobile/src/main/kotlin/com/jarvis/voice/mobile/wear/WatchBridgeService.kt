package com.jarvis.voice.mobile.wear

import android.util.Log
import com.google.android.gms.wearable.ChannelClient
import com.google.android.gms.wearable.MessageEvent
import com.google.android.gms.wearable.WearableListenerService
import com.jarvis.voice.mobile.core.JarvisRuntime
import com.jarvis.voice.shared.wear.DataLayer

/**
 * Entry point Data Layer sul telefono. È volatile (il sistema lo ricrea di continuo),
 * quindi NON tiene stato: delega tutto al singleton stabile [WatchRelay].
 */
class WatchBridgeService : WearableListenerService() {

    override fun onCreate() {
        super.onCreate()
        JarvisRuntime.init(this)
        WatchRelay.init(this)
    }

    override fun onMessageReceived(event: MessageEvent) {
        Log.i("WatchBridge", "msg '${event.path}' da ${event.sourceNodeId}")
        when (event.path) {
            DataLayer.PATH_TAP -> WatchRelay.onTap(event.sourceNodeId)
            DataLayer.PATH_STOP -> WatchRelay.onStop(String(event.data))
        }
    }

    override fun onChannelOpened(channel: ChannelClient.Channel) {
        if (isAudio(channel)) WatchRelay.onChannelOpened(channel)
    }

    override fun onChannelClosed(channel: ChannelClient.Channel, closeReason: Int, appSpecificErrorCode: Int) {
        if (isAudio(channel)) WatchRelay.onChannelClosed(channel, closeReason)
    }

    // Watch nuovo: canale a frame; watch legacy (< 0.3.0): PCM grezzo.
    private fun isAudio(channel: ChannelClient.Channel) =
        channel.path == DataLayer.PATH_AUDIO_FRAMED || channel.path == DataLayer.PATH_AUDIO
}
