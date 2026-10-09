import Foundation

/// The pasteboard calls `TokenCopier` needs, so it can be tested with a fake. `SystemPasteboard` is the real one.
public protocol TokenPasteboard: AnyObject {
    /// Goes up whenever anyone (this app or another) changes the pasteboard's contents.
    var changeCount: Int { get }
    /// Replaces the contents, in one write, with a single item holding `string` as plain text plus each of
    /// `markerTypes` (with empty data). Returns false if the write failed.
    func replaceContents(string: String, markerTypes: [String]) -> Bool
    /// Empties the pasteboard.
    func clearContents()
}

/// What "Copy board token" did. Its text is shown in the menu, so it never includes the token.
public enum TokenCopyResult: Equatable, CustomStringConvertible {
    case copied(clearsInSeconds: Int)
    case notCopied(reason: String)

    public var description: String {
        switch self {
        case .copied(let s): return "Board token copied — clears from the clipboard in \(s) s"
        case .notCopied(let reason): return "Could not copy the board token: \(reason)"
        }
    }
}

/// What happened when a copy's clear timer ran.
public enum TokenClearOutcome: Equatable, CustomStringConvertible {
    /// The pasteboard still held the token, and was cleared.
    case cleared
    /// Something else was copied since, so the pasteboard was left alone.
    case changedSince
    /// A later "Copy board token" owns the clear now.
    case superseded

    public var description: String {
        switch self {
        case .cleared: return "cleared"
        case .changedSince: return "left alone: the clipboard changed since"
        case .superseded: return "superseded by a later copy"
        }
    }
}

/// Copies the human token to the pasteboard for pasting into the dashboard's "Or paste a token" sign-in, marked
/// concealed and transient (the nspasteboard.org convention, so clipboard managers that honor it neither show nor
/// keep it), and clears it after `clearAfter` seconds, but only if the pasteboard still holds that copy (its
/// `changeCount` is unchanged), so it never wipes something the human copied afterwards.
///
/// The token is read through the caller's loader at copy time and kept nowhere here, so this object's
/// descriptions and mirror cannot reveal it. Use it from one thread (the app uses the main thread).
public final class TokenCopier: CustomStringConvertible {
    public static let concealedType = "org.nspasteboard.ConcealedType"
    public static let transientType = "org.nspasteboard.TransientType"
    public static let defaultClearAfter: TimeInterval = 60

    public typealias Scheduler = (_ seconds: TimeInterval, _ work: @escaping () -> Void) -> Void

    private let pasteboard: TokenPasteboard
    private let clearAfter: TimeInterval
    private let schedule: Scheduler
    /// The pasteboard's `changeCount` right after our latest copy, while that copy's clear is pending.
    private var pendingChangeCount: Int?

    public init(pasteboard: TokenPasteboard,
                clearAfter: TimeInterval = TokenCopier.defaultClearAfter,
                schedule: @escaping Scheduler = { seconds, work in
                    DispatchQueue.main.asyncAfter(deadline: .now() + seconds, execute: work)
                }) {
        self.pasteboard = pasteboard
        self.clearAfter = clearAfter
        self.schedule = schedule
    }

    /// Whether a copy of ours may still be on the pasteboard (its clear has not run yet).
    public var hasPendingClear: Bool { pendingChangeCount != nil }

    public var description: String { "TokenCopier(clearAfter: \(Int(clearAfter)) s, pending clear: \(hasPendingClear))" }

    /// Loads the token with `load` (the app passes its safe token-file loader) and copies it. If loading throws,
    /// the pasteboard is not touched and no clear is scheduled. `onClear` runs when this copy's timer fires.
    @discardableResult
    public func copy(load: () throws -> BearerToken,
                     onClear: @escaping (TokenClearOutcome) -> Void = { _ in }) -> TokenCopyResult {
        let token: BearerToken
        do {
            token = try load()
        } catch {
            return .notCopied(reason: String(describing: error))
        }
        guard pasteboard.replaceContents(string: token.secret,
                                         markerTypes: [Self.concealedType, Self.transientType]) else {
            return .notCopied(reason: "the clipboard refused the write")
        }
        let count = pasteboard.changeCount
        pendingChangeCount = count
        schedule(clearAfter) { [weak self] in
            guard let self else { return }
            onClear(self.clear(ifStillAt: count))
        }
        return .copied(clearsInSeconds: Int(clearAfter.rounded()))
    }

    /// Clears now if the pasteboard still holds our latest copy (for quitting before the timer runs).
    @discardableResult
    public func clearNowIfUnchanged() -> TokenClearOutcome? {
        guard let count = pendingChangeCount else { return nil }
        return clear(ifStillAt: count)
    }

    func clear(ifStillAt count: Int) -> TokenClearOutcome {
        guard pendingChangeCount == count else { return .superseded }
        pendingChangeCount = nil
        guard pasteboard.changeCount == count else { return .changedSince }
        pasteboard.clearContents()
        return .cleared
    }
}
