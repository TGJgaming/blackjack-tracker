import UIKit
import WebKit
import ReplayKit
import Network
import os

/// Loopback TCP receiver (`127.0.0.1:8766`) paired with `LoopbackFrameSender` in
/// `BroadcastExtension`. Enables full live frame streaming even when free Apple ID
/// sideloading (Sideloadly / AltStore / SideStore) strips `com.apple.security.application-groups`.
final class LoopbackFrameReceiver {
    private final class Session {
        let conn: NWConnection
        var rxBuffer = Data()
        init(conn: NWConnection) {
            self.conn = conn
        }
    }

    private let logger = Logger(subsystem: "com.tgjgaming.blackjacktracker", category: "LoopbackRx")
    private let queue = DispatchQueue(label: "com.tgjgaming.blackjacktracker.loopbackFrameRx", qos: .userInitiated)
    private let stateLock = NSLock()

    private var listener: NWListener?
    private var activeSession: Session?
    private var latestPacket: Data?
    private var broadcastActive = false
    private var hasConnectedOnce = false
    private var loggedFrameDims: (Int, Int) = (0, 0)

    func startIfNeeded(port: UInt16 = FrameIPCConstants.loopbackPort) {
        queue.async { [weak self] in
            self?._startOnQueue(port: port)
        }
    }

    private func _startOnQueue(port: UInt16) {
        guard listener == nil, let nwPort = NWEndpoint.Port(rawValue: port) else { return }
        let tcpOpts = NWProtocolTCP.Options()
        tcpOpts.noDelay = true
        let params = NWParameters(tls: nil, tcp: tcpOpts)
        params.allowLocalEndpointReuse = true
        params.preferNoProxies = true
        params.requiredLocalEndpoint = NWEndpoint.hostPort(host: .ipv4(.loopback), port: nwPort)

        do {
            let newListener = try NWListener(using: params)
            newListener.stateUpdateHandler = { [weak self, weak newListener] state in
                guard let self = self, let newListener = newListener else { return }
                switch state {
                case .ready:
                    self.logger.info("Loopback frame receiver listening on 127.0.0.1:\(port)")
                case .failed(let err):
                    self.logger.error("Loopback frame receiver failed: \(String(describing: err), privacy: .public)")
                    newListener.cancel()
                    if self.listener === newListener {
                        self.listener = nil
                        self.queue.asyncAfter(deadline: .now() + 1.0) { [weak self] in
                            self?._startOnQueue(port: port)
                        }
                    }
                case .cancelled:
                    if self.listener === newListener {
                        self.listener = nil
                    }
                default:
                    break
                }
            }
            newListener.newConnectionHandler = { [weak self] conn in
                self?.accept(conn)
            }
            newListener.start(queue: queue)
            listener = newListener
        } catch {
            logger.error("Failed to create LoopbackFrameReceiver on 127.0.0.1:\(port): \(error.localizedDescription, privacy: .public)")
        }
    }

    func stop() {
        queue.async { [weak self] in
            guard let self = self else { return }
            self.listener?.cancel()
            self.listener = nil
            if let sess = self.activeSession {
                self.closeSession(sess, markFinished: true)
            }
        }
    }

    private func accept(_ conn: NWConnection) {
        if let prev = activeSession {
            closeSession(prev, markFinished: false)
        }
        let session = Session(conn: conn)
        activeSession = session
        stateLock.lock()
        hasConnectedOnce = true
        broadcastActive = true
        stateLock.unlock()

        logger.info("Accepted BroadcastExtension loopback connection")
        conn.stateUpdateHandler = { [weak self, weak session] state in
            guard let self = self, let session = session else { return }
            switch state {
            case .failed, .cancelled:
                self.closeSession(session, markFinished: true)
            default:
                break
            }
        }
        conn.start(queue: queue)
        readNextChunk(on: session)
    }

    private func readNextChunk(on session: Session) {
        session.conn.receive(minimumIncompleteLength: 1, maximumLength: 1_048_576) { [weak self, weak session] data, _, isComplete, error in
            guard let self = self, let session = session else { return }
            guard self.activeSession === session else {
                session.conn.cancel()
                return
            }
            if let data = data, !data.isEmpty {
                session.rxBuffer.append(data)
                self.processBuffer(for: session)
            }
            if isComplete || error != nil {
                self.closeSession(session, markFinished: true)
                return
            }
            self.readNextChunk(on: session)
        }
    }

