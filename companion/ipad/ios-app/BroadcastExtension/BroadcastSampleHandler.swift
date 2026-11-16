import ReplayKit
import CoreImage
import CoreVideo
import ImageIO
import QuartzCore
import Network
import os

/// App-Group-free loopback frame streamer (`127.0.0.1:8766`) used automatically when
/// Sideloadly / a free Apple ID strips `com.apple.security.application-groups`.
/// Enforces strict 1-frame-in-flight backpressure so `BroadcastExtension` stays well under
/// iPadOS's 50 MB Jetsam memory cap.
private final class LoopbackFrameSender {
    private let logger = Logger(subsystem: "com.tgjgaming.blackjacktracker.BroadcastExtension", category: "LoopbackTx")
    private let queue = DispatchQueue(label: "com.tgjgaming.blackjacktracker.loopbackFrameTx", qos: .userInitiated)
    private let lock = NSLock()

    private var connection: NWConnection?
    private var ready = false
    private var sending = false
    private var sendStartNs: UInt64 = 0
    private var lastAttemptNs: UInt64 = 0
    private var sentStartedControl = false
    private var loggedConnected = false

    func prepareForFrameAndCheckCanRender() -> Bool {
        let nowNs = DispatchTime.now().uptimeNanoseconds
        lock.lock()
        let isReady = ready
        let isSending = sending
        let elapsedSinceSend = nowNs >= sendStartNs ? (nowNs - sendStartNs) : 0
        let shouldConnect = (connection == nil) && (lastAttemptNs == 0 || (nowNs - lastAttemptNs) > 350_000_000)
        if shouldConnect {
            lastAttemptNs = nowNs
        }
        lock.unlock()

        if shouldConnect {
            connectIfNeeded()
        }
        if !isReady {
            return false
        }
        if isSending {
            if elapsedSinceSend > 2_000_000_000 {
                logger.notice("Loopback send stalled >2s; resetting connection")
                resetConnection()
            }
            return false
        }
        return true
    }

    func connectIfNeeded() {
        queue.async { [weak self] in
            self?._connectOnQueue()
        }
    }

    private func _connectOnQueue() {
        lock.lock()
        let existing = connection
        if existing != nil {
            lock.unlock()
            return
        }
        lastAttemptNs = DispatchTime.now().uptimeNanoseconds
        lock.unlock()

        guard let port = NWEndpoint.Port(rawValue: FrameIPCConstants.loopbackPort) else { return }
        let tcpOpts = NWProtocolTCP.Options()
        tcpOpts.noDelay = true
        tcpOpts.connectionTimeout = 2
        let params = NWParameters(tls: nil, tcp: tcpOpts)
        params.preferNoProxies = true

        let endpoint = NWEndpoint.hostPort(host: .ipv4(.loopback), port: port)
        let conn = NWConnection(to: endpoint, using: params)

        lock.lock()
        connection = conn
        ready = false
        sending = false
        sentStartedControl = false
        lock.unlock()

        conn.stateUpdateHandler = { [weak self, weak conn] state in
            guard let self = self, let conn = conn else { return }
            self.lock.lock()
            let isCurrent = (self.connection === conn)
            if !isCurrent {
                self.lock.unlock()
                return
            }
            switch state {
            case .ready:
                self.ready = true
                let needHello = !self.sentStartedControl
                self.sentStartedControl = true
                let firstLog = !self.loggedConnected
                self.loggedConnected = true
                self.lock.unlock()
                if firstLog {
                    self.logger.info("Connected loopback frame sender to 127.0.0.1:\(FrameIPCConstants.loopbackPort)")
                }
                if needHello {
                    let ctrl = FrameIPCConstants.makeControlRecord(msgType: FrameIPCConstants.msgBroadcastStarted)
                    conn.send(content: ctrl, completion: .idempotent)
                }
            case .failed(let err):
                self.ready = false
                self.sending = false
                self.connection = nil
                self.lock.unlock()
                self.logger.error("Loopback frame connection failed: \(String(describing: err), privacy: .public)")
                conn.cancel()
            case .waiting(let err):
                self.ready = false
                self.sending = false
                self.connection = nil
                self.lock.unlock()
                self.logger.notice("Loopback frame receiver not ready yet: \(String(describing: err), privacy: .public)")
                conn.cancel()
            case .cancelled:
                self.ready = false
                self.sending = false
                self.connection = nil
                self.lock.unlock()
            default:
                self.lock.unlock()
            }
        }
        conn.start(queue: queue)
    }

