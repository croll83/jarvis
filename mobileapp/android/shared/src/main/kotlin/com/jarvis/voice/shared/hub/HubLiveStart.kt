package com.jarvis.voice.shared.hub

import com.jarvis.voice.shared.protocol.EndReason
import org.json.JSONArray
import org.json.JSONObject

/**
 * Interpretazione di `live_session_start` dall'orchestrator. Il ramo hub parte SOLO con
 * `backend == "hub"`: senza quel campo il messaggio è quello di oggi (sessione live Hermes)
 * e il comportamento resta invariato.
 */
sealed class HubLiveStart {

    /** Nessun backend hub: vecchio path, invariato. */
    object Legacy : HubLiveStart()

    /** backend hub ma messaggio inutilizzabile: non partire, rispondi con live_session_end(reason). */
    data class Invalid(val callId: String, val reason: String) : HubLiveStart()

    data class Start(
        val callId: String,
        val profile: String,
        val name: String,
        val voice: String,
        val locationId: String,
        val room: String,
        val deliver: List<String>,
        val host: String,
        val port: Int,
        /** Campo `vad` dell'offer (automaticActivityDetection di Gemini). Vuoto = default dell'hub. */
        val vad: Map<String, Any> = emptyMap(),
    ) : HubLiveStart() {
        /** Primo frame verso l'hub; i campi opzionali vuoti non vengono mandati. */
        fun offerJson(): String {
            val o = JSONObject()
                .put("type", "offer")
                .put("call_id", callId)
                .put("profile", profile)
                .put("name", name)
            if (voice.isNotEmpty()) o.put("voice", voice)
            if (locationId.isNotEmpty()) o.put("location_id", locationId)
            if (room.isNotEmpty()) o.put("room", room)
            if (deliver.isNotEmpty()) o.put("deliver", JSONArray(deliver))
            if (vad.isNotEmpty()) o.put("vad", JSONObject(vad))
            return o.toString()
        }
    }

    companion object {
        const val BACKEND_HUB = "hub"
        const val REASON_ERROR = EndReason.ERROR
        const val REASON_UNREACHABLE = EndReason.HUB_UNREACHABLE
        const val MAX_CALL_ID_BYTES = 255

        /**
         * VAD meno sensibile per lo watch: speaker a pochi cm dal mic e AEC del watch debole,
         * un residuo d'eco a inizio frase non deve contare come l'utente che interrompe (falso
         * barge-in = risposta tagliata). Stesse chiavi e valori di default del wakeword-server
         * per l'Atom (HUB_VAD_START_SENSITIVITY / HUB_VAD_PREFIX_PADDING_MS).
         */
        val SPEAKER_NEAR_MIC_VAD: Map<String, Any> = mapOf(
            "startOfSpeechSensitivity" to "START_SENSITIVITY_LOW",
            "prefixPaddingMs" to 300,
        )

        fun parse(msg: JSONObject): HubLiveStart {
            if (msg.optString("backend") != BACKEND_HUB) return Legacy
            // call_id lo conia SOLO l'orchestrator: senza, non si parte.
            val callId = msg.optString("call_id").trim()
            if (callId.isEmpty()) return Invalid("", REASON_ERROR)
            // Il framing AUDIO porta id_len su un byte: un call_id più lungo non è inviabile.
            if (callId.toByteArray(Charsets.UTF_8).size > MAX_CALL_ID_BYTES) return Invalid("", REASON_ERROR)
            val hub = msg.optJSONObject("hub")
            val host = hub?.optString("host")?.trim().orEmpty()
            val port = hub?.optInt("port", 0) ?: 0
            if (host.isEmpty() || port !in 1..65535) return Invalid(callId, REASON_UNREACHABLE)
            val deliver = msg.optJSONArray("deliver")?.let { arr ->
                (0 until arr.length()).mapNotNull { i -> arr.optString(i).takeIf { it.isNotEmpty() } }
            } ?: emptyList()
            return Start(
                callId = callId,
                profile = msg.optString("profile"),
                name = msg.optString("name"),
                voice = msg.optString("voice"),
                locationId = msg.optString("location_id"),
                room = msg.optString("room"),
                deliver = deliver,
                host = host,
                port = port,
            )
        }
    }
}
