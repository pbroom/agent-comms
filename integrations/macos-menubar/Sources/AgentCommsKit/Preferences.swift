import Foundation

/// The app's settings, from UserDefaults (`defaults write dev.agentcomms.menubar port 8787`), or for one run
/// from launch arguments (`open AgentComms.app --args -port 8799`), which UserDefaults reads without saving.
public struct Preferences: Equatable, Sendable {
    public static let portKey = "port"
    public static let repoPathKey = "repoPath"
    public static let tokenFileKey = "tokenFile"
    public static let defaultPort = 8787
    public static let defaultRepoPath = "~/agent-comms"

    public var port: Int
    public var repoPath: String   // tilde-expanded

    public init(port: Int = Preferences.defaultPort, repoPath: String = Preferences.defaultRepoPath) {
        self.port = Endpoint.validPort(port) ?? Preferences.defaultPort
        self.repoPath = (repoPath as NSString).expandingTildeInPath
    }

    public static func load(_ defaults: UserDefaults = .standard) -> Preferences {
        let port = defaults.object(forKey: portKey) != nil ? defaults.integer(forKey: portKey) : defaultPort
        let repo = defaults.string(forKey: repoPathKey).flatMap { $0.isEmpty ? nil : $0 } ?? defaultRepoPath
        return Preferences(port: port, repoPath: repo)
    }
}

/// The only place URLs are built. The app talks to http://127.0.0.1:<port> and nothing else, and no URL ever
/// carries the token (it goes in the Authorization header; the dashboard keeps its own browser sign-in).
public struct Endpoint: Equatable, Sendable {
    public static let host = "127.0.0.1"
    public let port: Int

    public init(port: Int) {
        self.port = Endpoint.validPort(port) ?? Preferences.defaultPort
    }

    public static func validPort(_ port: Int) -> Int? {
        (1...65535).contains(port) ? port : nil
    }

    private func url(path: String, fragment: String? = nil) -> URL {
        var c = URLComponents()
        c.scheme = "http"
        c.host = Endpoint.host
        c.port = port
        c.path = path
        c.fragment = fragment
        return c.url!
    }

    public var base: URL { url(path: "/") }
    public var dashboard: URL { url(path: "/") }
    public var settings: URL { url(path: "/", fragment: "settings") }
    /// The plain dashboard URL for a page (no sign-in; the fallback when login links are unavailable).
    public func page(_ page: DashboardPage) -> URL { url(path: "/", fragment: page.fragment) }
    public var summary: URL { url(path: "/api/summary") }
    public var needsYou: URL { url(path: "/api/needs-you") }
    public var loginLinks: URL { url(path: "/api/login-links") }
    public func finalize(postId: Int) -> URL { url(path: "/api/posts/\(postId)/finalize") }
    public func transition(taskId: Int) -> URL { url(path: "/api/tasks/\(taskId)/transition") }
    public var pause: URL { url(path: "/api/admin/pause") }
    public var unpause: URL { url(path: "/api/admin/unpause") }
    /// The same route the dashboard's Settings page uses to stop the dispatcher.
    public var stopDispatcher: URL { url(path: "/api/admin/dispatch/stop") }

    /// True only for URLs this app may send the token to.
    public func isBoardURL(_ u: URL) -> Bool {
        u.scheme == "http" && u.host == Endpoint.host && u.port == port && u.user == nil && u.password == nil
            && u.query == nil
    }
}
