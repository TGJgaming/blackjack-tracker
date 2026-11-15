import UIKit
import WebKit
import ReplayKit
import Network
import os

/// Unified custom URL scheme handler serving both the bundled tracker UI (`bjtracker://localhost/...`)
/// and live downscaled ReplayKit broadcast frames (`/companion-frame/latest` and `companion-frame://latest`).
/// Using a fixed custom scheme origin ensures persistent `localStorage` and same-origin XHR polling.
final class CompanionSchemeHandler: NSObject, WKURLSchemeHandler {
    private let logger = Logger(subsystem: "com.tgjgaming.blackjacktracker", category: "SchemeHandler")
    private let lock = NSLock()
    private var ring: SharedFrameRing?
    private var stoppedTaskIds = Set<ObjectIdentifier>()
    private var loggedRingReady = false
    private var loggedFrameDims: (Int, Int) = (0, 0)
    private var loggedInitialUI = false

    private func isTaskStopped(_ task: WKURLSchemeTask) -> Bool {
        lock.lock()
        let stopped = stoppedTaskIds.contains(ObjectIdentifier(task))
        lock.unlock()
        return stopped
    }

    private func respond(
        to task: WKURLSchemeTask,
        url: URL,
        statusCode: Int,
        headers: [String: String],
        body: Data? = nil
    ) {
        guard !isTaskStopped(task) else { return }
        var allHeaders = headers
        if let body = body {
            allHeaders["Content-Length"] = String(body.count)
        } else {
            allHeaders["Content-Length"] = "0"
        }
        guard let response = HTTPURLResponse(
            url: url,
            statusCode: statusCode,
            httpVersion: "HTTP/1.1",
            headerFields: allHeaders
        ) else {
            if !isTaskStopped(task) {
                task.didFailWithError(URLError(.cannotParseResponse))
            }
            return
        }
        task.didReceive(response)
        if let body = body, !body.isEmpty {
            guard !isTaskStopped(task) else { return }
            task.didReceive(body)
        }
        guard !isTaskStopped(task) else { return }
        task.didFinish()
    }

