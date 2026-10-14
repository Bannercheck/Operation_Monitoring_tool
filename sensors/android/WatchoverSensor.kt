// Watchover mobile sensor for Android (reference implementation, single file, no dependency beyond OkHttp if you use it).
//
//   class App : Application() { override fun onCreate() { super.onCreate()
//       WatchoverSensor.start(this, endpoint = "https://watchover.sirket.com.tr/mobile", key = "pk_...") } }
//   OkHttpClient.Builder().addInterceptor(WatchoverSensor.interceptor()).build()   // timing + traceparent on every call
//   WatchoverSensor.setUser(personnelNo)     // pseudonymised by the server, never stored as given
//   WatchoverSensor.action("Bordro indir")   // optional business step marks
//
// Records: app start (cold / warm), screen views (Activity resumes; Fragments with screen()), network calls (method, URL without
// query, status, duration, W3C traceparent so the backend logs join the same trace), uncaught exceptions as crashes.
// Never records: input fields, screen content, location, contacts, query strings, request / response bodies.
// Batches are kept in memory, sent every 10 s and when the app goes to background; on failure they wait for the next try.
package com.watchover.sensor

import android.app.Activity
import android.app.Application
import android.os.Build
import android.os.Bundle
import android.os.SystemClock
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.security.SecureRandom
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

object WatchoverSensor {
    private val rnd = SecureRandom()
    private val queue = ArrayList<JSONObject>()
    private val io = Executors.newSingleThreadScheduledExecutor()
    private var endpoint = ""
    private var key = ""
    private var version = ""
    private var user: String? = null
    private var session = hex(12)
    private var lastActive = System.currentTimeMillis()
    private var started = false
    @Volatile private var current: Step? = null

    private class Step(val trace: String, val span: String, val start: Long)

    fun hex(bytes: Int): String { val b = ByteArray(bytes); rnd.nextBytes(b); return b.joinToString("") { "%02x".format(it) } }

    @JvmStatic
    fun start(app: Application, endpoint: String, key: String) {
        if (started) return
        started = true
        this.endpoint = endpoint.trimEnd('/'); this.key = key
        version = try { app.packageManager.getPackageInfo(app.packageName, 0).versionName ?: "" } catch (e: Exception) { "" }
        val processStart = if (Build.VERSION.SDK_INT >= 24) android.os.Process.getStartElapsedRealtime() else SystemClock.elapsedRealtime()
        var firstResume = true
        app.registerActivityLifecycleCallbacks(object : Application.ActivityLifecycleCallbacks {
            override fun onActivityResumed(a: Activity) {
                touch()
                if (firstResume) {
                    firstResume = false
                    push(JSONObject().put("t", "start").put("ts", System.currentTimeMillis()).put("dur", SystemClock.elapsedRealtime() - processStart).put("cold", true))
                }
                screen(a.javaClass.simpleName.removeSuffix("Activity"))
            }
            override fun onActivityPaused(a: Activity) { flushAsync() }
            override fun onActivityCreated(a: Activity, b: Bundle?) {}
            override fun onActivityStarted(a: Activity) {}
            override fun onActivityStopped(a: Activity) {}
            override fun onActivitySaveInstanceState(a: Activity, b: Bundle) {}
            override fun onActivityDestroyed(a: Activity) {}
        })
        val previous = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { t, e ->
            push(JSONObject().put("t", "crash").put("ts", System.currentTimeMillis()).put("msg", "${e.javaClass.name}: ${e.message ?: ""}".take(300))
                .put("stack", e.stackTrace.take(15).joinToString("\n") { "at $it" }))
            try { send(drain()) } catch (_: Exception) {}
            previous?.uncaughtException(t, e)
        }
        io.scheduleWithFixedDelay({ flush() }, 10, 10, TimeUnit.SECONDS)
    }

    @JvmStatic fun setUser(id: String?) { user = id }

