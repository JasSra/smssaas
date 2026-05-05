package com.smssaas.app

import android.app.*
import android.content.Context
import android.content.Intent
import android.os.IBinder
import android.telephony.SmsManager
import android.util.Log
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.*
import java.util.concurrent.ConcurrentHashMap

class SmsWorkerService : Service() {

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private val pendingJobs = ConcurrentHashMap<Int, SmsJob>()

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        // Android 14 may deny foreground service when started from background broadcast.
        // Fall back to running as a regular background service if that happens.
        try {
            startForeground(NOTIF_ID, buildNotification("Running — waiting for jobs"))
        } catch (e: Exception) {
            Log.w(TAG, "startForeground denied, running as background service: ${e.message}")
        }
        startPolling()
        startHeartbeat()
        DiagnosticsReporter.start(this, scope)
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    private fun startPolling() {
        scope.launch {
            while (isActive) {
                try {
                    val jobs = ApiClient.pollJobs()
                    jobs.forEach { job ->
                        pendingJobs[job.id] = job
                        sendSms(job)
                    }
                } catch (e: Exception) {
                    Log.w(TAG, "poll error: ${e.message}")
                }
                delay(POLL_MS)
            }
        }
    }

    private fun startHeartbeat() {
        scope.launch {
            while (isActive) {
                try { ApiClient.postHeartbeat() } catch (_: Exception) {}
                delay(HEARTBEAT_MS)
            }
        }
    }

    @Suppress("DEPRECATION")
    private fun sendSms(job: SmsJob) {
        val smsManager = getSystemService(SmsManager::class.java)

        // Android 14 routes implicit broadcasts away from non-exported receivers; bind
        // the intents to our package so the SmsSentReceiver / SmsDeliveredReceiver fire.
        val sentIntent = PendingIntent.getBroadcast(
            this,
            job.id,
            Intent(ACTION_SMS_SENT).setPackage(packageName).putExtra(EXTRA_JOB_ID, job.id),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val deliveredIntent = PendingIntent.getBroadcast(
            this,
            job.id + 100_000,
            Intent(ACTION_SMS_DELIVERED).setPackage(packageName).putExtra(EXTRA_JOB_ID, job.id),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

        val parts = smsManager.divideMessage(job.body)
        if (parts.size == 1) {
            smsManager.sendTextMessage(job.toNumber, null, job.body, sentIntent, deliveredIntent)
        } else {
            val sentList = ArrayList<PendingIntent>(parts.size).also { it.add(sentIntent) }
            val deliveredList = ArrayList<PendingIntent>(parts.size).also { it.add(deliveredIntent) }
            smsManager.sendMultipartTextMessage(job.toNumber, null, parts, sentList, deliveredList)
        }
        Log.d(TAG, "SMS dispatched: job=${job.id} to=${job.toNumber}")
    }

    fun onSmsSent(jobId: Int, resultCode: Int) {
        val result = if (resultCode == Activity.RESULT_OK) "SENT" else "FAILED"
        val errorCode = if (resultCode != Activity.RESULT_OK) resultCode else null
        scope.launch { ApiClient.postReceipt(jobId, result, errorCode) }
    }

    fun onSmsDelivered(jobId: Int) {
        scope.launch { ApiClient.postReceipt(jobId, "DELIVERED") }
        pendingJobs.remove(jobId)
    }

    private fun createNotificationChannel() {
        val chan = NotificationChannel(CHANNEL_ID, "SMS Worker", NotificationManager.IMPORTANCE_LOW)
        getSystemService(NotificationManager::class.java).createNotificationChannel(chan)
    }

    private fun buildNotification(text: String): Notification =
        NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("SmsSaaS Worker")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.sym_action_chat)
            .setOngoing(true)
            .build()

    companion object {
        private const val TAG = "SmsWorkerService"
        private const val NOTIF_ID = 1
        private const val CHANNEL_ID = "sms_worker"
        private const val POLL_MS = 3_000L
        private const val HEARTBEAT_MS = 30_000L

        const val ACTION_SMS_SENT = "com.smssaas.app.SMS_SENT"
        const val ACTION_SMS_DELIVERED = "com.smssaas.app.SMS_DELIVERED"
        const val EXTRA_JOB_ID = "job_id"
    }
}