    func webView(_ webView: WKWebView, start urlSchemeTask: WKURLSchemeTask) {
        let taskId = ObjectIdentifier(urlSchemeTask)
        defer {
            lock.lock()
            stoppedTaskIds.remove(taskId)
            lock.unlock()
        }

        guard let url = urlSchemeTask.request.url else {
            if !isTaskStopped(urlSchemeTask) {
                urlSchemeTask.didFailWithError(URLError(.badURL))
            }
            return
        }

        let scheme = (url.scheme ?? "").lowercased()
        let rawPath = url.path.isEmpty ? "/" : url.path

        if scheme == "companion-frame" || (scheme == "bjtracker" && (rawPath == "/companion-frame/latest" || rawPath.hasPrefix("/companion-frame/"))) {
            if ring == nil {
                ring = SharedFrameRing(isWriter: false)
                if let ring = ring, !loggedRingReady {
                    loggedRingReady = true
                    logger.info("Connected SharedFrameRing reader: \(ring.storageDescription, privacy: .public)")
                }
            }
            if let packet = ring?.readLatestPacket() {
                if packet.count >= SharedFrameRing.headerBytes {
                    var w: UInt32 = 0
                    var h: UInt32 = 0
                    _ = withUnsafeMutableBytes(of: &w) { packet.copyBytes(to: $0, from: 4..<8) }
                    _ = withUnsafeMutableBytes(of: &h) { packet.copyBytes(to: $0, from: 8..<12) }
                    let dims = (Int(UInt32(littleEndian: w)), Int(UInt32(littleEndian: h)))
                    if dims != loggedFrameDims {
                        loggedFrameDims = dims
                        logger.info("Streaming broadcast frame dimensions: \(dims.0)x\(dims.1)")
                    }
                }
                respond(
                    to: urlSchemeTask,
                    url: url,
                    statusCode: 200,
                    headers: [
                        "Content-Type": "application/octet-stream",
                        "Cache-Control": "no-store",
                        "Access-Control-Allow-Origin": "*"
                    ],
                    body: packet
                )
            } else {
                respond(
                    to: urlSchemeTask,
                    url: url,
                    statusCode: 204,
                    headers: [
                        "Cache-Control": "no-store",
                        "Access-Control-Allow-Origin": "*"
                    ]
                )
            }
            return
        }

        if scheme == "bjtracker" {
            if rawPath == "/favicon.ico" {
                respond(to: urlSchemeTask, url: url, statusCode: 204, headers: ["Cache-Control": "no-store"])
                return
            }

            let asset: (name: String, ext: String, mime: String)?
            switch rawPath {
            case "/", "/index.html":
                asset = ("index", "html", "text/html; charset=utf-8")
            case "/companion/ipad/default_pack.js":
                asset = ("default_pack", "js", "application/javascript; charset=utf-8")
            case "/companion/ipad/companion_engine.js":
                asset = ("companion_engine", "js", "application/javascript; charset=utf-8")
            default:
                asset = nil
            }

            guard let target = asset,
                  let fileUrl = Bundle.main.url(forResource: target.name, withExtension: target.ext),
                  let data = try? Data(contentsOf: fileUrl) else {
                logger.error("Unmapped or missing bundled resource requested: \(rawPath, privacy: .public)")
                let notFoundData = Data("Not Found".utf8)
                respond(
                    to: urlSchemeTask,
                    url: url,
                    statusCode: 404,
                    headers: [
                        "Content-Type": "text/plain; charset=utf-8",
                        "Cache-Control": "no-store"
                    ],
                    body: notFoundData
                )
                return
            }

            if !loggedInitialUI && (rawPath == "/" || rawPath == "/index.html") {
                loggedInitialUI = true
                logger.info("Serving bundled index.html (\(data.count) bytes) on bjtracker://localhost")
            }

            respond(
                to: urlSchemeTask,
                url: url,
                statusCode: 200,
                headers: [
                    "Content-Type": target.mime,
                    "Cache-Control": "no-store"
                ],
                body: data
            )
            return
        }

        if !isTaskStopped(urlSchemeTask) {
            urlSchemeTask.didFailWithError(URLError(.unsupportedURL))
        }
    }

    func webView(_ webView: WKWebView, stop urlSchemeTask: WKURLSchemeTask) {
        lock.lock()
        stoppedTaskIds.insert(ObjectIdentifier(urlSchemeTask))
        lock.unlock()
    }
}

/// Lightweight loopback WebSocket relay (`ws://127.0.0.1:8765`) so the optional Safari Keep-Alive
/// Web Extension (`safari-web-extension://...`) can receive `{ "type": "ext_reload" }` commands
/// during Watch-mode Place Bets windows without any DOM/content-script injection on Playtech.
final class KeepaliveExtensionRelay {
    private let logger = Logger(subsystem: "com.tgjgaming.blackjacktracker", category: "KeepaliveRelay")
    private var listener: NWListener?
    private var extConnections: [NWConnection] = []
    var onConnectionCountChanged: ((Int) -> Void)?