    func sendAuxiliaryRecord(_ wireData: Data) {
        lock.lock()
        guard ready, let conn = connection else {
            lock.unlock()
            return
        }
        lock.unlock()

        queue.async { [weak self, weak conn] in
            guard let self = self, let conn = conn else { return }
            conn.send(content: wireData, completion: .contentProcessed { [weak self, weak conn] err in
                guard let self = self, let conn = conn else { return }
                if let err = err {
                    self.lock.lock()
                    let isCurrent = (self.connection === conn)
                    if isCurrent {
                        self.ready = false
                        self.sending = false
                        self.connection = nil
                    }
                    self.lock.unlock()
                    if isCurrent {
                        self.logger.error("Loopback meta send error: \(String(describing: err), privacy: .public)")
                        conn.cancel()
                    }
                }
            })
        }
    }

    func sendFrameRecord(_ wireData: Data) {
        lock.lock()
        guard ready, !sending, let conn = connection else {
            lock.unlock()
            return
        }
        sending = true
        sendStartNs = DispatchTime.now().uptimeNanoseconds
        lock.unlock()

        queue.async { [weak self, weak conn] in
            guard let self = self, let conn = conn else { return }
            conn.send(content: wireData, completion: .contentProcessed { [weak self, weak conn] err in
                guard let self = self, let conn = conn else { return }
                self.lock.lock()
                let isCurrent = (self.connection === conn)
                if isCurrent {
                    self.sending = false
                    if err != nil {
                        self.ready = false
                        self.connection = nil
                    }
                }
                self.lock.unlock()
                if let err = err, isCurrent {
                    self.logger.error("Loopback send error: \(String(describing: err), privacy: .public)")
                    conn.cancel()
                }
            })
        }
    }

    func sendControlIfReady(_ msgType: UInt32, waitSeconds: TimeInterval = 0) {
        lock.lock()
        guard ready, let conn = connection else {
            lock.unlock()
            return
        }
        lock.unlock()

        let ctrl = FrameIPCConstants.makeControlRecord(msgType: msgType)
        let sem = waitSeconds > 0 ? DispatchSemaphore(value: 0) : nil
        queue.async {
            conn.send(content: ctrl, completion: .contentProcessed { _ in
                sem?.signal()
            })
        }
        if let sem = sem {
            _ = sem.wait(timeout: .now() + waitSeconds)
        }
    }

    func resetConnection() {
        lock.lock()
        let old = connection
        connection = nil
        ready = false
        sending = false
        sentStartedControl = false
        lock.unlock()
        if let old = old {
            queue.async {
                old.cancel()
            }
        }
    }
}

/// Lightweight ReplayKit Broadcast Upload Extension (`RPBroadcastSampleHandler`).
/// Strictly engineered to stay well below iPadOS's 50 MB extension limit:
/// - Throttles capture to 12 FPS and skips duplicate static frames between 0.45s heartbeats
/// - Remaps `RPVideoSampleOrientationKey` to EXIF `CIImage.oriented(_:)` (swapping `.left` <-> `.right`) and normalizes extent origin
/// - Downscales incoming `CMSampleBuffer` to <= 1280x960 using hardware CIContext
/// - Writes RGBA bytes directly into `SharedFrameRing` (when App Group exists) OR
///   streams directly over loopback TCP (`127.0.0.1:8766`) when App Group is stripped
final class BroadcastSampleHandler: RPBroadcastSampleHandler {
    private let logger = Logger(subsystem: "com.tgjgaming.blackjacktracker.BroadcastExtension", category: "Broadcast")
    private let ciContext = CIContext(options: [
        .useSoftwareRenderer: false,
        .cacheIntermediates: false
    ])
    private let srgbColorSpace = CGColorSpace(name: CGColorSpace.sRGB) ?? CGColorSpaceCreateDeviceRGB()

