import Foundation
import CoreVideo
import CoreImage
import Darwin
import os

/// Lock-free memory-mapped single-frame ring buffer shared via App Group container
/// between `BroadcastExtension` (writer, <50 MB RAM limit) and `BlackjackTracker` (reader).
///
/// Binary layout of `frame_ring.bin`:
/// - Bytes 0..3:   uint32 sequence number (0 while write in progress, non-zero after barrier)
/// - Bytes 4..7:   uint32 width in pixels
/// - Bytes 8..11:  uint32 height in pixels
/// - Bytes 12..15: uint32 channels (4 = RGBA)
/// - Bytes 16..N:  raw RGBA pixel bytes (width * height * 4)
final class SharedFrameRing {
    static let appGroupId = "group.com.tgjgaming.blackjacktracker"
    static let fileName = "frame_ring.bin"
    static let headerBytes = 16
    static let maxWidth = 1280
    static let maxHeight = 960
    static let capacityBytes = headerBytes + (maxWidth * maxHeight * 4)

    private static let logger = Logger(subsystem: "com.tgjgaming.blackjacktracker", category: "SharedFrameRing")
    private static var loggedFallbackContainer = false

    private var fd: Int32 = -1
    private var mappedPtr: UnsafeMutableRawPointer?
    let storageDescription: String

    init?(isWriter: Bool) {
        let baseDir: URL
        let usingAppGroup: Bool
        if let groupURL = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: Self.appGroupId) {
            baseDir = groupURL
            usingAppGroup = true
        } else {
            baseDir = FileManager.default.temporaryDirectory
            usingAppGroup = false
            if !Self.loggedFallbackContainer {
                Self.loggedFallbackContainer = true
                Self.logger.notice("App Group '\(Self.appGroupId, privacy: .public)' unavailable (unsigned/entitlement missing); falling back to process temporary directory")
            }
        }

        let fileURL = baseDir.appendingPathComponent(Self.fileName)
        self.storageDescription = "\(usingAppGroup ? "appGroup" : "tmp"):\(fileURL.lastPathComponent)"

        let flags = isWriter ? (O_CREAT | O_RDWR) : O_RDONLY
        fd = open(fileURL.path, flags, S_IRUSR | S_IWUSR | S_IRGRP | S_IWGRP)
        if fd < 0 {
            let err = errno
            if err != ENOENT || isWriter {
                Self.logger.error("Failed to open ring file (\(self.storageDescription, privacy: .public)): errno=\(err)")
            }
            return nil
        }

        if isWriter {
            if ftruncate(fd, off_t(Self.capacityBytes)) != 0 {
                let err = errno
                Self.logger.error("ftruncate failed on \(self.storageDescription, privacy: .public): errno=\(err)")
                close(fd)
                fd = -1
                return nil
            }
        } else {
            var st = stat()
            if fstat(fd, &st) != 0 || Int(st.st_size) < Self.capacityBytes {
                close(fd)
                fd = -1
                return nil
            }
        }

        let prot = isWriter ? (PROT_READ | PROT_WRITE) : PROT_READ
        let ptr = mmap(nil, Self.capacityBytes, prot, MAP_SHARED, fd, 0)
        if ptr == MAP_FAILED {
            let err = errno
            Self.logger.error("mmap failed on \(self.storageDescription, privacy: .public): errno=\(err)")
            close(fd)
            fd = -1
            return nil
        }
        mappedPtr = ptr
        Self.logger.info("Opened SharedFrameRing (\(isWriter ? "writer" : "reader", privacy: .public)) at \(self.storageDescription, privacy: .public)")
    }

    deinit {
        if let ptr = mappedPtr {
            munmap(ptr, Self.capacityBytes)
        }
        if fd >= 0 {
            close(fd)
        }
    }

    func writeRGBAFrame(width: Int, height: Int, rgbaSource: UnsafeRawPointer, bytesPerRow: Int, seq: UInt32) {
        guard let base = mappedPtr, width > 0, height > 0 else { return }
        let w = min(width, Self.maxWidth)
        let h = min(height, Self.maxHeight)
        let dstStride = w * 4
        if Self.headerBytes + (dstStride * h) > Self.capacityBytes { return }

        let hdr = base.assumingMemoryBound(to: UInt32.self)
        let safeSeq: UInt32 = (seq == 0) ? 1 : seq

        // Seqlock write protocol: zero seq before writing so readers detect mid-write tearing
        hdr[0] = 0
        OSMemoryBarrier()

        hdr[1] = UInt32(w)
        hdr[2] = UInt32(h)
        hdr[3] = 4

        let dstPixels = base.advanced(by: Self.headerBytes)
        if bytesPerRow == dstStride {
            memcpy(dstPixels, rgbaSource, dstStride * h)
        } else {
            for row in 0..<h {
                let srcRow = rgbaSource.advanced(by: row * bytesPerRow)
                let dstRow = dstPixels.advanced(by: row * dstStride)
                memcpy(dstRow, srcRow, dstStride)
            }
        }

        OSMemoryBarrier()
        hdr[0] = safeSeq
    }

    func readLatestPacket() -> Data? {
        guard let base = mappedPtr else { return nil }
        let hdr = base.assumingMemoryBound(to: UInt32.self)

        for _ in 0..<3 {
            let seq1 = hdr[0]
            OSMemoryBarrier()
            if seq1 == 0 {
                sched_yield()
                continue
            }

            let w = Int(hdr[1])
            let h = Int(hdr[2])
            let c = Int(hdr[3])
            if w <= 0 || h <= 0 || w > Self.maxWidth || h > Self.maxHeight || c != 4 {
                return nil
            }
            let total = Self.headerBytes + (w * h * 4)
            if total > Self.capacityBytes {
                return nil
            }

            let data = Data(bytes: base, count: total)
            OSMemoryBarrier()
            let seq2 = hdr[0]
            if seq1 == seq2 {
                return data
            }
        }
        return nil
    }
}
