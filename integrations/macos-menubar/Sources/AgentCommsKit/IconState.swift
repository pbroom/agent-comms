import Foundation

/// What the menu bar icon shows. The app draws it as a template image from SF Symbols, so it follows the
/// menu bar's light or dark appearance.
public struct IconState: Equatable, Sendable {
    public enum Base: Equatable, Sendable {
        case loading
        case offline      // the server did not answer
        case problem      // token missing or refused, or an unexpected answer
        case paused
        case normal
        case needsYou
    }

    public var base: Base
    public var count: Int          // needs-you count (0: no badge)
    public var agentsRunning: Bool // dispatched agents running now

    public init(base: Base, count: Int = 0, agentsRunning: Bool = false) {
        self.base = base
        self.count = count
        self.agentsRunning = agentsRunning
    }

    public static func from(summary s: BoardSummary) -> IconState {
        let count = max(0, s.needsYou.count)
        let base: Base = s.paused ? .paused : (count > 0 ? .needsYou : .normal)
        return IconState(base: base, count: count, agentsRunning: !s.dispatcher.runs.isEmpty)
    }

    /// The SF Symbol for the base state (all available on macOS 13).
    public var symbolName: String {
        switch base {
        case .loading, .normal: return "bubble.left.and.bubble.right"
        case .needsYou: return "bubble.left.and.bubble.right.fill"
        case .paused: return "pause.circle"
        case .offline: return "bubble.left.and.bubble.right"
        case .problem: return "exclamationmark.triangle"
        }
    }

    /// Dimmed (like a disconnected Wi-Fi icon) when the server is offline or still loading.
    public var dimmed: Bool { base == .offline || base == .loading }

    /// The badge text: the needs-you count, capped.
    public var badge: String? {
        guard count > 0 else { return nil }
        return count > 99 ? "99+" : String(count)
    }

    public var accessibilityLabel: String {
        var parts: [String]
        switch base {
        case .loading: parts = ["Agent Comms: loading"]
        case .offline: parts = ["Agent Comms: board server not running"]
        case .problem: parts = ["Agent Comms: needs attention"]
        case .paused: parts = ["Agent Comms: board paused"]
        case .normal, .needsYou: parts = ["Agent Comms"]
        }
        if count > 0 { parts.append("\(count) need\(count == 1 ? "s" : "") you") }
        if agentsRunning { parts.append("dispatched agents running") }
        return parts.joined(separator: ", ")
    }
}
