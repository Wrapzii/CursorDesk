import Foundation
import Combine

@MainActor
final class DeskModel: ObservableObject {
    @Published var hostAddress: String
    @Published var projectLabel: String
    @Published var status: String = "Ready"
    @Published var showSettings = false

    private let defaults = UserDefaults.standard

    init() {
        hostAddress = defaults.string(forKey: "hostAddress") ?? ""
        projectLabel = defaults.string(forKey: "projectLabel") ?? "My Project / Cursor"
    }

    func save() {
        defaults.set(hostAddress.trimmingCharacters(in: .whitespacesAndNewlines), forKey: "hostAddress")
        defaults.set(projectLabel, forKey: "projectLabel")
        status = "Saved"
    }

    /// Prefer Moonlight deep link when installed; fall back to Sunshine web UI.
    func connectURL() -> URL? {
        let host = hostAddress.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !host.isEmpty else { return nil }

        // Moonlight custom URL schemes vary by build; host://IP is widely accepted.
        if let moon = URL(string: "moonlight://\(host)") {
            return moon
        }
        return URL(string: "https://\(host):47990")
    }

    func sunshineURL() -> URL? {
        let host = hostAddress.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !host.isEmpty else { return nil }
        return URL(string: "https://\(host):47990")
    }
}
