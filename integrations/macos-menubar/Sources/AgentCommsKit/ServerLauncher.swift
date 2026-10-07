import Darwin
import Foundation

/// Finds `uv`. Apps started from Finder or at login get launchd's minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin),
/// so the usual install locations are checked first, then a search of a sane PATH.
public struct UVLocator {
    public var home: String
    public var environmentPath: String?
    public var isExecutable: (String) -> Bool

    public init(home: String = NSHomeDirectory(),
                environmentPath: String? = ProcessInfo.processInfo.environment["PATH"],
                isExecutable: @escaping (String) -> Bool = UVLocator.isExecutableFile) {
        self.home = home
        self.environmentPath = environmentPath
        self.isExecutable = isExecutable
    }

    public static func isExecutableFile(_ path: String) -> Bool {
        var st = stat()
        guard stat(path, &st) == 0, (st.st_mode & S_IFMT) == S_IFREG else { return false }
        return access(path, X_OK) == 0
    }

    public var candidates: [String] {
        ["/opt/homebrew/bin/uv", "/usr/local/bin/uv", "\(home)/.local/bin/uv"]
    }

    /// The PATH children get: the usual tool locations first, then whatever PATH the app was given.
    public var sanePath: String {
        var dirs = ["/opt/homebrew/bin", "/usr/local/bin", "\(home)/.local/bin", "\(home)/.cargo/bin",
                    "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        for d in (environmentPath ?? "").split(separator: ":").map(String.init)
        where d.hasPrefix("/") && !dirs.contains(d) {
            dirs.append(d)
        }
        return dirs.joined(separator: ":")
    }

    public func locate() -> String? {
        for c in candidates where isExecutable(c) {
            return c
        }
        for dir in sanePath.split(separator: ":") {
            let c = "\(dir)/uv"
            if isExecutable(c) { return c }
        }
        return nil
    }
}

public enum ServerLaunchError: Error, CustomStringConvertible, Equatable {
    case uvNotFound
    case repoNotFound(String)
    case logFile(String)
    case spawn(String)

    public var description: String {
        switch self {
        case .uvNotFound:
            return "Could not find uv (looked in /opt/homebrew/bin, /usr/local/bin, ~/.local/bin and PATH)"
        case .repoNotFound(let p):
            return "No agent-comms checkout at \(p). Set it with: defaults write dev.agentcomms.menubar repoPath /path/to/agent-comms"
        case .logFile(let m): return "Could not open the server log: \(m)"
        case .spawn(let m): return "Could not start the board server: \(m)"
        }
    }
}

/// Starts `uv run --project <repo> board serve --port <port>` detached from the app: explicit argv, no shell,
/// stdin from /dev/null, stdout and stderr appended to `<repo>/data/menubar-server.log` (mode 600).
public struct ServerLauncher {
    public var repoPath: String
    public var port: Int
    public var locator: UVLocator

    public init(repoPath: String, port: Int, locator: UVLocator = UVLocator()) {
        self.repoPath = repoPath
        self.port = port
        self.locator = locator
    }

    public var logPath: String { "\(repoPath)/data/menubar-server.log" }

    public func arguments() -> [String] {
        ["run", "--project", repoPath, "board", "serve", "--port", String(port)]
    }

    /// The app's environment with a sane PATH and without anything that looks like a credential: the server
    /// needs no token (it reads agents.toml), so none is passed on.
    public func environment(_ base: [String: String] = ProcessInfo.processInfo.environment) -> [String: String] {
        var env = base.filter { key, _ in
            let k = key.uppercased()
            return !(k.contains("TOKEN") || k.contains("SECRET") || k.contains("PASSWORD") || k.hasSuffix("_KEY"))
        }
        env["PATH"] = locator.sanePath
        env["HOME"] = env["HOME"] ?? locator.home
        return env
    }

    public func checkRepo() throws {
        var isDir: ObjCBool = false
        guard FileManager.default.fileExists(atPath: "\(repoPath)/pyproject.toml"),
              FileManager.default.fileExists(atPath: repoPath, isDirectory: &isDir), isDir.boolValue
        else { throw ServerLaunchError.repoNotFound(repoPath) }
    }

    /// Opens (creating if needed) the log file with mode 600, refusing a symlink.
    func openLog() throws -> FileHandle {
        let dataDir = "\(repoPath)/data"
        if !FileManager.default.fileExists(atPath: dataDir) {
            do {
                try FileManager.default.createDirectory(atPath: dataDir, withIntermediateDirectories: false,
                                                        attributes: [.posixPermissions: 0o700])
            } catch {
                throw ServerLaunchError.logFile(error.localizedDescription)
            }
        }
        let fd = open(logPath, O_WRONLY | O_CREAT | O_APPEND | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard fd >= 0 else { throw ServerLaunchError.logFile(String(cString: strerror(errno))) }
        var st = stat()
        guard fstat(fd, &st) == 0, (st.st_mode & S_IFMT) == S_IFREG, st.st_uid == getuid(),
              fchmod(fd, 0o600) == 0 else {
            close(fd)
            throw ServerLaunchError.logFile("\(logPath) is not a regular file you own")
        }
        return FileHandle(fileDescriptor: fd, closeOnDealloc: true)
    }

    /// Spawns the server and returns its pid. The app does not wait for it; it keeps running after the app quits.
    @discardableResult
    public func launch() throws -> Int32 {
        try checkRepo()
        guard let uv = locator.locate() else { throw ServerLaunchError.uvNotFound }
        let log = try openLog()
        let header = "\n--- \(ISO8601DateFormatter().string(from: Date())) started by the menu bar app: uv \(arguments().joined(separator: " "))\n"
        log.write(Data(header.utf8))

        let p = Process()
        p.executableURL = URL(fileURLWithPath: uv)
        p.arguments = arguments()
        p.environment = environment()
        p.currentDirectoryURL = URL(fileURLWithPath: repoPath)
        p.standardInput = FileHandle.nullDevice
        p.standardOutput = log
        p.standardError = log
        do {
            try p.run()
        } catch {
            throw ServerLaunchError.spawn(error.localizedDescription)
        }
        return p.processIdentifier
    }
}
