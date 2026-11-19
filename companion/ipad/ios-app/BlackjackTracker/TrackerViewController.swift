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
    private var listenerIsWaiting = false
    private var activeSession: Session?
    private var latestPacket: Data?
    private var latestFrameMeta: (rawW: Int, rawH: Int, rawOrientation: UInt32, dstW: Int, dstH: Int)?
    private var broadcastActive = false
    private var hasConnectedOnce = false
    private var loggedFrameDims: (Int, Int) = (0, 0)

    func startIfNeeded(port: UInt16 = FrameIPCConstants.loopbackPort) {
        queue.async { [weak self] in
            self?._startOnQueue(port: port)
        }
    }

    private func _startOnQueue(port: UInt16) {
        if let existing = listener {
            if listenerIsWaiting {
                logger.notice("Loopback frame receiver stuck in .waiting; restarting listener")
                existing.cancel()
                listener = nil
                listenerIsWaiting = false
            } else {
                return
            }
        }
        guard let nwPort = NWEndpoint.Port(rawValue: port) else { return }
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
                    if self.listener === newListener {
                        self.listenerIsWaiting = false
                    }
                    self.logger.info("Loopback frame receiver listening on 127.0.0.1:\(port)")
                case .waiting(let err):
                    if self.listener === newListener {
                        self.listenerIsWaiting = true
                    }
                    self.logger.notice("Loopback frame receiver waiting: \(String(describing: err), privacy: .public)")
                case .failed(let err):
                    self.logger.error("Loopback frame receiver failed: \(String(describing: err), privacy: .public)")
                    newListener.cancel()
                    if self.listener === newListener {
                        self.listener = nil
                        self.listenerIsWaiting = false
                        self.queue.asyncAfter(deadline: .now() + 1.0) { [weak self] in
                            self?._startOnQueue(port: port)
                        }
                    }
                case .cancelled:
                    if self.listener === newListener {
                        self.listener = nil
                        self.listenerIsWaiting = false
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
        var readOffset = 0
        var newestFrameRange: Range<Int>?
        var newestMeta: (rawW: Int, rawH: Int, rawOrientation: UInt32, dstW: Int, dstH: Int)?
        var sawStarted = false
        var sawFinished = false

        while (session.rxBuffer.count - readOffset) >= FrameIPCConstants.tcpHeaderBytes {
            let magic = FrameIPCConstants.readUInt32LE(from: session.rxBuffer, offset: readOffset)
            let msgType = FrameIPCConstants.readUInt32LE(from: session.rxBuffer, offset: readOffset + 4)
            let payloadLen = Int(FrameIPCConstants.readUInt32LE(from: session.rxBuffer, offset: readOffset + 8))

            if magic != FrameIPCConstants.recordMagic || payloadLen < 0 || (FrameIPCConstants.tcpHeaderBytes + payloadLen) > maxRecordBytes {
                logger.error("Corrupt loopback record header (magic=\(magic), len=\(payloadLen)); resetting session")
                closeSession(session, markFinished: true)
                return
            }

            let totalLen = FrameIPCConstants.tcpHeaderBytes + payloadLen
            if (session.rxBuffer.count - readOffset) < totalLen {
                break
            }

            let payloadRelOffset = readOffset + FrameIPCConstants.tcpHeaderBytes
            let payloadStart = session.rxBuffer.startIndex + payloadRelOffset
            let payloadEnd = payloadStart + payloadLen
            if msgType == FrameIPCConstants.msgFrame {
                if payloadLen >= SharedFrameRing.headerBytes {
                    newestFrameRange = payloadStart..<payloadEnd
                    sawStarted = true
                    sawFinished = false
                }
            } else if msgType == FrameIPCConstants.msgMeta {
                if let meta = FrameIPCConstants.readMetaPayload(from: session.rxBuffer, offset: payloadRelOffset) {
                    newestMeta = meta
                }
            } else if msgType == FrameIPCConstants.msgBroadcastStarted {
                sawStarted = true
                sawFinished = false
            } else if msgType == FrameIPCConstants.msgBroadcastFinished {
                sawFinished = true
                newestFrameRange = nil
                newestMeta = nil
            }

            readOffset += totalLen
        }

        let newestFrame = newestFrameRange.map { session.rxBuffer.subdata(in: $0) }
        if readOffset >= session.rxBuffer.count {
            session.rxBuffer.removeAll(keepingCapacity: true)
        } else if readOffset > 0 {
            session.rxBuffer.removeSubrange(session.rxBuffer.startIndex..<(session.rxBuffer.startIndex + readOffset))
        }

        if newestFrame != nil || newestMeta != nil || sawStarted || sawFinished {
            stateLock.lock()
            if let meta = newestMeta {
                latestFrameMeta = meta
            }
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
                latestFrameMeta = nil
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
            latestFrameMeta = nil
            stateLock.unlock()
            logger.info("BroadcastExtension loopback session closed")
        }
    }

    func latestState() -> (
        packet: Data?,
        meta: (rawW: Int, rawH: Int, rawOrientation: UInt32, dstW: Int, dstH: Int)?,
        broadcastActive: Bool,
        hasConnectedOnce: Bool
    ) {
        stateLock.lock()
        let result = (latestPacket, latestFrameMeta, broadcastActive, hasConnectedOnce)
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
    private let frameReadQueue = DispatchQueue(label: "com.tgjgaming.blackjacktracker.schemeFrameRead", qos: .userInitiated)
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

        guard let url = urlSchemeTask.request.url else {
            lock.lock()
            let stopped = stoppedTaskIds.remove(taskId) != nil
            lock.unlock()
            if !stopped {
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

            let broadcastExtPresent = self.broadcastExtensionPresent
            let appGroupAvailable = (self.appGroupId != nil)

            frameReadQueue.async { [weak self] in
                guard let self = self else { return }
                if self.isTaskStopped(urlSchemeTask) {
                    self.lock.lock()
                    self.stoppedTaskIds.remove(taskId)
                    self.lock.unlock()
                    return
                }

                let loopbackState = self.loopbackReceiver.latestState()
                var selectedPacket = loopbackState.packet
                var transport = (selectedPacket != nil) ? "loopback" : (loopbackState.broadcastActive ? "loopback_waiting" : "idle")
                var activeFlag = loopbackState.broadcastActive
                var ringSeqUnchanged = false

                if selectedPacket == nil, appGroupAvailable {
                    if self.ring == nil {
                        self.ring = SharedFrameRing(isWriter: false)
                        if let ring = self.ring, !self.loggedRingReady {
                            self.loggedRingReady = true
                            self.logger.info("Connected SharedFrameRing reader: \(ring.storageDescription, privacy: .public)")
                        }
                    }
                    if let ring = self.ring {
                        let res = ring.readLatestPacket(sinceSeq: clientLastSeq)
                        if res.isActive {
                            transport = "appgroup"
                            activeFlag = true
                            if let data = res.data {
                                selectedPacket = data
                            } else if clientLastSeq != 0 && res.seq == clientLastSeq {
                                ringSeqUnchanged = true
                            }
                        }
                    }
                }

                var frameInfoStr = ""
                if let meta = loopbackState.meta {
                    frameInfoStr = "\(meta.rawW)x\(meta.rawH),ori=\(meta.rawOrientation)->\(meta.dstW)x\(meta.dstH)"
                } else if let packet = selectedPacket, packet.count >= SharedFrameRing.headerBytes {
                    let pw = Int(FrameIPCConstants.readUInt32LE(from: packet, offset: 4))
                    let ph = Int(FrameIPCConstants.readUInt32LE(from: packet, offset: 8))
                    frameInfoStr = "\(pw)x\(ph)"
                }

                let diagHeaders: [String: String] = [
                    "Cache-Control": "no-store",
                    "Access-Control-Allow-Origin": "*",
                    "X-BJ-Transport": transport,
                    "X-BJ-Broadcast-Active": activeFlag ? "1" : "0",
                    "X-BJ-AppGroup-Ok": appGroupAvailable ? "1" : "0",
                    "X-BJ-BroadcastExt-Present": broadcastExtPresent ? "1" : "0",
                    "X-BJ-Frame-Info": frameInfoStr
                ]

                let responseStatus: Int
                let responseHeaders: [String: String]
                let responseBody: Data?

                if ringSeqUnchanged {
                    responseStatus = 204
                    responseHeaders = diagHeaders
                    responseBody = nil
                } else if let packet = selectedPacket, packet.count >= SharedFrameRing.headerBytes {
                    let pktSeq = FrameIPCConstants.readUInt32LE(from: packet, offset: 0)
                    let w = Int(FrameIPCConstants.readUInt32LE(from: packet, offset: 4))
                    let h = Int(FrameIPCConstants.readUInt32LE(from: packet, offset: 8))
                    let dims = (w, h)
                    if dims != self.loggedFrameDims {
                        self.loggedFrameDims = dims
                        self.logger.info("Streaming broadcast frame dimensions: \(dims.0)x\(dims.1) (via \(transport, privacy: .public))")
                    }

                    if clientLastSeq != 0 && pktSeq == clientLastSeq {
                        responseStatus = 204
                        responseHeaders = diagHeaders
                        responseBody = nil
                    } else {
                        var headers200 = diagHeaders
                        headers200["Content-Type"] = "application/octet-stream"
                        responseStatus = 200
                        responseHeaders = headers200
                        responseBody = packet
                    }
                } else {
                    responseStatus = 204
                    responseHeaders = diagHeaders
                    responseBody = nil
                }

                DispatchQueue.main.async { [weak self] in
                    guard let self = self else { return }
                    defer {
                        self.lock.lock()
                        self.stoppedTaskIds.remove(taskId)
                        self.lock.unlock()
                    }
                    self.respond(
                        to: urlSchemeTask,
                        url: url,
                        statusCode: responseStatus,
                        headers: responseHeaders,
                        body: responseBody
                    )
                }
            }
            return
        }

        defer {
            lock.lock()
            stoppedTaskIds.remove(taskId)
            lock.unlock()
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
            case "/companion/ipad/companion_worker.js":
                asset = ("companion_worker", "js", "application/javascript; charset=utf-8")
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
                    "Cache-Control": "no-store",
                    "Access-Control-Allow-Origin": "*"
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
    private struct PendingReload {
        let keywords: [String]?
        let allowActiveFallback: Bool
        let testImmediate: Bool
        let expiresAt: Date
    }

    private let logger = Logger(subsystem: "com.tgjgaming.blackjacktracker", category: "KeepaliveRelay")
    private var listener: NWListener?
    private var extConnections: [NWConnection] = []
    private var verifiedIds = Set<ObjectIdentifier>()
    private var isStopped = false
    private var port: UInt16 = 8765

    private var pendingReload: PendingReload?
    private var permissionOk: Bool?
    private var tabCount: Int = 0
    private var matchedCount: Int = 0
    private var lastHelloTs: Double = 0
    private var lastAck: [String: Any]?

    var onConnectionCountChanged: ((Int) -> Void)?
    var onStatusChanged: (([String: Any]) -> Void)?

    var verifiedConnectionCount: Int {
        verifiedIds.count
    }

    private func hasActivePendingReload() -> Bool {
        guard let pending = pendingReload else { return false }
        if pending.expiresAt <= Date() {
            pendingReload = nil
            return false
        }
        return true
    }

    func statusDictionary() -> [String: Any] {
        var dict: [String: Any] = [
            "connected": !verifiedIds.isEmpty,
            "verifiedCount": verifiedIds.count,
            "tabCount": tabCount,
            "matchedCount": matchedCount,
            "pendingQueued": hasActivePendingReload(),
            "lastHelloTs": lastHelloTs
        ]
        if let permissionOk = permissionOk {
            dict["permissionOk"] = permissionOk
        }
        if let lastAck = lastAck {
            dict["lastAck"] = lastAck
        }
        return dict
    }

    private func emitStatus() {
        onConnectionCountChanged?(verifiedIds.count)
        onStatusChanged?(statusDictionary())
    }

    private var listenerIsWaiting = false

    func startIfNeeded(port: UInt16 = 8765) {
        self.port = port
        self.isStopped = false
        if let existing = listener {
            if listenerIsWaiting {
                logger.notice("KeepaliveRelay stuck in .waiting; restarting listener")
                existing.cancel()
                listener = nil
                listenerIsWaiting = false
            } else {
                return
            }
        }
        guard let nwPort = NWEndpoint.Port(rawValue: port) else { return }
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
                    if self.listener === newListener {
                        self.listenerIsWaiting = false
                    }
                    self.logger.info("Loopback WebSocket relay ready on 127.0.0.1:\(port)")
                case .waiting(let error):
                    if self.listener === newListener {
                        self.listenerIsWaiting = true
                    }
                    self.logger.notice("KeepaliveRelay waiting: \(String(describing: error), privacy: .public)")
                case .failed(let error):
                    self.logger.error("KeepaliveRelay failed: \(String(describing: error), privacy: .public)")
                    newListener.cancel()
                    if self.listener === newListener {
                        self.listener = nil
                        self.listenerIsWaiting = false
                        if !self.isStopped {
                            DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
                                guard let self = self, !self.isStopped else { return }
                                self.startIfNeeded(port: self.port)
                            }
                        }
                    }
                case .cancelled:
                    self.logger.info("KeepaliveRelay cancelled")
                    if self.listener === newListener {
                        self.listener = nil
                        self.listenerIsWaiting = false
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
        isStopped = true
        listener?.cancel()
        listener = nil
        for conn in extConnections {
            conn.cancel()
        }
        extConnections.removeAll()
        verifiedIds.removeAll()
        pendingReload = nil
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
                    self.emitStatus()
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
            guard let self = self, let conn = conn else { return }
            if error != nil {
                conn.cancel()
                return
            }
            if let data = data,
               let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let msgType = obj["type"] as? String {
                if msgType == "ext_hello" || msgType == "ext_ping" {
                    let oid = ObjectIdentifier(conn)
                    let newlyVerified = self.verifiedIds.insert(oid).inserted
                    if let perm = obj["permissionOk"] as? Bool {
                        self.permissionOk = perm
                    }
                    if let tc = obj["tabCount"] as? Int {
                        self.tabCount = tc
                    }
                    if let mc = obj["matchedCount"] as? Int {
                        self.matchedCount = mc
                    }
                    self.lastHelloTs = Date().timeIntervalSince1970
                    if newlyVerified {
                        self.logger.info("Verified Safari Keep-Alive extension handshake (verified=\(self.verifiedIds.count), perm=\(String(describing: self.permissionOk)), tabs=\(self.tabCount))")
                    }
                    if self.hasActivePendingReload(), let queued = self.pendingReload {
                        self.pendingReload = nil
                        self.sendReload(queued, to: [conn])
                        self.logger.info("Flushed queued Safari keep-alive reload upon extension wakeup")
                    }
                    self.emitStatus()
                } else if msgType == "ext_reload_ack" {
                    var ack = obj
                    ack["ts"] = Date().timeIntervalSince1970
                    self.lastAck = ack
                    if let perm = obj["permissionOk"] as? Bool {
                        self.permissionOk = perm
                    }
                    if let tc = obj["tabCount"] as? Int {
                        self.tabCount = tc
                    }
                    if let mc = obj["matchedCount"] as? Int {
                        self.matchedCount = mc
                    }
                    let ok = (obj["ok"] as? Bool) ?? false
                    let reason = (obj["reason"] as? String) ?? (obj["matchedBy"] as? String) ?? ""
                    self.logger.info("Safari Keep-Alive reload ack ok=\(ok) detail=\(reason, privacy: .public)")
                    self.emitStatus()
                }
            }
            self.receiveNext(on: conn)
        }
    }

    private func sendReload(_ cmd: PendingReload, to targets: [NWConnection]) {
        var payloadObj: [String: Any] = [
            "type": "ext_reload",
            "allowActiveFallback": cmd.allowActiveFallback,
            "testImmediate": cmd.testImmediate
        ]
        if let kws = cmd.keywords, !kws.isEmpty {
            payloadObj["keywords"] = kws
        }
        guard let payload = try? JSONSerialization.data(withJSONObject: payloadObj) else { return }
        let meta = NWProtocolWebSocket.Metadata(opcode: .text)
        let ctx = NWConnection.ContentContext(identifier: "wsText", metadata: [meta])
        for conn in targets {
            conn.send(content: payload, contentContext: ctx, isComplete: true, completion: .idempotent)
        }
    }

    func requestReload(keywords: [String]? = nil, allowActiveFallback: Bool = false, testImmediate: Bool = false) -> String {
        let targets = extConnections.filter { verifiedIds.contains(ObjectIdentifier($0)) }
        let cmd = PendingReload(
            keywords: keywords,
            allowActiveFallback: allowActiveFallback,
            testImmediate: testImmediate,
            expiresAt: Date().addingTimeInterval(75.0)
        )
        if !targets.isEmpty {
            pendingReload = nil
            sendReload(cmd, to: targets)
            emitStatus()
            return "delivered"
        }
        pendingReload = cmd
        emitStatus()
        return "queued"
    }

    func requestTabDiag(keywords: [String]? = nil) {
        let targets = extConnections.filter { verifiedIds.contains(ObjectIdentifier($0)) }
        guard !targets.isEmpty else { return }
        var obj: [String: Any] = ["type": "ext_status_req"]
        if let kws = keywords, !kws.isEmpty {
            obj["keywords"] = kws
        }
        guard let payload = try? JSONSerialization.data(withJSONObject: obj) else { return }
        let meta = NWProtocolWebSocket.Metadata(opcode: .text)
        let ctx = NWConnection.ContentContext(identifier: "wsText", metadata: [meta])
        for conn in targets {
            conn.send(content: payload, contentContext: ctx, isComplete: true, completion: .idempotent)
        }
    }

    @discardableResult
    func broadcastReload() -> Bool {
        return requestReload(keywords: nil, allowActiveFallback: false, testImmediate: false) == "delivered"
    }
}

private final class WeakScriptMessageHandler: NSObject, WKScriptMessageHandler {
    weak var delegate: WKScriptMessageHandler?
    init(_ delegate: WKScriptMessageHandler) {
        self.delegate = delegate
    }
    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        delegate?.userContentController(userContentController, didReceive: message)
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
    private var discoveredSafariExtId: String?

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

        discoveredBroadcastExtId = Self.discoverExtensionBundleId(pointIdentifier: "com.apple.broadcast-services-upload")
        discoveredSafariExtId = Self.discoverExtensionBundleId(pointIdentifier: "com.apple.Safari.web-extension")
        schemeHandler.broadcastExtensionPresent = (discoveredBroadcastExtId != nil)
        logger.info("Extension discovery: broadcast=\(self.discoveredBroadcastExtId ?? "MISSING", privacy: .public), safariKeepalive=\(self.discoveredSafariExtId ?? "MISSING", privacy: .public)")

        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        config.setURLSchemeHandler(schemeHandler, forURLScheme: "bjtracker")
        config.setURLSchemeHandler(schemeHandler, forURLScheme: "companion-frame")
        config.userContentController.add(WeakScriptMessageHandler(self), name: "ipadCompanion")
        config.allowsInlineMediaPlayback = true

        webView = WKWebView(frame: .zero, configuration: config)
        webView.translatesAutoresizingMaskIntoConstraints = false
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.isOpaque = false
        webView.backgroundColor = view.backgroundColor
        webView.scrollView.backgroundColor = view.backgroundColor
        #if DEBUG
        if #available(iOS 16.4, *) {
            webView.isInspectable = true
        }
        #endif
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

    private static func discoverExtensionBundleId(pointIdentifier: String) -> String? {
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
               (nsExt["NSExtensionPointIdentifier"] as? String) == pointIdentifier,
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
        extRelay.onStatusChanged = { [weak self] st in
            DispatchQueue.main.async {
                self?.pushExtStatusToWebView(st)
            }
        }
        extRelay.startIfNeeded(port: 8765)
    }

    private func pushExtStatusToWebView(_ status: [String: Any]? = nil) {
        let payload = status ?? extRelay.statusDictionary()
        let extConn = ((payload["connected"] as? Bool) ?? (extRelay.verifiedConnectionCount > 0)) ? "true" : "false"
        var statusJson = "{}"
        if JSONSerialization.isValidJSONObject(payload),
           let jsonData = try? JSONSerialization.data(withJSONObject: payload),
           let s = String(data: jsonData, encoding: .utf8) {
            statusJson = s
        }
        let js = """
        if (window.__ipadCompanionSetExtStatus) {
          window.__ipadCompanionSetExtStatus(\(statusJson));
        } else if (window.__ipadCompanionSetExtConnected) {
          window.__ipadCompanionSetExtConnected(\(extConn));
        }
        """
        webView?.evaluateJavaScript(js, completionHandler: nil)
    }

    private func pushHostDiagnosticsToWebView() {
        let appGroupOk = SharedFrameRing.availableAppGroupId != nil
        let infoPayload: [String: Any] = [
            "appGroupAvailable": appGroupOk,
            "appGroupId": SharedFrameRing.availableAppGroupId ?? "",
            "broadcastExtPresent": discoveredBroadcastExtId != nil,
            "broadcastExtId": discoveredBroadcastExtId ?? "",
            "safariExtPresent": discoveredSafariExtId != nil,
            "safariExtId": discoveredSafariExtId ?? "",
            "loopbackPort": Int(FrameIPCConstants.loopbackPort),
            "nativeRelayAvailable": true
        ]
        var hostJson = "{}"
        if let jsonData = try? JSONSerialization.data(withJSONObject: infoPayload),
           let s = String(data: jsonData, encoding: .utf8) {
            hostJson = s
        }
        let js = "window.__ipadCompanionSetHostInfo && window.__ipadCompanionSetHostInfo(\(hostJson));"
        webView?.evaluateJavaScript(js, completionHandler: nil)
        pushExtStatusToWebView(nil)
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

        if scheme == "file" || scheme == "companion-frame" {
            decisionHandler(.allow)
            return
        }

        logger.notice("Blocked unexpected WebView navigation to scheme '\(scheme, privacy: .public)'")
        decisionHandler(.cancel)
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        logger.error("WKWebView WebContent process terminated (Jetsam/OOM); reloading tracker UI")
        ensureServicesRunning()
        webView.reload()
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
            let keywords = body["keywords"] as? [String]
            let allowActiveFallback = (body["allowActiveFallback"] as? Bool) ?? false
            let testImmediate = (body["testImmediate"] as? Bool) ?? false
            let delivery = extRelay.requestReload(
                keywords: keywords,
                allowActiveFallback: allowActiveFallback,
                testImmediate: testImmediate
            )
            logger.info("Relayed Safari keep-alive reload command (delivery=\(delivery, privacy: .public), verified=\(self.extRelay.verifiedConnectionCount))")
            pushExtStatusToWebView(nil)

        case "requestExtStatus":
            let keywords = body["keywords"] as? [String]
            extRelay.requestTabDiag(keywords: keywords)
            pushHostDiagnosticsToWebView()

        case "exportFile":
            guard let rawName = body["filename"] as? String,
                  let text = body["text"] as? String else {
                return
            }
            let payloadBytes = Data(text.utf8)
            let maxExportBytes = 25 * 1024 * 1024
            if payloadBytes.count > maxExportBytes {
                logger.error("Rejected oversized export (\(payloadBytes.count) bytes > \(maxExportBytes))")
                return
            }
            let safeName = (rawName as NSString).lastPathComponent
            let cleanName = safeName.isEmpty ? "tracker_export.txt" : safeName
            let outURL = FileManager.default.temporaryDirectory.appendingPathComponent(cleanName)
            do {
                try payloadBytes.write(to: outURL, options: .atomic)
                let activityVC = UIActivityViewController(activityItems: [outURL], applicationActivities: nil)
                activityVC.completionWithItemsHandler = { _, _, _, _ in
                    try? FileManager.default.removeItem(at: outURL)
                }
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
                    logger.info("Presented iOS Share Sheet for export '\(cleanName, privacy: .public)' (\(payloadBytes.count) bytes)")
                } else {
                    try? FileManager.default.removeItem(at: outURL)
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