    private func processBuffer(for session: Session) {
        let maxRecordBytes = FrameIPCConstants.tcpHeaderBytes + SharedFrameRing.capacityBytes
        var newestFrame: Data?
        var sawStarted = false
        var sawFinished = false

        while session.rxBuffer.count >= FrameIPCConstants.tcpHeaderBytes {
            let magic = FrameIPCConstants.readUInt32LE(from: session.rxBuffer, offset: 0)
            let msgType = FrameIPCConstants.readUInt32LE(from: session.rxBuffer, offset: 4)
            let payloadLen = Int(FrameIPCConstants.readUInt32LE(from: session.rxBuffer, offset: 8))

            if magic != FrameIPCConstants.recordMagic || payloadLen < 0 || (FrameIPCConstants.tcpHeaderBytes + payloadLen) > maxRecordBytes {
                logger.error("Corrupt loopback record header (magic=\(magic), len=\(payloadLen)); resetting session")
                closeSession(session, markFinished: true)
                return
            }

            let totalLen = FrameIPCConstants.tcpHeaderBytes + payloadLen
            if session.rxBuffer.count < totalLen {
                break
            }

            let payloadStart = session.rxBuffer.startIndex + FrameIPCConstants.tcpHeaderBytes
            let payloadEnd = payloadStart + payloadLen
            if msgType == FrameIPCConstants.msgFrame {
                if payloadLen >= SharedFrameRing.headerBytes {
                    newestFrame = session.rxBuffer.subdata(in: payloadStart..<payloadEnd)
                    sawStarted = true
                    sawFinished = false
                }
            } else if msgType == FrameIPCConstants.msgBroadcastStarted {
                sawStarted = true
                sawFinished = false
            } else if msgType == FrameIPCConstants.msgBroadcastFinished {
                sawFinished = true
                newestFrame = nil
            }

            if session.rxBuffer.count == totalLen {
                session.rxBuffer.removeAll(keepingCapacity: false)
            } else {
                session.rxBuffer = session.rxBuffer.subdata(in: payloadEnd..<session.rxBuffer.endIndex)
            }
        }

        if newestFrame != nil || sawStarted || sawFinished {
            stateLock.lock()
            if let pkt = newestFrame {
                latestPacket = pkt
                broadcastActive = true
                let w = Int(FrameIPCConstants.readUInt32LE(from: pkt, offset: 4))
                let h = Int(FrameIPCConstants.readUInt32LE(from: pkt, offset: 8))
                if (w, h) != loggedFrameDims {
                    loggedFrameDims = (w, h)
                    logger.info("Streaming loopback broadcast frame dimensions: \(w)x\(h)")
                }
            } else if sawFinished {
                latestPacket = nil
                broadcastActive = false
            } else if sawStarted {
                broadcastActive = true
            }
            stateLock.unlock()
        }
    }

    private func closeSession(_ session: Session, markFinished: Bool) {
        session.conn.cancel()
        session.rxBuffer.removeAll(keepingCapacity: false)
        guard activeSession === session else { return }
        activeSession = nil
        if markFinished {
            stateLock.lock()
            broadcastActive = false
            latestPacket = nil
            stateLock.unlock()
            logger.info("BroadcastExtension loopback session closed")
        }
    }

    func latestState() -> (packet: Data?, broadcastActive: Bool, hasConnectedOnce: Bool) {
        stateLock.lock()
        let result = (latestPacket, broadcastActive, hasConnectedOnce)
        stateLock.unlock()
        return result
    }
}

/// Unified custom URL scheme handler serving both the bundled tracker UI (`bjtracker://localhost/...`)
/// and live downscaled ReplayKit broadcast frames (`/companion-frame/latest` and `companion-frame://latest`).
/// Reads App Group shared memory when available, seamlessly falling back to `LoopbackFrameReceiver`.
final class CompanionSchemeHandler: NSObject, WKURLSchemeHandler {
    private let logger = Logger(subsystem: "com.tgjgaming.blackjacktracker", category: "SchemeHandler")
    private let lock = NSLock()
    private let loopbackReceiver: LoopbackFrameReceiver
    var broadcastExtensionPresent: Bool = false

