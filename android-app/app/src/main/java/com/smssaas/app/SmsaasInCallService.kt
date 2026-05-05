package com.smssaas.app

import android.content.Intent
import android.telecom.Call
import android.telecom.InCallService
import android.util.Log
import java.util.UUID
import java.util.concurrent.Executors

/**
 * InCallService intercepts incoming calls and auto-answers them, then
 * notifies VoiceWorkerService to open the WebSocket audio stream.
 *
 * Requires:
 *   - android.permission.MANAGE_OWN_CALLS (or full InCallService permission)
 *   - <meta-data android:name="android.telecom.IN_CALL_SERVICE_UI" android:value="true"/>
 *
 * One-time ADB setup to make this app the default phone handler for calls:
 *   adb shell telecom set-default-dialer com.smssaas.app
 */
class SmsaasInCallService : InCallService() {

    private val net = Executors.newSingleThreadExecutor()

    override fun onCreate() {
        super.onCreate()
        // Telecom may bind us before MainActivity ever runs, so make sure ApiClient is hydrated.
        val prefs = getSharedPreferences("smssaas", MODE_PRIVATE)
        if (ApiClient.deviceId.isBlank()) {
            ApiClient.baseUrl      = prefs.getString("base_url", ApiClient.baseUrl) ?: ApiClient.baseUrl
            ApiClient.deviceSecret = prefs.getString("device_secret", ApiClient.deviceSecret) ?: ApiClient.deviceSecret
            ApiClient.deviceId     = prefs.getString("device_id", "") ?: ""
        }
    }

    override fun onDestroy() {
        net.shutdownNow()
        super.onDestroy()
    }

    private fun postState(callId: String, status: String, from: String? = null, to: String? = null,
                          direction: String? = null, durationSec: Int? = null) {
        net.execute {
            try { ApiClient.postCallState(callId, status, from, to, direction, durationSec) }
            catch (e: Exception) { Log.w(TAG, "postCallState: ${e.message}") }
        }
    }

    override fun onCallAdded(call: Call) {
        super.onCallAdded(call)
        val handle = call.details.handle?.schemeSpecificPart ?: "unknown"
        Log.d(TAG, "onCallAdded state=${call.state} handle=$handle")

        when (call.state) {
            Call.STATE_RINGING        -> handleInbound(call, handle)
            Call.STATE_CONNECTING,
            Call.STATE_DIALING        -> handleOutbound(call, handle)
            else                      -> Log.d(TAG, "ignoring call in state ${call.state}")
        }
    }

    private fun handleInbound(call: Call, from: String) {
        val callId = UUID.randomUUID().toString()
        ActiveCalls.register(callId, call)

        // Auto-answer (TODO: gate on IVR flow's auto-answer policy — brick #2b)
        call.answer(0)
        Log.d(TAG, "auto-answered inbound $callId from $from")

        startSession(callId, from = from, to = "", direction = "inbound")
        attachLifecycle(call, callId)
    }

    private fun handleOutbound(call: Call, toHandle: String) {
        val callId = PendingOutbound.take(toHandle) ?: run {
            Log.w(TAG, "outbound call to $toHandle has no pending callId — was this placed by another app?")
            return
        }
        ActiveCalls.register(callId, call)
        Log.d(TAG, "tracking outbound $callId to $toHandle")

        attachLifecycle(call, callId, outbound = true, toNumber = toHandle)
    }

    private fun attachLifecycle(
        call: Call,
        callId: String,
        outbound: Boolean = false,
        toNumber: String = "",
    ) {
        var sessionStarted = false
        call.registerCallback(object : Call.Callback() {
            override fun onStateChanged(call: Call, state: Int) {
                Log.d(TAG, "$callId state → $state")
                when (state) {
                    Call.STATE_RINGING -> if (outbound) {
                        postState(callId, "ringing", to = toNumber, direction = "outbound")
                    }
                    Call.STATE_ACTIVE -> if (outbound && !sessionStarted) {
                        sessionStarted = true
                        startSession(callId, from = "", to = toNumber, direction = "outbound")
                    }
                    Call.STATE_DISCONNECTED -> {
                        ActiveCalls.remove(callId)
                        val endIntent = Intent(this@SmsaasInCallService, VoiceWorkerService::class.java).apply {
                            action = VoiceWorkerService.ACTION_CALL_END
                            putExtra(VoiceWorkerService.EXTRA_CALL_ID, callId)
                        }
                        startService(endIntent)
                    }
                }
            }
        })
    }

    private fun startSession(callId: String, from: String, to: String, direction: String) {
        val intent = Intent(this, VoiceWorkerService::class.java).apply {
            action = VoiceWorkerService.ACTION_CALL_START
            putExtra(VoiceWorkerService.EXTRA_CALL_ID, callId)
            putExtra(VoiceWorkerService.EXTRA_FROM, from)
            putExtra(VoiceWorkerService.EXTRA_TO, to)
            putExtra(VoiceWorkerService.EXTRA_DIR, direction)
        }
        startService(intent)
    }

    companion object {
        private const val TAG = "SmsaasInCallService"
    }
}

object ActiveCalls {
    private val map = mutableMapOf<String, Call>()

    fun register(callId: String, call: Call) { map[callId] = call }
    fun remove(callId: String) { map.remove(callId) }
    fun get(callId: String): Call? = map[callId]
}