    func start(port: UInt16 = 8765) {
        guard listener == nil, let nwPort = NWEndpoint.Port(rawValue: port) else { return }
        let params = NWParameters.tcp
        params.allowLocalEndpointReuse = true
        params.requiredLocalEndpoint = NWEndpoint.hostPort(host: .ipv4(.loopback), port: nwPort)

        let wsOptions = NWProtocolWebSocket.Options()
        wsOptions.autoReplyPing = true
        params.defaultProtocolStack.applicationProtocols.insert(wsOptions, at: 0)

        do {
            let newListener = try NWListener(using: params)
            newListener.stateUpdateHandler = { [weak self, weak newListener] state in
                guard let self = self, let newListener = newListener else { return }
                switch state {
                case .ready:
                    self.logger.info("Loopback WebSocket relay ready on 127.0.0.1:\(port)")
                case .failed(let error):
                    self.logger.error("KeepaliveRelay failed: \(String(describing: error), privacy: .public)")
                    newListener.cancel()
                    if self.listener === newListener {
                        self.listener = nil
                    }
                case .cancelled:
                    self.logger.info("KeepaliveRelay cancelled")
                default:
                    break
                }
            }
            newListener.newConnectionHandler = { [weak self] conn in
                self?.accept(conn)
            }
            newListener.start(queue: .main)
            self.listener = newListener
        } catch {
            logger.error("Failed to create NWListener on 127.0.0.1:\(port): \(error.localizedDescription, privacy: .public)")
        }
    }

    func stop() {
        listener?.cancel()
        listener = nil
        for conn in extConnections {
            conn.cancel()
        }
        extConnections.removeAll()
    }

    private func accept(_ conn: NWConnection) {
        extConnections.append(conn)
        onConnectionCountChanged?(extConnections.count)
        logger.info("Safari Keep-Alive extension connected (active=\(self.extConnections.count))")
        conn.stateUpdateHandler = { [weak self, weak conn] state in
            guard let self = self, let conn = conn else { return }
            switch state {
            case .failed, .cancelled:
                self.extConnections.removeAll { $0 === conn }
                self.onConnectionCountChanged?(self.extConnections.count)
                self.logger.info("Safari Keep-Alive extension disconnected (active=\(self.extConnections.count))")
            default:
                break
            }
        }
        conn.start(queue: .main)
        receiveNext(on: conn)
    }

    private func receiveNext(on conn: NWConnection) {
        conn.receiveMessage { [weak self, weak conn] _, _, _, error in
            guard let self = self, let conn = conn, error == nil else { return }
            self.receiveNext(on: conn)
        }
    }

    @discardableResult
    func broadcastReload() -> Bool {
        guard !extConnections.isEmpty else { return false }
        let payload = #"{"type":"ext_reload"}"#.data(using: .utf8)!
        let meta = NWProtocolWebSocket.Metadata(opcode: .text)
        let ctx = NWConnection.ContentContext(identifier: "wsText", metadata: [meta])
        for conn in extConnections {
            conn.send(content: payload, contentContext: ctx, isComplete: true, completion: .idempotent)
        }
        return true
    }
}

final class TrackerViewController: UIViewController, WKScriptMessageHandler, WKNavigationDelegate, WKUIDelegate {
    private let logger = Logger(subsystem: "com.tgjgaming.blackjacktracker", category: "TrackerVC")
    private var webView: WKWebView!
    private var broadcastPicker: RPSystemBroadcastPickerView?
    private let schemeHandler = CompanionSchemeHandler()
    private let extRelay = KeepaliveExtensionRelay()

    override var preferredStatusBarStyle: UIStatusBarStyle {
        .lightContent
    }

    deinit {
        webView?.configuration.userContentController.removeScriptMessageHandler(forName: "ipadCompanion")
        extRelay.stop()
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = UIColor(red: 15.0 / 255.0, green: 17.0 / 255.0, blue: 21.0 / 255.0, alpha: 1.0)

        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        config.setURLSchemeHandler(schemeHandler, forURLScheme: "bjtracker")
        config.setURLSchemeHandler(schemeHandler, forURLScheme: "companion-frame")
        config.userContentController.add(self, name: "ipadCompanion")
        config.allowsInlineMediaPlayback = true

        webView = WKWebView(frame: .zero, configuration: config)
        webView.translatesAutoresizingMaskIntoConstraints = false
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.isOpaque = false
        webView.backgroundColor = view.backgroundColor
        webView.scrollView.backgroundColor = view.backgroundColor
        if #available(iOS 16.4, *) {
            webView.isInspectable = true
        }
        view.addSubview(webView)

