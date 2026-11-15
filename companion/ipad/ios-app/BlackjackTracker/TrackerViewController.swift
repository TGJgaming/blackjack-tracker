import UIKit
import WebKit
import ReplayKit
import Network

/// Custom URL scheme handler serving the latest downscaled ReplayKit broadcast frame
/// (`companion-frame://latest`) directly from the memory-mapped App Group file.
final class CompanionFrameSchemeHandler: NSObject, WKURLSchemeHandler {
    private var ring: SharedFrameRing?

    func webView(_ webView: WKWebView, start urlSchemeTask: WKURLSchemeTask) {
        if ring == nil {
            ring = SharedFrameRing(isWriter: false)
        }
        guard let url = urlSchemeTask.request.url else {
            urlSchemeTask.didFailWithError(URLError(.badURL))
            return
        }
        if let packet = ring?.readLatestPacket() {
            let resp = HTTPURLResponse(
                url: url,
                statusCode: 200,
                httpVersion: "HTTP/1.1",
                headerFields: [
                    "Content-Type": "application/octet-stream",
                    "Cache-Control": "no-store",
                    "Access-Control-Allow-Origin": "*"
                ]
            )!
            urlSchemeTask.didReceive(resp)
            urlSchemeTask.didReceive(packet)
            urlSchemeTask.didFinish()
        } else {
            let resp = HTTPURLResponse(
                url: url,
                statusCode: 204,
                httpVersion: "HTTP/1.1",
                headerFields: ["Access-Control-Allow-Origin": "*"]
            )!
            urlSchemeTask.didReceive(resp)
            urlSchemeTask.didFinish()
        }
    }

    func webView(_ webView: WKWebView, stop urlSchemeTask: WKURLSchemeTask) {}
}

/// Lightweight local WebSocket relay (`ws://127.0.0.1:8765`) so the optional Safari Keep-Alive
/// Web Extension (`safari-web-extension://...`) can receive `{ "type": "ext_reload" }` commands
/// during Watch-mode Place Bets windows without any DOM/content-script injection on Playtech.
final class KeepaliveExtensionRelay {
    private var listener: NWListener?
    private var extConnections: [NWConnection] = []
    var onConnectionCountChanged: ((Int) -> Void)?

    func start(port: UInt16 = 8765) {
        guard listener == nil else { return }
        let params = NWParameters.tcp
        let wsOptions = NWProtocolWebSocket.Options()
        wsOptions.autoReplyPing = true
        params.defaultProtocolStack.applicationProtocols.insert(wsOptions, at: 0)

        guard let nwPort = NWEndpoint.Port(rawValue: port),
              let listener = try? NWListener(using: params, on: nwPort) else {
            return
        }
        listener.newConnectionHandler = { [weak self] conn in
            self?.accept(conn)
        }
        listener.start(queue: .main)
        self.listener = listener
    }

    private func accept(_ conn: NWConnection) {
        extConnections.append(conn)
        onConnectionCountChanged?(extConnections.count)
        conn.stateUpdateHandler = { [weak self, weak conn] state in
            guard let self = self, let conn = conn else { return }
            switch state {
            case .failed, .cancelled:
                self.extConnections.removeAll { $0 === conn }
                self.onConnectionCountChanged?(self.extConnections.count)
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

final class TrackerViewController: UIViewController, WKScriptMessageHandler {
    private var webView: WKWebView!
    private var broadcastPicker: RPSystemBroadcastPickerView?
    private let frameSchemeHandler = CompanionFrameSchemeHandler()
    private let extRelay = KeepaliveExtensionRelay()

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = UIColor(red: 0.04, green: 0.05, blue: 0.08, alpha: 1.0)

        let config = WKWebViewConfiguration()
        config.setURLSchemeHandler(frameSchemeHandler, forURLScheme: "companion-frame")
        config.userContentController.add(self, name: "ipadCompanion")
        config.allowsInlineMediaPlayback = true

        webView = WKWebView(frame: view.bounds, configuration: config)
        webView.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        webView.isOpaque = false
        webView.backgroundColor = view.backgroundColor
        view.addSubview(webView)

        setupBroadcastPicker()
        setupKeepaliveRelay()
        loadTrackerApp()
    }

    private func setupBroadcastPicker() {
        let picker = RPSystemBroadcastPickerView(frame: CGRect(x: -100, y: -100, width: 44, height: 44))
        picker.preferredExtension = "com.tgjgaming.blackjacktracker.BroadcastExtension"
        picker.showsMicrophoneButton = false
        view.addSubview(picker)
        self.broadcastPicker = picker
    }

    private func setupKeepaliveRelay() {
        extRelay.onConnectionCountChanged = { [weak self] count in
            let connected = count > 0 ? "true" : "false"
            self?.webView.evaluateJavaScript("window.__ipadCompanionSetExtConnected && window.__ipadCompanionSetExtConnected(\(connected));", completionHandler: nil)
        }
        extRelay.start(port: 8765)
    }

    private func loadTrackerApp() {
        // Prepare a staging directory in Caches so relative script paths
        // (`companion/ipad/default_pack.js` and `companion/ipad/companion_engine.js`) resolve cleanly.
        let fm = FileManager.default
        let stageRoot = fm.temporaryDirectory.appendingPathComponent("tracker_web", isDirectory: true)
        let ipadSubdir = stageRoot.appendingPathComponent("companion/ipad", isDirectory: true)
        try? fm.createDirectory(at: ipadSubdir, withIntermediateDirectories: true)

        if let indexSrc = Bundle.main.url(forResource: "index", withExtension: "html") {
            let indexDst = stageRoot.appendingPathComponent("index.html")
            try? fm.removeItem(at: indexDst)
            try? fm.copyItem(at: indexSrc, to: indexDst)
        }
        for jsName in ["default_pack", "companion_engine"] {
            if let jsSrc = Bundle.main.url(forResource: jsName, withExtension: "js") {
                let jsDst = ipadSubdir.appendingPathComponent("\(jsName).js")
                try? fm.removeItem(at: jsDst)
                try? fm.copyItem(at: jsSrc, to: jsDst)
            }
        }

        let stagedIndex = stageRoot.appendingPathComponent("index.html")
        if fm.fileExists(atPath: stagedIndex.path) {
            webView.loadFileURL(stagedIndex, allowingReadAccessTo: stageRoot)
        }
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "ipadCompanion",
              let body = message.body as? [String: Any],
              let action = body["action"] as? String else {
            return
        }
        switch action {
        case "startBroadcast":
            guard let picker = broadcastPicker else { return }
            for subview in picker.subviews {
                if let btn = subview as? UIButton {
                    btn.sendActions(for: .allTouchEvents)
                    break
                }
            }
        case "reloadLeftSafari":
            _ = extRelay.broadcastReload()
        default:
            break
        }
    }
}