    private var ring: SharedFrameRing?
    private var attemptedRingInit = false
    private let loopbackSender = LoopbackFrameSender()
    private var seq: UInt32 = 0
    private var frameCount: UInt64 = 0
    private var lastFrameTs: CFTimeInterval = 0
    private let minFrameInterval: CFTimeInterval = 1.0 / 12.0
    private let duplicateHeartbeatInterval: CFTimeInterval = 0.45
    private var hasSentFrame = false
    private var lastSentFrameHash: UInt64 = 0
    private var lastSentDims: (Int, Int) = (0, 0)
    private var lastSentFrameTs: CFTimeInterval = 0
    private var lastMeta: (rawW: Int, rawH: Int, rawOri: UInt32, dstW: Int, dstH: Int)?
    private var loggedFirstFrame = false
    private var frameBuffer = [UInt8](repeating: 0, count: FrameIPCConstants.tcpHeaderBytes + SharedFrameRing.capacityBytes)

    /// ReplayKit's `RPVideoSampleOrientationKey` indicates the clockwise correction needed
    /// (`.left` = 8 for +90° CW, `.right` = 6 for 270° CW), which is the opposite of EXIF /
    /// `CIImage.oriented(_:)` where `.right` (6) rotates +90° CW and `.left` (8) rotates 90° CCW.
    private static func ciOrientation(forReplayKitOrientation rpOrientation: CGImagePropertyOrientation) -> CGImagePropertyOrientation {
        switch rpOrientation {
        case .left:
            return .right
        case .right:
            return .left
        case .leftMirrored:
            return .rightMirrored
        case .rightMirrored:
            return .leftMirrored
        default:
            return rpOrientation
        }
    }

    private static func computeStridedPixelHash(_ raw: UnsafeRawPointer, byteCount: Int) -> UInt64 {
        let pixelCount = byteCount / 4
        guard pixelCount > 0 else { return 0 }
        let stride = max(1, pixelCount / 8192)
        var h: UInt64 = 14695981039346656037
        var i = 0
        while i < pixelCount {
            let word = raw.loadUnaligned(fromByteOffset: i * 4, as: UInt32.self)
            h ^= UInt64(word)
            h &*= 1099511628211
            i += stride
        }
        return h
    }

    override func broadcastStarted(withSetupInfo setupInfo: [String: NSObject]?) {
        logger.info("Broadcast started")
        attemptedRingInit = true
        ring = SharedFrameRing(isWriter: true)
        if let ring = ring {
            logger.info("Broadcast SharedFrameRing writer ready: \(ring.storageDescription, privacy: .public)")
        } else {
            logger.info("App Group unavailable; starting LoopbackFrameSender (127.0.0.1:\(FrameIPCConstants.loopbackPort))")
            loopbackSender.connectIfNeeded()
        }
        let seed = UInt32(truncatingIfNeeded: DispatchTime.now().uptimeNanoseconds)
        seq = (seed == 0) ? 1 : seed
        frameCount = 0
        lastFrameTs = 0
        hasSentFrame = false
        lastSentFrameHash = 0
        lastSentDims = (0, 0)
        lastSentFrameTs = 0
        lastMeta = nil
        loggedFirstFrame = false
    }

    override func broadcastPaused() {
        logger.info("Broadcast paused")
    }

    override func broadcastResumed() {
        logger.info("Broadcast resumed")
        if ring == nil {
            loopbackSender.sendControlIfReady(FrameIPCConstants.msgBroadcastStarted)
        }
    }

