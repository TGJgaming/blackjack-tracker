import UIKit
import os

final class SceneDelegate: UIResponder, UIWindowSceneDelegate {
    var window: UIWindow?
    private let logger = Logger(subsystem: "com.tgjgaming.blackjacktracker", category: "Scene")

    func scene(
        _ scene: UIScene,
        willConnectTo session: UISceneSession,
        options connectionOptions: UIScene.ConnectionOptions
    ) {
        guard let windowScene = scene as? UIWindowScene else {
            logger.error("Expected UIWindowScene during scene connection")
            return
        }
        logger.info("Scene willConnectTo session")
        let window = UIWindow(windowScene: windowScene)
        window.backgroundColor = UIColor(red: 15.0 / 255.0, green: 17.0 / 255.0, blue: 21.0 / 255.0, alpha: 1.0)
        window.rootViewController = TrackerViewController()
        window.makeKeyAndVisible()
        self.window = window
        UIApplication.shared.isIdleTimerDisabled = true
    }

    func sceneDidBecomeActive(_ scene: UIScene) {
        UIApplication.shared.isIdleTimerDisabled = true
    }

    func sceneDidEnterBackground(_ scene: UIScene) {
        logger.info("Scene entered background")
    }
}
