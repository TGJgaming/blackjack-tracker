import ReplayKit
import CoreImage
import CoreVideo

/// Lightweight ReplayKit Broadcast Upload Extension (`RPBroadcastSampleHandler`).
/// Strictly engineered to stay under 12 MB RAM (well below iPadOS's 50 MB extension limit):
/// - Throttles capture to 12 FPS
/// - Downscales incoming `CMSampleBuffer` to <= 1280px width using hardware CIContext
/// - Writes RGBA bytes directly into the memory-mapped App Group `SharedFrameRing`
final class BroadcastSampleHandler: RPBroadcastSampleHandler {
    private let ciContext = CIContext(options: [
        .useSoftwareRenderer: false,
        .cacheIntermediates: false
    ])
    private var ring: SharedFrameRing?
    private var seq: UInt32 = 0
    private var lastFrameTs: CFTimeInterval = 0
    private let minFrameInterval: CFTimeInterval = 1.0 / 12.0
    private var rgbaScratch = [UInt8](repeating: 0, count: SharedFrameRing.maxWidth * SharedFrameRing.maxHeight * 4)

    override func broadcastStarted(withSetupInfo setupInfo: [String: NSObject]?) {
        ring = SharedFrameRing(isWriter: true)
        seq = 0
        lastFrameTs = 0
    }

    override func broadcastPaused() {}

    override func broadcastResumed() {}

    override func broadcastFinished() {
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
            let srcW = CVPixelBufferGetWidth(pixelBuffer)
            let srcH = CVPixelBufferGetHeight(pixelBuffer)
            guard srcW > 0, srcH > 0 else { return }

            let scale = min(
                1.0,
                Double(SharedFrameRing.maxWidth) / Double(srcW),
                Double(SharedFrameRing.maxHeight) / Double(srcH)
            )
            let dstW = max(16, min(SharedFrameRing.maxWidth, Int((Double(srcW) * scale).rounded())))
            let dstH = max(16, min(SharedFrameRing.maxHeight, Int((Double(srcH) * scale).rounded())))

            var ciImage = CIImage(cvPixelBuffer: pixelBuffer)
            if scale < 0.999 {
                ciImage = ciImage.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
            }

            let rowBytes = dstW * 4
            let bounds = CGRect(x: 0, y: 0, width: dstW, height: dstH)
            let colorSpace = CGColorSpaceCreateDeviceRGB()

            rgbaScratch.withUnsafeMutableBytes { rawPtr in
                guard let baseAddress = rawPtr.baseAddress else { return }
                ciContext.render(
                    ciImage,
                    toBitmap: baseAddress,
                    rowBytes: rowBytes,
                    bounds: bounds,
                    format: .RGBA8,
                    colorSpace: colorSpace
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
            }
        }
    }
}
