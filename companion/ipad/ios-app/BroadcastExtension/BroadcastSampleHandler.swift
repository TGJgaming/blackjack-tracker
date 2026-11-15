import ReplayKit
import CoreImage
import CoreVideo
import ImageIO
import QuartzCore
import os

/// Lightweight ReplayKit Broadcast Upload Extension (`RPBroadcastSampleHandler`).
/// Strictly engineered to stay under 12 MB RAM (well below iPadOS's 50 MB extension limit):
/// - Throttles capture to 12 FPS
/// - Applies `RPVideoSampleOrientationKey` and normalizes extent origin before scaling
/// - Downscales incoming `CMSampleBuffer` to <= 1280x960 using hardware CIContext
/// - Writes RGBA bytes directly into the memory-mapped App Group `SharedFrameRing`
final class BroadcastSampleHandler: RPBroadcastSampleHandler {
    private let logger = Logger(subsystem: "com.tgjgaming.blackjacktracker.BroadcastExtension", category: "Broadcast")
    private let ciContext = CIContext(options: [
        .useSoftwareRenderer: false,
        .cacheIntermediates: false
    ])
    private let srgbColorSpace = CGColorSpace(name: CGColorSpace.sRGB) ?? CGColorSpaceCreateDeviceRGB()

    private var ring: SharedFrameRing?
    private var seq: UInt32 = 0
    private var frameCount: UInt64 = 0
    private var lastFrameTs: CFTimeInterval = 0
    private let minFrameInterval: CFTimeInterval = 1.0 / 12.0
    private var loggedFirstFrame = false
    private var rgbaScratch = [UInt8](repeating: 0, count: SharedFrameRing.maxWidth * SharedFrameRing.maxHeight * 4)

    override func broadcastStarted(withSetupInfo setupInfo: [String: NSObject]?) {
        logger.info("Broadcast started")
        ring = SharedFrameRing(isWriter: true)
        if let ring = ring {
            logger.info("Broadcast SharedFrameRing writer ready: \(ring.storageDescription, privacy: .public)")
        } else {
            logger.error("Failed to open SharedFrameRing writer on broadcast start")
        }
        seq = 0
        frameCount = 0
        lastFrameTs = 0
        loggedFirstFrame = false
    }

    override func broadcastPaused() {
        logger.info("Broadcast paused")
    }

    override func broadcastResumed() {
        logger.info("Broadcast resumed")
    }

    override func broadcastFinished() {
        logger.info("Broadcast finished after \(self.frameCount) rendered frame(s)")
        ring = nil
    }

    override func processSampleBuffer(_ sampleBuffer: CMSampleBuffer, with sampleBufferType: RPSampleBufferType) {
        guard sampleBufferType == .video else { return }
        let now = CACurrentMediaTime()
        if now - lastFrameTs < minFrameInterval { return }
        lastFrameTs = now

        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        if ring == nil {
            ring = SharedFrameRing(isWriter: true)
        }
        guard let ring = ring else { return }

        autoreleasepool {
            var ciImage = CIImage(cvPixelBuffer: pixelBuffer)

            var orientation: CGImagePropertyOrientation = .up
            if let orientationAttachment = CMGetAttachment(
                sampleBuffer,
                key: RPVideoSampleOrientationKey as CFString,
                attachmentModeOut: nil
            ) as? NSNumber,
               let parsedOrientation = CGImagePropertyOrientation(rawValue: orientationAttachment.uint32Value) {
                orientation = parsedOrientation
            }

            if orientation != .up {
                ciImage = ciImage.oriented(orientation)
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
            let bounds = CGRect(x: 0, y: 0, width: dstW, height: dstH)

            rgbaScratch.withUnsafeMutableBytes { rawPtr in
                guard let baseAddress = rawPtr.baseAddress else { return }
                // CIContext.render(_:toBitmap:rowBytes:bounds:format:colorSpace:) writes top-down row order
                ciContext.render(
                    ciImage,
                    toBitmap: baseAddress,
                    rowBytes: rowBytes,
                    bounds: bounds,
                    format: .RGBA8,
                    colorSpace: srgbColorSpace
                )
                seq &+= 1
                if seq == 0 { seq = 1 }
                ring.writeRGBAFrame(
                    width: dstW,
                    height: dstH,
                    rgbaSource: baseAddress,
                    bytesPerRow: rowBytes,
                    seq: seq
                )
                frameCount &+= 1
                if !loggedFirstFrame {
                    loggedFirstFrame = true
                    logger.info("Rendered first broadcast frame \(dstW)x\(dstH) (rawOrientation=\(orientation.rawValue))")
                }
            }
        }
    }
}
