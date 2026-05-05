package com.smssaas.app

import android.Manifest
import android.app.*
import android.content.Intent
import android.content.pm.PackageManager
import android.media.*
import android.net.Uri
import android.os.Bundle
import android.os.IBinder
import android.telecom.TelecomManager
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import kotlinx.coroutines.*
import okhttp3.*
import okio.ByteString
import okio.ByteString.Companion.toByteString
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicInteger

/**
 * Foreground service that handles voice calls:
 *  1. Monitors for incoming call state from SmsaasInCallService
 *  2. Opens a WebSocket to the server for audio streaming
 *  3. Captures call audio via AudioRecord (CAPTURE_AUDIO_OUTPUT granted via ADB)
 *  4. Injects server audio via AudioTrack
 */
class VoiceWorkerService : Service() {

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private var activeSession: VoiceSession? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        // Hydrate ApiClient from prefs in case this service starts before MainActivity
        // (Telecom may spawn our process to host SmsaasInCallService first).
        val prefs = getSharedPreferences("smssaas", MODE_PRIVATE)
        if (ApiClient.deviceId.isBlank()) {
            ApiClient.baseUrl      = prefs.getString("base_url", ApiClient.baseUrl) ?: ApiClient.baseUrl
            ApiClient.deviceSecret = prefs.getString("device_secret", ApiClient.deviceSecret) ?: ApiClient.deviceSecret
            ApiClient.deviceId     = prefs.getString("device_id", "") ?: ""
        }
        createNotificationChannel()
        try {
            startForeground(NOTIF_ID, buildNotification("Voice — idle"))
        } catch (e: Exception) {
            Log.w(TAG, "startForeground denied: ${e.message}")
        }
        startCommandPoller()
    }

    private fun startCommandPoller() {
        scope.launch {
            while (isActive) {
                try {
                    if (ApiClient.deviceId.isNotBlank()) {
                        val cmds = ApiClient.pollCommands()
                        cmds.forEach { handleCommand(it) }
                    }
                } catch (e: Exception) {
                    Log.w(TAG, "command poll error: ${e.message}")
                }
                delay(COMMAND_POLL_MS)
            }
        }
    }

    private fun handleCommand(cmd: DeviceCommand) {
        when (cmd.command) {
            "dial" -> {
                val callId   = cmd.payload.optString("call_id")
                val toNumber = cmd.payload.optString("to_number")
                if (callId.isEmpty() || toNumber.isEmpty()) {
                    ApiClient.ackCommand(cmd.id, "failed")
                    return
                }
                val ok = placeOutboundCall(callId, toNumber)
                ApiClient.ackCommand(cmd.id, if (ok) "done" else "failed")
            }
            "hangup" -> {
                val callId = cmd.payload.optString("call_id")
                ActiveCalls.get(callId)?.disconnect()
                ApiClient.ackCommand(cmd.id, "done")
            }
            else -> {
                Log.w(TAG, "unknown command: ${cmd.command}")
                ApiClient.ackCommand(cmd.id, "failed")
            }
        }
    }

    private fun placeOutboundCall(callId: String, toNumber: String): Boolean {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CALL_PHONE)
            != PackageManager.PERMISSION_GRANTED) {
            Log.w(TAG, "CALL_PHONE not granted — cannot place call")
            ApiClient.postCallState(callId, "failed", toNumber = toNumber, direction = "outbound")
            return false
        }

        // Stash callId so SmsaasInCallService can correlate the new Call object.
        PendingOutbound.put(toNumber, callId)
        ApiClient.postCallState(callId, "dialing", toNumber = toNumber, direction = "outbound")

        return try {
            val tm = getSystemService(TelecomManager::class.java)
            val uri = Uri.fromParts("tel", toNumber, null)
            val extras = Bundle().apply {
                putBoolean(TelecomManager.EXTRA_START_CALL_WITH_SPEAKERPHONE, false)
                putString(EXTRA_CALL_ID, callId)
            }
            val outerExtras = Bundle().apply {
                putBundle(TelecomManager.EXTRA_OUTGOING_CALL_EXTRAS, extras)
            }
            tm.placeCall(uri, outerExtras)
            true
        } catch (e: SecurityException) {
            Log.w(TAG, "placeCall security: ${e.message}")
            PendingOutbound.remove(toNumber)
            ApiClient.postCallState(callId, "failed", toNumber = toNumber, direction = "outbound")
            false
        } catch (e: Exception) {
            Log.w(TAG, "placeCall failed: ${e.message}")
            PendingOutbound.remove(toNumber)
            ApiClient.postCallState(callId, "failed", toNumber = toNumber, direction = "outbound")
            false
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_CALL_START -> {
                val callId = intent.getStringExtra(EXTRA_CALL_ID) ?: return START_NOT_STICKY
                val from   = intent.getStringExtra(EXTRA_FROM) ?: ""
                val to     = intent.getStringExtra(EXTRA_TO) ?: ""
                val dir    = intent.getStringExtra(EXTRA_DIR) ?: "inbound"
                startSession(callId, from, to, dir)
            }
            ACTION_CALL_END -> {
                val callId = intent.getStringExtra(EXTRA_CALL_ID)
                if (callId == activeSession?.callId) activeSession?.stop()
            }
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        activeSession?.stop()
        scope.cancel()
        super.onDestroy()
    }

    private fun startSession(callId: String, from: String, to: String, direction: String) {
        activeSession?.stop()
        activeSession = VoiceSession(callId, from, to, direction, scope, ::onSessionEnded)
        activeSession?.start()
        updateNotification("Call active: $from")
        scope.launch { ApiClient.postCallState(callId, "active", from, to, direction) }
    }

    private fun onSessionEnded(callId: String, durationSec: Int) {
        scope.launch { ApiClient.postCallState(callId, "completed", durationSec = durationSec) }
        updateNotification("Voice — idle")
    }

    private fun createNotificationChannel() {
        val chan = NotificationChannel(CHANNEL_ID, "Voice Worker", NotificationManager.IMPORTANCE_LOW)
        getSystemService(NotificationManager::class.java).createNotificationChannel(chan)
    }

    private fun buildNotification(text: String): Notification =
        NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("SmsSaaS Voice")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_menu_call)
            .setOngoing(true)
            .build()

    private fun updateNotification(text: String) {
        val nm = getSystemService(NotificationManager::class.java)
        nm.notify(NOTIF_ID, buildNotification(text))
    }

    companion object {
        private const val TAG = "VoiceWorkerService"
        const val ACTION_CALL_START = "com.smssaas.app.CALL_START"
        const val ACTION_CALL_END   = "com.smssaas.app.CALL_END"
        const val EXTRA_CALL_ID = "call_id"
        const val EXTRA_FROM    = "from"
        const val EXTRA_TO      = "to"
        const val EXTRA_DIR     = "direction"
        private const val NOTIF_ID  = 2
        private const val CHANNEL_ID = "voice_worker"
        private const val COMMAND_POLL_MS = 3_000L
    }
}