    /** A screen became visible (call from Fragments / Compose destinations too). */
    @JvmStatic fun screen(name: String) {
        val s = Step(hex(16), hex(8), System.currentTimeMillis()); current = s
        push(JSONObject().put("t", "view").put("ts", s.start).put("name", name.take(80)).put("trace", s.trace).put("span", s.span))
    }

    /** A business step the user took ("Bordro indir"); calls made in the next seconds belong to its trace. */
    @JvmStatic fun action(name: String) {
        val s = Step(hex(16), hex(8), System.currentTimeMillis()); current = s
        push(JSONObject().put("t", "action").put("ts", s.start).put("name", name.replace(Regex("\\d{4,}"), "#").take(60)).put("trace", s.trace).put("span", s.span))
    }

    @JvmStatic fun error(t: Throwable) {
        push(JSONObject().put("t", "error").put("ts", System.currentTimeMillis()).put("msg", "${t.javaClass.simpleName}: ${t.message ?: ""}".take(300))
            .put("stack", t.stackTrace.take(10).joinToString("\n") { "at $it" }))
    }

    /** Values for one outgoing call: the traceparent header to set and the ids to report. */
    fun beginCall(): Triple<String, String, String?> {
        val s = current?.takeIf { System.currentTimeMillis() - it.start < 10_000 }
        val trace = s?.trace ?: hex(16); val span = hex(8)
        return Triple(trace, span, s?.span)
    }

    fun endCall(method: String, url: String, status: Int, startMs: Long, trace: String, span: String, parent: String?) {
        push(JSONObject().put("t", "http").put("ts", startMs).put("method", method).put("url", url.substringBefore('?').substringBefore('#'))
            .put("status", status).put("dur", System.currentTimeMillis() - startMs).put("trace", trace).put("span", span).apply { if (parent != null) put("parent", parent) })
    }

    /** OkHttp interceptor (add with addInterceptor). Uses reflection-free plain types so this file compiles without OkHttp if unused. */
    fun interceptor(): okhttp3.Interceptor = okhttp3.Interceptor { chain ->
        val req = chain.request()
        if (req.url.toString().startsWith(endpoint)) return@Interceptor chain.proceed(req)
        val (trace, span, parent) = beginCall()
        val t0 = System.currentTimeMillis()
        try {
            val resp = chain.proceed(req.newBuilder().header("traceparent", "00-$trace-$span-01").build())
            endCall(req.method, req.url.toString(), resp.code, t0, trace, span, parent); resp
        } catch (e: java.io.IOException) {
            endCall(req.method, req.url.toString(), 0, t0, trace, span, parent); throw e
        }
    }

    private fun touch() {
        val now = System.currentTimeMillis()
        if (now - lastActive > 30 * 60 * 1000) session = hex(12)          // a new session after 30 minutes in the background
        lastActive = now
    }

    private fun push(e: JSONObject) { synchronized(queue) { queue.add(e); if (queue.size >= 30) flushAsync() } }
    private fun drain(): List<JSONObject> = synchronized(queue) { val c = ArrayList(queue); queue.clear(); c }
    private fun flushAsync() { io.execute { flush() } }

    private fun flush() { val batch = drain(); if (batch.isEmpty()) return; try { send(batch) } catch (_: Exception) { synchronized(queue) { if (queue.size < 1000) queue.addAll(0, batch) } } }

    private fun send(batch: List<JSONObject>) {
        if (batch.isEmpty() || endpoint.isEmpty()) return
        val body = JSONObject().put("session", session).put("platform", "android").put("version", version)
            .put("device", JSONObject().put("os", "Android ${Build.VERSION.RELEASE}").put("model", Build.MODEL))
            .put("events", JSONArray(batch)).apply { user?.let { put("user", it) } }
        val c = URL("$endpoint?k=$key").openConnection() as HttpURLConnection
        c.requestMethod = "POST"; c.doOutput = true; c.connectTimeout = 5000; c.readTimeout = 5000
        c.setRequestProperty("Content-Type", "application/json")
        c.outputStream.use { it.write(body.toString().toByteArray()) }
        val code = c.responseCode; c.disconnect()
        if (code >= 500) throw java.io.IOException("receiver $code")
    }
}
