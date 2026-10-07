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
            Text(verbatim: text)
        case .online(let summary):
            OnlineSections(summary: summary, extraSeconds: model.secondsSinceFetch, model: model)
        }

        if let message = model.message {
            Divider()
            Button { model.clearMessage() } label: { Text(verbatim: message) }
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

/// One "Needs you" item: a submenu with its preview (plain text) and the actions that apply to it.
struct NeedsYouSubmenu: View {
    let item: NeedsYouItem
    let project: String?
    @ObservedObject var model: BoardModel

    var body: some View {
        Menu {
            let preview = Display.preview(item.preview)
            if !preview.isEmpty {
                Text(verbatim: preview)
                Divider()
            }
            ForEach(Array(ItemAction.available(for: item).enumerated()), id: \.offset) { _, action in
                Button {
                    model.perform(action, on: item, project: project)
                } label: {
                    Text(verbatim: Self.title(action))
                }
            }
        } label: {
            Text(verbatim: Display.needsYouTitle(item, project: project))
        }
    }

    static func title(_ action: ItemAction) -> String {
        switch action {
        case .view: return "View in Dashboard"
        case .finalizeDecision: return "Finalize Decision…"
        case .acceptTask(let id): return "Accept Task #\(id)…"
        }
    }
}

/// Counts and server-stamped identifiers only; every string comes from `Display`.
struct OnlineSections: View {
    let summary: BoardSummary
    let extraSeconds: Int
    @ObservedObject var model: BoardModel

    var body: some View {
        Text(verbatim: Display.statusLine(summary))
        Text(verbatim: Display.dispatcherLine(summary.dispatcher))

        Divider()
        if summary.needsYou.count == 0 {
            Text("Nothing needs you")
        } else {
            let entries = model.needsYouItems(summary)
            Section(header: Text(verbatim: "Needs you (\(summary.needsYou.count))")) {
                ForEach(entries, id: \.item.postId) { entry in
                    NeedsYouSubmenu(item: entry.item, project: entry.project, model: model)
                }
                if summary.needsYou.count > entries.count {
                    Button {
                        model.openDashboard()
                    } label: {
                        Text(verbatim: "… \(summary.needsYou.count - entries.count) more in the dashboard")
                    }
                }
            }
        }

        if !summary.dispatcher.runs.isEmpty {
            Divider()
            Section("Running agents") {
                ForEach(Array(summary.dispatcher.runs.enumerated()), id: \.offset) { _, run in
                    Text(verbatim: Display.run(run, in: summary, extraSeconds: extraSeconds))
                }
            }
        }

        if !summary.approvals.isEmpty {
            Divider()
            Section("Approvals") {
                ForEach(summary.approvals, id: \.ruleId) { a in
                    Text(verbatim: Display.approval(a, in: summary))
                }
            }
        }

        Divider()
        if summary.liveSessions.agents.isEmpty {
            Text("No live agent sessions (last \(summary.liveSessions.windowMinutes) min)")
        } else {
            Section("Live sessions (last \(summary.liveSessions.windowMinutes) min)") {
                ForEach(summary.liveSessions.agents, id: \.agent) { s in
                    Text(verbatim: Display.liveSession(s))
                }
            }
        }
    }
}
