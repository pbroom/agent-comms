import AgentCommsKit
import AppKit
import Foundation
import ServiceManagement

/// The app's state: the latest summary (or why there is none), polling, and the menu's actions.
@MainActor
final class BoardModel: ObservableObject {
    enum Phase: Equatable {
        case loading
        case online(BoardSummary)
        case offline
        case problem(String)
    }

    @Published private(set) var phase: Phase = .loading
    @Published private(set) var fetchedAt = Date()
    @Published private(set) var message: String?      // the result of the last action, shown in the menu
    @Published private(set) var startingServer = false
    @Published private(set) var launchAtLogin = false

    let prefs: Preferences
    let endpoint: Endpoint
    private let client: BoardClient
    private let tokenPath: String
    private var token: BearerToken?                    // memory only; never logged, shown or put in a URL
    private var menuOpen = false
    private var refreshing = false
    private var pollTask: Task<Void, Never>?
    private var observers: [NSObjectProtocol] = []

    static let pollSeconds: UInt64 = 10

    init() {
        prefs = Preferences.load()
        endpoint = Endpoint(port: prefs.port)
        client = BoardClient(endpoint: endpoint)
        tokenPath = TokenFile.path()
        launchAtLogin = loginItemAvailable && SMAppService.mainApp.status == .enabled
        observeMenu()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let model = self else { return }
                if !model.menuOpen {
                    await model.refresh()
                }
                try? await Task.sleep(nanoseconds: BoardModel.pollSeconds * 1_000_000_000)
            }
        }
    }

    var iconState: IconState {
        switch phase {
        case .loading: return IconState(base: .loading)
        case .offline: return IconState(base: .offline)
        case .problem: return IconState(base: .problem)
        case .online(let s): return IconState.from(summary: s)
        }
    }

    /// Seconds since the summary was fetched, to keep elapsed times current while the menu is open.
    var secondsSinceFetch: Int { max(0, Int(Date().timeIntervalSince(fetchedAt))) }

    // MARK: polling

    /// Refresh when the menu opens and pause the poll while it is open. A MenuBarExtra menu is an NSMenu, and
    /// this accessory app shows no other menus, so menu tracking means our menu.
    private func observeMenu() {
        let center = NotificationCenter.default
        observers.append(center.addObserver(forName: NSMenu.didBeginTrackingNotification, object: nil,
                                            queue: .main) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                self.menuOpen = true
                await self.refresh()
            }
        })
        observers.append(center.addObserver(forName: NSMenu.didEndTrackingNotification, object: nil,
                                            queue: .main) { [weak self] _ in
            Task { @MainActor in self?.menuOpen = false }
        })
    }

    private func loadToken() throws -> BearerToken {
        if let token { return token }
        let t = try TokenFile.load(path: tokenPath)
        token = t
        return t
    }

    func refresh() async {
        if refreshing { return }
        refreshing = true
        defer { refreshing = false }
        let token: BearerToken
        do {
            token = try loadToken()
        } catch {
            phase = .problem(String(describing: error))
            return
        }
        do {
            let s = try await client.summary(token: token)
            phase = .online(s)
            fetchedAt = Date()
            startingServer = false
        } catch let e as BoardClientError {
            if e == .unauthorized || e == .forbidden {
                self.token = nil          // re-read the file next time (it may have been rotated)
            }
            phase = e == .offline ? .offline : .problem(e.description)
        } catch {
            phase = .problem("Unexpected error")
        }
    }

    private func refreshSoon(after seconds: [Double]) {
        for s in seconds {
            Task { [weak self] in
                try? await Task.sleep(nanoseconds: UInt64(s * 1_000_000_000))
                await self?.refresh()
            }
        }
    }

    // MARK: actions

    func openDashboard() {
        NSWorkspace.shared.open(endpoint.dashboard)
    }

    func openSettings() {
        NSWorkspace.shared.open(endpoint.settings)
    }

    func pauseWithConfirmation() {
        later {
            guard self.confirm(title: "Pause the board?",
                               text: "Agents can still read, but every agent write is rejected until you unpause. "
                                   + "The dispatcher launches nothing while paused; agents already running are left alone.",
                               button: "Pause") else { return }
            await self.setPaused(true)
        }
    }

    func unpause() {
        later { await self.setPaused(false) }
    }

    private func setPaused(_ paused: Bool) async {
        do {
            try await client.setPaused(paused, token: try loadToken())
            message = paused ? "Board paused" : "Board unpaused"
        } catch {
            message = "\(paused ? "Pause" : "Unpause") failed: \(error)"
        }
        await refresh()
    }

    func stopDispatcherWithConfirmation() {
        later {
            guard self.confirm(title: "Stop the dispatcher?",
                               text: "The dispatcher stops launching agents, terminates the agents it started, and exits "
                                   + "(the same as `board dispatch stop`). Approvals stay as they are.",
                               button: "Stop Dispatcher") else { return }
            do {
                let requested = try await self.client.stopDispatcher(token: try self.loadToken())
                self.message = requested ? "Asked the dispatcher to stop" : "The dispatcher was not running"
            } catch {
                self.message = "Stop failed: \(error)"
            }
            await self.refresh()
            self.refreshSoon(after: [3, 8])
        }
    }

    func startServer() {
        guard !startingServer else { return }
        let launcher = ServerLauncher(repoPath: prefs.repoPath, port: prefs.port)
        do {
            try launcher.launch()
            startingServer = true
            message = "Starting the board server (log: \(abbreviate(launcher.logPath)))"
            refreshSoon(after: [1.5, 3, 5, 8, 12, 20])
            Task { [weak self] in
                try? await Task.sleep(nanoseconds: 25_000_000_000)
                guard let self, self.startingServer else { return }
                self.startingServer = false
                if self.phase == .offline {
                    self.message = "The board server did not start; see \(abbreviate(launcher.logPath))"
                }
            }
        } catch {
            message = String(describing: error)
        }
    }

    func clearMessage() { message = nil }

    // MARK: launch at login

    /// SMAppService.mainApp only works for an app bundle (not `swift run`).
    var loginItemAvailable: Bool {
        Bundle.main.bundleURL.pathExtension == "app" && Bundle.main.bundleIdentifier != nil
    }

    func setLaunchAtLogin(_ on: Bool) {
        guard loginItemAvailable else {
            message = "Launch at login works only when run as AgentComms.app"
            return
        }
        do {
            if on {
                try SMAppService.mainApp.register()
            } else {
                try SMAppService.mainApp.unregister()
            }
        } catch {
            message = "Launch at login: \(error.localizedDescription)"
        }
        let status = SMAppService.mainApp.status
        launchAtLogin = status == .enabled
        if status == .requiresApproval {
            message = "Allow Agent Comms in System Settings > General > Login Items"
        }
    }

    // MARK: helpers

    /// Run after the menu has closed, so an alert is not shown on top of a tracking menu.
    private func later(_ work: @escaping @MainActor () async -> Void) {
        DispatchQueue.main.async {
            Task { @MainActor in await work() }
        }
    }

    private func confirm(title: String, text: String, button: String) -> Bool {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = text
        alert.alertStyle = .warning
        alert.addButton(withTitle: button)
        alert.addButton(withTitle: "Cancel")
        return alert.runModal() == .alertFirstButtonReturn
    }
}

func abbreviate(_ path: String) -> String {
    (path as NSString).abbreviatingWithTildeInPath
}