    override func broadcastFinished() {
        logger.info("Broadcast finished after \(self.frameCount) rendered frame(s)")
        if ring == nil {
            loopbackSender.sendControlIfReady(FrameIPCConstants.msgBroadcastFinished, waitSeconds: 0.15)
            loopbackSender.resetConnection()
        } else {
            ring?.clear()
        }
        ring = nil
        attemptedRingInit = false
        hasSentFrame = false
        lastSentFrameHash = 0
        lastSentDims = (0, 0)
        lastSentFrameTs = 0
        lastMeta = nil
    }

    override func processSampleBuffer(_ sampleBuffer: CMSampleBuffer, with sampleBufferType: RPSampleBufferType) {
        guard sampleBufferType == .video else { return }
        let now = CACurrentMediaTime()
        if now - lastFrameTs < minFrameInterval { return }

        if !attemptedRingInit {
            attemptedRingInit = true
            ring = SharedFrameRing(isWriter: true)
        }
        if ring == nil && !loopbackSender.prepareForFrameAndCheckCanRender() {
            return
        }
        lastFrameTs = now

        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        let rawW = CVPixelBufferGetWidth(pixelBuffer)
        let rawH = CVPixelBufferGetHeight(pixelBuffer)

        autoreleasepool {
            var ciImage = CIImage(cvPixelBuffer: pixelBuffer)

            var rpOrientation: CGImagePropertyOrientation = .up
            if let orientationAttachment = CMGetAttachment(
                sampleBuffer,
                key: RPVideoSampleOrientationKey as CFString,
                attachmentModeOut: nil
            ) as? NSNumber,
               let parsedOrientation = CGImagePropertyOrientation(rawValue: orientationAttachment.uint32Value) {
                rpOrientation = parsedOrientation
            }

            let ciOrientation = Self.ciOrientation(forReplayKitOrientation: rpOrientation)
            if ciOrientation != .up {
                ciImage = ciImage.oriented(ciOrientation)
            }

            let orientedExtent = ciImage.extent
            guard !orientedExtent.isEmpty, orientedExtent.width > 0, orientedExtent.height > 0 else { return }
            if orientedExtent.origin.x != 0 || orientedExtent.origin.y != 0 {
                ciImage = ciImage.transformed(
                    by: CGAffineTransform(translationX: -orientedExtent.origin.x, y: -orientedExtent.origin.y)
                )
            }

            let srcW = Double(orientedExtent.width)
            let srcH = Double(orientedExtent.height)
            let scale = min(
                1.0,
                Double(SharedFrameRing.maxWidth) / srcW,
                Double(SharedFrameRing.maxHeight) / srcH
            )
            let dstW = max(16, min(SharedFrameRing.maxWidth, Int((srcW * scale).rounded())))
            let dstH = max(16, min(SharedFrameRing.maxHeight, Int((srcH * scale).rounded())))

            if scale < 0.999 || dstW != Int(srcW) || dstH != Int(srcH) {
                let sx = Double(dstW) / srcW
                let sy = Double(dstH) / srcH
                ciImage = ciImage.transformed(by: CGAffineTransform(scaleX: sx, y: sy))
                let scaledOrigin = ciImage.extent.origin
                if scaledOrigin.x != 0 || scaledOrigin.y != 0 {
                    ciImage = ciImage.transformed(
                        by: CGAffineTransform(translationX: -scaledOrigin.x, y: -scaledOrigin.y)
                    )
                }
            }

            let rowBytes = dstW * 4
            let rgbaCount = rowBytes * dstH
            let packetBytes = SharedFrameRing.headerBytes + rgbaCount
            let totalRecordBytes = FrameIPCConstants.tcpHeaderBytes + packetBytes
            guard totalRecordBytes <= frameBuffer.count else { return }
            let bounds = CGRect(x: 0, y: 0, width: dstW, height: dstH)
            let pixelOffset = FrameIPCConstants.tcpHeaderBytes + SharedFrameRing.headerBytes

            let curMeta = (rawW: rawW, rawH: rawH, rawOri: rpOrientation.rawValue, dstW: dstW, dstH: dstH)
            let metaChanged: Bool
            if let prevMeta = lastMeta,
               prevMeta.rawW == curMeta.rawW,
               prevMeta.rawH == curMeta.rawH,
               prevMeta.rawOri == curMeta.rawOri,
               prevMeta.dstW == curMeta.dstW,
               prevMeta.dstH == curMeta.dstH {
                metaChanged = false
            } else {
                metaChanged = true
                lastMeta = curMeta
                logger.info("Broadcast geometry: raw=\(rawW)x\(rawH) rpOri=\(rpOrientation.rawValue) ciOri=\(ciOrientation.rawValue) dst=\(dstW)x\(dstH)")
            }

            var loopbackRecordData: Data?
            var loopbackMetaData: Data?
            var skippedDuplicate = false

            frameBuffer.withUnsafeMutableBytes { rawPtr in
                guard let baseAddress = rawPtr.baseAddress else { return }
                let pixelPtr = baseAddress.advanced(by: pixelOffset)
                // CIContext.render(_:toBitmap:rowBytes:bounds:format:colorSpace:) writes top-down row order
                ciContext.render(
                    ciImage,
                    toBitmap: pixelPtr,
                    rowBytes: rowBytes,
                    bounds: bounds,
                    format: .RGBA8,
                    colorSpace: srgbColorSpace
                )

                let frameHash = Self.computeStridedPixelHash(pixelPtr, byteCount: rgbaCount)
                if hasSentFrame && !metaChanged && (dstW, dstH) == lastSentDims && frameHash == lastSentFrameHash && (now - lastSentFrameTs) < duplicateHeartbeatInterval {
                    skippedDuplicate = true
                    return
                }

                hasSentFrame = true
                lastSentFrameHash = frameHash
                lastSentDims = (dstW, dstH)
                lastSentFrameTs = now

                seq &+= 1
                if seq == 0 { seq = 1 }

                if let ring = ring {
                    ring.writeRGBAFrame(
                        width: dstW,
                        height: dstH,
                        rgbaSource: pixelPtr,
                        bytesPerRow: rowBytes,
                        seq: seq
                    )
                } else {
                    if metaChanged {
                        loopbackMetaData = FrameIPCConstants.makeMetaRecord(
                            rawW: rawW,
                            rawH: rawH,
                            rawOrientation: rpOrientation.rawValue,
                            dstW: dstW,
                            dstH: dstH
                        )
                    }
                    FrameIPCConstants.writeUInt32LE(FrameIPCConstants.recordMagic, to: baseAddress, offset: 0)
                    FrameIPCConstants.writeUInt32LE(FrameIPCConstants.msgFrame, to: baseAddress, offset: 4)
                    FrameIPCConstants.writeUInt32LE(UInt32(packetBytes), to: baseAddress, offset: 8)
                    FrameIPCConstants.writeUInt32LE(seq, to: baseAddress, offset: 12)
                    FrameIPCConstants.writeUInt32LE(UInt32(dstW), to: baseAddress, offset: 16)
                    FrameIPCConstants.writeUInt32LE(UInt32(dstH), to: baseAddress, offset: 20)
                    FrameIPCConstants.writeUInt32LE(4, to: baseAddress, offset: 24)
                    loopbackRecordData = Data(bytes: baseAddress, count: totalRecordBytes)
                }

                frameCount &+= 1
                if !loggedFirstFrame {
                    loggedFirstFrame = true
                    let mode = (ring != nil) ? "appGroup" : "loopback"
                    logger.info("Rendered first broadcast frame \(dstW)x\(dstH) via \(mode, privacy: .public) (rawOrientation=\(rpOrientation.rawValue), ciOrientation=\(ciOrientation.rawValue))")
                }
            }

            if skippedDuplicate { return }
            if let metaData = loopbackMetaData {
                loopbackSender.sendAuxiliaryRecord(metaData)
            }
            if let recordData = loopbackRecordData {
                loopbackSender.sendFrameRecord(recordData)
            }
        }
    }
}