        NSLayoutConstraint.activate([
            webView.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            webView.leadingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.trailingAnchor),
            webView.bottomAnchor.constraint(equalTo: view.bottomAnchor)
        ])

        setupBroadcastPicker()
        setupKeepaliveRelay()
        loadTrackerApp()
    }

    private func setupBroadcastPicker() {
        let picker = RPSystemBroadcastPickerView(frame: .zero)
        picker.translatesAutoresizingMaskIntoConstraints = false
        picker.preferredExtension = (Bundle.main.bundleIdentifier ?? "com.tgjgaming.blackjacktracker") + ".BroadcastExtension"
        picker.showsMicrophoneButton = false
        picker.alpha = 0.015
        picker.isUserInteractionEnabled = false
        view.addSubview(picker)
        NSLayoutConstraint.activate([
            picker.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 4),
            picker.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            picker.widthAnchor.constraint(equalToConstant: 44),
            picker.heightAnchor.constraint(equalToConstant: 44)
        ])
        self.broadcastPicker = picker
    }

    private func setupKeepaliveRelay() {
        extRelay.onConnectionCountChanged = { [weak self] count in
            DispatchQueue.main.async {
                let connected = count > 0 ? "true" : "false"
                self?.webView?.evaluateJavaScript(
                    "window.__ipadCompanionSetExtConnected && window.__ipadCompanionSetExtConnected(\(connected));",
                    completionHandler: nil
                )
            }
        }
        extRelay.start(port: 8765)
    }

    private func loadTrackerApp() {
        guard let url = URL(string: "bjtracker://localhost/index.html") else { return }
        logger.info("Loading tracker UI from \(url.absoluteString, privacy: .public)")
        webView.load(URLRequest(url: url))
    }

    // MARK: - WKNavigationDelegate

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        logger.info("Tracker WKWebView finished initial navigation")
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        handleNavigationFailure(error)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        handleNavigationFailure(error)
    }

    private func handleNavigationFailure(_ error: Error) {
        let nsErr = error as NSError
        if nsErr.domain == NSURLErrorDomain && nsErr.code == NSURLErrorCancelled {
            return
        }
        logger.error("WKWebView navigation failed: \(error.localizedDescription, privacy: .public)")
        let alert = UIAlertController(
            title: "Tracker Load Error",
            message: error.localizedDescription,
            preferredStyle: .alert
        )
        alert.addAction(UIAlertAction(title: "Retry", style: .default) { [weak self] _ in
            self?.loadTrackerApp()
        })
        alert.addAction(UIAlertAction(title: "Dismiss", style: .cancel))
        presentAlertSafely(alert, onSkipped: {})
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.cancel)
            return
        }
        let scheme = (url.scheme ?? "").lowercased()
        let path = url.path

        if scheme == "bjtracker" {
            if path == "/" || path.isEmpty || path == "/index.html" {
                decisionHandler(.allow)
                return
            }
            if path.contains("baccarat-tracker") {
                decisionHandler(.cancel)
                let alert = UIAlertController(
                    title: "Standalone iPadOS App",
                    message: "Baccarat Tracker is a separate companion web app and is not bundled inside this Blackjack Tracker iPad build.",
                    preferredStyle: .alert
                )
                alert.addAction(UIAlertAction(title: "OK", style: .default))
                presentAlertSafely(alert, onSkipped: {})
                return
            }
            decisionHandler(.cancel)
            return
        }

        if scheme == "about" || scheme == "blob" || scheme == "data" {
            decisionHandler(.allow)
            return
        }

        if (scheme == "http" || scheme == "https"), navigationAction.navigationType == .linkActivated {
            UIApplication.shared.open(url)
            decisionHandler(.cancel)
            return
        }

        decisionHandler(.allow)
    }

    // MARK: - WKUIDelegate

    private func presentAlertSafely(_ alert: UIAlertController, onSkipped: @escaping () -> Void) {
        var host: UIViewController = self
        while let presented = host.presentedViewController, !presented.isBeingDismissed {
            host = presented
        }
        if host is UIAlertController {
            onSkipped()
            return
        }
        host.present(alert, animated: true)
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptAlertPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping () -> Void
    ) {
        let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "OK", style: .default) { _ in
            completionHandler()
        })
        presentAlertSafely(alert, onSkipped: completionHandler)
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptConfirmPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping (Bool) -> Void
    ) {
        let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in
            completionHandler(false)
        })
        alert.addAction(UIAlertAction(title: "OK", style: .default) { _ in
            completionHandler(true)
        })
        presentAlertSafely(alert) {
            completionHandler(false)
        }
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptTextInputPanelWithPrompt prompt: String,
        defaultText: String?,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping (String?) -> Void
    ) {
        let alert = UIAlertController(title: nil, message: prompt, preferredStyle: .alert)
        alert.addTextField { tf in
            tf.text = defaultText
        }
        alert.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in
            completionHandler(nil)
        })
        alert.addAction(UIAlertAction(title: "OK", style: .default) { _ in
            completionHandler(alert.textFields?.first?.text)
        })
        presentAlertSafely(alert) {
            completionHandler(nil)
        }
    }

    // MARK: - WKScriptMessageHandler

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "ipadCompanion",
              let body = message.body as? [String: Any],
              let action = body["action"] as? String else {
            return
        }
        switch action {
        case "startBroadcast":
            guard let picker = broadcastPicker else { return }
            logger.info("Triggering iPadOS ReplayKit broadcast picker")
            var triggered = false
            for subview in picker.subviews {
                if let btn = subview as? UIButton {
                    btn.sendActions(for: .touchUpInside)
                    triggered = true
                    break
                }
            }
            if !triggered {
                logger.error("RPSystemBroadcastPickerView internal button not found")
            }

        case "reloadLeftSafari":
            let sent = extRelay.broadcastReload()
            logger.info("Relayed Safari keep-alive reload command (delivered=\(sent))")

        case "exportFile":
            guard let rawName = body["filename"] as? String,
                  let text = body["text"] as? String else {
                return
            }
            let safeName = (rawName as NSString).lastPathComponent
            let cleanName = safeName.isEmpty ? "tracker_export.txt" : safeName
            let outURL = FileManager.default.temporaryDirectory.appendingPathComponent(cleanName)
            do {
                try Data(text.utf8).write(to: outURL, options: .atomic)
                let activityVC = UIActivityViewController(activityItems: [outURL], applicationActivities: nil)
                if let popover = activityVC.popoverPresentationController {
                    popover.sourceView = view
                    popover.sourceRect = CGRect(x: view.bounds.midX, y: 80, width: 1, height: 1)
                    popover.permittedArrowDirections = .up
                }
                var host: UIViewController = self
                while let presented = host.presentedViewController, !presented.isBeingDismissed {
                    host = presented
                }
                if !(host is UIAlertController) {
                    host.present(activityVC, animated: true)
                    logger.info("Presented iOS Share Sheet for export '\(cleanName, privacy: .public)' (\(text.utf8.count) bytes)")
                } else {
                    logger.error("Skipped Share Sheet for export '\(cleanName, privacy: .public)' because an alert is already active")
                }
            } catch {
                logger.error("Failed to stage export file '\(cleanName, privacy: .public)': \(error.localizedDescription, privacy: .public)")
            }

        case "jsError":
            let msg = (body["message"] as? String) ?? "Unknown JS error"
            let src = (body["source"] as? String) ?? ""
            let line = (body["line"] as? Int) ?? 0
            logger.error("WKWebView JS Error [\(src, privacy: .public):\(line)]: \(msg, privacy: .public)")

        default:
            break
        }
    }
}
