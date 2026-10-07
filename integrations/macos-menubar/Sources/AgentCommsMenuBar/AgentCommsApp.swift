import AgentCommsKit
import AppKit
import SwiftUI

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationWillFinishLaunching(_ notification: Notification) {
        // No Dock icon or app menu, also when run outside the bundle (where LSUIElement does not apply).
        NSApp.setActivationPolicy(.accessory)
    }
}

@main
struct AgentCommsApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var model = BoardModel()

    var body: some Scene {
        MenuBarExtra {
            MenuContent(model: model)
        } label: {
            let state = model.iconState
            Image(nsImage: StatusIcon.image(for: state))
                .accessibilityLabel(state.accessibilityLabel)
        }
        .menuBarExtraStyle(.menu)
    }
}

struct MenuContent: View {
    @ObservedObject var model: BoardModel

    var body: some View {
        switch model.phase {
        case .loading:
            Text("Checking the board…")
        case .offline:
            Text("Board server not running")
            Button(model.startingServer ? "Starting Board Server…" : "Start Board Server") {
                model.startServer()
            }
            .disabled(model.startingServer)
        case .problem(let text):
            Text(text)
        case .online(let summary):
            OnlineSections(summary: summary, extraSeconds: model.secondsSinceFetch, model: model)
        }

        if let message = model.message {
            Divider()
            Button(message) { model.clearMessage() }
        }

        Divider()
        Button("Open Dashboard") { model.openDashboard() }
        Button("Open Settings") { model.openSettings() }
        if case .online(let summary) = model.phase {
            if summary.paused {
                Button("Unpause Board") { model.unpause() }
            } else {
                Button("Pause Board…") { model.pauseWithConfirmation() }
            }
            if summary.dispatcher.running {
                Button("Stop Dispatcher…") { model.stopDispatcherWithConfirmation() }
            }
        }
        Button("Refresh") { Task { await model.refresh() } }
            .keyboardShortcut("r")

        Divider()
        Toggle("Launch at Login", isOn: Binding(get: { model.launchAtLogin },
                                                set: { model.setLaunchAtLogin($0) }))
            .disabled(!model.loginItemAvailable)
        Button("Quit Agent Comms") { NSApp.terminate(nil) }
            .keyboardShortcut("q")
    }
}

/// Counts and server-stamped identifiers only; every string comes from `Display`.
struct OnlineSections: View {
    let summary: BoardSummary
    let extraSeconds: Int
    @ObservedObject var model: BoardModel

    var body: some View {
        Text(Display.statusLine(summary))
        Text(Display.dispatcherLine(summary.dispatcher))

        Divider()
        if summary.needsYou.count == 0 {
            Text("Nothing needs you")
        } else {
            Section("Needs you (\(summary.needsYou.count))") {
                ForEach(summary.needsYou.items, id: \.postId) { item in
                    Button(Display.needsYouItem(item, in: summary)) { model.openDashboard() }
                }
                if summary.needsYou.count > summary.needsYou.items.count {
                    Button("… \(summary.needsYou.count - summary.needsYou.items.count) more in the dashboard") {
                        model.openDashboard()
                    }
                }
            }
        }

        if !summary.dispatcher.runs.isEmpty {
            Divider()
            Section("Running agents") {
                ForEach(Array(summary.dispatcher.runs.enumerated()), id: \.offset) { _, run in
                    Text(Display.run(run, in: summary, extraSeconds: extraSeconds))
                }
            }
        }

        if !summary.approvals.isEmpty {
            Divider()
            Section("Approvals") {
                ForEach(summary.approvals, id: \.ruleId) { a in
                    Text(Display.approval(a, in: summary))
                }
            }
        }

        Divider()
        if summary.liveSessions.agents.isEmpty {
            Text("No live agent sessions (last \(summary.liveSessions.windowMinutes) min)")
        } else {
            Section("Live sessions (last \(summary.liveSessions.windowMinutes) min)") {
                ForEach(summary.liveSessions.agents, id: \.agent) { s in
                    Text(Display.liveSession(s))
                }
            }
        }
    }
}
