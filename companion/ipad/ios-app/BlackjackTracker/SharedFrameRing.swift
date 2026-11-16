import Foundation
import Darwin
import os

/// Shared wire constants & little-endian header helpers for the App-Group-free loopback
/// fallback (`127.0.0.1:8766`) between `BroadcastExtension` and `BlackjackTracker`.
/// Compiled into both targets (`APPLICATION_EXTENSION_API_ONLY: YES`).
enum FrameIPCConstants {
    static let loopbackPort: UInt16 = 8766
    static let recordMagic: UInt32 = 0x424A4652 // "BJFR"
    static let msgFrame: UInt32 = 1
    static let msgBroadcastStarted: UInt32 = 2
    static let msgBroadcastFinished: UInt32 = 3
    static let tcpHeaderBytes: Int = 12 // [magic(4), msgType(4), payloadLen(4)]

    static func writeUInt32LE(_ value: UInt32, to base: UnsafeMutableRawPointer, offset: Int) {
        let le = value.littleEndian
        let dst = base.advanced(by: offset).assumingMemoryBound(to: UInt8.self)
        dst[0] = UInt8(truncatingIfNeeded: le)
        dst[1] = UInt8(truncatingIfNeeded: le >> 8)
        dst[2] = UInt8(truncatingIfNeeded: le >> 16)
        dst[3] = UInt8(truncatingIfNeeded: le >> 24)
    }

    static func readUInt32LE(from data: Data, offset: Int) -> UInt32 {
        let i = data.startIndex + offset
        let b0 = UInt32(data[i])
        let b1 = UInt32(data[i + 1]) << 8
        let b2 = UInt32(data[i + 2]) << 16
        let b3 = UInt32(data[i + 3]) << 24
        return b0 | b1 | b2 | b3
    }

    static func makeControlRecord(msgType: UInt32) -> Data {
        var bytes = [UInt8](repeating: 0, count: tcpHeaderBytes)
        bytes.withUnsafeMutableBytes { raw in
            guard let base = raw.baseAddress else { return }
            writeUInt32LE(recordMagic, to: base, offset: 0)
            writeUInt32LE(msgType, to: base, offset: 4)
            writeUInt32LE(0, to: base, offset: 8)
        }
        return Data(bytes)
    }
}

/// Lock-free memory-mapped single-frame ring buffer shared via App Group container
/// between `BroadcastExtension` (writer, <50 MB RAM limit) and `BlackjackTracker` (reader).
/// Returns `nil` when no App Group container is provisioned (e.g., free Apple ID sideloading)
/// so the caller uses the `127.0.0.1:8766` loopback TCP channel without wasting extension RAM.
///
/// Binary layout of `frame_ring.bin`:
/// - Bytes 0..3:   uint32 sequence number (0 while write in progress, non-zero after barrier)
/// - Bytes 4..7:   uint32 width in pixels
/// - Bytes 8..11:  uint32 height in pixels
/// - Bytes 12..15: uint32 channels (4 = RGBA)
/// - Bytes 16..N:  raw RGBA pixel bytes (width * height * 4)
final class SharedFrameRing {
    static let defaultAppGroupId = "group.com.tgjgaming.blackjacktracker"
    static let appGroupId = defaultAppGroupId
    static let fileName = "frame_ring.bin"
    static let headerBytes = 16
    static let maxWidth = 1280
    static let maxHeight = 960
    static let capacityBytes = headerBytes + (maxWidth * maxHeight * 4)

    private static let logger = Logger(subsystem: "com.tgjgaming.blackjacktracker", category: "SharedFrameRing")
    private static let resolveLock = NSLock()
    private static var didResolveGroup = false
    private static var cachedGroup: (id: String, url: URL)?
    private static var loggedUnavailable = false

    private var fd: Int32 = -1
    private var mappedPtr: UnsafeMutableRawPointer?
    let resolvedGroupId: String
    let storageDescription: String

    static var availableAppGroupId: String? {
        resolveAppGroupContainer()?.id
    }

    private static func provisionedAppGroupIds() -> [String] {
        guard let provUrl = Bundle.main.url(forResource: "embedded", withExtension: "mobileprovision"),
              let raw = try? Data(contentsOf: provUrl) else {
            return []
        }
        let startMarker = Data("<plist".utf8)
        let endMarker = Data("</plist>".utf8)
        guard let startRange = raw.range(of: startMarker),
              let endRange = raw.range(of: endMarker, options: [], in: startRange.lowerBound..<raw.endIndex) else {
            return []
        }
        let plistData = raw.subdata(in: startRange.lowerBound..<endRange.upperBound)
        guard let plist = try? PropertyListSerialization.propertyList(from: plistData, options: [], format: nil) as? [String: Any],
              let entitlements = plist["Entitlements"] as? [String: Any],
              let groups = entitlements["com.apple.security.application-groups"] as? [String] else {
            return []
        }
        return groups
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty && !$0.contains("*") }
    }

    static func candidateAppGroupIds() -> [String] {
        var candidates: [String] = []
        var seen = Set<String>()
        let appendUnique: (String) -> Void = { gid in
            guard !gid.isEmpty, !gid.contains("*"), seen.insert(gid).inserted else { return }
            candidates.append(gid)
        }

        appendUnique(defaultAppGroupId)

        let provisioned = provisionedAppGroupIds()
        for gid in provisioned where gid.localizedCaseInsensitiveContains("blackjacktracker") {
            appendUnique(gid)
        }
        if let rawBundleId = Bundle.main.bundleIdentifier, !rawBundleId.isEmpty {
            let mainBundleId: String
            if rawBundleId.hasSuffix(".BroadcastExtension") {
                mainBundleId = String(rawBundleId.dropLast(".BroadcastExtension".count))
            } else {
                mainBundleId = rawBundleId
            }
            if !mainBundleId.isEmpty {
                appendUnique("group.\(mainBundleId)")
            }
        }
        for gid in provisioned {
            appendUnique(gid)
        }
        return candidates
    }

    static func resolveAppGroupContainer() -> (id: String, url: URL)? {
        resolveLock.lock()
        defer { resolveLock.unlock() }
        if didResolveGroup {
            return cachedGroup
        }
        didResolveGroup = true

        let fm = FileManager.default
        for gid in candidateAppGroupIds() {
            if let groupURL = fm.containerURL(forSecurityApplicationGroupIdentifier: gid) {
                cachedGroup = (gid, groupURL)
                logger.info("Resolved App Group container '\(gid, privacy: .public)'")
                return cachedGroup
            }
        }
        if !loggedUnavailable {
            loggedUnavailable = true
            logger.notice("No App Group container available (free Apple ID / stripped entitlements); loopback IPC \(FrameIPCConstants.loopbackPort) will be used")
        }
        cachedGroup = nil
        return nil
    }

    init?(isWriter: Bool) {
        guard let resolved = Self.resolveAppGroupContainer() else {
            return nil
        }

        self.resolvedGroupId = resolved.id
        let fileURL = resolved.url.appendingPathComponent(Self.fileName)
        self.storageDescription = "appGroup:\(resolved.id)"

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

    func clear() {
        guard let base = mappedPtr else { return }
        let hdr = base.assumingMemoryBound(to: UInt32.self)
        hdr[0] = 0
        OSMemoryBarrier()
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

