import AgentCommsKit
import AppKit

/// Draws the menu bar icon from SF Symbols as one template image, so macOS tints it for a light or dark menu bar:
/// the base symbol (dimmed when offline), the needs-you count, and a bolt while dispatched agents run.
enum StatusIcon {
    static let height: CGFloat = 18

    static func image(for state: IconState) -> NSImage {
        let base = symbol(state.symbolName, pointSize: 14, weight: .regular)
            ?? symbol("circle", pointSize: 14, weight: .regular)
            ?? NSImage(size: NSSize(width: 16, height: 16))
        let bolt = state.agentsRunning ? symbol("bolt.fill", pointSize: 9, weight: .bold) : nil
        let badge = state.badge.map {
            NSAttributedString(string: $0, attributes: [
                .font: NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .semibold),
                .foregroundColor: NSColor.black,
            ])
        }

        var width = base.size.width
        if let badge { width += 2 + ceil(badge.size().width) }
        if let bolt { width += 1 + bolt.size.width }

        let image = NSImage(size: NSSize(width: ceil(width), height: height), flipped: false) { _ in
            var x: CGFloat = 0
            base.draw(in: NSRect(x: x, y: (height - base.size.height) / 2, width: base.size.width,
                                 height: base.size.height),
                      from: .zero, operation: .sourceOver, fraction: state.dimmed ? 0.35 : 1)
            x += base.size.width
            if let badge {
                x += 2
                let size = badge.size()
                badge.draw(at: NSPoint(x: x, y: (height - size.height) / 2))
                x += ceil(size.width)
            }
            if let bolt {
                x += 1
                bolt.draw(in: NSRect(x: x, y: (height - bolt.size.height) / 2, width: bolt.size.width,
                                     height: bolt.size.height),
                          from: .zero, operation: .sourceOver, fraction: 1)
            }
            return true
        }
        image.isTemplate = true
        image.accessibilityDescription = state.accessibilityLabel
        return image
    }

    private static func symbol(_ name: String, pointSize: CGFloat, weight: NSFont.Weight) -> NSImage? {
        NSImage(systemSymbolName: name, accessibilityDescription: nil)?
            .withSymbolConfiguration(NSImage.SymbolConfiguration(pointSize: pointSize, weight: weight))
    }
}