    private var ring: SharedFrameRing?
    private let appGroupId: String? = SharedFrameRing.availableAppGroupId
    private var stoppedTaskIds = Set<ObjectIdentifier>()
    private var loggedRingReady = false
    private var loggedFrameDims: (Int, Int) = (0, 0)
    private var loggedInitialUI = false

    init(loopbackReceiver: LoopbackFrameReceiver) {
        self.loopbackReceiver = loopbackReceiver
        super.init()
    }

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
        allHeaders["Content-Length"] = String(body?.count ?? 0)
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
            var clientLastSeq: UInt32 = 0
            if let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
               let sinceStr = components.queryItems?.first(where: { $0.name == "since" })?.value,
               let sinceVal = UInt32(sinceStr) {
                clientLastSeq = sinceVal
            }

            let loopbackState = loopbackReceiver.latestState()
            var selectedPacket = loopbackState.packet
            var transport = (selectedPacket != nil) ? "loopback" : (loopbackState.broadcastActive ? "loopback_waiting" : "idle")
            var activeFlag = loopbackState.broadcastActive

            if selectedPacket == nil, appGroupId != nil {
                if ring == nil {
                    ring = SharedFrameRing(isWriter: false)
                    if let ring = ring, !loggedRingReady {
                        loggedRingReady = true
                        logger.info("Connected SharedFrameRing reader: \(ring.storageDescription, privacy: .public)")
                    }
                }
                if let ringPacket = ring?.readLatestPacket() {
                    selectedPacket = ringPacket
                    transport = "appgroup"
                    activeFlag = true
                }
            }

            let diagHeaders: [String: String] = [
                "Cache-Control": "no-store",
                "Access-Control-Allow-Origin": "*",
                "X-BJ-Transport": transport,
                "X-BJ-Broadcast-Active": activeFlag ? "1" : "0",
                "X-BJ-AppGroup-Ok": (appGroupId != nil) ? "1" : "0",
                "X-BJ-BroadcastExt-Present": broadcastExtensionPresent ? "1" : "0"
            ]