/**
 * Shared outbound-call correlation map. VoiceWorkerService.placeOutboundCall()
 * inserts (toNumber → callId) before TelecomManager.placeCall(); SmsaasInCallService
 * looks it up when the matching Call object materializes via onCallAdded().
 */
object PendingOutbound {
    private val map = ConcurrentHashMap<String, String>()
    fun put(toNumber: String, callId: String) { map[normalize(toNumber)] = callId }
    fun take(toNumber: String): String? = map.remove(normalize(toNumber))
    fun remove(toNumber: String) { map.remove(normalize(toNumber)) }
    private fun normalize(n: String) = n.filter { it.isDigit() || it == '+' }
}

// ── Voice session (one per call) ──────────────────────────────────────────────

private class VoiceSession(
    val callId: String,
    val from: String,
    val to: String,
    val direction: String,
    val scope: CoroutineScope,
    val onEnded: (String, Int) -> Unit,
) {
    private val TAG = "VoiceSession"

    private val SAMPLE_RATE = 8000
    private val CHANNEL_IN  = AudioFormat.CHANNEL_IN_MONO
    private val CHANNEL_OUT = AudioFormat.CHANNEL_OUT_MONO
    private val ENCODING    = AudioFormat.ENCODING_PCM_16BIT
    private val FRAME_BYTES = 320  // 20ms @ 8kHz PCM16
    private val HEADER_SIZE = 4

    private val seqOut = AtomicInteger(0)
    private var recorder: AudioRecord? = null
    private var player: AudioTrack? = null
    private var ws: WebSocket? = null
    private var startedAt = System.currentTimeMillis()
    private var job: Job? = null

    fun start() {
        val wsUrl = ApiClient.voiceWsUrl(callId)
        val request = Request.Builder().url(wsUrl).build()
        val client = okhttp3.OkHttpClient()

        ws = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                Log.d(TAG, "WS open for call $callId")
                job = scope.launch { streamAudio(webSocket) }
            }

            override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                handleIncoming(bytes.toByteArray())
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.w(TAG, "WS failure: ${t.message}")
                stop()
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                stop()
            }
        })
    }

    private suspend fun streamAudio(webSocket: WebSocket) {
        val bufSize = AudioRecord.getMinBufferSize(SAMPLE_RATE, CHANNEL_IN, ENCODING)
            .coerceAtLeast(FRAME_BYTES * 4)

        @Suppress("DEPRECATION")
        recorder = AudioRecord(
            MediaRecorder.AudioSource.VOICE_CALL,  // requires CAPTURE_AUDIO_OUTPUT (ADB-granted)
            SAMPLE_RATE, CHANNEL_IN, ENCODING, bufSize,
        )

        player = AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                .build())
            .setAudioFormat(AudioFormat.Builder()
                .setSampleRate(SAMPLE_RATE)
                .setChannelMask(CHANNEL_OUT)
                .setEncoding(ENCODING)
                .build())
            .setBufferSizeInBytes(FRAME_BYTES * 8)
            .setTransferMode(AudioTrack.MODE_STREAM)
            .build()

        recorder?.startRecording()
        player?.play()

        val buf = ByteArray(FRAME_BYTES)
        var seq = seqOut.get()
        while (isActive) {
            val read = recorder?.read(buf, 0, FRAME_BYTES) ?: break
            if (read <= 0) continue

            // Build frame: direction=0x01, type=0x01 (audio), seq, payload
            val frame = ByteArray(HEADER_SIZE + read)
            frame[0] = 0x01  // phone→server
            frame[1] = 0x01  // audio
            frame[2] = ((seq shr 8) and 0xFF).toByte()
            frame[3] = (seq and 0xFF).toByte()
            System.arraycopy(buf, 0, frame, HEADER_SIZE, read)
            seq++

            webSocket.send(frame.toByteString())
        }
    }

    private fun handleIncoming(data: ByteArray) {
        if (data.size < HEADER_SIZE) return
        val type = data[1].toInt() and 0xFF
        val payload = data.copyOfRange(HEADER_SIZE, data.size)

        when (type) {
            0x01 -> { // audio — inject into call
                player?.write(payload, 0, payload.size)
            }
            0x02 -> { // DTMF digit — ignored on phone side (server sends back)
            }
            0x03 -> { // hangup
                stop()
            }
        }
    }

    fun stop() {
        job?.cancel()
        recorder?.stop()
        recorder?.release()
        player?.stop()
        player?.release()
        ws?.close(1000, null)
        val duration = ((System.currentTimeMillis() - startedAt) / 1000).toInt()
        onEnded(callId, duration)
    }

    private val isActive: Boolean get() = job?.isActive != false
}
