package com.jarvis.voice.shared.hub

import android.media.AudioAttributes
import android.media.MediaRecorder
import com.jarvis.voice.shared.audio.LiveAudio
import com.jarvis.voice.shared.protocol.EndReason
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Gating della modalità live: il ramo hub parte SOLO con backend=="hub" e un call_id; fuori
 * sessione il profilo audio resta quello di oggi (VOICE_RECOGNITION, gain x8, USAGE_MEDIA).
 */
class LiveGatingTest {

    private fun msg(s: String) = JSONObject(s)

    @Test fun legacyMessage_staysOnOldPath() {
        assertSame(HubLiveStart.Legacy, HubLiveStart.parse(msg("""{"type":"live_session_start"}""")))
        assertSame(HubLiveStart.Legacy, HubLiveStart.parse(msg("""{"type":"live_session_start","backend":"hermes"}""")))
        // campi hub senza backend: comunque vecchio path
        assertSame(HubLiveStart.Legacy, HubLiveStart.parse(
            msg("""{"type":"live_session_start","call_id":"x","hub":{"host":"h","port":8790}}""")))
    }

    @Test fun hubWithoutCallId_isError() {
        val r = HubLiveStart.parse(msg("""{"type":"live_session_start","backend":"hub","hub":{"host":"h","port":8790}}"""))
        assertEquals(HubLiveStart.Invalid("", EndReason.ERROR), r)
    }

    @Test fun hubWithOverlongCallId_isError() {
        // id_len del frame AUDIO è un byte: oltre 255 byte non si parte (niente crash sul thread mic)
        val long = "x".repeat(256)
        val r = HubLiveStart.parse(msg("""{"type":"live_session_start","backend":"hub","call_id":"$long","hub":{"host":"h","port":8790}}"""))
        assertEquals(HubLiveStart.Invalid("", EndReason.ERROR), r)
        val ok = HubLiveStart.parse(msg("""{"type":"live_session_start","backend":"hub","call_id":"${"y".repeat(255)}","hub":{"host":"h","port":8790}}"""))
        assertTrue(ok is HubLiveStart.Start)
    }

    @Test fun fallbackReasons_matchSharedDecisionD3() {
        // l'orchestrator ripiega (e il device aspetta) solo per questi casi
        assertTrue(EndReason.awaitsFallback("c1", EndReason.HUB_REJECTED))
        assertTrue(EndReason.awaitsFallback("c1", EndReason.HUB_UNREACHABLE))
        assertTrue(EndReason.awaitsFallback("", EndReason.ERROR))
        assertTrue(EndReason.awaitsFallback("c1", EndReason.ERROR))  // anche dopo l'accept
        assertFalse(EndReason.awaitsFallback("c1", EndReason.BUTTON))
        assertFalse(EndReason.awaitsFallback("c1", EndReason.HUB_HANGUP))
    }

    @Test fun hubWithoutAddress_isUnreachable() {
        val noHub = HubLiveStart.parse(msg("""{"type":"live_session_start","backend":"hub","call_id":"c1"}"""))
        assertEquals(HubLiveStart.Invalid("c1", EndReason.HUB_UNREACHABLE), noHub)
        val portZero = HubLiveStart.parse(
            msg("""{"type":"live_session_start","backend":"hub","call_id":"c1","hub":{"host":"h","port":0}}"""))
        assertEquals(HubLiveStart.Invalid("c1", EndReason.HUB_UNREACHABLE), portZero)
    }

    @Test fun fullMessage_buildsOffer() {
        val r = HubLiveStart.parse(msg("""
            {"type":"live_session_start","backend":"hub","call_id":"9f8e7d6c","profile":"hermes-shared",
             "name":"Famiglia","voice":"Kore","location_id":"napoli","room":"cucina",
             "deliver":["orchestrator"],"hub":{"host":"100.116.99.9","port":8790}}
        """.trimIndent())) as HubLiveStart.Start
        assertEquals("100.116.99.9", r.host)
        assertEquals(8790, r.port)
        val offer = JSONObject(r.offerJson())
        assertEquals("offer", offer.getString("type"))
        assertEquals("9f8e7d6c", offer.getString("call_id"))
        assertEquals("hermes-shared", offer.getString("profile"))
        assertEquals("Famiglia", offer.getString("name"))
        assertEquals("Kore", offer.getString("voice"))
        assertEquals("napoli", offer.getString("location_id"))
        assertEquals("cucina", offer.getString("room"))
        assertEquals("orchestrator", offer.getJSONArray("deliver").getString(0))
    }

    @Test fun audioProfile_normalIsTodays() {
        val p = LiveAudio.profile(live = false)
        assertEquals(MediaRecorder.AudioSource.VOICE_RECOGNITION, p.micSource)
        assertEquals(8f, p.micGain)
        assertFalse(p.effects)
        assertEquals(AudioAttributes.USAGE_MEDIA, p.playbackUsage)
        // il fallback media non tocca il path normale
        assertEquals(p, LiveAudio.profile(live = false, mediaUsageFallback = true))
    }

    @Test fun audioProfile_liveIsVoip() {
        val p = LiveAudio.profile(live = true, mediaUsageFallback = false)
        assertEquals(MediaRecorder.AudioSource.VOICE_COMMUNICATION, p.micSource)
        assertEquals(1f, p.micGain)
        assertTrue(p.effects)
        assertEquals(AudioAttributes.USAGE_VOICE_COMMUNICATION, p.playbackUsage)
        assertEquals(AudioAttributes.USAGE_MEDIA, LiveAudio.profile(live = true, mediaUsageFallback = true).playbackUsage)
    }
}
