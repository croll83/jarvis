package com.jarvis.voice.shared.audio

import android.content.Context
import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.os.Build
import android.util.Log

/**
 * MODE_IN_COMMUNICATION per la durata di una sessione live (ingaggia l'AEC di piattaforma),
 * con ripristino del modo precedente all'uscita. Richiede MODIFY_AUDIO_SETTINGS.
 *
 * In MODE_IN_COMMUNICATION Android manda la voce alla capsula auricolare: se è quella la
 * destinazione, forziamo lo speaker integrato (vivavoce). Cuffie/BT restano come sono.
 */
class LiveAudioRouting(context: Context) {

    private val tag = "LiveAudioRouting"
    private val am = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private var prevMode: Int? = null
    private var prevSpeakerphone: Boolean? = null
    private var commDeviceSet = false

    fun enter() {
        if (prevMode != null) return
        prevMode = am.mode
        runCatching { am.mode = AudioManager.MODE_IN_COMMUNICATION }
            .onFailure { Log.w(tag, "setMode: ${it.message}") }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            val cur = am.communicationDevice
            if (cur == null || cur.type == AudioDeviceInfo.TYPE_BUILTIN_EARPIECE) {
                am.availableCommunicationDevices
                    .firstOrNull { it.type == AudioDeviceInfo.TYPE_BUILTIN_SPEAKER }
                    ?.let { commDeviceSet = runCatching { am.setCommunicationDevice(it) }.getOrDefault(false) }
            }
        } else {
            @Suppress("DEPRECATION")
            if (!am.isWiredHeadsetOn && !am.isBluetoothScoOn) {
                prevSpeakerphone = am.isSpeakerphoneOn
                am.isSpeakerphoneOn = true
            }
        }
    }

    fun exit() {
        val mode = prevMode ?: return
        if (commDeviceSet && Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            runCatching { am.clearCommunicationDevice() }
        }
        commDeviceSet = false
        @Suppress("DEPRECATION")
        prevSpeakerphone?.let { am.isSpeakerphoneOn = it }
        prevSpeakerphone = null
        runCatching { am.mode = mode }
        prevMode = null
    }
}
