import UIKit
import os

@main
final class AppDelegate: UIResponder, UIApplicationDelegate {
    private let logger = Logger(subsystem: "com.tgjgaming.blackjacktracker", category: "Lifecycle")

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        logger.info("BlackjackTracker launched")
        // Prevent iPad screen auto-lock while tracking a live Playtech shoe
        application.isIdleTimerDisabled = true
        return true
    }

    func application(
        _ application: UIApplication,
        configurationForConnecting connectingSceneSession: UISceneSession,
        options: UIScene.ConnectionOptions
    ) -> UISceneConfiguration {
        let config = UISceneConfiguration(
            name: "Default Configuration",
            sessionRole: connectingSceneSession.role
        )
        config.delegateClass = SceneDelegate.self
        return config
    }
}
