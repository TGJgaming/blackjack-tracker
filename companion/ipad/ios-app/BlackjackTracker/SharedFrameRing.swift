import Foundation
import CoreVideo
import CoreImage

/// Lock-free memory-mapped single-frame ring buffer shared via App Group container
/// between `BroadcastExtension` (writer, <50 MB RAM limit) and `BlackjackTracker` (reader).
///
/// Binary layout of `frame_ring.bin`:
/// - Bytes 0..3:   uint32 sequence number (incremented atomically after write)
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

    private var fd: Int32 = -1
    private var mappedPtr: UnsafeMutableRawPointer?

    init?(isWriter: Bool) {
        let baseDir: URL
        if let groupURL = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: Self.appGroupId) {
            baseDir = groupURL
        } else {
            baseDir = FileManager.default.temporaryDirectory
        }
        let fileURL = baseDir.appendingPathComponent(Self.fileName)
        let flags = isWriter ? (O_CREAT | O_RDWR) : O_RDONLY
        fd = open(fileURL.path, flags, S_IRUSR | S_IWUSR | S_IRGRP | S_IWGRP)
        if fd < 0 {
            return nil
        }
        if isWriter {
            _ = ftruncate(fd, off_t(Self.capacityBytes))
        } else {
            var st = stat()
            if fstat(fd, &st) != 0 || Int(st.st_size) < Self.headerBytes {
                close(fd)
                fd = -1
                return nil
            }
        }
        let prot = isWriter ? (PROT_READ | PROT_WRITE) : PROT_READ
        let ptr = mmap(nil, Self.capacityBytes, prot, MAP_SHARED, fd, 0)
        if ptr == MAP_FAILED {
            close(fd)
            fd = -1
            return nil
        }
        mappedPtr = ptr
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
        guard let base = mappedPtr else { return }
        let w = min(width, Self.maxWidth)
        let h = min(height, Self.maxHeight)
        let dstStride = w * 4
        if Self.headerBytes + (dstStride * h) > Self.capacityBytes { return }

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

        let hdr = base.assumingMemoryBound(to: UInt32.self)
        hdr[1] = UInt32(w)
        hdr[2] = UInt32(h)
        hdr[3] = 4
        OSMemoryBarrier()
        hdr[0] = seq
    }

    func readLatestPacket() -> Data? {
        guard let base = mappedPtr else { return nil }
        let hdr = base.assumingMemoryBound(to: UInt32.self)
        let seq1 = hdr[0]
        OSMemoryBarrier()
        let w = Int(hdr[1])
        let h = Int(hdr[2])
        let c = Int(hdr[3])
        if seq1 == 0 || w <= 0 || h <= 0 || w > Self.maxWidth || h > Self.maxHeight || c != 4 {
            return nil
        }
        let total = Self.headerBytes + (w * h * 4)
        let data = Data(bytes: base, count: total)
        OSMemoryBarrier()
        let seq2 = hdr[0]
        if seq1 != seq2 {
            // Writer updated mid-read; caller will pick up next frame on next poll
            return data
        }
        return data
    }
}
