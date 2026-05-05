package com.smssaas.app

import android.app.Activity
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.provider.Telephony
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import java.time.Instant

private val receiversScope = CoroutineScope(Dispatchers.IO)

/**
 * Catches the SENT result from SmsManager.sendTextMessage().
 */
class SmsSentReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val jobId = intent.getIntExtra(SmsWorkerService.EXTRA_JOB_ID, -1)
        if (jobId < 0) return
        val result = if (resultCode == Activity.RESULT_OK) "SENT" else "FAILED"
        val errorCode = if (resultCode != Activity.RESULT_OK) resultCode else null
        Log.d("SmsSentReceiver", "job=$jobId result=$result errorCode=$errorCode")
        receiversScope.launch { ApiClient.postReceipt(jobId, result, errorCode) }
    }
}

/**
 * Catches the carrier delivery acknowledgement.
 */
class SmsDeliveredReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (resultCode != Activity.RESULT_OK) return
        val jobId = intent.getIntExtra(SmsWorkerService.EXTRA_JOB_ID, -1)
        if (jobId < 0) return
        Log.d("SmsDeliveredReceiver", "delivered job=$jobId")
        receiversScope.launch { ApiClient.postReceipt(jobId, "DELIVERED") }
    }
}

/**
 * Listens for all incoming SMS messages and forwards them to the server.
 */
class SmsReceiveReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Telephony.Sms.Intents.SMS_RECEIVED_ACTION) return
        val messages = Telephony.Sms.Intents.getMessagesFromIntent(intent)
        if (messages.isNullOrEmpty()) return

        val from = messages[0].originatingAddress ?: return
        val body = messages.joinToString("") { it.messageBody }
        val receivedAt = Instant.now().toString()

        Log.d("SmsReceiveReceiver", "inbound from=$from len=${body.length}")
        receiversScope.launch { ApiClient.postInbound(from, body, receivedAt) }
    }
}