            if let packet = selectedPacket, packet.count >= SharedFrameRing.headerBytes {
                let pktSeq = FrameIPCConstants.readUInt32LE(from: packet, offset: 0)
                let w = Int(FrameIPCConstants.readUInt32LE(from: packet, offset: 4))
                let h = Int(FrameIPCConstants.readUInt32LE(from: packet, offset: 8))
                let dims = (w, h)
                if dims != loggedFrameDims {
                    loggedFrameDims = dims
                    logger.info("Streaming broadcast frame dimensions: \(dims.0)x\(dims.1) (via \(transport, privacy: .public))")
                }

                if clientLastSeq != 0 && pktSeq == clientLastSeq {
                    respond(
                        to: urlSchemeTask,
                        url: url,
                        statusCode: 204,
                        headers: diagHeaders
                    )
                } else {
                    var headers200 = diagHeaders
                    headers200["Content-Type"] = "application/octet-stream"
                    respond(
                        to: urlSchemeTask,
                        url: url,
                        statusCode: 200,
                        headers: headers200,
                        body: packet
                    )
                }
            } else {
                respond(
                    to: urlSchemeTask,
                    url: url,
                    statusCode: 204,
                    headers: diagHeaders
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
    private var verifiedIds = Set<ObjectIdentifier>()
    var onConnectionCountChanged: ((Int) -> Void)?

    var verifiedConnectionCount: Int {
        verifiedIds.count
    }

    func startIfNeeded(port: UInt16 = 8765) {
        guard listener == nil, let nwPort = NWEndpoint.Port(rawValue: port) else { return }
        let params = NWParameters.tcp
        params.allowLocalEndpointReuse = true
        params.preferNoProxies = true
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
                    if self.listener === newListener {
                        self.listener = nil
                    }
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
        verifiedIds.removeAll()
    }

    private func accept(_ conn: NWConnection) {
        extConnections.append(conn)
        conn.stateUpdateHandler = { [weak self, weak conn] state in
            guard let self = self, let conn = conn else { return }
            switch state {
            case .failed, .cancelled:
                let oid = ObjectIdentifier(conn)
                let wasVerified = self.verifiedIds.remove(oid) != nil
                self.extConnections.removeAll { $0 === conn }
                if wasVerified {
                    self.onConnectionCountChanged?(self.verifiedIds.count)
                }
                self.logger.info("Safari Keep-Alive extension disconnected (verified=\(self.verifiedIds.count))")
            default:
                break
            }
        }
        conn.start(queue: .main)
        receiveNext(on: conn)
    }

    private func receiveNext(on conn: NWConnection) {
        conn.receiveMessage { [weak self, weak conn] data, _, _, error in
            guard let self = self, let conn = conn, error == nil else { return }
            if let data = data,
               let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let msgType = obj["type"] as? String {
                if msgType == "ext_hello" || msgType == "ext_ping" {
                    let oid = ObjectIdentifier(conn)
                    if self.verifiedIds.insert(oid).inserted {
                        self.logger.info("Verified Safari Keep-Alive extension handshake (verified=\(self.verifiedIds.count))")
                        self.onConnectionCountChanged?(self.verifiedIds.count)
                    }
                } else if msgType == "ext_reload_ack" {
                    let ok = (obj["ok"] as? Bool) ?? false
                    let reason = (obj["reason"] as? String) ?? ""
                    self.logger.info("Safari Keep-Alive reload ack ok=\(ok) reason=\(reason, privacy: .public)")
                }
            }
            self.receiveNext(on: conn)
        }
    }

    @discardableResult
    func broadcastReload() -> Bool {
        let targets = extConnections.filter { verifiedIds.contains(ObjectIdentifier($0)) }
        guard !targets.isEmpty else { return false }
        let payload = #"{"type":"ext_reload"}"#.data(using: .utf8)!
        let meta = NWProtocolWebSocket.Metadata(opcode: .text)
        let ctx = NWConnection.ContentContext(identifier: "wsText", metadata: [meta])
        for conn in targets {
            conn.send(content: payload, contentContext: ctx, isComplete: true, completion: .idempotent)
        }
        return true
    }
}

final class TrackerViewController: UIViewController, WKScriptMessageHandler, WKNavigationDelegate, WKUIDelegate {
    private let logger = Logger(subsystem: "com.tgjgaming.blackjacktracker", category: "TrackerVC")
    private var webView: WKWebView!
    private var broadcastPicker: RPSystemBroadcastPickerView?
    private let loopbackReceiver = LoopbackFrameReceiver()
    private lazy var schemeHandler = CompanionSchemeHandler(loopbackReceiver: loopbackReceiver)
    private let extRelay = KeepaliveExtensionRelay()
    private var discoveredBroadcastExtId: String?

    override var preferredStatusBarStyle: UIStatusBarStyle {
        .lightContent
    }

    deinit {
        webView?.configuration.userContentController.removeScriptMessageHandler(forName: "ipadCompanion")
        extRelay.stop()
        loopbackReceiver.stop()
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = UIColor(red: 15.0 / 255.0, green: 17.0 / 255.0, blue: 21.0 / 255.0, alpha: 1.0)

        discoveredBroadcastExtId = Self.discoverBroadcastExtensionBundleId()
        schemeHandler.broadcastExtensionPresent = (discoveredBroadcastExtId != nil)
        logger.info("BroadcastExtension discovery: \(self.discoveredBroadcastExtId ?? "MISSING (stripped)", privacy: .public)")

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
        setupServices()
        loadTrackerApp()
    }

    func ensureServicesRunning() {
        loopbackReceiver.startIfNeeded(port: FrameIPCConstants.loopbackPort)
        extRelay.startIfNeeded(port: 8765)
    }

    private static func discoverBroadcastExtensionBundleId() -> String? {
        guard let pluginsURL = Bundle.main.builtInPlugInsURL,
              let entries = try? FileManager.default.contentsOfDirectory(
                  at: pluginsURL,
                  includingPropertiesForKeys: nil,
                  options: [.skipsHiddenFiles]
              ) else {
            return nil
        }
        for url in entries where url.pathExtension == "appex" {
            if let extBundle = Bundle(url: url),
               let nsExt = extBundle.infoDictionary?["NSExtension"] as? [String: Any],
               (nsExt["NSExtensionPointIdentifier"] as? String) == "com.apple.broadcast-services-upload",
               let bid = extBundle.bundleIdentifier, !bid.isEmpty {
                return bid
            }
        }
        return nil
    }

    private func setupBroadcastPicker() {
        let picker = RPSystemBroadcastPickerView(frame: .zero)
        picker.translatesAutoresizingMaskIntoConstraints = false
        picker.preferredExtension = discoveredBroadcastExtId ?? ((Bundle.main.bundleIdentifier ?? "com.tgjgaming.blackjacktracker") + ".BroadcastExtension")
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

    private func setupServices() {
        loopbackReceiver.startIfNeeded(port: FrameIPCConstants.loopbackPort)
        extRelay.onConnectionCountChanged = { [weak self] count in
            DispatchQueue.main.async {
                let connected = count > 0 ? "true" : "false"
                self?.webView?.evaluateJavaScript(
                    "window.__ipadCompanionSetExtConnected && window.__ipadCompanionSetExtConnected(\(connected));",
                    completionHandler: nil
                )
            }
        }
        extRelay.startIfNeeded(port: 8765)
    }

    private func pushHostDiagnosticsToWebView() {
        let extConn = extRelay.verifiedConnectionCount > 0 ? "true" : "false"
        let appGroupOk = SharedFrameRing.availableAppGroupId != nil
        let infoPayload: [String: Any] = [
            "appGroupAvailable": appGroupOk,
            "appGroupId": SharedFrameRing.availableAppGroupId ?? "",
            "broadcastExtPresent": discoveredBroadcastExtId != nil,
            "broadcastExtId": discoveredBroadcastExtId ?? "",
            "loopbackPort": Int(FrameIPCConstants.loopbackPort)
        ]
        var hostJson = "{}"
        if let jsonData = try? JSONSerialization.data(withJSONObject: infoPayload),
           let s = String(data: jsonData, encoding: .utf8) {
            hostJson = s
        }
        let js = """
        window.__ipadCompanionSetExtConnected && window.__ipadCompanionSetExtConnected(\(extConn));
        window.__ipadCompanionSetHostInfo && window.__ipadCompanionSetHostInfo(\(hostJson));
        """
        webView?.evaluateJavaScript(js, completionHandler: nil)
    }

    private func findButton(in root: UIView) -> UIButton? {
        if let btn = root as? UIButton {
            return btn
        }
        for sub in root.subviews {
            if let found = findButton(in: sub) {
                return found
            }
        }
        return nil
    }

    private func loadTrackerApp() {
        guard let url = URL(string: "bjtracker://localhost/index.html") else { return }
        logger.info("Loading tracker UI from \(url.absoluteString, privacy: .public)")
        webView.load(URLRequest(url: url))
    }

    // MARK: - WKNavigationDelegate

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        logger.info("Tracker WKWebView finished initial navigation")
        pushHostDiagnosticsToWebView()
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
        case "companionReady":
            ensureServicesRunning()
            pushHostDiagnosticsToWebView()

        case "startBroadcast":
            ensureServicesRunning()
            guard discoveredBroadcastExtId != nil else {
                let alert = UIAlertController(
                    title: "Broadcast Extension Missing",
                    message: "BroadcastExtension.appex was removed during sideloading. In Sideloadly, make sure 'Remove extensions' is UNCHECKED and reinstall.",
                    preferredStyle: .alert
                )
                alert.addAction(UIAlertAction(title: "OK", style: .default))
                presentAlertSafely(alert, onSkipped: {})
                return
            }
            guard let picker = broadcastPicker else { return }
            logger.info("Triggering iPadOS ReplayKit broadcast picker")
            if let btn = findButton(in: picker) {
                btn.sendActions(for: .touchUpInside)
            } else {
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

