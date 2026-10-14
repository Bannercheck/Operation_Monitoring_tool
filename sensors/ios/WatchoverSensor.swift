// Watchover mobile sensor for iOS (reference implementation, single file, Foundation + UIKit only).
//
//   WatchoverSensor.start(endpoint: "https://watchover.sirket.com.tr/mobile", key: "pk_...")   // in application(_:didFinishLaunching...)
//   let session = WatchoverSensor.urlSession()      // use for API calls: timed, traceparent header added
//   WatchoverSensor.screen("Bordro")                // in viewDidAppear (or SwiftUI .onAppear)
//   WatchoverSensor.setUser(personnelNo)            // pseudonymised by the server, never stored as given
//
// Records: app start, screen views, network calls (method, URL without query, status, duration, W3C traceparent), uncaught
// NSExceptions as crashes. Never records: text fields, screen content, location, contacts, query strings, bodies.
import Foundation
#if canImport(UIKit)
import UIKit
#endif

public final class WatchoverSensor: NSObject, URLSessionTaskDelegate {
    public static let shared = WatchoverSensor()
    private var endpoint = "", key = "", version = "", user: String?
    private var session = WatchoverSensor.hex(12)
    private var queue: [[String: Any]] = []
    private let lock = NSLock()
    private var timer: Timer?
    private var current: (trace: String, span: String, start: Date)?
    private static let launch = Date()

    static func hex(_ n: Int) -> String { (0..<n).map { _ in String(format: "%02x", UInt8.random(in: 0...255)) }.joined() }
    static func ms(_ d: Date = Date()) -> Double { (d.timeIntervalSince1970 * 1000).rounded() }

    public static func start(endpoint: String, key: String) {
        let s = shared
        s.endpoint = endpoint.hasSuffix("/") ? String(endpoint.dropLast()) : endpoint
        s.key = key
        s.version = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? ""
        s.push(["t": "start", "ts": ms(launch), "dur": Date().timeIntervalSince(launch) * 1000, "cold": true])
        NSSetUncaughtExceptionHandler { e in
            WatchoverSensor.shared.push(["t": "crash", "ts": WatchoverSensor.ms(), "msg": "\(e.name.rawValue): \(e.reason ?? "")".prefix(300).description,
                                         "stack": e.callStackSymbols.prefix(15).joined(separator: "\n")])
            WatchoverSensor.shared.flush(sync: true)
        }
        DispatchQueue.main.async { s.timer = Timer.scheduledTimer(withTimeInterval: 10, repeats: true) { _ in s.flush() } }
        #if canImport(UIKit)
        NotificationCenter.default.addObserver(forName: UIApplication.didEnterBackgroundNotification, object: nil, queue: nil) { _ in s.flush() }
        #endif
    }

    public static func setUser(_ id: String?) { shared.user = id }

    public static func screen(_ name: String) {
        let c = (hex(16), hex(8), Date()); shared.current = c
        shared.push(["t": "view", "ts": ms(c.2), "name": String(name.prefix(80)), "trace": c.0, "span": c.1])
    }

    public static func action(_ name: String) {
        let c = (hex(16), hex(8), Date()); shared.current = c
        let clean = name.replacingOccurrences(of: "\\d{4,}", with: "#", options: .regularExpression)
        shared.push(["t": "action", "ts": ms(c.2), "name": String(clean.prefix(60)), "trace": c.0, "span": c.1])
    }

    public static func error(_ error: Error) { shared.push(["t": "error", "ts": ms(), "msg": String(String(describing: error).prefix(300))]) }

    /// A URLSession whose requests are timed and carry a traceparent header (pass your own configuration to keep its settings).
    public static func urlSession(configuration: URLSessionConfiguration = .default) -> URLSession {
        URLSession(configuration: configuration, delegate: shared, delegateQueue: nil)
    }

    /// For requests you build yourself: returns the request with traceparent and a closure to call when it finishes.
    public static func track(_ request: URLRequest) -> (URLRequest, (Int) -> Void) {
        var r = request
        let s = shared.current.flatMap { Date().timeIntervalSince($0.start) < 10 ? $0 : nil }
        let trace = s?.trace ?? hex(16), span = hex(8), t0 = Date()
        r.setValue("00-\(trace)-\(span)-01", forHTTPHeaderField: "traceparent")
        return (r, { status in
            var e: [String: Any] = ["t": "http", "ts": ms(t0), "method": request.httpMethod ?? "GET", "url": strip(request.url), "status": status,
                                    "dur": Date().timeIntervalSince(t0) * 1000, "trace": trace, "span": span]
            if let p = s?.span { e["parent"] = p }
            shared.push(e)
        })
    }

    // URLSessionTaskDelegate: metrics for calls made through urlSession()
    public func urlSession(_ session: URLSession, task: URLSessionTask, didFinishCollecting metrics: URLSessionTaskMetrics) {
        guard let req = task.originalRequest, !(req.url?.absoluteString.hasPrefix(endpoint) ?? false) else { return }
        let status = (task.response as? HTTPURLResponse)?.statusCode ?? 0
        let tp = req.value(forHTTPHeaderField: "traceparent")?.split(separator: "-")
        push(["t": "http", "ts": WatchoverSensor.ms(metrics.taskInterval.start), "method": req.httpMethod ?? "GET", "url": WatchoverSensor.strip(req.url),
              "status": status, "dur": metrics.taskInterval.duration * 1000, "trace": tp.map { String($0[1]) } ?? WatchoverSensor.hex(16),
              "span": tp.map { String($0[2]) } ?? WatchoverSensor.hex(8)])
    }

    public func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                           completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(request) }

    static func strip(_ u: URL?) -> String {
        guard let u = u, var c = URLComponents(url: u, resolvingAgainstBaseURL: false) else { return "" }
        c.query = nil; c.fragment = nil
        return c.string ?? ""
    }

    private func push(_ e: [String: Any]) {
        lock.lock(); queue.append(e); let n = queue.count; lock.unlock()
        if n >= 30 { flush() }
    }

    func flush(sync: Bool = false) {
        lock.lock(); let batch = queue; queue.removeAll(); lock.unlock()
        guard !batch.isEmpty, !endpoint.isEmpty, let url = URL(string: "\(endpoint)?k=\(key)") else { return }
        var body: [String: Any] = ["session": session, "platform": "ios", "version": version, "events": batch]
        #if canImport(UIKit)
        body["device"] = ["os": "iOS \(UIDevice.current.systemVersion)", "model": UIDevice.current.model]
        #endif
        if let u = user { body["user"] = u }
        var req = URLRequest(url: url); req.httpMethod = "POST"; req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        let sem = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: req) { _, resp, err in
            if err != nil || ((resp as? HTTPURLResponse)?.statusCode ?? 500) >= 500 {
                self.lock.lock(); if self.queue.count < 1000 { self.queue.insert(contentsOf: batch, at: 0) }; self.lock.unlock()
            }
            sem.signal()
        }.resume()
        if sync { _ = sem.wait(timeout: .now() + 2) }
    }
}
