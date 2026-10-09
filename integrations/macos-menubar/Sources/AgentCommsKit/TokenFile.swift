import Darwin
import Foundation

/// The human's bearer token, held in memory only. Its descriptions and mirror are redacted so that printing,
/// logging, string-interpolating or `dump`ing it by mistake never reveals the value.
public struct BearerToken: Sendable, CustomStringConvertible, CustomDebugStringConvertible, CustomReflectable {
    private let value: String

    init(_ value: String) { self.value = value }

    /// The Authorization header value. Used only when building a request to 127.0.0.1.
    var headerValue: String { "Bearer \(value)" }

    /// The bare token. Used only by `TokenCopier`, which puts it on the pasteboard concealed and auto-cleared.
    var secret: String { value }

    public var description: String { "BearerToken(redacted)" }
    public var debugDescription: String { description }
    public var customMirror: Mirror { Mirror(self, children: [], displayStyle: .struct) }
}

public enum TokenFileError: Error, Equatable, CustomStringConvertible {
    case missing(path: String)
    case unreadable(path: String, errno: Int32)
    case notRegularFile(path: String)
    case notOwnedByUser(path: String)
    case tooPermissive(path: String, mode: UInt16)
    case malformed(path: String)

    public var description: String {
        switch self {
        case .missing(let p):
            return "No human token at \(p). Run `uv run board init` in the agent-comms repo."
        case .unreadable(let p, let e):
            return "Cannot read \(p): \(String(cString: strerror(e)))"
        case .notRegularFile(let p):
            return "\(p) must be a regular file (not a symlink, folder or pipe)."
        case .notOwnedByUser(let p):
            return "\(p) must be owned by you."
        case .tooPermissive(let p, let mode):
            return "\(p) has mode \(String(mode, radix: 8)); it must be 600 or stricter (chmod 600)."
        case .malformed(let p):
            return "\(p) is empty or malformed."
        }
    }
}

/// Reads the human token file with the same checks as the Python `load_agent_token`: a regular file owned by
/// this user, with no group or other permission bits (mode 600 or stricter), holding one token with no
/// whitespace. It opens the file without following a symlink and checks the open descriptor, so the file cannot
/// be swapped between the check and the read.
public enum TokenFile {
    /// `AGENT_COMMS_TOKEN_FILE` (as the Python CLI uses), then the `tokenFile` default, then
    /// `~/.config/agent-comms/human.token`.
    public static func path(environment: [String: String] = ProcessInfo.processInfo.environment,
                            defaults: UserDefaults = .standard) -> String {
        if let env = environment["AGENT_COMMS_TOKEN_FILE"], !env.isEmpty {
            return (env as NSString).expandingTildeInPath
        }
        if let d = defaults.string(forKey: Preferences.tokenFileKey), !d.isEmpty {
            return (d as NSString).expandingTildeInPath
        }
        return ("~/.config/agent-comms/human.token" as NSString).expandingTildeInPath
    }

    static let maxBytes = 4096

    public static func load(path: String, uid: uid_t = getuid()) throws -> BearerToken {
        let fd = open(path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)
        if fd < 0 {
            let e = errno
            if e == ENOENT { throw TokenFileError.missing(path: path) }
            if e == ELOOP { throw TokenFileError.notRegularFile(path: path) }   // a symlink
            throw TokenFileError.unreadable(path: path, errno: e)
        }
        defer { close(fd) }
        var st = stat()
        guard fstat(fd, &st) == 0 else { throw TokenFileError.unreadable(path: path, errno: errno) }
        guard (st.st_mode & S_IFMT) == S_IFREG else { throw TokenFileError.notRegularFile(path: path) }
        guard st.st_uid == uid else { throw TokenFileError.notOwnedByUser(path: path) }
        let mode = UInt16(st.st_mode) & 0o7777
        guard mode & 0o077 == 0 else { throw TokenFileError.tooPermissive(path: path, mode: mode) }
        guard st.st_size > 0, st.st_size <= maxBytes else { throw TokenFileError.malformed(path: path) }

        var buffer = [UInt8](repeating: 0, count: maxBytes)
        var total = 0
        while total < maxBytes {
            let n = buffer.withUnsafeMutableBytes { raw in
                read(fd, raw.baseAddress! + total, maxBytes - total)
            }
            if n < 0 { throw TokenFileError.unreadable(path: path, errno: errno) }
            if n == 0 { break }
            total += n
        }
        defer { _ = buffer.withUnsafeMutableBytes { memset_s($0.baseAddress, $0.count, 0, $0.count) } }
        guard let text = String(bytes: buffer[0..<total], encoding: .utf8) else {
            throw TokenFileError.malformed(path: path)
        }
        let token = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !token.isEmpty, !token.unicodeScalars.contains(where: {
            CharacterSet.whitespacesAndNewlines.contains($0) || CharacterSet.controlCharacters.contains($0)
        }) else { throw TokenFileError.malformed(path: path) }
        return BearerToken(token)
    }
}
